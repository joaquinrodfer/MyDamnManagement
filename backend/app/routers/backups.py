"""Copias de seguridad (plan H0). Solo para administradores.

- `GET /backups`: configuración, copias programadas y si hay `BACKUP_DIR`.
- `PUT /backups/settings`: frecuencia (`off`/`daily`/`weekly`) y cuántas guardar.
- `POST /backups/run`: una copia programada ahora, en `BACKUP_DIR`.
- `POST /backups/download`: una copia que se descarga (no se guarda).
- `GET /backups/files/{name}`: descarga una copia guardada.

La copia lleva usuarios y contraseñas cifradas: por eso es de administración.
"""

from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app import auth, backup
from app import index as index_mod
from app.config import settings
from app.dependencies import get_current_user, get_repo
from app.repository import Repository

router = APIRouter(tags=["backups"])


def _admin(user: auth.User = Depends(get_current_user)) -> auth.User:
    if not user.is_admin:
        raise HTTPException(403, "solo un administrador gestiona las copias de seguridad")
    return user


class BackupSettings(BaseModel):
    frequency: Literal["off", "daily", "weekly"]
    keep: int = Field(default=backup.DEFAULT_KEEP, ge=1, le=backup.MAX_KEEP)


def _state(conn) -> dict:
    return {
        "configured": bool(settings.backup_dir),
        **backup.get_settings(conn),
        "files": backup.listing(),
    }


@router.get("/backups")
def get_backups(repo: Repository = Depends(get_repo), _: auth.User = Depends(_admin)):
    return _state(repo.conn)


@router.put("/backups/settings")
def put_settings(
    payload: BackupSettings,
    repo: Repository = Depends(get_repo),
    _: auth.User = Depends(_admin),
):
    if payload.frequency != "off" and not settings.backup_dir:
        raise HTTPException(409, "BACKUP_DIR no está configurado en el servidor: no hay dónde guardar")
    index_mod.set_meta(repo.conn, backup.META_FREQUENCY, payload.frequency)
    index_mod.set_meta(repo.conn, backup.META_KEEP, str(payload.keep))
    repo.conn.commit()
    if settings.backup_dir:
        backup.prune(payload.keep)
    return _state(repo.conn)


@router.post("/backups/run")
def run_now(repo: Repository = Depends(get_repo), _: auth.User = Depends(_admin)):
    try:
        made = backup.run_scheduled(repo.conn)
    except backup.BackupError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"made": made, **_state(repo.conn)}


@router.post("/backups/download")
def download(_: auth.User = Depends(_admin)):
    dest = os.path.join(settings.export_dir, backup.file_name())
    manifest = backup.make_backup(dest, kind="descarga")
    return FileResponse(
        dest,
        media_type="application/gzip",
        filename=os.path.basename(dest),
        headers={"X-Backup-Pages": str(manifest["pages"]), "X-Backup-Files": str(manifest["vault_files"])},
        background=BackgroundTask(os.unlink, dest),
    )


@router.get("/backups/files/{name}")
def get_file(name: str, _: auth.User = Depends(_admin)):
    # Solo un nombre de copia, nunca una ruta (ver `backup.FILE_RE`).
    if not backup.FILE_RE.fullmatch(name) or not settings.backup_dir:
        raise HTTPException(404, "no existe esa copia")
    path = os.path.join(settings.backup_dir, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "no existe esa copia")
    return FileResponse(path, media_type="application/gzip", filename=name)
