"""Captura rápida (P13): `POST /capture` y la bandeja de entrada (`GET /inbox`).

Apuntar algo tiene que costar dos segundos desde cualquier sitio. Este router
es la mitad del servidor de eso: un `POST` con el texto crea una nota en la
bandeja (`CAPTURE_DIR`, `Bandeja/` por defecto) y `GET /inbox` devuelve lo que
hay sin clasificar, que es lo que pinta la sección «Bandeja (N)» del panel.

La otra mitad son los scripts de `tools/capture/` (macOS, Windows, Linux): el
atajo global lo pone el sistema operativo y nosotros solo damos el endpoint.

Lo que fija este módulo:

- **El texto es obligatorio** y tiene tope (`CAPTURE_MAX_BYTES`, 64 KB por
  defecto): una captura es un apunte, no un documento, y un atajo que meta un
  fichero entero por accidente no puede acabar en el vault. Vacío o solo
  espacios es 400, no una nota en blanco.
- **Título**: el que llegue o `Captura 2026-11-02 14-33`. El `:` de la hora se
  escribe `-` porque **el título ES el nombre del fichero** (ADR-0001) y `:` no
  es un carácter válido en un nombre de fichero en Windows: escribirlo tal cual
  lo convertiría en un espacio y el título saldría «Captura 2026-11-02 14 33».
  Es la misma convención que las carpetas de la papelera (`vaultfs.py`).
  Si el título ya existe en la bandeja no se falla: el vault resuelve la
  colisión con ` (2)`, ` (3)`… igual que en cualquier otra nota.
- **Autoría**: `captured_by` es el `display_name` del dueño de la sesión o del
  token, y `capture_source` el origen (`shortcut`, `raycast`, `cli`, `api`).
  Van al frontmatter junto a `captured_at`, que es el instante UTC de la
  captura (no la fecha del fichero: un `rsync` la cambia).
- **La escritura pasa por el vault** (`Repository.capture_note` →
  `VaultStore.create`): `.md` atómico, índice actualizado en la misma
  transacción y aviso por `/events`, como cualquier otra nota. Aquí no se
  escribe ni un fichero a mano.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import auth, schemas
from app.config import settings
from app.dependencies import get_current_user, get_repo
from app.events import bus
from app.repository import Repository
from app.serializers import serialize_page
from app.vault.store import VaultError

router = APIRouter(tags=["capture"])

# De dónde viene la captura. `api` es el cajón de sastre para una llamada
# manual; los otros tres los mandan los scripts de `tools/capture/`.
CaptureSource = Literal["shortcut", "raycast", "cli", "api"]

# El título por defecto, con `-` en la hora: ver el docstring del módulo.
DEFAULT_TITLE_FORMAT = "%Y-%m-%d %H-%M"


class CaptureRequest(BaseModel):
    text: str
    title: Optional[str] = None
    source: CaptureSource = "api"


class CaptureResult(BaseModel):
    id: uuid.UUID
    title: str
    path: str


class InboxRead(BaseModel):
    """La bandeja: su carpeta, cuántas notas tiene y las notas."""

    dir: str
    count: int
    pages: list[schemas.PageRead]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _publish(repo: Repository, page_id: uuid.UUID) -> None:
    """Avisa al panel del alta, igual que el vigilante con un `.md` de fuera.

    La escritura es de la propia app, así que el índice ya está al día y el
    vigilante no ve deriva: sin este aviso, una pestaña abierta no se enteraría
    de la nota capturada desde el atajo hasta refrescar a mano.
    """
    bus.publish(
        {
            "type": "page",
            "action": "added",
            "page_id": str(page_id),
            "path": repo.page_path(page_id),
        }
    )


@router.post("/capture", response_model=CaptureResult, status_code=201)
def capture(
    payload: CaptureRequest,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    """Crea una nota en la bandeja con el texto capturado.

    Entra con sesión de navegador o con un token personal de captura
    (`Authorization: Bearer <token>` o `X-User-Token`). `get_current_user` lee
    `request.state.user`, que en los dos casos lo ha dejado el middleware de
    `app.main`: para esta ruta, un token vale como identidad, pero solo para
    esta ruta (cualquier otra responde 403 con un token).
    """
    text = payload.text or ""
    if not text.strip():
        raise HTTPException(400, "el texto de la captura no puede estar vacío")
    size = len(text.encode("utf-8"))
    if size > settings.capture_max_bytes:
        raise HTTPException(
            413,
            f"la captura ocupa {size} bytes y el tope son "
            f"{settings.capture_max_bytes}: es un apunte, no un documento",
        )

    title = (payload.title or "").strip() or (
        "Captura " + _now_utc().strftime(DEFAULT_TITLE_FORMAT)
    )
    try:
        page = repo.capture_note(
            text=text,
            title=title,
            source=payload.source,
            captured_by=user.display_name,
            folder=settings.capture_dir,
        )
    except VaultError as exc:
        # Un título que no se puede escribir en el vault (ruta imposible, etc.).
        raise HTTPException(400, str(exc)) from exc

    _publish(repo, page.id)
    return CaptureResult(
        id=page.id, title=page.title, path=repo.page_path(page.id) or ""
    )


@router.get("/inbox", response_model=InboxRead)
def inbox(repo: Repository = Depends(get_repo)):
    """Lo que hay sin clasificar en la bandeja, lo más reciente primero.

    Solo la sesión del dueño: un token de captura escribe, no lee (el
    middleware responde 403 a un token en esta ruta). El panel no tiene la ruta
    de cada nota, así que esta es la lista que le dice qué está en la bandeja.
    """
    pages = repo.inbox_pages(settings.capture_dir)
    colors = repo.author_colors()
    return InboxRead(
        dir=settings.capture_dir,
        count=len(pages),
        pages=[serialize_page(page, colors) for page in pages],
    )
