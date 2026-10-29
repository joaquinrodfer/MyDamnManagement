from typing import List

from fastapi import APIRouter, Depends, Query

from app import schemas
from app.dependencies import get_repo
from app.repository import Repository
from app.serializers import serialize_page

router = APIRouter(tags=["search"])


@router.get("/search", response_model=List[schemas.PageRead])
def search_pages(
    q: str = Query(..., min_length=1), repo: Repository = Depends(get_repo)
):
    """Búsqueda full-text (FTS5 sobre título + cuerpo Markdown).

    El índice usa `unicode61 remove_diacritics 2`: `reunion` encuentra
    `Reunión`, y cada término se busca también como prefijo (`reunion` alcanza
    `Reuniones`), que es lo que hacía el `plainto_tsquery('spanish', ...)` de
    Postgres con su lematizador.
    """
    return [serialize_page(p) for p in repo.search(q, limit=50)]
