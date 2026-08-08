"""Modelo de datos completo (ver Codigo/DESARROLLO.md para la explicación).

Núcleo genérico: workspace -> page -> (page_content | database + rows).
CRM y Tareas/Proyectos NO tienen tablas propias: son `databases` con un
schema concreto (ver Fase 3 del roadmap). Esto es deliberado: un único
motor sirve para notas, wiki, CRM y gestor de tareas.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.database import Base


def gen_uuid() -> uuid.UUID:
    return uuid.uuid4()


class PageType(str, enum.Enum):
    note = "note"
    database = "database"
    database_row = "database_row"


class ViewType(str, enum.Enum):
    table = "table"
    board = "board"
    calendar = "calendar"
    list = "list"


class LinkKind(str, enum.Enum):
    wikilink = "wikilink"
    relation = "relation"


page_tags = Table(
    "page_tags",
    Base.metadata,
    Column("page_id", UUID(as_uuid=True), ForeignKey("pages.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False, default="default")
    created_at = Column(DateTime, default=datetime.utcnow)

    pages = relationship("Page", back_populates="workspace")


class Page(Base):
    __tablename__ = "pages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    workspace_id = Column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("pages.id", ondelete="CASCADE"), nullable=True)
    database_id = Column(UUID(as_uuid=True), ForeignKey("databases.id", ondelete="SET NULL"), nullable=True)

    type = Column(Enum(PageType), nullable=False, default=PageType.note)
    title = Column(String, nullable=False, default="Sin título")
    icon = Column(String, nullable=True)  # "obligatorio" a nivel de producto: la API
    # siempre asigna uno por defecto al crear; la columna es nullable para no
    # forzar un backfill de las páginas ya existentes.
    created_by = Column(String, nullable=True)  # idem: la API lo rellena siempre al crear
    description = Column(String, nullable=True)
    header_image_path = Column(String, nullable=True)
    properties = Column(JSONB, nullable=True)  # solo si type == database_row

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    workspace = relationship("Workspace", back_populates="pages")
    parent = relationship("Page", remote_side=[id], backref="children")
    content = relationship("PageContent", uselist=False, back_populates="page", cascade="all, delete-orphan")
    tags = relationship("Tag", secondary=page_tags, back_populates="pages")
    attachments = relationship("Attachment", back_populates="page", cascade="all, delete-orphan")


class PageContent(Base):
    __tablename__ = "page_content"

    page_id = Column(UUID(as_uuid=True), ForeignKey("pages.id", ondelete="CASCADE"), primary_key=True)
    body_markdown = Column(Text, nullable=False, default="")

    page = relationship("Page", back_populates="content")


class DatabaseDef(Base):
    """Una `database` tipo Notion: define el schema de propiedades que
    tendrán sus filas (que son Page con type=database_row)."""

    __tablename__ = "databases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    page_id = Column(UUID(as_uuid=True), ForeignKey("pages.id", ondelete="CASCADE"), nullable=False, unique=True)
    schema_def = Column(JSONB, nullable=False, default=list)  # [{key, name, type, options?}]

    # foreign_keys explícito: pages y databases se referencian mutuamente
    # (databases.page_id -> pages.id, pages.database_id -> databases.id),
    # así que SQLAlchemy no puede adivinar solo cuál de las dos usar aquí.
    page = relationship("Page", foreign_keys=[page_id])
    views = relationship("View", back_populates="database", cascade="all, delete-orphan")


class View(Base):
    __tablename__ = "views"

    id = Column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    database_id = Column(UUID(as_uuid=True), ForeignKey("databases.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    type = Column(Enum(ViewType), nullable=False, default=ViewType.table)
    config = Column(JSONB, nullable=False, default=dict)  # filters, sort, group_by, visible_properties

    database = relationship("DatabaseDef", back_populates="views")


class Link(Base):
    """Wikilinks (extraídos de [[...]] en el markdown) y relaciones
    explícitas entre páginas. Es lo que alimenta backlinks y el grafo."""

    __tablename__ = "links"

    id = Column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    source_page_id = Column(UUID(as_uuid=True), ForeignKey("pages.id", ondelete="CASCADE"), nullable=False)
    target_page_id = Column(UUID(as_uuid=True), ForeignKey("pages.id", ondelete="CASCADE"), nullable=False)
    kind = Column(Enum(LinkKind), nullable=False, default=LinkKind.wikilink)


class Tag(Base):
    __tablename__ = "tags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False, unique=True)

    pages = relationship("Page", secondary=page_tags, back_populates="tags")


class Attachment(Base):
    __tablename__ = "attachments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    page_id = Column(UUID(as_uuid=True), ForeignKey("pages.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String, nullable=False)
    path = Column(String, nullable=False)
    mime = Column(String, nullable=True)
    size = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    page = relationship("Page", back_populates="attachments")
