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

**Cómo se consulta.** El grado se calcula con dos `GROUP BY` sobre los índices
que ya existen (`links_from` y `links_to`, ambos cubrientes para esto) y se
mezcla en un diccionario, en vez de un `UNION ALL` de 2N filas ordenado en un
árbol temporal: con 12.000 notas y 30.000 enlaces eso era la mitad del tiempo.
Las filas de página se piden solo para los nodos elegidos, por clave primaria:
`pages` guarda el cuerpo en la misma fila, y un `SCAN pages` para ordenar por
grado arrastra megabytes de texto que el grafo no usa.
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

# El grafo toca muchas páginas dispersas de `pages` y de `links` en cada
# petición, y cada petición abre su propia conexión (ver `get_repo`): con la
# caché de páginas por defecto (2 MB) cada consulta vuelve a leer del disco lo
# que la anterior ya había leído. Con el fichero mapeado en memoria los accesos
# aleatorios salen de la caché del sistema y no de la caché de una conexión que
# se cierra al terminar. Medido con 12.000 notas: global de 240 a 140 ms.
_MMAP_BYTES = 256 * 1024 * 1024

# La vecindad se recorre en Python, un salto por consulta: como `depth` es 1 o
# 2, son como mucho cuatro consultas por índice y no hace falta una CTE
# recursiva que SQLite tenga que planificar distinto en cada versión. El troceo
# mantiene el número de parámetros acotado aunque un nodo tenga miles de enlaces.
_NEIGHBORS_OUT = "SELECT to_id AS id FROM links WHERE from_id IN ({marks})"
_NEIGHBORS_IN = "SELECT from_id AS id FROM links WHERE to_id IN ({marks})"

# Grado de un conjunto de nodos: dos consultas que usan los índices de `links`
# como cubrientes (no tocan la tabla).
_DEGREE_OUT = (
    "SELECT from_id AS id, count(*) AS n FROM links"
    " WHERE from_id IN ({marks}) GROUP BY from_id"
)
_DEGREE_IN = (
    "SELECT to_id AS id, count(*) AS n FROM links"
    " WHERE to_id IN ({marks}) GROUP BY to_id"
)
_DEGREE_ALL_OUT = "SELECT from_id AS id, count(*) AS n FROM links GROUP BY from_id"
_DEGREE_ALL_IN = "SELECT to_id AS id, count(*) AS n FROM links GROUP BY to_id"


def _chunks(items: list[str]):
    for start in range(0, len(items), _PARAM_CHUNK):
        yield items[start : start + _PARAM_CHUNK]


def _as_node(row: sqlite3.Row, degree: int) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "type": row["type"],
        "degree": degree,
        "updated_at": row["updated_at"],
    }


def _as_edge(row: sqlite3.Row) -> dict:
    return {"source": row["source"], "target": row["target"], "kind": row["kind"]}


def _bump(degrees: dict[str, int], rows) -> None:
    for row in rows:
        degrees[row["id"]] = degrees.get(row["id"], 0) + row["n"]


def _degrees(conn: sqlite3.Connection, ids: list[str]) -> dict[str, int]:
    """Grado de esos nodos (entrantes + salientes). Los que no aparecen: 0."""
    degrees: dict[str, int] = {}
    for chunk in _chunks(ids):
        marks = ",".join("?" for _ in chunk)
        _bump(degrees, conn.execute(_DEGREE_OUT.format(marks=marks), chunk))
        _bump(degrees, conn.execute(_DEGREE_IN.format(marks=marks), chunk))
    return degrees


def _all_degrees(conn: sqlite3.Connection) -> dict[str, int]:
    """Grado de todas las páginas en dos pasadas, sin ordenar 2N filas."""
    degrees: dict[str, int] = {}
    _bump(degrees, conn.execute(_DEGREE_ALL_OUT))
    _bump(degrees, conn.execute(_DEGREE_ALL_IN))
    return degrees


def _all_page_ids(conn: sqlite3.Connection) -> list[str]:
    """Ids de todas las páginas. Solo hace falta cuando el vault tiene menos
    páginas enlazadas que el tope y hay que completar con nodos de grado 0."""
    return [row[0] for row in conn.execute("SELECT id FROM pages")]


