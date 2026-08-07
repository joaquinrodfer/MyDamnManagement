import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.models import PageType


class PageBase(BaseModel):
    title: str = "Sin título"
    type: PageType = PageType.note
    parent_id: Optional[uuid.UUID] = None
    icon: Optional[str] = None


class PageCreate(PageBase):
    body_markdown: Optional[str] = ""


class PageUpdate(BaseModel):
    title: Optional[str] = None
    icon: Optional[str] = None
    body_markdown: Optional[str] = None


class PageRead(PageBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    body_markdown: Optional[str] = None
