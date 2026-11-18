import re
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    # Plan H2: en la raíz, `shared` (todo el espacio) o `private` (solo yo). Sin
    # él, privada como siempre.
    section: Optional[Literal["shared", "private"]] = None


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
    # Plan H2: pasar a Compartido o a Privado (página de la raíz) y mover a
    # otro espacio (queda en la raíz del destino).
    section: Optional[Literal["shared", "private"]] = None
    workspace_id: Optional[uuid.UUID] = None


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
    # Bloque B: el avatar de quien la creó (`auth.AVATARS`), resuelto igual que
    # el color. `null` = el nombre no es de ningún usuario.
    created_by_avatar: Optional[str] = None
    header_image_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    body_markdown: Optional[str] = None
    # 3.0, hito 2: cuántas veces se ha escrito. Va de vuelta en `If-Match` al
    # guardar; si ya no es la actual, `PATCH` da 409 con el documento de ahora.
    version: int = 1
    # Solo en una fila (`type=database_row`): la base a la que pertenece. Con
    # él la página de la fila pide el esquema y pinta sus propiedades; en una
    # nota o una base es `null`.
    database_id: Optional[uuid.UUID] = None


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
    # `parent_row`: subtareas. La celda es el id de la fila madre, de la MISMA
    # base, y una base tiene como mucho una propiedad de este tipo.
    parent_row = "parent_row"
    person = "person"


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
    # Orden semántico de esos valores: `[alta, media, baja]` es como se leen (y
    # el orden de las columnas del tablero), no el alfabético. Es opt-in: una
    # base que no lo declara se ordena como siempre. `None` al editar = «no lo
    # toco» (se conserva el que ya tenía, ver `app.properties.plan_schema`);
    # `[]` = bórralo.
    sort_order: Optional[list[str]] = None
    # Modo de orden de la misma selección: `manual` (manda `sort_order`, y el
    # tablero agrupa en ese orden) o `auto` (alfabético natural). `None` al
    # editar = «no lo toco»; sin ninguna de las dos claves la base ordena en
    # `auto`, salvo que declare `sort_order`, que entonces se lee en manual.
    sort_mode: Optional[str] = None
    # Solo en `relation`. `None` al editar = «no lo toco» (se conserva el que
    # ya tenía la propiedad, ver `app.properties.plan_schema`); `""` = bórralo.
    target_database: Optional[str] = None
    multiple: Optional[bool] = None
    # Solo en la propiedad que dice si una fila está cerrada: `true` en un
    # `checkbox` (marcado), o la lista de valores en un `select`/`multiselect`
    # (la celda vale uno de ellos). Lo usa el contador de subtareas. `None` al
    # editar = «no lo toco» (se conserva el que ya tenía).
    done: Optional[Any] = None
    # Icono de la propiedad (un emoji), como en Notion: sale delante de su
    # nombre en la cabecera de la tabla. `None` al editar = «no lo toco»;
    # `""` = quítalo.
    icon: Optional[str] = None

    @field_validator("icon")
    @classmethod
    def icon_is_short(cls, v):
        """Un icono es un emoji (o un par de caracteres), no un texto."""
        if v is None:
            return v
        v = str(v).strip()
        if len(v) > 16:
            raise ValueError("icon es un emoji, no un texto")
        return v

    @field_validator("key")
    @classmethod
    def key_must_be_identifier(cls, v: str) -> str:
        # Vacía = «derívala del nombre». Con valor, el contrato de siempre.
        if v and not KEY_RE.match(v):
            raise ValueError("key debe ser snake_case (minúsculas, dígitos, _), empezando por letra")
        return v

    @field_validator("sort_order", mode="before")
    @classmethod
    def sort_order_is_a_list(cls, v):
        """El orden puede llegar escrito a mano como texto (`alta, media`).

        La interfaz manda una lista; aceptar el texto con comas evita un 422
        por un detalle de forma, igual que hace el lector del `.md`. Lo que no
        sea texto ni lista lo rechaza la validación normal (`list[str]`).
        """
        if v is None or isinstance(v, list):
            return v
        if isinstance(v, str):
            return v.split(",")
        return v

    @field_validator("sort_mode")
    @classmethod
    def sort_mode_is_known(cls, v):
        """El modo de orden es `manual` o `auto`; se normaliza lo escrito."""
        if v is None:
            return v
        modo = str(v).strip().lower()
        if modo not in ("manual", "auto"):
            raise ValueError("sort_mode es `manual` o `auto`")
        return modo

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


class ClosedGroup(BaseModel):
    """Un grupo de estados (plan H3): un nombre, si cuenta como cerrado y las
    opciones de la propiedad que contiene."""

    name: str
    closed: bool = False
    options: list[str] = []