def _neighbors(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    """Vecinos a un salto, en las dos direcciones."""
    found: set[str] = set()
    for chunk in _chunks(ids):
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


def _nodes(conn: sqlite3.Connection, ids: list[str], degrees: dict[str, int]):
    """Nodos y sus cuerpos en una sola pasada por la tabla.

    El cuerpo se pide aquí, junto a los campos del nodo, porque el recuento de
    enlaces rotos lo necesita: dos consultas por id eran el doble de accesos
    aleatorios a `pages` para los mismos nodos. Devuelve `(nodos, cuerpos)`.
    """
    nodes: list[dict] = []
    bodies: dict[str, str] = {}
    for chunk in _chunks(ids):
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT id, title, type, updated_at, body FROM pages"
            f" WHERE id IN ({marks})",
            chunk,
        )
        for row in rows:
            nodes.append(_as_node(row, degrees.get(row["id"], 0)))
            bodies[row["id"]] = row["body"]
    nodes.sort(key=lambda n: (-n["degree"], n["title"].casefold(), n["id"]))
    return nodes, bodies


def _edges_among(conn: sqlite3.Connection, node_ids: list[str]) -> list[dict]:
    """Aristas con los dos extremos dentro del conjunto.

    Se pide por bloques de `from_id` (índice `links_from`) y se filtra el
    destino en memoria: así el número de parámetros nunca depende del tamaño
    del grafo y no hace falta una tabla temporal.
    """
    wanted = set(node_ids)
    edges: list[dict] = []
    for chunk in _chunks(node_ids):
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT l.from_id AS source, l.to_id AS target, l.kind FROM links l"
            f" WHERE l.from_id IN ({marks})",
            chunk,
        ).fetchall()
        edges.extend(_as_edge(r) for r in rows if r["target"] in wanted)
    edges.sort(key=lambda e: (e["source"], e["target"], e["kind"]))
    return edges


def _count_broken(conn: sqlite3.Connection, bodies: dict[str, str]) -> int:
    """Wikilinks sin destino en los cuerpos de los nodos devueltos.

    Se cuenta por nota y título distinto, plegando mayúsculas/acentos igual
    que `sync_wikilinks`: `[[Inexistente]]` dos veces en la misma nota es un
    enlace roto, no dos. Cuesta releer los cuerpos, y por eso solo se hace
    sobre los nodos que van en la respuesta -- el recuento es "los rotos de lo
    que estás viendo", no un total del vault que nadie ha pedido.
    """
    if not bodies:
        return 0
    # La mayoría de los cuerpos no llevan wikilinks: si no hay un solo `[[`,
    # no hace falta ni cargar los títulos del workspace.
    if not any("[[" in (body or "") for body in bodies.values()):
        return 0
    known = {
        fold_title(r["title"]) for r in conn.execute("SELECT title FROM pages")
    }
    broken = 0
    for body in bodies.values():
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
    conn.execute(f"PRAGMA mmap_size = {_MMAP_BYTES}")

    if mode == "local":
        if page_id is None:
            raise HTTPException(422, "mode=local necesita page_id")
        if repo.get_page(page_id) is None:
            raise HTTPException(404, "Página no encontrada")
        ids = _local_ids(conn, str(page_id), depth)
        nodes, bodies = _nodes(conn, ids, _degrees(conn, ids))
        return {
            "nodes": nodes,
            "edges": _edges_among(conn, [n["id"] for n in nodes]),
            "truncated": False,
            "broken": _count_broken(conn, bodies),
        }

    # Global: primero los ids por grado (sin tocar las filas de página), luego
    # solo las filas de los elegidos. El desempate por id hace determinista el
    # corte cuando varios nodos empatan a grado.
    degrees = _all_degrees(conn)
    total = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
    linked = sorted(degrees, key=lambda page: (-degrees[page], page))
    if len(linked) >= limit:
        # Hay más páginas enlazadas que hueco: el corte no necesita mirar las
        # páginas sin enlaces (grado 0), que nunca entrarían antes que estas.
        chosen = linked[:limit]
    else:
        # Vault pequeño: se completa con las páginas sin enlaces, que también
        # son nodos del grafo (una nota aislada es un punto, no un error).
        sin_enlaces = [p for p in _all_page_ids(conn) if p not in degrees]
        chosen = linked + sin_enlaces[: limit - len(linked)]
    nodes, bodies = _nodes(conn, chosen, degrees)
    return {
        "nodes": nodes,
        "edges": _edges_among(conn, [n["id"] for n in nodes]),
        "truncated": total > len(nodes),
        "broken": _count_broken(conn, bodies),
    }
