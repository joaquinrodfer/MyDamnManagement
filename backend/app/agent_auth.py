"""Identidad de los agentes (P10b): qué agente hay detrás de una petición.

Hasta P10a el campo `agent` de una sugerencia era una etiqueta que ponía quien
llamaba: cualquiera con acceso a la API podía firmar como otro. Esta tarjeta
cierra eso con un secreto compartido por agente, y solo por agente — el dueño
sigue entrando sin token (la app no tiene login todavía y esta tarjeta no lo
inventa).

El contrato es pequeño a propósito:

- `AGENT_TOKENS="token1:agente-hitos,token2:agente-revision"`. Pares
  `token:nombre` separados por comas. Sin la variable (o vacía) **no hay
  agentes**: los endpoints `/agent/...` responden **404**, no 403. Un 403 dice
  «existe y no puedes»; un 404 dice «aquí no hay nada», que es la verdad
  cuando el despliegue no ha configurado ninguno.
- La cabecera `X-Agent-Token` lleva el token. El **nombre sale del token**,
  nunca del cuerpo: un agente no puede proponer como otro mandando
  `{"agent": "otro"}`.
- Comparación en tiempo constante (`hmac.compare_digest`) contra **todos** los
  tokens configurados, sin cortocircuito: el tiempo de respuesta no dice si un
  token existía ni cuál de ellos coincidió.
- Un token ausente o inválido es **401** con el mismo mensaje en los dos casos,
  para no distinguir «no mandaste nada» de «mandaste uno que no existe».
- Los tokens **no se registran**: ni en un acierto, ni en un 401, ni en los
  avisos de configuración. Los avisos hablan del nombre del agente y de la
  posición de la entrada, que es lo que hace falta para arreglar la variable
  sin volcar el secreto en el log.
- Un token válido es un permiso **acotado**: `app.main` rechaza con **403**
  cualquier petición que lo traiga y no vaya a `/agent/...`. No es
  autenticación —la API del dueño sigue sin tenerla— pero hace que un token
  filtrado no sirva de llave para escribir, borrar ni aceptar sugerencias:
  solo propone, que es lo que el contrato promete.

`hmac.compare_digest` sobre `str` exige ASCII: un token con un acento lanzaría
`TypeError` (un 500 por una cabecera rara). Se comparan los bytes UTF-8, que
además es como viaja en la cabecera.
"""

from __future__ import annotations

import hmac
import logging
import re
from dataclasses import dataclass

from fastapi import Header, HTTPException

from app.config import settings

log = logging.getLogger("mdm.agents")

# El nombre de un agente es también el nombre de su nota de instrucciones
# (`Agentes/<nombre>.md`), así que se limita a lo que es un nombre de fichero
# portable: sin barras, sin `..`, sin espacios. Un nombre raro no se arregla en
# silencio (se descarta la entrada y se avisa): si el dueño escribe
# `Agentes/../x` en la variable, lo que no puede pasar es que el token exista.
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Un token es un secreto opaco: cualquier cosa sin espacios ni comas. El tope
# es el mismo que el de `agent` en una sugerencia (200 B), para que el nombre
# que acaba escrito en el `.json` quepa siempre.
MAX_TOKEN_BYTES = 200


@dataclass(frozen=True)
class Agent:
    """El agente autenticado en esta petición. Solo el nombre: el token no sale
    de `agent_auth` ni se pasa a nadie más."""

    name: str


def parse_tokens(raw: str) -> dict[str, str]:
    """`token:nombre,token2:nombre2` -> `{token: nombre}`.

    Las entradas rotas se descartan con un aviso que **no** incluye el token
    (se nombra su posición y, si la hay, la parte que debería ser un nombre).
    Es deliberado que una entrada mala no tumbe las demás: el dueño añade un
    agente nuevo y no se lleva por delante a los que ya funcionaban.
    """
    parsed: dict[str, str] = {}
    for position, entry in enumerate((raw or "").split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        token, sep, name = entry.partition(":")
        token, name = token.strip(), name.strip()
        if not sep or not token or not name:
            log.warning(
                "AGENT_TOKENS: entrada %d ignorada (formato esperado token:nombre)",
                position,
            )
            continue
        if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            log.warning(
                "AGENT_TOKENS: entrada %d ignorada (token de más de %d bytes)",
                position,
                MAX_TOKEN_BYTES,
            )
            continue
        if not AGENT_NAME_RE.match(name):
            log.warning(
                "AGENT_TOKENS: entrada %d ignorada (nombre de agente %r no válido: "
                "solo letras, dígitos, '.', '_' y '-')",
                position,
                name[:80],
            )
            continue
        if token in parsed:
            log.warning(
                "AGENT_TOKENS: entrada %d ignorada (el token ya estaba a nombre de %r)",
                position,
                parsed[token],
            )
            continue
        parsed[token] = name
    return parsed


def configured() -> dict[str, str]:
    """Los tokens de este proceso. Se lee en cada petición, no al importar."""
    return parse_tokens(settings.agent_tokens)


def resolve(token: str | None, tokens: dict[str, str] | None = None) -> str | None:
    """Nombre del agente dueño de `token`, o `None` si no hay o no existe.

    Sin excepciones a propósito: lo usa también el guardia de `app.main`, que
    decide si una petición firmada como agente puede pasar, y allí un token
    inválido no es un error sino «esto no es una petición de agente».
    """
    if not token:
        return None
    if tokens is None:
        tokens = configured()
    candidate = token.encode("utf-8")
    found: str | None = None
    # Se comparan TODOS los tokens, sin romper el bucle al acertar: el tiempo
    # de respuesta no debe decir si el token existía ni en qué posición estaba.
    for known, name in tokens.items():
        if hmac.compare_digest(candidate, known.encode("utf-8")):
            found = name
    return found


def require_agent(x_agent_token: str | None = Header(default=None)) -> Agent:
    """Dependencia de FastAPI: resuelve el agente del token, o corta con 401/404.

    `Header` convierte `x_agent_token` en la cabecera `X-Agent-Token` (los
    guiones y las mayúsculas no importan en HTTP). Se lee de la cabecera y no
    de la query a propósito: una URL acaba en el log de acceso y en el
    historial del cliente, una cabecera no.
    """
    tokens = configured()
    if not tokens:
        # Sin agentes configurados la ruta no existe. Ver el docstring del
        # módulo: 404 y no 403.
        raise HTTPException(404, "no hay agentes configurados")

    name = resolve(x_agent_token, tokens)
    if name is None:
        # Mismo texto que «no mandaste token»: no se filtra si el token existía.
        raise HTTPException(401, "token de agente ausente o no válido")
    return Agent(name=name)
