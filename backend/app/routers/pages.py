import os
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from app import acl, activity, audit, auth, export, notifications, schemas
from app import workspaces as ws_mod
from app.config import settings
from app.dependencies import get_current_user, get_repo
from app.enums import PageType
from app.events import bus, publish_database
from app.repository import Repository
from app.serializers import serialize_page
from app.vault import attachments
from app.vault.store import VaultError, VersionConflict
from app.versioning import conflict, etag, parse_if_match

router = APIRouter(prefix="/pages", tags=["pages"])

MAX_HEADER_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB, generoso para una imagen de cabecera
# Un archivo dentro del cuerpo de una página (imagen, y más adelante vídeo,
# audio, PDF…). 50 MB: cabe un vídeo corto sin llenar el servidor.
MAX_FILE_BYTES = 50 * 1024 * 1024


def avisar_de_cambio(antes, page) -> None:
    """Las demás pestañas se enteran de un cambio que se ve fuera de la nota.

    - Título o icono nuevos: el árbol de la barra lateral (evento `tree`). En
      producción el vigilante del disco está apagado, así que sin esto otra
      pestaña seguía enseñando el nombre viejo hasta recargar.
    - Si es una fila, además su base (`database`/`rows`), para que la tabla
      se repinte.

    El cuerpo no avisa de nada: no sale ni en el árbol ni en la tabla, y
    avisar en cada guardado haría repintar mientras alguien escribe.
    """
    if antes is not None and (antes.title, antes.icon) == (page.title, page.icon):
        return
    bus.publish({"type": "tree", "action": "changed", "page_id": str(page.id)})
    if page.type == PageType.database_row and page.database_id:
        publish_database(page.database_id, "rows")


def _get_or_404(repo: Repository, page_id: uuid.UUID):
    page = repo.get_page(page_id)
    if page is None:
        raise HTTPException(404, "Página no encontrada")
    return page


@router.get("", response_model=List[schemas.PageRead])
def list_pages(repo: Repository = Depends(get_repo)):
    # El mapa de colores se hace una vez por respuesta, no una por página.
    colors = repo.author_colors()
    return [serialize_page(p, colors) for p in repo.list_pages()]


