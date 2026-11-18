import contextlib
import uuid
from typing import Iterator, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from app import closed, due, hierarchy, schemas
from app import kinds as kind_rules
from app.config import settings
from app import auth
from app.dependencies import get_current_user, get_repo
from app.events import publish_database
from app.properties import (
    order_ranks,
    people_of,
    person_ids,
    person_keys,
    plan_schema,
    related_refs,
    relation_ids,
    schema_types,
    schema_with_relation_warnings,
    validate_properties,
)
from app.repository import Database, Page, Repository, View
from app.serializers import as_utc
from app import templates as template_defs
from app.vault.store import UNSET, VaultError, VersionConflict
from app.versioning import conflict, parse_if_match
from app.views import apply_view, sort_keys, validar_rango

router = APIRouter(prefix="/databases", tags=["databases"])


@contextlib.contextmanager
def _vault_errors() -> Iterator[None]:
    """Un `VaultError` del vault es un 400 con el motivo, no un 500 mudo.

    Los caminos de base y fila escriben ficheros (P2): si el `.md` de la base ya
    no está en el disco, o el título no cabe en un nombre de fichero, lo que
    falla es la petición, no el servidor.
    """
    try:
        yield
    except VaultError as exc:
        raise HTTPException(400, str(exc)) from exc


def _get_database_or_404(repo: Repository, database_id: uuid.UUID) -> Database:
    db_def = repo.get_database(database_id)
    if db_def is None:
        raise HTTPException(404, "Base de datos no encontrada")
    return db_def


def _get_row_or_404(repo: Repository, database_id: uuid.UUID, row_id: uuid.UUID) -> Page:
    row = repo.get_row(database_id, row_id)
    if row is None:
        raise HTTPException(404, "Fila no encontrada")
    return row


def _serialize_database(
    db_def: Database, known_databases: set[str]
) -> schemas.DatabaseRead:
    """La base, con sus relaciones rotas marcadas (`warning` en la propiedad).

    `known_databases` son los ids de las bases que existen ahora mismo
    (`repo.database_ids()`, una consulta por respuesta): borrar la base destino
    no toca el `.md` de quien la enlazaba, así que la propiedad solo puede
    saberlo al leerse.
    """
    return schemas.DatabaseRead(
        id=db_def.id,
        page_id=db_def.page_id,
        title=db_def.title,
        icon=db_def.icon,
        schema_def=schema_with_relation_warnings(db_def.schema_def, known_databases),
        title_name=db_def.title_name,
        title_hidden=db_def.title_hidden,
        # La regla de «cerrada» viaja resuelta y con su origen: la interfaz
        # enseña la columna marcada y puede decir que viene del `done` viejo.
        kind=schemas.KindRead(**db_def.kind) if db_def.kind else None,
        closed=(
            schemas.ClosedRule(
                property=str(db_def.closed[closed.PROPERTY_KEY]),
                values=list(db_def.closed.get(closed.VALUES_KEY) or []),
                groups=[schemas.ClosedGroup(**g) for g in (db_def.closed.get(closed.GROUPS_KEY) or [])],
                source=closed.SOURCE_DONE if db_def.closed_from_done else closed.SOURCE_BASE,
            )
            if db_def.closed
            else None
        ),
        # Las plantillas de fila viajan con la base: el «+ Nueva fila» las
        # ofrece sin otra consulta y el cliente no tiene que leer el `.md`.
        # Con `values` coaccionados (el `.md` no infiere tipos y `Boolean("false")`
        # es `true`) y con `error` cuando ya no encajan con el schema.
        row_templates=[
            schemas.RowTemplateRead(**t)
            for t in template_defs.resolve_row_templates(
                db_def.row_templates, db_def.schema_def
            )
        ],
        created_at=as_utc(db_def.created_at),
        updated_at=as_utc(db_def.updated_at),
    )


