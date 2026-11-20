"""Espacios de trabajo y grupos (plan H1).

- `GET /workspaces`: los espacios del usuario (todos, si es superadministrador),
  con su rol y cuántas páginas tienen.
- `POST /workspaces`, `DELETE /workspaces/{id}`: solo superadministrador.
- `PATCH /workspaces/{id}`: nombre, icono y ajustes; administrador del espacio.
- `GET /workspaces/{id}/members`, `PUT`/`DELETE /workspaces/{id}/members/...`:
  administrador del espacio.
- `/groups`: crear, renombrar, borrar y fijar sus miembros; superadministrador
  (listar, cualquiera que sea miembro de algún espacio: para elegirlos).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import audit, auth, webhooks
from app import workspaces as ws
from app.dependencies import get_current_user
from app.routers.auth import get_conn

router = APIRouter(tags=["workspaces"])


def _raise(exc: ws.WorkspaceError):
    raise HTTPException(exc.status, str(exc)) from None


def _superadmin(user: auth.User = Depends(get_current_user)) -> auth.User:
    if not user.is_admin:
        raise HTTPException(403, "solo un administrador puede hacer esto")
    return user


def _manager(conn: sqlite3.Connection, user: auth.User, workspace_id: str) -> None:
    """Administrador del espacio (o superadministrador); 404 si no lo ve."""
    role = ws.role_of(conn, user, workspace_id)
    if ws.get(conn, workspace_id) is None or role is None:
        raise HTTPException(404, "no existe ese espacio")
    if role != "admin":
        raise HTTPException(403, "solo un administrador del espacio puede hacer esto")


class WorkspaceCreate(BaseModel):
    name: str
    icon: Optional[str] = None


class WorkspaceUpdate(BaseModel):
    name: Optional[str] = None
    icon: Optional[str] = None
    settings: Optional[dict[str, Any]] = None


class MemberPut(BaseModel):
    role: Literal["admin", "editor", "viewer"]


class GroupIn(BaseModel):
    name: str


class GroupMembers(BaseModel):
    user_ids: list[str]


# ---------------------------------------------------------------- espacios


@router.get("/workspaces")
def list_workspaces(user: auth.User = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return ws.list_for(conn, user)


@router.post("/workspaces/personal")
def ensure_personal_workspace(user: auth.User = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Crea «Espacio de <nombre>» si esta persona aún no lo tiene (idempotente)."""
    ws.ensure_personal(conn, user)
    return ws.list_for(conn, user)


@router.post("/workspaces", status_code=201)
def create_workspace(
    payload: WorkspaceCreate,
    user: auth.User = Depends(_superadmin),
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        created = ws.create(conn, payload.name, payload.icon)
        # Quien lo crea entra como administrador del espacio nuevo.
        ws.set_member(conn, created["id"], "user", str(user.id), "admin")
    except ws.WorkspaceError as exc:
        _raise(exc)
    audit.record(conn, user, "workspace.created", workspace_id=created["id"],
                 target_type="workspace", target_id=created["id"], target_name=created["name"])
    return ws.get_dict(conn, user, created["id"])


@router.patch("/workspaces/{workspace_id}")
def update_workspace(
    workspace_id: str,
    payload: WorkspaceUpdate,
    user: auth.User = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    _manager(conn, user, workspace_id)
    try:
        ws.update(conn, workspace_id, name=payload.name, icon=payload.icon, settings=payload.settings)
    except ws.WorkspaceError as exc:
        _raise(exc)
    audit.record(conn, user, "workspace.updated", workspace_id=workspace_id, target_type="workspace",
                 target_id=workspace_id, target_name=payload.name,
                 metadata={k: v for k, v in payload.model_dump(exclude_none=True).items()})
    return ws.get_dict(conn, user, workspace_id)


@router.delete("/workspaces/{workspace_id}", status_code=204)
def delete_workspace(
    workspace_id: str, user: auth.User = Depends(_superadmin), conn: sqlite3.Connection = Depends(get_conn)
):
    nombre = (ws.get(conn, workspace_id) or {"name": None})["name"]
    try:
        ws.delete(conn, workspace_id)
    except ws.WorkspaceError as exc:
        _raise(exc)
    audit.record(conn, user, "workspace.deleted", workspace_id=workspace_id,
                 target_type="workspace", target_id=workspace_id, target_name=nombre)


# ----------------------------------------------------------------- webhook


class WebhookIn(BaseModel):
    url: str
    enabled: bool = True
    events: list[str] = list(webhooks.EVENTS)


def _webhook_view(conn: sqlite3.Connection, workspace_id: str, secret: Optional[str] = None) -> dict:
    return {
        "config": webhooks.get_config(conn, workspace_id),
        "events": list(webhooks.EVENTS),
        "deliveries": webhooks.recent(conn, workspace_id),
        # El secreto solo sale cuando se crea o se regenera.
        **({"secret": secret} if secret else {}),
    }


@router.get("/workspaces/{workspace_id}/webhook")
def get_webhook(
    workspace_id: str, user: auth.User = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_conn)
):
    _manager(conn, user, workspace_id)
    return _webhook_view(conn, workspace_id)


@router.put("/workspaces/{workspace_id}/webhook")
def put_webhook(
    workspace_id: str,
    payload: WebhookIn,
    user: auth.User = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    _manager(conn, user, workspace_id)
    try:
        _cfg, secret = webhooks.set_config(
            conn, workspace_id, url=payload.url, enabled=payload.enabled, events=payload.events
        )
    except webhooks.WebhookError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    audit.record(conn, user, "webhook.configured", workspace_id=workspace_id, target_type="workspace",
                 target_id=workspace_id, metadata={"enabled": payload.enabled, "events": payload.events})
    return _webhook_view(conn, workspace_id, secret)


@router.post("/workspaces/{workspace_id}/webhook/secret")
def regenerate_webhook_secret(
    workspace_id: str, user: auth.User = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_conn)
):
    _manager(conn, user, workspace_id)
    try:
        secret = webhooks.regenerate_secret(conn, workspace_id)
    except webhooks.WebhookError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    audit.record(conn, user, "webhook.secret_regenerated", workspace_id=workspace_id,
                 target_type="workspace", target_id=workspace_id)
    return _webhook_view(conn, workspace_id, secret)


@router.delete("/workspaces/{workspace_id}/webhook", status_code=204)
def delete_webhook(
    workspace_id: str, user: auth.User = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_conn)
):
    _manager(conn, user, workspace_id)
    webhooks.delete_config(conn, workspace_id)
    audit.record(conn, user, "webhook.deleted", workspace_id=workspace_id,
                 target_type="workspace", target_id=workspace_id)


