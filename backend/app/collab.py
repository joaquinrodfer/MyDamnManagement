"""Colaboración en vivo con autoridad en el servidor (3.0, hito 4; ADR-0009).

Una **sala** por nota abierta. La sala tiene el texto vivo de la nota, un
número de `version` (cuántos cambios ha aceptado desde que se abrió) y el
registro de los últimos cambios. Es el modelo de `prosemirror-collab` /
`@codemirror/collab`, que es el que pide el ADR-0009:

- el cliente manda sus cambios con la versión **desde la que los hizo**;
- si es la de la sala, se aplican, se guardan y se reparten a todos (al que
  los mandó como `ack`, a los demás como `update`);
- si no, `reject` con los cambios que le faltan: el cliente los aplica,
  **rebasa** los suyos encima (eso lo hace `@codemirror/collab` en el
  navegador) y los vuelve a mandar. Si le faltan demasiados (más de lo que
  guarda el registro) o la sala es otra (se reinició), `reject` con el
  documento entero y el cliente recarga.

El servidor nunca rebasa ni fusiona: solo aplica cambios hechos sobre su propia
versión, así que no puede producir un documento que ningún cliente haya visto.
Un cambio que no encaja (longitudes que no cuadran) se rechaza entero.

**Formato de un cambio**: el `ChangeSet.toJSON()` de CodeMirror. Una lista de
tramos: un número es «deja N unidades como están»; una lista `[n, ...líneas]`
es «borra N unidades y pon estas líneas unidas por saltos». Las longitudes son
en **unidades UTF-16** (las de una cadena de JavaScript), no en caracteres de
Python: un emoji fuera del plano básico mide 2. `apply_changes` trabaja sobre
UTF-16 por eso.

**Memoria** (hito 5): nada por nota en RAM salvo las salas abiertas. Una sala
nace con el primer cliente y muere con el último (después de guardar). El
registro de cambios está acotado (`LOG_LIMIT`).

**Guardado**: la sala escribe el cuerpo en la base (`Repository.update_page`,
la misma ruta que el `PATCH`) como mucho cada `PERSIST_DELAY` segundos y al
cerrarse. Antes de escribir mira si el cuerpo de la base sigue siendo el que
ella guardó: si alguien lo cambió por otro camino (un `PATCH` sin sala, una
sugerencia aceptada, el vigilante con proyección), la sala se reinicia con el
cuerpo de la base y los clientes recargan. Es el caso raro; lo normal es que
todo el que edita esté en la sala.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("mdm.collab")

LOG_LIMIT = 500           # cambios que se recuerdan para rebasar a un cliente atrasado
PERSIST_DELAY = 0.5       # segundos entre guardados de una sala con cambios
MAX_DOC_UNITS = 2_000_000  # tope del documento (unidades UTF-16): ~4 MB
MAX_UPDATES_PER_MESSAGE = 200
# Colores de cursor (bloque B). No son el color del usuario: cada persona que
# entra en una nota recibe uno que no tenga nadie más DENTRO de esa nota, al
# azar entre los libres. Así dos personas en la misma nota nunca se confunden,
# aunque su color de usuario fuera el mismo. Doce tonos que se distinguen bien
# entre sí y sobre fondo claro y oscuro.
CURSOR_COLORS = (
    "#e5484d", "#3e63dd", "#30a46c", "#f76b15", "#8e4ec6", "#12a594",
    "#d6409f", "#ffb224", "#0090ff", "#a18072", "#5bb98b", "#e93d82",
)

# Lo que viaja por `meta` (título, descripción, icono): campos sueltos, no
# texto colaborativo. Gana el último que escribe, como en cualquier campo de
# una línea; el cuerpo sigue siendo el único documento con rebase.
META_FIELDS = {"title": 500, "description": 5000, "icon": 64}


class BadChange(ValueError):
    """Un cambio que no encaja en el documento (o no tiene la forma de uno)."""


# --------------------------------------------------------------------------
# aplicar un ChangeSet de CodeMirror
# --------------------------------------------------------------------------


def _units(text: str) -> bytes:
    return text.encode("utf-16-le", "surrogatepass")


def utf16_len(text: str) -> int:
    return len(_units(text)) // 2


def apply_changes(doc: str, changes: Any) -> str:
    """Aplica un `ChangeSet.toJSON()` a `doc`. `BadChange` si no encaja."""
    if not isinstance(changes, list):
        raise BadChange("un cambio es una lista de tramos")
    src = _units(doc)
    total = len(src) // 2
    out = bytearray()
    pos = 0
    for part in changes:
        if isinstance(part, bool):
            raise BadChange("tramo no válido")
        if isinstance(part, int):
            if part < 0 or pos + part > total:
                raise BadChange("el cambio se sale del documento")
            out += src[pos * 2:(pos + part) * 2]
            pos += part
            continue
        if not isinstance(part, list) or not part or not isinstance(part[0], int) or isinstance(part[0], bool):
            raise BadChange("tramo no válido")
        borrar = part[0]
        if borrar < 0 or pos + borrar > total:
            raise BadChange("el cambio borra fuera del documento")
        lineas = part[1:]
        if not all(isinstance(x, str) for x in lineas):
            raise BadChange("el texto insertado no es texto")
        pos += borrar
        if lineas:
            out += _units("\n".join(lineas))
    if pos != total:
        raise BadChange(
            f"el cambio es para un documento de {pos} unidades y este mide {total}"
        )
    if len(out) // 2 > MAX_DOC_UNITS:
        raise BadChange("el documento pasaría del tamaño máximo")
    try:
        return out.decode("utf-16-le")
    except UnicodeDecodeError as exc:  # partió un par sustituto (un emoji) por la mitad
        raise BadChange("el cambio corta un carácter por la mitad") from exc


# --------------------------------------------------------------------------
# salas
# --------------------------------------------------------------------------


@dataclass
class Peer:
    """Un cliente conectado a una sala."""

    send: Any                 # corutina: send(dict)
    user_id: str
    name: str
    color: Optional[str]
    readonly: bool
    peer_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    presence: Optional[dict] = None
    avatar: Optional[str] = None


@dataclass
class Room:
    page_id: str
    doc: str
    persisted_body: str
    epoch: str = field(default_factory=lambda: uuid.uuid4().hex)
    version: int = 0
    log: list[dict] = field(default_factory=list)  # los últimos cambios aceptados
    log_start: int = 0                               # versión del primero de `log`
    peers: dict[str, Peer] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dirty: bool = False
    persist_task: Optional[asyncio.Task] = None
    closed: bool = False
    last_persist_ms: float = 0.0
    # Los `meta` se guardan de uno en uno y en el orden en que llegan: dos
    # títulos seguidos no pueden quedar guardados al revés.
    meta_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Quién ha escrito desde el último guardado (para la actividad del inicio).
    editors: set = field(default_factory=set)

    def updates_since(self, version: int) -> Optional[list[dict]]:
        """Los cambios desde `version`, o None si ya no están en el registro."""
        if version < self.log_start or version > self.version:
            return None
        return self.log[version - self.log_start:]

    def remember(self, updates: list[dict]) -> None:
        self.log.extend(updates)
        self.version += len(updates)
        sobra = len(self.log) - LOG_LIMIT
        if sobra > 0:
            del self.log[:sobra]
            self.log_start += sobra


class Rooms:
    """El registro de salas abiertas del proceso.

    `load(page_id)` y `store(page_id, body, expected)` son las dos únicas
    puertas a la base (síncronas: se llaman en un hilo, no en el bucle). Así
    las salas no saben de SQLite y los tests pueden sustituirlas.
    """

    def __init__(self, load, store, store_meta=None, record_activity=None):
        self._load = load
        self._store = store
        # `record_activity(page_id, user_ids)` apunta quién editó (opcional;
        # síncrono, se llama en un hilo tras un guardado que ha ido bien).
        self._record_activity = record_activity
        # `store_meta(page_id, fields) -> dict` guarda título/descripción/icono
        # y devuelve lo que quedó (el título puede salir saneado) con la
        # versión nueva. Sin él, la sala no acepta `meta`.
        self._store_meta = store_meta
        self._rooms: dict[str, Room] = {}
        self._guard = asyncio.Lock()
        # Lo que tardan los últimos guardados (ms), para `/status` (hito 5).
        # Acotado: no crece con el tiempo ni con el número de notas.
        self.persist_ms: deque[float] = deque(maxlen=500)

    def stats(self) -> dict:
        """Salas y guardados, para `/status`: lo que hace falta para vigilarlas."""
        tiempos = sorted(self.persist_ms)

        def pct(p: float):
            return round(tiempos[min(len(tiempos) - 1, int(len(tiempos) * p))], 1) if tiempos else None

        return {
            "rooms": len(self._rooms),
            "peers": sum(len(r.peers) for r in self._rooms.values()),
            "persists": len(tiempos),
            "persist_ms_p50": pct(0.5),
            "persist_ms_p95": pct(0.95),
        }

    @property
    def open_rooms(self) -> int:
        return len(self._rooms)

    async def join(self, page_id: str, peer: Peer) -> Room:
        async with self._guard:
            room = self._rooms.get(page_id)
            if room is None or room.closed:
                body = await asyncio.to_thread(self._load, page_id)
                if body is None:
                    raise LookupError(page_id)
                room = Room(page_id=page_id, doc=body, persisted_body=body)
                self._rooms[page_id] = room
            peer.color = _cursor_color(room, peer)
            room.peers[peer.peer_id] = peer
            return room

    async def leave(self, room: Room, peer: Peer) -> None:
        async with room.lock:
            room.peers.pop(peer.peer_id, None)
            vacia = not room.peers
        await self._broadcast(room, {"type": "presence", "peer": peer.peer_id, "gone": True})
        if vacia:
            # El último en salir: se guarda lo pendiente y la sala muere (nada
            # por nota se queda en memoria). `shield`: si la conexión se cancela
            # (el servidor se apaga), el guardado termina igual.
            await asyncio.shield(self.flush(room))
            async with self._guard:
                if not room.peers and self._rooms.get(room.page_id) is room:
                    room.closed = True
                    del self._rooms[room.page_id]

    # ------------------------------------------------------------ cambios

    async def push(self, room: Room, peer: Peer, version: Any, updates: Any) -> None:
        """Un `update` del cliente: se aplica, se rechaza o se le pone al día."""
        if peer.readonly:
            await peer.send({"type": "reject", "reason": "readonly", "version": room.version})
            return
        if not isinstance(version, int) or not isinstance(updates, list) or not updates:
            await peer.send({"type": "error", "reason": "update mal formado"})
            return
        if len(updates) > MAX_UPDATES_PER_MESSAGE:
            await peer.send({"type": "error", "reason": "demasiados cambios en un mensaje"})
            return
        async with room.lock:
            if version != room.version:
                await self._reject(room, peer, version)
                return
            doc = room.doc
            aceptados = []
            try:
                for upd in updates:
                    if not isinstance(upd, dict):
                        raise BadChange("cada cambio es un objeto")
                    doc = apply_changes(doc, upd.get("changes"))
                    aceptados.append(
                        {"clientID": str(upd.get("clientID") or peer.peer_id), "changes": upd["changes"]}
                    )
            except BadChange as exc:
                # El cliente y la sala no ven el mismo documento: que recargue.
                log.warning("sala %s: cambio rechazado (%s)", room.page_id, exc)
                await peer.send({
                    "type": "reject", "reason": f"cambio no válido: {exc}",
                    "epoch": room.epoch, "version": room.version, "doc": room.doc,
                })
                return
            desde = room.version
            room.doc = doc
            room.remember(aceptados)
            room.dirty = True
            if peer.user_id:
                room.editors.add(peer.user_id)
            otros = [p for p in room.peers.values() if p is not peer]
            await peer.send({"type": "ack", "version": desde, "updates": aceptados})
        for p in otros:
            await _safe_send(p, {"type": "update", "version": desde, "updates": aceptados})
        self._schedule_persist(room)

    async def meta(self, room: Room, peer: Peer, seq: Any, fields: Any) -> None:
        """Título, descripción o icono nuevos: se guardan y se reparten.

        A quien lo manda le vuelve `meta-ack` con lo que quedó guardado (y la
        versión); a los demás, `meta` con lo mismo, para que lo vean al momento.
        """
        if peer.readonly:
            await peer.send({"type": "meta-reject", "seq": seq, "reason": "readonly"})
            return
        if (
            self._store_meta is None
            or not isinstance(fields, dict)
            or not fields
            or any(
                k not in META_FIELDS or not isinstance(v, str) or len(v) > META_FIELDS[k]
                for k, v in fields.items()
            )
        ):
            await peer.send({"type": "meta-reject", "seq": seq, "reason": "meta mal formado"})
            return
        async with room.meta_lock:
            try:
                saved = await asyncio.to_thread(self._store_meta, room.page_id, dict(fields))
            except LookupError:
                await self._broadcast(room, {"type": "gone"})
                return
            except Exception as exc:  # noqa: BLE001 - p. ej. un nombre que el vault no admite
                log.warning("sala %s: meta no guardado (%s)", room.page_id, exc)
                await peer.send({"type": "meta-reject", "seq": seq, "reason": str(exc)})
                return
        await peer.send({"type": "meta-ack", "seq": seq, "fields": saved})
        await self._broadcast(
            room, {"type": "meta", **_peer_info(peer), "fields": saved}, skip=peer
        )

    async def pull(self, room: Room, peer: Peer, version: Any) -> None:
        async with room.lock:
            await self._reject(room, peer, version if isinstance(version, int) else -1, kind="update")

    async def _reject(self, room: Room, peer: Peer, version: int, kind: str = "reject") -> None:
        faltan = room.updates_since(version)
        if faltan is None:
            await peer.send({
                "type": "reject", "reason": "versión fuera del registro",
                "epoch": room.epoch, "version": room.version, "doc": room.doc,
            })
            return
        await peer.send({"type": kind, "version": version, "updates": faltan})

    async def presence(self, room: Room, peer: Peer, data: dict) -> None:
        cursor = {
            "anchor": data.get("anchor"), "head": data.get("head"), "version": data.get("version"),
        }
        if not all(isinstance(cursor[k], int) for k in ("anchor", "head", "version")):
            return
        peer.presence = cursor
        await self._broadcast(room, {"type": "presence", **_peer_info(peer), **cursor}, skip=peer)

    async def _broadcast(self, room: Room, message: dict, skip: Optional[Peer] = None) -> None:
        for p in list(room.peers.values()):
            if p is not skip:
                await _safe_send(p, message)

    # ------------------------------------------------------------ guardar

    def _schedule_persist(self, room: Room) -> None:
        if room.persist_task is None or room.persist_task.done():
            room.persist_task = asyncio.create_task(self._persist_later(room))

    async def _persist_later(self, room: Room) -> None:
        await asyncio.sleep(PERSIST_DELAY)
        await self.flush(room)

    async def flush(self, room: Room) -> None:
        """Guarda el texto de la sala si tiene cambios sin guardar."""
        async with room.lock:
            if not room.dirty:
                return
            body, expected = room.doc, room.persisted_body
            room.dirty = False
            editores, room.editors = room.editors, set()
        t0 = time.perf_counter()
        try:
            stored = await asyncio.to_thread(self._store, room.page_id, body, expected)
        except ExternalChange as exc:
            await self._reset(room, exc.body)
            return
        except LookupError:
            await self._broadcast(room, {"type": "gone"})
            return
        except Exception:  # noqa: BLE001 - un guardado fallido no tumba la sala
            log.exception("sala %s: no se pudo guardar", room.page_id)
            async with room.lock:
                room.dirty = True
                room.editors |= editores
            self._schedule_persist(room)
            return
        room.persisted_body = stored
        room.last_persist_ms = (time.perf_counter() - t0) * 1000
        if editores and self._record_activity is not None:
            try:
                await asyncio.to_thread(self._record_activity, room.page_id, editores)
            except Exception:  # noqa: BLE001 - la actividad es un extra
                log.warning("sala %s: no se pudo apuntar la actividad", room.page_id)
        self.persist_ms.append(room.last_persist_ms)

    async def _reset(self, room: Room, body: str) -> None:
        """El cuerpo cambió por otro camino: la sala empieza de nuevo con él."""
        async with room.lock:
            room.doc = body
            room.persisted_body = body
            room.epoch = uuid.uuid4().hex
            room.version = 0
            room.log.clear()
            room.log_start = 0
            room.dirty = False
            msg = {"type": "reject", "reason": "el documento cambió fuera de la sala",
                   "epoch": room.epoch, "version": 0, "doc": body}
        await self._broadcast(room, msg)

    async def flush_all(self) -> None:
        """Guarda todas las salas abiertas (al apagar el servidor)."""
        for room in list(self._rooms.values()):
            await self.flush(room)

    async def reload_from_store(self, page_id: str) -> None:
        """Aviso de fuera (un `PATCH` sin sala): si hay sala, se reinicia."""
        room = self._rooms.get(page_id)
        if room is None:
            return
        body = await asyncio.to_thread(self._load, page_id)
        if body is not None and body != room.persisted_body:
            await self._reset(room, body)


class ExternalChange(Exception):
    """Al guardar, el cuerpo de la base no era el que la sala guardó la última vez."""

    def __init__(self, body: str):
        super().__init__("el cuerpo cambió fuera de la sala")
        self.body = body


def _cursor_color(room: Room, peer: Peer) -> str:
    """El color del cursor de `peer` en esta sala: el mismo que ya tenga su
    usuario en otra pestaña dentro de la nota, o uno libre al azar."""
    for otro in room.peers.values():
        if otro.user_id == peer.user_id and otro.color:
            return otro.color
    usados = {p.color for p in room.peers.values()}
    libres = [c for c in CURSOR_COLORS if c not in usados]
    return random.choice(libres or list(CURSOR_COLORS))


def _peer_info(peer: Peer) -> dict:
    return {"peer": peer.peer_id, "user": user_info(peer)}


def user_info(peer: Peer) -> dict:
    return {"id": peer.user_id, "name": peer.name, "color": peer.color, "avatar": peer.avatar}


async def _safe_send(peer: Peer, message: dict) -> None:
    try:
        await peer.send(message)
    except Exception:  # noqa: BLE001 - un cliente caído no para a los demás
        pass
