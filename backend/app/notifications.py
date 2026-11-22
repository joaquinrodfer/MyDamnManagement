"""Avisos (plan H6): menciones, asignaciones, plazos y cambios de una página.

Cada aviso es una fila de `notifications` para **una** persona. Se generan en
cuatro sitios y todos pasan por `notify`, que decide si llega:

- **Menciones** (`@usuario` en el cuerpo de una página): al guardar, las que no
  estaban antes. Una por página y día y persona.
- **Asignaciones** (`task_assigned`): una celda de persona de una fila pasa a
  ser alguien (que no es quien la cambia).
- **Plazos** (`deadline_soon`, `deadline_overdue`): un trabajador cada hora mira
  las tareas abiertas de cada persona; un aviso por tarea, tipo y día.
- **Cambios** (`page_update`): solo para quien se suscribió a la página con
  `all`, y **agrupados**: como mucho uno por página y hora, porque la edición en
  vivo guarda cada segundo.

**Suscripción por página** (`page_subscriptions`): `all` (todo, incluido el
cambio de cuerpo), `mentions_only` (por defecto: menciones, asignaciones y
plazos) y `muted` (nada). Quien no puede ver la página no recibe avisos de ella.

El aviso no cuenta nada que su destinatario no pueda abrir: antes de crearlo se
comprueba que ve la página.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app import acl
from app import index as index_mod
from app.events import bus

log = logging.getLogger("app.notifications")

TYPES = ("mention", "task_assigned", "deadline_soon", "deadline_overdue", "page_update")
MODES = ("all", "mentions_only", "muted")
DEFAULT_MODE = "mentions_only"

MENTION_RE = re.compile(r"(?<![\w@/])@([a-z][a-z0-9_]{2,31})\b")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------- suscripción


def get_mode(conn: sqlite3.Connection, user_id: str, page_id: str) -> str:
    row = conn.execute(
        "SELECT mode FROM page_subscriptions WHERE user_id = ? AND page_id = ?",
        (str(user_id), str(page_id)),
    ).fetchone()
    return row[0] if row else DEFAULT_MODE


def set_mode(conn: sqlite3.Connection, user_id: str, page_id: str, mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"modo desconocido: {mode!r}")
    if mode == DEFAULT_MODE:
        conn.execute(
            "DELETE FROM page_subscriptions WHERE user_id = ? AND page_id = ?",
            (str(user_id), str(page_id)),
        )
    else:
        conn.execute(
            "INSERT INTO page_subscriptions (user_id, page_id, mode) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id, page_id) DO UPDATE SET mode = excluded.mode",
            (str(user_id), str(page_id), mode),
        )
    return mode


# --------------------------------------------------------------------- crear


class _Person:
    """Lo mínimo que `acl.level_for` necesita de quien recibe el aviso."""

    def __init__(self, user_id: str, is_admin: bool) -> None:
        self.id = user_id
        self.is_admin = is_admin


def _person(conn: sqlite3.Connection, user_id: str) -> Optional[_Person]:
    row = conn.execute("SELECT id, is_admin FROM users WHERE id = ?", (str(user_id),)).fetchone()
    return _Person(row["id"], bool(row["is_admin"])) if row else None


def notify(
    conn: sqlite3.Connection,
    user_id: str,
    type_: str,
    message: str,
    *,
    page_id: Optional[str] = None,
    dedupe_key: Optional[str] = None,
) -> bool:
    """Crea un aviso si procede; devuelve si se creó.

    No se crea si la persona no existe, no ve la página, la silenció, solo
    quiere menciones y esto es un cambio, o ya hay uno con la misma clave.
    """
    if type_ not in TYPES:
        raise ValueError(f"tipo de aviso desconocido: {type_!r}")
    person = _person(conn, user_id)
    if person is None:
        return False
    workspace_id = None
    if page_id:
        row = conn.execute("SELECT workspace_id FROM pages WHERE id = ?", (str(page_id),)).fetchone()
        if row is None:
            return False
        workspace_id = row["workspace_id"]
        if acl.level_for(conn, person, page_id) is None:
            return False
        mode = get_mode(conn, user_id, page_id)
        if mode == "muted":
            return False
        if type_ == "page_update" and mode != "all":
            return False
    elif type_ == "page_update":
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO notifications"
        " (id, user_id, workspace_id, page_id, type, message, is_read, created_at, dedupe_key)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (
            str(uuid.uuid4()), str(user_id), workspace_id, str(page_id) if page_id else None,
            type_, message[:300], _now().isoformat(), dedupe_key,
        ),
    )
    if cur.rowcount:
        bus.publish({"type": "notification", "action": "created", "user_id": str(user_id)})
    return bool(cur.rowcount)


# ------------------------------------------------------------ cuándo crearlos


def mentions_in(conn: sqlite3.Connection, body: Optional[str]) -> set[str]:
    """Los ids de los usuarios que nombra el texto con `@usuario`."""
    names = set(MENTION_RE.findall(body or ""))
    if not names:
        return set()
    marks = ",".join("?" * len(names))
    return {
        r["id"]
        for r in conn.execute(f"SELECT id FROM users WHERE username IN ({marks})", tuple(names))
    }


def after_body_change(
    conn: sqlite3.Connection,
    page_id: Any,
    old_body: Optional[str],
    new_body: Optional[str],
    actor_id: Optional[str],
) -> None:
    """Menciones nuevas y, para quien se suscribió a todo, el cambio (agrupado)."""
    page_id = str(page_id)
    row = conn.execute("SELECT title FROM pages WHERE id = ?", (page_id,)).fetchone()
    if row is None or (old_body or "") == (new_body or ""):
        return
    title = row["title"] or "Sin título"
    today = _now().date().isoformat()
    for uid in mentions_in(conn, new_body) - mentions_in(conn, old_body):
        if uid == actor_id:
            continue
        notify(conn, uid, "mention", f"Te han mencionado en «{title}»",
               page_id=page_id, dedupe_key=f"mention:{page_id}:{today}")
    hour = _now().strftime("%Y-%m-%dT%H")
    for sub in conn.execute(
        "SELECT user_id FROM page_subscriptions WHERE page_id = ? AND mode = 'all'", (page_id,)
    ).fetchall():
        if sub["user_id"] == actor_id:
            continue
        notify(conn, sub["user_id"], "page_update", f"«{title}» se ha editado",
               page_id=page_id, dedupe_key=f"update:{page_id}:{hour}")


def after_row_change(
    conn: sqlite3.Connection,
    schema_def: Iterable[dict],
    row_id: Any,
    old_props: Optional[dict],
    new_props: Optional[dict],
    actor_id: Optional[str],
) -> None:
    """Quien pasa a ser responsable de una fila recibe un aviso."""
    row = conn.execute("SELECT title FROM pages WHERE id = ?", (str(row_id),)).fetchone()
    if row is None:
        return
    title = row["title"] or "Sin título"
    today = _now().date().isoformat()
    for prop in schema_def or []:
        if not isinstance(prop, dict) or prop.get("type") != "person" or not prop.get("key"):
            continue
        antes = (old_props or {}).get(prop["key"])
        ahora = (new_props or {}).get(prop["key"])
        if not ahora or ahora == antes or ahora == actor_id:
            continue
        for uid in (ahora if isinstance(ahora, list) else [ahora]):
            if isinstance(uid, str) and uid and uid != actor_id and uid not in (antes if isinstance(antes, list) else [antes]):
                notify(conn, uid, "task_assigned", f"Te han asignado «{title}»",
                       page_id=str(row_id), dedupe_key=f"assign:{row_id}:{uid}:{today}")


def scan_deadlines(conn: sqlite3.Connection) -> int:
    """Avisos de plazo de lo que cada persona tiene abierto. Devuelve cuántos creó."""
    from app import mytasks  # import tardío: ese módulo usa los de arriba

    hoy = _now().date().isoformat()
    creados = 0
    for user in conn.execute("SELECT id, is_admin FROM users").fetchall():
        visible = acl.visible_ids(conn, _Person(user["id"], False))
        data = mytasks.my_tasks(conn, user["id"], visible)
        for task in data.get("tasks", []):
            if task["due"] == "overdue":
                tipo, texto = "deadline_overdue", f"«{task['title']}» está vencida"
            elif task.get("days") in (0, 1):
                cuando = "hoy" if task["days"] == 0 else "mañana"
                tipo, texto = "deadline_soon", f"«{task['title']}» vence {cuando}"
            else:
                continue
            clave = f"{'overdue' if tipo == 'deadline_overdue' else 'soon'}:{task['row_id']}:{hoy}"
            if notify(conn, user["id"], tipo, texto, page_id=task["row_id"], dedupe_key=clave):
                creados += 1
            if tipo == "deadline_overdue":
                # Plan H9: el webhook del espacio, una vez por tarea y día.
                from app import webhooks

                webhooks.emit_task(conn, "task.overdue", task["row_id"], dedupe_key=f"overdue:{task['row_id']}:{hoy}")
    return creados


# ------------------------------------------------------------------- leer


def list_for(
    conn: sqlite3.Connection, user_id: str, *, unread_only: bool = False, limit: int = 50
) -> dict[str, Any]:
    where = "n.user_id = ?" + (" AND n.is_read = 0" if unread_only else "")
    rows = conn.execute(
        "SELECT n.id, n.type, n.message, n.page_id, n.workspace_id, n.is_read, n.created_at,"
        " p.title AS page_title, p.type AS page_type, p.parent_id AS parent_id,"
        " d.id AS database_id, w.name AS workspace_name"
        " FROM notifications n LEFT JOIN pages p ON p.id = n.page_id"
        " LEFT JOIN rows r ON r.id = n.page_id LEFT JOIN workspaces w ON w.id = n.workspace_id"
        " LEFT JOIN databases d ON d.id = r.database_id"
        f" WHERE {where} ORDER BY n.created_at DESC LIMIT ?",
        (str(user_id), max(1, min(limit, 200))),
    ).fetchall()
    return {
        "unread": unread_count(conn, user_id),
        "items": [
            {
                "id": r["id"], "type": r["type"], "message": r["message"],
                "page_id": r["page_id"], "page_title": r["page_title"],
                "workspace_id": r["workspace_id"], "workspace_name": r["workspace_name"],
                "is_read": bool(r["is_read"]), "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


def unread_count(conn: sqlite3.Connection, user_id: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM notifications WHERE user_id = ? AND is_read = 0", (str(user_id),)
    ).fetchone()[0]


def mark_read(conn: sqlite3.Connection, user_id: str, ids: Optional[list[str]] = None) -> int:
    """Marca como leídos estos avisos (o todos). Solo toca los de `user_id`."""
    if ids is None:
        cur = conn.execute(
            "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0", (str(user_id),)
        )
    else:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0 AND id IN ({marks})",
            (str(user_id), *[str(i) for i in ids]),
        )
    if cur.rowcount:
        bus.publish({"type": "notification", "action": "read", "user_id": str(user_id)})
    return cur.rowcount


def purge_old(conn: sqlite3.Connection, days: int = 90) -> int:
    """Quita los avisos leídos de hace más de `days` días."""
    limite = (_now() - timedelta(days=days)).isoformat()
    return conn.execute(
        "DELETE FROM notifications WHERE is_read = 1 AND created_at < ?", (limite,)
    ).rowcount


# --------------------------------------------------------------- el trabajador


class DeadlineWorker(threading.Thread):
    """Cada hora mira los plazos y limpia los avisos viejos."""

    def __init__(self, interval: float = 3600.0) -> None:
        super().__init__(name="deadline-worker", daemon=True)
        self.interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        # Una pasada al arrancar (tras un minuto, para no competir con el
        # reindexado) y luego cada hora.
        if self._stop_event.wait(60.0):
            return
        while True:
            self.tick()
            if self._stop_event.wait(self.interval):
                return

    def tick(self) -> int:
        from app.config import settings

        try:
            conn = index_mod.open_index(settings.index_path)
            try:
                creados = scan_deadlines(conn)
                purge_old(conn)
                from app import activity  # la actividad del inicio se poda con lo mismo

                activity.purge_old(conn)
                # Las suscripciones de festivos (URL y país) se releen a diario.
                from app import holiday_import

                try:
                    holiday_import.sync_due(conn)
                except Exception:  # noqa: BLE001 - sin red no se paran los plazos
                    log.exception("falló la actualización de los festivos")
                return creados
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - un fallo no mata el hilo
            log.exception("falló la pasada de plazos")
            return 0
