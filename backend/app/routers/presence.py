"""Presencia (plan H7): latido, listado y estado manual. Ver `app.presence`."""

from __future__ import annotations

import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app import auth, presence
from app.dependencies import get_current_user, get_repo
from app.events import bus
from app.repository import Repository

router = APIRouter(tags=["presence"])


class Heartbeat(BaseModel):
    page_id: Optional[uuid.UUID] = None


class StatusIn(BaseModel):
    status: Literal["available", "focus", "meeting", "custom"]
    text: str = ""


@router.post("/presence/heartbeat", status_code=204)
def heartbeat(payload: Heartbeat, user: auth.User = Depends(get_current_user)):
    changed = presence.beat(str(user.id), str(payload.page_id) if payload.page_id else None)
    if changed:
        bus.publish({"type": "presence", "action": "changed", "user_id": str(user.id)})
    return Response(status_code=204)


@router.post("/presence/leave", status_code=204)
def leave(user: auth.User = Depends(get_current_user)):
    presence.leave(str(user.id))
    bus.publish({"type": "presence", "action": "changed", "user_id": str(user.id)})
    return Response(status_code=204)


@router.get("/presence")
def get_presence(repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    online = presence.listing(repo.conn, user)
    return {
        "me": presence.get_status(repo.conn, str(user.id)),
        "online": online,
        "workspaces": presence.by_workspace(repo.conn, user, online),
    }


@router.put("/presence/status")
def put_status(
    payload: StatusIn,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    try:
        mine = presence.set_status(repo.conn, str(user.id), payload.status, payload.text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    bus.publish({"type": "presence", "action": "status", "user_id": str(user.id)})
    return mine
