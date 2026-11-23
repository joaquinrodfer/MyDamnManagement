"""Enlaces de acceso de un solo uso: invitar a alguien y ponerle una
contraseña nueva a quien la olvidó.

Notemil no manda correo: quien administra crea el enlace y lo pasa por donde
quiera (chat, mensaje…). Por eso el enlace es la credencial:

- **El token** es largo (256 bits) y solo se guarda su hash: quien lea el
  índice no puede usar un enlace pendiente.
- **Un solo uso** y **caduca**: la invitación a los 7 días y la contraseña
  nueva a las 24 horas.
- **Invitación:** quien la abre elige su usuario, su nombre y su contraseña, y
  entra ya como miembro del espacio que se dijo, con su rol.
- **Contraseña nueva:** pone la contraseña y cierra todas las sesiones que esa
  persona tuviera abiertas. Crear un enlace nuevo anula los anteriores.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

INVITE_TTL = timedelta(days=7)
RESET_TTL = timedelta(hours=24)
PATH_PREFIX = "/enlace/"


class LinkError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _create(conn: sqlite3.Connection, *, kind: str, created_by: str, ttl: timedelta, **campos: Any) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    ahora = _now()
    caduca = (ahora + ttl).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO access_links (token_hash, kind, user_id, workspace_id, role, display_name,"
        " created_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_hash(token), kind, campos.get("user_id"), campos.get("workspace_id"), campos.get("role"),
         campos.get("display_name"), created_by, ahora.isoformat(timespec="seconds"), caduca),
    )
    return {"id": cur.lastrowid, "kind": kind, "path": PATH_PREFIX + token, "expires_at": caduca}


def create_invite(conn: sqlite3.Connection, *, created_by: str, workspace_id: Optional[str] = None,
                  role: Optional[str] = None, display_name: Optional[str] = None) -> dict[str, Any]:
    return _create(conn, kind="invite", created_by=created_by, ttl=INVITE_TTL, workspace_id=workspace_id,
                   role=role if workspace_id else None, display_name=(display_name or "").strip()[:80] or None)


def create_reset(conn: sqlite3.Connection, *, created_by: str, user_id: str) -> dict[str, Any]:
    # Solo vale el último: los enlaces anteriores de esa persona se anulan.
    conn.execute(
        "UPDATE access_links SET used_at = ? WHERE kind = 'reset' AND user_id = ? AND used_at IS NULL",
        (_now().isoformat(timespec="seconds"), user_id),
    )
    return _create(conn, kind="reset", created_by=created_by, ttl=RESET_TTL, user_id=user_id)


def find(conn: sqlite3.Connection, token: str) -> Optional[sqlite3.Row]:
    """El enlace si sigue valiendo (existe, no usado, sin caducar); si no, None."""
    if not token or len(token) > 200:
        return None
    fila = conn.execute("SELECT * FROM access_links WHERE token_hash = ?", (_hash(token),)).fetchone()
    if fila is None or fila["used_at"]:
        return None
    try:
        if datetime.fromisoformat(fila["expires_at"]) <= _now():
            return None
    except ValueError:
        return None
    return fila


def mark_used(conn: sqlite3.Connection, link_id: int) -> bool:
    """Lo marca usado; False si otro lo usó a la vez (gana el primero)."""
    cur = conn.execute(
        "UPDATE access_links SET used_at = ? WHERE id = ? AND used_at IS NULL",
        (_now().isoformat(timespec="seconds"), link_id),
    )
    return cur.rowcount == 1


def pending(conn: sqlite3.Connection, created_by: Optional[str] = None) -> list[dict[str, Any]]:
    """Las invitaciones sin usar ni caducar (de quien se diga, o todas)."""
    ahora = _now().isoformat(timespec="seconds")
    sql = (
        "SELECT a.id, a.workspace_id, a.role, a.display_name, a.created_at, a.expires_at, w.name AS workspace_name,"
        " u.display_name AS created_by_name FROM access_links a"
        " LEFT JOIN workspaces w ON w.id = a.workspace_id LEFT JOIN users u ON u.id = a.created_by"
        " WHERE a.kind = 'invite' AND a.used_at IS NULL AND a.expires_at > ?"
    )
    args: list[Any] = [ahora]
    if created_by:
        sql += " AND a.created_by = ?"
        args.append(created_by)
    return [dict(r) for r in conn.execute(sql + " ORDER BY a.id DESC", args)]


def revoke(conn: sqlite3.Connection, link_id: int, created_by: Optional[str] = None) -> bool:
    sql = "UPDATE access_links SET used_at = ? WHERE id = ? AND kind = 'invite' AND used_at IS NULL"
    args: list[Any] = [_now().isoformat(timespec="seconds"), link_id]
    if created_by:
        sql += " AND created_by = ?"
        args.append(created_by)
    return conn.execute(sql, args).rowcount == 1
