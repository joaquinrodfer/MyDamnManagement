"""Tipos de base: Proyectos y Tareas (plan H4).

Una base normal es libre: cada equipo pone las columnas que quiere. Una base
**tipada** sigue siendo libre (se pueden añadir, editar y borrar todas las
columnas que se quiera) pero además *declara qué columna hace de qué* —quién
es el responsable, cuál es el estado, cuándo empieza y acaba, cuántas horas
cuesta—, y con eso se puede calcular la carga de trabajo de todo el equipo sin
saber cómo llamó cada equipo a sus columnas.

Se declara en el frontmatter de la base, como la regla de «cerrado»:

    kind:
      type: tasks
      roles: {assignee: responsable, status: estado, start: inicio, end: fin, hours: horas}
      options: {hours_split: equal}

- `type`: `projects`, `tasks` o `workload` (C6: cualquier base que cuenta
  para la carga, con solo el responsable obligatorio). Cada tipo es una **ficha** de este módulo:
  qué roles lleva (obligatorios y opcionales), con qué tipo de propiedad y las
  opciones que admite. Añadir un tipo nuevo es añadir otra ficha.
- `roles`: `{rol: clave de la propiedad}`. Las columnas de un rol **se pueden
  renombrar y ocultar, pero no borrar ni cambiar de tipo** mientras la base
  tenga el tipo (`check_schema`): se quitaría lo que el cálculo necesita.
- `options`: ajustes del tipo. En Tareas, `hours_split` (cómo se reparten las
  horas entre varios responsables: `full` = completas a cada uno, `equal` = a
  partes iguales, `dedication` = según su dedicación al espacio) y `spread`
  (cómo se reparten en el tiempo: `uniform`, `start`, `end`, `due`; C5).

Una fecha de la plantilla es un par de columnas (Inicio y Fin): la propiedad
`date` del motor guarda un día, no un rango.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from fastapi import HTTPException

KIND_TYPES = ("projects", "tasks", "workload")

# Tipo de propiedad que exige cada rol.
ROLE_TYPES = {
    "assignee": "person",
    "status": "select",
    "start": "date",
    "end": "date",
    "hours": "number",
    "project": "relation",
    "estimate": "number",
    "size": "select",
}

ROLE_LABELS = {
    "assignee": "Responsable",
    "status": "Estado",
    "start": "Inicio",
    "end": "Fin",
    "hours": "Horas",
    "project": "Proyecto",
    "estimate": "Estimación (h)",
    "size": "Talla",
}

# Nombres que suelen llevar esas columnas en bases ya hechas, para proponer la
# correspondencia al convertir una base existente.
ROLE_SYNONYMS = {
    "assignee": ("responsable", "responsables", "asignado", "asignada", "assignee", "encargado", "owner"),
    "status": ("estado", "status", "situación", "situacion"),
    "start": ("inicio", "fecha de inicio", "empieza", "start", "desde"),
    "end": ("fin", "entrega", "fecha límite", "fecha limite", "vencimiento", "plazo", "end", "due", "hasta", "fecha"),
    "hours": ("horas", "esfuerzo", "estimación", "estimacion", "hours", "effort", "puntos"),
    "project": ("proyecto", "project"),
    "estimate": ("estimación", "estimacion", "horas estimadas", "presupuesto", "estimate"),
    "size": ("talla", "tamaño", "tamano", "size", "t-shirt"),
}

# La ficha de cada tipo: roles obligatorios y opcionales y opciones con su valor
# por defecto.
SPECS: dict[str, dict[str, Any]] = {
    "projects": {
        "title": "Proyectos",
        "required": ("assignee", "status", "start", "end"),
        "optional": ("estimate",),
        "options": {},
    },
    "tasks": {
        "title": "Tareas",
        "required": ("assignee", "status", "start", "end", "hours"),
        "optional": ("project", "size"),
        "options": {"hours_split": "full"},
        # Opciones que solo se escriben si se cambian (sin ellas, lo de siempre).
        "extra_options": {"spread": "uniform"},
    },
}

# Cómo se reparten las horas entre varios responsables (C5): completas a cada
# uno, a partes iguales o según la dedicación de cada uno al espacio.
# «Cuenta para la carga» (C6): cualquier base (tickets, encargos, turnos…)
# con solo un responsable; lo demás, si lo tiene.
SPECS["workload"] = {
    "title": "Cuenta para la carga",
    "required": ("assignee",),
    "optional": ("status", "start", "end", "hours", "size", "project"),
    "options": {"hours_split": "full"},
    "extra_options": {"spread": "uniform"},
}
# Los tipos cuyas filas cuentan para la carga del equipo.
WORKLOAD_TYPES = ("tasks", "workload")

HOURS_SPLITS = ("full", "equal", "dedication")
# Cómo se reparten en el tiempo (C5): por igual en los días de trabajo del
# rango, todo el primero, todo el último o todo el día de entrega.
SPREADS = ("uniform", "start", "end", "due")

# Estados de partida de las bases tipadas, ya organizados en grupos (H3).
DEFAULT_STATUS_OPTIONS = ["Por hacer", "En curso", "Bloqueada", "En revisión", "Hecha"]
DEFAULT_STATUS_GROUPS = [
    {"name": "Pendiente", "closed": False, "options": ["Por hacer", "Bloqueada", "En revisión"]},
    {"name": "En curso", "closed": False, "options": ["En curso"]},
    {"name": "Hecho", "closed": True, "options": ["Hecha"]},
]

_DONE_WORDS = re.compile(r"^(hech[oa]|completad[oa]|cerrad[oa]|terminad[oa]|done|finalizad[oa]|resuelt[oa])$", re.I)


def _fail(message: str) -> None:
    raise HTTPException(400, message)


def type_of(prop: dict) -> str:
    raw = prop.get("type")
    return str(getattr(raw, "value", raw) or "")


def spec(kind_type: str) -> dict[str, Any]:
    if kind_type not in SPECS:
        _fail(f"tipo de base desconocido: {kind_type!r} (los hay: {', '.join(KIND_TYPES)})")
    return SPECS[kind_type]


def all_roles(kind_type: str) -> tuple[str, ...]:
    s = spec(kind_type)
    return (*s["required"], *s["optional"])


# --------------------------------------------------------------- leer / guardar


def normalize(raw: Any) -> Optional[dict[str, Any]]:
    """`{type, roles, options}` tal como lo trae el `.md`, o `None`."""
    if not isinstance(raw, dict):
        return None
    kind_type = raw.get("type")
    if kind_type not in SPECS:
        return None
    roles = raw.get("roles") if isinstance(raw.get("roles"), dict) else {}
    roles = {
        str(r): str(k)
        for r, k in roles.items()
        if r in ROLE_TYPES and isinstance(k, str) and k.strip()
    }
    options = raw.get("options") if isinstance(raw.get("options"), dict) else {}
    return {
        "type": kind_type,
        "roles": roles,
        "options": {**SPECS[kind_type]["options"], **options},
    }


def from_frontmatter(front: Optional[dict]) -> Optional[dict[str, Any]]:
    return normalize((front or {}).get("kind"))


def validate_options(kind_type: str, options: Any) -> dict[str, Any]:
    """Las opciones del tipo, comprobadas; 400 si algo no vale."""
    base = dict(spec(kind_type)["options"])
    if options is None:
        return base
    if not isinstance(options, dict):
        _fail("las opciones del tipo van como un objeto")
    for key, value in options.items():
        if key not in base and key not in spec(kind_type).get("extra_options", {}):
            _fail(f"el tipo {kind_type} no tiene la opción {key!r}")
        if key == "hours_split" and value not in HOURS_SPLITS:
            _fail("hours_split es «full» (completas a cada uno), «equal» (a partes iguales) o «dedication» (según la dedicación)")
        if key == "spread" and value not in SPREADS:
            _fail("spread es «uniform», «start», «end» o «due»")
        base[key] = value
    return base


# --------------------------------------------------------------- comprobar


def _by_key(schema: Iterable[dict]) -> dict[str, dict]:
    return {str(p.get("key")): p for p in schema or [] if isinstance(p, dict) and p.get("key")}


def check_roles(kind_type: str, roles: dict[str, str], schema: Iterable[dict]) -> None:
    """Cada rol apunta a una propiedad que existe y es del tipo que pide el rol."""
    props = _by_key(schema)
    required = spec(kind_type)["required"]
    for role in required:
        if role not in roles:
            _fail(f"falta la columna del rol «{ROLE_LABELS[role]}»")
    seen: dict[str, str] = {}
    for role, key in roles.items():
        if role not in all_roles(kind_type):
            _fail(f"el tipo {kind_type} no usa el rol {role!r}")
        prop = props.get(key)
        if prop is None:
            _fail(f"«{key}» no es una columna de la base (rol «{ROLE_LABELS[role]}»)")
        if type_of(prop) != ROLE_TYPES[role]:
            _fail(
                f"«{prop.get('name') or key}» es de tipo {type_of(prop)} y el rol "
                f"«{ROLE_LABELS[role]}» pide {ROLE_TYPES[role]}"
            )
        if key in seen:
            _fail(f"«{prop.get('name') or key}» no puede hacer de «{ROLE_LABELS[seen[key]]}» y de «{ROLE_LABELS[role]}» a la vez")
        seen[key] = role


def check_schema(kind: Optional[dict], new_schema: Iterable[dict]) -> None:
    """Una base tipada no pierde ni cambia de tipo las columnas de sus roles.

    Se pueden renombrar y ocultar; borrarlas o cambiarles el tipo se rechaza
    con el nombre de la columna y el motivo.
    """
    if not kind:
        return
    props = _by_key(new_schema)
    for role, key in (kind.get("roles") or {}).items():
        prop = props.get(key)
        label = ROLE_LABELS.get(role, role)
        if prop is None:
            _fail(
                f"No se puede borrar la columna «{label}»: es parte del tipo "
                f"{SPECS[kind['type']]['title']} (se puede renombrar u ocultar). "
                "Para quitarla, convierte antes la base en una base normal."
            )
        if type_of(prop) != ROLE_TYPES[role]:
            _fail(
                f"No se puede cambiar el tipo de «{prop.get('name') or key}»: "
                f"es la columna «{label}» del tipo {SPECS[kind['type']]['title']}."
            )


# --------------------------------------------------------------- convertir


def auto_map(kind_type: str, schema: Iterable[dict]) -> dict[str, str]:
    """La columna que mejor encaja con cada rol en una base que ya existe."""
    props = [p for p in schema or [] if isinstance(p, dict) and p.get("key")]
    used: set[str] = set()
    out: dict[str, str] = {}
    for role in all_roles(kind_type):
        candidates = [p for p in props if type_of(p) == ROLE_TYPES[role] and p["key"] not in used]
        if not candidates:
            continue
        synonyms = ROLE_SYNONYMS[role]
        best = next(
            (p for p in candidates if str(p.get("name") or "").strip().casefold() in synonyms
             or str(p["key"]).casefold() in synonyms),
            None,
        )
        # Sin nombre que cuadre, la primera de ese tipo solo vale para los roles
        # obligatorios de los que hay una sola columna posible (persona, estado).
        if best is None and role in ("assignee", "status") and len(candidates) >= 1:
            best = candidates[0]
        if best is not None:
            out[role] = best["key"]
            used.add(best["key"])
    return out


def new_column(role: str, taken_names: set[str], **extra: Any) -> dict[str, Any]:
    """La propiedad nueva que hace falta para un rol que la base no tiene."""
    name = ROLE_LABELS[role]
    n = 2
    while name.casefold() in taken_names:
        name = f"{ROLE_LABELS[role]} {n}"
        n += 1
    taken_names.add(name.casefold())
    multiple_people = extra.pop("multiple_people", False)
    prop: dict[str, Any] = {"key": "", "name": name, "type": ROLE_TYPES[role], **extra}
    if role == "assignee" and multiple_people:
        prop["multiple"] = True
    if role == "status":
        prop["options"] = list(DEFAULT_STATUS_OPTIONS)
    return prop


def default_groups_for(options: Iterable[str]) -> Optional[list[dict[str, Any]]]:
    """Grupos de partida para un estado que ya existe: lo hecho a «Hecho», el
    resto a «Pendiente». `None` si ninguna opción suena a hecha."""
    options = [str(o) for o in options or []]
    hechas = [o for o in options if _DONE_WORDS.match(o.strip())]
    if not hechas:
        return None
    return [
        {"name": "Pendiente", "closed": False, "options": [o for o in options if o not in hechas]},
        {"name": "Hecho", "closed": True, "options": hechas},
    ]
