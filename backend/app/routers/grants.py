"""Permisos que se conceden (plan de roles, R2): `app.grants`.

- `GET /grants`: los permisos que existen y a quién se han dado. Solo el
  administrador de la instalación.
- `POST /grants` `{capability, subject_type, subject_id, workspace_id|null}`:
  concede (en todos los espacios con `workspace_id` null). Queda en la auditoría.
- `DELETE /grants/{id}`: retira. Queda en la auditoría.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app import audit, auth, grants
from app.dependencies import get_current_user, get_repo
from app.repository import Repository

router = APIRouter(tags=["grants"])


def _admin(user: auth.User = Depends(get_current_user)) -> auth.User:
    if not user.is_admin:
        raise HTTPException(403, "solo un administrador concede permisos")
    return user


class GrantIn(BaseModel):
    capability: str
    subject_type: str
    subject_id: str
    workspace_id: Optional[str] = None


def _catalogo() -> list[dict]:
    return [
        {"key": k, "label": label, "help": ayuda, "scoped": scoped}
        for k, label, ayuda, scoped in grants.CAPABILITIES
    ]


@router.get("/grants")
def list_grants(repo: Repository = Depends(get_repo), _: auth.User = Depends(_admin)):
    return {"capabilities": _catalogo(), "grants": grants.listing(repo.conn)}


@router.post("/grants", status_code=201)
def create_grant(payload: GrantIn, repo: Repository = Depends(get_repo), user: auth.User = Depends(_admin)):
    try:
        nuevo = grants.grant(
            repo.conn, payload.capability, payload.subject_type, payload.subject_id,
            payload.workspace_id, granted_by=str(user.id),
        )
    except grants.GrantError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    audit.record(
        repo.conn, user, "grant.added", workspace_id=payload.workspace_id,
        target_type=payload.subject_type, target_id=payload.subject_id, target_name=nuevo["subject_name"],
        metadata={"capability": payload.capability, "scope": payload.workspace_id or "all"},
    )
    return nuevo


@router.delete("/grants/{grant_id}", status_code=204)
def delete_grant(grant_id: str, repo: Repository = Depends(get_repo), user: auth.User = Depends(_admin)):
    previo = grants.get(repo.conn, grant_id)
    if previo is None:
        raise HTTPException(404, "no existe ese permiso")
    grants.revoke(repo.conn, grant_id)
    audit.record(
        repo.conn, user, "grant.removed", workspace_id=previo["workspace_id"],
        target_type=previo["subject_type"], target_id=previo["subject_id"], target_name=previo["subject_name"],
        metadata={"capability": previo["capability"], "scope": previo["workspace_id"] or "all"},
    )
    return Response(status_code=204)


@router.get("/users/{user_id}/access")
def user_access(user_id: str, repo: Repository = Depends(get_repo), _: auth.User = Depends(_admin)):
    """La ficha de permisos de una persona (solo el administrador): sus espacios
    con su rol (directo o por un grupo) y cada permiso con sus concesiones."""
    from app import workspaces as ws_mod

    conn = repo.conn
    persona = conn.execute("SELECT id, display_name, username, is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    if persona is None:
        raise HTTPException(404, "no existe esa persona")
    espacios = []
    for r in conn.execute(
        "SELECT w.id, w.name, w.settings, m.role, m.subject_type, g.name AS group_name"
        " FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id"
        " LEFT JOIN groups g ON m.subject_type = 'group' AND g.id = m.subject_id"
        " WHERE (m.subject_type = 'user' AND m.subject_id = ?)"
        " OR (m.subject_type = 'group' AND m.subject_id IN (SELECT group_id FROM group_members WHERE user_id = ?))"
        " ORDER BY w.name COLLATE NOCASE",
        (user_id, user_id),
    ):
        if ws_mod.is_personal(r):
            continue
        espacios.append({
            "id": r["id"], "name": r["name"], "role": r["role"],
            "via": "group" if r["subject_type"] == "group" else "user", "group_name": r["group_name"],
        })
    return {
        "user": {"id": persona["id"], "display_name": persona["display_name"], "username": persona["username"],
                 "is_admin": bool(persona["is_admin"])},
        "workspaces": espacios,
        "capabilities": grants.user_access(conn, user_id),
    }
