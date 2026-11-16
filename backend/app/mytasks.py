"""«Mis tareas»: las filas de **todas** las bases cuyo responsable soy yo.

Lo que hay que cruzar está en dos tablas que no se conocen entre sí: la
propiedad `person` de una base guarda el **id** de un usuario en el JSON de la
fila (`rows.properties_json`), y qué propiedades son `person` lo dice el
schema de cada base (`databases.schema_json`). La vista contesta a la pregunta
«¿qué me toca hoy?» sin que el panel tenga que entrar base por base:

- se leen los schemas de **todas** las bases en una consulta;
- se leen las filas de las bases que tienen alguna propiedad `person` en
  **una** consulta (troceada por el tope de parámetros de SQLite, no una por
  base ni una por fila), y el filtro por persona se hace en memoria sobre ese
  JSON: con 12.000 notas son milisegundos y no hace falta `json_each` ni un
  índice nuevo por el que pagar en cada guardado;
- el nombre de la base sale del `JOIN` con su página, porque la fila sola no
  dice de dónde viene.

**Qué es «abierta».** Manda la regla que declara cada base: su bloque
`closed: {property, values}` en el frontmatter (`app.closed`), el mismo que
decide el contador de subtareas y el filtro `is_closed`/`is_open`. Una fila
cerrada según esa regla no sale. Una base **sin** regla no tiene estado y no
filtra nada: sus filas entran todas, porque no hay nada que diga que están
hechas. No hay lista de respaldo escondida en el código —era otra verdad, y la
pantalla podía contradecir al `.md`—: los valores cerrados que viajan con la
respuesta (`closed_values`) son la unión de los que declaran las bases, para que
la interfaz explique el criterio sin repetirlo a mano.

La propiedad de estado es la de **selección** llamada «Estado» o con la clave
`estado`; si no la hay, no se enseña estado. La fecha límite es la **primera**
propiedad de tipo `date` de la base (el mismo criterio que `GET /dashboard`), y
su valor se devuelve tal cual (`YYYY-MM-DD`).

**Qué está vencido lo dice este módulo, no el navegador.** La fecha de cada fila
se compara contra el día de hoy **del servidor** (`app.due.server_today()`) y el
resultado viaja en la respuesta: `today` (el día con el que se comparó),
`counts.overdue` / `counts.today` (los dos números de la barra lateral) y, por
cada tarea, su bloque (`due`: `overdue` / `today` / `week` / `later`, o `null`
sin fecha) y los días que le quedan (`days`). Así un equipo con los portátiles en
husos distintos no puede ver dos «hoy» distintos: la pantalla pinta la resta que
ya hizo el servidor y no mira ningún reloj.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from typing import Any, Optional

from app import closed, due
from app.properties import person_keys

# Lo que el schema marca como selección: `select` (un valor) y `multiselect`
# (varios) pueden hacer de estado. Un `text` llamado «Estado» no: sus valores
# serían texto libre y cualquier palabra contaría como cerrada.
SELECT_TYPES = ("select", "multiselect")

DATE = "date"

# La clave que se busca primero y el nombre visible que se acepta después: la
# clave la deriva la app del nombre al crear la propiedad (`plan_schema`), así
# que «Estado» nace `estado` y renombrarla a «Situación» conserva la clave.
STATUS_KEY = "estado"

# Cuántos ids de base caben en un `IN (...)`. El tope de parámetros de SQLite
# es del motor (32766 en las versiones que la app soporta) y no queremos
# depender de él; trocear no cambia el número de consultas por base, que sigue
# siendo cero.
_CHUNK = 900


def folded(value: Any) -> str:
    """Texto comparable: NFC, sin acentos, sin espacios de sobra, minúsculas."""
    text = unicodedata.normalize("NFD", str(value))
    plano = "".join(ch for ch in text if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", plano).strip().casefold()


def closed_row(rule: Optional[dict], props: dict) -> bool:
    """¿La fila cuenta como cerrada (y por tanto no sale en la vista)?

    Lo decide la regla que declara la base (`closed: {property, values}`, ver
    `app.closed`), la misma que usa el contador de subtareas y el filtro
    `is_closed`. Sin regla no hay estado: la fila entra, porque no hay nada que
    diga que está hecha.
    """
    return closed.is_closed(rule, props)


def closed_values(bases: dict[str, dict]) -> list[str]:
    """Los valores que alguna base declara como cerrados, sin repetir.

    Es la lista que la interfaz necesita para explicar la vista, y sale de las
    bases que se han leído (no de una constante): si ninguna declara estado, la
    lista va vacía y la nota lo dice.
    """
    vistos: dict[str, str] = {}
    for base in bases.values():
        for valor in (base.get("closed") or {}).get(closed.VALUES_KEY) or []:
            vistos.setdefault(str(valor).strip().casefold(), str(valor).strip())
    return [vistos[clave] for clave in sorted(vistos) if vistos[clave]]


def status_property(schema_def: list[dict]) -> Optional[dict]:
    """La propiedad de selección que hace de estado, o `None`.

    Primero por **clave** (`estado`), que es la identidad de la propiedad y
    sobrevive a un renombrado; después por **nombre visible** «Estado», para
    una base que la tenga con otra clave.
    """
    candidates = [
        prop
        for prop in schema_def or []
        if isinstance(prop, dict) and prop.get("type") in SELECT_TYPES and prop.get("key")
    ]
    for prop in candidates:
        if folded(prop["key"]) == STATUS_KEY:
            return prop
    for prop in candidates:
        if folded(prop.get("name") or "") == STATUS_KEY:
            return prop
    return None


def date_property(schema_def: list[dict]) -> Optional[dict]:
    """La **primera** propiedad `date` de la base: la fecha límite de la fila.

    Una base puede tener varias fechas (creada, vence, recordatorio); la vista
    no adivina cuál es «la buena» y se queda con la primera, en el orden del
    schema. Si no hay ninguna, la fila sale sin fecha.
    """
    for prop in schema_def or []:
        if isinstance(prop, dict) and prop.get("type") == DATE and prop.get("key"):
            return prop
    return None


def _cell(prop: Optional[dict], props: dict, *, parse_date: bool = False) -> Optional[dict]:
    """`{key, name, value}` de una celda, o `None` si la fila no la tiene."""
    if prop is None:
        return None
    raw = props.get(prop["key"])
    if isinstance(raw, list):
        raw = ", ".join(str(one) for one in raw if str(one).strip())
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    if parse_date:
        value = _iso_date(value)
        if value is None:
            # Una fecha que no se puede leer no se inventa: la fila va sin
            # fecha (y con ella al bloque «resto»), que es la verdad.
            return None
    return {"key": prop["key"], "name": prop.get("name") or prop["key"], "value": value}


def _iso_date(raw: str) -> Optional[str]:
    """`2026-11-14` (o un ISO completo) -> `2026-11-14`; si no casa, `None`."""
    day = due.iso_day(raw)
    return day.isoformat() if day else None


def _bases(conn: sqlite3.Connection) -> dict[str, dict]:
    """Las bases que tienen alguna propiedad `person`, con lo que hace falta.

    Una consulta: el `schema_json` de cada base y el título de su página (el de
    la fila de `pages`, que es donde vive el nombre de la base). Las bases sin
    propiedad `person` no entran: no pueden tener nada mío.
    """
    rows = conn.execute(
        "SELECT d.id AS id, d.schema_json AS schema_json, c.rule_json AS rule_json,"
        " p.title AS title"
        " FROM databases d JOIN pages p ON p.id = d.page_id"
        " LEFT JOIN database_closed c ON c.database_id = d.id"
        " ORDER BY p.title COLLATE NOCASE, d.id"
    ).fetchall()
    bases: dict[str, dict] = {}
    for row in rows:
        schema_def = json.loads(row["schema_json"] or "[]")
        keys = person_keys(schema_def)
        if not keys:
            continue
        # La regla de cada base, resuelta como en cualquier otra lectura: el
        # bloque `closed:` o, si no lo hay, los `done` viejos del schema.
        rule, _origen = closed.effective(json.loads(row["rule_json"] or "null"), schema_def)
        bases[row["id"]] = {
            "title": row["title"] or "Base de datos",
            "schema_def": schema_def,
            "person_keys": keys,
            "status": status_property(schema_def),
            "date": date_property(schema_def),
            "closed": rule,
        }
    return bases


def my_tasks(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """Las filas de todas las bases cuyo responsable es `user_id`.

    Devuelve `{"count", "today", "counts", "closed_values", "tasks"}`. Cada
    tarea lleva de dónde sale (`database_id`, `database_title`), su título, su
    estado si la base tiene uno, su fecha si la base tiene alguna y en qué
    bloque cae (`due`) con los días que le quedan (`days`), contados contra el
    día del servidor. Ordenadas por fecha ascendente (las sin fecha al final) y,
    a igual fecha, por base y título.

    `counts` son los dos números de la barra lateral: lo **vencido** y lo que
    **vence hoy**. Se cuentan sobre las mismas tareas que se devuelven (las
    abiertas), así que lo que dice el número y lo que se ve al abrir la vista no
    pueden discrepar.
    """
    user_id = str(user_id or "").strip()
    hoy = due.server_today()
    salida: dict[str, Any] = {
        "count": 0,
        "today": hoy.isoformat(),
        "counts": {"overdue": 0, "today": 0},
        "closed_values": [],
        "tasks": [],
    }
    if not user_id:
        return salida

    bases = _bases(conn)
    if not bases:
        return salida
    # Lo que el usuario tiene que saber: los valores que las bases declaran
    # como cerrados. Es la verdad del `.md`, no una lista del servidor.
    salida["closed_values"] = closed_values(bases)

    tasks: list[dict[str, Any]] = []
    ids = list(bases)
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT r.id AS id, r.database_id AS database_id,"
            " r.properties_json AS properties_json, p.title AS title, p.icon AS icon"
            " FROM rows r JOIN pages p ON p.id = r.id"
            f" WHERE r.database_id IN ({marks})",
            chunk,
        ).fetchall()
        for row in rows:
            base = bases[row["database_id"]]
            props = json.loads(row["properties_json"] or "{}")
            mine = any(
                isinstance(props.get(key), str) and props[key].strip() == user_id
                for key in base["person_keys"]
            )
            if not mine:
                continue
            status = _cell(base["status"], props)
            if closed_row(base["closed"], props):
                continue
            fecha = _cell(base["date"], props, parse_date=True)
            dia = due.iso_day(fecha["value"]) if fecha else None
            tasks.append(
                {
                    "row_id": row["id"],
                    "database_id": row["database_id"],
                    "database_title": base["title"],
                    "title": row["title"] or "Sin título",
                    "icon": row["icon"],
                    "status": status,
                    "date": fecha,
                    "due": due.classify(dia, hoy) if dia else None,
                    "days": (dia - hoy).days if dia else None,
                }
            )

    tasks.sort(
        key=lambda task: (
            task["date"] is None,
            (task["date"] or {}).get("value", ""),
            folded(task["database_title"]),
            folded(task["title"]),
        )
    )
    salida["tasks"] = tasks
    salida["count"] = len(tasks)
    # Los dos números de la barra lateral, contados sobre lo que se devuelve.
    salida["counts"] = {
        "overdue": sum(1 for task in tasks if task["due"] == due.OVERDUE),
        "today": sum(1 for task in tasks if task["due"] == due.TODAY),
    }
    return salida
