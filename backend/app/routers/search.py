from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db
from app.serializers import serialize_page

router = APIRouter(tags=["search"])

TS_CONFIG = "spanish"


@router.get("/search", response_model=List[schemas.PageRead])
def search_pages(q: str = Query(..., min_length=1), db: Session = Depends(get_db)):
    """Búsqueda full-text (Postgres tsvector) sobre título + cuerpo Markdown."""
    tsquery = func.plainto_tsquery(TS_CONFIG, q)
    document = func.to_tsvector(
        TS_CONFIG,
        models.Page.title + " " + func.coalesce(models.PageContent.body_markdown, ""),
    )
    rank = func.ts_rank(document, tsquery)

    rows = (
        db.query(models.Page, models.PageContent, rank.label("rank"))
        .outerjoin(models.PageContent, models.PageContent.page_id == models.Page.id)
        .filter(document.op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(50)
        .all()
    )
    return [serialize_page(page, content) for page, content, _rank in rows]
