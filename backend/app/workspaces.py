"""Espacios de trabajo, grupos y roles (plan H1).

Un **espacio** separa un contexto de trabajo (Desarrollo, Investigación…) de
los demás: sus páginas, bases, búsqueda y grafo no se mezclan. Quien no está en
un espacio no ve nada de él (salvo lo que le compartan como invitado, H2).

- **Miembros** (`workspace_members`): una persona o un grupo entero, con un rol
  - `admin`: gestiona los miembros y los ajustes del espacio;
  - `editor`: crea, edita, mueve y borra;
  - `viewer`: solo lee. Su rol limita lo que le llega **por el espacio**: una
    página compartida con todo el espacio en `edit` solo le deja `view`; lo
    suyo (una página de la que es propietario) no se recorta.
- **Grupos** (`groups`, `group_members`): listas de personas a las que se da
  acceso de una vez, a un espacio o a una página (`acl`, sujeto `group`).
- **Instancia**: `users.is_admin` es el administrador: crea y borra espacios y
  gestiona los miembros y ajustes de cualquiera que no sea personal, pero
  **no entra** en los que no es miembro (plan de roles, R1): no ve sus páginas.
  `users.can_view_global_workload` es un permiso aparte para la Carga del
  equipo.

Este módulo solo habla con la base; los permisos de página siguen siendo cosa
de `app.acl`, que usa aquí `member_workspaces_sql`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app import index as index_mod

ROLES = {"viewer": 1, "editor": 2, "admin": 3}
DEFAULT_ROLE = "editor"
SUBJECT_TYPES = ("user", "group")


class WorkspaceError(Exception):
    """Algo no vale (nombre repetido, último espacio…): la API contesta 400/409."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rank(role: Optional[str]) -> int:
    return ROLES.get(role or "", 0)


def best_role(*roles: Optional[str]) -> Optional[str]:
    found = [r for r in roles if r in ROLES]
    return max(found, key=_rank) if found else None


def slugify(name: str) -> str:
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "espacio"


# --------------------------------------------------------------------------
# a qué espacios pertenece alguien
# --------------------------------------------------------------------------

# Los espacios de un usuario, directamente o por un grupo. Es un fragmento SQL
# (con dos `?`: el id del usuario, dos veces) para que `acl` lo meta en sus
# consultas de visibilidad sin una consulta aparte por página.
MEMBER_WORKSPACES_SQL = (
    "SELECT m.workspace_id FROM workspace_members m"
    " WHERE (m.subject_type = 'user' AND m.subject_id = ?)"
    " OR (m.subject_type = 'group' AND m.subject_id IN"
    " (SELECT gm.group_id FROM group_members gm WHERE gm.user_id = ?))"
)


def member_workspaces_sql(user_id: Any) -> tuple[str, list[str]]:
    return MEMBER_WORKSPACES_SQL, [str(user_id), str(user_id)]


def member_role(conn: sqlite3.Connection, user_id: Any, workspace_id: Any) -> Optional[str]:
    """El mejor rol de la persona en el espacio (directo o por grupo), o None."""
    rows = conn.execute(
        "SELECT m.role FROM workspace_members m WHERE m.workspace_id = ? AND ("
        " (m.subject_type = 'user' AND m.subject_id = ?)"
        " OR (m.subject_type = 'group' AND m.subject_id IN"
        "  (SELECT gm.group_id FROM group_members gm WHERE gm.user_id = ?)))",
        (str(workspace_id), str(user_id), str(user_id)),
    ).fetchall()
    return best_role(*(r[0] for r in rows))


def role_of(conn: sqlite3.Connection, user: Any, workspace_id: Any) -> Optional[str]:
    """El rol efectivo en el espacio: el de miembro (el sistema, `admin` en todos).

    El administrador de la instalación no tiene rol en un espacio en el que no
    está: no lo ve por dentro (plan de roles, R1). Gestionarlo es otra cosa:
    `can_manage`."""
    if user is None:
        return "admin"
    return member_role(conn, user.id, workspace_id)


def is_personal(row: Optional[sqlite3.Row]) -> bool:
    if row is None:
        return False
    try:
        return bool(json.loads(row["settings"] or "{}").get("personal_of"))
    except ValueError:
        return False


def can_manage(conn: sqlite3.Connection, user: Any, workspace_id: Any) -> bool:
    """¿Puede cambiar miembros y ajustes? El administrador del espacio, o el de
    la instalación si el espacio no es personal (para que nadie se quede fuera
    de uno). Gestionar no es leer: no le da acceso a las páginas."""
    if role_of(conn, user, workspace_id) == "admin":
        return True
    return bool(getattr(user, "is_admin", False)) and not is_personal(get(conn, workspace_id))