@router.post("", response_model=schemas.PageRead)
def create_page(
    payload: schemas.PageCreate,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    try:
        page = repo.create_page(
            title=payload.title,
            type=payload.type,
            parent_id=payload.parent_id,
            icon=payload.icon or settings.default_icon,
            description=payload.description,
            # Autoría real (P11a): el `display_name` de quien tiene la sesión.
            # Ya no hay literal de repuesto (`DEFAULT_USER_NAME`).
            created_by=user.display_name,
            body_markdown=payload.body_markdown or "",
        )
    except VaultError as exc:
        # Padre inexistente o de un tipo que todavía no vive en ficheros: 400
        # con el motivo, en vez de un 500 desde el fondo del vault.
        raise HTTPException(400, str(exc)) from exc
    if payload.section == "shared" and not payload.parent_id:
        repo.set_section(page.id, "shared")  # plan H2: nace para todo el espacio
    activity.record(repo.conn, page.id, user.id, "created")
    bus.publish({"type": "tree", "action": "created", "page_id": str(page.id)})
    return serialize_page(page, repo.author_colors())


@router.get("/tree")
def get_tree(repo: Repository = Depends(get_repo)):
    """Árbol de navegación (notas + contenedores de database), sin cuerpo.

    Las filas (type=database_row) se excluyen a propósito: viven dentro de
    la tabla/tablero de su database, no como nodos sueltos del árbol.
    """
    return repo.tree()


@router.get("/shared-with-me")
def get_shared_with_me(repo: Repository = Depends(get_repo)):
    """Lo que me han compartido como invitado en espacios que no son los míos."""
    return repo.guest_tree()


@router.get("/{page_id}", response_model=schemas.PageRead)
def get_page(page_id: uuid.UUID, response: Response, repo: Repository = Depends(get_repo)):
    page = _get_or_404(repo, page_id)
    response.headers["ETag"] = etag(page.version)
    return serialize_page(page, repo.author_colors())


@router.patch(
    "/{page_id}",
    response_model=schemas.PageRead,
    responses={409: {"description": "`If-Match` con una versión que ya no es la actual"}},
)
def update_page(
    page_id: uuid.UUID,
    payload: schemas.PageUpdate,
    response: Response,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
    if_match: str | None = Header(default=None),
):
    """Guarda los campos que llegan. Con `If-Match: <version>` (3.0, hito 2),
    solo si la página sigue en esa versión; si no, 409 con la de ahora."""
    expected = parse_if_match(if_match)
    antes = repo.get_page(page_id)
    if payload.section is not None or payload.workspace_id is not None:
        _reorganizar(repo, user, page_id, payload)
        if not _tiene_otros_cambios(payload):
            despues = _get_or_404(repo, page_id)
            bus.publish({"type": "tree", "action": "changed", "page_id": str(page_id)})
            response.headers["ETag"] = etag(despues.version)
            return serialize_page(despues, repo.author_colors())
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
            expected_version=expected,
            **extra,
        )
    except VersionConflict as exc:
        actual = _get_or_404(repo, page_id)
        return conflict(serialize_page(actual, repo.author_colors()), exc.current, exc.expected)
    except VaultError as exc:
        raise HTTPException(400, str(exc)) from exc
    if page is None:
        raise HTTPException(404, "Página no encontrada")
    avisar_de_cambio(antes, page)
    activity.record(repo.conn, page_id, user.id)
    if payload.body_markdown is not None:
        # Plan H6: menciones nuevas y cambios para quien se suscribió a todo.
        notifications.after_body_change(
            repo.conn, page_id, antes.body_markdown if antes else None, page.body_markdown, str(user.id)
        )
    response.headers["ETag"] = etag(page.version)
    return serialize_page(page, repo.author_colors())


