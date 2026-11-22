"""Cómo cuenta la carga en cada espacio (plan de la carga, C4 y C5).

Va en los ajustes del espacio (`workspaces.settings["workload"]`); sin tocar
nada, lo de siempre: horas, sin peso para lo no estimado y el semáforo al 70 y
al 100 %.

**Unidad de esfuerzo (C4).** Qué significa el número de la columna de esfuerzo
de las bases de Tareas del espacio:

| `unit`   | el número es…       | horas                               |
|----------|---------------------|-------------------------------------|
| `hours`  | horas               | tal cual                            |
| `days`   | días                | × `hours_per_unit` (8 por defecto)  |
| `points` | puntos              | × `hours_per_unit` (4 por defecto)  |
| `sizes`  | una talla (S, M, L…) de la columna «Talla» | lo que diga `sizes` |
| `tasks`  | (nada: cuenta la tarea) | `hours_per_unit` por tarea (4)  |

`default_hours`: lo que cuenta una tarea con responsable y sin estimar (0 = no
cuenta; sigue saliendo en «Sin estimar»).

**Semáforo (C5).** `warn` (amarillo desde, %), `over` (rojo por encima de, %) y
`under` (infrautilizado por debajo de, %; 0 = sin aviso).
"""

from __future__ import annotations

import json
from typing import Any, Optional

UNITS: dict[str, tuple[str, Optional[float]]] = {
    "hours": ("Horas", 1.0),
    "days": ("Días", 8.0),
    "points": ("Puntos", 4.0),
    "sizes": ("Tallas", None),
    "tasks": ("Número de tareas", 4.0),
}
DEFAULT_SIZES = {"XS": 1.0, "S": 2.0, "M": 4.0, "L": 8.0, "XL": 16.0}
DEFAULTS: dict[str, Any] = {
    "unit": "hours",
    "hours_per_unit": None,  # None = el de la unidad
    "sizes": DEFAULT_SIZES,
    "default_hours": 0.0,
    "warn": 70.0,
    "over": 100.0,
    "under": 0.0,
}


def _num(value: Any, minimo: float, maximo: float, nombre: str) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{nombre}: tiene que ser un número.") from None
    if not (minimo <= n <= maximo):
        raise ValueError(f"{nombre}: va de {minimo:g} a {maximo:g}.")
    return round(n, 2)


def validate(raw: Any) -> dict[str, Any]:
    """Los ajustes de la carga de un espacio, comprobados (ValueError si no valen)."""
    if not isinstance(raw, dict):
        raise ValueError("Los ajustes de la carga van como un objeto.")
    out = dict(DEFAULTS)
    if "unit" in raw:
        if raw["unit"] not in UNITS:
            raise ValueError("La unidad es horas, días, puntos, tallas o número de tareas.")
        out["unit"] = raw["unit"]
    if raw.get("hours_per_unit") is not None:
        out["hours_per_unit"] = _num(raw["hours_per_unit"], 0.1, 200, "La equivalencia en horas")
    if raw.get("sizes") is not None:
        if not isinstance(raw["sizes"], dict) or not raw["sizes"]:
            raise ValueError("Las tallas van como {talla: horas}.")
        tallas = {}
        for k, v in raw["sizes"].items():
            nombre = str(k).strip()[:20]
            if nombre:
                tallas[nombre] = _num(v, 0, 1000, f"La talla {nombre}")
        if not tallas:
            raise ValueError("Hace falta al menos una talla.")
        out["sizes"] = tallas
    if raw.get("default_hours") is not None:
        out["default_hours"] = _num(raw["default_hours"], 0, 200, "El peso de lo no estimado")
    for clave, nombre in (("warn", "El umbral amarillo"), ("over", "El umbral rojo"), ("under", "El umbral de infrautilización")):
        if raw.get(clave) is not None:
            out[clave] = _num(raw[clave], 0, 500, nombre)
    if out["warn"] > out["over"]:
        raise ValueError("El amarillo tiene que empezar antes que el rojo.")
    if out["under"] and out["under"] >= out["warn"]:
        raise ValueError("La infrautilización tiene que estar por debajo del amarillo.")
    return out


def of_settings(settings: Any) -> dict[str, Any]:
    """Los ajustes de la carga de un espacio (de su `settings`), con los valores
    por defecto donde no haya nada. Nunca falla: lo roto, por defecto."""
    if isinstance(settings, str):
        try:
            settings = json.loads(settings or "{}")
        except ValueError:
            settings = {}
    raw = (settings or {}).get("workload") if isinstance(settings, dict) else None
    if not isinstance(raw, dict):
        return dict(DEFAULTS)
    try:
        return validate(raw)
    except ValueError:
        return dict(DEFAULTS)


def per_unit(cfg: dict[str, Any]) -> float:
    return float(cfg.get("hours_per_unit") or UNITS[cfg["unit"]][1] or 1.0)


def to_hours(cfg: dict[str, Any], value: Any, size: Any = None) -> tuple[float, bool]:
    """`(horas, estimada)` de una tarea: su esfuerzo pasado a horas y si tenía
    estimación. Sin estimación, `default_hours` (y `estimada` False)."""
    unit = cfg["unit"]
    if unit == "tasks":
        return per_unit(cfg), True
    if unit == "sizes":
        talla = str(size).strip() if size not in (None, "") else ""
        if talla and talla in cfg["sizes"]:
            return float(cfg["sizes"][talla]), True
        # Sin talla: si la columna de esfuerzo tiene un número, son horas.
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = 0.0
    if n > 0:
        factor = 1.0 if unit in ("hours", "sizes") else per_unit(cfg)
        return n * factor, True
    return float(cfg.get("default_hours") or 0.0), False


def thresholds(cfg: dict[str, Any]) -> dict[str, float]:
    return {"warn": cfg["warn"], "over": cfg["over"], "under": cfg["under"]}
