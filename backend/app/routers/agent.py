"""Puerta para agentes (P10b): leer contexto, proponer y ver lo decidido.

Tres rutas, todas bajo `/agent` y todas con token (`X-Agent-Token`, ver
`app/agent_auth.py`):

```
GET  /agent/context?page_id=<uuid>|path=Notas/Algo.md
POST /agent/suggestions
GET  /agent/suggestions?status=...&since=<iso>
```

Lo que **no** hay aquí es tan importante como lo que hay: un agente no puede
editar (`PATCH /pages/{id}`), ni borrar, ni aceptar ni rechazar sus propias
sugerencias. Esas siguen siendo rutas del dueño, sin token. La puerta del
agente es de solo lectura sobre el vault más un canal de propuesta; si el
token se filtra, lo peor que puede pasar es que alguien llene de sugerencias
pendientes una nota, y para eso está el tope (`429`).

`GET /agent/context` devuelve todo lo que un LLM necesita para proponer algo
sensato en **una** respuesta: la nota, lo que la enlaza, lo que enlaza ella, el
hilo de sugerencias (aceptadas y rechazadas incluidas: ahí está la respuesta
del dueño) y su `brief`, la nota de instrucciones `Agentes/<nombre>.md`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app import schemas
from app.agent_auth import Agent, require_agent
from app.config import settings
from app.dependencies import get_repo
from app.limits import check_size
from app.links import fold_title
from app.repository import Repository
from app.vault import suggestions as vault_suggestions
from app.vault import vaultfs
from app.vault.store import VaultError
from app.vault.suggestions import Suggestion, SuggestionError
from app.wikilinks import extract_titles

log = logging.getLogger("mdm.agents")

router = APIRouter(prefix="/agent", tags=["agent"])

# Dónde se busca el `brief` del agente, en orden. `Agentes/<nombre>.md` es la
# ruta que pide la tarjeta y la que aparece si alguien crea la nota a mano en
# la raíz del vault; `Notas/Agentes/<nombre>.md` es la misma nota creada desde
# la app (las notas nuevas nacen dentro de `Notas/`), así que sin esto el
# `brief` no existiría en el caso normal. Se prueba en ese orden y gana la
# primera que exista; documentado en el README y en el ADR-0004.
BRIEF_DIRS = ("Agentes", "Notas/Agentes")


# --------------------------------------------------------------------------
# ayudas
# --------------------------------------------------------------------------


def _truncate(body: str, limit: int) -> tuple[str, bool]:
    """Corta el cuerpo a `limit` bytes UTF-8 sin partir un carácter."""
    data = body.encode("utf-8")
    if len(data) <= limit:
        return body, False
    return data[:limit].decode("utf-8", "ignore"), True


def _page_for(repo: Repository, page_id: Optional[uuid.UUID], path: Optional[str]):
    """La página que pide el agente, por id o por ruta. Exactamente una."""
    if (page_id is None) == (path is None):
        raise HTTPException(
            400, "manda page_id o path, exactamente uno de los dos"
        )
    if page_id is not None:
        page = repo.get_page(page_id)
        if page is None:
            raise HTTPException(404, "Página no encontrada")
        return page

    raw = (path or "").strip().replace("\\", "/").lstrip("/")
    try:
        rel = vaultfs.safe_rel(repo.vault.root, raw)
    except vaultfs.PathEscape as exc:
        raise HTTPException(400, f"path fuera del vault: {exc}") from exc
    page = repo.page_by_path(rel)
    if page is None:
        raise HTTPException(404, f"no hay ninguna página en {rel!r}")
    return page


def _brief(repo: Repository, agent_name: str) -> Optional[str]:
    """Las instrucciones que el dueño le escribió a este agente, o None.

    El nombre sale del token y `agent_auth` solo acepta nombres de fichero
    portables (sin barras ni `..`), así que aquí no hay ruta que se pueda
    escapar del vault.
    """
    for folder in BRIEF_DIRS:
        page = repo.page_by_path(f"{folder}/{agent_name}.md")
        if page is not None:
            return page.body_markdown or ""
    return None


def _as_utc(text: Optional[str]) -> Optional[datetime]:
    """ISO 8601 -> datetime en UTC. `Z` vale. None si no se puede leer."""
    if not text:
        return None
    raw = text.strip()
    if raw[-1:] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _since(raw: Optional[str]) -> Optional[datetime]:
    """El `?since=` de la petición. 400 si viene y no es una fecha."""
    if raw is None:
        return None
    value = _as_utc(raw)
    if value is None:
        raise HTTPException(
            400, "since tiene que ser una fecha ISO 8601 (p. ej. 2026-11-02T10:00:00Z)"
        )
    return value


def _stamp(text: Optional[str]) -> Optional[str]:
    """Una marca ISO del `.json` en UTC, o None si no se puede leer.

    Un sidecar tocado a mano con `created_at: ""` no debe tumbar el contexto de
    su nota (el recorrido del vault ya ignora un `.json` que no es una
    sugerencia): se devuelve `null` y el resto del listado sale.
    """
    moment = _as_utc(text)
    return moment.isoformat().replace("+00:00", "Z") if moment is not None else None


def _uuid_or_none(text) -> Optional[uuid.UUID]:
    """El id de página de un `.json`, o None si lo que hay no es un UUID."""
    try:
        return uuid.UUID(str(text))
    except (AttributeError, TypeError, ValueError):
        return None


def _suggestion_out(suggestion: Suggestion, note_rel: Optional[str]) -> dict:
    """La sugerencia en la forma compacta que lee un agente.

    Incluye `reason` y `replies` porque son el canal de vuelta: un agente que
    vuelve a mirar tiene que ver que el dueño rechazó aquello **y por qué**.
    """
    return {
        "id": suggestion.id,
        "page_id": _uuid_or_none(suggestion.page_id),
        "path": note_rel,
        "status": suggestion.status,
        "kind": suggestion.kind,
        "agent": suggestion.agent,
        "title": suggestion.title,
        "created_at": _stamp(suggestion.created_at),
        "decided_at": _stamp(suggestion.decided_at),
        "old_text": suggestion.old_text,
        "new_text": suggestion.new_text,
        "rationale": suggestion.rationale,
        "reason": suggestion.reason,
        "replies": list(suggestion.replies),
    }


# --------------------------------------------------------------------------
# 1. leer contexto
# --------------------------------------------------------------------------


@router.get("/context", response_model=schemas.AgentContextRead)
def agent_context(
    page_id: Optional[uuid.UUID] = Query(default=None),
    path: Optional[str] = Query(default=None),
    repo: Repository = Depends(get_repo),
    agent: Agent = Depends(require_agent),
):
    """Todo lo que hace falta para proponer sobre una nota, en una respuesta.

    - `body` recortado a `AGENT_CONTEXT_MAX_BODY` (20 KB por defecto), con
      `body_truncated: true` si se cortó: un agente que no ve el final de la
      nota no debe creer que la ha visto entera.
    - `links` son los wikilinks del cuerpo con si resuelven o no (mismo
      criterio que el guardado: `fold_title`), para que el agente sepa qué
      puede enlazar sin inventarse títulos.
    - `suggestions` son **todas** las de la nota, resueltas incluidas: el hilo
      con el dueño es el canal de vuelta, no un tablón de pendientes.
    """
    page = _page_for(repo, page_id, path)
    note_rel = repo.page_path(page.id)
    body = page.body_markdown or ""
    body_out, truncated = _truncate(body, max(settings.agent_context_max_body, 0))

    titles = repo.folded_titles(page.workspace_id)
    links = [
        {"title": title, "exists": fold_title(title) in titles}
        for title in extract_titles(body)
    ]

    return {
        "page": {
            "id": page.id,
            "title": page.title,
            "path": note_rel,
            "type": page.type,
            "updated_at": page.updated_at,
        },
        "body": body_out,
        "body_truncated": truncated,
        "backlinks": [
            {"id": backlink.id, "title": backlink.title}
            for backlink in repo.backlinks(page.id)
        ],
        "links": links,
        "suggestions": [
            _suggestion_out(suggestion, note_rel)
            for suggestion in (repo.list_suggestions(page.id) or [])
        ],
        "brief": _brief(repo, agent.name),
    }


# --------------------------------------------------------------------------
# 2. proponer
# --------------------------------------------------------------------------


@router.post("/suggestions", response_model=schemas.SuggestionRead)
def create_agent_suggestion(
    payload: schemas.AgentSuggestionCreate,
    repo: Repository = Depends(get_repo),
    agent: Agent = Depends(require_agent),
):
    """Propone un parche. El `agent` de la sugerencia **es el del token**.

    Si el cuerpo manda `agent`, se ignora (el modelo no lo declara): un agente
    no puede firmar como otro. Los topes de tamaño son los mismos que por la
    puerta del dueño (`413`), y encima están los de pendientes (`429`).
    """
    page = repo.get_page(payload.page_id)
    if page is None:
        raise HTTPException(404, "Página no encontrada")

    kind = str(getattr(payload.kind, "value", payload.kind))
    check_size(agent.name, vault_suggestions.MAX_AGENT, "agent")
    check_size(payload.rationale, vault_suggestions.MAX_RATIONALE, "rationale")
    check_size(payload.title or "", vault_suggestions.MAX_TITLE, "title")
    if kind == "new_page":
        check_size(payload.new_text, vault_suggestions.MAX_NEW_PAGE_BODY, "new_text")
    else:
        check_size(payload.old_text, vault_suggestions.MAX_OLD_TEXT, "old_text")
        check_size(payload.new_text, vault_suggestions.MAX_NEW_TEXT, "new_text")

    on_note, by_agent = repo.pending_counts(payload.page_id, agent.name)
    if on_note >= settings.agent_max_pending_note:
        raise HTTPException(
            429,
            f"la nota ya tiene {on_note} sugerencias pendientes "
            f"(tope {settings.agent_max_pending_note}): el dueño tiene que "
            "decidir las que hay antes de que lleguen más",
        )
    if by_agent >= settings.agent_max_pending_agent:
        raise HTTPException(
            429,
            f"el agente {agent.name} tiene {by_agent} sugerencias pendientes en "
            f"el vault (tope {settings.agent_max_pending_agent}): espera a que "
            "el dueño decida antes de proponer más",
        )

    try:
        suggestion = repo.create_suggestion(
            payload.page_id,
            agent=agent.name,
            kind=kind,
            old_text=payload.old_text,
            new_text=payload.new_text,
            rationale=payload.rationale,
            title=payload.title,
            offset=payload.offset,
        )
    except SuggestionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except VaultError as exc:
        raise HTTPException(400, str(exc)) from exc
    if suggestion is None:      # pragma: no cover - ya comprobado arriba
        raise HTTPException(404, "Página no encontrada")

    # Una línea por sugerencia creada, sin el texto: el parche es contenido del
    # dueño y el log no es su sitio. Tampoco el token, que aquí ni se tiene.
    log.info(
        "sugerencia %s: agente=%s nota=%s kind=%s parche=%d B",
        suggestion.id,
        agent.name,
        repo.page_path(page.id),
        kind,
        len(payload.old_text.encode("utf-8")) + len(payload.new_text.encode("utf-8")),
    )
    return suggestion.as_dict()


# --------------------------------------------------------------------------
# 3. ver lo decidido
# --------------------------------------------------------------------------


@router.get("/suggestions", response_model=list[schemas.AgentSuggestionRead])
def list_agent_suggestions(
    status: Optional[schemas.SuggestionStatus] = Query(default=None),
    since: Optional[str] = Query(default=None),
    repo: Repository = Depends(get_repo),
    agent: Agent = Depends(require_agent),
):
    """Las sugerencias **de este agente**, con lo que el dueño decidió.

    `?since=` es la pregunta «¿qué ha pasado desde mi última visita?» y mira la
    última actividad —crear, decidir o la última respuesta del dueño—, no la
    fecha de creación: una sugerencia de anteayer con una respuesta de ayer es
    novedad de ayer. `?status=` filtra por estado.
    """
    since_dt = _since(since)
    found = repo.list_agent_suggestions(
        agent.name, str(status.value) if status is not None else None
    )
    out: list[dict] = []
    for note_rel, suggestion in found:
        if since_dt is not None:
            at = _as_utc(vault_suggestions.activity_at(suggestion))
            if at is None or at < since_dt:
                continue
        out.append(_suggestion_out(suggestion, note_rel))
    return out
