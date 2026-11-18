"""Permisos por nota, heredados por las hijas (3.0, hito 3).

Modelo mínimo y ampliable:

- **Quién**: un usuario (`subject_type='user'`, su id) o el espacio entero
  (`'workspace'`, el id del workspace). Hoy hay un único espacio y todos los
  usuarios están en él; el día que haya más, basta con saber a qué espacios
  pertenece cada usuario (`_workspaces_of`).
- **Qué**: una página (`'page'`) o una base (`'database'`; su id es el de su
  página). Las filas no tienen permiso propio: respetan el de su base.
- **Cuánto**: `view` < `edit` < `owner`. `owner` además decide quién entra.

**Herencia hacia abajo.** El permiso de una página vale para sus hijas (a
cualquier profundidad) y, si es una base, para sus filas. Una página que
**declara** su propio permiso (`acl_roots`) corta la herencia: desde ella para
abajo manda lo suyo, aunque esté vacío («solo los administradores»).

**Sin recursión por consulta.** Cada página guarda en `pages.acl_source` la
página cuyo permiso manda (ella misma si declara, si no la de su madre; la de su
base si es una fila). Saber si alguien ve una página es un JOIN con `acl` por
esa columna, y filtrar un listado de 10.000 notas, un `IN` indexado. La columna
se recalcula entera (`refresh_sources`, una CTE recursiva sobre el árbol) solo
cuando cambia algo que la mueve: un permiso, el padre de una página, un
reindexado. Crear una página la calcula para esa sola (`inherit_source`).

**Regla por defecto.** Un espacio es privado: una página nueva en la raíz es de
quien la crea (`owner`) y nadie más la ve. Compartir es declarar entradas. Lo
que no tiene ningún permiso por encima (`acl_source` NULL: una página puesta a
mano en el disco y reindexada) solo lo ven los administradores. `is_admin` lo
ve todo, para soporte.

**Un solo punto de comprobación.** `authorize(conn, user, resource, level)`.
La API lo llama desde una única guarda (`app.guard`), enganchada a todos los
routers; los listados filtran con `visible_clause`. Los routers no llevan `if`
de permisos.

Sin permiso de ver: **404** (no se filtra la existencia). Con permiso de ver
pero no de lo que se pide: **403**.

Los permisos no viajan en el export a Markdown (como los usuarios): no son
contenido del vault. Se dice aquí y en el ADR-0005, que es lo que pide su etapa 0.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from app import index as index_mod

LEVELS = {"view": 1, "edit": 2, "owner": 3}
SUBJECT_TYPES = ("user", "workspace")
RESOURCE_TYPES = ("page", "database")


class NotVisible(Exception):
    """El usuario no puede ni ver el recurso: la API contesta 404."""


class Forbidden(Exception):
    """Lo ve, pero no puede hacer lo que pide: 403."""


def _rank(level: Optional[str]) -> int:
    return LEVELS.get(level or "", 0)


def _is_system(user: Any) -> bool:
    """Sin usuario (scripts, arranque, agentes) o administrador: lo ve todo."""
    return user is None or bool(getattr(user, "is_admin", False))


def _workspaces_of(user: Any) -> list[str]:
    # Hoy hay un solo espacio y todo usuario está en él. Es el sitio donde
    # entrará la pertenencia el día que haya varios.
    return [index_mod.DEFAULT_WORKSPACE_ID]


def _subjects(user: Any) -> tuple[str, list[Any]]:
    """Condición SQL «la entrada `a` es de este usuario o de su espacio»."""
    workspaces = _workspaces_of(user)
    marks = ",".join("?" * len(workspaces))
    return (
        f"((a.subject_type = 'user' AND a.subject_id = ?)"
        f" OR (a.subject_type = 'workspace' AND a.subject_id IN ({marks})))",
        [str(user.id), *workspaces],
    )


# --------------------------------------------------------------------------
# comprobar
# --------------------------------------------------------------------------


def level_for(conn: sqlite3.Connection, user: Any, resource_id: Any) -> Optional[str]:
    """El nivel efectivo de `user` sobre la página (o None si no la ve)."""
    if _is_system(user):
        return "owner"
    cond, args = _subjects(user)
    rows = conn.execute(
        "SELECT a.level FROM pages p JOIN acl a ON a.resource_id = p.acl_source"
        f" WHERE p.id = ? AND {cond}",
        (str(resource_id), *args),
    ).fetchall()
    best = max((r[0] for r in rows), key=_rank, default=None)
    return best


def authorize(conn: sqlite3.Connection, user: Any, resource_id: Any, level: str) -> str:
    """El único punto de comprobación. Devuelve el nivel efectivo.

    `NotVisible` si no la ve (o no existe: la respuesta es la misma, así no se
    filtra qué ids existen), `Forbidden` si la ve pero no le llega el nivel.
    """
    if _is_system(user):
        # Que exista o no lo dice la ruta (su 404 de siempre).
        return "owner"
    actual = level_for(conn, user, resource_id)
    if actual is None:
        raise NotVisible(str(resource_id))
    if _rank(actual) < _rank(level):
        raise Forbidden(f"hace falta permiso de {level} y tienes {actual}")
    return actual


def visible_clause(user: Any, alias: str = "p") -> tuple[str, list[Any]]:
    """Condición SQL para filtrar un listado a lo que `user` ve (o «todo»)."""
    if _is_system(user):
        return "1", []
    cond, args = _subjects(user)
    return (
        f"{alias}.acl_source IN (SELECT a.resource_id FROM acl a WHERE {cond})",
        args,
    )


def visible_ids(conn: sqlite3.Connection, user: Any) -> Optional[set[str]]:
    """Los ids de página que ve `user`, o None si lo ve todo.

    Para los listados que se arman con varias consultas (grafo, panel, «Mis
    tareas»): una consulta y un filtro en memoria, sin una por página.
    """
    if _is_system(user):
        return None
    cond, args = visible_clause(user)
    return {r[0] for r in conn.execute(f"SELECT p.id FROM pages p WHERE {cond}", args)}


# --------------------------------------------------------------------------
# herencia: `pages.acl_source`
# --------------------------------------------------------------------------


def inherit_source(conn: sqlite3.Connection, page_id: Any) -> None:
    """Calcula `acl_source` de UNA página recién escrita (sin tocar el resto).

    Ella misma si declara; si es una fila, la de su base; si no, la de su
    madre. La sentencia vive en el almacén (`store.inherit_acl_source`): solo
    él escribe en `pages`.
    """
    from app.vault import store

    store.inherit_acl_source(conn, page_id)


def refresh_sources(conn: sqlite3.Connection) -> None:
    """Recalcula `acl_source` de todas las páginas (ver `store.refresh_acl_sources`)."""
    from app.vault import store

    store.refresh_acl_sources(conn)


# --------------------------------------------------------------------------
# declarar
# --------------------------------------------------------------------------


@dataclass
class Entry:
    subject_type: str
    subject_id: str
    level: str


def declared(conn: sqlite3.Connection, resource_id: Any) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM acl_roots WHERE resource_id = ?", (str(resource_id),)
        ).fetchone()
        is not None
    )


def entries(conn: sqlite3.Connection, resource_id: Any) -> list[Entry]:
    return [
        Entry(r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT subject_type, subject_id, level FROM acl WHERE resource_id = ?"
            " ORDER BY subject_type DESC, subject_id",
            (str(resource_id),),
        )
    ]


def set_entries(
    conn: sqlite3.Connection,
    resource_type: str,
    resource_id: Any,
    new_entries: Iterable[Entry],
) -> None:
    """Declara el permiso de la página: estas entradas y ninguna más.

    No recalcula la herencia: quien llama hace `refresh_sources` en la misma
    transacción (una vez, aunque cambie varias cosas).
    """
    rid = str(resource_id)
    conn.execute(
        "INSERT INTO acl_roots (resource_id, resource_type) VALUES (?, ?)"
        " ON CONFLICT(resource_id) DO UPDATE SET resource_type = excluded.resource_type",
        (rid, resource_type),
    )
    conn.execute("DELETE FROM acl WHERE resource_id = ?", (rid,))
    conn.executemany(
        "INSERT INTO acl (id, subject_type, subject_id, resource_type, resource_id, level)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (str(uuid.uuid4()), e.subject_type, e.subject_id, resource_type, rid, e.level)
            for e in new_entries
        ],
    )


def clear(conn: sqlite3.Connection, resource_id: Any) -> None:
    """Deja de declarar: la página vuelve a heredar de su madre."""
    rid = str(resource_id)
    conn.execute("DELETE FROM acl WHERE resource_id = ?", (rid,))
    conn.execute("DELETE FROM acl_roots WHERE resource_id = ?", (rid,))


def make_owner(conn: sqlite3.Connection, resource_type: str, resource_id: Any, user_id: str) -> None:
    """Página nueva en la raíz: es de quien la crea, y de nadie más."""
    set_entries(conn, resource_type, resource_id, [Entry("user", str(user_id), "owner")])
    inherit_source(conn, resource_id)


def share_roots_with_workspace(conn: sqlite3.Connection) -> int:
    """Las páginas de la raíz sin permiso propio quedan compartidas con el espacio.

    Es lo que se hace con lo que ya existía al migrar (índice viejo o vault de
    disco importado por primera vez): nadie pierde de vista lo que veía. Una
    sola sentencia por tabla, sin bucle por página.
    """
    roots = (
        "SELECT p.id, CASE p.type WHEN 'database' THEN 'database' ELSE 'page' END"
        " FROM pages p WHERE p.parent_id IS NULL AND p.type != 'database_row'"
        " AND NOT EXISTS (SELECT 1 FROM acl_roots r WHERE r.resource_id = p.id)"
    )
    found = conn.execute(roots).fetchall()
    if not found:
        return 0
    conn.executemany(
        "INSERT INTO acl_roots (resource_id, resource_type) VALUES (?, ?)", found
    )
    conn.executemany(
        "INSERT INTO acl (id, subject_type, subject_id, resource_type, resource_id, level)"
        " VALUES (?, 'workspace', ?, ?, ?, 'edit')",
        [(str(uuid.uuid4()), index_mod.DEFAULT_WORKSPACE_ID, kind, rid) for rid, kind in found],
    )
    return len(found)


def is_shared_sql(alias: str = "p") -> str:
    """Expresión SQL: 1 si la página la ve alguien más que sus propietarios."""
    return (
        f"EXISTS (SELECT 1 FROM acl s WHERE s.resource_id = {alias}.acl_source"
        " AND NOT (s.subject_type = 'user' AND s.level = 'owner'))"
    )
