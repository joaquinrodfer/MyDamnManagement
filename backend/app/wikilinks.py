"""Extracción y resolución de wikilinks [[Título]] / [[Título|Alias]].

Los enlaces se resuelven en el momento de guardar, por título exacto
(sin distinguir mayúsculas) dentro del mismo workspace. Es una limitación
conocida: renombrar una página no repara automáticamente los wikilinks
que otras páginas ya guardaron apuntando al título antiguo — se corrigen
la próxima vez que esa página de origen se vuelva a guardar.
"""

import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def extract_titles(body_markdown: str) -> list[str]:
    seen: list[str] = []
    for match in WIKILINK_RE.finditer(body_markdown or ""):
        title = match.group(1).strip()
        if title and title not in seen:
            seen.append(title)
    return seen


def sync_wikilinks(db: Session, page: models.Page, body_markdown: str) -> None:
    """Recalcula los wikilinks salientes de `page` a partir de su body_markdown."""
    db.query(models.Link).filter(
        models.Link.source_page_id == page.id,
        models.Link.kind == models.LinkKind.wikilink,
    ).delete()

    titles = extract_titles(body_markdown)
    if not titles:
        return

    lowered = [t.lower() for t in titles]
    targets = (
        db.query(models.Page)
        .filter(
            models.Page.workspace_id == page.workspace_id,
            models.Page.id != page.id,
            func.lower(models.Page.title).in_(lowered),
        )
        .all()
    )
    for target in targets:
        db.add(
            models.Link(
                source_page_id=page.id,
                target_page_id=target.id,
                kind=models.LinkKind.wikilink,
            )
        )
