"""Cuántas horas tiene cada persona cada día (plan de la carga, fase C1).

La capacidad deja de ser «40 h a la semana» fija y pasa a calcularse día a día:

- **Jornada** de cada persona: horas por día de la semana, de lunes a domingo
  (`work_schedules`). Sin jornada propia vale la capacidad semanal que se
  guardaba antes (`workload_capacity`, repartida de lunes a viernes) y, sin
  nada, 8 h de lunes a viernes. La edita solo un administrador.
- **Festivos**: comunes a toda la empresa (`workspace_id` nulo) y propios de
  cada espacio. A una persona le cuentan los comunes y los de los espacios de
  los que es miembro (directamente o por un grupo).
- **Ausencias** (vacaciones, baja, formación…): un rango de días de una
  persona. Sin `hours`, el día entero; con `hours`, las horas que falta ese día
  (una mañana libre). Cada persona registra las suyas; un administrador, las de
  cualquiera.
- **Dedicación** de una persona a cada espacio, en %: la parte de su capacidad
  que le toca a ese espacio cuando se mira la carga de un solo espacio. Sin
  dedicación declarada, el 100 % (toda su capacidad).

`Calendar` lo junta todo una vez por informe y responde `available(persona,
día)` sin volver a la base de datos.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app import index as index_mod

DAYS = 7
DEFAULT_DAY_HOURS: tuple[float, ...] = (8.0, 8.0, 8.0, 8.0, 8.0, 0.0, 0.0)
META_CAPACITY = "workload_capacity"  # lo de antes: {user_id: horas por semana}
ABSENCE_KINDS = ("vacaciones", "baja", "formacion", "personal", "otro")
ABSENCE_LABELS = {
    "vacaciones": "Vacaciones",
    "baja": "Baja",
    "formacion": "Formación",
    "personal": "Asuntos propios",
    "otro": "Otra ausencia",
}
MAX_ABSENCE_DAYS = 366


def _num(value: Any) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return n if n > 0 else 0.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# jornada
# --------------------------------------------------------------------------


def _legacy_weekly(conn: sqlite3.Connection) -> dict[str, float]:
    raw = index_mod.get_meta(conn, META_CAPACITY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return {str(k): _num(v) for k, v in data.items() if _num(v) > 0}


def from_weekly(hours: float) -> list[float]:
    """Una capacidad semanal de las de antes, de lunes a viernes por igual."""
    per_day = round(hours / 5, 4)
    return [per_day] * 5 + [0.0, 0.0]


def clean_hours(hours: Iterable[Any]) -> list[float]:
    """Siete horas válidas (0–24) o `ValueError` con el motivo."""
    lista = list(hours)
    if len(lista) != DAYS:
        raise ValueError("La jornada son siete valores, de lunes a domingo.")
    salida = []
    for h in lista:
        try:
            n = float(h or 0)
        except (TypeError, ValueError):
            raise ValueError("Cada día de la jornada es un número de horas.") from None
        if n < 0 or n > 24:
            raise ValueError("Un día de la jornada va de 0 a 24 horas.")
        salida.append(round(n, 2))
    return salida


def schedules(conn: sqlite3.Connection) -> dict[str, list[float]]:
    """La jornada declarada de cada persona que tiene una (las demás, `schedule_of`)."""
    salida: dict[str, list[float]] = {}
    for uid, weekly in _legacy_weekly(conn).items():
        salida[uid] = from_weekly(weekly)
    for r in conn.execute("SELECT user_id, hours_json FROM work_schedules"):
        try:
            salida[str(r[0])] = clean_hours(json.loads(r[1]))
        except (ValueError, TypeError):
            continue
    return salida


def schedule_of(conn: sqlite3.Connection, user_id: Any) -> list[float]:
    return schedules(conn).get(str(user_id), list(DEFAULT_DAY_HOURS))


def set_schedule(conn: sqlite3.Connection, user_id: Any, hours: Optional[Iterable[Any]]) -> list[float]:
    """Guarda la jornada (o la quita con `None`: vuelve a la de por defecto)."""
    uid = str(user_id)
    # La capacidad semanal de antes se olvida al tocar la jornada: si no, quitar
    # la jornada devolvería un número viejo que nadie ve.
    legacy = _legacy_weekly(conn)
    if uid in legacy:
        legacy.pop(uid)
        index_mod.set_meta(conn, META_CAPACITY, json.dumps(legacy, sort_keys=True))
    if hours is None:
        conn.execute("DELETE FROM work_schedules WHERE user_id = ?", (uid,))
        return list(DEFAULT_DAY_HOURS)
    limpias = clean_hours(hours)
    conn.execute(
        "INSERT INTO work_schedules (user_id, hours_json) VALUES (?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET hours_json = excluded.hours_json",
        (uid, json.dumps(limpias)),
    )
    return limpias


def weekly(hours: Iterable[float]) -> float:
    return round(sum(hours), 2)


# --------------------------------------------------------------------------
# dedicación por espacio
# --------------------------------------------------------------------------


def dedications(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    salida: dict[str, dict[str, float]] = defaultdict(dict)
    for r in conn.execute("SELECT user_id, workspace_id, percent FROM workspace_dedication"):
        salida[str(r[0])][str(r[1])] = float(r[2])
    return dict(salida)


def set_dedication(conn: sqlite3.Connection, user_id: Any, mapping: dict[str, Any]) -> dict[str, float]:
    """Sustituye la dedicación de una persona. Cada valor de 0 a 100 y la suma,
    como mucho 100: no se puede dedicar a los espacios más de lo que se tiene."""
    limpio: dict[str, float] = {}
    for ws, pct in (mapping or {}).items():
        if pct is None or pct == "":
            continue
        try:
            n = float(pct)
        except (TypeError, ValueError):
            raise ValueError("La dedicación es un porcentaje.") from None
        if n < 0 or n > 100:
            raise ValueError("La dedicación a un espacio va de 0 a 100 %.")
        limpio[str(ws)] = round(n, 2)
    if sum(limpio.values()) > 100.0001:
        raise ValueError("La dedicación a todos los espacios suma más del 100 %.")
    existentes = {r[0] for r in conn.execute("SELECT id FROM workspaces")}
    desconocidos = [ws for ws in limpio if ws not in existentes]
    if desconocidos:
        raise ValueError("Uno de los espacios de la dedicación no existe.")
    uid = str(user_id)
    conn.execute("DELETE FROM workspace_dedication WHERE user_id = ?", (uid,))
    conn.executemany(
        "INSERT INTO workspace_dedication (user_id, workspace_id, percent) VALUES (?, ?, ?)",
        [(uid, ws, pct) for ws, pct in limpio.items()],
    )
    return limpio


# --------------------------------------------------------------------------
# festivos
# --------------------------------------------------------------------------


def _day(value: Any) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError("La fecha tiene que ser AAAA-MM-DD.") from None


def holidays(
    conn: sqlite3.Connection,
    workspace_id: Optional[str] = None,
    *,
    year: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Los festivos de un ámbito: comunes (`workspace_id` None) o de un espacio."""
    sql = (
        "SELECT h.id, h.workspace_id, h.day, h.name, h.source_id, s.name AS source_name"
        " FROM holidays h LEFT JOIN holiday_sources s ON s.id = h.source_id WHERE "
    )
    args: list[Any] = []
    if workspace_id is None:
        sql += "h.workspace_id IS NULL"
    else:
        sql += "h.workspace_id = ?"
        args.append(str(workspace_id))
    if year is not None:
        sql += " AND h.day LIKE ?"
        args.append(f"{int(year):04d}-%")
    sql += " ORDER BY h.day, h.name"
    return [dict(r) for r in conn.execute(sql, args)]


