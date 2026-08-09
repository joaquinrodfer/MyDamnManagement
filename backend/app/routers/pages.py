import uuid
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app import models, schemas
from app.config import settings
from app.database import get_db
from app.serializers import serialize_page
from app.wikilinks import sync_wikilinks

router = APIRouter(prefix="/pages", tags=["pages"])

ATTACHMENTS_DIR = Path("/app/attachments")
MAX_HEADER_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB, generoso para una imagen de cabecera


def _get_default_workspace(db: Session) -> models.Workspace:
    ws = db.query(models.Workspace).first()
    if not ws:
        raise HTTPException(500, "No hay workspace inicializado")
    return ws


@router.get("", response_model=List[schemas.PageRead])
def list_pages(db: Session = Depends(get_db)):
    pages = db.query(models.Page).order_by(models.Page.created_at.desc()).all()
    return [serialize_page(p) for p in pages]


@router.post("", response_model=schemas.PageRead)
def create_page(payload: schemas.PageCreate, db: Session = Depends(get_db)):
    ws = _get_default_workspace(db)
    page = models.Page(
        workspace_id=ws.id,
        title=payload.title,
        type=payload.type,
        parent_id=payload.parent_id,
        icon=payload.icon or settings.default_icon,
        description=payload.description,
        created_by=settings.default_user_name,  # "obligatorio": la API siempre lo rellena
    )
    db.add(page)
    db.flush()  # asigna page.id sin cerrar la transacción

    body = payload.body_markdown or ""
    content = models.PageContent(page_id=page.id, body_markdown=body)
    db.add(content)
    sync_wikilinks(db, page, body)

    db.commit()
    db.refresh(page)
    return serialize_page(page, content)


@router.get("/tree")
def get_tree(db: Session = Depends(get_db)):
    """Árbol de navegación (notas + contenedores de database), sin cuerpo.

    Las filas (type=database_row) se excluyen a propósito: viven dentro de
    la tabla/tablero de su database, no como nodos sueltos del árbol.
    """
    pages = (
        db.query(models.Page)
        .filter(models.Page.type != models.PageType.database_row)
        .order_by(models.Page.title)
        .all()
    )

    # El id de una `page` de type=database (Page.id) y el id de la fila
    # `databases` que la describe (DatabaseDef.id) son dos UUID distintos
    # -- el frontend necesita este segundo para pedir /databases/{id}.
    db_defs = db.query(models.DatabaseDef).filter(
        models.DatabaseDef.page_id.in_([p.id for p in pages if p.type == models.PageType.database])
    )
    database_id_by_page_id = {d.page_id: d.id for d in db_defs}

    nodes = {
        p.id: {
            "id": p.id,
            "title": p.title,
            "type": p.type,
            "icon": p.icon,
            "parent_id": p.parent_id,  # para el botón "subir un nivel" del panel
            "database_id": database_id_by_page_id.get(p.id),
            "children": [],
        }
        for p in pages
    }
    roots = []
    for p in pages:
        node = nodes[p.id]
        if p.parent_id and p.parent_id in nodes:
            nodes[p.parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


@router.get("/{page_id}", response_model=schemas.PageRead)
def get_page(page_id: uuid.UUID, db: Session = Depends(get_db)):
    page = db.get(models.Page, page_id)
    if not page:
        raise HTTPException(404, "Página no encontrada")
    return serialize_page(page)


@router.patch("/{page_id}", response_model=schemas.PageRead)
def update_page(page_id: uuid.UUID, payload: schemas.PageUpdate, db: Session = Depends(get_db)):
    page = db.get(models.Page, page_id)
    if not page:
        raise HTTPException(404, "Página no encontrada")

    if payload.title is not None:
        page.title = payload.title
    if payload.icon is not None:
        page.icon = payload.icon
    if payload.description is not None:
        page.description = payload.description
    if payload.body_markdown is not None:
        if page.content:
            page.content.body_markdown = payload.body_markdown
        else:
            db.add(models.PageContent(page_id=page.id, body_markdown=payload.body_markdown))
        sync_wikilinks(db, page, payload.body_markdown)
        # page.updated_at tiene onupdate=datetime.utcnow, pero eso solo
        # dispara cuando SQLAlchemy considera "sucia" alguna columna de la
        # propia Page -- el cuerpo vive en page_content (otra tabla), así
        # que guardar solo el cuerpo no tocaba la Page en absoluto y
        # updated_at se quedaba congelado en el último cambio de
        # título/icono/descripción, por poco reciente que fuera de verdad
        # el contenido (afectaba al orden de "Páginas recientes" del panel).
        page.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(page)
    return serialize_page(page)


@router.delete("/{page_id}", status_code=204)
def delete_page(page_id: uuid.UUID, db: Session = Depends(get_db)):
    page = db.get(models.Page, page_id)
    if not page:
        raise HTTPException(404, "Página no encontrada")
    db.delete(page)
    db.commit()


@router.get("/{page_id}/backlinks", response_model=List[schemas.PageRead])
def get_backlinks(page_id: uuid.UUID, db: Session = Depends(get_db)):
    """Páginas cuyo cuerpo contiene un [[wikilink]] hacia esta página."""
    page = db.get(models.Page, page_id)
    if not page:
        raise HTTPException(404, "Página no encontrada")

    source_ids = (
        db.query(models.Link.source_page_id)
        .filter(
            models.Link.target_page_id == page_id,
            models.Link.kind == models.LinkKind.wikilink,
        )
        .subquery()
    )
    pages = db.query(models.Page).filter(models.Page.id.in_(source_ids)).all()
    return [serialize_page(p) for p in pages]


@router.post("/{page_id}/header-image", response_model=schemas.PageRead)
async def upload_header_image(page_id: uuid.UUID, file: UploadFile, db: Session = Depends(get_db)):
    page = db.get(models.Page, page_id)
    if not page:
        raise HTTPException(404, "Página no encontrada")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "El archivo debe ser una imagen")

    data = await file.read()
    if len(data) > MAX_HEADER_IMAGE_BYTES:
        raise HTTPException(400, f"Máximo {MAX_HEADER_IMAGE_BYTES // (1024 * 1024)} MB")

    page_dir = ATTACHMENTS_DIR / "pages" / str(page_id)
    page_dir.mkdir(parents=True, exist_ok=True)

    # Nombre fijo ("header.<ext>"): reemplaza cualquier cabecera anterior de
    # esta página en vez de ir acumulando archivos huérfanos.
    for old in page_dir.glob("header.*"):
        old.unlink(missing_ok=True)

    ext = Path(file.filename or "").suffix or ".jpg"
    dest = page_dir / f"header{ext}"
    dest.write_bytes(data)

    page.header_image_path = f"/files/pages/{page_id}/{dest.name}"
    db.commit()
    db.refresh(page)
    return serialize_page(page)


@router.delete("/{page_id}/header-image", response_model=schemas.PageRead)
def delete_header_image(page_id: uuid.UUID, db: Session = Depends(get_db)):
    page = db.get(models.Page, page_id)
    if not page:
        raise HTTPException(404, "Página no encontrada")

    page_dir = ATTACHMENTS_DIR / "pages" / str(page_id)
    for old in page_dir.glob("header.*"):
        old.unlink(missing_ok=True)

    page.header_image_path = None
    db.commit()
    db.refresh(page)
    return serialize_page(page)
