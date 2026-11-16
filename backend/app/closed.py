"""Qué cuenta como «fila cerrada»: una regla de la base, no un marcador suelto.

Hasta ahora «cerrada» lo decía un marcador `done` metido dentro de una propiedad
del `schema` (`done: [Hecho]` en una selección, `done: true` en una casilla).
Ese marcador no se validaba contra las opciones reales de la propiedad —se podía
marcar como cerrada un valor que no existe y el contador no contaba nunca— y
vivía repartido por el schema.

La regla ahora es **una sola por base** y se declara en el frontmatter de
`Bases/<slug>.md`:

    closed:
      property: estado
      values: [hecha, cerrada]

- `property` es la clave de la propiedad que dice el estado (una selección o una
  casilla). Una base tiene **como mucho una**: si se marca otra, se mueve.
- `values` son los valores de esa propiedad que cuentan como cerrada. En una
  selección `values` no puede estar vacía y cada valor tiene que ser una opción
  real de la propiedad (`validate`); en una casilla no hay valores —«marcada»
  es el único estado que cierra— y `values` va vacía.
- Sin bloque `closed`, la base **no declara estado** y nada se considera
  cerrado: el contador sale «0 de N» y «Mis tareas» no esconde nada. No hay
  lista de respaldo escondida en el código: una sola verdad, la del `.md`.

**Compatibilidad con el `done` viejo.** Las bases que ya tenían marcadores
`done` se leen como si declararan `closed` (`legacy`), sin migración masiva y
sin reescribir sus ficheros. Al guardar la regla desde la interfaz se escribe el
bloque nuevo y los `done` viejos se retiran del `schema` en la misma escritura:
el formato nuevo es el único que la app produce.

La semántica se aplica en un solo sitio —este módulo— y todos los consumidores
la usan: el contador de subtareas (`app.hierarchy.child_counts`), el campo
`closed` de cada fila, los filtros de una vista (`is_closed` / `is_open`) y
`GET /my-tasks`. Cambiar los valores de la regla recalcula contadores sin tocar
el `.md` de ninguna fila; quitar el bloque no cierra nada.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from fastapi import HTTPException

# Tipos de propiedad que pueden hacer de estado. Un texto libre no: cualquier
# palabra contaría como cerrada.
STATUS_TYPES = ("select", "multiselect", "checkbox")

# El bloque del frontmatter y las claves que lo forman.
CLOSED_KEY = "closed"
PROPERTY_KEY = "property"
VALUES_KEY = "values"

# De dónde sale la regla efectiva de una base.
SOURCE_BASE = "base"
SOURCE_DONE = "done"

# Valores que el `.md` (YAML sin tipos) puede devolver donde se escribió `true`.
_TRUTHY = ("true", "1", "yes", "on", "si", "sí")


def type_of(prop: dict) -> str:
    """El tipo de una propiedad como texto.

    `type` llega del `.md` como texto, pero una petición de la API lo trae como
    `PropertyType` (un `str`-Enum) y `str(...)` de un enum da
    «PropertyType.select». Se compara siempre por su valor.
    """
    raw = prop.get("type")
    return str(getattr(raw, "value", raw) or "")


def _flag(raw: Any) -> bool:
    """`true` de verdad, venga como booleano o como el texto del `.md`."""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in _TRUTHY


def _values(raw: Any) -> list[str]:
    """La lista de valores, sin vacíos. Un valor suelto cuenta como lista de uno."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return [str(v).strip() for v in items if v is not None and str(v).strip()]


def normalize(raw: Any) -> Optional[dict[str, Any]]:
    """`{property, values}` a partir de lo que trae el `.md`, o `None`.

    Tolera lo que un fichero editado a mano puede tener: `values` como lista o
    como un valor suelto, y espacios de más. Un bloque sin `property` —o con una
    que no es texto— no declara nada: se lee como si no estuviera, que es la
    verdad (no hay propiedad de estado que aplicar).
    """
    if not isinstance(raw, dict):
        return None
    prop = raw.get(PROPERTY_KEY)
    if not isinstance(prop, str) or not prop.strip():
        return None
    return {PROPERTY_KEY: prop.strip(), VALUES_KEY: _values(raw.get(VALUES_KEY))}


def from_frontmatter(front: Optional[dict]) -> Optional[dict[str, Any]]:
    """La regla declarada en el frontmatter de la base, si la hay."""
    if not front:
        return None
    return normalize(front.get(CLOSED_KEY))


def legacy(schema_def: Iterable[dict]) -> Optional[dict[str, Any]]:
    """La regla que implican los marcadores `done` del schema, si hay alguno.

    Es la lectura compatible con las bases de antes: `done: true` en una casilla
    (marcada = cerrada) o `done: [valor, ...]` en una selección. Se queda con la
    PRIMERA propiedad marcada, en el orden del schema, para que dos marcadores no
    puedan dar dos respuestas distintas.
    """
    for prop in schema_def or []:
        if not isinstance(prop, dict) or not prop.get("key"):
            continue
        raw = prop.get("done")
        if isinstance(raw, list):
            valores = _values(raw)
            if valores:
                return {PROPERTY_KEY: str(prop["key"]), VALUES_KEY: valores}
        elif _flag(raw):
            return {PROPERTY_KEY: str(prop["key"]), VALUES_KEY: []}
    return None


