"""Canal de avisos al navegador (P8): un bus de suscriptores en memoria.

El vigilante del vault corre en su propio hilo (el recorrido es síncrono) y el
endpoint SSE vive en el bucle de eventos, así que el aviso tiene que cruzar de
un hilo al otro sin que ninguno espere al otro:

- Cada cliente de `/events` tiene **su** `asyncio.Queue`, creada y consumida en
  el bucle (una sola tarea, sin candados).
- `publish()` se puede llamar desde cualquier hilo: no toca las colas
  directamente, solo pide al bucle que entregue el evento
  (`loop.call_soon_threadsafe`). Con un solo proceso y una sola pestaña esto
  basta; no hay fan-out entre réplicas porque no hay réplicas.
- Un cliente que deja de leer no bloquea al vigilante ni se lleva la conexión:
  su cola tiene tope y, si se llena, se descarta el aviso más viejo (para una
  pantalla que se ha quedado atrás, el último estado es el que importa).
- El conjunto de colas lo tocan dos hilos (el bucle da de alta y de baja, el
  vigilante reparte), así que alta, baja y snapshot van bajo un `threading.Lock`.
  Sin él, `list(self._queues)` en `publish` puede caer en mitad de una
  mutación con `RuntimeError: Set changed size during iteration`: los clientes
  restantes se quedarían sin el aviso y la pasada entera se anotaría como
  fallida aunque el reindexado fuera bien.

El bus no guarda estado ni historial: un cliente que conecta recibe los avisos
a partir de ese momento y carga el estado por la API, que es la que manda.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

log = logging.getLogger("mdm.events")

# Avisos que se le guardan a un cliente que no está leyendo. Con 2 s entre
# pasadas del vigilante, 256 es margen de sobra para una pestaña en segundo
# plano; llenarlo significa que nadie va a leerlos.
QUEUE_MAXSIZE = 256


class EventBus:
    """Suscriptores de `/events` y entrega de eventos entre hilos."""

    def __init__(self, maxsize: int = QUEUE_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._queues: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Alta/baja en el bucle, snapshot en el hilo del vigilante.
        self._lock = threading.Lock()

    # ------------------------------------------------------------ suscripción

    @property
    def subscribers(self) -> int:
        """Clientes conectados (para comprobar que una desconexión no deja cola)."""
        with self._lock:
            return len(self._queues)

    def subscribe(self) -> asyncio.Queue:
        """Cola nueva para un cliente. Se llama desde el bucle de eventos."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        with self._lock:
            self._loop = loop
            self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Suelta la cola del cliente que se ha ido. Idempotente."""
        with self._lock:
            self._queues.discard(queue)

    # --------------------------------------------------------------- entrega

    def publish(self, event: dict[str, Any]) -> None:
        """Entrega un evento a todos los clientes. Seguro desde cualquier hilo."""
        with self._lock:
            loop = self._loop
            queues = tuple(self._queues)
        if loop is None or loop.is_closed():
            # Nadie ha abierto `/events` todavía, o el bucle ya se ha cerrado:
            # no hay a quién avisar y no es un error.
            return
        for queue in queues:
            try:
                loop.call_soon_threadsafe(self._deliver, queue, event)
            except RuntimeError:
                # El bucle se cerró entre la comprobación y esto (parada de la
                # app): el evento se pierde, que es lo correcto.
                return

    def _deliver(self, queue: asyncio.Queue, event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Cliente rezagado: se tira el aviso más viejo y se guarda el nuevo.
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - carrera imposible
                pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover
                log.debug("cola de /events llena; evento descartado")


# Uno solo para todo el proceso: el vigilante publica aquí y `/events` lee de
# aquí. Se instancia al importar, sin tocar la red ni el disco.
bus = EventBus()
