from fastapi import FastAPI
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models import Workspace
from app.routers import pages

app = FastAPI(title="MyDamnManagement API")

app.include_router(pages.router)


@app.on_event("startup")
def on_startup() -> None:
    # Fase 0: create_all en vez de Alembic para iterar rápido sobre el
    # schema. Migramos a Alembic en cuanto el modelo de datos se estabilice
    # (Fase 1), porque en ese punto ya habrá datos reales que preservar.
    Base.metadata.create_all(bind=engine)
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
