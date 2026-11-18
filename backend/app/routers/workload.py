"""Carga del equipo (plan H5): `GET /workload`.

Solo para el superadministrador y para quien tiene `can_view_global_workload`.
Abarca **todos** los espacios; los títulos de lo que quien pregunta no puede ver
salen enmascarados (ver `app.workload`).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app import acl, auth, workload
from app.due import server_today
from app.dependencies import get_current_user, get_repo
from app.repository import Repository

router = APIRouter(tags=["workload"])


def _allowed(user: auth.User) -> None:
    if not (user.is_admin or user.can_view_global_workload):
        raise HTTPException(403, "no tienes permiso para ver la carga del equipo")


class CapacityIn(BaseModel):
    # Horas por semana; `null` vuelve al valor por defecto (40).
    hours: Optional[float] = Field(default=None, gt=0, le=168)


@router.get("/workload")
def get_workload(
    weeks: int = Query(default=8, ge=1, le=workload.MAX_WEEKS),
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    _allowed(user)
    # La carga mira todos los espacios: sin el filtro del espacio activo.
    visible = acl.visible_ids(repo.conn, user)
    return workload.report(repo.conn, visible=visible, today=server_today(), weeks=weeks)


@router.put("/workload/capacity/{user_id}")
def put_capacity(
    user_id: str,
    payload: CapacityIn,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    if not user.is_admin:
        raise HTTPException(403, "solo un administrador cambia la capacidad semanal")
    if repo.conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise HTTPException(404, "no existe ese usuario")
    caps = workload.set_capacity(repo.conn, user_id, payload.hours)
    return {"user_id": user_id, "hours": caps.get(user_id, workload.DEFAULT_CAPACITY)}
