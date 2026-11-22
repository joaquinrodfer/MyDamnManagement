"""La vista nueva de la carga (plan de la carga, C3).

Una rejilla **fila × periodo**:

- **Escala:** día, semana (de lunes a domingo) o mes.
- **Filas:** persona, grupo (de Administración), espacio o proyecto.
- **Celda:** las horas que caen en ese periodo y, cuando la fila es de personas
  (una persona o un grupo), la capacidad de ese periodo: su jornada, menos
  festivos y ausencias (y por su dedicación si se mira un solo espacio). Con las
  dos sale el semáforo.
- **Detalle:** las tareas que llenan cada celda.
- **Huecos:** las horas «sin fecha» y las tareas «sin estimar» (con
  responsable pero sin horas), que no caben en ningún periodo.

Sale de las mismas piezas que `/workload` (`workload.pieces`, ahora por día) y
con las mismas reglas de quién ve qué: las personas que entran y los títulos
enmascarados los decide quien llama.
"""

from __future__ import annotations

import calendar as cal_mod
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Optional

from app import worktime
from app.workload import mask, pieces

SCALES = ("day", "week", "month")
GROUPS = ("person", "group", "workspace", "project")
DEFAULT_PERIODS = {"day": 14, "week": 8, "month": 6}
MAX_PERIODS = {"day": 62, "week": 26, "month": 12}


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def periods(scale: str, start: date, count: int) -> list[tuple[str, date, date]]:
    """`[(clave, primer día, último día)]` desde el periodo que contiene `start`."""
    out = []
    if scale == "day":
        for i in range(count):
            d = start + timedelta(days=i)
            out.append((d.isoformat(), d, d))
    elif scale == "week":
        first = _monday(start)
        for i in range(count):
            a = first + timedelta(weeks=i)
            out.append((a.isoformat(), a, a + timedelta(days=6)))
    else:
        first = date(start.year, start.month, 1)
        for i in range(count):
            a = _add_months(first, i)
            b = date(a.year, a.month, cal_mod.monthrange(a.year, a.month)[1])
            out.append((a.strftime("%Y-%m"), a, b))
    return out


def _key_of(scale: str, day: date) -> str:
    if scale == "day":
        return day.isoformat()
    if scale == "week":
        return _monday(day).isoformat()
    return day.strftime("%Y-%m")


