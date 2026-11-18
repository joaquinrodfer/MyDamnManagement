"""Webhooks salientes por espacio (plan H9).

Un espacio puede tener **una URL** a la que la app manda un JSON cuando pasa algo
en sus bases de Tareas: `task.created`, `task.completed` y `task.overdue`. Sirve
para Slack, Discord, Mattermost o n8n: el cuerpo lleva `text` (Slack y
Mattermost), `content` (Discord) y los datos estructurados (`event`, `workspace`,
`task`) para n8n.

**Seguridad.** Un webhook hace que el servidor pida una URL que escribe un
usuario: sin cuidado, sirve para tocar la red interna (SSRF). Por eso:

- solo `http` y `https`, sin credenciales en la URL;
- el nombre se resuelve **antes de conectar** y se rechaza si *alguna* de sus
  direcciones es privada, de bucle local, de enlace local, multicast, reservada
  o la de metadatos de la nube (`169.254.169.254`); y se conecta a la dirección
  ya comprobada (no se vuelve a resolver: no hay rebinding de DNS);
- no se siguen redirecciones;
- el cuerpo va **firmado**: `X-Noteping-Signature: sha256=` + HMAC-SHA256 de
  `<marca de tiempo>.<cuerpo>` con el secreto del espacio, que se enseña **una
  sola vez** al crearlo y no se vuelve a leer.

**Entrega.** Cada aviso es una fila de `webhook_deliveries` y un hilo las manda
(así los reintentos sobreviven a un reinicio): hasta 4 intentos con espera
creciente; después queda como `failed` con el motivo.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import secrets
import socket
import sqlite3
import ssl
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

log = logging.getLogger("app.webhooks")

EVENTS = ("task.created", "task.completed", "task.overdue")
MAX_ATTEMPTS = 4
BACKOFF = (0, 5, 30, 300)  # segundos de espera antes de cada intento
TIMEOUT = 8.0
MAX_URL = 500

# Direcciones que nunca se llaman aunque «parezcan» públicas.
_ALWAYS_BLOCKED = (ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fe80::/10"))


class WebhookError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------- comprobar la URL


def _addr(ip: str):
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr


def _always_blocked(ip: str) -> bool:
    """Enlace local (incluida la dirección de metadatos de la nube): ni con
    `WEBHOOK_ALLOW_PRIVATE`, que es para servicios de la propia red."""
    addr = _addr(ip)
    return any(addr in net for net in _ALWAYS_BLOCKED if net.version == addr.version)


def _is_internal(ip: str) -> bool:
    addr = _addr(ip)
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
        or addr.is_reserved or addr.is_unspecified or _always_blocked(ip)
    )


def resolve_public(host: str, port: int, allow_private: bool = False) -> list[str]:
    """Las direcciones de `host`, o `WebhookError` si alguna es interna."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebhookError(f"no se puede resolver {host!r}") from exc
    ips = list(dict.fromkeys(i[4][0] for i in infos))
    if not ips:
        raise WebhookError(f"{host!r} no tiene dirección")
    for ip in ips:
        if _always_blocked(ip) or (not allow_private and _is_internal(ip)):
            raise WebhookError(
                f"{host!r} apunta a una dirección interna ({ip}): un webhook solo llama a servidores públicos"
            )
    return ips


def validate_url(url: str, allow_private: Optional[bool] = None) -> str:
    """La URL limpia, o `WebhookError` si no vale (esquema, credenciales, red interna)."""
    from app.config import settings

    allow = settings.webhook_allow_private if allow_private is None else allow_private
    url = (url or "").strip()
    if not url or len(url) > MAX_URL:
        raise WebhookError(f"la URL es obligatoria (hasta {MAX_URL} caracteres)")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise WebhookError("la URL tiene que ser http o https")
    if parts.username or parts.password:
        raise WebhookError("la URL no puede llevar usuario ni contraseña")
    if not parts.hostname:
        raise WebhookError("la URL no tiene servidor")
    resolve_public(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), allow)
    return url


# ------------------------------------------------------------- configuración


