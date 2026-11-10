"""Validación ligera de `properties` de una fila contra el schema_def de su database.

Deliberadamente laxa: valida que las claves existan en el schema y hace una
comprobación de tipo básica (numérico, booleano, opciones de select), pero
no fuerza campos obligatorios ni formatos estrictos de fecha/url — eso puede
endurecerse más adelante si hace falta, sin romper filas ya guardadas.

Aquí vive también el **plan** del schema: la interfaz solo pide el nombre
visible de una propiedad y la clave se deriva detrás. La clave no se enseña
nunca, así que el cliente no tiene que saber snake_case; y cuando el nombre de
una propiedad existente cambia, el plan dice qué claves hay que migrar en las
filas y en las vistas.
"""

import re
import unicodedata
from typing import Any

from fastapi import HTTPException

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Nombre visible de la columna del título cuando la base no la ha renombrado.
DEFAULT_TITLE_NAME = "Título"


def slug_key(name: str, taken: set[str]) -> str:
    """La clave interna de un nombre visible: `Fecha de entrega` -> `fecha_de_entrega`.

    Sin tildes (NFD + fuera las marcas), en minúsculas, y todo lo que no sea
    letra o dígito se convierte en `_` (varios seguidos son uno, sin `_` en los
    extremos). Si el nombre no deja nada (`···`), la clave es `propiedad`; si
    empieza por dígito, se prefija `p_` porque el contrato exige empezar por
    letra. Si choca con una clave ya usada, sufijo numérico: `fecha_2`, `fecha_3`.
    """
    plano = unicodedata.normalize("NFKD", str(name or ""))
    plano = "".join(c for c in plano if not unicodedata.combining(c)).lower()
    base = re.sub(r"[^a-z0-9]+", "_", plano).strip("_")
    if not base:
        base = "propiedad"
    if not base[0].isalpha():
        base = f"p_{base}"
    key = base
    attempt = 2
    while key in taken:
        key = f"{base}_{attempt}"
        attempt += 1
    return key


def plan_schema(
    current: list[dict], requested: list[dict]
) -> tuple[list[dict], dict[str, str], list[str]]:
    """Traduce lo que pide el diálogo al `schema` que se guarda.

    `requested` trae, por propiedad, su `key` (la que tenía: la identidad de la
    fila en el diálogo; vacía en una propiedad nueva) y el **nombre** que ha
    escrito el usuario. Devuelve `(schema_final, renombres, borradas)`:

    - `schema_final`: las propiedades con su clave definitiva. Una entrada con
      `key` vacía la deriva del nombre; una con `key` desconocida la respeta (la
      API sigue aceptando claves explícitas, como los tests y las plantillas).
    - `renombres`: `{clave_vieja: clave_nueva}` de las propiedades cuyo **nombre
      ha cambiado**. Una clave vieja que el usuario no ha tocado no se
      «normaliza» sola: las bases con `proximo_contacto` siguen como están
      hasta que alguien renombre esa propiedad de verdad.
    - `borradas`: claves que ya no están en el schema (para limpiar las vistas
      que las nombran en `group_by`, `date_property`, `filters`, `sort` o
      `visible_properties`).

    Dos propiedades no pueden quedar con la misma clave; si el nombre nuevo
    choca con una existente, se le pone sufijo numérico (la clave es interna,
    el usuario solo ve nombres).
    """
    actuales = {str(p.get("key")): p for p in current if isinstance(p, dict) and p.get("key")}
    renombres: dict[str, str] = {}

    def nombre(item: dict) -> str:
        return str(item.get("name") or "").strip()

    # Primero, qué claves se quedan: las que vienen con su clave de siempre y
    # el mismo nombre. Todo lo demás (nombre cambiado) es un renombrado.
    conservadas: set[str] = set()
    for item in requested:
        key = str(item.get("key") or "").strip()
        if key in actuales and nombre(item).casefold() == nombre(actuales[key]).casefold():
            conservadas.add(key)
    taken: set[str] = set(conservadas)

    finales: list[dict[str, Any]] = []
    for item in requested:
        key = str(item.get("key") or "").strip()
        name = nombre(item)
        if not name:
            raise HTTPException(400, "Cada propiedad necesita un nombre")
        if key in actuales and key not in conservadas:
            # Renombrado: el nombre visible ha cambiado. La clave nueva se
            # deriva del nombre y se migra en filas y vistas.
            nueva = slug_key(name, taken)
            renombres[key] = nueva
            key = nueva
        elif key and key not in actuales:
            # Clave explícita de una propiedad nueva (plantillas y API directa).
            if not KEY_RE.match(key):
                raise HTTPException(400, f"Clave no válida: {key!r}")
            if key in taken:
                raise HTTPException(400, f"Clave repetida: {key!r}")
        elif not key:
            key = slug_key(name, taken)
        taken.add(key)
        prop: dict[str, Any] = {"key": key, "name": name, "type": item.get("type")}
        if item.get("options"):
            prop["options"] = list(item["options"])
        finales.append(prop)

    claves_finales = {p["key"] for p in finales}
    # Una clave renombrada ya no está entre las finales, pero no es una
    # propiedad borrada: sus vistas se traducen, no se vacían.
    borradas = [k for k in actuales if k not in claves_finales and k not in renombres]
    return finales, renombres, borradas


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
