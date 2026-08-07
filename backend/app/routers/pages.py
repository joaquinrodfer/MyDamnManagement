import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db

router = APIRouter(prefix="/pages", tags=["pages"])


def _get_default_workspace(db: Session) -> models.Workspace:
    ws = db.query(models.Workspace).first()
    if not ws:
        raise HTTPException(500, "No hay workspace inicializado")
    return ws


def _serialize(page: models.Page, content: Optional[models.PageContent] = None) -> schemas.PageRead:
    body = content.body_markdown if content else (page.content.body_markdown if page.content else None)
    return schemas.PageRead(
        id=page.id,
        workspace_id=page.workspace_id,
        title=page.title,
        type=page.type,
        parent_id=page.parent_id,
        icon=page.icon,
        created_at=page.created_at,
        updated_at=page.updated_at,
        body_markdown=body,
    )


@router.get("", response_model=List[schemas.PageRead])
def list_pages(db: Session = Depends(get_db)):
    pages = db.query(models.Page).order_by(models.Page.created_at.desc()).all()
    return [_serialize(p) for p in pages]


@router.post("", response_model=schemas.PageRead)
def create_page(payload: schemas.PageCreate, db: Session = Depends(get_db)):
    ws = _get_default_workspace(db)
    page = models.Page(
        workspace_id=ws.id,
        title=payload.title,
        type=payload.type,
        parent_id=payload.parent_id,
        icon=payload.icon,
    )
    db.add(page)
    db.flush()  # asigna page.id sin cerrar la transacción

    content = models.PageContent(page_id=page.id, body_markdown=payload.body_markdown or "")
    db.add(content)
    db.commit()
    db.refresh(page)
    return _serialize(page, content)


@router.get("/{page_id}", response_model=schemas.PageRead)
def get_page(page_id: uuid.UUID, db: Session = Depends(get_db)):
    page = db.get(models.Page, page_id)
    if not page:
        raise HTTPException(404, "Página no encontrada")
    return _serialize(page)


@router.patch("/{page_id}", response_model=schemas.PageRead)
def update_page(page_id: uuid.UUID, payload: schemas.PageUpdate, db: Session = Depends(get_db)):
    page = db.get(models.Page, page_id)
    if not page:
        raise HTTPException(404, "Página no encontrada")

    if payload.title is not None:
        page.title = payload.title
    if payload.icon is not None:
        page.icon = payload.icon
    if payload.body_markdown is not None:
        if page.content:
            page.content.body_markdown = payload.body_markdown
        else:
            db.add(models.PageContent(page_id=page.id, body_markdown=payload.body_markdown))

    db.commit()
    db.refresh(page)
    return _serialize(page)


@router.delete("/{page_id}", status_code=204)
def delete_page(page_id: uuid.UUID, db: Session = Depends(get_db)):
    page = db.get(models.Page, page_id)
    if not page:
        raise HTTPException(404, "Página no encontrada")
    db.delete(page)
    db.commit()