def can_edit(conn: sqlite3.Connection, user: Any, workspace_id: Any) -> bool:
    return _rank(role_of(conn, user, workspace_id)) >= ROLES["editor"]


def is_workspace_admin(conn: sqlite3.Connection, user: Any, workspace_id: Any) -> bool:
    return role_of(conn, user, workspace_id) == "admin"


# --------------------------------------------------------------------------
# espacios
# --------------------------------------------------------------------------


def _row(conn: sqlite3.Connection, r: sqlite3.Row, role: Optional[str]) -> dict:
    try:
        settings = json.loads(r["settings"] or "{}")
    except ValueError:
        settings = {}
    pages = conn.execute(
        "SELECT count(*) FROM pages WHERE workspace_id = ?", (r["id"],)
    ).fetchone()[0]
    return {
        "id": r["id"],
        "name": r["name"],
        "slug": r["slug"],
        "icon": r["icon"],
        "settings": settings,
        "created_at": r["created_at"],
        "role": role,
        "pages": pages,
        "is_default": r["id"] == index_mod.DEFAULT_WORKSPACE_ID,
    }


def list_for(conn: sqlite3.Connection, user: Any, manage: bool = False) -> list[dict]:
    """Los espacios de `user`: en los que es miembro.

    Con `manage`, los que puede gestionar (`can_manage`): para el administrador
    de la instalación, también los que no son suyos ni personales, con
    `role` None y `member` False (los ve por fuera, no por dentro).

    Cada uno trae `member` y `personal_of` (el id de la persona de quien es el
    «Espacio de …»). El de la propia persona va el primero.
    """
    rows = conn.execute("SELECT * FROM workspaces ORDER BY created_at, name").fetchall()
    uid = str(getattr(user, "id", "") or "")
    out = []
    for r in rows:
        role = role_of(conn, user, r["id"])
        if manage:
            if not can_manage(conn, user, r["id"]):
                continue
        elif role is None:
            continue
        d = _row(conn, r, role)
        d["member"] = member_role(conn, uid, r["id"]) is not None if uid else True
        d["personal_of"] = d["settings"].get("personal_of")
        out.append(d)
    out.sort(key=lambda d: (d["personal_of"] != uid, not d["member"], d["personal_of"] is not None))
    return out


def ensure_personal(conn: sqlite3.Connection, user: Any) -> None:
    """Cada persona tiene su «Espacio de <nombre>» con su Privado y su Compartido.

    Se crea la primera vez que la persona pide sus espacios (o al darla de alta) y
    se apunta en `meta`: si luego lo borra, no vuelve a aparecer.
    """
    uid = str(getattr(user, "id", "") or "")
    if not uid or getattr(user, "is_system", False):
        return
    key = f"personal_ws:{uid}"
    if conn.execute("SELECT 1 FROM meta WHERE key = ?", (key,)).fetchone():
        return
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is None:
        return
    base = f"Espacio de {getattr(user, 'display_name', None) or getattr(user, 'username', 'usuario')}"
    created = None
    for name in (base, f"{base} ({getattr(user, 'username', uid)})", f"{base} ({uid[:6]})"):
        try:
            created = create(conn, name)
            break
        except WorkspaceError:
            continue
    if created is None:
        return
    update(conn, created["id"], settings={"personal_of": uid})
    set_member(conn, created["id"], "user", uid, "admin")
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, created["id"])
    )


def get(conn: sqlite3.Connection, workspace_id: Any) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM workspaces WHERE id = ?", (str(workspace_id),)).fetchone()


def get_dict(conn: sqlite3.Connection, user: Any, workspace_id: Any) -> Optional[dict]:
    r = get(conn, workspace_id)
    if r is None:
        return None
    role = role_of(conn, user, r["id"])
    if role or can_manage(conn, user, r["id"]):
        return _row(conn, r, role)
    return None


def _unique_slug(conn: sqlite3.Connection, name: str, exclude: Optional[str] = None) -> str:
    base = slugify(name)
    slug, n = base, 2
    while conn.execute(
        "SELECT 1 FROM workspaces WHERE slug = ? AND id != ?", (slug, exclude or "")
    ).fetchone():
        slug, n = f"{base}-{n}", n + 1
    return slug


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise WorkspaceError("el nombre del espacio es obligatorio (hasta 80 caracteres)")
    return name