class ClosedValues(BaseModel):
    """Lo que la interfaz manda al declarar qué cuenta como fila cerrada.

    `property` es la clave de la columna de estado y `values` los valores que
    cierran (vacía en una casilla). Con `groups` (plan H3), `values` se deriva
    de los grupos cerrados y no hace falta mandarla. El servidor lo valida
    contra las opciones reales de la propiedad antes de escribir nada
    (`app.closed.validate`).
    """

    property: str = ""
    values: list[str] = []
    groups: Optional[list[ClosedGroup]] = None


class ClosedRule(BaseModel):
    """La regla efectiva de una base, tal y como la lee la interfaz.

    `source` dice de dónde sale: `base` (el bloque `closed:` del `.md`) o
    `done` (el marcador viejo del schema, que se sigue leyendo para no romper
    ninguna base y se convierte al guardar desde la interfaz).
    """

    property: str
    values: list[str] = []
    groups: list[ClosedGroup] = []
    source: str = "base"


class DatabaseCreate(BaseModel):
    title: str = "Sin título"
    icon: Optional[str] = None
    parent_id: Optional[uuid.UUID] = None
    section: Optional[Literal["shared", "private"]] = None
    schema_def: list[PropertyDef] = []
    # La regla de qué cuenta como fila cerrada, si la plantilla la trae. Se
    # valida contra las opciones del schema: un valor que no existe es un 400.
    closed: Optional[ClosedValues] = None


class DatabaseFromTemplate(BaseModel):
    """Crear una base a partir de una plantilla de serie.

    `title` es el nombre de la base nueva (si falta, el de la plantilla) y
    `parent_id` la carpeta destino: una nota de la que cuelga. Sin él, la base
    nace en `Bases/`, como cualquier base creada desde el diálogo.
    """

    template: str
    title: Optional[str] = None
    parent_id: Optional[uuid.UUID] = None


class RowTemplate(BaseModel):
    """Una plantilla de fila: nombre, valores por defecto y cuerpo inicial.

    Vive en el frontmatter de su base (`row_templates:`) y se copia al índice.
    `properties` está en la forma del `.md` (claves del schema y valores tal y
    como se escriben); el servidor los valida contra el schema al guardarla y al
    usarla, porque una plantilla que nombra un valor que no existe no puede
    crear una fila a medias.
    """

    name: str
    properties: dict[str, Any] = {}
    body: str = ""


class RowTemplateRead(RowTemplate):
    """Una plantilla de fila tal y como la lee la interfaz.

    `properties` es lo que dice el `.md` (texto: el fichero no infiere tipos) y
    `values` los mismos valores coaccionados al tipo de su propiedad —un
    `false` de verdad, un número, una fecha—, que es lo que se puede pintar en
    un control. `error` explica por qué la plantilla ya no encaja con la base
    (una opción que se quitó, una columna que se borró): el alta avisa con ese
    motivo y no crea la fila a medias.
    """

    values: dict[str, Any] = {}
    error: Optional[str] = None


class RowTemplatesUpdate(BaseModel):
    """El cuerpo de `PUT /databases/{id}/row-templates`."""

    row_templates: list[RowTemplate] = []


class DatabaseUpdate(BaseModel):
    """El cuerpo de `PATCH /databases/{id}`: título, icono y schema.

    La **regla de fila cerrada** no se cambia por esta ruta: tiene la suya
    (`PUT /databases/{id}/closed`, con la validación contra las opciones reales
    de la propiedad). El campo `closed` se declara solo para poder rechazarlo
    con un 400 que diga dónde está: sin declararlo, Pydantic lo descartaba y el
    cliente recibía un 200 de algo que no se había guardado.
    """

    title: Optional[str] = None
    icon: Optional[str] = None
    schema_def: Optional[list[PropertyDef]] = None
    # Nombre visible de la columna del título (`null` = no se toca). Es una
    # columna más: se renombra y se quita, aunque su clave siga siendo `title`.
    title_name: Optional[str] = None
    # `True` = la tabla deja de enseñar la columna del título. La fila se sigue
    # llamando por su `id` (el fichero), y su `title` sigue en el `.md`.
    title_hidden: Optional[bool] = None
    # Solo para rechazarlo: `PATCH` no cambia la regla de estado cerrado. El
    # router contesta 400 si la petición trae la clave (aunque venga a `null`),
    # porque un 200 que no ha guardado nada es peor que un error que lo dice.
    closed: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "No se cambia por aquí: la regla de fila cerrada se declara en "
            "`PUT /databases/{id}/closed` (y se quita con `DELETE`). Un `closed` "
            "en este cuerpo es un 400 que lo dice."
        ),
    )


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
    # Qué cuenta como fila cerrada en esta base, o `None` si no declara estado
    # (entonces nada se considera cerrado: ni el contador, ni «Mis tareas», ni
    # los filtros). Viene resuelta: la interfaz no tiene que mirar los `done`
    # viejos del schema.
    closed: Optional[ClosedRule] = None
    # Plantillas de fila de ESTA base, en el orden del `.md`: el «+ Nueva fila»
    # las ofrece y el servidor las aplica por nombre. `[]` = la base no tiene.
    # Viajan con sus valores ya coaccionados (`values`) y, si no encajan, con el
    # motivo (`error`): el cliente no reinterpreta el texto del `.md`.
    row_templates: list[RowTemplateRead] = []
    created_at: datetime
    updated_at: datetime


