import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.index import ensure_index, open_index
from app.repository import Repository
from app.routers import (
    agent,
    dashboard,
    databases,
    events,
    files,
    graph,
    pages,
    search,
    status,
    suggestions,
    trash,
)
from app.vault.store import EMPTY_VAULT_HINT_STARTUP, VaultError
from app.vault.watcher import VaultWatcher

log = logging.getLogger("mdm.main")


app = FastAPI(
    title="MyDamnManagement API",
    # `DOCS_ENABLED=0` apaga la documentación interactiva: `/docs`, `/redoc`
    # y `/openapi.json` dan 404 (ver `Settings.docs_enabled`). Con el ajuste
    # por defecto (encendido) el comportamiento es el de siempre.
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)

app.include_router(pages.router)
app.include_router(databases.router)
app.include_router(status.router)
app.include_router(trash.router)
app.include_router(search.router)
app.include_router(dashboard.router)
# `/files` es una ruta y no un `StaticFiles` (P1.2b): los adjuntos viven dentro
# del vault (`_attachments/`) y la URL es su ruta ahí, así que hay que resolver
# el tipo por magic bytes y decidir qué se sirve inline en cada petición.
app.include_router(files.router)
# `/events` (SSE, P8): avisos de lo que cambia en el vault. Es una ruta y no
# parte de `/pages` porque no habla de páginas sino del vault entero.
app.include_router(events.router)
# `/graph` (P9): la tabla `links` servida como nodos y aristas para el visor.
app.include_router(graph.router)
# `/pages/{id}/suggestions` y `/suggestions/{sid}/...` (P10a): los parches que
# proponen los agentes, en el sidecar de la nota. Solo backend: la interfaz va
# en su propia tarjeta.
app.include_router(suggestions.router)
# `/agent/...` (P10b): la puerta de los agentes — leer contexto, proponer y ver
# lo decidido, con `X-Agent-Token`. Sin `AGENT_TOKENS` las tres rutas dan 404
# (no existen), que es lo que hace un despliegue sin agentes configurados.
app.include_router(agent.router)

# Vigilante del vault (P8). Vive en el módulo (y no en `app.state`) para que
# los tests y el apagado puedan pararlo sin depender del objeto `app`.
watcher: Optional[VaultWatcher] = None


def _index_has_no_notes(conn) -> bool:
    return conn.execute(
        "SELECT count(*) FROM pages WHERE type = 'note'"
    ).fetchone()[0] == 0


@app.on_event("startup")
def on_startup() -> None:
    # No hay Alembic ni Postgres: el esquema del índice se crea o se migra
    # aquí mismo (`PRAGMA user_version`), y un fichero ilegible se aparta y se
    # rehace en vez de impedir el arranque.
    #
    # Desde P1.2a el índice es derivado y se reconstruye desde el vault en
    # CADA arranque (ADR-0001 D2, ROADMAP §1 D2), no solo cuando se ha tenido
    # que recrear. No es un lujo: si el proceso murió entre escribir el `.md` y
    # actualizar el índice, el índice sirve el cuerpo viejo y el autoguardado
    # del panel lo escribe encima del fichero, que era el único que tenía el
    # último guardado. Coste medido en el spike P0: 0,32 s con 5.033 notas.
    info = ensure_index(settings.index_path)
    if info.recreated:
        log.warning("índice creado de cero en %s (%s)", info.path, info.reason)

    conn = open_index(settings.index_path)
    try:
        repo = Repository(conn)
        repo.ensure_workspace()
        if _index_has_no_notes(conn):
            # Instalación nueva (el índice no tiene notas): se crea la
            # estructura del vault, que es lo que espera el primer POST /pages.
            # Con notas en el índice no se crea nada: si el vault no está,
            # `rebuild()` se niega a vaciarlas y el arranque falla en claro en
            # vez de servir datos sin fichero detrás.
            repo.vault.ensure_structure()
        elif settings.vault_allow_empty and not Path(settings.vault_path).is_dir():
            # `VAULT_ALLOW_EMPTY` vale para un vault que existe y está vacío
            # (vaciado a mano, restore parcial del volumen). Una raíz que no
            # existe es un volumen sin montar y nunca se lee como vault vacío.
            raise RuntimeError(
                f"no se puede arrancar: la raíz del vault {settings.vault_path!r} "
                "no existe. VAULT_ALLOW_EMPTY=1 no cubre ese caso (un volumen sin "
                "montar no es un vault vacío): monta el directorio y vuelve a "
                "arrancar."
            )
        try:
            stats = repo.reindex(
                force=settings.vault_allow_empty,
                empty_hint=EMPTY_VAULT_HINT_STARTUP,
            )
        except VaultError as exc:
            raise RuntimeError(
                f"no se puede arrancar: {exc} "
                "(el índice no se puede reconstruir desde el vault)"
            ) from exc
        if settings.vault_allow_empty:
            log.warning(
                "VAULT_ALLOW_EMPTY=1: se acepta un vault vacío a propósito; "
                "el índice se queda con %d notas (las bases, filas y vistas se "
                "conservan, sin padre)",
                stats.notes,
            )
        log.info(
            "índice reconstruido desde %s: %d notas, %d enlaces en %.2f s (%d avisos)",
            settings.vault_path,
            stats.notes,
            stats.links,
            stats.seconds,
            len(stats.warnings),
        )
    finally:
        conn.close()

    _start_watcher()


def _start_watcher() -> None:
    """Arranca el vigilante del vault si `WATCH_ENABLED` lo permite.

    Se arranca **después** del reindexado de arranque: el vigilante sondea
    contra un índice ya al día, así que su primera pasada no tiene nada que
    reindexar y no llega ningún aviso por cambios que nadie ha hecho.
    """
    global watcher
    if not settings.watch_enabled:
        log.info(
            "WATCH_ENABLED=0: no se vigila el vault; los cambios hechos en los "
            "`.md` por fuera se recogen al arrancar y con POST /reindex"
        )
        return
    watcher = VaultWatcher(
        vault_path=settings.vault_path,
        index_path=settings.index_path,
        interval=settings.watch_interval,
    )
    watcher.start()
    log.info(
        "vigilando %s cada %s s (WATCH_INTERVAL); los avisos salen por /events",
        settings.vault_path,
        watcher.interval,
    )


@app.on_event("shutdown")
def on_shutdown() -> None:
    """Para el vigilante. Sin esto, el hilo seguiría sondeando tras cerrar."""
    global watcher
    if watcher is not None:
        watcher.stop()
        watcher = None
        log.info("vigilante del vault detenido")


@app.get("/health")
def health():
    return {"status": "ok"}


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles normal, pero fuerza al navegador a revalidar (If-None-Match)
    en vez de servir de caché sin preguntar. Sin esto, un cambio en app.js o
    index.html puede quedarse "pegado" en el navegador de forma invisible —
    el ETag sigue haciendo baratas las recargas que no cambiaron nada."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Montado al final a propósito: así no tapa /health, /status, /pages, /trash,
# /files ni /docs, y sigue sirviendo cualquier otra ruta (incluida "/") como
# archivos estáticos.
app.mount(
    "/",
    NoCacheStaticFiles(directory=settings.frontend_dir, html=True),
    name="frontend",
)
