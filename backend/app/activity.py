"""Quién crea y edita cada página: la «Actividad reciente» del inicio.

El vault guarda el contenido, no quién lo tocó. Para que el inicio pueda decir
«Marta editó *Plan de lanzamiento* hace 20 min», cada guardado con autor deja
una fila en `page_activity` (índice v20):

- **Se junta.** Varias ediciones seguidas de la misma persona en la misma página
  (el autoguardado escribe cada pocos segundos) son UNA fila: si la última de
  esa persona en esa página es de hace menos de `MERGE_MINUTES`, se actualiza su
  hora y su cuenta en vez de añadir otra.
- **Sin autor, nada.** La sala de edición en vivo apunta a quien escribió; un
  guardado sin usuario (un proceso interno) no deja rastro.
- **Se poda.** Lo de más de `KEEP_DAYS` días sobra (`purge_old`).
- **Respeta los permisos al leer**: `recent` solo devuelve páginas que quien
  pregunta puede ver, y nunca lo suyo propio (eso ya lo sabe).

Es estado de la app, no contenido: no viaja en el export ni en la huella.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

MERGE_MINUTES = 15
KEEP_DAYS = 60
ACTIONS = ("created", "edited")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def record(
    conn: sqlite3.Connection,
    page_id: Any,
    user_id: Any,
    action: str = "edited",
    now: Optional[datetime] = None,
) -> None:
    """Apunta que `user_id` creó o editó `page_id`. Nunca lanza: la actividad
    es un extra y no puede tumbar un guardado."""
    if not page_id or not user_id or action not in ACTIONS:
        return
    page_id, user_id = str(page_id), str(user_id)
    now = now or _now()
    try:
        if action == "edited":
            desde = _iso(now - timedelta(minutes=MERGE_MINUTES))
            previa = conn.execute(
                "SELECT id FROM page_activity WHERE page_id = ? AND user_id = ? AND ts >= ?"
                " ORDER BY ts DESC LIMIT 1",
                (page_id, user_id, desde),
            ).fetchone()
            if previa is not None:
                conn.execute(
                    "UPDATE page_activity SET ts = ?, edits = edits + 1 WHERE id = ?",
                    (_iso(now), previa[0]),
                )
                return
        conn.execute(
            "INSERT INTO page_activity (page_id, user_id, action, ts) VALUES (?, ?, ?, ?)",
            (page_id, user_id, action, _iso(now)),
        )
    except sqlite3.Error:
        pass


def record_many(conn: sqlite3.Connection, page_id: Any, user_ids: Iterable[Any]) -> None:
    for uid in user_ids:
        record(conn, page_id, uid)


def recent(
    conn: sqlite3.Connection,
    user_id: Any,
    visible: Optional[set[str]],
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Lo último que han hecho OTROS en páginas que `user_id` puede ver.

    Una entrada por página y persona (la más reciente), de la más nueva a la
    más vieja. `visible` es lo de `acl.visible_ids` (`None` = lo ve todo).
    """
    rows = conn.execute(
        "SELECT a.page_id, a.user_id, a.action, a.ts, a.edits,"
        " p.title, p.type, p.icon, p.workspace_id, w.name AS workspace_name,"
        " u.display_name, u.username, u.color, u.avatar, r.database_id"
        " FROM page_activity a JOIN pages p ON p.id = a.page_id"
        " LEFT JOIN users u ON u.id = a.user_id"
        " LEFT JOIN workspaces w ON w.id = p.workspace_id"
        " LEFT JOIN rows r ON r.id = a.page_id"
        " WHERE a.user_id != ? ORDER BY a.ts DESC LIMIT ?",
        (str(user_id), max(1, limit) * 6),
    ).fetchall()
    from app import auth  # import tardío: `auth` no depende de este módulo

    vistos: set[tuple[str, str]] = set()
    salida: list[dict[str, Any]] = []
    for r in rows:
        if visible is not None and r["page_id"] not in visible:
            continue
        clave = (r["page_id"], r["user_id"])
        if clave in vistos:
            continue
        vistos.add(clave)
        salida.append(
            {
                "page_id": r["page_id"],
                "page_title": r["title"] or "Sin título",
                "page_type": r["type"],
                "icon": r["icon"],
                "database_id": r["database_id"],
                "workspace_id": r["workspace_id"],
                "workspace_name": r["workspace_name"],
                "action": r["action"],
                "edits": r["edits"],
                "ts": r["ts"],
                "user": {
                    "id": r["user_id"],
                    "name": r["display_name"] or r["username"] or "Alguien",
                    "color": r["color"],
                    "avatar": auth.avatar_of(r) if r["display_name"] is not None or r["username"] is not None else None,
                },
            }
        )
        if len(salida) >= limit:
            break
    return salida


def purge_old(conn: sqlite3.Connection, days: int = KEEP_DAYS, now: Optional[datetime] = None) -> int:
    limite = _iso((now or _now()) - timedelta(days=days))
    return conn.execute("DELETE FROM page_activity WHERE ts < ?", (limite,)).rowcount
