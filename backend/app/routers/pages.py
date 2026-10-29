import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from app import schemas
from app.config import settings
from app.dependencies import get_repo
from app.repository import Repository
from app.serializers import serialize_page
from app.vault import attachments
from app.vault.store import VaultError

router = APIRouter(prefix="/pages", tags=["pages"])

MAX_HEADER_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB, generoso para una imagen de cabecera


def _get_or_404(repo: Repository, page_id: uuid.UUID):
    page = repo.get_page(page_id)
    if page is None:
        raise HTTPException(404, "Página no encontrada")
    return page


@router.get("", response_model=List[schemas.PageRead])
def list_pages(repo: Repository = Depends(get_repo)):
    return [serialize_page(p) for p in repo.list_pages()]


@router.post("", response_model=schemas.PageRead)
def create_page(payload: schemas.PageCreate, repo: Repository = Depends(get_repo)):
    try:
        page = repo.create_page(
            title=payload.title,
            type=payload.type,
            parent_id=payload.parent_id,
            icon=payload.icon or settings.default_icon,
            description=payload.description,
            created_by=settings.default_user_name,  # "obligatorio": la API siempre lo rellena
            body_markdown=payload.body_markdown or "",
        )
    except VaultError as exc:
        # Padre inexistente o de un tipo que todavía no vive en ficheros: 400
        # con el motivo, en vez de un 500 desde el fondo del vault.
        raise HTTPException(400, str(exc)) from exc
    return serialize_page(page)


@router.get("/tree")
def get_tree(repo: Repository = Depends(get_repo)):
    """Árbol de navegación (notas + contenedores de database), sin cuerpo.

    Las filas (type=database_row) se excluyen a propósito: viven dentro de
    la tabla/tablero de su database, no como nodos sueltos del árbol.
    """
    return repo.tree()


@router.get("/{page_id}", response_model=schemas.PageRead)
def get_page(page_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    return serialize_page(_get_or_404(repo, page_id))


@router.patch("/{page_id}", response_model=schemas.PageRead)
def update_page(
    page_id: uuid.UUID, payload: schemas.PageUpdate, repo: Repository = Depends(get_repo)
):
    extra = {}
    if "parent_id" in payload.model_fields_set:
        # Presente en el cuerpo: mover (o `null` para subir a la raíz).
        extra["parent_id"] = payload.parent_id
    try:
        page = repo.update_page(
            page_id,
            title=payload.title,
            icon=payload.icon,
            description=payload.description,
            body_markdown=payload.body_markdown,
            **extra,
        )
    except VaultError as exc:
        raise HTTPException(400, str(exc)) from exc
    if page is None:
        raise HTTPException(404, "Página no encontrada")
    return serialize_page(page)


@router.delete("/{page_id}", status_code=204)
def delete_page(page_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    _get_or_404(repo, page_id)
    # El borrado en cascada del índice se lleva, para una página type=database,
    # su database, sus vistas y sus filas -- el mismo estado que DELETE
    # /databases/{id}, sin dejar filas huérfanas fuera del árbol.
    try:
        repo.delete_page(page_id)
    except VaultError as exc:
        # p. ej. una base colgando de la nota: la cascada se la llevaría sin
        # papelera. 400 con el motivo, como el resto de reglas del vault.
        raise HTTPException(400, str(exc)) from exc


@router.get("/{page_id}/backlinks", response_model=List[schemas.PageRead])
def get_backlinks(page_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    """Páginas cuyo cuerpo contiene un [[wikilink]] hacia esta página."""
    _get_or_404(repo, page_id)
    return [serialize_page(p) for p in repo.backlinks(page_id)]


@router.post("/{page_id}/header-image", response_model=schemas.PageRead)
async def upload_header_image(
    page_id: uuid.UUID, file: UploadFile, repo: Repository = Depends(get_repo)
):
    _get_or_404(repo, page_id)

    data = await file.read()
    if len(data) > MAX_HEADER_IMAGE_BYTES:
        raise HTTPException(400, f"Máximo {MAX_HEADER_IMAGE_BYTES // (1024 * 1024)} MB")

    # La extensión sale del formato real (magic bytes), nunca del nombre de
    # archivo ni del Content-Type que declara el cliente: es lo que decide con
    # qué nombre y con qué Content-Type se sirve luego desde `/files`.
    ext = attachments.detect_image_format(data)
    if ext is None:
        raise HTTPException(
            415, "Formato no admitido: la cabecera debe ser una imagen PNG, JPEG, GIF o WebP"
        )

    # El adjunto va DENTRO del vault (`_attachments/<año>/<mes>/`): copiar la
    # carpeta del vault se lleva la imagen con su nota (ADR-0001, P1.2b).
    rel = attachments.save_header(settings.vault_path, page_id, data, ext)
    # Nombre fijo por página: reemplaza la cabecera anterior --también si era
    # de otro formato-- en vez de ir acumulando ficheros huérfanos.
    attachments.delete_header(settings.vault_path, page_id, keep=rel)

    try:
        page = repo.set_header_image(page_id, attachments.url_for(rel))
    except VaultError as exc:
        # Sin `.md` (borrado por fuera) no hay dónde apuntar la cabecera: se
        # retira la imagen recién subida para no dejar un huérfano y se explica.
        (Path(settings.vault_path) / rel).unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    return serialize_page(page)


@router.delete("/{page_id}/header-image", response_model=schemas.PageRead)
def delete_header_image(page_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    _get_or_404(repo, page_id)

    try:
        page = repo.set_header_image(page_id, None)
    except VaultError as exc:
        raise HTTPException(400, str(exc)) from exc
    # La imagen se borra DESPUÉS de que el `.md` deje de apuntar a ella: si el
    # guardado falla, el frontmatter no queda apuntando a un fichero borrado.
    attachments.delete_header(settings.vault_path, page_id)
    return serialize_page(page)
