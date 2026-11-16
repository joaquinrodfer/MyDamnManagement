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

**Qué es «abierta»** (decisión de esta tarjeta, a falta de la función 5 del
estado «cerrado» con semántica): una fila cuya propiedad de estado tenga un
valor de `CLOSED_STATES` no sale. Una base **sin** propiedad de estado no
filtra nada — sus filas entran todas, porque no hay nada que diga que están
hechas. La lista se devuelve con la respuesta (`closed_values`) para que la
interfaz pueda explicarla sin repetirla a mano.

La propiedad de estado es la de **selección** llamada «Estado» o con la clave
`estado`; si no la hay, no se enseña estado. La fecha límite es la **primera**
propiedad de tipo `date` de la base (el mismo criterio que `GET /dashboard`), y
su valor se devuelve tal cual (`YYYY-MM-DD`): quién decide si está vencida o en
qué bloque cae es el navegador, que es el único que sabe qué día es hoy para
quien está mirando la pantalla.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from datetime import date, datetime
from typing import Any, Optional

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

# Qué cuenta como «hecha». Es una lista cerrada y en minúsculas sin acentos
# (`folded`): «Hecho», «HECHO» y «cerrada» casan; «en curso» y «bloqueada», no.
# Mientras no exista la semántica del estado «cerrado» (función 5), esto es lo
# que hay, y se dice en la interfaz.
CLOSED_STATES: tuple[str, ...] = (
    "hecho",
    "hecha",
    "hechos",
    "hechas",
    "cerrado",
    "cerrada",
    "cerrados",
    "cerradas",
    "done",
    "ganado",
    "ganada",
    "ganados",
    "ganadas",
)

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


_CLOSED_FOLDED = frozenset(folded(state) for state in CLOSED_STATES)


def is_closed(value: Any) -> bool:
    """¿El valor de estado de una fila la marca como hecha?

    Un `multiselect` cuenta como cerrado si **alguno** de sus valores lo está:
    «hecho, archivado» sigue siendo una tarea hecha. Un valor que no es texto
    (o vacío) no cierra nada.
    """
    values = value if isinstance(value, list) else [value]
    return any(
        folded(one) in _CLOSED_FOLDED for one in values if isinstance(one, str) and one.strip()
    )


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
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        return None


def _bases(conn: sqlite3.Connection) -> dict[str, dict]:
    """Las bases que tienen alguna propiedad `person`, con lo que hace falta.

    Una consulta: el `schema_json` de cada base y el título de su página (el de
    la fila de `pages`, que es donde vive el nombre de la base). Las bases sin
    propiedad `person` no entran: no pueden tener nada mío.
    """
    rows = conn.execute(
        "SELECT d.id AS id, d.schema_json AS schema_json, p.title AS title"
        " FROM databases d JOIN pages p ON p.id = d.page_id"
        " ORDER BY p.title COLLATE NOCASE, d.id"
    ).fetchall()
    bases: dict[str, dict] = {}
    for row in rows:
        schema_def = json.loads(row["schema_json"] or "[]")
        keys = person_keys(schema_def)
        if not keys:
            continue
        bases[row["id"]] = {
            "title": row["title"] or "Base de datos",
            "person_keys": keys,
            "status": status_property(schema_def),
            "date": date_property(schema_def),
        }
    return bases


def my_tasks(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """Las filas de todas las bases cuyo responsable es `user_id`.

    Devuelve `{"count", "closed_values", "tasks"}`. Cada tarea lleva de dónde
    sale (`database_id`, `database_title`), su título, su estado si la base
    tiene uno y su fecha si la base tiene alguna. Ordenadas por fecha
    ascendente (las sin fecha al final) y, a igual fecha, por base y título:
    los bloques (vencidas/hoy/esta semana/resto) los pinta quien las enseña.
    """
    user_id = str(user_id or "").strip()
    salida: dict[str, Any] = {
        "count": 0,
        "closed_values": list(CLOSED_STATES),
        "tasks": [],
    }
    if not user_id:
        return salida

    bases = _bases(conn)
    if not bases:
        return salida

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
            if status is not None and is_closed(props.get(base["status"]["key"])):
                continue
            tasks.append(
                {
                    "row_id": row["id"],
                    "database_id": row["database_id"],
                    "database_title": base["title"],
                    "title": row["title"] or "Sin título",
                    "icon": row["icon"],
                    "status": status,
                    "date": _cell(base["date"], props, parse_date=True),
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
    return salida
