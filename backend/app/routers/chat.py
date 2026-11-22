"""Chat del equipo (`docs/PLAN-CHAT.md`), con los permisos de `app.chat`.

- `GET /chat/conversations`: las mías (canales de mis espacios y directos), con no leídos.
- `GET /chat/unread`: el total sin leer (para el contador de la pestaña).
- `GET /chat/conversations/{id}/messages?before=&limit=`: por páginas, de viejo a nuevo.
- `POST /chat/conversations/{id}/messages` `{body, attachments}`.
- `PATCH /chat/messages/{id}` `{body}` y `DELETE /chat/messages/{id}`: solo su autor.
- `POST /chat/conversations/{id}/read` `{last_id}`.
- `POST /chat/direct` `{user_ids}`: abre (o reutiliza) un directo.
- `POST /chat/conversations/{id}/members` `{user_ids}`, `DELETE /chat/conversations/{id}/members/me`.
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


@router.post("/conversations/{conversation_id}/members")
def add_members(
    conversation_id: str, payload: PeopleIn, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)
):
    conv = _conv(repo, user, conversation_id)
    try:
        chat.add_members(repo.conn, user, conv, payload.user_ids)
    except chat.ChatError as exc:
        _err(exc)
    chat.publish(repo.conn, conv, "members")
    return next(c for c in chat.listing(repo.conn, user) if c["id"] == conv["id"])


@router.delete("/conversations/{conversation_id}/members/me", status_code=204)
def leave(conversation_id: str, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    conv = _conv(repo, user, conversation_id)
    try:
        chat.leave(repo.conn, user, conv)
    except chat.ChatError as exc:
        _err(exc)
    chat.publish(repo.conn, conv, "members")
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
