from typing import Optional

from app import models, schemas


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
        created_at=page.created_at,
        updated_at=page.updated_at,
        body_markdown=body,
    )
