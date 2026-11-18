"""`GET /audit`: el registro de auditoría (plan H8). Solo superadministrador."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app import audit, auth
from app.dependencies import get_current_user, get_repo
from app.repository import Repository

router = APIRouter(tags=["audit"])


@router.get("/audit")
def get_audit(
    action: Optional[str] = None,
    actor: Optional[str] = None,
    workspace: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=audit.MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    if not user.is_admin:
        raise HTTPException(403, "solo un administrador puede ver la auditoría")
    result = audit.query(
        repo.conn, action=action, actor_id=actor, workspace_id=workspace,
        since=since, until=until, limit=limit, offset=offset,
    )
    return {**result, "actions": audit.actions(repo.conn)}
