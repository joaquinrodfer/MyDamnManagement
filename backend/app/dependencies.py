"""Dependencias de FastAPI: una conexión al índice por petición.

Sustituye a `get_db()` (sesiones de SQLAlchemy). La ruta no sabe de dónde sale
el repositorio; en P1.2 la misma firma puede devolver uno que lea los ficheros.
"""

from typing import Iterator

from fastapi import HTTPException, Request

from app import auth
from app.config import settings
from app.index import open_index
from app.repository import Repository


def get_repo() -> Iterator[Repository]:
    repo = Repository(open_index(settings.index_path))
    try:
        yield repo
    finally:
        repo.close()


def get_current_user(request: Request) -> auth.User:
    """El usuario de la sesión, tal como lo dejó el middleware de `app.main`.

    No vuelve a mirar la cookie: el middleware ya la resolvió (y ya empujó la
    caducidad deslizante) antes de llegar aquí. Si no hay usuario es porque la
    ruta se ha llamado fuera del middleware (un test que invoca la función a
    pelo): se responde igual que la puerta, 401 con el mismo texto.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "no autenticado")
    return user