@router.post("/{page_id}/move", status_code=204)
def move_page(
    page_id: uuid.UUID,
    payload: schemas.PageMove,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    """Arrastrar en el panel izquierdo: cambiar de madre, de sección o de
    espacio y colocar la página entre sus hermanas."""
    page = _get_or_404(repo, page_id)
    destino = str(payload.workspace_id) if payload.workspace_id else str(page.workspace_id)
    if payload.parent_id:
        madre = repo.get_page(payload.parent_id)
        if madre is None:
            raise HTTPException(404, "La página de destino no existe")
        destino = str(madre.workspace_id)
    cambia_de_sitio = destino != str(page.workspace_id) or (
        not payload.parent_id and payload.section is not None
    )
    if cambia_de_sitio:
        # Como `_reorganizar`: solo la propietaria cambia con quién se comparte,
        # y en el destino hay que poder editar.
        try:
            acl.authorize(repo.conn, user, page_id, "owner")
        except acl.NotVisible:
            raise HTTPException(404, "Página no encontrada") from None
        except acl.Forbidden:
            raise HTTPException(403, "Solo el propietario puede cambiar con quién se comparte o mover la página") from None
        role = ws_mod.role_of(repo.conn, user, destino)
        if ws_mod.get(repo.conn, destino) is None or role is None:
            raise HTTPException(404, "No existe ese espacio")
        if role == "viewer":
            raise HTTPException(403, "En ese espacio solo puedes leer")
    try:
        repo.move_page(
            page_id,
            parent_id=payload.parent_id,
            before_id=payload.before_id,
            workspace_id=destino,
            section=payload.section,
        )
    except VaultError as exc:
        raise HTTPException(400, str(exc)) from exc
    if cambia_de_sitio:
        audit.record(repo.conn, user, "page.moved", workspace_id=destino, target_type="page",
                     target_id=page_id, metadata={"to_workspace": destino, "section": payload.section})
    bus.publish({"type": "tree", "action": "changed", "page_id": str(page_id)})


def _tiene_otros_cambios(payload: schemas.PageUpdate) -> bool:
    return bool(
        {"title", "icon", "description", "body_markdown", "parent_id"} & payload.model_fields_set
    )


def _reorganizar(repo: Repository, user: auth.User, page_id: uuid.UUID, payload: schemas.PageUpdate) -> None:
    """Compartido/Privado y mover a otro espacio (plan H2). Solo el propietario
    cambia con quién se comparte, y mover pide poder editar en el destino."""
    try:
        acl.authorize(repo.conn, user, page_id, "owner")
    except acl.NotVisible:
        raise HTTPException(404, "Página no encontrada") from None
    except acl.Forbidden:
        raise HTTPException(403, "Solo el propietario puede cambiar con quién se comparte o mover la página") from None
    try:
        if payload.workspace_id is not None:
            destino = str(payload.workspace_id)
            role = ws_mod.role_of(repo.conn, user, destino)
            if ws_mod.get(repo.conn, destino) is None or role is None:
                raise HTTPException(404, "No existe ese espacio")
            if role == "viewer":
                raise HTTPException(403, "En ese espacio solo puedes leer")
            repo.move_to_workspace(page_id, destino, payload.section)
            audit.record(repo.conn, user, "page.moved", workspace_id=destino, target_type="page",
                         target_id=page_id, metadata={"to_workspace": destino, "section": payload.section})
        elif payload.section is not None:
            repo.set_section(page_id, payload.section)
            audit.record(repo.conn, user, "page.section_changed", target_type="page",
                         target_id=page_id, metadata={"section": payload.section})
    except VaultError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/{page_id}", status_code=204)
def delete_page(
    page_id: uuid.UUID,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    borrada = _get_or_404(repo, page_id)
    # El borrado en cascada del índice se lleva, para una página type=database,
    # su database, sus vistas y sus filas -- el mismo estado que DELETE
    # /databases/{id}, sin dejar filas huérfanas fuera del árbol.
    try:
        repo.delete_page(page_id)
    except VaultError as exc:
        # p. ej. una base colgando de la nota: la cascada se la llevaría sin
        # papelera. 400 con el motivo, como el resto de reglas del vault.
        raise HTTPException(400, str(exc)) from exc
    audit.record(repo.conn, user, "page.deleted", workspace_id=str(borrada.workspace_id),
                 target_type="page", target_id=page_id, target_name=borrada.title)
    bus.publish({"type": "tree", "action": "deleted", "page_id": str(page_id)})


@router.get("/{page_id}/backlinks", response_model=List[schemas.PageRead])
def get_backlinks(page_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    """Páginas cuyo cuerpo contiene un [[wikilink]] hacia esta página."""
    _get_or_404(repo, page_id)
    colors = repo.author_colors()
    return [serialize_page(p, colors) for p in repo.backlinks(page_id)]


@router.get("/{page_id}/export.md", response_class=Response)
def export_page_markdown(
    page_id: uuid.UUID,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    """El `.md` de la nota, con cada bloque de base sustituido por su tabla (P17.4).

    Descarga de verdad (`Content-Disposition: attachment`), y **solo lectura**:
    compone un texto a partir de lo que ya está en el índice. El navegador ya no
    tiene que pedir N veces la API y montar la tabla; el `.md` que se guarda se
    lee fuera de la app.
    """
    page = _get_or_404(repo, page_id)
    audit.record(repo.conn, user, "database.exported" if page.type == "database" else "page.exported",
                 workspace_id=str(page.workspace_id), target_type=page.type, target_id=page_id, target_name=page.title)
    return Response(
        content=export.export_page(repo, page),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": export.content_disposition(page.title)},
    )


@router.post("/{page_id}/files", status_code=201)
async def upload_file(page_id: uuid.UUID, file: UploadFile, repo: Repository = Depends(get_repo)):
    """Sube un archivo para el cuerpo de la página y devuelve cómo enlazarlo.

    Va al vault (`_attachments/<año>/<mes>/<id>-<nombre>.<ext>`) como la
    portada, así que la copia de la carpeta se lleva el archivo con su página.
    El tipo sale de los magic bytes, nunca del nombre ni del Content-Type del
    cliente. Por ahora solo imágenes (PNG, JPEG, GIF, WebP). Pide `edit` sobre
    la página (la guarda: es un POST bajo `/pages/{page_id}`).

    Devuelve `{url, name, size, kind, markdown}`: `markdown` es la línea que
    el editor inserta (`![nombre](url)`).
    """
    _get_or_404(repo, page_id)
    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, f"Máximo {MAX_FILE_BYTES // (1024 * 1024)} MB por archivo")
    if not data:
        raise HTTPException(400, "El archivo está vacío")
    ext = attachments.detect_image_format(data)
    if ext is None:
        raise HTTPException(415, "Por ahora solo se pueden subir imágenes PNG, JPEG, GIF o WebP")
    nombre = os.path.basename(file.filename or "") or f"imagen.{ext}"
    rel = attachments.save_file(settings.vault_path, nombre, data, ext)
    url = attachments.url_for(rel)
    # El texto alternativo es el nombre sin extensión; los corchetes y saltos
    # romperían la sintaxis de la imagen.
    alt = os.path.splitext(nombre)[0].replace("[", "(").replace("]", ")").replace("\n", " ").strip() or "imagen"
    return {
        "url": url,
        "name": nombre,
        "size": len(data),
        "kind": "image",
        "markdown": f"![{alt}]({url})",
    }


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
    return serialize_page(page, repo.author_colors())


# Portadas genéricas (degradados con los colores de la marca) que sirve el
# propio frontend en `/covers/<nombre>.svg`. La cabecera apunta ahí en vez de a
# un adjunto del vault, así que no hay fichero que copiar ni que borrar.
COVER_PRESETS = ("indigo", "ping", "noche", "aurora", "coral", "lavanda", "crepusculo", "niebla")


class HeaderPreset(BaseModel):
    preset: str


@router.put("/{page_id}/header-image/preset", response_model=schemas.PageRead)
def put_header_preset(page_id: uuid.UUID, payload: HeaderPreset, repo: Repository = Depends(get_repo)):
    """Pone una de las portadas genéricas (`COVER_PRESETS`) como cabecera."""
    _get_or_404(repo, page_id)
    if payload.preset not in COVER_PRESETS:
        raise HTTPException(400, f"Portada desconocida: {payload.preset!r}")
    try:
        page = repo.set_header_image(page_id, f"/covers/{payload.preset}.svg")
    except VaultError as exc:
        raise HTTPException(400, str(exc)) from exc
    # La imagen subida que hubiera deja de usarse: fuera, como al quitarla.
    attachments.delete_header(settings.vault_path, page_id)
    return serialize_page(page, repo.author_colors())


class HeaderPosition(BaseModel):
    position: int = Field(ge=0, le=100)


@router.put("/{page_id}/header-image/position", response_model=schemas.PageRead)
def put_header_position(page_id: uuid.UUID, payload: HeaderPosition, repo: Repository = Depends(get_repo)):
    """Encuadre vertical de la imagen de cabecera (0 arriba, 100 abajo)."""
    page = _get_or_404(repo, page_id)
    if not page.header_image_path:
        raise HTTPException(400, "La página no tiene imagen de cabecera")
    try:
        page = repo.set_header_position(page_id, payload.position)
    except VaultError as exc:
        raise HTTPException(400, str(exc)) from exc
    return serialize_page(page, repo.author_colors())


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
    return serialize_page(page, repo.author_colors())
