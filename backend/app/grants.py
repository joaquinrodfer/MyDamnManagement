"""Permisos que se conceden (plan de roles, R2).

El administrador de la instalación gestiona (usuarios, grupos, espacios) pero
no lee lo ajeno (R1). Lo demás que antes venía con `is_admin` es ahora un
**permiso** que se da a una persona o a un grupo, para **todos los espacios**
(`workspace_id` NULL) o para **uno**:

| clave              | qué permite                                                   |
|--------------------|---------------------------------------------------------------|
| `manage_people`    | jornada, dedicación y ausencias de las personas de esos espacios |
| `view_workload`    | la carga de las personas de esos espacios                     |
| `manage_holidays`  | los festivos comunes (todos) o los de esos espacios           |
| `view_audit`       | el registro de auditoría (solo «todos»)                       |
| `backups`          | copias, exportar el vault entero y la papelera (solo «todos») |

Solo el administrador concede y retira, y queda en la auditoría. Al migrar,
cada administrador que había recibe todos de forma explícita (y retirable); un
administrador nuevo no recibe ninguno, salvo el primero de la instalación, que
no tendría a nadie que se los diera.

`can()` es la única comprobación: mira a la persona y a sus grupos.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

# (clave, nombre, explicación, ¿se puede limitar a espacios?)
CAPABILITIES: list[tuple[str, str, str, bool]] = [
    ("manage_people", "Gestionar personas",
     "Jornada, dedicación y ausencias de otras personas, con su tipo y su nota.", True),
    ("view_workload", "Ver carga",
     "La carga de trabajo de las personas de esos espacios. Las ausencias salen como «Ausente».", True),
    ("manage_holidays", "Gestionar festivos",
     "Los festivos comunes (con «Todos los espacios») o los de esos espacios, incluida la importación.", True),
    ("view_audit", "Ver auditoría",
     "El registro de quién hizo qué. No guarda contenido.", False),
    ("backups", "Copias de seguridad",
     "Hacer y descargar copias, exportar el vault entero y la papelera. Llevan todo el contenido.", False),
]
KEYS = [c[0] for c in CAPABILITIES]
SCOPED = {c[0] for c in CAPABILITIES if c[3]}
LABELS = {c[0]: c[1] for c in CAPABILITIES}
SUBJECT_TYPES = ("user", "group")


class GrantError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _subjects_sql(user_id: str) -> tuple[str, list[str]]:
    return (
        "((g.subject_type = 'user' AND g.subject_id = ?)"
        " OR (g.subject_type = 'group' AND g.subject_id IN"
        " (SELECT gm.group_id FROM group_members gm WHERE gm.user_id = ?)))",
        [user_id, user_id],
    )


def scopes(conn: sqlite3.Connection, user: Any, capability: str) -> Optional[set[str]]:
    """Dónde tiene `user` el permiso: `None` = en todos los espacios; un
    conjunto (quizá vacío) = solo en esos. El proceso interno (`None`), en todos."""
    if user is None:
        return None
    uid = str(getattr(user, "id", user))
    cond, args = _subjects_sql(uid)
    found: set[str] = set()
    for (wid,) in conn.execute(
        f"SELECT g.workspace_id FROM grants g WHERE g.capability = ? AND {cond}",
        [capability, *args],
    ):
        if wid is None:
            return None
        found.add(wid)
    return found


def can(conn: sqlite3.Connection, user: Any, capability: str, workspace_id: Optional[str] = None) -> bool:
    """¿Tiene el permiso? Sin `workspace_id`, hace falta tenerlo en todos los
    espacios; con él, en todos o en ese."""
    alcance = scopes(conn, user, capability)
    if alcance is None:
        return True
    return workspace_id is not None and str(workspace_id) in alcance


def has_any(conn: sqlite3.Connection, user: Any, capability: str) -> bool:
    """¿Lo tiene en algún espacio (o en todos)?"""
    alcance = scopes(conn, user, capability)
    return alcance is None or bool(alcance)


def summary(conn: sqlite3.Connection, user: Any) -> dict[str, Any]:
    """`{permiso: "all" | [ids de espacio]}`, solo los que tiene (para `/auth/me`)."""
    out: dict[str, Any] = {}
    for key in KEYS:
        alcance = scopes(conn, user, key)
        if alcance is None:
            out[key] = "all"
        elif alcance:
            out[key] = sorted(alcance)
    return out


def people_in(conn: sqlite3.Connection, workspace_ids: set[str]) -> set[str]:
    """Las personas que son miembros (directos o por un grupo) de esos espacios."""
    if not workspace_ids:
        return set()
    ids = sorted(workspace_ids)
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT m.subject_id FROM workspace_members m WHERE m.subject_type = 'user' AND m.workspace_id IN ({marks})"
        " UNION SELECT gm.user_id FROM workspace_members m JOIN group_members gm ON gm.group_id = m.subject_id"
        f" WHERE m.subject_type = 'group' AND m.workspace_id IN ({marks})",
        [*ids, *ids],
    )
    return {r[0] for r in rows}


def can_for_person(conn: sqlite3.Connection, user: Any, capability: str, person_id: str) -> bool:
    """¿Tiene el permiso sobre esa persona? En todos los espacios, sobre
    cualquiera; si no, sobre quien es miembro de alguno de sus espacios."""
    alcance = scopes(conn, user, capability)
    if alcance is None:
        return True
    return str(person_id) in people_in(conn, alcance)


# --------------------------------------------------------------------------
# conceder y retirar
# --------------------------------------------------------------------------


def listing(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT g.*, COALESCE(u.display_name, gr.name) AS subject_name, u.username AS username,"
        " u.color AS color, w.name AS workspace_name, b.display_name AS granted_by_name"
        " FROM grants g"
        " LEFT JOIN users u ON g.subject_type = 'user' AND u.id = g.subject_id"
        " LEFT JOIN groups gr ON g.subject_type = 'group' AND gr.id = g.subject_id"
        " LEFT JOIN workspaces w ON w.id = g.workspace_id"
        " LEFT JOIN users b ON b.id = g.granted_by"
        " ORDER BY g.capability, g.workspace_id IS NOT NULL, w.name COLLATE NOCASE,"
        " subject_name COLLATE NOCASE"
    ).fetchall()
    return [
        {
            "id": r["id"], "capability": r["capability"],
            "subject_type": r["subject_type"], "subject_id": r["subject_id"],
            "subject_name": r["subject_name"] or "(borrado)", "username": r["username"], "color": r["color"],
            "workspace_id": r["workspace_id"], "workspace_name": r["workspace_name"],
            "granted_by": r["granted_by"], "granted_by_name": r["granted_by_name"],
            "granted_at": r["granted_at"],
        }
        for r in rows
    ]


def get(conn: sqlite3.Connection, grant_id: str) -> Optional[dict[str, Any]]:
    return next((g for g in listing(conn) if g["id"] == grant_id), None)


def grant(
    conn: sqlite3.Connection,
    capability: str,
    subject_type: str,
    subject_id: str,
    workspace_id: Optional[str] = None,
    granted_by: Optional[str] = None,
) -> dict[str, Any]:
    """Concede (idempotente: si ya lo tenía, devuelve el que había)."""
    if capability not in KEYS:
        raise GrantError("Ese permiso no existe.")
    if subject_type not in SUBJECT_TYPES:
        raise GrantError("Se concede a una persona o a un grupo.")
    tabla = "users" if subject_type == "user" else "groups"
    if conn.execute(f"SELECT 1 FROM {tabla} WHERE id = ?", (str(subject_id),)).fetchone() is None:
        raise GrantError("No existe esa persona o ese grupo.", 404)
    if workspace_id is not None:
        if capability not in SCOPED:
            raise GrantError(f"«{LABELS[capability]}» es para todos los espacios.")
        if conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (str(workspace_id),)).fetchone() is None:
            raise GrantError("No existe ese espacio.", 404)
    previo = conn.execute(
        "SELECT id FROM grants WHERE capability = ? AND subject_type = ? AND subject_id = ?"
        " AND COALESCE(workspace_id, '') = COALESCE(?, '')",
        (capability, subject_type, str(subject_id), workspace_id),
    ).fetchone()
    if previo is not None:
        return get(conn, previo[0])  # type: ignore[return-value]
    gid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO grants (id, subject_type, subject_id, capability, workspace_id, granted_by, granted_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (gid, subject_type, str(subject_id), capability, workspace_id, granted_by, _now()),
    )
    return get(conn, gid)  # type: ignore[return-value]


def revoke(conn: sqlite3.Connection, grant_id: str) -> bool:
    return conn.execute("DELETE FROM grants WHERE id = ?", (grant_id,)).rowcount > 0


def revoke_matching(
    conn: sqlite3.Connection, capability: str, subject_type: str, subject_id: str,
    workspace_id: Optional[str] = None,
) -> None:
    conn.execute(
        "DELETE FROM grants WHERE capability = ? AND subject_type = ? AND subject_id = ?"
        " AND COALESCE(workspace_id, '') = COALESCE(?, '')",
        (capability, subject_type, str(subject_id), workspace_id),
    )


def forget_subject(conn: sqlite3.Connection, subject_type: str, subject_id: str) -> None:
    """Al borrar una persona o un grupo, sus permisos se van con él."""
    conn.execute("DELETE FROM grants WHERE subject_type = ? AND subject_id = ?", (subject_type, str(subject_id)))


def grant_all(conn: sqlite3.Connection, user_id: str, granted_by: Optional[str] = None) -> None:
    """Todos los permisos, en todos los espacios (los administradores al migrar
    y el primero de una instalación nueva)."""
    for key in KEYS:
        grant(conn, key, "user", user_id, None, granted_by)


def migrate_from_admins(conn: sqlite3.Connection) -> None:
    """Migración v23: lo de antes, ahora explícito. Cada administrador recibe
    todos los permisos en todos los espacios; quien tenía
    `can_view_global_workload`, «Ver carga» en todos."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for (uid,) in conn.execute("SELECT id FROM users WHERE is_admin = 1").fetchall():
        grant_all(conn, uid)
    if "can_view_global_workload" in cols:
        for (uid,) in conn.execute(
            "SELECT id FROM users WHERE can_view_global_workload = 1 AND is_admin = 0"
        ).fetchall():
            grant(conn, "view_workload", "user", uid, None)


