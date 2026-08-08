import uuid
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db
from app.properties import validate_properties
from app.templates import TEMPLATES

router = APIRouter(prefix="/databases", tags=["databases"])


def _get_default_workspace(db: Session) -> models.Workspace:
    ws = db.query(models.Workspace).first()
    if not ws:
        raise HTTPException(500, "No hay workspace inicializado")
    return ws


def _get_database_or_404(db: Session, database_id: uuid.UUID) -> models.DatabaseDef:
    db_def = db.get(models.DatabaseDef, database_id)
    if not db_def:
        raise HTTPException(404, "Base de datos no encontrada")
    return db_def


def _get_row_or_404(db: Session, database_id: uuid.UUID, row_id: uuid.UUID) -> models.Page:
    row = db.get(models.Page, row_id)
    if not row or row.database_id != database_id:
        raise HTTPException(404, "Fila no encontrada")
    return row


def _serialize_database(db_def: models.DatabaseDef) -> schemas.DatabaseRead:
    page = db_def.page
    return schemas.DatabaseRead(
        id=db_def.id,
        page_id=page.id,
        title=page.title,
        icon=page.icon,
        schema_def=db_def.schema_def,
        created_at=page.created_at,
        updated_at=page.updated_at,
    )


def _serialize_row(row: models.Page) -> schemas.RowRead:
    return schemas.RowRead(
        id=row.id,
        database_id=row.database_id,
        title=row.title,
        icon=row.icon,
        properties=row.properties or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _serialize_view(view: models.View) -> schemas.ViewRead:
    return schemas.ViewRead(
        id=view.id,
        database_id=view.database_id,
        name=view.name,
        type=view.type,
        config=view.config or {},
    )


# ---------------------------------------------------------------- databases

@router.post("", response_model=schemas.DatabaseRead)
def create_database(payload: schemas.DatabaseCreate, db: Session = Depends(get_db)):
    ws = _get_default_workspace(db)
    page = models.Page(
        workspace_id=ws.id,
        title=payload.title,
        type=models.PageType.database,
        parent_id=payload.parent_id,
        icon=payload.icon,
    )
    db.add(page)
    db.flush()

    db_def = models.DatabaseDef(
        page_id=page.id,
        schema_def=[p.model_dump() for p in payload.schema_def],
    )
    db.add(db_def)

    db.commit()
    db.refresh(db_def)
    return _serialize_database(db_def)


@router.get("", response_model=List[schemas.DatabaseRead])
def list_databases(db: Session = Depends(get_db)):
    return [_serialize_database(d) for d in db.query(models.DatabaseDef).all()]


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
def create_from_template(payload: schemas.DatabaseFromTemplate, db: Session = Depends(get_db)):
    template = TEMPLATES.get(payload.template)
    if not template:
        raise HTTPException(404, f"Plantilla '{payload.template}' no existe")

    ws = _get_default_workspace(db)
    page = models.Page(workspace_id=ws.id, title=payload.title or template["title"], type=models.PageType.database)
    db.add(page)
    db.flush()

    db_def = models.DatabaseDef(page_id=page.id, schema_def=template["schema_def"])
    db.add(db_def)
    db.flush()

    for v in template["views"]:
        db.add(models.View(database_id=db_def.id, name=v["name"], type=v["type"], config=v["config"]))

    db.commit()
    db.refresh(db_def)
    return _serialize_database(db_def)


@router.get("/{database_id}", response_model=schemas.DatabaseRead)
def get_database(database_id: uuid.UUID, db: Session = Depends(get_db)):
    return _serialize_database(_get_database_or_404(db, database_id))


@router.patch("/{database_id}", response_model=schemas.DatabaseRead)
def update_database(database_id: uuid.UUID, payload: schemas.DatabaseUpdate, db: Session = Depends(get_db)):
    db_def = _get_database_or_404(db, database_id)
    page = db_def.page

    if payload.title is not None:
        page.title = payload.title
    if payload.icon is not None:
        page.icon = payload.icon
    if payload.schema_def is not None:
        # Nota (v1): no se revalidan retroactivamente las filas existentes
        # contra el schema nuevo — pueden quedar con propiedades que ya no
        # están definidas hasta que se vuelvan a guardar.
        db_def.schema_def = [p.model_dump() for p in payload.schema_def]

    db.commit()
    db.refresh(db_def)
    return _serialize_database(db_def)


@router.delete("/{database_id}", status_code=204)
def delete_database(database_id: uuid.UUID, db: Session = Depends(get_db)):
    db_def = _get_database_or_404(db, database_id)
    # Las filas no cuelgan de la página vía parent_id, así que el cascade de
    # `pages` no las alcanza al borrar la página contenedora: hay que
    # borrarlas explícitamente primero.
    db.query(models.Page).filter(models.Page.database_id == database_id).delete()
    db.delete(db_def.page)  # cascada: DatabaseDef, views, links, tags, adjuntos de esa página
    db.commit()


# --------------------------------------------------------------------- rows

@router.post("/{database_id}/rows", response_model=schemas.RowRead)
def create_row(database_id: uuid.UUID, payload: schemas.RowCreate, db: Session = Depends(get_db)):
    db_def = _get_database_or_404(db, database_id)
    ws = _get_default_workspace(db)
    cleaned = validate_properties(db_def.schema_def, payload.properties)

    row = models.Page(
        workspace_id=ws.id,
        title=payload.title,
        type=models.PageType.database_row,
        database_id=database_id,
        icon=payload.icon,
        properties=cleaned,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_row(row)


@router.get("/{database_id}/rows", response_model=List[schemas.RowRead])
def list_rows(database_id: uuid.UUID, view: Optional[uuid.UUID] = None, db: Session = Depends(get_db)):
    _get_database_or_404(db, database_id)
    rows = db.query(models.Page).filter(models.Page.database_id == database_id).all()

    if view is not None:
        view_def = db.get(models.View, view)
        if not view_def or view_def.database_id != database_id:
            raise HTTPException(404, "Vista no encontrada")
        rows = _apply_view(rows, view_def.config or {})

    return [_serialize_row(r) for r in rows]


@router.get("/{database_id}/rows/{row_id}", response_model=schemas.RowRead)
def get_row(database_id: uuid.UUID, row_id: uuid.UUID, db: Session = Depends(get_db)):
    return _serialize_row(_get_row_or_404(db, database_id, row_id))


@router.patch("/{database_id}/rows/{row_id}", response_model=schemas.RowRead)
def update_row(database_id: uuid.UUID, row_id: uuid.UUID, payload: schemas.RowUpdate, db: Session = Depends(get_db)):
    db_def = _get_database_or_404(db, database_id)
    row = _get_row_or_404(db, database_id, row_id)

    if payload.title is not None:
        row.title = payload.title
    if payload.icon is not None:
        row.icon = payload.icon
    if payload.properties is not None:
        cleaned = validate_properties(db_def.schema_def, payload.properties)
        row.properties = {**(row.properties or {}), **cleaned}

    db.commit()
    db.refresh(row)
    return _serialize_row(row)


@router.delete("/{database_id}/rows/{row_id}", status_code=204)
def delete_row(database_id: uuid.UUID, row_id: uuid.UUID, db: Session = Depends(get_db)):
    row = _get_row_or_404(db, database_id, row_id)
    db.delete(row)
    db.commit()


# -------------------------------------------------------------------- views

@router.post("/{database_id}/views", response_model=schemas.ViewRead)
def create_view(database_id: uuid.UUID, payload: schemas.ViewCreate, db: Session = Depends(get_db)):
    _get_database_or_404(db, database_id)
    view = models.View(
        database_id=database_id,
        name=payload.name,
        type=payload.type,
        config=payload.config or {},
    )
    db.add(view)
    db.commit()
    db.refresh(view)
    return _serialize_view(view)


@router.get("/{database_id}/views", response_model=List[schemas.ViewRead])
def list_views(database_id: uuid.UUID, db: Session = Depends(get_db)):
    _get_database_or_404(db, database_id)
    views = db.query(models.View).filter(models.View.database_id == database_id).all()
    return [_serialize_view(v) for v in views]


@router.patch("/{database_id}/views/{view_id}", response_model=schemas.ViewRead)
def update_view(
    database_id: uuid.UUID, view_id: uuid.UUID, payload: schemas.ViewUpdate, db: Session = Depends(get_db)
):
    view = db.get(models.View, view_id)
    if not view or view.database_id != database_id:
        raise HTTPException(404, "Vista no encontrada")

    if payload.name is not None:
        view.name = payload.name
    if payload.type is not None:
        view.type = payload.type
    if payload.config is not None:
        view.config = payload.config

    db.commit()
    db.refresh(view)
    return _serialize_view(view)


@router.delete("/{database_id}/views/{view_id}", status_code=204)
def delete_view(database_id: uuid.UUID, view_id: uuid.UUID, db: Session = Depends(get_db)):
    view = db.get(models.View, view_id)
    if not view or view.database_id != database_id:
        raise HTTPException(404, "Vista no encontrada")
    db.delete(view)
    db.commit()


# ------------------------------------------------------------- aplicar vista

def _apply_view(rows: list[models.Page], config: dict) -> list[models.Page]:
    """Filtra y ordena en Python (no en SQL): a escala personal, con todas
    las filas ya en memoria, es más simple y suficientemente rápido que
    construir predicados dinámicos sobre JSONB. group_by/visible_properties
    son metadatos para que el frontend decida cómo dibujar la vista (tablero,
    calendario...) — no se aplican aquí."""
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