def get_config(conn: sqlite3.Connection, workspace_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM workspace_webhooks WHERE workspace_id = ?", (workspace_id,)).fetchone()
    if row is None:
        return None
    return {
        "url": row["url"], "enabled": bool(row["enabled"]),
        "events": json.loads(row["events"] or "[]"), "secret_set": bool(row["secret"]),
    }


def set_config(
    conn: sqlite3.Connection, workspace_id: str, *, url: str, enabled: bool, events: list[str]
) -> tuple[dict, Optional[str]]:
    """Guarda la configuración. Devuelve `(config, secreto_nuevo)`: el secreto
    solo sale la primera vez (o al regenerarlo)."""
    url = validate_url(url)
    bad = [e for e in events if e not in EVENTS]
    if bad:
        raise WebhookError(f"evento desconocido: {bad[0]} (los hay: {', '.join(EVENTS)})")
    existing = conn.execute("SELECT secret FROM workspace_webhooks WHERE workspace_id = ?", (workspace_id,)).fetchone()
    secret = None if existing else secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO workspace_webhooks (workspace_id, url, secret, events, enabled, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(workspace_id) DO UPDATE SET url = excluded.url,"
        " events = excluded.events, enabled = excluded.enabled",
        (workspace_id, url, secret or "", json.dumps(sorted(set(events))), int(enabled), _now().isoformat()),
    )
    return get_config(conn, workspace_id), secret  # type: ignore[return-value]


def regenerate_secret(conn: sqlite3.Connection, workspace_id: str) -> str:
    if get_config(conn, workspace_id) is None:
        raise WebhookError("este espacio no tiene webhook", 404)
    secret = secrets.token_urlsafe(32)
    conn.execute("UPDATE workspace_webhooks SET secret = ? WHERE workspace_id = ?", (secret, workspace_id))
    return secret


def delete_config(conn: sqlite3.Connection, workspace_id: str) -> None:
    conn.execute("DELETE FROM workspace_webhooks WHERE workspace_id = ?", (workspace_id,))
    conn.execute("DELETE FROM webhook_deliveries WHERE workspace_id = ? AND status = 'pending'", (workspace_id,))


def recent(conn: sqlite3.Connection, workspace_id: str, limit: int = 10) -> list[dict]:
    return [
        {k: r[k] for k in ("id", "event", "status", "attempts", "last_error", "created_at", "delivered_at")}
        for r in conn.execute(
            "SELECT id, event, status, attempts, last_error, created_at, delivered_at FROM webhook_deliveries"
            " WHERE workspace_id = ? ORDER BY created_at DESC LIMIT ?", (workspace_id, limit))
    ]


# ------------------------------------------------------------------ encolar


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def build_payload(event: str, workspace: dict, text: str, data: dict) -> dict:
    return {
        "text": text,        # Slack y Mattermost
        "content": text,     # Discord
        "event": event,
        "workspace": workspace,
        "timestamp": _now().isoformat(),
        **data,
    }


def enqueue(
    conn: sqlite3.Connection,
    workspace_id: str,
    event: str,
    text: str,
    data: dict,
    dedupe_key: Optional[str] = None,
) -> bool:
    """Encola un envío si el espacio tiene el webhook activo y suscrito a `event`."""
    cfg = get_config(conn, workspace_id)
    if not cfg or not cfg["enabled"] or event not in cfg["events"]:
        return False
    ws = conn.execute("SELECT id, name FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if ws is None:
        return False
    payload = build_payload(event, {"id": ws["id"], "name": ws["name"]}, text, data)
    cur = conn.execute(
        "INSERT OR IGNORE INTO webhook_deliveries (id, workspace_id, event, payload, status, attempts,"
        " next_attempt, created_at, dedupe_key) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
        (str(uuid.uuid4()), workspace_id, event, json.dumps(payload, ensure_ascii=False),
         _now().isoformat(), _now().isoformat(), dedupe_key),
    )
    return bool(cur.rowcount)


def task_data(conn: sqlite3.Connection, row_id: str) -> Optional[tuple[str, dict, str]]:
    """`(workspace_id, datos, título)` de una fila de una base de Tareas, o `None`."""
    row = conn.execute(
        "SELECT p.title AS title, p.workspace_id AS ws, r.properties_json AS props, r.database_id AS db,"
        " k.kind_json AS kind, bp.title AS base_title, d.schema_json AS schema"
        " FROM rows r JOIN pages p ON p.id = r.id JOIN databases d ON d.id = r.database_id"
        " JOIN pages bp ON bp.id = d.page_id JOIN database_kinds k ON k.database_id = d.id"
        " WHERE r.id = ?", (row_id,)).fetchone()
    if row is None:
        return None
    kind = json.loads(row["kind"] or "null") or {}
    if kind.get("type") != "tasks":
        return None
    props = json.loads(row["props"] or "{}")
    roles = kind.get("roles") or {}

    def cell(role):
        return props.get(roles.get(role)) if roles.get(role) else None

    people = cell("assignee")
    ids = people if isinstance(people, list) else ([people] if people else [])
    names = [r["display_name"] for r in conn.execute(
        f"SELECT display_name FROM users WHERE id IN ({','.join('?' * len(ids))})", ids)] if ids else []
    return row["ws"], {
        "task": {
            "id": row_id, "title": row["title"], "base": row["base_title"],
            "status": cell("status"), "assignees": names, "hours": cell("hours"),
            "start": cell("start"), "end": cell("end"),
        }
    }, row["title"] or "Sin título"


def emit_task(conn: sqlite3.Connection, event: str, row_id: Any, dedupe_key: Optional[str] = None) -> bool:
    found = task_data(conn, str(row_id))
    if found is None:
        return False
    workspace_id, data, title = found
    texto = {
        "task.created": f"Tarea creada: «{title}»",
        "task.completed": f"Tarea completada: «{title}»",
        "task.overdue": f"Tarea vencida: «{title}»",
    }[event]
    if data["task"]["assignees"]:
        texto += f" ({', '.join(data['task']['assignees'])})"
    return enqueue(conn, workspace_id, event, texto, data, dedupe_key)


# ------------------------------------------------------------------- enviar


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Se conecta a la dirección ya comprobada, no a la que diga el DNS ahora."""

    def __init__(self, host, port, ip, **kw):
        super().__init__(host, port, **kw)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, **kw):
        super().__init__(host, port, context=ssl.create_default_context(), **kw)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def post(url: str, body: bytes, headers: dict[str, str], allow_private: Optional[bool] = None) -> int:
    """POST sin seguir redirecciones, a una dirección pública ya comprobada.
    Devuelve el código HTTP; lanza `WebhookError` si no se puede entregar."""
    from app.config import settings

    allow = settings.webhook_allow_private if allow_private is None else allow_private
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    ip = resolve_public(parts.hostname or "", port, allow)[0]
    cls = _PinnedHTTPSConnection if parts.scheme == "https" else _PinnedHTTPConnection
    conn = cls(parts.hostname, port, ip, timeout=TIMEOUT)
    try:
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        resp.read(2048)
        return resp.status
    except (OSError, http.client.HTTPException) as exc:
        raise WebhookError(f"no se pudo entregar: {exc}") from exc
    finally:
        conn.close()


def deliver(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Un intento de entrega. Devuelve si salió bien."""
    cfg = conn.execute("SELECT url, secret FROM workspace_webhooks WHERE workspace_id = ?", (row["workspace_id"],)).fetchone()
    intento = row["attempts"] + 1
    if cfg is None:
        conn.execute("UPDATE webhook_deliveries SET status='failed', last_error=? WHERE id=?",
                     ("el espacio ya no tiene webhook", row["id"]))
        return False
    body = row["payload"].encode()
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Noteping-Webhook/1",
        "X-Noteping-Event": row["event"],
        "X-Noteping-Delivery": row["id"],
        "X-Noteping-Timestamp": ts,
        "X-Noteping-Signature": _sign(cfg["secret"], ts, body),
    }
    error = None
    try:
        status = post(cfg["url"], body, headers)
        if 200 <= status < 300:
            conn.execute(
                "UPDATE webhook_deliveries SET status='delivered', attempts=?, delivered_at=?, last_error=NULL WHERE id=?",
                (intento, _now().isoformat(), row["id"]))
            return True
        error = f"el servidor contestó {status}"
    except WebhookError as exc:
        error = str(exc)
    if intento >= MAX_ATTEMPTS:
        conn.execute("UPDATE webhook_deliveries SET status='failed', attempts=?, last_error=? WHERE id=?",
                     (intento, error, row["id"]))
    else:
        espera = BACKOFF[min(intento, len(BACKOFF) - 1)]
        conn.execute("UPDATE webhook_deliveries SET attempts=?, last_error=?, next_attempt=? WHERE id=?",
                     (intento, error, (_now() + timedelta(seconds=espera)).isoformat(), row["id"]))
    return False


def send_pending(conn: sqlite3.Connection, limit: int = 20) -> int:
    """Manda lo que toque. Devuelve cuántos salieron bien."""
    ok = 0
    for row in conn.execute(
        "SELECT * FROM webhook_deliveries WHERE status = 'pending' AND next_attempt <= ?"
        " ORDER BY created_at LIMIT ?", (_now().isoformat(), limit)).fetchall():
        if deliver(conn, row):
            ok += 1
    return ok


def purge_old(conn: sqlite3.Connection, days: int = 30) -> int:
    limite = (_now() - timedelta(days=days)).isoformat()
    return conn.execute("DELETE FROM webhook_deliveries WHERE status != 'pending' AND created_at < ?", (limite,)).rowcount


class WebhookWorker(threading.Thread):
    """Manda las entregas pendientes cada pocos segundos."""

    def __init__(self, interval: float = 3.0) -> None:
        super().__init__(name="webhook-worker", daemon=True)
        self.interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        ciclos = 0
        while not self._stop_event.wait(self.interval):
            self.tick()
            ciclos += 1
            if ciclos % 1200 == 0:
                self.tick(purge=True)

    def tick(self, purge: bool = False) -> int:
        from app import index as index_mod
        from app.config import settings

        try:
            conn = index_mod.open_index(settings.index_path)
            try:
                if purge:
                    purge_old(conn)
                return send_pending(conn)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            log.exception("falló el envío de webhooks")
            return 0
