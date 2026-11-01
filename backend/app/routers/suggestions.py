"""Sugerencias de agentes (P10a): crear, listar, aceptar, rechazar y responder.

Solo backend. El modelo vive en `app/vault/suggestions.py` (un `.json` por
sugerencia en la carpeta hermana de la nota) y aceptar escribe la nota por la
ruta de un guardado normal (`Repository.update_page` / `create_page`): `.md`
atómico, índice al día y aviso al navegador por `/events`. La interfaz y el
token de agente son tarjetas aparte.

Las rutas de colección cuelgan de la nota (`/pages/{id}/suggestions`) porque
una sugerencia siempre se propone sobre una nota; las de decisión son planas
(`/suggestions/{sid}/...`) porque el id ya es único en el vault y quien acepta
no tiene por qué saber de qué nota salió.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app import schemas
from app.dependencies import get_repo
from app.events import bus
from app.repository import Repository
from app.vault import suggestions
from app.vault.store import VaultError
from app.vault.suggestions import (
    SuggestionError,
    SuggestionNotFound,
    SuggestionResolved,
    SuggestionUnanchored,
)

router = APIRouter(tags=["suggestions"])


def _check_size(value: str, limit: int, what: str) -> None:
    """413 si `value` pasa del tope, contando bytes UTF-8 (no caracteres)."""
    if len((value or "").encode("utf-8")) > limit:
        raise HTTPException(413, f"{what} pasa del tope de {limit // 1024} KB")


def _get_or_404(repo: Repository, page_id: uuid.UUID):
    page = repo.get_page(page_id)
    if page is None:
        raise HTTPException(404, "Página no encontrada")
    return page


def _publish(repo: Repository, page_id: uuid.UUID, action: str) -> None:
    """Avisa al navegador del cambio, igual que el vigilante con un `.md` tocado.

    Sin esto, aceptar una sugerencia cambiaría el fichero y el índice sin que
    una pestaña abierta se enterase: es el mismo evento que espera el panel
    para recargar la nota.
    """
    bus.publish(
        {
            "type": "page",
            "action": action,
            "page_id": str(page_id),
            "path": repo.page_path(page_id),
        }
    )


@router.post(
    "/pages/{page_id}/suggestions", response_model=schemas.SuggestionRead
)
def create_suggestion(
    page_id: uuid.UUID,
    payload: schemas.SuggestionCreate,
    repo: Repository = Depends(get_repo),
):
    """Crea una sugerencia sobre una nota. Nace `pending` o `orphaned`.

    Que el `old_text` no esté en el cuerpo **no** es un error: el agente pudo
    basarse en una versión anterior y la sugerencia se guarda igual, marcada
    `orphaned`, para que se pueda leer el motivo y responderle.
    """
    _get_or_404(repo, page_id)

    kind = str(getattr(payload.kind, "value", payload.kind))
    _check_size(payload.rationale, suggestions.MAX_RATIONALE, "rationale")
    if kind == "new_page":
        _check_size(payload.new_text, suggestions.MAX_NEW_PAGE_BODY, "new_text")
        _check_size(payload.title or "", suggestions.MAX_TITLE, "title")
    else:
        _check_size(payload.old_text, suggestions.MAX_OLD_TEXT, "old_text")
        _check_size(payload.new_text, suggestions.MAX_NEW_TEXT, "new_text")

    try:
        suggestion = repo.create_suggestion(
            page_id,
            agent=payload.agent,
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
    return suggestion.as_dict()


@router.get(
    "/pages/{page_id}/suggestions", response_model=list[schemas.SuggestionRead]
)
def list_suggestions(
    page_id: uuid.UUID,
    status: schemas.SuggestionStatus | None = None,
    repo: Repository = Depends(get_repo),
):
    """Sugerencias de la nota, las más recientes primero; `?status=` filtra."""
    _get_or_404(repo, page_id)
    found = repo.list_suggestions(
        page_id, str(status.value) if status is not None else None
    )
    return [suggestion.as_dict() for suggestion in found or []]


@router.get("/suggestions/{suggestion_id}", response_model=schemas.SuggestionRead)
def get_suggestion(suggestion_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    """Una sugerencia suelta, con su hilo. 404 si ya no está en el vault."""
    suggestion = repo.get_suggestion(str(suggestion_id))
    if suggestion is None:
        raise HTTPException(404, "Sugerencia no encontrada")
    return suggestion.as_dict()


@router.post(
    "/suggestions/{suggestion_id}/accept", response_model=schemas.SuggestionAcceptRead
)
def accept_suggestion(suggestion_id: uuid.UUID, repo: Repository = Depends(get_repo)):
    """Aplica el parche si el ancla sigue estando.

    409 `{"error": "desanclada"}` cuando el `old_text` ya no aparece: la
    sugerencia se queda `orphaned` y el `.md` no se toca.
    """
    try:
        result = repo.accept_suggestion(str(suggestion_id))
    except SuggestionNotFound as exc:
        raise HTTPException(404, "Sugerencia no encontrada") from exc
    except SuggestionResolved as exc:
        return JSONResponse(
            status_code=409,
            content={"error": "ya_resuelta", "status": exc.status},
        )
    except SuggestionUnanchored:
        return JSONResponse(status_code=409, content={"error": "desanclada"})
    except VaultError as exc:
        # El ancla estaba, pero el vault no pudo escribir (fichero borrado por
        # fuera, padre inexistente...). La sugerencia sigue como estaba: no se
        # ha aplicado nada y se puede reintentar.
        return JSONResponse(
            status_code=409, content={"error": "no_aplicada", "detail": str(exc)}
        )

    page = result["page"]
    _publish(repo, page.id, "added" if result["created"] else "modified")
    return {
        "suggestion": result["suggestion"].as_dict(),
        "page_id": page.id,
        "created": result["created"],
        "stale": result["stale"],
    }


@router.post(
    "/suggestions/{suggestion_id}/reject", response_model=schemas.SuggestionRead
)
def reject_suggestion(
    suggestion_id: uuid.UUID,
    payload: schemas.SuggestionRejectRequest | None = None,
    repo: Repository = Depends(get_repo),
):
    """Marca `rejected` (con motivo si se manda). El `.md` no se toca."""
    try:
        suggestion = repo.reject_suggestion(
            str(suggestion_id), reason=(payload.reason if payload else None)
        )
    except SuggestionNotFound as exc:
        raise HTTPException(404, "Sugerencia no encontrada") from exc
    except SuggestionResolved as exc:
        return JSONResponse(
            status_code=409,
            content={"error": "ya_resuelta", "status": exc.status},
        )
    return suggestion.as_dict()


@router.post(
    "/suggestions/{suggestion_id}/reply", response_model=schemas.SuggestionRead
)
def reply_suggestion(
    suggestion_id: uuid.UUID,
    payload: schemas.SuggestionReplyRequest,
    repo: Repository = Depends(get_repo),
):
    """Añade una entrada al hilo del dueño (el canal de vuelta al agente)."""
    _check_size(payload.text, suggestions.MAX_REPLY_TEXT, "text")
    try:
        suggestion = repo.reply_to_suggestion(
            str(suggestion_id), text=payload.text, author=payload.author
        )
    except SuggestionNotFound as exc:
        raise HTTPException(404, "Sugerencia no encontrada") from exc
    except SuggestionError as exc:
        raise HTTPException(400, str(exc)) from exc
    return suggestion.as_dict()
