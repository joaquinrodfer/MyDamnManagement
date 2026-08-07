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


# Montado al final a propósito: así no tapa /health, /status, /pages ni /docs,
# y sigue sirviendo cualquier otra ruta (incluida "/") como archivos estáticos.
app.mount("/", StaticFiles(directory="/app/frontend", html=True), name="frontend")
