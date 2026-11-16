"""Filtrado y ordenación de las filas de una base según la configuración de una vista.

Vive aquí y no en el router (`app/routers/databases.py`) porque hay dos
consumidores: `GET /databases/{id}/rows?view=...`, que devuelve las filas que
pinta la vista, y la exportación a Markdown (P17.4), que materializa la misma
tabla que se ve en el panel. Duplicar el filtro haría que el `.md` exportado y
la pantalla pudieran discrepar sin que ningún test lo notara.

Se filtra y ordena en Python (no en SQL): a escala personal, con todas las
filas ya en memoria, es más simple y suficientemente rápido que construir
predicados dinámicos sobre JSON. `group_by`/`visible_properties` son metadatos
para que el frontend decida cómo dibujar la vista (tablero, calendario...) —
la exportación sí usa `visible_properties` para elegir columnas, pero eso lo
decide `app.export`.

Forma del `config` de una vista (en el frontmatter de `Bases/<slug>.md`):

- `filters: [{key, op, value}]`: se combinan con **Y** (la fila se enseña si
  cumple todas). `value` no se escribe en los operadores que no lo necesitan
  (`is_empty`, `is_not_empty`, `checked`, `unchecked`).
- `sort: [{key, dir}]`: varias claves en orden de prioridad. También se acepta
  la forma de una sola clave que escribían las versiones anteriores
  (`sort: {key, dir}`); se sigue leyendo y no se reescribe al mirarla.
- `parent_level: roots | all`: qué filas de una base con jerarquía se enseñan.
  `roots` deja solo las que no son subtareas de nadie (por defecto en tabla y
  tablero: una hija ya se ve debajo de su madre, y duplicarla es ruido); `all`
  enseña todas, indentadas bajo su madre. Sin la clave manda el tipo de vista
  (ver `app.hierarchy.level`), así que una base sin jerarquía no cambia nada.

Los valores del frontmatter se leen como texto (el parser de `fm` no infiere
tipos), así que los operadores comparan como número cuando los dos lados lo
son y como texto en el resto de los casos: una fecha ISO se compara bien como
texto.

Una propiedad `person` guarda el **id** de un usuario, pero lo que se lee y se
escribe es su nombre visible: `eq`/`neq` aceptan el nombre además del id y el
orden va por nombre. Para eso hace falta `people` (`id -> display_name`), que
resuelve quien llama; sin él se compara y se ordena por el id, que sigue siendo
determinista.
"""

from typing import Any, Mapping

from app import hierarchy
from app.repository import Page

# Operadores que no llevan `value`: la condición es sobre el estado del valor.
SIN_VALOR = frozenset({"is_empty", "is_not_empty", "checked", "unchecked"})

# Operadores que comparan magnitudes (número, o fecha ISO como texto).
COMPARACIONES = frozenset({"gt", "gte", "lt", "lte", "before", "after"})


def apply_view(
    rows: list[Page],
    config: dict,
    *,
    parent_key: str | None = None,
    view_type: str | None = None,
    people: Mapping[str, str] | None = None,
    person_keys: frozenset[str] | set[str] = frozenset(),
) -> list[Page]:
    """Aplica los `filters`, el `sort` y el nivel de una vista sobre sus filas.

    `parent_key` es la clave de la propiedad `parent_row` de la base, si la
    tiene. Con ella, la vista puede quedarse solo con las madres (el
    `parent_level` de su `config`, por defecto `roots` en tabla y tablero) y las
    hijas se colocan detrás de su madre en el resultado: el orden de las ramas
    lo decide el `sort` de la vista, y dentro de una rama se repite la regla
    (`app.hierarchy.arrange`). Sin `parent_key` —una base sin jerarquía— nada de
    esto se aplica y el resultado es exactamente el de siempre.

    El nivel se aplica ANTES que los `filters`: «solo madres» elige qué filas
    son las de la vista y el filtro se aplica a esas. Consecuencia conocida: un
    filtro sobre una propiedad que solo tienen las hijas deja la vista vacía
    (la madre no lo cumple), porque una fila no se enseña «porque su hija
    cumple». Heredar el filtro hacia la madre es una decisión de producto aparte
    y hoy no se hace.

    `people` e `person_keys` son para las propiedades `person`: el `.md` guarda
    el id de un usuario y el nombre visible lo resuelve el servidor. Sin
    `person_keys` todo se comporta como siempre.
    """
    gente = people or {}
    claves_persona = person_keys or frozenset()
    if parent_key and hierarchy.level(config, view_type) == hierarchy.LEVEL_ROOTS:
        rows = hierarchy.only_roots(rows, parent_key)

    for f in config.get("filters") or []:
        if not isinstance(f, dict):
            continue
        key, op, value = f.get("key"), f.get("op", "eq"), f.get("value")
        if not key:
            continue
        es_persona = key in claves_persona
        rows = [
            r
            for r in rows
            if _match(
                (r.properties or {}).get(key), op, value, persona=es_persona, people=gente
            )
        ]

    claves = _sort_clauses(config.get("sort"))
    # La PRIMERA clave es la que manda, así que se ordena de la menos
    # significativa a la más significativa: `sorted` es estable y cada pasada
    # conserva el orden de la anterior (así se hace un orden por varias claves
    # sin construir una tupla de longitud variable).
    for key, direccion in reversed(claves):
        rows = _sort_rows(
            rows,
            key,
            direccion == "desc",
            persona=key in claves_persona,
            people=gente,
        )

    if parent_key:
        rows = hierarchy.arrange(rows, parent_key)
    return rows


