"""Vigilante del vault (P8): los `.md` que cambian por fuera, reindexados solos.

Hoy el vault es la verdad (ADR-0001), pero la app solo lo leía al arrancar y
con `POST /reindex`. Esto cierra ese hueco: un hilo que cada `WATCH_INTERVAL`
segundos compara la huella de cada `.md` (`mtime` + tamaño, sin leer cuerpos)
con la que guarda el índice y reindexa **solo** los ficheros que han cambiado,
avisando al navegador por el bus de `/events`.

Decisiones, y por qué:

- **Sondeo, no inotify.** El recorrido ya existía para `/status`
  (`app.vault.drift`): un `scandir` con un `stat` por fichero. Medido con 2.000
  ficheros, una pasada cuesta ~9 ms en este contenedor (WSL sobre 9p, que es el
  peor caso: rehace el `stat`); con el intervalo por defecto de 2 s es ~0,5 % de
  un núcleo y no lee ni un byte de contenido. `watchdog` habría añadido una
  dependencia y una API de eventos que, en un volumen de red (donde el vault
  suele vivir en producción), ni siquiera es fiable. Con 2.000 ficheros no hay
  nada que justifique el cambio.
- **Un hilo, no el bucle de eventos.** El recorrido y `sqlite3` son síncronos:
  en el hilo del vigilante no bloquean ni una petición. La conexión al índice
  es propia (una por pasada, `check_same_thread=False`), así que no compite con
  las de las peticiones por el `BEGIN IMMEDIATE`: si el índice está ocupado,
  espera (`busy_timeout` de 5 s) y sigue.
- **Una pasada no puede tumbar la app.** Cada fichero se indexa en su propia
  transacción y con su propio `try`: un `.md` ilegible, un enlace roto o un
  fichero que desaparece a mitad se registran y la pasada sigue. Un error del
  recorrido entero (el vault desaparece, el índice se bloquea) también se
  registra y se reintenta en la pasada siguiente.
- **Idempotente.** Si el proceso muere a mitad, la pasada siguiente ve lo que
  falte: el índice se corrige solo. Y un fichero cuyo contenido no ha cambiado
  (un `touch`, o el `mtime` que movió el propio guardado de la app) no genera
  aviso.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from app import index as index_mod
from app.events import bus
from app.vault import drift
from app.vault.store import VaultStore

log = logging.getLogger("mdm.watch")

THREAD_NAME = "mdm-vault-watcher"

# Suelo del intervalo: por debajo de esto el sondeo deja de ser barato y el
# hilo se pasa el día recorriendo el vault. `WATCH_INTERVAL=0` no lo apaga
# (`WATCH_ENABLED=0` sí): lo deja en el mínimo.
MIN_INTERVAL = 0.05


class VaultWatcher:
    """Hilo que sondea el vault, reindexa lo cambiado y publica los avisos."""

    def __init__(
        self,
        vault_path: str,
        index_path: str,
        interval: float = 2.0,
        publish: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.vault_path = vault_path
        self.index_path = index_path
        try:
            self.interval = max(float(interval), MIN_INTERVAL)
        except (TypeError, ValueError):
            self.interval = 2.0
        self._publish = publish or bus.publish
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Último error por fichero: un `.md` que no se puede leer no debe
        # escribir una línea de log cada dos segundos para siempre.
        self._errors: dict[str, str] = {}

    # --------------------------------------------------------------- ciclo

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=THREAD_NAME, daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Para el hilo y espera a que salga (como mucho `timeout` segundos)."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - el bucle nunca se cae
                log.exception(
                    "la pasada del vigilante falló; se reintenta en %s s", self.interval
                )

    # ------------------------------------------------------------- pasadas

    def run_once(self) -> list[dict]:
        """Una pasada: compara, reindexa lo cambiado y publica los avisos.

        Devuelve los eventos emitidos (los tests lo usan para no depender del
        hilo). Lanza si falla el recorrido o el índice: el bucle lo captura.
        """
        conn = index_mod.open_index(self.index_path)
        try:
            found = drift.diff(self.vault_path, conn)
            if found.changed == 0:
                return []
            store = VaultStore(self.vault_path, conn)
            events = store.sync_changes(
                added=[change.path for change in found.added],
                modified=[change.path for change in found.modified],
                missing=found.missing,
                on_error=self._note_error,
            )
        finally:
            conn.close()
        for event in events:
            self._publish(event)
        if events:
            log.info(
                "vigilante: %d fichero(s) reindexado(s) (%s)",
                len(events),
                ", ".join(sorted({event["action"] for event in events})),
            )
        return events

    def _note_error(self, rel: str, exc: BaseException) -> None:
        """Un fichero que no se pudo indexar: se registra una vez y se sigue."""
        detail = f"{type(exc).__name__}: {exc}"
        if self._errors.get(rel) == detail:
            return
        self._errors[rel] = detail
        log.warning(
            "no se pudo reindexar %s (%s); se reintenta en la próxima pasada",
            rel,
            detail,
        )
