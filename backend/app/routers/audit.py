"""`GET /audit`: el registro de auditoría (plan H8). Con el permiso «Ver auditoría»."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app import audit, auth, grants
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
    if not grants.can(repo.conn, user, "view_audit"):
        raise HTTPException(403, "hace falta el permiso «Ver auditoría»")
    result = audit.query(
        repo.conn, action=action, actor_id=actor, workspace_id=workspace,
        since=since, until=until, limit=limit, offset=offset,
    )
    return {**result, "actions": audit.actions(repo.conn)}
