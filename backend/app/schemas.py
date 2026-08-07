import re
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from app.models import PageType, ViewType


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


# --- Motor de database/view (Fase 2) -----------------------------------

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class PropertyType(str, Enum):
    text = "text"
    number = "number"
    select = "select"
    multiselect = "multiselect"
    date = "date"
    checkbox = "checkbox"
    relation = "relation"
    url = "url"


class PropertyDef(BaseModel):
    key: str
    name: str
    type: PropertyType
    options: Optional[list[str]] = None  # para select / multiselect

    @field_validator("key")
    @classmethod
    def key_must_be_identifier(cls, v: str) -> str:
        if not KEY_RE.match(v):
            raise ValueError("key debe ser snake_case (minúsculas, dígitos, _), empezando por letra")
        return v


class DatabaseCreate(BaseModel):
    title: str = "Sin título"
    icon: Optional[str] = None
    parent_id: Optional[uuid.UUID] = None
    schema_def: list[PropertyDef] = []


class DatabaseUpdate(BaseModel):
    title: Optional[str] = None
    icon: Optional[str] = None
    schema_def: Optional[list[PropertyDef]] = None


class DatabaseRead(BaseModel):
    id: uuid.UUID  # id de la fila `databases`
    page_id: uuid.UUID
    title: str
    icon: Optional[str] = None
    schema_def: list[PropertyDef]
    created_at: datetime
    updated_at: datetime


class RowCreate(BaseModel):
    title: str = "Sin título"
    icon: Optional[str] = None
    properties: dict[str, Any] = {}


class RowUpdate(BaseModel):
    title: Optional[str] = None
    icon: Optional[str] = None
    properties: Optional[dict[str, Any]] = None


class RowRead(BaseModel):
    id: uuid.UUID
    database_id: uuid.UUID
    title: str
    icon: Optional[str] = None
    properties: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ViewCreate(BaseModel):
    name: str
    type: ViewType = ViewType.table
    config: dict[str, Any] = {}


class ViewUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[ViewType] = None
    config: Optional[dict[str, Any]] = None


class ViewRead(BaseModel):
    id: uuid.UUID
    database_id: uuid.UUID
    name: str
    type: ViewType
    config: dict[str, Any]
