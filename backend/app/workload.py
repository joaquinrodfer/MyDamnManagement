"""Carga del equipo (plan H5): las horas de todas las bases de Tareas, por persona y semana.

La carga no guarda totales: calcula una lista de **piezas** y todo lo demás son
sumas sobre ella. Cada pieza es una hora (o un trozo de las horas) de una tarea
abierta que cae en una semana:

    persona · espacio · proyecto · base · tarea · semana · horas

Así, por persona y semana, por proyecto o por espacio salen del mismo detalle, y
cualquier función nueva (filtrar, previsiones, exportar) parte de él sin rehacer
el cálculo.

**Qué cuenta.** Las filas **no cerradas** (`app.closed`, la regla de cada base,
con los grupos de estado) de **todas** las bases de tipo Tareas de **todos** los
espacios, con la columna de cada rol que declara la base (`app.kinds`).

**Cómo se reparten las horas.**
- *Entre semanas:* por igual entre los días laborables (lunes a viernes) del
  rango inicio–fin. Con solo fecha de fin, todas caen en la semana de esa fecha
  (con solo la de inicio, en la suya). Sin ninguna fecha, en «sin fecha».
- *Entre responsables:* según la opción `hours_split` de la base: `full` (las
  horas completas a cada uno) o `equal` (a partes iguales).

**Privacidad entre espacios.** Quien ve la carga de un espacio al que no
pertenece (o de una página que no ve) recibe las horas y las fechas, pero no los
títulos: «Proyecto privado [Espacio]».
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Optional

from app import closed, worktime
from app.due import iso_day

DEFAULT_CAPACITY = worktime.weekly(worktime.DEFAULT_DAY_HOURS)
MAX_WEEKS = 26


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def working_days(start: date, end: date) -> list[date]:
    """Los días de lunes a viernes entre `start` y `end`, ambos incluidos."""
    out: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def spread(start: Optional[date], end: Optional[date], hours: float) -> list[tuple[Optional[date], float]]:
    """`[(lunes de la semana, horas)]`: las horas repartidas por semanas.

    Sin fechas, una sola entrada con `None` (sin fecha). Con una sola, todas a
    la semana de esa fecha. Con rango, por igual entre sus días laborables (si
    no hay ninguno —un fin de semana—, a la semana de la fecha de fin).
    """
    if hours <= 0:
        return []
    if start is None and end is None:
        return [(None, hours)]
    if start is None or end is None:
        return [(monday(start or end), hours)]  # type: ignore[arg-type]
    if end < start:
        start, end = end, start
    days = working_days(start, end)
    if not days:
        return [(monday(end), hours)]
    per_day = hours / len(days)
    weeks: dict[date, float] = defaultdict(float)
    for d in days:
        weeks[monday(d)] += per_day
    return sorted(weeks.items())


def _number(value: Any) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return n if n > 0 else 0.0


def _ids(value: Any) -> list[str]:
    """Los ids de una celda de persona o de relación (uno o varios)."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip() if value is not None else ""
    return [text] if text else []


def capacities(conn: sqlite3.Connection) -> dict[str, float]:
    """Las horas por semana de la jornada de cada persona que tiene una propia
    (sin festivos ni ausencias). Las demás, `DEFAULT_CAPACITY`."""
    return {uid: worktime.weekly(h) for uid, h in worktime.schedules(conn).items()}


def set_capacity(conn: sqlite3.Connection, user_id: str, hours: Optional[float]) -> dict[str, float]:
    """Lo de antes (horas por semana): una jornada de lunes a viernes por igual."""
    worktime.set_schedule(conn, user_id, None if hours is None else worktime.from_weekly(float(hours)))
    return capacities(conn)


