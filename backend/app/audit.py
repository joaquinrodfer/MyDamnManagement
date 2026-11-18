"""Registro de auditoría (plan H8): quién hizo qué y cuándo.

Cada acción crítica deja una fila en `audit_logs`: crear un espacio, cambiar un
permiso, borrar una página, exportar, crear un usuario, descargar una copia…
Es solo para el superadministrador (`GET /audit`).

**Nunca rompe una petición.** Un fallo al auditar se anota en el log y la acción
sigue: la auditoría observa, no decide. Tampoco guarda contenido (cuerpos,
contraseñas): solo qué cosa, quién y un puñado de datos para entenderla
(`metadata`, un JSON corto).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("app.audit")

MAX_LIMIT = 200


def record(
    conn: sqlite3.Connection,
    actor: Any,
    action: str,
    *,
    workspace_id: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[Any] = None,
    target_name: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Anota una acción. `actor` es el usuario de la sesión (o `None`: el sistema)."""
    try:
        conn.execute(
            "INSERT INTO audit_logs (id, ts, actor_id, actor_name, workspace_id, action,"
            " target_type, target_id, target_name, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                datetime.now(timezone.utc).isoformat(),
                str(actor.id) if actor is not None else None,
                getattr(actor, "display_name", None) if actor is not None else "sistema",
                workspace_id,
                action,
                target_type,
                str(target_id) if target_id is not None else None,
                (target_name or "")[:200] or None,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True)[:2000] if metadata else None,
            ),
        )
    except Exception:  # noqa: BLE001 - auditar no puede romper la acción
        log.exception("no se pudo anotar la acción %s", action)


def query(
    conn: sqlite3.Connection,
    *,
    action: Optional[str] = None,
    actor_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Las filas que cumplen los filtros, de la más nueva a la más vieja."""
    where, args = ["1"], []
    if action:
        where.append("l.action LIKE ?")
        args.append(f"{action}%")
    if actor_id:
        where.append("l.actor_id = ?")
        args.append(actor_id)
    if workspace_id:
        where.append("l.workspace_id = ?")
        args.append(workspace_id)
    if since:
        where.append("l.ts >= ?")
        args.append(since)
    if until:
        where.append("l.ts < ?")
        args.append(until)
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT count(*) FROM audit_logs l WHERE {clause}", args).fetchone()[0]
    rows = conn.execute(
        "SELECT l.*, w.name AS workspace_name FROM audit_logs l"
        " LEFT JOIN workspaces w ON w.id = l.workspace_id"
        f" WHERE {clause} ORDER BY l.ts DESC, l.id LIMIT ? OFFSET ?",
        (*args, max(1, min(limit, MAX_LIMIT)), max(0, offset)),
    ).fetchall()
    items = []
    for r in rows:
        try:
            meta = json.loads(r["metadata"]) if r["metadata"] else None
        except ValueError:
            meta = None
        items.append({
            "id": r["id"], "ts": r["ts"], "actor_id": r["actor_id"], "actor_name": r["actor_name"],
            "workspace_id": r["workspace_id"], "workspace_name": r["workspace_name"],
            "action": r["action"], "target_type": r["target_type"], "target_id": r["target_id"],
            "target_name": r["target_name"], "metadata": meta,
        })
    return {"total": total, "items": items}


def actions(conn: sqlite3.Connection) -> list[str]:
    """Las acciones distintas que hay (para el filtro de la pantalla)."""
    return [r[0] for r in conn.execute("SELECT DISTINCT action FROM audit_logs ORDER BY action")]