def add_holiday(
    conn: sqlite3.Connection,
    workspace_id: Optional[str],
    day: Any,
    name: str,
    source_id: Optional[int] = None,
) -> dict[str, Any]:
    dia = _day(day).isoformat()
    nombre = (name or "").strip()[:200] or "Festivo"
    ws = str(workspace_id) if workspace_id else None
    # El mismo día dos veces en el mismo ámbito no suma nada: se queda el que hay.
    previo = conn.execute(
        "SELECT id FROM holidays WHERE day = ? AND workspace_id IS ? AND source_id IS ?",
        (dia, ws, source_id),
    ).fetchone()
    if previo is not None:
        return {"id": previo[0], "workspace_id": ws, "day": dia, "name": nombre, "source_id": source_id}
    cur = conn.execute(
        "INSERT INTO holidays (workspace_id, day, name, source_id) VALUES (?, ?, ?, ?)",
        (ws, dia, nombre, source_id),
    )
    return {"id": cur.lastrowid, "workspace_id": ws, "day": dia, "name": nombre, "source_id": source_id}


def get_holiday(conn: sqlite3.Connection, holiday_id: int) -> Optional[dict[str, Any]]:
    r = conn.execute("SELECT * FROM holidays WHERE id = ?", (int(holiday_id),)).fetchone()
    return dict(r) if r else None


