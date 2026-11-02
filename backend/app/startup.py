"""El índice de arranque, construido en segundo plano (P16).

Antes de P16, `app.main.on_startup` reconstruía el índice entero **antes** de
que uvicorn empezara a escuchar: con 12.000 notas eso son minutos de socket
cerrado. El contenedor parecía caído (el `healthcheck` de Docker no podía
siquiera conectar), `docker compose up` en un vault restaurado parecía colgado
y el reinicio en bucle era cuestión de tiempo.

Ahora el arranque se parte en dos:

- **Fase rápida y bloqueante** (en `main.on_startup`): esquema del índice,
  workspace, estructura del vault cuando está vacío, el administrador de
  `ADMIN_USER`/`ADMIN_PASSWORD` y las comprobaciones que deben abortar el
  arranque (un vault que no está donde dice, un índice ilegible). Es lo mismo
  que hacía antes, menos el reindexado, y cuesta milisegundos.
- **Fase lenta y en segundo plano** (este módulo): `Repository.reindex`. El
  servidor ya acepta peticiones mientras tanto.

Mientras la fase lenta no ha terminado, la app **no finge** que tiene índice:

- `/health` responde `200 {"status": "starting", "progress": …}` y solo dice
  `ok` cuando el índice está reconstruido. Es la regla que P7 dejó escrita: un
  `ok` con el índice a medio construir es exactamente el fallo que se arregló.
- El resto de rutas responden `503` con el progreso (ver el middleware de
  `main`): esperar dentro de la petición sujetaría un hilo del pool hasta
  minutos, y devolver lo que hay sería servir una lista a medias.
- Si el reindexado revienta, o lleva más de `STARTUP_STALL_SECONDS` sin
  avanzar, el estado pasa a `error` y `/health` responde `503` con el motivo:
  el `healthcheck` del compose falla y `docker inspect` enseña por qué. Nunca
  se reinicia en silencio ni se queda `healthy` con el índice vacío.

El estado vive en un objeto (no en variables sueltas) para que se pueda mirar
y reiniciar desde los tests sin tocar `app.state` ni el objeto `app`. Cada
arranque estrena **generación**: si un hilo viejo sigue vivo (en los tests la
app arranca una vez por `TestClient`, en el mismo proceso), sus avisos ya no
cuentan para el arranque nuevo.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("mdm.startup")

STARTING = "starting"
OK = "ok"
ERROR = "error"

ProgressCallback = Callable[[str, int, int], None]


class _Handle:
    """El mando que se le da al trabajo del arranque: informar, no decidir.

    Va atado a la generación con la que se lanzó: un hilo que sobrevive a su
    arranque (proceso que se reinicia en los tests, `reset()` en caliente) no
    puede cambiar el estado del arranque siguiente.
    """

    def __init__(self, dueno: "StartupIndex", generacion: int) -> None:
        self._dueno = dueno
        self._generacion = generacion

    def progress(self, phase: str, done: int = 0, total: int = 0) -> None:
        self._dueno._progress(self._generacion, phase, done, total)

    def succeed(self, detail: str = "") -> None:
        self._dueno._succeed(self._generacion, detail)

    def fail(self, mensaje: str) -> None:
        self._dueno._fail(self._generacion, mensaje)


class StartupIndex:
    """Estado del índice de arranque: `starting` -> `ok` | `error`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._generacion = 0
        self._status = STARTING
        self._phase = "esperando a que arranque el índice"
        self._done = 0
        self._total = 0
        self._error: Optional[str] = None
        self._started_at: Optional[float] = None
        self._finished_at: Optional[float] = None
        self._last_progress_at: Optional[float] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------- arranque

    def reset(self) -> None:
        """Vuelve a `starting` y desautoriza al hilo anterior, si lo hay.

        Lo llama `on_startup` en cada arranque: el objeto es de módulo, y un
        `TestClient` nuevo (en los tests la app arranca muchas veces en el
        mismo proceso) tiene que empezar el índice de cero, no heredar el `ok`
        del arranque anterior. La generación es lo que hace que el hilo viejo,
        si todavía estaba trabajando, no pueda escribir en este.
        """
        with self._lock:
            self._generacion += 1
            self._ready.clear()
            self._status = STARTING
            self._phase = "esperando a que arranque el índice"
            self._done = 0
            self._total = 0
            self._error = None
            self._started_at = None
            self._finished_at = None
            self._last_progress_at = None
            self._thread = None

    def start(self, trabajo: Callable[[_Handle], None]) -> threading.Thread:
        """Lanza `trabajo` en un hilo daemon y devuelve el hilo.

        Daemon a propósito: si el proceso se apaga a mitad del indexado no hay
        nada que esperar (el índice es derivado y se reconstruye en el
        siguiente arranque), y un `uvicorn` que no termina de apagarse es peor
        que uno que corta el reindexado.
        """
        with self._lock:
            self._generacion += 1
            generacion = self._generacion
            self._ready.clear()
            self._status = STARTING
            self._started_at = time.monotonic()
            self._last_progress_at = self._started_at
            hilo = threading.Thread(
                target=self._run,
                args=(trabajo, _Handle(self, generacion)),
                name="mdm-indice-arranque",
                daemon=True,
            )
            self._thread = hilo
        hilo.start()
        return hilo

    def _run(self, trabajo: Callable[[_Handle], None], mando: _Handle) -> None:
        try:
            trabajo(mando)
        except BaseException as exc:  # noqa: BLE001 - el hilo no puede morir callado
            # `BaseException` y no `Exception`: un `KeyboardInterrupt` o un
            # `SystemExit` dentro del hilo dejarían la app en `starting` para
            # siempre, y `/health` mentiría diciendo que está arrancando.
            detalle = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            mando.fail(f"el índice no se pudo construir: {detalle}")

    # -------------------------------------------------------------- progreso

    def _progress(self, generacion: int, phase: str, done: int, total: int) -> None:
        with self._lock:
            if generacion != self._generacion or self._status != STARTING:
                return
            self._phase = phase
            self._done = max(0, int(done))
            self._total = max(0, int(total))
            self._last_progress_at = time.monotonic()

    def _succeed(self, generacion: int, detail: str = "") -> None:
        with self._lock:
            if generacion != self._generacion:
                log.warning(
                    "se ignora el final de un indexado viejo (generación %s, la "
                    "actual es %s)",
                    generacion,
                    self._generacion,
                )
                return
            self._status = OK
            self._phase = "índice al día"
            self._finished_at = time.monotonic()
            self._last_progress_at = self._finished_at
            self._error = None
        self._ready.set()
        if detail:
            log.info("índice de arranque listo: %s", detail)

    def _fail(self, generacion: int, mensaje: str) -> None:
        with self._lock:
            if generacion != self._generacion:
                log.warning(
                    "se ignora el fallo de un indexado viejo (generación %s, la "
                    "actual es %s): %s",
                    generacion,
                    self._generacion,
                    mensaje,
                )
                return
            if self._status == OK:
                # Un fallo después de estar listo no puede convertir en «roto»
                # un índice que ya sirve: se apunta y se sigue.
                log.error("fallo tardío del índice de arranque (ignorado): %s", mensaje)
                return
            self._status = ERROR
            self._error = mensaje
            self._phase = "índice fallido"
            self._finished_at = time.monotonic()
        self._ready.set()  # despierta a quien espere: ya hay respuesta (un 503)
        log.error("índice de arranque fallido: %s", mensaje)

    # ---------------------------------------------------------------- estado

    def snapshot(self, stall_seconds: Optional[float] = None) -> dict:
        """Foto del estado para `/health` y para el middleware del 503.

        `stall_seconds` convierte un indexado parado en un fallo: si el hilo
        lleva más de eso sin avisar de progreso, la respuesta es la de `error`.
        Sin él (o con `None`) no se juzga el tiempo.
        """
        with self._lock:
            ahora = time.monotonic()
            estado = self._status
            error = self._error
            last = self._last_progress_at
            started = self._started_at
            stalled = False
            if (
                estado == STARTING
                and stall_seconds is not None
                and last is not None
                and (ahora - last) > stall_seconds
            ):
                estado = ERROR
                stalled = True
                error = (
                    "el indexado lleva "
                    f"{int(ahora - last)} s sin avanzar (más de "
                    f"{int(stall_seconds)} s): algo lo tiene atascado "
                    "(¿disco lleno, I/O bloqueado, un fichero en un montaje "
                    "que no responde?). El índice puede estar a medias."
                )
            transcurrido = (self._finished_at or ahora) - started if started else 0.0
            percent = (
                int(round(100.0 * self._done / self._total)) if self._total else None
            )
            if estado == STARTING and percent is not None:
                # Mientras el índice no está listo, `100 %` sería una mentira
                # por definición: aún queda cerrar la transacción y marcar el
                # índice como bueno. El 100 es de `ok`; un `starting` se queda
                # como mucho en 99, así que un `100 %` en `/health` siempre
                # significa lo mismo que `status: "ok"`.
                percent = min(percent, 99)
            return {
                "status": estado,
                "phase": self._phase,
                "done": self._done,
                "total": self._total,
                "percent": percent,
                "elapsed_seconds": round(transcurrido, 1),
                "stalled": stalled,
                "error": error,
            }

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def is_ready(self) -> bool:
        return self.status == OK

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Espera a que el índice esté listo (o falle). Devuelve si está listo.

        Lo usan los tests y cualquier script que necesite el índice completo;
        ninguna ruta HTTP espera aquí (ver el middleware de `main`).
        """
        return self._ready.wait(timeout) and self.is_ready()


# Instancia única del proceso. `on_startup` la reinicia en cada arranque.
startup_index = StartupIndex()
