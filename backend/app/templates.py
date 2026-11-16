"""Plantillas de serie: proyectos, tareas, CRM y reuniones.

Deliberadamente no son código nuevo del motor: son **datos** —un `schema_def`,
unas `views`, la regla de qué cuenta como cerrada y las plantillas de fila— que
se pasan a las mismas operaciones de creación que ya existían (crear database,
crear view). Copiar una plantilla es crear una base nueva: lo que se escribe en
su `.md` es una copia, así que cambiar la plantilla después no cambia las bases
ya creadas.

Dos decisiones que la tarjeta pide explícitamente:

- **Sin filas de ejemplo.** Una base nace vacía. Una fila «Tarea de ejemplo» hay
  que borrarla en cada proyecto y, mientras está, cuenta en los contadores y en
  las vistas: es trabajo de mentira. Lo que sí trae la plantilla son
  **plantillas de fila** (`row_templates`), que no son datos: no existen hasta
  que alguien crea una fila con ellas.
- **Nada de plantillas que mientan.** Antes de escribir nada se valida el
  schema (tipos y opciones que el código conoce), la regla `closed`, las vistas
  (agrupación, fechas, filtros, orden y anchos sobre propiedades que existen) y
  las plantillas de fila (claves y valores reales). Un tipo o un valor que el
  código no conoce es un 400 con el motivo y la base no se crea a medias.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from fastapi import HTTPException

from app import closed as closed_rules
from app.enums import ViewType
from app.schemas import PropertyType

# Tipos que una plantilla puede declarar. Es el contrato del motor: si el
# código no conoce el tipo, la plantilla miente y no se crea.
TYPE_VALUES = {tipo.value for tipo in PropertyType}

# Una propiedad de relación apunta a una base que se crea después (no puede
# venir en la plantilla); una de subtareas se declara en el schema, pero no se
# le puede dar un valor por defecto: el id de la madre no existe al crear una
# fila. Y una persona se guarda por el id de un usuario, que no es portable.
NO_DEFAULT_TYPES = {"relation", "parent_row", "person"}

# Los operadores que entiende `app.views` (los mismos que ofrece la interfaz).
FILTER_OPS = {
    "eq",
    "neq",
    "contains",
    "includes",
    "is_empty",
    "is_not_empty",
    "checked",
    "unchecked",
    "gt",
    "gte",
    "lt",
    "lte",
    "before",
    "after",
    "is_closed",
    "is_open",
}

SELECT_TYPES = {"select", "multiselect"}
SORTABLE_TYPES = {
    "select",
    "multiselect",
    "number",
    "date",
    "text",
    "url",
    "checkbox",
    "person",
}

_TIPOS_VISIBLES = {
    "text": "texto",
    "number": "número",
    "select": "selección",
    "multiselect": "selección múltiple",
    "date": "fecha",
    "checkbox": "casilla",
    "relation": "relación",
    "url": "URL",
    "parent_row": "subtareas",
    "person": "persona",
}


def _tipo(prop: dict) -> str:
    raw = prop.get("type")
    return str(getattr(raw, "value", raw) or "")


def _propiedades(schema: list[dict]) -> dict[str, dict]:
    return {
        str(p["key"]): p
        for p in schema or []
        if isinstance(p, dict) and p.get("key")
    }


def _opciones(prop: dict) -> list[str]:
    return [str(o) for o in (prop.get("options") or [])]


def _error(title: str, motivo: str) -> HTTPException:
    return HTTPException(400, f"La plantilla «{title}» no es válida: {motivo}")


def _sin_prefijo(detalle: str, title: str) -> str:
    """El motivo de un 400 de plantilla, sin la coletilla «La plantilla ...».

    El mismo texto sirve para el `detail` de la API y para la línea que la
    interfaz enseña junto a una plantilla de fila que ya no encaja; el prefijo
    sobra en lo segundo.
    """
    prefijo = f"La plantilla «{title}» no es válida: "
    return detalle[len(prefijo):] if detalle.startswith(prefijo) else detalle


# --------------------------------------------------------------------------
# validación
# --------------------------------------------------------------------------

def validate_template(template: dict) -> None:
    """Valida una plantilla entera ANTES de escribir nada.

    `template` trae ya el `schema_def` con sus claves definitivas (el router
    aplica `plan_schema` antes). Cualquier cosa que el motor no sepa reproducir
    —un tipo desconocido, una opción que no está, una vista que agrupa por una
    propiedad que no existe— es un 400 con el motivo. No se crea nada.
    """
    title = str(template.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "La plantilla no tiene nombre")
    schema = template.get("schema_def") or []
    if not isinstance(schema, list) or not schema:
        raise _error(title, "no declara ninguna propiedad")

    parent_rows = 0
    for prop in schema:
        if not isinstance(prop, dict):
            raise _error(title, f"propiedad inválida: {prop!r}")
        name = str(prop.get("name") or "").strip()
        tipo = _tipo(prop)
        if not name:
            raise _error(title, "hay una propiedad sin nombre")
        if tipo not in TYPE_VALUES:
            raise _error(
                title,
                f"«{name}» declara el tipo «{tipo or '(vacío)'}», que la app no "
                f"conoce. Los tipos que hay: {', '.join(sorted(TYPE_VALUES))}.",
            )
        if tipo in SELECT_TYPES and not _opciones(prop):
            raise _error(
                title,
                f"«{name}» es una {_TIPOS_VISIBLES[tipo]} sin opciones: elige al "
                "menos un valor.",
            )
        if tipo == "parent_row":
            parent_rows += 1
    if parent_rows > 1:
        raise _error(
            title,
            "declara dos propiedades de subtareas y una base solo puede tener "
            "una: no se sabría de qué columna cuelga una fila.",
        )

    _validate_closed(template, schema, title)
    _validate_views(template.get("views") or [], schema, title)
    validate_row_templates(template.get("row_templates") or [], schema, title)


def _validate_closed(template: dict, schema: list[dict], title: str) -> None:
    regla = template.get("closed")
    if regla in (None, {}):
        return
    if not isinstance(regla, dict):
        raise _error(title, "el bloque `closed` no es un mapa")
    try:
        closed_rules.validate(
            schema,
            regla.get(closed_rules.PROPERTY_KEY),
            regla.get(closed_rules.VALUES_KEY),
        )
    except HTTPException as exc:
        raise _error(title, str(exc.detail)) from exc


def _validate_views(views: Any, schema: list[dict], title: str) -> None:
    if not isinstance(views, list):
        raise _error(title, "`views` no es una lista")
    props = _propiedades(schema)
    tipos = {tipo.value for tipo in ViewType}
    for position, view in enumerate(views):
        if not isinstance(view, dict):
            raise _error(title, f"la vista #{position + 1} no es un mapa")
        name = str(view.get("name") or "").strip()
        if not name:
            raise _error(title, f"la vista #{position + 1} no tiene nombre")
        vtype = str(getattr(view.get("type"), "value", view.get("type")) or "table")
        if vtype not in tipos:
            raise _error(
                title,
                f"la vista «{name}» es de tipo «{vtype}», que la app no conoce "
                f"({', '.join(sorted(tipos))}).",
            )
        config = view.get("config") or {}
        if not isinstance(config, dict):
            raise _error(title, f"el `config` de la vista «{name}» no es un mapa")

        group_by = str(config.get("group_by") or "").strip()
        if group_by:
            if group_by not in props:
                raise _error(
                    title,
                    f"la vista «{name}» agrupa por «{group_by}», que no es una "
                    "propiedad de la plantilla.",
                )
            if _tipo(props[group_by]) not in SELECT_TYPES | {"checkbox"}:
                raise _error(
                    title,
                    f"la vista «{name}» agrupa por «{props[group_by].get('name')}», "
                    "que no es una selección ni una casilla.",
                )

        date_property = str(config.get("date_property") or "").strip()
        if date_property:
            if date_property not in props:
                raise _error(
                    title,
                    f"la vista «{name}» pone el calendario sobre «{date_property}», "
                    "que no es una propiedad de la plantilla.",
                )
            if _tipo(props[date_property]) != "date":
                raise _error(
                    title,
                    f"la vista «{name}» pone el calendario sobre "
                    f"«{props[date_property].get('name')}», que no es una fecha.",
                )

        for filtro in config.get("filters") or []:
            if not isinstance(filtro, dict):
                raise _error(title, f"la vista «{name}» tiene un filtro inválido")
            op = str(filtro.get("op") or "eq")
            if op not in FILTER_OPS:
                raise _error(
                    title,
                    f"la vista «{name}» usa el operador «{op}», que la app no "
                    "conoce.",
                )
            if op in {"is_closed", "is_open"}:
                # Quién está cerrada lo dice la regla de la base; un `key` aquí
                # no hace falta y, si viene, tiene que existir.
                key = str(filtro.get("key") or "").strip()
                if key and key not in props:
                    raise _error(
                        title,
                        f"la vista «{name}» filtra por «{key}», que no es una "
                        "propiedad de la plantilla.",
                    )
                continue
            key = str(filtro.get("key") or "").strip()
            if not key or key not in props:
                raise _error(
                    title,
                    f"la vista «{name}» filtra por «{key}», que no es una "
                    "propiedad de la plantilla.",
                )
            if op not in {"is_empty", "is_not_empty", "checked", "unchecked"}:
                value = filtro.get("value")
                if value is None or value == "":
                    raise _error(
                        title,
                        f"la vista «{name}» filtra por «{props[key].get('name')}» "
                        f"con el operador «{op}» y sin valor.",
                    )
                if _tipo(props[key]) == "select" and _opciones(props[key]):
                    if str(value) not in _opciones(props[key]):
                        raise _error(
                            title,
                            f"la vista «{name}» filtra por «{value}», que no es un "
                            f"valor de «{props[key].get('name')}» "
                            f"({', '.join(_opciones(props[key]))}).",
                        )

        for clave in _sort_clauses(config.get("sort")):
            key = str(clave.get("key") or "").strip()
            if not key or key not in props:
                raise _error(
                    title,
                    f"la vista «{name}» ordena por «{key}», que no es una "
                    "propiedad de la plantilla.",
                )
            if _tipo(props[key]) not in SORTABLE_TYPES:
                raise _error(
                    title,
                    f"la vista «{name}» ordena por «{props[key].get('name')}», "
                    "que no es una propiedad ordenable.",
                )
            if str(clave.get("dir") or "asc") not in {"asc", "desc"}:
                raise _error(
                    title,
                    f"la vista «{name}» ordena por «{key}» en una dirección que "
                    "la app no conoce (asc o desc).",
                )

        for clave in ("visible_properties", "order"):
            for key in config.get(clave) or []:
                if str(key) not in props:
                    raise _error(
                        title,
                        f"la vista «{name}» nombra la propiedad «{key}» en "
                        f"`{clave}`, y la plantilla no la declara.",
                    )

        widths = config.get("widths") or {}
        if not isinstance(widths, dict):
            raise _error(title, f"los `widths` de la vista «{name}» no son un mapa")
        for key, ancho in widths.items():
            if str(key) not in props:
                raise _error(
                    title,
                    f"la vista «{name}» da ancho a «{key}», que no es una "
                    "propiedad de la plantilla.",
                )
            try:
                valor = int(str(ancho).strip())
            except (TypeError, ValueError):
                valor = 0
            if valor <= 0:
                raise _error(
                    title,
                    f"la vista «{name}» da a «{key}» un ancho que no es un número "
                    "positivo de píxeles.",
                )


def _sort_clauses(raw: Any) -> list[dict]:
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [clave for clave in raw if isinstance(clave, dict)]


# --------------------------------------------------------------------------
# plantillas de fila
# --------------------------------------------------------------------------

def _valor_por_defecto(prop: dict, valor: Any, title: str) -> Any:
    """Un valor por defecto, coaccionado al tipo de su propiedad o 400.

    El `.md` no infiere tipos (`true` es el texto `"true"`), así que aquí se
    normaliza igual que al guardar una celda: la plantilla que se lee del
    fichero y la que llega de la interfaz se comportan igual.

    Qué se valida es **lo que el control del tipo no puede representar**, no lo
    que parece mal escrito: una opción que ya no está en `options`, una fecha que
    el `<input type="date">` deja en blanco (y la celda nacería vacía sin decir
    nada) o un número que no lo es. `text` y `url` no se validan a propósito: el
    editor los pinta en un `<input type="text">` que conserva cualquier cosa y
    una celda `url` acepta el mismo texto tal cual (`app.properties._coerce` no
    valida formato), así que una plantilla más estricta que la celda rechazaría
    plantillas del `.md` que hoy crean filas —y rompería ficheros del usuario que
    no han perdido nada—. Validar «esto no parece una URL» es una decisión de
    producto que hay que tomar en la celda primero.
    """
    nombre = prop.get("name") or prop.get("key")
    tipo = _tipo(prop)
    if tipo in NO_DEFAULT_TYPES:
        raise _error(
            title,
            f"«{nombre}» es de tipo {_TIPOS_VISIBLES.get(tipo, tipo)} y no puede "
            "traer un valor por defecto: no se puede escribir sin el id de una "
            "fila o de un usuario.",
        )
    if tipo == "select":
        opciones = _opciones(prop)
        valor = str(valor).strip()
        canonica = {o.casefold(): o for o in opciones}
        real = canonica.get(valor.casefold())
        if real is None:
            raise _error(
                title,
                f"«{valor}» no es un valor de «{nombre}». Los que hay: "
                f"{', '.join(opciones)}.",
            )
        return real
    if tipo == "multiselect":
        opciones = _opciones(prop)
        canonica = {o.casefold(): o for o in opciones}
        if not isinstance(valor, list):
            raise _error(
                title, f"«{nombre}» es una selección múltiple: su valor es una lista."
            )
        elegidos: list[str] = []
        for item in valor:
            real = canonica.get(str(item).strip().casefold())
            if real is None:
                raise _error(
                    title,
                    f"«{item}» no es un valor de «{nombre}». Los que hay: "
                    f"{', '.join(opciones)}.",
                )
            if real not in elegidos:
                elegidos.append(real)
        return elegidos
    if tipo == "checkbox":
        if isinstance(valor, bool):
            return valor
        texto = str(valor).strip().lower()
        if texto in {"true", "1", "yes", "on", "si", "sí", "x"}:
            return True
        if texto in {"false", "0", "no", "off", ""}:
            return False
        raise _error(title, f"«{nombre}» es una casilla: su valor es true o false.")
    if tipo == "number":
        try:
            return float(valor)
        except (TypeError, ValueError):
            raise _error(title, f"«{nombre}» es un número y «{valor}» no lo es.")
    if isinstance(valor, (dict, list)):
        raise _error(
            title,
            f"«{nombre}» es de tipo {_TIPOS_VISIBLES.get(tipo, tipo)} y su valor "
            "no puede ser una lista ni un mapa.",
        )
    if tipo == "date":
        # Una fecha que el `<input type="date">` rechaza deja la celda vacía y
        # la fila nacería sin el dato, sin decir nada: mejor un 400 aquí, que
        # es donde se puede explicar.
        texto = str(valor).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto):
            raise _error(
                title, f"«{nombre}» es una fecha: su valor es AAAA-MM-DD, no «{texto}»."
            )
        try:
            date.fromisoformat(texto)
        except ValueError as exc:
            raise _error(
                title, f"«{nombre}» tiene una fecha que no existe: «{texto}»."
            ) from exc
        return texto
    # `text` y `url`: tal cual (ver el docstring: el control los conserva).
    return str(valor)


def validate_row_templates(
    templates: list, schema: list[dict], title: str = "Plantillas de fila"
) -> list[dict]:
    """Normaliza y valida las plantillas de fila contra el schema.

    Devuelve la lista que se escribe en el `.md` (valores ya coaccionados) o
    lanza 400 con el motivo. Los nombres no se repiten: son la forma de elegir
    la plantilla en el alta y dos iguales serían ambiguas.
    """
    if templates in (None, ""):
        return []
    if not isinstance(templates, list):
        raise _error(title, "las plantillas de fila no son una lista")
    salida: list[dict] = []
    vistos: set[str] = set()
    props = _propiedades(schema)
    for position, template in enumerate(templates):
        if not isinstance(template, dict):
            raise _error(title, f"la plantilla de fila #{position + 1} no es un mapa")
        name = str(template.get("name") or "").strip()
        if not name:
            raise _error(title, f"la plantilla de fila #{position + 1} no tiene nombre")
        if name.casefold() in vistos:
            raise _error(title, f"hay dos plantillas de fila llamadas «{name}»")
        vistos.add(name.casefold())
        properties = template.get("properties")
        if properties in (None, ""):
            properties = {}
        if not isinstance(properties, dict):
            raise _error(title, f"`properties` de «{name}» no es un mapa")
        limpias: dict[str, Any] = {}
        for key, value in properties.items():
            prop = props.get(str(key))
            if prop is None:
                raise _error(
                    title,
                    f"la plantilla de fila «{name}» da valor a «{key}», que no es "
                    "una propiedad de la base.",
                )
            if value is None:
                continue
            limpias[str(key)] = _valor_por_defecto(prop, value, title)
        body = template.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise _error(title, f"`body` de «{name}» no es texto")
        salida.append({"name": name, "properties": limpias, "body": body})
    return salida


def apply_row_template(template: dict, schema: list[dict]) -> tuple[dict, str]:
    """Los valores y el cuerpo de una plantilla, listos para crear la fila.

    Vuelve a validar contra el schema actual: el `.md` es del usuario y puede
    haber cambiado desde que se indexó, así que una plantilla que ya no encaja
    es un 400 con el motivo en vez de una fila a medias.
    """
    validadas = validate_row_templates([template], schema)
    if not validadas:
        raise HTTPException(400, "La plantilla de fila no tiene nombre")
    return validadas[0]["properties"], validadas[0]["body"]


def motivo_de_row_template(template: dict, schema: list[dict]) -> str | None:
    """Por qué una plantilla de fila guardada ya no encaja con su base.

    `None` = encaja. El `.md` es del usuario: puede haberlo escrito a mano, o el
    schema puede haber cambiado desde que se guardó. La plantilla no se toca al
    leerla (el fichero manda), pero quien la va a usar —el alta de fila— tiene
    que saberlo antes de crear nada a medias.
    """
    props = _propiedades(schema)
    for key in (template.get("properties") or {}):
        if str(key) not in props:
            return f"«{key}» ya no es una propiedad de la base."
    try:
        validate_row_templates([template], schema, "Plantillas de fila")
    except HTTPException as exc:
        return _sin_prefijo(str(exc.detail), "Plantillas de fila")
    return None


def resolve_row_templates(templates: list, schema: list[dict]) -> list[dict]:
    """Las plantillas de fila de una base, con sus valores ya coaccionados.

    El `.md` no infiere tipos: `false`, `3` o una fecha llegan como texto. Quien
    pinta un control con ese texto lo interpreta a su manera (`Boolean("false")`
    es `true`), así que la coerción se hace **una vez, aquí**, con las mismas
    reglas con las que se aplican al crear la fila.

    Devuelve, por plantilla, `{name, properties, body, values, error}`:

    - `properties`: lo que dice el `.md`, tal cual (la forma que se reescribe);
    - `values`: los valores coaccionados de las propiedades que sí encajan;
    - `error`: el motivo por el que la plantilla no se puede usar, o `None`.
      Un valor que ya no es válido no entra en `values` —pintarlo en un control
      que no lo puede representar lo perdería en silencio— y la interfaz lo
      enseña al lado de la plantilla.
    """
    props = _propiedades(schema)
    salida: list[dict] = []
    for template in templates or []:
        if not isinstance(template, dict):
            continue
        name = str(template.get("name") or "").strip()
        properties = template.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        body = template.get("body")
        body = body if isinstance(body, str) else ""
        values: dict[str, Any] = {}
        for key, valor in properties.items():
            prop = props.get(str(key))
            if prop is None or valor is None:
                continue
            try:
                values[str(key)] = _valor_por_defecto(prop, valor, name or "Plantillas")
            except HTTPException:
                continue
        salida.append(
            {
                "name": name,
                "properties": properties,
                "body": body,
                "values": values,
                "error": motivo_de_row_template(
                    {"name": name, "properties": properties, "body": body}, schema
                ),
            }
        )
    return salida


def adapt_row_templates(
    templates: list, schema: list[dict], mapping: dict[str, str] | None = None
) -> list[dict]:
    """Las plantillas de fila que siguen encajando tras cambiar el schema.

    Se usa al guardar el schema de la base, como las vistas: un rename
    traduce las claves (`mapping`), una propiedad borrada deja de aparecer y un
    valor que el tipo nuevo ya no admite se retira. En silencio a propósito —
    nadie está editando la plantilla en esa petición y guardar el resto de la
    base no puede fallar por ella—, pero sin dejar en el `.md` una plantilla que
    mienta: el alta se negaría a usarla.
    """
    props = _propiedades(schema)
    salida: list[dict] = []
    for template in templates or []:
        if not isinstance(template, dict):
            continue
        name = str(template.get("name") or "").strip()
        if not name:
            continue
        body = template.get("body")
        body = body if isinstance(body, str) else ""
        limpias: dict[str, Any] = {}
        for key, valor in (template.get("properties") or {}).items():
            clave = str(key)
            if mapping:
                clave = mapping.get(clave, clave)
            if clave is None or clave not in props or valor is None:
                continue
            try:
                limpias[clave] = _valor_por_defecto(props[clave], valor, name)
            except HTTPException:
                continue
        salida.append({"name": name, "properties": limpias, "body": body})
    return salida


# --------------------------------------------------------------------------
# plantillas de serie
# --------------------------------------------------------------------------

ESTADO = {
    "key": "estado",
    "name": "Estado",
    "type": "select",
    "options": ["abierta", "en curso", "en revisión", "hecha"],
    "sort_order": ["abierta", "en curso", "en revisión", "hecha"],
}

PRIORIDAD = {
    "key": "prioridad",
    "name": "Prioridad",
    "type": "select",
    "options": ["alta", "media", "baja"],
    "sort_order": ["alta", "media", "baja"],
}

TEMPLATES: dict[str, dict] = {
    "proyecto": {
        "title": "Proyecto",
        "description": (
            "Tareas con estado, responsable, prioridad y fechas; las subtareas "
            "cuelgan de su tarea. Tabla, tablero por estado y calendario."
        ),
        "schema_def": [
            ESTADO,
            {"key": "responsable", "name": "Responsable", "type": "person"},
            PRIORIDAD,
            {"key": "inicio", "name": "Inicio", "type": "date"},
            {"key": "entrega", "name": "Entrega", "type": "date"},
            {"key": "parte_de", "name": "Subtarea de", "type": "parent_row"},
        ],
        "closed": {"property": "estado", "values": ["hecha"]},
        "views": [
            {
                "name": "Tabla",
                "type": "table",
                "config": {
                    "order": ["estado", "responsable", "prioridad", "entrega"],
                    "sort": [{"key": "entrega", "dir": "asc"}],
                    "widths": {
                        "estado": 140,
                        "responsable": 170,
                        "prioridad": 110,
                        "entrega": 130,
                    },
                },
            },
            {
                # Lo hecho sale del tablero: para verlo está la tabla. Es la
                # configuración que la tarjeta pide de serie (agrupación +
                # filtros) y no un `is_closed`, que depende de la regla y no
                # debe cambiar de forma si la base la mueve.
                "name": "Tablero",
                "type": "board",
                "config": {
                    "group_by": "estado",
                    "filters": [{"key": "estado", "op": "neq", "value": "hecha"}],
                },
            },
            {
                # Un calendario solo pinta filas con fecha: sin este filtro, la
                # vista arrastra las que no tienen entrega y no salen.
                "name": "Calendario",
                "type": "calendar",
                "config": {
                    "date_property": "entrega",
                    "filters": [{"key": "entrega", "op": "is_not_empty"}],
                },
            },
        ],
        "row_templates": [
            {
                "name": "Revisión semanal",
                "properties": {"estado": "abierta", "prioridad": "media"},
                "body": "## Qué revisar\n\n- \n",
            },
            {
                "name": "Tarea urgente",
                "properties": {"estado": "abierta", "prioridad": "alta"},
                "body": "## Qué hay que hacer\n",
            },
        ],
    },
    "tasks": {
        "title": "Tareas sueltas",
        "description": (
            "Lo justo para apuntar tareas: estado, prioridad y fecha. Tabla y "
            "tablero por estado."
        ),
        "schema_def": [
            {
                "key": "estado",
                "name": "Estado",
                "type": "select",
                "options": ["pendiente", "hecha"],
                "sort_order": ["pendiente", "hecha"],
            },
            PRIORIDAD,
            {"key": "fecha", "name": "Fecha", "type": "date"},
        ],
        "closed": {"property": "estado", "values": ["hecha"]},
        "views": [
            {
                "name": "Tabla",
                "type": "table",
                "config": {
                    "order": ["estado", "prioridad", "fecha"],
                    "sort": [{"key": "fecha", "dir": "asc"}],
                    "widths": {"estado": 140, "prioridad": 110, "fecha": 130},
                },
            },
            {
                "name": "Tablero",
                "type": "board",
                "config": {
                    "group_by": "estado",
                    "filters": [{"key": "estado", "op": "neq", "value": "hecha"}],
                },
            },
        ],
        "row_templates": [
            {
                "name": "Tarea rápida",
                "properties": {"estado": "pendiente", "prioridad": "media"},
                "body": "",
            }
        ],
    },
    "crm": {
        "title": "CRM",
        "description": (
            "Contactos, empresas y oportunidades: fase del pipeline, valor y próximo "
            "contacto. Tablero, tabla y agenda."
        ),
        "schema_def": [
            {"key": "empresa", "name": "Empresa", "type": "text"},
            {
                "key": "fase",
                "name": "Fase",
                "type": "select",
                "options": ["lead", "contactado", "propuesta", "ganado", "perdido"],
                "sort_order": ["lead", "contactado", "propuesta", "ganado", "perdido"],
            },
            {"key": "valor", "name": "Valor", "type": "number"},
            {"key": "proximo_contacto", "name": "Próximo contacto", "type": "date"},
        ],
        # Ganado y perdido son el final de la oportunidad: «Mis tareas» no
        # enseña lo que ya está cerrado. El pipeline sigue viéndolo todo.
        "closed": {"property": "fase", "values": ["ganado", "perdido"]},
        "views": [
            {
                "name": "Pipeline",
                "type": "board",
                "config": {"group_by": "fase"},
            },
            {
                "name": "Tabla",
                "type": "table",
                "config": {
                    "order": ["empresa", "fase", "valor", "proximo_contacto"],
                    "sort": [{"key": "valor", "dir": "desc"}],
                    "widths": {
                        "empresa": 200,
                        "fase": 130,
                        "valor": 110,
                        "proximo_contacto": 150,
                    },
                },
            },
            {
                "name": "Agenda",
                "type": "calendar",
                "config": {
                    "date_property": "proximo_contacto",
                    "filters": [{"key": "proximo_contacto", "op": "is_not_empty"}],
                },
            },
        ],
        "row_templates": [
            {
                "name": "Nuevo contacto",
                "properties": {"fase": "lead"},
                "body": "## Contexto\n",
            }
        ],
    },
    "reuniones": {
        "title": "Reuniones",
        "description": (
            "Fecha, asistente (una persona del equipo) y notas de la reunión. "
            "Tabla y calendario."
        ),
        "schema_def": [
            {"key": "fecha", "name": "Fecha", "type": "date"},
            {"key": "asistente", "name": "Asistente", "type": "person"},
            {"key": "notas", "name": "Notas", "type": "text"},
        ],
        "views": [
            {
                "name": "Tabla",
                "type": "table",
                "config": {
                    "order": ["fecha", "asistente", "notas"],
                    "sort": [{"key": "fecha", "dir": "desc"}],
                    "widths": {"fecha": 130, "asistente": 170, "notas": 260},
                },
            },
            {
                "name": "Calendario",
                "type": "calendar",
                "config": {
                    "date_property": "fecha",
                    "filters": [{"key": "fecha", "op": "is_not_empty"}],
                },
            },
        ],
        "row_templates": [
            {
                "name": "Reunión de equipo",
                "properties": {},
                "body": "## Orden del día\n\n- \n\n## Notas\n\n",
            }
        ],
    },
}

# Orden en el que se ofrecen en la interfaz: el proyecto es el caso normal y
# «Tareas sueltas» la versión mínima; CRM y Reuniones van detrás.
TEMPLATE_ORDER = ("proyecto", "tasks", "crm", "reuniones")


def catalog() -> list[dict]:
    """Lo que enseña «Crear desde plantilla»: clave, nombre, descripción y forma.

    No se manda el `schema_def` entero: la interfaz solo necesita saber qué
    ofrece cada plantilla para elegirla. Los datos viven en el servidor y son
    los que se copian al `.md` al crear.
    """
    return [
        {
            "key": key,
            "title": TEMPLATES[key]["title"],
            "description": TEMPLATES[key].get("description", ""),
            "properties": [p["name"] for p in TEMPLATES[key]["schema_def"]],
            "views": [v["name"] for v in TEMPLATES[key]["views"]],
            "closed": TEMPLATES[key].get("closed"),
            "row_templates": [
                t["name"] for t in TEMPLATES[key].get("row_templates", [])
            ],
        }
        for key in TEMPLATE_ORDER
        if key in TEMPLATES
    ]