class RowCreate(BaseModel):
    title: str = "Sin título"
    icon: Optional[str] = None
    properties: dict[str, Any] = {}
    # Nombre de la plantilla de fila con la que nace la fila (opcional). El
    # servidor la valida contra el schema actual —una plantilla que miente es un
    # 400 con el motivo, no una fila a medias— y rellena con sus valores las
    # propiedades que el cuerpo no traiga. `properties` manda sobre la plantilla:
    # lo que el usuario cambió en el alta se respeta. En blanco (o solo espacios)
    # es «sin plantilla»: no se busca una plantilla llamada «».
    template: Optional[str] = None
    # Cuerpo markdown inicial de la fila (lo usa la plantilla de fila: una fila
    # nueva puede nacer con su «## Qué revisar»). Ausente = cuerpo vacío.
    body_markdown: Optional[str] = None


class RowUpdate(BaseModel):
    title: Optional[str] = None
    icon: Optional[str] = None
    properties: Optional[dict[str, Any]] = None
    # Cuerpo markdown de la fila. Ausente (o `null`) = no tocarlo; `""` = dejarlo
    # vacío. El router distingue «no viene» de `""` con `model_fields_set`, así
    # que guardar una celda no puede borrar el cuerpo sin querer.
    body_markdown: Optional[str] = None


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


class RowChildren(BaseModel):
    """El contador de la tarjeta: subtareas DIRECTAS de una fila.

    `total` son las hijas y `closed` las que están cerradas según la regla de la
    base (`app.closed.is_closed`: el bloque `closed: {property, values}` de su
    `.md`). Se cuenta
    sobre todas las filas de la base, no sobre las que deja pasar la vista: el
    contador de una fila no puede cambiar porque la vista filtre por otra cosa.
    """

    total: int = 0
    closed: int = 0
class PersonRef(BaseModel):
    """El usuario al que apunta una celda de tipo `person`, resuelto al leer.

    `display_name: None` = el id guardado ya no está en `users` (se borró la
    cuenta). El `.md` conserva el id y la celda enseña que ya no está, igual que
    una relación con la fila borrada: una celda vacía no explicaría nada.
    """

    id: str
    display_name: Optional[str] = None
    username: Optional[str] = None
    color: Optional[str] = None
    avatar: Optional[str] = None


class RowRead(BaseModel):
    id: uuid.UUID
    database_id: uuid.UUID
    title: str
    # 3.0, hito 2: la versión de la fila, para `If-Match` (ver `PageRead`).
    version: int = 1
    icon: Optional[str] = None
    properties: dict[str, Any]
    # El id de su fila madre si la base tiene jerarquía y esta fila es una
    # subtarea. Es el mismo id que hay en la celda `parent_row` de
    # `properties`, resuelto aparte para que el cliente no tenga que saber cuál
    # es la columna; `null` = no cuelga de nadie (o de una fila que ya no está).
    parent_id: Optional[str] = None
    # Cuántas subtareas tiene y cuántas están cerradas: el «1 de 3» de la fila.
    children: RowChildren = Field(default_factory=RowChildren)
    # Si ESTA fila está cerrada, con la misma regla que cuenta el contador de su
    # madre (`app.closed.is_closed`). Lo calcula el servidor para que el cliente
    # no tenga que repetir la regla y las dos no puedan discrepar. Va como
    # `description` (y no solo como comentario) porque es de lo que lee quien
    # consume la API en el OpenAPI.
    closed: bool = Field(
        default=False,
        description=(
            "La fila está cerrada según la regla de la base "
            "(`closed: {property, values}` en el `.md`), la misma con la que se "
            "cuenta el contador de su madre."
        ),
    )
    # Claves de las propiedades de fecha cuyo día ya pasó, para que la tabla
    # marque esa celda. Vacío cuando la base no declara regla de cierre (sin
    # estado no hay «sin terminar») o la fila está cerrada (una tarea hecha
    # tarde no está vencida): la comparación la hace el servidor contra su
    # propio día, así que dos husos no pueden pintar cosas distintas.
    overdue_keys: list[str] = Field(
        default_factory=list,
        description=(
            "Claves de las propiedades `date` con una fecha anterior al día del "
            "servidor, en una fila abierta de una base que declara regla de "
            "cierre. La tabla marca esas celdas."
        ),
    )
    # `{clave_de_la_propiedad: [fila, ...]}` de las propiedades `relation`, con
    # el título ya resuelto por el servidor (una consulta por respuesta). Una
    # fila sin relaciones devuelve `{}`.
    related: dict[str, list[RelatedRow]] = {}
    # `{clave_de_la_propiedad: persona}` de las propiedades `person`, con el
    # nombre visible ya resuelto por el servidor: la celda enseña el nombre y
    # no el id, y el cliente no necesita la tabla de usuarios. Una fila sin
    # personas devuelve `{}`.
    people: dict[str, PersonRef] = {}
    created_at: datetime
    updated_at: datetime


