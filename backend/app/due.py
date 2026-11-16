"""Cuándo vence una fila, contado contra el día del **servidor**.

Una fecha de una fila es un día del calendario (`YYYY-MM-DD`), y decir si ya
pasó, si es hoy o si cae esta semana es una resta contra «hoy». Hasta ahora esa
resta la hacía cada pantalla con el reloj del navegador: dos personas del mismo
equipo en husos distintos veían dos «hoy» distintos, y el contador de la barra
lateral podía decir «2 vencidas» mientras la lista de vencidas estaba vacía
porque para ese portátil todavía era ayer.

Aquí la resta se hace **una sola vez y en el servidor** (`server_today()`, el
día local del proceso que sirve la app) y su resultado viaja con la respuesta:

- `GET /my-tasks` manda `today` —el día con el que se ha comparado—, los
  recuentos (`counts.overdue` / `counts.today`) y, por cada fila, su bloque
  (`due`) y los días que le quedan (`days`). El cliente pinta lo que le dicen:
  no vuelve a mirar un reloj, así que no puede contradecirlo.
- Cada fila de una base viaja con `overdue_keys`: las claves de sus propiedades
  de fecha que ya pasaron **y que la base marca como no cerradas**. La tabla se
  limita a pintar en tono de aviso la celda de esa clave.

**Qué NO cuenta como vencida.**

- Una fila **cerrada** con la fecha pasada: una tarea hecha tarde no está
  vencida. Lo decide la regla de cierre de la base (`app.closed`), la misma que
  esconde filas en «Mis tareas» y cuenta subtareas; aquí no se reimplementa.
- Una base **sin** regla de cierre no tiene estado, así que sus filas nunca se
  marcan en la tabla (no hay nada que diga que están hechas). En «Mis tareas»
  esas filas sí entran y una fecha pasada **sí** cuenta: si nada puede cerrarlas,
  siguen abiertas.
- Una fecha **imposible** (`cuando pueda`, `2026-13-45`) no es una fecha: no
  cuenta como vencida ni como nada. No se inventa el día que no dice.

El coste es el de la respuesta: una resta por fila y propiedad de fecha. No hay
ningún trabajo en segundo plano ni ningún aviso fuera de la app: esto es «cuando
entras, lo ves».
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Optional

from app import closed

# Los bloques en que se reparte una tarea según su fecha. Son los mismos que
# pinta «Mis tareas», y el nombre viaja en la respuesta para que el cliente no
# tenga que deducirlo del día.
OVERDUE = "overdue"   # la fecha ya pasó
TODAY = "today"       # vence hoy
WEEK = "week"         # de mañana al domingo de esta semana
LATER = "later"       # más adelante

DATE = "date"


def server_today() -> date:
    """El día de hoy **del servidor**: la única respuesta que hay.

    Se llama desde un módulo (y no se importa la función por nombre) a propósito:
    los tests fijan el día con `monkeypatch.setattr(app.due, "server_today", …)`
    y así la comparación que se prueba es exactamente la que corre en la app.
    """
    return date.today()


def iso_day(raw: Any) -> Optional[date]:
    """El día de una celda de fecha, o `None` si lo que hay no es una fecha.

    Acepta lo que el `.md` puede tener: un día suelto (`2026-11-14`) y un ISO
    completo (`2026-11-14T09:30:00`). Lo que no casa —texto libre, un mes 13, un
    día 32— devuelve `None`: una fecha que no se puede leer no se inventa.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def week_end(today: date) -> date:
    """El domingo de la semana de `today` (la semana empieza el lunes).

    «Esta semana» es de mañana al domingo: hoy tiene su propio bloque y lo de la
    semana que viene no es asunto de un vistazo.
    """
    return date.fromordinal(today.toordinal() + (7 - today.isoweekday()))


def classify(day: date, today: date) -> str:
    """En qué bloque cae `day` mirándolo desde `today`."""
    if day < today:
        return OVERDUE
    if day == today:
        return TODAY
    if day <= week_end(today):
        return WEEK
    return LATER


def due_state(raw: Any, today: date) -> Optional[str]:
    """El bloque de una celda de fecha, o `None` si no hay fecha legible."""
    day = iso_day(raw)
    return classify(day, today) if day else None


def days_left(raw: Any, today: date) -> Optional[int]:
    """Días de calendario que faltan para la fecha (negativo = ya pasó)."""
    day = iso_day(raw)
    return (day - today).days if day else None


def overdue_date_keys(
    schema_def: Iterable[dict],
    properties: dict,
    rule: Optional[dict],
    today: date,
) -> list[str]:
    """Claves de las fechas pasadas que la tabla tiene que marcar.

    Solo en una base que **declara la regla de cierre** (sin ella no hay estado y
    una fila no puede estar «sin terminar»: marcar todas las fechas viejas sería
    una opinión del cliente) y solo si la fila **no está cerrada** (una tarea
    hecha tarde no está vencida). Se devuelven todas las propiedades de fecha
    pasadas, no solo la primera: la tabla pinta celdas, no la fecha «oficial».
    """
    if not rule:
        return []
    if closed.is_closed(rule, properties or {}):
        return []
    claves: list[str] = []
    for prop in schema_def or []:
        if not isinstance(prop, dict) or prop.get("type") != DATE:
            continue
        key = prop.get("key")
        if not key:
            continue
        day = iso_day((properties or {}).get(key))
        if day is not None and day < today:
            claves.append(str(key))
    return claves
