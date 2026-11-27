"""Chat del equipo (`docs/PLAN-CHAT.md`), con los permisos de `app.chat`.

- `GET /chat/conversations`: las mías (canales de mis espacios y directos), con no leídos.
- `GET /chat/unread`: el total sin leer (para el contador de la pestaña).
- `GET /chat/conversations/{id}/messages?before=&limit=`: por páginas, de viejo a nuevo.
- `POST /chat/conversations/{id}/messages` `{body, attachments}`.
- `PATCH /chat/messages/{id}` `{body}` y `DELETE /chat/messages/{id}`: solo su autor.
- `POST /chat/conversations/{id}/read` `{last_id}`.
- `POST /chat/direct` `{user_ids}`: abre (o reutiliza) el chat privado con una persona.
- `POST /chat/groups` `{title, user_ids}`: un grupo nuevo; `PATCH /chat/conversations/{id}` `{title}` lo renombra.
- `POST /chat/conversations/{id}/members` `{user_ids}`, `DELETE /chat/conversations/{id}/members/me`: solo grupos.
"""

from __future__ import annotations

from typing import Any, Optional

import os

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from app import auth, chat
from app.config import settings
from app.vault import attachments
from app.dependencies import get_current_user, get_repo
from app.repository import Repository

router = APIRouter(prefix="/chat", tags=["chat"])


def _err(exc: chat.ChatError):
    raise HTTPException(exc.status, str(exc)) from None


class MessageIn(BaseModel):
    body: str = Field(default="", max_length=chat.MAX_BODY + 100)
    attachments: Optional[list[dict[str, Any]]] = None


class EditIn(BaseModel):
    body: str = Field(default="", max_length=chat.MAX_BODY + 100)


class ReadIn(BaseModel):
    last_id: Optional[int] = None


class PeopleIn(BaseModel):
    user_ids: list[str]


class GroupIn(BaseModel):
    title: str = Field(default="", max_length=chat.MAX_TITLE + 20)
    user_ids: list[str]


class RenameIn(BaseModel):
    title: str = Field(default="", max_length=chat.MAX_TITLE + 20)


def _publish_event(repo: Repository, conv: dict, msg: Optional[dict]) -> None:
    if msg is not None:
        chat.publish(repo.conn, conv, "message", message=msg)


def _conv(repo: Repository, user: auth.User, conversation_id: str) -> dict:
    try:
        return chat.get_for(repo.conn, user, conversation_id)
    except chat.ChatError as exc:
        _err(exc)