def create(conn: sqlite3.Connection, name: str, icon: Optional[str] = None) -> dict:
    name = _clean_name(name)
    if conn.execute("SELECT 1 FROM workspaces WHERE lower(name) = lower(?)", (name,)).fetchone():
        raise WorkspaceError(f"ya hay un espacio llamado {name!r}", 409)
    wid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO workspaces (id, name, created_at, slug, icon) VALUES (?, ?, ?, ?, ?)",
        (wid, name, _now(), _unique_slug(conn, name), (icon or None)),
    )
    return _row(conn, get(conn, wid), "admin")


def update(
    conn: sqlite3.Connection,
    workspace_id: Any,
    *,
    name: Optional[str] = None,
    icon: Optional[str] = None,
    settings: Optional[dict] = None,
) -> None:
    row = get(conn, workspace_id)
    if row is None:
        raise WorkspaceError("no existe ese espacio", 404)
    if name is not None:
        name = _clean_name(name)
        if conn.execute(
            "SELECT 1 FROM workspaces WHERE lower(name) = lower(?) AND id != ?",
            (name, row["id"]),
        ).fetchone():
            raise WorkspaceError(f"ya hay un espacio llamado {name!r}", 409)
        conn.execute(
            "UPDATE workspaces SET name = ?, slug = ? WHERE id = ?",
            (name, _unique_slug(conn, name, row["id"]), row["id"]),
        )
    if icon is not None:
        conn.execute("UPDATE workspaces SET icon = ? WHERE id = ?", (icon or None, row["id"]))
    if settings is not None:
        try:
            current = json.loads(row["settings"] or "{}")
        except ValueError:
            current = {}
        current.update(settings)
        conn.execute(
            "UPDATE workspaces SET settings = ? WHERE id = ?",
            (json.dumps(current, ensure_ascii=False), row["id"]),
        )


def delete(conn: sqlite3.Connection, workspace_id: Any) -> None:
    """Borra un espacio vacío. Con páginas dentro, no: se mueven o se borran antes."""
    row = get(conn, workspace_id)
    if row is None:
        raise WorkspaceError("no existe ese espacio", 404)
    if conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] <= 1:
        raise WorkspaceError("no se puede borrar el único espacio", 409)
    n = conn.execute("SELECT count(*) FROM pages WHERE workspace_id = ?", (row["id"],)).fetchone()[0]
    if n:
        raise WorkspaceError(
            f"el espacio tiene {n} páginas: muévelas a otro espacio o bórralas antes", 409
        )
    conn.execute("DELETE FROM acl WHERE subject_type = 'workspace' AND subject_id = ?", (row["id"],))
    conn.execute("DELETE FROM workspace_members WHERE workspace_id = ?", (row["id"],))
    conn.execute("DELETE FROM workspaces WHERE id = ?", (row["id"],))


def resolve_active(conn: sqlite3.Connection, user: Any, requested: Optional[str]) -> Optional[str]:
    """El espacio activo de una petición (cabecera `X-Workspace`).

    `None` si no se pide ninguno (la API se comporta como siempre: todo lo que
    el usuario ve). Un espacio que no existe o al que no pertenece da
    `WorkspaceError` 404, la misma respuesta para los dos casos.
    """
    if not requested:
        return None
    r = get(conn, requested)
    if r is None or role_of(conn, user, r["id"]) is None:
        raise WorkspaceError("no existe ese espacio", 404)
    return r["id"]


# --------------------------------------------------------------------------
# miembros
# --------------------------------------------------------------------------


def members(conn: sqlite3.Connection, workspace_id: Any) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT subject_type, subject_id, role FROM workspace_members WHERE workspace_id = ?",
        (str(workspace_id),),
    ):
        if r["subject_type"] == "user":
            u = conn.execute(
                "SELECT id, username, display_name, color, avatar FROM users WHERE id = ?",
                (r["subject_id"],),
            ).fetchone()
            if u is None:
                continue
            from app import auth  # import tardío: `auth` no importa este módulo

            out.append({
                "subject_type": "user",
                "subject_id": u["id"],
                "name": u["display_name"],
                "username": u["username"],
                "color": u["color"],
                "avatar": auth.avatar_of(u),
                "role": r["role"],
            })
        else:
            g = conn.execute("SELECT id, name FROM groups WHERE id = ?", (r["subject_id"],)).fetchone()
            if g is None:
                continue
            count = conn.execute(
                "SELECT count(*) FROM group_members WHERE group_id = ?", (g["id"],)
            ).fetchone()[0]
            out.append({
                "subject_type": "group",
                "subject_id": g["id"],
                "name": g["name"],
                "members": count,
                "role": r["role"],
            })
    out.sort(key=lambda m: (-_rank(m["role"]), m["subject_type"] != "group", m["name"].casefold()))
    return out


