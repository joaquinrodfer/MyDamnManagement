import uuid
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from app import schemas
from app.config import settings
from app.dependencies import get_repo
from app.properties import validate_properties
from app.repository import Database, Page, Repository, View
from app.serializers import as_utc
from app.templates import TEMPLATES

router = APIRouter(prefix="/databases", tags=["databases"])


def _get_database_or_404(repo: Repository, database_id: uuid.UUID) -> Database:
    db_def = repo.get_database(database_id)
    if db_def is None:
        raise HTTPException(404, "Base de datos no encontrada")
    return db_def


def _get_row_or_404(repo: Repository, database_id: uuid.UUID, row_id: uuid.UUID) -> Page:
    row = repo.get_row(database_id, row_id)
    if row is None:
        raise HTTPException(404, "Fila no encontrada")
    return row


def _serialize_database(db_def: Database) -> schemas.DatabaseRead:
    return schemas.DatabaseRead(
        id=db_def.id,
        page_id=db_def.page_id,
        title=db_def.title,
        icon=db_def.icon,
        schema_def=db_def.schema_def,
        created_at=as_utc(db_def.created_at),
        updated_at=as_utc(db_def.updated_at),
    )


def _serialize_row(row: Page) -> schemas.RowRead:
    return schemas.RowRead(
        id=row.id,
        database_id=row.database_id,
        title=row.title,
        icon=row.icon,
        properties=row.properties or {},
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


def _serialize_view(view: View) -> schemas.ViewRead:
    return schemas.ViewRead(
        id=view.id,
        database_id=view.database_id,
        name=view.name,
        type=view.type,
        config=view.config or {},
    )


# ---------------------------------------------------------------- databases

@router.post("", response_model=schemas.DatabaseRead)
def create_database(payload: schemas.DatabaseCreate, repo: Repository = Depends(get_repo)):
    db_def = repo.create_database(
        title=payload.title,
        parent_id=payload.parent_id,
        icon=payload.icon or settings.default_icon,
        created_by=settings.default_user_name,
        schema_def=[p.model_dump() for p in payload.schema_def],
    )
    return _serialize_database(db_def)


@router.get("", response_model=List[schemas.DatabaseRead])
def list_databases(repo: Repository = Depends(get_repo)):
    return [_serialize_database(d) for d in repo.list_databases()]


# Rutas literales de un segmento ("templates", "from-template") declaradas
# ANTES de "/{database_id}": si fueran después, FastAPI intentaría parsear
# "templates" como un UUID y fallaría con 422 en vez de llegar aquí — el
# mismo problema que ya resolvimos con /pages/tree en la Fase 1.

@router.get("/templates")
def list_templates():
    return [
        {"key": key, "title": t["title"], "properties": [p["name"] for p in t["schema_def"]]}
        for key, t in TEMPLATES.items()
    ]


@router.post("/from-template", response_model=schemas.DatabaseRead)
def create_from_template(
    payload: schemas.DatabaseFromTemplate, repo: Repository = Depends(get_repo)
):
    template = TEMPLATES.get(payload.template)
    if not template:
        raise HTTPException(404, f"Plantilla '{payload.template}' no existe")

    db_def = repo.create_database(
        title=payload.title or template["title"],
        icon=settings.default_icon,
        created_by=settings.default_user_name,
        schema_def=template["schema_def"],
        views=template["views"],
    )
    return _serialize_database(db_def)


@router.get("/{database_id}", response_model=schemas.DatabaseRead)
def get_database(database_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    return _serialize_database(_get_database_or_404(repo, database_id))


@router.patch("/{database_id}", response_model=schemas.DatabaseRead)
def update_database(
    database_id: uuid.UUID, payload: schemas.DatabaseUpdate, repo: Repository = Depends(get_repo)
):
    db_def = repo.update_database(
        database_id,
        title=payload.title,
        icon=payload.icon,
        schema_def=None if payload.schema_def is None else [p.model_dump() for p in payload.schema_def],
    )
    if db_def is None:
        raise HTTPException(404, "Base de datos no encontrada")
    return _serialize_database(db_def)


@router.delete("/{database_id}", status_code=204)
def delete_database(database_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    if not repo.delete_database(database_id):
        raise HTTPException(404, "Base de datos no encontrada")


# --------------------------------------------------------------------- rows

@router.post("/{database_id}/rows", response_model=schemas.RowRead)
def create_row(
    database_id: uuid.UUID, payload: schemas.RowCreate, repo: Repository = Depends(get_repo)
):
    db_def = _get_database_or_404(repo, database_id)
    cleaned = validate_properties(db_def.schema_def, payload.properties)
    row = repo.create_row(
        database_id, title=payload.title, icon=payload.icon, properties=cleaned
    )
    return _serialize_row(row)


@router.get("/{database_id}/rows", response_model=List[schemas.RowRead])
def list_rows(
    database_id: uuid.UUID,
    view: Optional[uuid.UUID] = None,
    repo: Repository = Depends(get_repo),
):
    _get_database_or_404(repo, database_id)
    rows = repo.list_rows(database_id)

    if view is not None:
        view_def = repo.get_view(database_id, view)
        if view_def is None:
            raise HTTPException(404, "Vista no encontrada")
        rows = _apply_view(rows, view_def.config or {})

    return [_serialize_row(r) for r in rows]


@router.get("/{database_id}/rows/{row_id}", response_model=schemas.RowRead)
def get_row(
    database_id: uuid.UUID, row_id: uuid.UUID, repo: Repository = Depends(get_repo)
):
    return _serialize_row(_get_row_or_404(repo, database_id, row_id))


@router.patch("/{database_id}/rows/{row_id}", response_model=schemas.RowRead)
def update_row(
    database_id: uuid.UUID,
    row_id: uuid.UUID,
    payload: schemas.RowUpdate,
    repo: Repository = Depends(get_repo),
):
    db_def = _get_database_or_404(repo, database_id)
    _get_row_or_404(repo, database_id, row_id)

    cleaned = None
    if payload.properties is not None:
        cleaned = validate_properties(db_def.schema_def, payload.properties)

    row = repo.update_row(
        database_id,
        row_id,
        title=payload.title,
        icon=payload.icon,
        properties=cleaned,
    )
    if row is None:
        raise HTTPException(404, "Fila no encontrada")
    return _serialize_row(row)


@router.delete("/{database_id}/rows/{row_id}", status_code=204)
def delete_row(
    database_id: uuid.UUID, row_id: uuid.UUID, repo: Repository = Depends(get_repo)
):
    if not repo.delete_row(database_id, row_id):
        raise HTTPException(404, "Fila no encontrada")


# -------------------------------------------------------------------- views

@router.post("/{database_id}/views", response_model=schemas.ViewRead)
def create_view(
    database_id: uuid.UUID, payload: schemas.ViewCreate, repo: Repository = Depends(get_repo)
):
    _get_database_or_404(repo, database_id)
    view = repo.create_view(database_id, name=payload.name, type=payload.type, config=payload.config)
    return _serialize_view(view)


@router.get("/{database_id}/views", response_model=List[schemas.ViewRead])
def list_views(database_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    _get_database_or_404(repo, database_id)
    return [_serialize_view(v) for v in repo.list_views(database_id)]


@router.patch("/{database_id}/views/{view_id}", response_model=schemas.ViewRead)
def update_view(
    database_id: uuid.UUID,
    view_id: uuid.UUID,
    payload: schemas.ViewUpdate,
    repo: Repository = Depends(get_repo),
):
    view = repo.update_view(
        database_id, view_id, name=payload.name, type=payload.type, config=payload.config
    )
    if view is None:
        raise HTTPException(404, "Vista no encontrada")
    return _serialize_view(view)


@router.delete("/{database_id}/views/{view_id}", status_code=204)
def delete_view(
    database_id: uuid.UUID, view_id: uuid.UUID, repo: Repository = Depends(get_repo)
):
    if not repo.delete_view(database_id, view_id):
        raise HTTPException(404, "Vista no encontrada")


# ------------------------------------------------------------- aplicar vista

def _apply_view(rows: list[Page], config: dict) -> list[Page]:
    """Filtra y ordena en Python (no en SQL): a escala personal, con todas
    las filas ya en memoria, es más simple y suficientemente rápido que
    construir predicados dinámicos sobre JSON. group_by/visible_properties
    son metadatos para que el frontend decida cómo dibujar la vista (tablero,
    calendario...) — no se aplican aquí.
    """
    for f in config.get("filters") or []:
        key, op, value = f.get("key"), f.get("op", "eq"), f.get("value")
        rows = [r for r in rows if _match((r.properties or {}).get(key), op, value)]

    sort = config.get("sort")
    if sort and sort.get("key"):
        key = sort["key"]
        rows = sorted(
            rows,
            key=lambda r: _sort_key((r.properties or {}).get(key)),
            reverse=(sort.get("dir") == "desc"),
        )

    return rows


def _match(actual: Any, op: str, value: Any) -> bool:
    if op == "eq":
        return actual == value
    if op == "neq":
        return actual != value
    if op == "contains":
        return isinstance(actual, list) and value in actual
    if op == "is_empty":
        return actual in (None, "", [])
    if op == "is_not_empty":
        return actual not in (None, "", [])
    return True


def _sort_key(value: Any) -> tuple:
    # None siempre al final, con independencia del tipo del valor real
    return (value is None, str(value) if isinstance(value, (list, dict)) else value)