def delete_holiday(conn: sqlite3.Connection, holiday_id: int) -> bool:
    return conn.execute("DELETE FROM holidays WHERE id = ?", (int(holiday_id),)).rowcount > 0


# --------------------------------------------------------------------------
# ausencias
# --------------------------------------------------------------------------


def absences(
    conn: sqlite3.Connection,
    user_id: Optional[str] = None,
    *,
    since: Optional[date] = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM absences WHERE 1 = 1"
    args: list[Any] = []
    if user_id is not None:
        sql += " AND user_id = ?"
        args.append(str(user_id))
    if since is not None:
        sql += " AND end >= ?"
        args.append(since.isoformat())
    sql += " ORDER BY start, id"
    salida = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["label"] = ABSENCE_LABELS.get(d["kind"], d["kind"])
        salida.append(d)
    return salida


def add_absence(
    conn: sqlite3.Connection,
    user_id: str,
    start: Any,
    end: Any,
    kind: str,
    *,
    hours: Optional[float] = None,
    note: str = "",
    created_by: Optional[str] = None,
) -> dict[str, Any]:
    inicio, fin = _day(start), _day(end or start)
    if fin < inicio:
        raise ValueError("La ausencia acaba antes de empezar.")
    if (fin - inicio).days + 1 > MAX_ABSENCE_DAYS:
        raise ValueError("Una ausencia dura como mucho un año: divídela en varias.")
    if kind not in ABSENCE_KINDS:
        raise ValueError("Ese tipo de ausencia no existe.")
    if hours is not None:
        h = float(hours)
        if h <= 0 or h > 24:
            raise ValueError("Las horas de una ausencia parcial van de 0 a 24.")
        hours = round(h, 2)
    cur = conn.execute(
        "INSERT INTO absences (user_id, start, end, kind, hours, note, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(user_id), inicio.isoformat(), fin.isoformat(), kind, hours, (note or "").strip()[:500], created_by, _now()),
    )
    return get_absence(conn, cur.lastrowid)  # type: ignore[return-value]


def get_absence(conn: sqlite3.Connection, absence_id: int) -> Optional[dict[str, Any]]:
    r = conn.execute("SELECT * FROM absences WHERE id = ?", (int(absence_id),)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["label"] = ABSENCE_LABELS.get(d["kind"], d["kind"])
    return d


def delete_absence(conn: sqlite3.Connection, absence_id: int) -> bool:
    return conn.execute("DELETE FROM absences WHERE id = ?", (int(absence_id),)).rowcount > 0


# --------------------------------------------------------------------------
# el calendario de un informe
# --------------------------------------------------------------------------


def _memberships(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Los espacios de cada persona (directo o por grupo)."""
    salida: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute(
        "SELECT workspace_id, subject_id FROM workspace_members WHERE subject_type = 'user'"
    ):
        salida[str(r[1])].add(str(r[0]))
    for r in conn.execute(
        "SELECT m.workspace_id, gm.user_id FROM workspace_members m"
        " JOIN group_members gm ON gm.group_id = m.subject_id WHERE m.subject_type = 'group'"
    ):
        salida[str(r[1])].add(str(r[0]))
    return salida


class Calendar:
    """La jornada, los festivos y las ausencias de todo el equipo, en memoria."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._schedules = schedules(conn)
        self._dedications = dedications(conn)
        self._members = _memberships(conn)
        self._common: dict[str, str] = {}
        self._by_space: dict[str, dict[str, str]] = defaultdict(dict)
        for r in conn.execute("SELECT workspace_id, day, name FROM holidays"):
            if r[0] is None:
                self._common.setdefault(r[1], r[2])
            else:
                self._by_space[str(r[0])].setdefault(r[1], r[2])
        # {persona: {día: horas que falta (None = el día entero), tipo}}
        self._off: dict[str, dict[str, tuple[Optional[float], str]]] = defaultdict(dict)
        for r in conn.execute("SELECT user_id, start, end, kind, hours FROM absences"):
            try:
                d, fin = date.fromisoformat(r[1]), date.fromisoformat(r[2])
            except ValueError:
                continue
            while d <= fin:
                previo = self._off[str(r[0])].get(d.isoformat())
                horas = None if r[4] is None else float(r[4])
                if previo is None or previo[0] is not None:
                    # Dos ausencias el mismo día: el día entero gana; dos
                    # parciales se suman.
                    if horas is not None and previo is not None:
                        horas = horas + (previo[0] or 0)
                    self._off[str(r[0])][d.isoformat()] = (horas, r[3])
                d += timedelta(days=1)

    def schedule(self, person: str) -> list[float]:
        return self._schedules.get(str(person), list(DEFAULT_DAY_HOURS))

    def nominal(self, person: str, day: date) -> float:
        """Las horas de su jornada ese día de la semana, sin festivos ni ausencias."""
        return self.schedule(person)[day.weekday()]

    def holiday(self, person: str, day: date) -> Optional[str]:
        iso = day.isoformat()
        if iso in self._common:
            return self._common[iso]
        for ws in self._members.get(str(person), ()):
            nombre = self._by_space.get(ws, {}).get(iso)
            if nombre:
                return nombre
        return None

    def absence(self, person: str, day: date) -> Optional[tuple[Optional[float], str]]:
        return self._off.get(str(person), {}).get(day.isoformat())

    def available(self, person: str, day: date) -> float:
        """Las horas que de verdad tiene ese día: jornada, menos festivo y ausencia."""
        h = self.nominal(person, day)
        if h <= 0 or self.holiday(person, day):
            return 0.0
        ausencia = self.absence(person, day)
        if ausencia is not None:
            if ausencia[0] is None:
                return 0.0
            h = max(0.0, h - ausencia[0])
        return h

    def week(self, person: str, monday: date) -> dict[str, Any]:
        """La semana de una persona: capacidad real y qué la recorta."""
        disponible = 0.0
        nominal = 0.0
        festivos = 0
        ausencias = 0
        for i in range(DAYS):
            d = monday + timedelta(days=i)
            n = self.nominal(person, d)
            nominal += n
            disponible += self.available(person, d)
            if n > 0 and self.holiday(person, d):
                festivos += 1
            elif n > 0 and self.absence(person, d):
                ausencias += 1
        return {
            "capacity": round(disponible, 2),
            "nominal": round(nominal, 2),
            "holidays": festivos,
            "absences": ausencias,
        }

    def dedication(self, person: str, workspace_id: str) -> float:
        """El % de su capacidad que le toca a un espacio (100 sin declarar)."""
        propias = self._dedications.get(str(person))
        if not propias:
            return 100.0
        return propias.get(str(workspace_id), 0.0)

    def dedications(self, person: str) -> dict[str, float]:
        return dict(self._dedications.get(str(person), {}))

    def spread(
        self, person: str, start: Optional[date], end: Optional[date], hours: float
    ) -> list[tuple[Optional[date], float]]:
        """`[(día, horas)]`: las horas de una tarea repartidas en los días que
        esa persona trabaja, en proporción a las horas que tiene cada día.

        - Sin fechas: `[(None, horas)]` (va a «sin fecha»).
        - Con una sola fecha: todo ese día.
        - Con rango: entre los días con horas disponibles (jornada menos
          festivos y ausencias). Si no hay ninguno —la persona está de
          vacaciones todo el rango—, entre los días de su jornada, para que la
          sobrecarga se vea en vez de desaparecer; y si su jornada no tiene
          ningún día en el rango, todo el día de fin.
        """
        if hours <= 0:
            return []
        if start is None and end is None:
            return [(None, hours)]
        if start is None or end is None:
            return [(start or end, hours)]
        if end < start:
            start, end = end, start
        dias = []
        d = start
        while d <= end:
            dias.append(d)
            d += timedelta(days=1)
        pesos = [self.available(person, x) for x in dias]
        if sum(pesos) <= 0:
            pesos = [self.nominal(person, x) for x in dias]
        total = sum(pesos)
        if total <= 0:
            return [(end, hours)]
        return [(x, hours * w / total) for x, w in zip(dias, pesos) if w > 0]
