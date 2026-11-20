"""Páginas fijadas («Fijadas», arriba del panel izquierdo).

Cada persona fija las páginas que más usa; es una preferencia suya, no algo de
la página: vive en `page_pins` (índice v21), no en el `.md`, y nadie más la ve.

- `GET /pins`: mis fijadas, en mi orden, solo las que sigo pudiendo ver (si me
  quitan el permiso, desaparece de la lista sin borrar la fila).
- `PUT /pins/{page_id}`: fijar (al final). Fijar algo que no veo es 404.
- `DELETE /pins/{page_id}`: quitar.
- `PUT /pins`: el orden nuevo, `{"ids": [...]}` (lo que no venga se queda detrás).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app import acl, auth
from app.dependencies import get_current_user, get_repo
from app.repository import Repository

router = APIRouter(tags=["pins"])

MAX_PINS = 50


class PinOrder(BaseModel):
    ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_PINS)


def _mine(repo: Repository, user: auth.User) -> list[dict]:
    visible = acl.visible_ids(repo.conn, user)
    rows = repo.conn.execute(
        "SELECT p.id, p.title, p.icon, p.type, p.workspace_id, d.id AS database_id"
        " FROM page_pins x JOIN pages p ON p.id = x.page_id"
        " LEFT JOIN databases d ON d.page_id = p.id"
        " WHERE x.user_id = ? ORDER BY x.position, p.title COLLATE NOCASE",
        (str(user.id),),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"] or "Sin título",
            "icon": r["icon"],
            "type": r["type"],
            "workspace_id": r["workspace_id"],
            "database_id": r["database_id"],
        }
        for r in rows
        if visible is None or r["id"] in visible
    ]


@router.get("/pins")
def list_pins(repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    return _mine(repo, user)


@router.put("/pins/{page_id}")
def pin(page_id: uuid.UUID, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    visible = acl.visible_ids(repo.conn, user)
    existe = repo.conn.execute("SELECT 1 FROM pages WHERE id = ?", (str(page_id),)).fetchone()
    if existe is None or (visible is not None and str(page_id) not in visible):
        raise HTTPException(404, "Página no encontrada")
    total = repo.conn.execute("SELECT count(*) FROM page_pins WHERE user_id = ?", (str(user.id),)).fetchone()[0]
    ya = repo.conn.execute(
        "SELECT 1 FROM page_pins WHERE user_id = ? AND page_id = ?", (str(user.id), str(page_id))
    ).fetchone()
    if ya is None:
        if total >= MAX_PINS:
            raise HTTPException(400, f"Como mucho {MAX_PINS} páginas fijadas")
        siguiente = repo.conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM page_pins WHERE user_id = ?", (str(user.id),)
        ).fetchone()[0]
        repo.conn.execute(
            "INSERT INTO page_pins (user_id, page_id, position) VALUES (?, ?, ?)",
            (str(user.id), str(page_id), siguiente),
        )
    return _mine(repo, user)


@router.delete("/pins/{page_id}")
def unpin(page_id: uuid.UUID, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    repo.conn.execute(
        "DELETE FROM page_pins WHERE user_id = ? AND page_id = ?", (str(user.id), str(page_id))
    )
    return _mine(repo, user)


@router.put("/pins")
def reorder(payload: PinOrder, repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    orden = [str(i) for i in payload.ids]
    actuales = [
        r[0]
        for r in repo.conn.execute(
            "SELECT page_id FROM page_pins WHERE user_id = ? ORDER BY position", (str(user.id),)
        )
    ]
    nuevo = [i for i in orden if i in actuales] + [i for i in actuales if i not in orden]
    for pos, pid in enumerate(nuevo):
        repo.conn.execute(
            "UPDATE page_pins SET position = ? WHERE user_id = ? AND page_id = ?", (pos, str(user.id), pid)
        )
    return _mine(repo, user)
