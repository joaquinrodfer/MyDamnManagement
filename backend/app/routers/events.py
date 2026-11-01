"""`GET /events`: avisos al navegador por Server-Sent Events (P8).

SSE y no WebSocket porque el flujo es de uno a muchos y de un solo sentido: el
servidor cuenta lo que ha cambiado en el vault y el navegador solo escucha. Con
SSE eso es `StreamingResponse` y un `EventSource` en el cliente, sin
dependencias nuevas ni protocolo de handshake; el navegador además reconecta
solo si se cae la conexión.

El formato es el de la especificación:

    event: page
    data: {"type":"page","action":"modified","page_id":"...","path":"Notas/X.md"}

más un comentario `: ping` cada `PING_INTERVAL` segundos. Los comentarios no
llegan a `onmessage` (el cliente los ignora por especificación) y sirven para
que ni un proxy ni el navegador cierren una conexión que lleva un rato callada.

Cada cliente tiene su cola (`app.events.bus`); al desconectar, el generador se
cancela, el `finally` suelta la cola y no queda nada colgado.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.events import bus

router = APIRouter(tags=["events"])

# Cada cuánto se manda un `: ping`. Los proxies suelen cortar a los 30-60 s de
# silencio; con 15 s hay margen de sobra y no es ruido perceptible.
PING_INTERVAL = 15.0


@router.get("/events")
async def stream_events() -> StreamingResponse:
    """Flujo de cambios del vault. Un evento `page` por fichero reindexado."""

    async def generate():
        queue = bus.subscribe()
        try:
            # Un comentario de entrada: abre el flujo y confirma al cliente que
            # está suscrito antes del primer cambio (EventSource considera la
            # conexión abierta en cuanto llega cualquier byte).
            yield ": conectado\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                except asyncio.CancelledError:
                    # El cliente se ha ido (o la app se para): sale por el
                    # `finally`, que es donde se suelta la cola.
                    raise
                yield f"event: page\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            # Sin caché y sin buffering en proxies (nginx): un evento que se
            # queda en un buffer no es un aviso, es una mentira.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