def _sort_clauses(raw: Any) -> list[tuple[str, str]]:
    """Las claves de orden normalizadas, aceptando la forma de lista y la de mapa.

    La forma de mapa (`sort: {key, dir}`) es la de las vistas que ya están
    escritas en los `.md`; la de lista (`sort: [{key, dir}]`) es la que hace
    falta para ordenar por varias claves. Las dos se leen igual.
    """
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [
        (str(item["key"]), str(item.get("dir") or "asc"))
        for item in raw
        if isinstance(item, dict) and item.get("key")
    ]


def _sort_rows(
    rows: list[Page],
    key: str,
    descendente: bool,
    *,
    persona: bool = False,
    people: Mapping[str, str] | None = None,
) -> list[Page]:
    """Ordena por una clave, dejando SIEMPRE las filas sin valor al final.

    `reverse=True` pondría las vacías delante, así que se separan antes: un
    hueco no es «lo más pequeño», es la ausencia de dato, y en el panel se
    espera al final en las dos direcciones.

    Una persona se ordena por su **nombre visible** (el id no se lee), y un id
    que ya no resuelve por sí mismo, para que no se quede fuera del orden.
    """
    con_valor = [r for r in rows if _valor(r, key) is not None]
    sin_valor = [r for r in rows if _valor(r, key) is None]

    def clave(row: Page) -> tuple:
        valor = _valor(row, key)
        if persona and people:
            nombre = people.get(str(valor).strip())
            if nombre is not None:
                return _sort_key(nombre)
        return _sort_key(valor)

    con_valor.sort(key=clave, reverse=descendente)
    return con_valor + sin_valor


def _valor(row: Page, key: str) -> Any:
    return (row.properties or {}).get(key)


def _match(
    actual: Any,
    op: str,
    value: Any,
    *,
    persona: bool = False,
    people: Mapping[str, str] | None = None,
) -> bool:
    """Una condición del filtro sobre el valor de una fila."""
    if op == "is_empty":
        return _vacio(actual)
    if op == "is_not_empty":
        return not _vacio(actual)
    if op == "checked":
        return _marcado(actual)
    if op == "unchecked":
        return not _marcado(actual)
    if op == "eq":
        if persona:
            return _igual_persona(actual, value, people)
        return _igual(actual, value)
    if op == "neq":
        if persona:
            return not _igual_persona(actual, value, people)
        return not _igual(actual, value)
    if op == "contains":
        return _contiene(actual, value)
    if op == "includes":
        return _incluye(actual, value)
    if op in COMPARACIONES:
        return _compara(actual, op, value)
    # Un operador que este backend no conoce no puede fingir que filtra: deja
    # pasar la fila (mismo criterio que la versión anterior). La interfaz solo
    # ofrece los de arriba.
    return True


