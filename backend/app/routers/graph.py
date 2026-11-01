"""Grafo de nodos del vault: `GET /graph`.

Sirve la tabla `links` ya resuelta, en los dos modos que la interfaz necesita:

- **local**: la página pedida y sus vecinos a `depth` saltos (1 o 2), con las
  aristas que quedan dentro de esa vecindad. Es el modo por defecto cuando hay
  una nota abierta: el grafo entero de un vault grande no cabe en la pantalla
  ni en la cabeza.
- **global**: los `limit` nodos de mayor grado del índice y las aristas cuyos
  **dos** extremos están dentro del corte. Cortar solo los nodos dejaría
  aristas colgando hacia la nada, que es peor que no pintarlas.

Decisiones que la respuesta hace explícitas:

- **Enlaces rotos**: se excluyen del grafo y se cuentan en `broken`. El índice
  no guarda filas para un `[[Título]]` que no resuelve (`sync_wikilinks` solo
  inserta destinos existentes), así que el recuento se hace releyendo el
  cuerpo de los nodos devueltos y comparando sus wikilinks con los títulos del
  workspace. Se cuenta **una vez por nota y título distinto** (sin distinguir
  mayúsculas, igual que la resolución). Un nodo fantasma sin título ni
  contenido confundiría más que ayudar; el aviso va en la interfaz.
- **`truncated`** solo puede ser cierto en `global`: dice que se han dejado
  nodos fuera por el tope. En `local` la vecindad se sirve entera.
- **`degree`** es el número de filas de `links` que tocan la página, en
  cualquier dirección. Mismo número en los dos modos, para que el tamaño del
  nodo no baile al cambiar de vista.

Los tipos de página no se filtran: un wikilink puede apuntar a una base de
datos (o, de forma rara, a una fila) y esconderla sería mentir sobre el grafo.
La interfaz colorea cada tipo y deja las filas en un color apagado.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app import schemas
from app.dependencies import get_repo
from app.links import fold_title
from app.repository import Repository
from app.wikilinks import extract_titles

router = APIRouter(tags=["graph"])

DEFAULT_GLOBAL_LIMIT = 400
MAX_GLOBAL_LIMIT = 800
MAX_LOCAL_DEPTH = 2

# Tope de parámetros por consulta. SQLite admite 999 variables desde la 3.27
# (la versión mínima que exige el índice), así que se trocea muy por debajo en
# vez de apostar por el límite alto de versiones más nuevas.
_PARAM_CHUNK = 400

# La vecindad se recorre en Python, un salto por consulta: como `depth` es 1 o
# 2, son como mucho cuatro consultas por índice (`links_from` / `links_to`) y
# no hace falta una CTE recursiva que SQLite tenga que planificar distinto en
# cada versión. El troceo mantiene el número de parámetros acotado aunque un
# nodo tenga miles de enlaces.
_NEIGHBORS_OUT = "SELECT to_id AS id FROM links WHERE from_id IN ({marks})"
_NEIGHBORS_IN = "SELECT from_id AS id FROM links WHERE to_id IN ({marks})"

# Grado de todo el índice en una pasada (`UNION ALL` + `GROUP BY`, no una
# subconsulta por página). El desempate por `updated_at` y `id` hace la
# respuesta determinista aunque el tope caiga justo en un empate de grado.
_GLOBAL_NODES_SQL = """
WITH incident AS (
    SELECT from_id AS id FROM links
    UNION ALL
    SELECT to_id AS id FROM links
), degree AS (
    SELECT id, count(*) AS degree FROM incident GROUP BY id
)
SELECT p.id, p.title, p.type, p.updated_at, COALESCE(degree.degree, 0) AS degree
  FROM pages p
  LEFT JOIN degree ON degree.id = p.id
 ORDER BY degree DESC, p.updated_at DESC, p.id
 LIMIT :limit
"""

_LOCAL_NODES_SQL = """
SELECT p.id, p.title, p.type, p.updated_at,
       (SELECT count(*) FROM links l WHERE l.from_id = p.id)
     + (SELECT count(*) FROM links l WHERE l.to_id = p.id) AS degree
  FROM pages p
 WHERE p.id IN ({marks})
 ORDER BY degree DESC, p.title COLLATE NOCASE, p.id