class RowPageRead(RowRead):
    """Una fila leída para abrirla como página: lo de `RowRead` + su cuerpo.

    El cuerpo va SOLO en la lectura de UNA fila
    (`GET /databases/{id}/rows/{row}`) y en `PATCH` no vuelve: una tabla de
    cien filas no tiene por qué arrastrar cien cuerpos que no se van a
    enseñar, y el PATCH de una celda —que es el caso frecuente— tampoco.
    """

    body_markdown: str = ""
    # Las subtareas de esta fila, como filas completas (título, estado y fecha
    # salen de `properties` y de las fechas de la fila). Es la lista que la
    # página de la fila enseña debajo de sus propiedades. Solo en la lectura de
    # UNA fila: la tabla no arrastra las hijas de cada fila.
    subtasks: list[RowRead] = []
    # El id de su madre, si esta fila es una subtarea (para la miga de pan).
    parent_id: Optional[str] = None
    # El título de su madre, ya resuelto: la página de la fila puede decir de
    # quién es sin pedir otra fila. `null` = no tiene madre (o ya no existe).
    parent_title: Optional[str] = None


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


# --- Mis tareas (vista que cruza todas las bases) ------------------------


class MyTaskCell(BaseModel):
    """Una celda que la vista enseña: de qué propiedad sale y qué valor tiene.

    `value` es el texto tal como está guardado (para una fecha, `YYYY-MM-DD`):
    el nombre visible (`name`) va al lado porque la tabla de la base no está
    abierta y sin él no se sabe qué se está leyendo.
    """

    key: str
    name: str
    value: str


class MyTask(BaseModel):
    """Una fila que es mía: de qué base viene y qué se enseña de ella."""

    row_id: uuid.UUID
    database_id: uuid.UUID
    database_title: str
    title: str
    icon: Optional[str] = None
    # `None` cuando la base no tiene una propiedad de selección que haga de
    # estado (o la fila la tiene vacía): el interfaz no enseña hueco.
    status: Optional[MyTaskCell] = None
    # La primera propiedad `date` de la base. `None` = sin fecha o base sin
    # ninguna propiedad de fecha.
    date: Optional[MyTaskCell] = None
    # En qué bloque cae la fecha, contado contra el día del **servidor**
    # (`app.due`): `overdue` (ya pasó), `today`, `week` (de mañana al domingo de
    # esta semana) o `later`. `None` = sin fecha legible, que no es un bloque
    # sino la ausencia de uno.
    due: Optional[str] = None
    # Días de calendario que faltan para la fecha (negativo = ya pasó), con la
    # misma cuenta que `due`. `None` = sin fecha legible.
    days: Optional[int] = None


class MyTasksCounts(BaseModel):
    """Los dos números de la barra lateral, con el día del servidor."""

    # Ya vencidas: fecha anterior a hoy y fila abierta.
    overdue: int = 0
    # Vencen hoy.
    today: int = 0


class MyTasksRead(BaseModel):
    """`GET /my-tasks`: todas mis filas de todas las bases, de una vez.

    `closed_values` viaja con la respuesta para que la interfaz pueda explicar
    el criterio con la misma lista que se aplicó, y no con una copia que se
    quede vieja. `today` y `counts` viajan para que la barra lateral no tenga
    que mirar el reloj del navegador: el día con el que se ha comparado y el
    resultado de la comparación los dice el servidor.
    """

    count: int
    # El día del servidor con el que se ha comparado (`YYYY-MM-DD`).
    today: str
    counts: MyTasksCounts = Field(default_factory=MyTasksCounts)
    closed_values: list[str]
    tasks: list[MyTask]


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