def _vacio(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def _marcado(value: Any) -> bool:
    """Una casilla marcada, venga como booleano o como el texto del `.md`."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "on", "si", "sí", "x"}


def _numero(value: Any) -> float | None:
    """El valor como número, o `None` si no es un número (fechas, texto)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    texto = str(value).strip()
    if not texto:
        return None
    try:
        return float(texto)
    except ValueError:
        return None


def _igual(actual: Any, esperado: Any) -> bool:
    # «es» con un valor vacío pregunta por «está vacío»: es lo que espera quien
    # compara una celda sin rellenar (y no un `eq` que nunca se cumple).
    if esperado is None or (isinstance(esperado, str) and not esperado.strip()):
        return _vacio(actual)
    if _vacio(actual):
        return False
    a, b = _numero(actual), _numero(esperado)
    if a is not None and b is not None:
        return a == b
    if isinstance(actual, list):
        if isinstance(esperado, list):
            return {_texto(v) for v in actual} == {_texto(v) for v in esperado}
        return any(_texto_eq(v, esperado) for v in actual)
    return _texto_eq(actual, esperado)


def _igual_persona(
    actual: Any, esperado: Any, people: Mapping[str, str] | None
) -> bool:
    """`eq` de una persona: por id (`people` resuelto) o por nombre visible.

    Un valor vacío pregunta por «está vacío», como en cualquier otro tipo: así
    «Responsable es —» sirve, y el selector del filtro puede mandar el nombre
    que el usuario lee en el chip sin saber el id.
    """
    if esperado is None or (isinstance(esperado, str) and not esperado.strip()):
        return _vacio(actual)
    if _vacio(actual):
        return False
    if _texto_eq(actual, esperado):
        return True
    nombre = (people or {}).get(str(actual).strip())
    return nombre is not None and _texto(nombre) == _texto(esperado)


def _texto(value: Any) -> str:
    return str(value).strip().casefold()


def _texto_eq(actual: Any, esperado: Any) -> bool:
    if isinstance(actual, str) and isinstance(esperado, str):
        return _texto(actual) == _texto(esperado)
    return actual == esperado


def _contiene(actual: Any, esperado: Any) -> bool:
    """`contiene`: subcadena en un texto, o un elemento en una lista."""
    if isinstance(actual, list):
        return any(_texto_eq(v, esperado) for v in actual)
    if _vacio(actual) or esperado is None or _texto(esperado) == "":
        return False
    return _texto(esperado) in _texto(actual)


def _incluye(actual: Any, esperado: Any) -> bool:
    """`incluye` de una relación: el id (o los ids) de la celda.

    Una relación simple guarda un id y una múltiple una lista; las dos
    responden a «incluye» con el id buscado.
    """
    if esperado is None or str(esperado).strip() == "":
        return False
    ids = actual if isinstance(actual, list) else [actual]
    return any(str(v).strip() == str(esperado).strip() for v in ids if v is not None)


def _compara(actual: Any, op: str, esperado: Any) -> bool:
    """`>`, `<`, `≥`, `≤`, `antes de`, `después de`.

    Un valor vacío no cumple ninguna comparación (no hay nada que comparar), y
    los dos lados se comparan como número si lo son y como texto si no — una
    fecha ISO `2026-11-11` comparada como texto da el orden correcto.
    """
    if _vacio(actual) or esperado is None or (isinstance(esperado, str) and not esperado.strip()):
        return False
    a, b = _numero(actual), _numero(esperado)
    if a is None or b is None:
        a, b = _texto(actual), _texto(esperado)
    if op in ("gt", "after"):
        return a > b
    if op == "gte":
        return a >= b
    if op in ("lt", "before"):
        return a < b
    return a <= b


def _sort_key(value: Any) -> tuple:
    """Clave de orden total, sin `TypeError` al mezclar tipos.

    Los booleanos van primero, luego los números (también los que llegan como
    texto del `.md`, como `"12000"`) y al final el texto; dentro de cada grupo
    se compara lo suyo, y el texto sin distinguir mayúsculas.
    """
    if isinstance(value, bool):
        return (0, float(value), "")
    if isinstance(value, (list, dict)):
        return (2, 0.0, str(value).casefold())
    numero = _numero(value)
    if numero is not None:
        return (1, numero, "")
    return (2, 0.0, str(value).casefold())
