"""`GET/PUT /acl/{resource_type}/{resource_id}`: quién puede qué (3.0, hito 3).

- `GET` (hace falta ver el recurso): si hereda o declara, de quién hereda, las
  entradas que mandan (las suyas o las de la página de la que hereda, con el
  nombre de cada usuario) y el nivel de quien pregunta.
- `PUT` (hace falta ser `owner`): `{"inherit": false, "entries": [...]}`
  declara exactamente esas entradas (y corta la herencia); `{"inherit": true}`
  deja de declarar y vuelve a heredar de su madre.

Dos reglas para no dejar una página sin dueño: una página de la raíz no puede
«heredar» (no tiene de quién), y quien declara (si no es administrador) tiene
que seguir siendo `owner` de lo que declara.

Los permisos los comprueba la guarda (`app.guard`), no esta ruta.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import acl, auth
from app.dependencies import get_current_user, get_repo
from app import audit
from app import workspaces as ws_mod
from app.repository import Repository

router = APIRouter(prefix="/acl", tags=["acl"])

ResourceType = Literal["page", "database"]


class AclEntryIn(BaseModel):
    subject_type: Literal["user", "workspace", "group"]
    subject_id: str
    level: Literal["view", "edit", "owner"]


class AclPut(BaseModel):
    inherit: bool = False
    entries: list[AclEntryIn] = []


def _resource_or_404(repo: Repository, resource_type: str, resource_id: uuid.UUID):
    row = repo.conn.execute(
        "SELECT id, type, title, parent_id, acl_source, workspace_id FROM pages WHERE id = ?",
        (str(resource_id),),
    ).fetchone()
    expected = "database" if resource_type == "database" else "note"
    if row is None or row["type"] != expected:
        raise HTTPException(404, "No encontrada")
    return row


def _read(repo: Repository, user: auth.User, resource_type: str, resource_id: uuid.UUID) -> dict:
    row = _resource_or_404(repo, resource_type, resource_id)
    source = row["acl_source"]
    declared = acl.declared(repo.conn, resource_id)
    source_title = None
    if source and source != row["id"]:
        found = repo.conn.execute("SELECT title FROM pages WHERE id = ?", (source,)).fetchone()
        source_title = found["title"] if found else None
    users = {
        r["id"]: (r["display_name"], r["color"], auth.avatar_of(r))
        for r in repo.conn.execute("SELECT id, username, display_name, color, avatar FROM users")
    }
    # Plan H2: quien no es miembro del espacio de la página es invitado.
    members = {
        m["subject_id"]
        for m in ws_mod.members(repo.conn, row["workspace_id"])
        if m["subject_type"] == "user"
    }
    groups_of_ws = {
        m["subject_id"]
        for m in ws_mod.members(repo.conn, row["workspace_id"])
        if m["subject_type"] == "group"
    }
    if groups_of_ws:
        marks = ",".join("?" * len(groups_of_ws))
        members |= {
            r[0]
            for r in repo.conn.execute(
                f"SELECT user_id FROM group_members WHERE group_id IN ({marks})", tuple(groups_of_ws)
            )
        }
    entries = []
    for e in acl.entries(repo.conn, source) if source else []:
        name, color, avatar = users.get(e.subject_id, (None, None, None))
        if e.subject_type == "workspace":
            name = "Todo el espacio"
        elif e.subject_type == "group":
            found = repo.conn.execute("SELECT name FROM groups WHERE id = ?", (e.subject_id,)).fetchone()
            name = found["name"] if found else None
        entries.append({
            "subject_type": e.subject_type,
            "subject_id": e.subject_id,
            "name": name,
            "color": color,
            "avatar": avatar,
            "level": e.level,
            "guest": e.subject_type == "user" and e.subject_id not in members,
        })
    # Propietarios primero, luego quien edita y quien ve; el espacio, delante
    # de su nivel; dentro de cada nivel, por nombre.
    entries.sort(key=lambda e: (
        -acl.LEVELS.get(e["level"], 0), e["subject_type"] != "workspace", (e["name"] or "").casefold(),
    ))
    return {
        "resource_type": resource_type,
        "resource_id": str(resource_id),
        "inherit": not declared,
        "is_root": row["parent_id"] is None,
        "source_id": source,
        "source_title": source_title,
        "entries": entries,
        "workspace_id": row["workspace_id"],
        "your_level": acl.level_for(repo.conn, user, resource_id),
    }


@router.get("/{resource_type}/{resource_id}")
def get_acl(
    resource_type: ResourceType,
    resource_id: uuid.UUID,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    return _read(repo, user, resource_type, resource_id)


@router.put("/{resource_type}/{resource_id}")
def put_acl(
    resource_type: ResourceType,
    resource_id: uuid.UUID,
    payload: AclPut,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    row = _resource_or_404(repo, resource_type, resource_id)
    if payload.inherit:
        if row["parent_id"] is None:
            raise HTTPException(
                400, "una página de la raíz no tiene de quién heredar: declara sus permisos"
            )
    else:
        known = {r[0] for r in repo.conn.execute("SELECT id FROM users")}
        seen: set[tuple[str, str]] = set()
        for e in payload.entries:
            if e.subject_type == "user" and e.subject_id not in known:
                raise HTTPException(400, f"no existe el usuario {e.subject_id}")
            if e.subject_type == "workspace" and e.subject_id != row["workspace_id"]:
                raise HTTPException(400, f"el espacio de esta página es otro: {e.subject_id}")
            if e.subject_type == "group" and not repo.conn.execute(
                "SELECT 1 FROM groups WHERE id = ?", (e.subject_id,)
            ).fetchone():
                raise HTTPException(400, f"no existe el grupo {e.subject_id}")
            if (e.subject_type, e.subject_id) in seen:
                raise HTTPException(400, "cada usuario (o el espacio) va una sola vez")
            seen.add((e.subject_type, e.subject_id))
        if not user.is_admin and not any(
            e.subject_type == "user" and e.subject_id == str(user.id) and e.level == "owner"
            for e in payload.entries
        ):
            raise HTTPException(
                400, "no puedes quitarte la propiedad: deja tu entrada como propietario"
            )
    with repo._tx():
        if payload.inherit:
            acl.clear(repo.conn, resource_id)
        else:
            acl.set_entries(
                repo.conn,
                resource_type,
                resource_id,
                [acl.Entry(e.subject_type, e.subject_id, e.level) for e in payload.entries],
            )
        acl.refresh_sources(repo.conn)
    audit.record(
        repo.conn, user, "permission.changed", workspace_id=row["workspace_id"],
        target_type=resource_type, target_id=resource_id, target_name=row["title"],
        metadata={"inherit": payload.inherit,
                  "entries": [f"{e.subject_type}:{e.subject_id}={e.level}" for e in payload.entries]},
    )
    return _read(repo, user, resource_type, resource_id)
