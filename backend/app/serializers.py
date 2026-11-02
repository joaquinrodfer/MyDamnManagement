from datetime import datetime, timezone
from typing import Mapping, Optional

from app import schemas
from app.repository import Page


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Devuelve el timestamp con zona UTC explícita.

    En el índice las fechas se guardan en ISO 8601 con offset (`+00:00`), así
    que normalmente ya vienen con zona; el naive se sigue tratando como UTC
    (nunca como hora local: convertirlo así doblaría el desfase) para no romper
    datos migrados de Postgres, que eran `timestamp without time zone` en UTC.

    Es el único sitio donde se decide el formato de los timestamps de la API.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _color_of(
    display_name: Optional[str], author_colors: Optional[Mapping[str, str]]
) -> Optional[str]:
    if not display_name or not author_colors:
        return None
    return author_colors.get(display_name)


def serialize_page(
    page: Page, author_colors: Optional[Mapping[str, str]] = None
) -> schemas.PageRead:
    """Página -> JSON de la API.

    `author_colors` es el mapa `display_name -> color` de los usuarios (ver
    `Repository.author_colors`): con él la página dice el color de quien la
    creó, aunque el cliente no conozca a ese usuario. Sin mapa (o sin
    coincidencia), `created_by_color` es `null` y la interfaz pinta el gris de
    las notas antiguas.
    """
    return schemas.PageRead(
        id=page.id,
        workspace_id=page.workspace_id,
        title=page.title,
        type=page.type,
        parent_id=page.parent_id,
        icon=page.icon,
        description=page.description,
        created_by=page.created_by,
        created_by_color=_color_of(page.created_by, author_colors),
        header_image_path=page.header_image_path,
        created_at=as_utc(page.created_at),
        updated_at=as_utc(page.updated_at),
        body_markdown=page.body_markdown,
    )
