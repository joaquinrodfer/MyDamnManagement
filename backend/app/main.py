import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.index import ensure_index, open_index
from app.repository import Repository
from app.routers import dashboard, databases, pages, search, status
from app.vault.store import VaultError

log = logging.getLogger("mdm.main")

app = FastAPI(title="MyDamnManagement API")

app.include_router(pages.router)
app.include_router(databases.router)
app.include_router(status.router)
app.include_router(search.router)
app.include_router(dashboard.router)


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
            # Con notas en el índice no se crea nada: si el vault no está
            # montado, `rebuild()` se niega a vaciarlas y el arranque falla en
            # claro en vez de servir datos sin fichero detrás.
            repo.vault.ensure_structure()
        try:
            stats = repo.reindex()
        except VaultError as exc:
            raise RuntimeError(
                f"no se puede arrancar: {exc} "
                "(el índice no se puede reconstruir desde el vault)"
            ) from exc
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


class AttachmentStaticFiles(NoCacheStaticFiles):
    """StaticFiles de /files: no-cache (como todo lo demás) más los headers
    que impiden que un fichero colado en attachments/ se ejecute en el mismo
    origen que la app.

    `nosniff` impide que el navegador reinterprete el Content-Type declarado,
    y `Content-Disposition` va en `attachment` salvo para las imágenes raster
    conocidas (esas sí `inline`, porque las pinta el <img> de la cabecera):
    así un .svg o un .html que hubiera quedado de una subida antigua se
    descarga en vez de renderizarse. Sin CSP: frontend/index.html usa estilos
    y scripts inline y una CSP rompería la app entera.
    """

    INLINE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # args[0] es la ruta del fichero en las versiones de Starlette que
        # soporta FastAPI: file_response(path, stat_result, scope, ...).
        filename = Path(str(args[0])).name if args else "attachment"
        safe_name = filename.replace('"', "").replace("\\", "_")
        safe_name = safe_name.replace("\r", "").replace("\n", "") or "attachment"
        inline = Path(safe_name).suffix.lower() in self.INLINE_SUFFIXES
        response.headers["Content-Disposition"] = (
            f'{"inline" if inline else "attachment"}; filename="{safe_name}"'
        )
        return response


# El volumen de adjuntos puede llegar vacío en un contenedor limpio y
# StaticFiles no arranca sobre un directorio inexistente. El del panel NO se
# crea a propósito: si falta, la imagen está mal construida y debe fallar en
# claro en vez de servir una carpeta vacía.
Path(settings.attachments_dir).mkdir(parents=True, exist_ok=True)

# Adjuntos (imágenes de cabecera, etc.) subidos por la propia app -- ver
# POST /pages/{id}/header-image. Antes del mount de "/" por la misma razón
# de siempre: el catch-all no debe taparlo. AttachmentStaticFiles aquí también:
# el nombre de archivo es fijo por página ("header.<ext>"), así que sin esto
# quitar una cabecera y subir otra de inmediato podía seguir mostrando la
# vieja (misma URL, navegador sirviendo de caché sin revalidar).
app.mount(
    "/files",
    AttachmentStaticFiles(directory=settings.attachments_dir),
    name="files",
)

# Montado al final a propósito: así no tapa /health, /status, /pages, /files
# ni /docs, y sigue sirviendo cualquier otra ruta (incluida "/") como
# archivos estáticos.
app.mount(
    "/",
    NoCacheStaticFiles(directory=settings.frontend_dir, html=True),
    name="frontend",
)
