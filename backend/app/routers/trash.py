"""Papelera: listar lo borrado y devolverlo a su sitio.

Borrar ya mueve a `.trash/<fecha UTC>/<ruta original>` (ADR-0001), pero hasta
P1.2b no había forma de recuperarlo desde la app: solo con `mv` a mano y
sabiendo dónde mirar. Sin UI a propósito (eso es P4).
"""

from fastapi import APIRouter, Depends, HTTPException

from app import schemas
from app.dependencies import get_repo
from app.repository import Repository
from app.vault.store import VaultError
from app.vault.vaultfs import PathEscape, TrashConflict

router = APIRouter(prefix="/trash", tags=["trash"])


@router.get("", response_model=list[schemas.TrashEntry])
def list_trash(repo: Repository = Depends(get_repo)):
    """Lo que hay en la papelera, lo más reciente primero."""
    return [entry.as_dict() for entry in repo.vault.list_trash()]


@router.post("/restore", response_model=schemas.TrashRestoreResult)
def restore_from_trash(
    payload: schemas.TrashRestoreRequest, repo: Repository = Depends(get_repo)
):
    """Devuelve un fichero de la papelera a su ruta y reindexa.

    El reindexado va en la misma petición porque restaurar es recuperar una
    nota: dejarla en disco y no en el índice sería devolver el fichero y seguir
    sin verlo en la app. `force` no: restaurar siempre deja al menos un `.md`
    en el vault.
    """
    try:
        rel = repo.vault.restore(payload.path)
    except TrashConflict as exc:
        # 409: la ruta original está ocupada. No se renombra a `x (2).md` en
        # silencio; quien restaura decide qué hacer con lo que hay ahí.
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            404, f"No hay nada en la papelera en {payload.path!r}"
        ) from exc
    except (PathEscape, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        stats = repo.reindex()
    except VaultError as exc:
        raise HTTPException(
            409, f"restaurado {rel}, pero el índice no se pudo reconstruir: {exc}"
        ) from exc
    return schemas.TrashRestoreResult(
        restored_path=rel, reindexed=True, notes=stats.notes, links=stats.links
    )
