from datetime import datetime, timezone
from typing import Optional

from app import models, schemas


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Devuelve el timestamp con zona UTC explícita.

    En la BD las columnas `DateTime` guardan `datetime.utcnow()`: naive, pero
    en UTC (ver `app/models.py`). Pydantic/FastAPI lo serializan tal cual, sin
    offset ("2026-10-28T20:35:00"), y ECMAScript interpreta un ISO sin zona
    como hora *local* del navegador -- de ahí que el "hace X" del panel saliera
    desfasado 1-2 h. Por eso todos los serializadores pasan sus fechas por
    aquí: es el único sitio donde se decide el formato de los timestamps.

    Un naive ya es UTC: se le pone la zona, no se convierte (convertirlo como
    si fuera local doblaría el desfase). Uno con zona se normaliza a UTC.

    Pendiente (requiere migración): pasar las columnas a `timestamptz` es la
    solución definitiva; esto es el arreglo sin tocar el esquema.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def serialize_page(page: models.Page, content: Optional[models.PageContent] = None) -> schemas.PageRead:
    body = content.body_markdown if content else (page.content.body_markdown if page.content else None)
    return schemas.PageRead(
        id=page.id,
        workspace_id=page.workspace_id,
        title=page.title,
        type=page.type,
        parent_id=page.parent_id,
        icon=page.icon,
        description=page.description,
        created_by=page.created_by,
        header_image_path=page.header_image_path,
        created_at=as_utc(page.created_at),
        updated_at=as_utc(page.updated_at),
        body_markdown=body,
    )