def explain(conn: sqlite3.Connection, user: Any) -> list[dict[str, Any]]:
    """Mis permisos y de dónde me vienen (plan de roles, R5): por cada permiso
    que tengo, cada concesión con su alcance y si es directa o por un grupo."""
    uid = str(getattr(user, "id", user))
    cond, args = _subjects_sql(uid)
    filas = conn.execute(
        "SELECT g.capability, g.subject_type, gr.name AS group_name, g.workspace_id, w.name AS workspace_name"
        " FROM grants g LEFT JOIN groups gr ON g.subject_type = 'group' AND gr.id = g.subject_id"
        " LEFT JOIN workspaces w ON w.id = g.workspace_id"
        f" WHERE {cond} ORDER BY g.workspace_id IS NOT NULL, w.name COLLATE NOCASE",
        args,
    ).fetchall()
    salida = []
    for key, label, ayuda, _scoped in CAPABILITIES:
        propias = [f for f in filas if f["capability"] == key]
        if not propias:
            continue
        salida.append({
            "key": key, "label": label, "help": ayuda,
            "grants": [
                {
                    "workspace_id": f["workspace_id"],
                    "workspace_name": f["workspace_name"],
                    "via": "group" if f["subject_type"] == "group" else "user",
                    "group_name": f["group_name"],
                }
                for f in propias
            ],
        })
    return salida
