import re
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from app.enums import PageType, ViewType


class PageBase(BaseModel):
    title: str = "Sin título"
    type: PageType = PageType.note
    parent_id: Optional[uuid.UUID] = None
    icon: Optional[str] = None  # si no se manda, la API asigna settings.default_icon
    description: Optional[str] = None


class PageCreate(PageBase):
    body_markdown: Optional[str] = ""


class PageUpdate(BaseModel):
    title: Optional[str] = None
    icon: Optional[str] = None
    description: Optional[str] = None
    body_markdown: Optional[str] = None
    # Mover de carpeta: `parent_id` a otra nota, o `null` explícito a la raíz.
    # Si la clave no llega, la página no se mueve (no se distingue de "sin
    # cambios" si el valor por defecto es None, así que se mira el campo
    # presente en el cuerpo del PATCH — ver routers/pages.py).
    parent_id: Optional[uuid.UUID] = None


class PageRead(PageBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    created_by: Optional[str] = None
    header_image_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    body_markdown: Optional[str] = None


# --- Grafo de nodos (P9) ------------------------------------------------


class GraphNode(BaseModel):
    """Un nodo del grafo: una página del índice y su grado.

    `degree` es el número de enlaces (filas de `links`) que tocan la página en
    cualquier dirección -- entrantes más salientes, sin contar dos veces el
    mismo par. Es el mismo número en los dos modos, así que el tamaño del nodo
    no cambia al pasar de local a global.
    """

    id: uuid.UUID
    title: str
    type: PageType
    degree: int
    updated_at: datetime


class GraphEdge(BaseModel):
    """Una arista resuelta. `source` y `target` son ids de página."""

    source: uuid.UUID
    target: uuid.UUID
    kind: str


class GraphRead(BaseModel):
    """Respuesta de `GET /graph`.

    - `truncated`: había más nodos que el tope y se ha cortado (global).
    - `broken`: wikilinks de los nodos devueltos que no resuelven a ninguna
      página. No se pintan como nodos fantasma: solo se cuentan.
    """

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = False
    broken: int = 0


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


class DatabaseFromTemplate(BaseModel):
    template: str
    title: Optional[str] = None


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


# --- Papelera (P1.2b) ---------------------------------------------------


class TrashEntry(BaseModel):
    """Un fichero de `.trash/`, con la ruta de la que salió y cuándo."""

    path: str              # `.trash/<sello>/<ruta original>`, relativo al vault
    original_path: str     # `Notas/Proyectos.md`
    deleted_at: Optional[str] = None   # ISO 8601 UTC, del nombre del sello
    size: int
    kind: str              # "note" | "file"


class TrashRestoreRequest(BaseModel):
    # La ruta de la entrada que devuelve `GET /trash` (`path`), no la original:
    # dos borrados del mismo nombre dejan dos ficheros en la papelera y hay que
    # decir cuál vuelve.
    path: str


class TrashRestoreResult(BaseModel):
    restored_path: str     # ruta original, relativa al vault
    reindexed: bool
    notes: int
    links: int


# --- Sugerencias de agentes (P10a) -------------------------------------


class SuggestionKind(str, Enum):
    """`edit`: sustituye `old_text` por `new_text` dentro de la nota.

    `new_page`: propone una nota nueva (`old_text` vacio, `new_text` es el
    cuerpo y hace falta `title`).
    """

    edit = "edit"
    new_page = "new_page"


class SuggestionStatus(str, Enum):
    """`pending` -> `accepted` | `rejected`; `orphaned` si el ancla se perdio."""

    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"
    orphaned = "orphaned"


class SuggestionCreate(BaseModel):
    """Lo que manda un agente. `offset` es opcional: sin el, el ancla se
    supone en la primera aparicion del texto, que es lo que elegiria aceptar."""

    agent: str = "agente"
    kind: SuggestionKind = SuggestionKind.edit
    old_text: str = ""
    new_text: str = ""
    rationale: str = ""
    title: Optional[str] = None
    offset: Optional[int] = None


class SuggestionReply(BaseModel):
    """Una entrada del hilo: quien responde, que dice y cuando."""

    author: str
    text: str
    at: str


class SuggestionRead(BaseModel):
    id: uuid.UUID
    page_id: uuid.UUID
    agent: str
    created_at: datetime
    status: SuggestionStatus
    kind: SuggestionKind
    old_text: str = ""
    new_text: str = ""
    rationale: str = ""
    title: Optional[str] = None
    base_revision: Optional[datetime] = None
    offset: int = 0
    replies: list[SuggestionReply] = []
    decided_at: Optional[datetime] = None
    reason: Optional[str] = None


class SuggestionRejectRequest(BaseModel):
    reason: Optional[str] = None


class SuggestionReplyRequest(BaseModel):
    text: str
    author: Optional[str] = None


class SuggestionAcceptRead(BaseModel):
    """Resultado de aceptar: la sugerencia resuelta y la nota tocada.

    `stale` avisa de que la nota cambio desde que el agente propuso (su
    `base_revision` ya no es la actual); el parche se aplico igual porque el
    `old_text` seguia estando. `created` distingue la nota nueva de `new_page`.
    """

    suggestion: SuggestionRead
    page_id: uuid.UUID
    created: bool = False
    stale: bool = False