def pieces(
    conn: sqlite3.Connection,
    visible: Optional[set[str]],
    calendar: Optional[worktime.Calendar] = None,
    by_day: bool = False,
) -> list[dict[str, Any]]:
    """Todas las piezas de carga (sin enmascarar los títulos que no se ven).

    Con `by_day`, una pieza por persona, tarea y DÍA (con `day`), para la
    vista por días y meses (C3); `estimated` dice si la tarea tiene horas.

    Las horas de cada persona se reparten en los días que ESA persona trabaja
    (`worktime.Calendar.spread`: su jornada, sin festivos ni ausencias) y se
    suman por semana: una pieza por persona, tarea y semana."""
    calendar = calendar or worktime.Calendar(conn)
    bases = conn.execute(
        "SELECT d.id AS id, d.schema_json AS schema_json, k.kind_json AS kind_json,"
        " c.rule_json AS rule_json, p.title AS title, p.workspace_id AS workspace_id,"
        " w.name AS workspace_name, p.id AS page_id"
        " FROM databases d JOIN database_kinds k ON k.database_id = d.id"
        " JOIN pages p ON p.id = d.page_id JOIN workspaces w ON w.id = p.workspace_id"
        " LEFT JOIN database_closed c ON c.database_id = d.id"
    ).fetchall()
    out: list[dict[str, Any]] = []
    project_ids: set[str] = set()
    staged: list[dict[str, Any]] = []
    for base in bases:
        kind = json.loads(base["kind_json"] or "null") or {}
        if kind.get("type") != "tasks":
            continue
        roles = kind.get("roles") or {}
        if not roles.get("assignee"):
            continue
        schema_def = json.loads(base["schema_json"] or "[]")
        rule, _src = closed.effective(json.loads(base["rule_json"] or "null"), schema_def)
        split = (kind.get("options") or {}).get("hours_split", "full")
        for row in conn.execute(
            "SELECT r.id AS id, r.properties_json AS props, p.title AS title, p.acl_source AS acl_source"
            " FROM rows r JOIN pages p ON p.id = r.id WHERE r.database_id = ?",
            (base["id"],),
        ):
            props = json.loads(row["props"] or "{}")
            if closed.is_closed(rule, props):
                continue
            people = _ids(props.get(roles["assignee"]))
            if not people:
                continue
            hours = _number(props.get(roles.get("hours"))) if roles.get("hours") else 0.0
            start = iso_day(props.get(roles.get("start"))) if roles.get("start") else None
            end = iso_day(props.get(roles.get("end"))) if roles.get("end") else None
            projects = _ids(props.get(roles.get("project"))) if roles.get("project") else []
            project_ids.update(projects)
            staged.append({
                "row_id": row["id"], "row_title": row["title"] or "Sin título",
                "base_id": base["id"], "base_title": base["title"],
                "workspace_id": base["workspace_id"], "workspace_name": base["workspace_name"],
                "visible": visible is None or row["id"] in visible,
                "people": people, "hours": hours, "start": start, "end": end,
                "projects": projects, "split": split,
            })
    titles: dict[str, str] = {}
    if project_ids:
        ids = sorted(project_ids)
        for desde in range(0, len(ids), 400):
            trozo = ids[desde:desde + 400]
            for r in conn.execute(
                f"SELECT id, title FROM pages WHERE id IN ({','.join('?' * len(trozo))})", trozo
            ):
                titles[r["id"]] = r["title"] or "Sin título"
    for t in staged:
        share = t["hours"] / len(t["people"]) if t["split"] == "equal" else t["hours"]
        project_title = titles.get(t["projects"][0]) if t["projects"] else None
        for person in t["people"]:
            if by_day:
                for dia, h in calendar.spread(person, t["start"], t["end"], share) or [(None, 0.0)]:
                    out.append({
                        "person_id": person,
                        "workspace_id": t["workspace_id"], "workspace_name": t["workspace_name"],
                        "project": project_title, "project_id": t["projects"][0] if t["projects"] else None,
                        "base_id": t["base_id"], "base_title": t["base_title"],
                        "row_id": t["row_id"], "row_title": t["row_title"],
                        "day": dia.isoformat() if dia else None,
                        "hours": round(h, 4),
                        "estimated": t["hours"] > 0,
                        "start": t["start"].isoformat() if t["start"] else None,
                        "end": t["end"].isoformat() if t["end"] else None,
                        "visible": t["visible"],
                    })
                continue
            por_semana: dict[Optional[date], float] = defaultdict(float)
            for dia, h in calendar.spread(person, t["start"], t["end"], share) or [(None, 0.0)]:
                por_semana[monday(dia) if dia else None] += h
            slots = sorted(por_semana.items(), key=lambda x: (x[0] is None, x[0] or date.min))
            for week, h in slots:
                out.append({
                    "person_id": person,
                    "workspace_id": t["workspace_id"], "workspace_name": t["workspace_name"],
                    "project": project_title, "project_id": t["projects"][0] if t["projects"] else None,
                    "base_id": t["base_id"], "base_title": t["base_title"],
                    "row_id": t["row_id"], "row_title": t["row_title"],
                    "week": week.isoformat() if week else None,
                    "hours": round(h, 4),
                    "start": t["start"].isoformat() if t["start"] else None,
                    "end": t["end"].isoformat() if t["end"] else None,
                    "visible": t["visible"],
                })
    return out