@router.post("/workspaces/{workspace_id}/webhook/test")
def test_webhook(
    workspace_id: str, user: auth.User = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_conn)
):
    """Manda un `ping` ahora mismo y dice qué contestó el otro lado."""
    _manager(conn, user, workspace_id)
    cfg = conn.execute("SELECT url, secret FROM workspace_webhooks WHERE workspace_id = ?", (workspace_id,)).fetchone()
    if cfg is None:
        raise HTTPException(404, "este espacio no tiene webhook")
    ws_get = ws.get(conn, workspace_id)
    import json as _json
    import time as _time

    body = _json.dumps(webhooks.build_payload(
        "ping", {"id": workspace_id, "name": ws_get["name"]}, "Prueba de Notemil: el webhook funciona.", {}
    ), ensure_ascii=False).encode()
    ts = str(int(_time.time()))
    cabeceras = {
        "Content-Type": "application/json", "User-Agent": "Notemil-Webhook/1", "X-Notemil-Event": "ping",
        "X-Notemil-Timestamp": ts, "X-Notemil-Signature": webhooks._sign(cfg["secret"], ts, body),
    }
    cabeceras.update(webhooks.cabeceras_antiguas(cabeceras))
    try:
        status = webhooks.post(cfg["url"], body, cabeceras)
    except webhooks.WebhookError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": 200 <= status < 300, "status": status}


# ---------------------------------------------------------------- miembros


@router.get("/workspaces/{workspace_id}/members")
def list_members(
    workspace_id: str,
    user: auth.User = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if ws.role_of(conn, user, workspace_id) is None or ws.get(conn, workspace_id) is None:
        raise HTTPException(404, "no existe ese espacio")
    return ws.members(conn, workspace_id)


@router.put("/workspaces/{workspace_id}/members/{subject_type}/{subject_id}")
def put_member(
    workspace_id: str,
    subject_type: Literal["user", "group"],
    subject_id: str,
    payload: MemberPut,
    user: auth.User = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    _manager(conn, user, workspace_id)
    try:
        ws.set_member(conn, workspace_id, subject_type, subject_id, payload.role)
    except ws.WorkspaceError as exc:
        _raise(exc)
    audit.record(conn, user, "workspace.member_set", workspace_id=workspace_id, target_type=subject_type,
                 target_id=subject_id, metadata={"role": payload.role})
    return ws.members(conn, workspace_id)


@router.delete("/workspaces/{workspace_id}/members/{subject_type}/{subject_id}")
def delete_member(
    workspace_id: str,
    subject_type: Literal["user", "group"],
    subject_id: str,
    user: auth.User = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    _manager(conn, user, workspace_id)
    ws.remove_member(conn, workspace_id, subject_type, subject_id)
    audit.record(conn, user, "workspace.member_removed", workspace_id=workspace_id,
                 target_type=subject_type, target_id=subject_id)
    return ws.members(conn, workspace_id)


# ------------------------------------------------------------------ grupos


@router.get("/groups")
def list_groups(user: auth.User = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_conn)):
    # Todo el que está en algún espacio puede ver los grupos (para elegirlos al
    # compartir); crear y cambiarlos es del superadministrador.
    return ws.list_groups(conn)


@router.post("/groups", status_code=201)
def create_group(payload: GroupIn, user: auth.User = Depends(_superadmin), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        creado = ws.create_group(conn, payload.name)
    except ws.WorkspaceError as exc:
        _raise(exc)
    audit.record(conn, user, "group.created", target_type="group", target_id=creado["id"], target_name=creado["name"])
    return creado


@router.patch("/groups/{group_id}")
def rename_group(group_id: str, payload: GroupIn, _: auth.User = Depends(_superadmin), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        ws.rename_group(conn, group_id, payload.name)
    except ws.WorkspaceError as exc:
        _raise(exc)
    return next(g for g in ws.list_groups(conn) if g["id"] == group_id)


@router.put("/groups/{group_id}/members")
def put_group_members(group_id: str, payload: GroupMembers, user: auth.User = Depends(_superadmin), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        ws.set_group_members(conn, group_id, payload.user_ids)
    except ws.WorkspaceError as exc:
        _raise(exc)
    audit.record(conn, user, "group.members_changed", target_type="group", target_id=group_id,
                 metadata={"members": len(payload.user_ids)})
    return next(g for g in ws.list_groups(conn) if g["id"] == group_id)


@router.delete("/groups/{group_id}", status_code=204)
def delete_group(group_id: str, user: auth.User = Depends(_superadmin), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        ws.delete_group(conn, group_id)
    except ws.WorkspaceError as exc:
        _raise(exc)
    audit.record(conn, user, "group.deleted", target_type="group", target_id=group_id)
