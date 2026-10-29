"""Dependencias de FastAPI: una conexión al índice por petición.

Sustituye a `get_db()` (sesiones de SQLAlchemy). La ruta no sabe de dónde sale
el repositorio; en P1.2 la misma firma puede devolver uno que lea los ficheros.
"""

from typing import Iterator

from app.config import settings
from app.index import open_index
from app.repository import Repository


def get_repo() -> Iterator[Repository]:
    repo = Repository(open_index(settings.index_path))
    try:
        yield repo
    finally:
        repo.close()