def _serialize_rows(
    repo: Repository,
    db_def: Database,
    rows: List[Page],
    *,
    parent_key: Optional[str] = None,
    counts: Optional[dict[str, tuple[int, int]]] = None,
    users: Optional[dict] = None,
) -> List[schemas.RowRead]:
    """Serializa filas resolviendo relaciones y personas **en una consulta cada una**.

    Los ids de todas las celdas de relación de la respuesta se juntan y se
    resuelven de una vez (`repo.row_refs`): el coste es el de la respuesta, no
    el de cada fila. Lo que no resuelve (fila borrada) sale con `title: null`,
    y el id se queda en `properties`: la celda apunta a algo que ya no está y
    el interfaz tiene que poder decirlo.

    Las celdas `person` hacen lo mismo con los usuarios (`repo.user_refs`): el
    id se queda en `properties` y `people` lleva el nombre visible y el color.
    `users` se puede pasar ya resuelto (el router lo necesita antes para
    filtrar y ordenar por nombre); si no, se resuelve aquí.
    """
    properties = [row.properties or {} for row in rows]
    ids = relation_ids(db_def.schema_def, properties)
    refs = repo.row_refs(ids) if ids else {}
    cuentas = counts or {}
    # Un solo «hoy» por respuesta: la fecha de cada fila se compara contra el
    # día del servidor (app.due), no contra el reloj de quien mira.
    hoy = due.server_today()
    if users is None:
        gente = person_ids(db_def.schema_def, properties)
        users = repo.user_refs(gente) if gente else {}
    return [
        schemas.RowRead(
            id=row.id,
            database_id=row.database_id,
            title=row.title,
            version=row.version,
            icon=row.icon,
            properties=props,
            related=related_refs(db_def.schema_def, props, refs),
            # La jerarquía viaja resuelta: la clave de la celda la sabe el
            # servidor (una base puede llamarla «Subtarea de» o «Paso de»), y
            # con `parent_id` el cliente no tiene que preguntarla para saber
            # qué filas son hijas de cuál.
            parent_id=hierarchy.parent_of(props, parent_key),
            children=schemas.RowChildren(
                total=cuentas.get(str(row.id), (0, 0))[0],
                closed=cuentas.get(str(row.id), (0, 0))[1],
            ),
            closed=closed.is_closed(db_def.closed, props),
            # Qué fechas ya pasaron en esta fila. La regla de «no marcar lo
            # cerrado» y la de «solo donde la base declara estado» viven en
            # `app.due`: el cliente pinta la celda, no decide.
            overdue_keys=due.overdue_date_keys(
                db_def.schema_def, props, db_def.closed, hoy
            ),
            people=people_of(db_def.schema_def, props, users),
            created_at=as_utc(row.created_at),
            updated_at=as_utc(row.updated_at),
        )
        for row, props in zip(rows, properties)
    ]


def _hierarchy_counts(
    repo: Repository, db_def: Database
) -> tuple[Optional[str], dict[str, tuple[int, int]]]:
    """La clave de la jerarquía y el contador de cada madre, sobre toda la base.

    El contador se cuenta sobre TODAS las filas de la base (una consulta), no
    sobre las que deja pasar la vista: «1 de 3» es la verdad de la fila y no
    cambia porque la vista filtre por otra cosa.
    """
    key = hierarchy.parent_key(db_def.schema_def)
    if not key:
        return None, {}
    return key, hierarchy.child_counts(repo.list_rows(db_def.id), key, db_def.closed)


def _serialize_row(
    repo: Repository, db_def: Database, row: Page
) -> schemas.RowRead:
    """Una fila con su contador de subtareas (contado sobre toda la base)."""
    key, counts = _hierarchy_counts(repo, db_def)
    return _serialize_rows(repo, db_def, [row], parent_key=key, counts=counts)[0]


def _serialize_row_page(repo: Repository, db_def: Database, row: Page) -> schemas.RowPageRead:
    """La fila con su cuerpo, para abrirla como página.

    Se compone de `_serialize_row` (relaciones resueltas en una consulta, como
    en la tabla) y no al revés: la lectura de una fila suelta no necesita otro
    camino de serialización. `body_markdown` sale del índice, que lo tiene
    porque lo leyó del `.md` al indexar: reindexar el vault devuelve lo mismo.
    """
    key = hierarchy.parent_key(db_def.schema_def)
    # Una sola pasada por la base: de la misma lista salen el contador de la
    # fila y sus subtareas (ver `_serialize_row`, que hace lo propio cuando la
    # fila va sola).
    todas = repo.list_rows(db_def.id)
    counts = hierarchy.child_counts(todas, key, db_def.closed) if key else {}
    base = _serialize_rows(repo, db_def, [row], parent_key=key, counts=counts)[0]
    # Las subtareas van en orden de creación (el de `list_rows`): lo que se
    # cuelga de una fila es un plan, y un plan se lee en el orden en que se
    # escribió; el `sort` de una vista de la base no tiene por qué gobernarlo.
    subtasks = (
        _serialize_rows(
            repo,
            db_def,
            [r for r in todas if hierarchy.parent_of(r, key) == str(row.id)],
            parent_key=key,
            counts=counts,
        )
        if key
        else []
    )
    id_madre = hierarchy.parent_of(row.properties or {}, key)
    madre = repo.row_refs([id_madre]).get(id_madre) if id_madre else None
    return schemas.RowPageRead(
        **base.model_dump(),
        body_markdown=row.body_markdown or "",
        subtasks=subtasks,
        parent_title=madre[0] if madre else None,
    )


