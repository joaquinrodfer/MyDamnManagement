"""Carga del equipo (plan H5): `GET /workload`.

Para quien tiene el permiso «Ver carga» (`app.grants`): en todos los espacios,
a todas las personas; en unos, a los miembros de esos espacios (plan de roles,
R4). `?workspace=` deja solo lo de un espacio, con la capacidad de cada persona
por su dedicación a él.
Abarca **todos** los espacios; los títulos de lo que quien pregunta no puede ver
salen enmascarados (ver `app.workload`).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app import acl, auth, grants, workload
from app import workspaces as ws_mod
from app.due import server_today
from app.dependencies import get_current_user, get_repo
from app.repository import Repository

router = APIRouter(tags=["workload"])


def _allowed(repo: Repository, user: auth.User) -> Optional[set[str]]:
    """Dónde ve la carga (`None` = en todos los espacios); 403 si en ninguno."""
    alcance = grants.scopes(repo.conn, user, "view_workload")
    if alcance is not None and not alcance:
        raise HTTPException(403, "ver la carga del equipo pide el permiso «Ver carga»")
    return alcance


class CapacityIn(BaseModel):
    # Horas por semana; `null` vuelve al valor por defecto (40).
    hours: Optional[float] = Field(default=None, gt=0, le=168)


@router.get("/workload")
def get_workload(
    weeks: int = Query(default=8, ge=1, le=workload.MAX_WEEKS),
    workspace: Optional[str] = Query(default=None),
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    alcance = _allowed(repo, user)
    if workspace is not None and alcance is not None and workspace not in alcance:
        raise HTTPException(404, "no existe ese espacio")
    # Con «Ver carga» en unos espacios: sus miembros, con todo lo suyo (de
    # cualquier espacio; lo que no ve sale enmascarado). En todos: todos.
    personas = None if alcance is None else grants.people_in(repo.conn, alcance)
    # La carga mira todos los espacios: sin el filtro del espacio activo.
    visible = acl.visible_ids(repo.conn, user)
    informe = workload.report(
        repo.conn, visible=visible, today=server_today(), weeks=weeks,
        people=personas, workspace_id=workspace,
    )
    filas = repo.conn.execute("SELECT id, name, settings FROM workspaces ORDER BY name COLLATE NOCASE").fetchall()
    informe["workspaces"] = [
        {"id": r["id"], "name": r["name"]}
        for r in filas
        if not ws_mod.is_personal(r) and (alcance is None or r["id"] in alcance)
    ]
    informe["workspace_id"] = workspace
    # A quién puede abrir la «Jornada» quien mira (R3): el botón solo sale ahí.
    alcance = grants.scopes(repo.conn, user, "manage_people")
    gestionables = None if alcance is None else grants.people_in(repo.conn, alcance)
    for p in informe["people"]:
        p["manageable"] = gestionables is None or p["id"] in gestionables
    return informe


@router.put("/workload/capacity/{user_id}")
def put_capacity(
    user_id: str,
    payload: CapacityIn,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    if not grants.can_for_person(repo.conn, user, "manage_people", user_id):
        raise HTTPException(403, "cambiar la capacidad pide el permiso «Gestionar personas»")
    if repo.conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise HTTPException(404, "no existe ese usuario")
    caps = workload.set_capacity(repo.conn, user_id, payload.hours)
    return {"user_id": user_id, "hours": caps.get(user_id, workload.DEFAULT_CAPACITY)}
