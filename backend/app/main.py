from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Workspace
from app.routers import dashboard, databases, pages, search, status

app = FastAPI(title="MyDamnManagement API")

app.include_router(pages.router)
app.include_router(databases.router)
app.include_router(status.router)
app.include_router(search.router)
app.include_router(dashboard.router)


@app.on_event("startup")
def on_startup() -> None:
    # Fase 1: el esquema ya lo crea/migra Alembic (alembic upgrade head,
    # ver docker-compose.yml) antes de que arranque uvicorn. Aquí solo
    # queda la siembra de datos de aplicación (workspace por defecto).
    _ensure_default_workspace()


def _ensure_default_workspace() -> None:
    db: Session = SessionLocal()
    try:
        if not db.query(Workspace).first():
            db.add(Workspace(name="default"))
            db.commit()
    finally:
        db.close()


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


# Adjuntos (imágenes de cabecera, etc.) subidos por la propia app -- ver
# POST /pages/{id}/header-image. Antes del mount de "/" por la misma razón
# de siempre: el catch-all no debe taparlo. AttachmentStaticFiles aquí también:
# el nombre de archivo es fijo por página ("header.<ext>"), así que sin esto
# quitar una cabecera y subir otra de inmediato podía seguir mostrando la
# vieja (misma URL, navegador sirviendo de caché sin revalidar).
app.mount("/files", AttachmentStaticFiles(directory="/app/attachments"), name="files")

# Montado al final a propósito: así no tapa /health, /status, /pages, /files
# ni /docs, y sigue sirviendo cualquier otra ruta (incluida "/") como
# archivos estáticos.
app.mount("/", NoCacheStaticFiles(directory="/app/frontend", html=True), name="frontend")
