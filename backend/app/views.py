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
"""

from typing import Any

from app.repository import Page


def apply_view(rows: list[Page], config: dict) -> list[Page]:
    """Aplica los `filters` y el `sort` de una vista sobre sus filas."""
    for f in config.get("filters") or []:
        key, op, value = f.get("key"), f.get("op", "eq"), f.get("value")
        rows = [r for r in rows if _match((r.properties or {}).get(key), op, value)]

    sort = config.get("sort")
    if sort and sort.get("key"):
        key = sort["key"]
        rows = sorted(
            rows,
            key=lambda r: _sort_key((r.properties or {}).get(key)),
            reverse=(sort.get("dir") == "desc"),
        )

    return rows


def _match(actual: Any, op: str, value: Any) -> bool:
    if op == "eq":
        return actual == value
    if op == "neq":
        return actual != value
    if op == "contains":
        return isinstance(actual, list) and value in actual
    if op == "is_empty":
        return actual in (None, "", [])
    if op == "is_not_empty":
        return actual not in (None, "", [])
    return True


def _sort_key(value: Any) -> tuple:
    # None siempre al final, con independencia del tipo del valor real
    return (value is None, str(value) if isinstance(value, (list, dict)) else value)
