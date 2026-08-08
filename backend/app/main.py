from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Workspace
from app.routers import databases, pages, search, status

app = FastAPI(title="MyDamnManagement API")

app.include_router(pages.router)
app.include_router(databases.router)
app.include_router(status.router)
app.include_router(search.router)


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


# Adjuntos (imágenes de cabecera, etc.) subidos por la propia app -- ver
# POST /pages/{id}/header-image. Antes del mount de "/" por la misma razón
# de siempre: el catch-all no debe taparlo. NoCacheStaticFiles aquí también:
# el nombre de archivo es fijo por página ("header.<ext>"), así que sin esto
# quitar una cabecera y subir otra de inmediato podía seguir mostrando la
# vieja (misma URL, navegador sirviendo de caché sin revalidar).
app.mount("/files", NoCacheStaticFiles(directory="/app/attachments"), name="files")

# Montado al final a propósito: así no tapa /health, /status, /pages, /files
# ni /docs, y sigue sirviendo cualquier otra ruta (incluida "/") como
# archivos estáticos.
app.mount("/", NoCacheStaticFiles(directory="/app/frontend", html=True), name="frontend")
