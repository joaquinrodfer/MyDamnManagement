"""`POST /export`: el vault completo desde el índice (3.0, hito 1).

Ver `app.vault_export` para qué sale y de dónde. Dos formatos:

- `{"format": "zip"}` (por defecto): la respuesta ES el `.zip`, para
  descargarlo. Se construye en `EXPORT_DIR` y se borra al terminar de enviarlo.
- `{"format": "folder"}`: escribe el vault en `EXPORT_DIR/<fecha UTC>/` del
  servidor y devuelve los recuentos y la ruta. Es la forma de dejar una copia
  en el disco del servidor (un volumen de backup, por ejemplo).

Solo para administradores: el export lleva el contenido de todo el mundo (y
desde el hito 3 se salta los permisos por nota, igual que `is_admin`).
"""

import os
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app import auth
from app.config import settings
from app.dependencies import get_current_user, get_repo
from app.repository import Repository
from app.vault_export import ExportError, export_to_folder, export_to_zip

router = APIRouter(tags=["export"])


class ExportRequest(BaseModel):
    format: Literal["zip", "folder"] = "zip"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%fZ")


@router.post("/export")
def post_export(
    payload: ExportRequest | None = None,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    if not user.is_admin:
        raise HTTPException(403, "solo un administrador puede exportar el vault entero")
    fmt = (payload or ExportRequest()).format
    stamp = _stamp()
    try:
        if fmt == "folder":
            dest = os.path.join(settings.export_dir, stamp)
            stats = export_to_folder(repo.conn, settings.vault_path, dest)
            return stats.as_dict()
        dest = os.path.join(settings.export_dir, f"mdm-vault-{stamp}.zip")
        stats = export_to_zip(repo.conn, settings.vault_path, dest)
    except ExportError as exc:
        raise HTTPException(409, str(exc)) from exc
    return FileResponse(
        dest,
        media_type="application/zip",
        filename=os.path.basename(dest),
        headers={
            "X-Export-Pages": str(stats.pages),
            "X-Export-Files": str(stats.files),
            "X-Export-Unindexed": str(len(stats.unindexed)),
        },
        background=BackgroundTask(os.unlink, dest),
    )