def _serialize_view(view: View) -> schemas.ViewRead:
    return schemas.ViewRead(
        id=view.id,
        database_id=view.database_id,
        name=view.name,
        type=view.type,
        config=view.config or {},
    )


# ---------------------------------------------------------------- databases

@router.post("", response_model=schemas.DatabaseRead)
def create_database(
    payload: schemas.DatabaseCreate,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    # La clave de cada propiedad se deriva del nombre visible: la interfaz no
    # la pide (ni la enseña). Las claves explícitas de la API se respetan.
    schema_def, _renames, _borradas = plan_schema(
        [], [p.model_dump() for p in payload.schema_def]
    )
    # La regla de «cerrada» se valida contra las opciones del schema **antes**
    # de escribir nada: un valor que no existe es un 400, no un `closed` que
    # nunca cuenta.
    closed_rule = None
    if payload.closed is not None:
        closed_rule = closed.validate(
            schema_def,
            payload.closed.property,
            payload.closed.values,
            [g.model_dump() for g in payload.closed.groups] if payload.closed.groups else None,
        )
    with _vault_errors():
        db_def = repo.create_database(
            title=payload.title,
            parent_id=payload.parent_id,
            icon=payload.icon or settings.default_icon,
            created_by=user.display_name,
            schema_def=schema_def,
            closed_rule=closed_rule,
        )
        if payload.section == "shared" and not payload.parent_id:
            repo.set_section(db_def.page_id, "shared")  # plan H2
    return _serialize_database(db_def, repo.database_ids())


@router.get("", response_model=List[schemas.DatabaseRead])
def list_databases(repo: Repository = Depends(get_repo)):
    known = repo.database_ids()
    return [_serialize_database(d, known) for d in repo.list_databases()]


# Rutas literales de un segmento ("templates", "from-template") declaradas
# ANTES de "/{database_id}": si fueran después, FastAPI intentaría parsear
# "templates" como un UUID y fallaría con 422 en vez de llegar aquí — el
# mismo problema que ya resolvimos con /pages/tree en la Fase 1.

@router.get("/templates")
def list_templates():
    """Las plantillas de serie que ofrece «Crear desde plantilla».

    Es un catálogo, no el contenido: nombre, descripción y qué propiedades y
    vistas trae cada una. El `schema_def` se queda en el servidor y es lo que se
    copia en el `.md` de la base nueva.
    """
    return template_defs.catalog()


@router.post("/from-template", response_model=schemas.DatabaseRead)
def create_from_template(
    payload: schemas.DatabaseFromTemplate,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    """Crea una base copiando una plantilla de serie.

    La plantilla es **datos**: su schema, sus vistas, la regla `closed` y sus
    plantillas de fila se escriben en el `.md` de la base nueva. A partir de ahí
    son dos cosas independientes: cambiar la plantilla no cambia esta base.

    Antes de escribir nada se valida la plantilla entera —tipos, opciones,
    `closed`, vistas y plantillas de fila— y la carpeta destino: un tipo o un
    valor que el código no conoce es un 400 con el motivo y **no se crea a
    medias**. La base nace sin filas: no hay datos de ejemplo que borrar.
    """
    template = template_defs.TEMPLATES.get(payload.template)
    if not template:
        raise HTTPException(404, f"Plantilla '{payload.template}' no existe")

    # Las claves de las propiedades se derivan del nombre visible, igual que en
    # el alta normal; las plantillas las declaran explícitas y se respetan.
    schema_def, _renames, _borradas = plan_schema(
        [], [dict(p) for p in template["schema_def"]]
    )
    cuerpo = {
        **template,
        "schema_def": schema_def,
        "views": template.get("views") or [],
        "row_templates": template.get("row_templates") or [],
    }
    template_defs.validate_template(cuerpo)

    closed_rule = None
    if template.get("closed"):
        closed_rule = closed.validate(
            schema_def,
            template["closed"].get(closed.PROPERTY_KEY),
            template["closed"].get(closed.VALUES_KEY),
            template["closed"].get(closed.GROUPS_KEY),
        )
    kind_record = None
    if template.get("kind"):
        # Plan H4: las bases de Proyectos y de Tareas nacen tipadas.
        roles = dict(template["kind"]["roles"])
        if template["kind"]["type"] == "tasks" and payload.project_database:
            destino = _get_database_or_404(repo, payload.project_database)
            if (destino.kind or {}).get("type") != "projects":
                raise HTTPException(400, "esa base no es de tipo Proyectos")
            col = kind_rules.new_column(
                "project", {str(p.get("name") or "").casefold() for p in schema_def},
                target_database=str(destino.id), multiple=False,
            )
            schema_def, _r, _b = plan_schema(schema_def, [*schema_def, col])
            roles["project"] = schema_def[-1]["key"]
        kind_record = {
            "type": template["kind"]["type"],
            "roles": roles,
            "options": kind_rules.validate_options(template["kind"]["type"], template["kind"].get("options")),
        }
        kind_rules.check_roles(kind_record["type"], roles, schema_def)

    with _vault_errors():
        db_def = repo.create_database(
            title=payload.title or template["title"],
            parent_id=payload.parent_id,
            icon=settings.default_icon,
            created_by=user.display_name,
            schema_def=schema_def,
            views=template.get("views") or [],
            closed_rule=closed_rule,
            row_templates=cuerpo["row_templates"],
            kind=kind_record,
        )
        if payload.section == "shared" and not payload.parent_id:
            repo.set_section(db_def.page_id, "shared")  # plan H2
    return _serialize_database(db_def, repo.database_ids())


@router.put("/{database_id}/kind", response_model=schemas.DatabaseRead)
def set_kind(
    database_id: uuid.UUID,
    payload: schemas.KindSet,
    repo: Repository = Depends(get_repo),
):
    """Convierte una base en Proyectos o en Tareas, o cambia sus opciones.

    Reutiliza las columnas que ya encajan (un `person` para el responsable, el
    estado, las fechas…) y crea las que falten; `roles` permite elegir cuál
    hace de qué. Si el estado no declara qué cuenta como hecho, se le dan
    grupos de partida. No se toca ninguna fila.
    """
    db_def = _get_database_or_404(repo, database_id)
    kind_type = payload.type
    s = kind_rules.spec(kind_type)
    options = kind_rules.validate_options(
        kind_type, payload.options if payload.options is not None else (db_def.kind or {}).get("options")
    )
    schema = [dict(p) for p in db_def.schema_def]
    roles = dict(kind_rules.auto_map(kind_type, schema))
    if db_def.kind and db_def.kind.get("type") == kind_type:
        roles.update(db_def.kind.get("roles") or {})
    roles.update(payload.roles or {})
    names = {str(p.get("name") or "").casefold() for p in schema}

    nuevas: list[dict] = []
    for role in s["required"]:
        if role not in roles:
            col = kind_rules.new_column(role, names)
            nuevas.append((role, col))
    if "estimate" in s["optional"] and payload.estimate and "estimate" not in roles:
        nuevas.append(("estimate", kind_rules.new_column("estimate", names)))
    if "project" in s["optional"] and payload.project_database and "project" not in roles:
        destino = _get_database_or_404(repo, payload.project_database)
        if (destino.kind or {}).get("type") != "projects":
            raise HTTPException(400, "esa base no es de tipo Proyectos")
        nuevas.append(("project", kind_rules.new_column(
            "project", names, target_database=str(destino.id), multiple=False
        )))
    nuevo_schema = schema + [c for _, c in nuevas]
    if nuevas:
        nuevo_schema, _renames, _borradas = plan_schema(schema, nuevo_schema)
        # El rol de cada columna nueva es la última de ese tipo recién añadida.
        for (role, _col), prop in zip(nuevas, nuevo_schema[len(schema):]):
            roles[role] = prop["key"]
    kind_rules.check_roles(kind_type, roles, nuevo_schema)

    # El estado tiene que decir qué es «hecho»: si la base no lo declara, grupos de partida.
    status = next(p for p in nuevo_schema if p["key"] == roles["status"])
    regla = db_def.closed if db_def.closed and db_def.closed.get(closed.PROPERTY_KEY) == roles["status"] else None
    nueva_regla = Ellipsis
    if regla is None:
        grupos = kind_rules.default_groups_for(status.get("options") or [])
        if grupos is None:
            status["options"] = [*(status.get("options") or []), "Hecha"]
            grupos = kind_rules.default_groups_for(status["options"])
        nueva_regla = closed.validate(nuevo_schema, roles["status"], None, grupos)

    record = {"type": kind_type, "roles": roles, "options": options}
    with _vault_errors():
        actualizada = repo.set_kind(
            database_id, record,
            schema=nuevo_schema if (nuevas or nueva_regla is not Ellipsis) else None,
            closed_rule=UNSET if nueva_regla is Ellipsis else nueva_regla,
        )
    publish_database(database_id, "updated")
    return _serialize_database(actualizada, repo.database_ids())


@router.delete("/{database_id}/kind", response_model=schemas.DatabaseRead)
def clear_kind(database_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    """La base vuelve a ser normal: las columnas se quedan, ya sin estar protegidas."""
    _get_database_or_404(repo, database_id)
    with _vault_errors():
        actualizada = repo.set_kind(database_id, None)
    publish_database(database_id, "updated")
    return _serialize_database(actualizada, repo.database_ids())


@router.get("/{database_id}", response_model=schemas.DatabaseRead)
def get_database(database_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    return _serialize_database(
        _get_database_or_404(repo, database_id), repo.database_ids()
    )


@router.put("/{database_id}/closed", response_model=schemas.DatabaseRead)
def set_closed(
    database_id: uuid.UUID,
    payload: schemas.ClosedValues,
    repo: Repository = Depends(get_repo),
):
    """La base declara qué cuenta como fila cerrada.

    Se valida contra las opciones reales de la propiedad —un valor que no
    existe es un 400 que dice cuáles hay— y se escribe el bloque `closed:` en
    el frontmatter, retirando los marcadores `done` viejos. Una sola propiedad
    cerrada por base: si se manda otra, se mueve (una base no tiene dos
    verdades sobre qué es estar cerrada).

    No se toca ninguna fila: los contadores, «Mis tareas» y los filtros leen la
    regla de la base, así que cambiarla recalcula sin reescribir los `.md`.
    """
    db_def = _get_database_or_404(repo, database_id)
    rule = closed.validate(
        db_def.schema_def,
        payload.property,
        payload.values,
        [g.model_dump() for g in payload.groups] if payload.groups else None,
    )
    with _vault_errors():
        actualizada = repo.set_closed(database_id, rule)
    if actualizada is None:
        raise HTTPException(404, "Base de datos no encontrada")
    # M2: la tabla incrustada en una nota tiene que repintar sus contadores.
    publish_database(database_id, "updated")
    return _serialize_database(actualizada, repo.database_ids())


@router.delete("/{database_id}/closed", response_model=schemas.DatabaseRead)
def clear_closed(database_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    """La base deja de declarar estado.

    Quitar la regla no cierra nada: el contador vuelve a «0 de N», «Mis tareas»
    entra todo y los filtros `is_closed` no dejan pasar ninguna fila. Los `.md`
    de las filas no se tocan (ni los valores de la columna de estado).
    """
    _get_database_or_404(repo, database_id)
    with _vault_errors():
        actualizada = repo.set_closed(database_id, None)
    if actualizada is None:
        raise HTTPException(404, "Base de datos no encontrada")
    publish_database(database_id, "updated")
    return _serialize_database(actualizada, repo.database_ids())


@router.put("/{database_id}/row-templates", response_model=schemas.DatabaseRead)
def set_row_templates(
    database_id: uuid.UUID,
    payload: schemas.RowTemplatesUpdate,
    repo: Repository = Depends(get_repo),
):
    """Configura las plantillas de fila de la base (`row_templates:` en su `.md`).

    Cada plantilla se valida contra el schema antes de escribir nada: una clave
    que no existe, un valor que no es una opción o un tipo que no admite valor
    por defecto son un 400 con el motivo, y el fichero se queda como estaba. Los
    nombres no se repiten (son la forma de elegir la plantilla en el alta).
    """
    db_def = _get_database_or_404(repo, database_id)
    templates = template_defs.validate_row_templates(
        [t.model_dump() for t in payload.row_templates], db_def.schema_def
    )
    with _vault_errors():
        actualizada = repo.set_row_templates(database_id, templates)
    if actualizada is None:
        raise HTTPException(404, "Base de datos no encontrada")
    publish_database(database_id, "updated")
    return _serialize_database(actualizada, repo.database_ids())


@router.patch(
    "/{database_id}",
    response_model=schemas.DatabaseRead,
    responses={
        400: {
            "description": (
                "La petición trae `closed`: la regla de fila cerrada no se "
                "cambia por aquí, sino en `PUT /databases/{id}/closed`."
            )
        }
    },
)
def update_database(
    database_id: uuid.UUID, payload: schemas.DatabaseUpdate, repo: Repository = Depends(get_repo)
):
    """Actualiza el título, el icono y el schema de una base.

    La regla de qué cuenta como fila cerrada NO se cambia por aquí: tiene su
    ruta (`PUT /databases/{id}/closed`) y su validación contra las opciones de
    la propiedad. Un `closed` en el cuerpo es un 400 que lo dice — antes se
    descartaba en silencio y la respuesta era un 200 que no había guardado
    nada, así que un cliente que se equivocaba de ruta creía haber guardado.
    """
    if "closed" in payload.model_fields_set:
        raise HTTPException(
            400,
            "la regla de estado cerrado no se cambia con PATCH /databases/{id}: "
            f"usa PUT /databases/{database_id}/closed (o DELETE para quitarla)",
        )
    if payload.schema_def is not None:
        # Plan H4: una base tipada no pierde ni cambia de tipo las columnas de sus roles.
        actual = _get_database_or_404(repo, database_id)
        kind_rules.check_schema(actual.kind, [p.model_dump() for p in payload.schema_def])
    with _vault_errors():
        db_def = repo.update_database(
            database_id,
            title=payload.title,
            icon=payload.icon,
            schema_def=None if payload.schema_def is None else [p.model_dump() for p in payload.schema_def],
            title_name=payload.title_name,
            title_hidden=payload.title_hidden,
        )
    if db_def is None:
        raise HTTPException(404, "Base de datos no encontrada")
    # M2: otra pestaña con una nota que incrusta esta base repinta su bloque.
    publish_database(db_def.id, "updated")
    return _serialize_database(db_def, repo.database_ids())


@router.delete("/{database_id}", status_code=204)
def delete_database(database_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    with _vault_errors():
        borrada = repo.delete_database(database_id)
    if not borrada:
        raise HTTPException(404, "Base de datos no encontrada")
    # M2: la base ya no está; el bloque incrustado tiene que enseñar «Base no
    # encontrada» sin esperar a que se reabra la nota.
    publish_database(database_id, "deleted")


# --------------------------------------------------------------------- rows

@router.post("/{database_id}/rows", response_model=schemas.RowRead)
def create_row(
    database_id: uuid.UUID,
    payload: schemas.RowCreate,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    db_def = _get_database_or_404(repo, database_id)
    properties = dict(payload.properties or {})
    body = payload.body_markdown or ""
    # Un `template` en blanco es «sin plantilla»: el desplegable del alta manda
    # el nombre elegido y vaciarlo no es pedir una plantilla llamada «», que es
    # un 404 hablando de un nombre que nadie escribió.
    nombre = (payload.template or "").strip()
    if nombre:
        # La plantilla se valida aquí también, aunque el alta ya haya pintado
        # sus valores: es la última puerta antes de escribir, y una plantilla
        # que miente (una opción que ya no existe, una columna borrada) es un
        # 400 con el motivo, no una fila a medias. Lo que el usuario cambió en
        # el formulario manda sobre el valor por defecto.
        plantilla = next(
            (
                t
                for t in db_def.row_templates
                if str(t.get("name") or "").strip().casefold() == nombre.casefold()
            ),
            None,
        )
        if plantilla is None:
            raise HTTPException(
                404, f"La plantilla de fila «{nombre}» no existe en esta base"
            )
        por_defecto, cuerpo = template_defs.apply_row_template(
            plantilla, db_def.schema_def
        )
        properties = {**por_defecto, **properties}
        if not body:
            body = cuerpo
    cleaned = validate_properties(
        db_def.schema_def, properties, refs=repo.row_refs, users=repo.user_refs
    )
    # La jerarquía no puede colgar de otra base. Una fila nueva todavía no
    # existe, así que aquí solo se puede comprobar el destino; los ciclos se
    # comprueban al editar (ahí la fila ya está).
    hierarchy.validate_parent(
        db_def.schema_def, cleaned, row_id=None, rows=repo.list_rows(database_id)
    )
    with _vault_errors():
        row = repo.create_row(
            database_id,
            title=payload.title,
            icon=payload.icon,
            properties=cleaned,
            body=body,
            # Una fila es una página: dice quién la creó, como una nota.
            created_by=user.display_name,
        )
    # M2: la tabla incrustada en una nota se repinta (sin tocar el documento).
    publish_database(database_id, "rows")
    return _serialize_row(repo, db_def, row)


@router.get("/{database_id}/rows", response_model=List[schemas.RowRead])
def list_rows(
    database_id: uuid.UUID,
    view: Optional[uuid.UUID] = None,
    repo: Repository = Depends(get_repo),
):
    db_def = _get_database_or_404(repo, database_id)
    rows = repo.list_rows(database_id)
    # El contador de una madre cuenta TODAS sus hijas, también las que la vista
    # deja fuera: por eso se calcula antes de filtrar.
    key, counts = _hierarchy_counts(repo, db_def)

    # Una celda `person` guarda un id, pero el filtro se escribe con el nombre
    # que se lee y el orden va por nombre: se resuelven los usuarios de TODAS
    # las filas antes de filtrar, en una consulta (el filtro se lleva unas
    # cuantas y el nombre del resto sigue haciendo falta para el orden).
    claves_persona = person_keys(db_def.schema_def)
    users: Optional[dict] = None
    if claves_persona:
        users = repo.user_refs(
            person_ids(db_def.schema_def, [row.properties or {} for row in rows])
        )

    if view is not None:
        view_def = repo.get_view(database_id, view)
        if view_def is None:
            raise HTTPException(404, "Vista no encontrada")
        # El orden por tipo: el mapa de tipos siempre, y los títulos de las
        # relaciones solo cuando la vista ordena por una (no se paga una
        # consulta que la vista no va a mirar).
        tipos = schema_types(db_def.schema_def)
        refs_de_orden: dict[str, tuple[str, str]] = {}
        if any(tipos.get(clave) == "relation" for clave in sort_keys(view_def.config or {})):
            ids_relacion = relation_ids(
                db_def.schema_def, [row.properties or {} for row in rows]
            )
            refs_de_orden = repo.row_refs(ids_relacion) if ids_relacion else {}
        rows = apply_view(
            rows,
            view_def.config or {},
            parent_key=key,
            view_type=getattr(view_def.type, "value", view_def.type),
            people={key: datos[0] for key, datos in (users or {}).items()},
            person_keys=claves_persona,
            closed_rule=db_def.closed,
            # El orden declarado de cada selección en modo `manual`: ordenar
            # por `prioridad` da alta, media, baja y no el alfabeto. Las bases
            # que no lo declaran ordenan por el texto natural de su tipo.
            orders=order_ranks(db_def.schema_def),
            # El tipo de cada propiedad: cada clave ordena según el suyo
            # (número, fecha, casilla, persona por nombre, relación por
            # título...). Sin esto todo se ordenaba como texto.
            types=tipos,
            # Los títulos de las filas enlazadas, solo si alguna clave de
            # orden es una relación (una consulta, no una por fila).
            refs=refs_de_orden,
        )
    elif key:
        # «Todas» no tiene vista que diga el nivel, así que se ven todas las
        # filas; pero el orden sí es el de la jerarquía: una hija detrás de su
        # madre, como en cualquier vista de la base.
        rows = hierarchy.arrange(rows, key)

    return _serialize_rows(
        repo, db_def, rows, parent_key=key, counts=counts, users=users
    )


@router.get("/{database_id}/rows/{row_id}", response_model=schemas.RowPageRead)
def get_row(
    database_id: uuid.UUID, row_id: uuid.UUID, repo: Repository = Depends(get_repo)
):
    """Una fila con su cuerpo: la lectura que abre la fila como página.

    La lista (`GET .../rows`) sigue devolviendo `RowRead` sin cuerpo: cien
    filas no tienen por qué traer cien cuerpos que la tabla no enseña.
    """
    db_def = _get_database_or_404(repo, database_id)
    return _serialize_row_page(repo, db_def, _get_row_or_404(repo, database_id, row_id))


@router.patch(
    "/{database_id}/rows/{row_id}",
    response_model=schemas.RowRead,
    responses={409: {"description": "`If-Match` con una versión que ya no es la actual"}},
)
def update_row(
    database_id: uuid.UUID,
    row_id: uuid.UUID,
    payload: schemas.RowUpdate,
    repo: Repository = Depends(get_repo),
    if_match: str | None = Header(default=None),
):
    expected = parse_if_match(if_match)
    db_def = _get_database_or_404(repo, database_id)
    _get_row_or_404(repo, database_id, row_id)

    cleaned = None
    if payload.properties is not None:
        cleaned = validate_properties(
            db_def.schema_def, payload.properties, refs=repo.row_refs, users=repo.user_refs
        )
        # Con la fila delante, la comprobación completa: ni su propia madre ni
        # una descendiente suya (ciclo).
        hierarchy.validate_parent(
            db_def.schema_def,
            cleaned,
            row_id=str(row_id),
            rows=repo.list_rows(database_id),
        )

    # El cuerpo es del `.md` de la fila, como el de una nota: `""` lo vacía,
    # ausente (o `null`) no lo toca. Sin `model_fields_set`, un PATCH de una
    # celda —que no manda la clave— borraría el cuerpo de la fila.
    body = UNSET
    if "body_markdown" in payload.model_fields_set and payload.body_markdown is not None:
        body = payload.body_markdown

    try:
        with _vault_errors():
            row = repo.update_row(
                database_id,
                row_id,
                title=payload.title,
                icon=payload.icon,
                properties=cleaned,
                body_markdown=body,
                expected_version=expected,
            )
    except VersionConflict as exc:
        actual = _get_row_or_404(repo, database_id, row_id)
        return conflict(_serialize_row_page(repo, db_def, actual), exc.current, exc.expected)
    if row is None:
        raise HTTPException(404, "Fila no encontrada")
    publish_database(database_id, "rows")
    return _serialize_row(repo, db_def, row)


@router.delete("/{database_id}/rows/{row_id}", status_code=204)
def delete_row(
    database_id: uuid.UUID, row_id: uuid.UUID, repo: Repository = Depends(get_repo)
):
    with _vault_errors():
        borrada = repo.delete_row(database_id, row_id)
    if not borrada:
        raise HTTPException(404, "Fila no encontrada")
    publish_database(database_id, "rows")


# -------------------------------------------------------------------- views

@router.post("/{database_id}/views", response_model=schemas.ViewRead)
def create_view(
    database_id: uuid.UUID, payload: schemas.ViewCreate, repo: Repository = Depends(get_repo)
):
    db_def = _get_database_or_404(repo, database_id)
    # El rango de un calendario se valida contra el schema de la base, que el
    # esquema de la petición no tiene: una vista que nombra una fecha que no
    # existe es un 400 que dice cuál, no un calendario que cae a otra fecha.
    motivo = validar_rango(payload.config, db_def.schema_def)
    if motivo:
        raise HTTPException(400, motivo)
    with _vault_errors():
        view = repo.create_view(database_id, name=payload.name, type=payload.type, config=payload.config)
    publish_database(database_id, "updated")
    return _serialize_view(view)


@router.get("/{database_id}/views", response_model=List[schemas.ViewRead])
def list_views(database_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    _get_database_or_404(repo, database_id)
    return [_serialize_view(v) for v in repo.list_views(database_id)]


@router.patch("/{database_id}/views/{view_id}", response_model=schemas.ViewRead)
def update_view(
    database_id: uuid.UUID,
    view_id: uuid.UUID,
    payload: schemas.ViewUpdate,
    repo: Repository = Depends(get_repo),
):
    db_def = _get_database_or_404(repo, database_id)
    motivo = validar_rango(payload.config, db_def.schema_def)
    if motivo:
        raise HTTPException(400, motivo)
    with _vault_errors():
        view = repo.update_view(
            database_id, view_id, name=payload.name, type=payload.type, config=payload.config
        )
    if view is None:
        raise HTTPException(404, "Vista no encontrada")
    publish_database(database_id, "updated")
    return _serialize_view(view)


@router.delete("/{database_id}/views/{view_id}", status_code=204)
def delete_view(
    database_id: uuid.UUID, view_id: uuid.UUID, repo: Repository = Depends(get_repo)
):
    """Borra una vista; el frontmatter de `Bases/<slug>.md` se reescribe con el
    resto en la misma operación (el `.md` es la verdad: no hay otra copia).

    La última vista no se borra: es la pestaña con la que la base se abre y la
    que el bloque incrustado de una nota puede nombrar. Una base sin vistas es
    un estado válido (se queda en «Todas»), pero es el de una base creada sin
    ninguna, no algo a lo que se llegue borrando la única que había. Las filas
    no dependen de la vista: no se toca ninguna.
    """
    if repo.get_view(database_id, view_id) is None:
        raise HTTPException(404, "Vista no encontrada")
    if len(repo.list_views(database_id)) <= 1:
        raise HTTPException(
            409,
            "No se puede borrar la última vista: una base necesita al menos una. "
            "Las filas no se tocan: los datos son de la base, no de la vista.",
        )
    with _vault_errors():
        borrada = repo.delete_view(database_id, view_id)
    if not borrada:
        raise HTTPException(404, "Vista no encontrada")
    publish_database(database_id, "updated")


# ------------------------------------------------------------- aplicar vista
# `apply_view` (filtros y orden de una vista) vive en `app.views`: lo usan este
# router y la exportación a Markdown, y no puede haber dos versiones del filtro.