"""


def _as_node(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "type": row["type"],
        "degree": row["degree"],
        "updated_at": row["updated_at"],
    }


def _as_edge(row: sqlite3.Row) -> dict:
    return {"source": row["source"], "target": row["target"], "kind": row["kind"]}


def _neighbors(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    """Vecinos a un salto, en las dos direcciones."""
    found: set[str] = set()
    for start in range(0, len(ids), _PARAM_CHUNK):
        chunk = ids[start : start + _PARAM_CHUNK]
        marks = ",".join("?" for _ in chunk)
        found.update(r["id"] for r in conn.execute(_NEIGHBORS_OUT.format(marks=marks), chunk))
        found.update(r["id"] for r in conn.execute(_NEIGHBORS_IN.format(marks=marks), chunk))
    return found


def _local_ids(conn: sqlite3.Connection, start: str, depth: int) -> list[str]:
    """La página y todo lo alcanzable en `depth` saltos."""
    seen = {start}
    frontier = [start]
    for _ in range(depth):
        frontier = sorted(_neighbors(conn, frontier) - seen)
        if not frontier:
            break
        seen.update(frontier)
    return sorted(seen)


def _nodes_by_id(conn: sqlite3.Connection, ids: list[str]) -> list[dict]:
    nodes: list[dict] = []
    for start in range(0, len(ids), _PARAM_CHUNK):
        chunk = ids[start : start + _PARAM_CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(_LOCAL_NODES_SQL.format(marks=marks), chunk).fetchall()
        nodes.extend(_as_node(r) for r in rows)
    nodes.sort(key=lambda n: (-n["degree"], n["title"].casefold(), n["id"]))
    return nodes


def _edges_among(conn: sqlite3.Connection, node_ids: list[str]) -> list[dict]:
    """Aristas con los dos extremos dentro del conjunto.

    Se pide por bloques de `from_id` (índice `links_from`) y se filtra el
    destino en memoria: así el número de parámetros nunca depende del tamaño
    del grafo y no hace falta una tabla temporal.
    """
    wanted = set(node_ids)
    edges: list[dict] = []
    for start in range(0, len(node_ids), _PARAM_CHUNK):
        chunk = node_ids[start : start + _PARAM_CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT l.from_id AS source, l.to_id AS target, l.kind FROM links l"
            f" WHERE l.from_id IN ({marks})",
            chunk,
        ).fetchall()
        edges.extend(_as_edge(r) for r in rows if r["target"] in wanted)
    edges.sort(key=lambda e: (e["source"], e["target"], e["kind"]))
    return edges


def _count_broken(conn: sqlite3.Connection, node_ids: list[str]) -> int:
    """Wikilinks sin destino en los cuerpos de los nodos devueltos.

    Se cuenta por nota y título distinto, plegando mayúsculas/acentos igual
    que `sync_wikilinks`: `[[Inexistente]]` dos veces en la misma nota es un
    enlace roto, no dos. Cuesta releer los cuerpos, y por eso solo se hace
    sobre los nodos que van en la respuesta -- el recuento es "los rotos de lo
    que estás viendo", no un total del vault que nadie ha pedido.
    """
    if not node_ids:
        return 0
    bodies: list[str] = []
    for start in range(0, len(node_ids), _PARAM_CHUNK):
        chunk = node_ids[start : start + _PARAM_CHUNK]
        marks = ",".join("?" for _ in chunk)
        bodies.extend(
            r["body"]
            for r in conn.execute(f"SELECT body FROM pages WHERE id IN ({marks})", chunk)
        )
    # La mayoría de los cuerpos no llevan wikilinks: si no hay un solo `[[`,
    # no hace falta ni cargar los títulos del workspace.
    if not any("[[" in (body or "") for body in bodies):
        return 0
    known = {
        fold_title(r["title"]) for r in conn.execute("SELECT title FROM pages")
    }
    broken = 0
    for body in bodies:
        referenced = {fold_title(title) for title in extract_titles(body or "")}
        broken += sum(1 for title in referenced if title not in known)
    return broken


@router.get("/graph", response_model=schemas.GraphRead)
def get_graph(
    mode: Literal["local", "global"] = Query(
        ..., description="`local` (vecindad de una página) o `global` (por grado)"
    ),
    page_id: uuid.UUID | None = Query(
        None, description="Página centro del modo local (obligatoria ahí)"
    ),
    depth: int = Query(1, ge=1, le=MAX_LOCAL_DEPTH, description="Saltos en modo local"),
    limit: int = Query(
        DEFAULT_GLOBAL_LIMIT,
        ge=1,
        le=MAX_GLOBAL_LIMIT,
        description=f"Tope de nodos en modo global (máx. {MAX_GLOBAL_LIMIT})",
    ),
    repo: Repository = Depends(get_repo),
):
    """Grafo listo para pintar: nodos, aristas, si se ha cortado y enlaces rotos."""
    conn = repo.conn

    if mode == "local":
        if page_id is None:
            raise HTTPException(422, "mode=local necesita page_id")
        if repo.get_page(page_id) is None:
            raise HTTPException(404, "Página no encontrada")
        ids = _local_ids(conn, str(page_id), depth)
        nodes = _nodes_by_id(conn, ids)
        return {
            "nodes": nodes,
            "edges": _edges_among(conn, [n["id"] for n in nodes]),
            "truncated": False,
            "broken": _count_broken(conn, [n["id"] for n in nodes]),
        }

    node_rows = conn.execute(_GLOBAL_NODES_SQL, {"limit": limit}).fetchall()
    nodes = [_as_node(r) for r in node_rows]
    total = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
    return {
        "nodes": nodes,
        "edges": _edges_among(conn, [n["id"] for n in nodes]),
        "truncated": total > len(nodes),
        "broken": _count_broken(conn, [n["id"] for n in nodes]),
    }