def effective(
    declared: Optional[dict[str, Any]], schema_def: Iterable[dict]
) -> tuple[Optional[dict[str, Any]], str]:
    """La regla que manda en una base y de dónde sale.

    El bloque `closed` gana siempre; sin él, los marcadores `done` del schema
    (compatibilidad de lectura). Sin ninguno de los dos, `(None, "base")`: la
    base no declara estado.
    """
    if declared:
        return declared, SOURCE_BASE
    vieja = legacy(schema_def)
    if vieja:
        return vieja, SOURCE_DONE
    return None, SOURCE_BASE


def is_closed(rule: Optional[dict], properties: dict) -> bool:
    """¿La fila está cerrada según la regla de su base?

    Sin regla, nada está cerrado. Con valores, la celda vale uno de ellos (un
    `multiselect` cierra si **alguno** de sus valores está en la lista). Sin
    valores, la regla es una casilla: marcada = cerrada.
    """
    if not rule:
        return False
    key = rule.get(PROPERTY_KEY)
    if not key:
        return False
    value = (properties or {}).get(key)
    if value is None or value == "":
        return False
    cerradas = {str(v).strip().casefold() for v in (rule.get(VALUES_KEY) or [])}
    if not cerradas:
        return _flag(value)
    actuales = value if isinstance(value, list) else [value]
    return any(
        str(v).strip().casefold() in cerradas
        for v in actuales
        if v is not None and str(v).strip()
    )


def _rechazar(motivo: str) -> None:
    raise HTTPException(400, motivo)


def validate(
    schema_def: Iterable[dict], property_key: Any, values: Any
) -> dict[str, Any]:
    """La regla pedida, comprobada contra el schema; 400 si no vale.

    Es lo que impide el agujero de antes: un valor que no es una opción real de
    la propiedad no se guarda, así que no hay un `closed` que nunca cuenta. El
    mensaje dice qué valores sí existen y nombra la propiedad por su nombre
    visible (el usuario no ve claves).
    """
    key = str(property_key or "").strip()
    if not key:
        _rechazar("Falta la propiedad que dice si una fila está cerrada.")
    prop = next(
        (
            p
            for p in schema_def or []
            if isinstance(p, dict) and str(p.get("key") or "") == key
        ),
        None,
    )
    if prop is None:
        _rechazar(
            f"«{key}» no es una propiedad de esta base: elige la columna que "
            "dice si una fila está cerrada."
        )
    nombre = f"«{prop.get('name') or key}»"
    tipo = type_of(prop)
    if tipo not in STATUS_TYPES:
        _rechazar(
            f"{nombre} es de tipo {tipo}: solo una selección o una casilla "
            "puede decir si una fila está cerrada."
        )

    pedidos = _values(values)
    if tipo == "checkbox":
        if pedidos:
            _rechazar(
                f"{nombre} es una casilla: se cierra cuando está marcada, sin "
                "elegir valores."
            )
        return {PROPERTY_KEY: key, VALUES_KEY: []}

    opciones = _values(prop.get("options"))
    if not opciones:
        _rechazar(
            f"{nombre} no tiene opciones: añádelas antes de usarla como estado."
        )
    # Se compara sin mayúsculas ni espacios de sobra, pero se guarda la grafía
    # de la opción: el valor de la regla tiene que ser el de la celda.
    canonica = {opcion.casefold(): opcion for opcion in opciones}
    if not pedidos:
        _rechazar(
            f"{nombre}: elige al menos un valor de los que cuentan como cerrada "
            f"({', '.join(opciones)})."
        )
    elegidos: list[str] = []
    desconocidos: list[str] = []
    for valor in pedidos:
        real = canonica.get(valor.casefold())
        if real is None:
            desconocidos.append(valor)
        elif real not in elegidos:
            elegidos.append(real)
    if desconocidos:
        _rechazar(
            f"{nombre}: {' y '.join(f'«{v}»' for v in desconocidos)} no es un "
            f"valor de esa propiedad. Los que hay: {', '.join(opciones)}."
        )
    return {PROPERTY_KEY: key, VALUES_KEY: elegidos}


def without_legacy(schema_def: Iterable[dict]) -> list[dict]:
    """El schema sin los marcadores `done`: el formato nuevo los retira.

    Se usa al escribir la regla: el bloque `closed` es la única declaración, y
    dejar marcadores sueltos al lado sería una segunda verdad que reviviría si
    mañana se quitara el bloque.
    """
    return [
        {k: v for k, v in prop.items() if k != "done"} if isinstance(prop, dict) else prop
        for prop in schema_def or []
    ]
