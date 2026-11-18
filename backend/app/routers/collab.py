"""`WS /ws/pages/{id}`: la sala de edición en vivo de una nota (3.0, hito 4).

Protocolo (JSON, un mensaje por trama). Ver `app.collab` para el modelo.

Cliente → servidor:

- `{"type": "join", "lastSeenVersion": n | null, "epoch": "..." | null}` — el
  primero. Con `epoch` y `lastSeenVersion` de una conexión anterior (una
  reconexión), el servidor manda solo lo que falta; si no, el documento.
- `{"type": "update", "version": v, "updates": [{"clientID", "changes"}]}` —
  cambios hechos sobre la versión `v`.
- `{"type": "pull", "version": v}` — «mándame lo que hay desde `v`».
- `{"type": "presence", "anchor": a, "head": h, "version": v}` — dónde está
  el cursor (en el documento de la versión `v`).

Servidor → cliente:

- `{"type": "joined", "epoch", "version", "doc"? | "updates"?, "readonly",
  "peer", "peers": [...]}`.
- `{"type": "ack", "version": v, "updates": [...]}` — los propios cambios,
  aceptados a partir de la versión `v` (el cliente los reconoce por su
  `clientID` y los da por confirmados).
- `{"type": "update", "version": v, "updates": [...]}` — cambios de otros
  desde la versión `v`.
- `{"type": "reject", "version": v, "updates": [...]}` — el cambio se hizo
  sobre una versión vieja: aplica estos, rebasa los tuyos y vuelve a mandar.
  Con `"doc"` en vez de `"updates"`: no hay forma de ponerte al día, recarga.
- `{"type": "presence", "peer", "user": {id, name, color}, "anchor", "head",
  "version"}` o `{"type": "presence", "peer", "gone": true}`.
- `{"type": "gone"}` — la nota ya no existe.

Permisos (hito 3): al entrar se mira el nivel; con `view` se entra en modo
lectura (los `update` se rechazan). El nivel se vuelve a mirar en cada
`update`: a quien le quitan el permiso le dejan de valer sus cambios. La sesión
sale de la cookie (los middlewares HTTP no ven un WebSocket) y se comprueba que
el `Origin` es esta misma web: sin eso, otra página podría abrir la conexión con
la cookie del usuario.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import acl, auth
from app.collab import ExternalChange, Peer, Rooms, user_info
from app.config import settings
from app.index import open_index
from app.repository import Repository
from app.routers.pages import avisar_de_cambio

router = APIRouter(tags=["collab"])

MAX_MESSAGE_BYTES = 4 * 1024 * 1024


# Lo que tiene sala: las notas y, desde que se abren como páginas normales, las
# filas de una base (su cuerpo es un `.md` como el de una nota). Una base no:
# su «cuerpo» es la definición, que no se escribe a mano.
EDITABLE = ("note", "database_row")


def _load(page_id: str):
    conn = open_index(settings.index_path)
    try:
        row = conn.execute(
            "SELECT type, body FROM pages WHERE id = ?", (page_id,)
        ).fetchone()
        if row is None or row["type"] not in EDITABLE:
            return None
        return row["body"] or ""
    finally:
        conn.close()


def _store(page_id: str, body: str, expected: str) -> str:
    """Guarda el cuerpo de la sala si la base sigue con el que guardó la sala."""
    conn = open_index(settings.index_path)
    try:
        repo = Repository(conn)
        with repo._tx():
            actual = repo.get_page(uuid.UUID(page_id))
            if actual is None:
                raise LookupError(page_id)
            if (actual.body_markdown or "") != (expected or ""):
                raise ExternalChange(actual.body_markdown or "")
            repo.update_page(uuid.UUID(page_id), body_markdown=body)
        guardada = repo.get_page(uuid.UUID(page_id))
        return guardada.body_markdown if guardada else body
    finally:
        conn.close()


def _store_meta(page_id: str, fields: dict) -> dict:
    """Guarda título/descripción/icono desde la sala (como un `PATCH`)."""
    conn = open_index(settings.index_path)
    try:
        repo = Repository(conn)
        antes = repo.get_page(uuid.UUID(page_id))
        if antes is None:
            raise LookupError(page_id)
        page = repo.update_page(uuid.UUID(page_id), **fields)
        if page is None:
            raise LookupError(page_id)
        avisar_de_cambio(antes, page)
        return {
            "title": page.title,
            "description": page.description or "",
            "icon": page.icon,
            "version": page.version,
            "updated_at": page.updated_at.isoformat() if page.updated_at else None,
        }
    finally:
        conn.close()


rooms = Rooms(load=_load, store=_store, store_meta=_store_meta)


def _same_origin(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if not origin:
        return True  # clientes que no son un navegador (tests, scripts) no lo mandan
    host = ws.headers.get("host", "")
    return urlsplit(origin).netloc == host


def _who(page_id: str, token: str | None):
    """(usuario, nivel) de la cookie sobre la nota; nivel None si no la ve."""
    conn = open_index(settings.index_path)
    try:
        user = auth.user_for_token(conn, token) if token else None
        if user is None:
            return None, None
        row = conn.execute("SELECT type FROM pages WHERE id = ?", (page_id,)).fetchone()
        if row is None or row["type"] not in EDITABLE:
            return user, None
        return user, acl.level_for(conn, user, page_id)
    finally:
        conn.close()


@router.websocket("/ws/pages/{page_id}")
async def page_room(websocket: WebSocket, page_id: uuid.UUID):
    from app.startup import startup_index

    pid = str(page_id)
    # Se acepta ANTES de decidir: si no, el navegador solo ve un 403 en el
    # handshake y no sabe por qué. Aceptada, el cierre lleva su código:
    # 4403 otro origen, 4401 sin sesión, 4404 no la ve (o no existe), 1013
    # «aún arrancando, vuelve a probar».
    if not _same_origin(websocket):
        await websocket.accept()
        await websocket.close(code=4403)
        return
    await websocket.accept()
    if startup_index.snapshot().get("status") != "ok":
        await websocket.close(code=1013)
        return
    user, level = await asyncio.to_thread(_who, pid, websocket.cookies.get(auth.COOKIE_NAME))
    if user is None:
        await websocket.close(code=4401)
        return
    if level is None:
        await websocket.close(code=4404)
        return

    send_lock = asyncio.Lock()

    async def send(message: dict) -> None:
        async with send_lock:
            await websocket.send_text(json.dumps(message, ensure_ascii=False))

    peer = Peer(
        send=send,
        user_id=str(user.id),
        name=user.display_name,
        color=getattr(user, "color", None),  # la sala le da su color de cursor al entrar
        avatar=getattr(user, "avatar", None),
        readonly=level == "view",
    )
    try:
        first = await _receive(websocket)
        if not first or first.get("type") != "join":
            await websocket.close(code=4400)
            return
        try:
            room = await rooms.join(pid, peer)
        except LookupError:
            await websocket.close(code=4404)
            return
        try:
            await _joined(room, peer, first)
            while True:
                msg = await _receive(websocket)
                if msg is None:
                    continue
                kind = msg.get("type")
                if kind == "update":
                    nivel = await asyncio.to_thread(_who, pid, websocket.cookies.get(auth.COOKIE_NAME))
                    peer.readonly = nivel[1] not in ("edit", "owner")
                    await rooms.push(room, peer, msg.get("version"), msg.get("updates"))
                elif kind == "meta":
                    nivel = await asyncio.to_thread(_who, pid, websocket.cookies.get(auth.COOKIE_NAME))
                    peer.readonly = nivel[1] not in ("edit", "owner")
                    await rooms.meta(room, peer, msg.get("seq"), msg.get("fields"))
                elif kind == "pull":
                    await rooms.pull(room, peer, msg.get("version"))
                elif kind == "presence":
                    await rooms.presence(room, peer, msg)
        finally:
            await rooms.leave(room, peer)
    except WebSocketDisconnect:
        return


async def _receive(ws: WebSocket):
    text = await ws.receive_text()
    if len(text) > MAX_MESSAGE_BYTES:
        await ws.close(code=1009)
        raise WebSocketDisconnect(1009)
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _joined(room, peer: Peer, join: dict) -> None:
    async with room.lock:
        msg = {
            "type": "joined",
            "epoch": room.epoch,
            "readonly": peer.readonly,
            "peer": peer.peer_id,
            "peers": [
                {"peer": p.peer_id, "user": user_info(p),
                 **(p.presence or {})}
                for p in room.peers.values() if p is not peer
            ],
        }
        last = join.get("lastSeenVersion")
        faltan = (
            room.updates_since(last)
            if join.get("epoch") == room.epoch and isinstance(last, int)
            else None
        )
        if faltan is not None:
            msg.update({"version": last, "updates": faltan})
        else:
            msg.update({"version": room.version, "doc": room.doc})
        await peer.send(msg)