def mask(piece: dict[str, Any]) -> dict[str, Any]:
    """La pieza sin títulos ni ids: solo horas, fechas y el nombre del espacio."""
    etiqueta = f"Proyecto privado [{piece['workspace_name']}]"
    return {
        **piece,
        "project": etiqueta if piece.get("project") else None,
        "project_id": None,
        "base_id": None,
        "base_title": etiqueta,
        "row_id": None,
        "row_title": etiqueta,
        "masked": True,
    }


def report(
    conn: sqlite3.Connection,
    *,
    visible: Optional[set[str]],
    today: date,
    weeks: int = 8,
    people: Optional[set[str]] = None,
    workspace_id: Optional[str] = None,
) -> dict[str, Any]:
    """La carga del equipo: ventana de `weeks` semanas desde la de `today`.

    - `people`: solo estas personas (quien ve la carga de unos espacios ve a
      sus miembros, con todo lo suyo junto). `None`, todas.
    - `workspace_id`: solo lo de ese espacio, y la capacidad de cada persona
      por su dedicación a él (sin declarar, el 100 %)."""
    weeks = max(1, min(weeks, MAX_WEEKS))
    first = monday(today)
    window = [first + timedelta(weeks=i) for i in range(weeks)]
    keys = [w.isoformat() for w in window]
    calendar = worktime.Calendar(conn)

    users = {
        r["id"]: r
        for r in conn.execute("SELECT id, username, display_name, color, avatar FROM users")
    }
    from app import auth  # import tardío: `auth` no importa este módulo

    per_person: dict[str, dict[str, Any]] = {}
    for piece in pieces(conn, visible, calendar):
        if people is not None and piece["person_id"] not in people:
            continue
        if workspace_id is not None and piece["workspace_id"] != workspace_id:
            continue
        piece = piece if piece["visible"] else mask(piece)
        piece.pop("visible", None)
        pid = piece["person_id"]
        row = per_person.get(pid)
        if row is None:
            u = users.get(pid)
            row = per_person[pid] = {
                "id": pid,
                "display_name": u["display_name"] if u else None,
                "username": u["username"] if u else None,
                "color": u["color"] if u else None,
                "avatar": auth.avatar_of(u) if u else None,
                # `capacity`: su jornada semanal; `capacity_weeks`: lo que de
                # verdad tiene cada semana (sin festivos ni ausencias), que es
                # contra lo que se mide la saturación.
                "capacity": worktime.weekly(calendar.schedule(pid)),
                "schedule": calendar.schedule(pid),
                "capacity_weeks": {},
                "time_off": {},
                "dedication": calendar.dedications(pid),
                "weeks": {k: 0.0 for k in keys},
                "before": 0.0, "after": 0.0, "unscheduled": 0.0,
                "tasks": set(), "pieces": [],
            }
        week = piece["week"]
        if week is None:
            row["unscheduled"] += piece["hours"]
        elif week in row["weeks"]:
            row["weeks"][week] += piece["hours"]
        elif week < keys[0]:
            row["before"] += piece["hours"]
        else:
            row["after"] += piece["hours"]
        row["tasks"].add(piece["row_id"] or f"{piece['base_title']}:{piece['row_title']}:{piece['person_id']}")
        row["pieces"].append(piece)

    people = []
    for row in per_person.values():
        row["tasks"] = len(row["tasks"])
        parte = calendar.dedication(row["id"], workspace_id) / 100 if workspace_id else 1.0
        if workspace_id:
            row["capacity"] = round(row["capacity"] * parte, 2)
        for w, k in zip(window, keys):
            semana = calendar.week(row["id"], w)
            row["capacity_weeks"][k] = round(semana["capacity"] * parte, 2)
            if semana["holidays"] or semana["absences"]:
                row["time_off"][k] = {"holidays": semana["holidays"], "absences": semana["absences"]}
        row["weeks"] = {k: round(v, 2) for k, v in row["weeks"].items()}
        for k in ("before", "after", "unscheduled"):
            row[k] = round(row[k], 2)
        row["pieces"].sort(key=lambda p: (p["week"] or "9999", p["workspace_name"], p["row_title"]))
        people.append(row)
    people.sort(key=lambda p: ((p["display_name"] or "~").casefold(), p["id"]))
    return {
        "today": today.isoformat(),
        "weeks": keys,
        "capacity_default": DEFAULT_CAPACITY,
        "people": people,
    }
