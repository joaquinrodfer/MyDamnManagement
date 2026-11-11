import re
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from app.enums import PageType, ViewType
from app.properties import DEFAULT_TITLE_NAME


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
    # Color del usuario que la creó (P11b), resuelto en el servidor contra
    # `users` por `display_name`: el cliente no tiene por qué conocer la tabla
    # de usuarios para pintar el punto de un autor. `null` = nombre sin usuario
    # (notas viejas con «Tú»), y la interfaz pinta el gris neutro de siempre.
    created_by_color: Optional[str] = None
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
    """Una propiedad del schema.

    `key` es la clave interna del `.md` y **no se le pide al usuario**: el
    diálogo solo recoge el nombre visible y el servidor la deriva
    (`app.properties.slug_key`). Por eso al crear o editar puede llegar vacía
    (`""`); al leer, siempre viene con su valor, porque es la identidad de la
    propiedad en el fichero y en las filas.

    Una propiedad `relation` declara además **a qué base apunta**
    (`target_database`, el id de esa base) y si enlaza varias filas
    (`multiple`). Los dos son opcionales porque solo existen en ese tipo: una
    propiedad de texto no los lleva.
    """

    key: str = ""
    name: str
    type: PropertyType
    options: Optional[list[str]] = None  # para select / multiselect
    # Solo en `relation`. `None` al editar = «no lo toco» (se conserva el que
    # ya tenía la propiedad, ver `app.properties.plan_schema`); `""` = bórralo.
    target_database: Optional[str] = None
    multiple: Optional[bool] = None

    @field_validator("key")
    @classmethod
    def key_must_be_identifier(cls, v: str) -> str:
        # Vacía = «derívala del nombre». Con valor, el contrato de siempre.
        if v and not KEY_RE.match(v):
            raise ValueError("key debe ser snake_case (minúsculas, dígitos, _), empezando por letra")
        return v

    @field_validator("multiple", mode="before")
    @classmethod
    def multiple_is_truthy(cls, v):
        """`multiple` llega como booleano o como el texto que vuelve del `.md`.

        El índice guarda lo que dijo el parser de YAML, que no infiere tipos:
        `multiple: false` es la cadena `"false"` (verdadera en Python). Aquí se
        normaliza a `True`/`False` de verdad, igual que en `reader`.
        """
        if v is None or isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("true", "1", "yes", "on", "si", "sí")


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
    # Nombre visible de la columna del título (`null` = no se toca). Es una
    # columna más: se renombra y se quita, aunque su clave siga siendo `title`.
    title_name: Optional[str] = None
    # `True` = la tabla deja de enseñar la columna del título. La fila se sigue
    # llamando por su `id` (el fichero), y su `title` sigue en el `.md`.
    title_hidden: Optional[bool] = None


class DatabaseRead(BaseModel):
    id: uuid.UUID  # id de la fila `databases`
    page_id: uuid.UUID
    title: str
    icon: Optional[str] = None
    # El `schema` tal y como lo entiende la app: cada propiedad es lo que dice
    # el `.md` (sin claves inventadas), más `target_database`/`multiple` en una
    # `relation` y `warning` cuando la relación está rota. No es `PropertyDef`
    # a propósito: una propiedad escrita a mano con un tipo que la app no
    # conoce tiene que poder leerse (el `.md` manda), y así la respuesta no
    # rellena con `null` lo que el fichero no dice.
    schema_def: list[dict[str, Any]]
    # La columna del título: cómo se llama y si se enseña. Cualquier base de
    # antes de esta tarjeta no trae las claves en su `.md` y sale con los
    # valores por defecto («Título», visible).
    title_name: str = DEFAULT_TITLE_NAME
    title_hidden: bool = False
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


class RelatedRow(BaseModel):
    """Una fila a la que apunta una celda de relación, resuelta en el servidor.

    `title: None` = el id guardado ya no resuelve a ninguna fila (la borraron).
    El `.md` sigue apuntando a él y el id sigue en `properties`: una celda vacía
    no explicaría nada, y el interfaz necesita saber que hay algo roto ahí para
    enseñar «(borrada)». `base_id` es la base de la fila referenciada (o la que
    declara la propiedad, si la fila ya no está).
    """

    id: str
    title: Optional[str] = None
    base_id: Optional[str] = None


class RowRead(BaseModel):
    id: uuid.UUID
    database_id: uuid.UUID
    title: str
    icon: Optional[str] = None
    properties: dict[str, Any]
    # `{clave_de_la_propiedad: [fila, ...]}` de las propiedades `relation`, con
    # el título ya resuelto por el servidor (una consulta por respuesta). Una
    # fila sin relaciones devuelve `{}`.
    related: dict[str, list[RelatedRow]] = {}
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


# --- Puerta para agentes (P10b) ----------------------------------------


class AgentSuggestionCreate(BaseModel):
    """Lo que manda un agente en `POST /agent/suggestions`.

    Igual que `SuggestionCreate` pero con `page_id` en el cuerpo (la ruta del
    agente no cuelga de la nota) y **sin `agent`**: el nombre sale del token.
    Si el cuerpo trae `agent`, se ignora en silencio — no es un error, es que
    ese campo no lo pone quien llama.
    """

    model_config = ConfigDict(extra="ignore")

    page_id: uuid.UUID
    kind: SuggestionKind = SuggestionKind.edit
    old_text: str = ""
    new_text: str = ""
    rationale: str = ""
    title: Optional[str] = None
    offset: Optional[int] = None


class AgentPageRef(BaseModel):
    """La nota sobre la que se lee el contexto. `path`, la del vault."""

    id: uuid.UUID
    title: str
    path: Optional[str] = None
    type: PageType
    updated_at: datetime


class AgentBacklink(BaseModel):
    """Una página que enlaza a esta por `[[wikilink]]`."""

    id: uuid.UUID
    title: str


class AgentLink(BaseModel):
    """Un wikilink del cuerpo. `exists: false` = el título no resuelve hoy."""

    title: str
    exists: bool


class AgentSuggestionRead(BaseModel):
    """Una sugerencia con su hilo, en la forma compacta que lee un agente.

    `reason` y `replies` son el canal de vuelta: lo que el dueño dijo al
    rechazar y lo que ha respondido. `path` dice dónde vive la nota.
    """

    id: uuid.UUID
    page_id: Optional[uuid.UUID] = None
    path: Optional[str] = None
    status: SuggestionStatus
    kind: SuggestionKind
    agent: str
    title: Optional[str] = None
    created_at: Optional[datetime] = None
    decided_at: Optional[datetime] = None
    old_text: str = ""
    new_text: str = ""
    rationale: str = ""
    reason: Optional[str] = None
    replies: list[SuggestionReply] = []


class AgentContextRead(BaseModel):
    """`GET /agent/context`: la nota, su vecindario y su hilo, de una vez.

    `body_truncated` dice si el cuerpo se cortó (`AGENT_CONTEXT_MAX_BODY`):
    quien lee tiene que saber que no lo ha visto entero.
    """

    page: AgentPageRef
    body: str
    body_truncated: bool = False
    backlinks: list[AgentBacklink] = []
    links: list[AgentLink] = []
    suggestions: list[AgentSuggestionRead] = []
    brief: Optional[str] = None