def set_member(
    conn: sqlite3.Connection, workspace_id: Any, subject_type: str, subject_id: str, role: str
) -> None:
    if role not in ROLES:
        raise WorkspaceError("el rol es admin, editor o viewer")
    if subject_type not in SUBJECT_TYPES:
        raise WorkspaceError("el miembro es un usuario o un grupo")
    table = "users" if subject_type == "user" else "groups"
    if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (subject_id,)).fetchone() is None:
        raise WorkspaceError("no existe ese " + ("usuario" if subject_type == "user" else "grupo"), 404)
    if get(conn, workspace_id) is None:
        raise WorkspaceError("no existe ese espacio", 404)
    conn.execute(
        "INSERT INTO workspace_members (workspace_id, subject_type, subject_id, role)"
        " VALUES (?, ?, ?, ?) ON CONFLICT(workspace_id, subject_type, subject_id)"
        " DO UPDATE SET role = excluded.role",
        (str(workspace_id), subject_type, subject_id, role),
    )


def remove_member(conn: sqlite3.Connection, workspace_id: Any, subject_type: str, subject_id: str) -> None:
    conn.execute(
        "DELETE FROM workspace_members WHERE workspace_id = ? AND subject_type = ? AND subject_id = ?",
        (str(workspace_id), subject_type, subject_id),
    )


def enroll_user(
    conn: sqlite3.Connection, user_id: str, workspace_id: Optional[str] = None, role: str = DEFAULT_ROLE
) -> None:
    """Mete a una persona en un espacio (por defecto, el primero: General)."""
    if workspace_id is None:
        row = conn.execute("SELECT id FROM workspaces ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            return
        workspace_id = row[0]
    conn.execute(
        "INSERT OR IGNORE INTO workspace_members (workspace_id, subject_type, subject_id, role)"
        " VALUES (?, 'user', ?, ?)",
        (workspace_id, user_id, role),
    )


# --------------------------------------------------------------------------
# grupos
# --------------------------------------------------------------------------


def _clean_group_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise WorkspaceError("el nombre del grupo es obligatorio (hasta 60 caracteres)")
    return name


def list_groups(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for g in conn.execute("SELECT id, name, created_at FROM groups ORDER BY name COLLATE NOCASE"):
        ids = [r[0] for r in conn.execute("SELECT user_id FROM group_members WHERE group_id = ?", (g["id"],))]
        out.append({"id": g["id"], "name": g["name"], "created_at": g["created_at"], "members": sorted(ids)})
    return out


def create_group(conn: sqlite3.Connection, name: str) -> dict:
    name = _clean_group_name(name)
    gid = str(uuid.uuid4())
    try:
        conn.execute("INSERT INTO groups (id, name, created_at) VALUES (?, ?, ?)", (gid, name, _now()))
    except sqlite3.IntegrityError as exc:
        raise WorkspaceError(f"ya hay un grupo llamado {name!r}", 409) from exc
    return {"id": gid, "name": name, "members": []}


def rename_group(conn: sqlite3.Connection, group_id: str, name: str) -> None:
    name = _clean_group_name(name)
    if conn.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,)).fetchone() is None:
        raise WorkspaceError("no existe ese grupo", 404)
    try:
        conn.execute("UPDATE groups SET name = ? WHERE id = ?", (name, group_id))
    except sqlite3.IntegrityError as exc:
        raise WorkspaceError(f"ya hay un grupo llamado {name!r}", 409) from exc


def set_group_members(conn: sqlite3.Connection, group_id: str, user_ids: list[str]) -> None:
    if conn.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,)).fetchone() is None:
        raise WorkspaceError("no existe ese grupo", 404)
    ids = list(dict.fromkeys(user_ids))
    known = {r[0] for r in conn.execute(f"SELECT id FROM users WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
    missing = [i for i in ids if i not in known]
    if missing:
        raise WorkspaceError(f"no existe el usuario {missing[0]}", 404)
    conn.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
    conn.executemany(
        "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)", [(group_id, i) for i in ids]
    )


def delete_group(conn: sqlite3.Connection, group_id: str) -> None:
    if conn.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,)).fetchone() is None:
        raise WorkspaceError("no existe ese grupo", 404)
    conn.execute("DELETE FROM acl WHERE subject_type = 'group' AND subject_id = ?", (group_id,))
    conn.execute(
        "DELETE FROM workspace_members WHERE subject_type = 'group' AND subject_id = ?", (group_id,)
    )
    conn.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
    conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
