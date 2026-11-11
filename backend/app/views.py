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

Los valores del frontmatter se leen como texto (el parser de `fm` no infiere
tipos), así que los operadores comparan como número cuando los dos lados lo
son y como texto en el resto de los casos: una fecha ISO se compara bien como
texto.
"""

from typing import Any

from app.repository import Page

# Operadores que no llevan `value`: la condición es sobre el estado del valor.
SIN_VALOR = frozenset({"is_empty", "is_not_empty", "checked", "unchecked"})

# Operadores que comparan magnitudes (número, o fecha ISO como texto).
COMPARACIONES = frozenset({"gt", "gte", "lt", "lte", "before", "after"})


def apply_view(rows: list[Page], config: dict) -> list[Page]:
    """Aplica los `filters` y el `sort` de una vista sobre sus filas."""
    for f in config.get("filters") or []:
        if not isinstance(f, dict):
            continue
        key, op, value = f.get("key"), f.get("op", "eq"), f.get("value")
        if not key:
            continue
        rows = [r for r in rows if _match((r.properties or {}).get(key), op, value)]

    claves = _sort_clauses(config.get("sort"))
    # La PRIMERA clave es la que manda, así que se ordena de la menos
    # significativa a la más significativa: `sorted` es estable y cada pasada
    # conserva el orden de la anterior (así se hace un orden por varias claves
    # sin construir una tupla de longitud variable).
    for key, direccion in reversed(claves):
        rows = _sort_rows(rows, key, direccion == "desc")

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


def _sort_rows(rows: list[Page], key: str, descendente: bool) -> list[Page]:
    """Ordena por una clave, dejando SIEMPRE las filas sin valor al final.

    `reverse=True` pondría las vacías delante, así que se separan antes: un
    hueco no es «lo más pequeño», es la ausencia de dato, y en el panel se
    espera al final en las dos direcciones.
    """
    con_valor = [r for r in rows if _valor(r, key) is not None]
    sin_valor = [r for r in rows if _valor(r, key) is None]
    con_valor.sort(key=lambda r: _sort_key(_valor(r, key)), reverse=descendente)
    return con_valor + sin_valor


def _valor(row: Page, key: str) -> Any:
    return (row.properties or {}).get(key)


def _match(actual: Any, op: str, value: Any) -> bool:
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
        return _igual(actual, value)
    if op == "neq":
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
