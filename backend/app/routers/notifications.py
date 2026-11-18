"""Avisos de la persona (plan H6).

- `GET /notifications`: los suyos (`?unread=true`, `?limit=`), con el contador.
- `POST /notifications/read`: marcarlos leídos (`{"ids": [...]}` o `{"all": true}`).
- `GET`/`PUT /notifications/subscriptions/{page_id}`: cuánto ruido quiere de una
  página: `all`, `mentions_only` (por defecto) o `muted`.

Solo ve y toca los suyos. Las suscripciones no pasan por la guarda de permisos
de página (ponerse en silencio no es editarla): se comprueba aquí que la ve.
"""

from __future__ import annotations

import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app import acl, auth, notifications
from app.dependencies import get_current_user, get_repo
from app.repository import Repository

router = APIRouter(tags=["notifications"])


class ReadIn(BaseModel):
    ids: Optional[list[str]] = None
    all: bool = False


class SubscriptionIn(BaseModel):
    mode: Literal["all", "mentions_only", "muted"]


@router.get("/notifications")
def list_notifications(
    unread: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    return notifications.list_for(repo.conn, str(user.id), unread_only=unread, limit=limit)


@router.post("/notifications/read")
def read_notifications(
    payload: ReadIn,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    if not payload.all and payload.ids is None:
        raise HTTPException(400, "manda `ids` o `all: true`")
    n = notifications.mark_read(repo.conn, str(user.id), None if payload.all else payload.ids)
    return {"marked": n, "unread": notifications.unread_count(repo.conn, str(user.id))}


def _check_page(repo: Repository, user: auth.User, target_id: uuid.UUID) -> str:
    try:
        acl.authorize(repo.conn, user, target_id, "view")
    except acl.NotVisible:
        raise HTTPException(404, "Página no encontrada") from None
    if repo.conn.execute("SELECT 1 FROM pages WHERE id = ?", (str(target_id),)).fetchone() is None:
        raise HTTPException(404, "Página no encontrada")
    return str(target_id)


@router.get("/notifications/subscriptions/{target_id}")
def get_subscription(
    target_id: uuid.UUID,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    page = _check_page(repo, user, target_id)
    return {"page_id": page, "mode": notifications.get_mode(repo.conn, str(user.id), page)}


@router.put("/notifications/subscriptions/{target_id}")
def put_subscription(
    target_id: uuid.UUID,
    payload: SubscriptionIn,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    page = _check_page(repo, user, target_id)
    notifications.set_mode(repo.conn, str(user.id), page, payload.mode)
    return {"page_id": page, "mode": payload.mode}
