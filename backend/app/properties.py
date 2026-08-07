"""Validación ligera de `properties` de una fila contra el schema_def de su database.

Deliberadamente laxa: valida que las claves existan en el schema y hace una
comprobación de tipo básica (numérico, booleano, opciones de select), pero
no fuerza campos obligatorios ni formatos estrictos de fecha/url — eso puede
endurecerse más adelante si hace falta, sin romper filas ya guardadas.
"""

from typing import Any

from fastapi import HTTPException


def validate_properties(schema_def: list[dict], properties: dict[str, Any]) -> dict[str, Any]:
    allowed = {p["key"]: p for p in schema_def}
    unknown = set(properties) - set(allowed)
    if unknown:
        raise HTTPException(400, f"Propiedades no definidas en el schema: {', '.join(sorted(unknown))}")

    return {key: _coerce(allowed[key], value) for key, value in properties.items()}


def _coerce(prop: dict, value: Any) -> Any:
    if value is None:
        return None

    ptype = prop.get("type")

    if ptype == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"'{prop['key']}' debe ser numérico")

    if ptype == "checkbox":
        if not isinstance(value, bool):
            raise HTTPException(400, f"'{prop['key']}' debe ser true/false")
        return value

    if ptype == "select":
        options = prop.get("options")
        if options and value not in options:
            raise HTTPException(400, f"'{prop['key']}': '{value}' no está en options {options}")
        return value

    if ptype == "multiselect":
        if not isinstance(value, list):
            raise HTTPException(400, f"'{prop['key']}' debe ser una lista")
        options = prop.get("options")
        if options:
            bad = [v for v in value if v not in options]
            if bad:
                raise HTTPException(400, f"'{prop['key']}': valores no válidos {bad}")
        return value

    return value  # text, date, url, relation: sin validar formato en v1