@router.get("/conversations")
def list_conversations(repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    return chat.listing(repo.conn, user)


@router.get("/unread")
def unread(repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    return {"unread": chat.unread_total(repo.conn, user)}


@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: str,
    before: Optional[int] = Query(default=None, ge=1),
    limit: int = Query(default=chat.PAGE_SIZE, ge=1, le=200),
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    conv = _conv(repo, user, conversation_id)
    return {"messages": chat.messages(repo.conn, conv, before, limit)}


@router.post("/conversations/{conversation_id}/messages", status_code=201)
def post_message(
    conversation_id: str,
    payload: MessageIn,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    conv = _conv(repo, user, conversation_id)
    try:
        msg = chat.post(repo.conn, user, conv, payload.body, payload.attachments)
    except chat.ChatError as exc:
        _err(exc)
    chat.after_post(repo.conn, user, conv, msg)
    chat.publish(repo.conn, conv, "message", message=msg)
    return msg


def _own_conv(repo: Repository, user: auth.User, message_id: int) -> dict:
    msg = chat.get_message(repo.conn, message_id)
    if msg is None:
        raise HTTPException(404, "no existe ese mensaje")
    return _conv(repo, user, msg["conversation_id"])


@router.patch("/messages/{message_id}")
def edit_message(
    message_id: int, payload: EditIn, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)
):
    conv = _own_conv(repo, user, message_id)
    try:
        msg = chat.edit(repo.conn, user, message_id, payload.body)
    except chat.ChatError as exc:
        _err(exc)
    chat.publish(repo.conn, conv, "edited", message=msg)
    return msg


@router.get("/messages/{message_id}/edits")
def message_edits(message_id: int, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    """El rastro de un mensaje editado: el texto que tenía antes de cada edición."""
    _own_conv(repo, user, message_id)  # 404 si no puedo ver la conversación
    return {"edits": chat.edit_history(repo.conn, message_id)}


@router.delete("/messages/{message_id}")
def delete_message(message_id: int, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    conv = _own_conv(repo, user, message_id)
    try:
        msg = chat.delete(repo.conn, user, message_id)
    except chat.ChatError as exc:
        _err(exc)
    chat.publish(repo.conn, conv, "deleted", message=msg)
    return msg


@router.post("/conversations/{conversation_id}/read", status_code=204)
def mark_read(
    conversation_id: str, payload: ReadIn, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)
):
    conv = _conv(repo, user, conversation_id)
    chat.mark_read(repo.conn, user, conv, payload.last_id)
    return Response(status_code=204)


@router.post("/direct", status_code=201)
def open_direct(payload: PeopleIn, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    try:
        conv = chat.open_direct(repo.conn, user, payload.user_ids)
    except chat.ChatError as exc:
        _err(exc)
    return next(c for c in chat.listing(repo.conn, user) if c["id"] == conv["id"])


@router.post("/groups", status_code=201)
def create_group(payload: GroupIn, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    try:
        conv, evento = chat.create_group(repo.conn, user, payload.title, payload.user_ids)
    except chat.ChatError as exc:
        _err(exc)
    chat.publish(repo.conn, conv, "members")
    _publish_event(repo, conv, evento)
    return next(c for c in chat.listing(repo.conn, user) if c["id"] == conv["id"])


@router.patch("/conversations/{conversation_id}")
def rename_group(
    conversation_id: str, payload: RenameIn, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)
):
    conv = _conv(repo, user, conversation_id)
    try:
        evento = chat.rename_group(repo.conn, user, conv, payload.title)
    except chat.ChatError as exc:
        _err(exc)
    chat.publish(repo.conn, conv, "members")
    _publish_event(repo, conv, evento)
    return next(c for c in chat.listing(repo.conn, user) if c["id"] == conv["id"])


@router.post("/conversations/{conversation_id}/members")
def add_members(
    conversation_id: str, payload: PeopleIn, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)
):
    conv = _conv(repo, user, conversation_id)
    try:
        evento = chat.add_members(repo.conn, user, conv, payload.user_ids)
    except chat.ChatError as exc:
        _err(exc)
    chat.publish(repo.conn, conv, "members")
    _publish_event(repo, conv, evento)
    return next(c for c in chat.listing(repo.conn, user) if c["id"] == conv["id"])


@router.delete("/conversations/{conversation_id}/members/me", status_code=204)
def leave(conversation_id: str, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    conv = _conv(repo, user, conversation_id)
    try:
        evento = chat.leave(repo.conn, user, conv)
    except chat.ChatError as exc:
        _err(exc)
    chat.publish(repo.conn, conv, "members")
    _publish_event(repo, conv, evento)
    return Response(status_code=204)


@router.post("/conversations/{conversation_id}/files", status_code=201)
async def upload_file(
    conversation_id: str, file: UploadFile, repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    """Sube un adjunto para un mensaje (CH3). Va a `_attachments/`, como los de
    las páginas, y se sirve igual (`/files/...`): por bytes, nunca por nombre."""
    from app.routers.pages import MAX_FILE_BYTES

    _conv(repo, user, conversation_id)
    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, f"Máximo {MAX_FILE_BYTES // (1024 * 1024)} MB por archivo")
    if not data:
        raise HTTPException(400, "El archivo está vacío")
    nombre = os.path.basename(file.filename or "") or "archivo"
    ext = attachments.detect_image_format(data)
    if ext is not None:
        kind = "image"
    else:
        ext = attachments.detect_media_format(data)
        kind = attachments.MEDIA_KINDS[ext][0] if ext is not None else "file"
        if ext is None:
            ext = attachments.other_ext(nombre)
    rel = attachments.save_file(settings.vault_path, nombre, data, ext)
    return {"url": attachments.url_for(rel), "name": nombre, "size": len(data), "type": kind}


# ---------------------------------------------------------------- CH4


@router.get("/search")
def search(q: str = Query(min_length=1, max_length=200), repo: Repository = Depends(get_repo),
           user: auth.User = Depends(get_current_user)):
    """Buscar en el chat, solo en las conversaciones que veo."""
    return {"messages": chat.search(repo.conn, user, q)}


@router.post("/conversations/{conversation_id}/typing", status_code=204)
def typing(conversation_id: str, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    """«Está escribiendo…»: un aviso efímero, no se guarda."""
    conv = _conv(repo, user, conversation_id)
    chat.publish(repo.conn, conv, "typing", user_id=str(user.id), name=user.display_name)
    return Response(status_code=204)


class TaskIn(BaseModel):
    database_id: str
    title: Optional[str] = Field(default=None, max_length=300)
    # Responsable: sin mandarlo, quien escribió el mensaje; "" = nadie.
    assignee_id: Optional[str] = None
    # Fecha límite (AAAA-MM-DD), en la columna «Fin» de la base si la tiene.
    due: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


@router.post("/messages/{message_id}/task", status_code=201)
def message_to_task(
    message_id: int, payload: TaskIn, repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    """Convierte un mensaje en una fila de una base de Tareas o que cuenta para
    la carga: el texto como título (o el que se dé), quien lo escribió como
    responsable propuesto y, en el cuerpo, el mensaje y de dónde viene."""
    import uuid as uuid_mod

    from app import acl, schemas
    from app.routers.databases import create_row

    msg = chat.get_message(repo.conn, message_id)
    if msg is None or msg["deleted"] or msg.get("event"):
        raise HTTPException(404, "no existe ese mensaje")
    conv = _conv(repo, user, msg["conversation_id"])
    try:
        database_id = uuid_mod.UUID(payload.database_id)
    except ValueError:
        raise HTTPException(404, "no existe esa base") from None
    try:
        acl.authorize(repo.conn, user, str(database_id), "edit")
    except acl.NotVisible:
        raise HTTPException(404, "no existe esa base") from None
    except acl.Forbidden:
        raise HTTPException(403, "no puedes añadir filas a esa base") from None
    base = repo.get_database(database_id)
    if base is None:
        raise HTTPException(404, "no existe esa base")
    propiedades: dict[str, Any] = {}
    kind = base.kind or {}
    roles = (kind.get("roles") or {}) if kind.get("type") in ("tasks", "workload") else {}
    clave = roles.get("assignee")
    responsable = msg["user_id"] if payload.assignee_id is None else (payload.assignee_id or None)
    if clave and responsable:
        if repo.conn.execute("SELECT 1 FROM users WHERE id = ?", (responsable,)).fetchone() is None:
            raise HTTPException(404, "no existe esa persona")
        prop = next((p for p in base.schema_def if p.get("key") == clave), {})
        multiple = prop.get("multiple") in (True, "true")
        propiedades[clave] = [responsable] if multiple else responsable
    if payload.due and roles.get("end"):
        propiedades[roles["end"]] = payload.due
    texto = msg["body"] or ""
    titulo = (payload.title or texto.split("\n")[0] or "Tarea del chat").strip()[:200]
    donde = (
        f"«{conv_title(repo, conv)}»" if conv["kind"] == "workspace"
        else f"el grupo «{conv.get('title') or 'sin nombre'}»" if conv["kind"] == "group"
        else "un chat privado"
    )
    autor = (msg.get("author") or {}).get("display_name") or "alguien"
    cuerpo = "\n".join(f"> {linea}" for linea in texto.split("\n")) + f"\n\nDesde el chat ({donde}), de {autor}."
    fila = create_row(
        database_id, schemas.RowCreate(title=titulo, properties=propiedades, body_markdown=cuerpo), repo, user,
    )
    chat.set_task(repo.conn, message_id, str(fila.id))
    actualizado = chat.get_message(repo.conn, message_id)
    chat.publish(repo.conn, conv, "edited", message=actualizado)
    return {"task_id": str(fila.id), "title": fila.title, "message": actualizado}


def conv_title(repo: Repository, conv: dict) -> str:
    fila = repo.conn.execute("SELECT name FROM workspaces WHERE id = ?", (conv.get("workspace_id"),)).fetchone()
    return fila["name"] if fila else "canal"