def grid(
    conn: sqlite3.Connection,
    *,
    visible: Optional[set[str]],
    today: date,
    scale: str = "week",
    group: str = "person",
    start: Optional[date] = None,
    count: Optional[int] = None,
    people: Optional[set[str]] = None,
    workspace_id: Optional[str] = None,
    manageable: Optional[set[str]] = None,
) -> dict[str, Any]:
    scale = scale if scale in SCALES else "week"
    group = group if group in GROUPS else "person"
    count = max(1, min(count or DEFAULT_PERIODS[scale], MAX_PERIODS[scale]))
    tramos = periods(scale, start or today, count)
    keys = [k for k, _a, _b in tramos]
    first_day, last_day = tramos[0][1], tramos[-1][2]
    calendar = worktime.Calendar(conn)

    users = {r["id"]: r for r in conn.execute("SELECT id, username, display_name, color, avatar FROM users")}
    from app import auth  # import tardío

    # Grupos de Administración (para agrupar por grupo).
    group_of: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if group == "group":
        for r in conn.execute(
            "SELECT g.id, g.name, gm.user_id FROM groups g JOIN group_members gm ON gm.group_id = g.id"
        ):
            group_of[r["user_id"]].append((r["id"], r["name"]))

    rows: dict[str, dict[str, Any]] = {}
    gaps = {"unscheduled_hours": 0.0, "unscheduled": [], "unestimated": []}
    vistos_sin_estimar: set[tuple] = set()

    def fila(rid: str, nombre: str, **extra) -> dict[str, Any]:
        r = rows.get(rid)
        if r is None:
            r = rows[rid] = {
                "id": rid, "name": nombre, "hours": {k: 0.0 for k in keys},
                "capacity": None, "before": 0.0, "after": 0.0, "unscheduled": 0.0,
                "tasks": set(), "cells": defaultdict(list), "members": set(), **extra,
            }
        return r

    def destinos(pz: dict[str, Any]) -> list[dict[str, Any]]:
        pid = pz["person_id"]
        if group == "person":
            u = users.get(pid)
            return [fila(
                pid, (u["display_name"] if u else None) or "(ya no está)", kind="person",
                color=u["color"] if u else None, avatar=auth.avatar_of(u) if u else None,
                manageable=manageable is None or pid in manageable,
            )]
        if group == "group":
            gs = group_of.get(pid) or [("", "Sin grupo")]
            return [fila(f"group:{gid}", nombre, kind="group") for gid, nombre in gs]
        if group == "workspace":
            return [fila(f"ws:{pz['workspace_id']}", pz["workspace_name"], kind="workspace")]
        if pz.get("masked"):
            return [fila(f"masked:{pz['workspace_id']}", pz["project"] or pz["base_title"], kind="project")]
        if pz.get("project_id"):
            return [fila(f"project:{pz['project_id']}", pz["project"] or "Sin título", kind="project")]
        return [fila("project:", "Sin proyecto", kind="project")]

    for pz in pieces(conn, visible, calendar, by_day=True):
        if people is not None and pz["person_id"] not in people:
            continue
        if workspace_id is not None and pz["workspace_id"] != workspace_id:
            continue
        pz = pz if pz["visible"] else mask(pz)
        pz.pop("visible", None)
        u = users.get(pz["person_id"])
        pz["person_name"] = (u["display_name"] if u else None) or "(ya no está)"
        tarea = pz["row_id"] or f"{pz['base_title']}:{pz['row_title']}:{pz['person_id']}"
        if not pz["estimated"]:
            clave = (tarea, pz["person_id"])
            if clave not in vistos_sin_estimar:
                vistos_sin_estimar.add(clave)
                gaps["unestimated"].append({k: pz[k] for k in (
                    "row_id", "row_title", "base_title", "workspace_name", "person_name", "end", "masked"
                ) if k in pz})
            # Con un peso por defecto (C4) cuenta; sin él, solo sale aquí.
            if pz["hours"] <= 0:
                continue
        for r in destinos(pz):
            r["tasks"].add(tarea)
            r["members"].add(pz["person_id"])
            if pz["day"] is None:
                r["unscheduled"] += pz["hours"]
                continue
            dia = date.fromisoformat(pz["day"])
            if dia < first_day:
                r["before"] += pz["hours"]
            elif dia > last_day:
                r["after"] += pz["hours"]
            else:
                k = _key_of(scale, dia)
                r["hours"][k] += pz["hours"]
                r["cells"][k].append(pz)
        if pz["day"] is None:
            gaps["unscheduled_hours"] += pz["hours"]
            gaps["unscheduled"].append({k: pz[k] for k in (
                "row_id", "row_title", "base_title", "workspace_name", "person_name", "hours", "masked"
            ) if k in pz})

    # Capacidad: solo en filas de personas (una persona o un grupo).
    for r in rows.values():
        if r["kind"] not in ("person", "group"):
            continue
        miembros = [r["id"]] if r["kind"] == "person" else sorted(r["members"])
        cap = {}
        for k, a, b in tramos:
            total = 0.0
            for persona in miembros:
                parte = calendar.dedication(persona, workspace_id) / 100 if workspace_id else 1.0
                d = a
                while d <= b:
                    total += calendar.available(persona, d) * parte
                    d += timedelta(days=1)
            cap[k] = round(total, 2)
        r["capacity"] = cap

    salida = []
    for r in rows.values():
        celdas = {}
        for k, lista in r["cells"].items():
            juntas: dict[tuple, dict[str, Any]] = {}
            for pz in lista:
                clave = (pz["row_id"] or pz["row_title"], pz["person_id"])
                if clave in juntas:
                    juntas[clave]["hours"] += pz["hours"]
                else:
                    juntas[clave] = {c: pz.get(c) for c in (
                        "row_id", "row_title", "base_title", "project", "workspace_name",
                        "person_id", "person_name", "hours", "start", "end", "masked",
                    )}
            celdas[k] = sorted(
                ({**v, "hours": round(v["hours"], 2)} for v in juntas.values()),
                key=lambda x: -x["hours"],
            )
        salida.append({
            **{k: v for k, v in r.items() if k not in ("cells", "members", "tasks")},
            "hours": {k: round(v, 2) for k, v in r["hours"].items()},
            "before": round(r["before"], 2), "after": round(r["after"], 2),
            "unscheduled": round(r["unscheduled"], 2),
            "tasks": len(r["tasks"]),
            "people": len(r["members"]),
            "cells": celdas,
        })
    salida.sort(key=lambda r: (r["name"] or "~").casefold())
    gaps["unscheduled_hours"] = round(gaps["unscheduled_hours"], 2)
    return {
        "today": today.isoformat(),
        "scale": scale,
        "group": group,
        "periods": [{"key": k, "start": a.isoformat(), "end": b.isoformat()} for k, a, b in tramos],
        "rows": salida,
        "gaps": gaps,
    }
