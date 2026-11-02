"""Sugerencias de agentes (P10a): un parche en un sidecar, nunca dentro del `.md`.

Una sugerencia es lo que un agente propone sobre una nota: **el texto exacto
que se sustituye** (`old_text`) y **el que entra** (`new_text`), más el motivo
(`rationale`) y el hilo de respuestas del dueño (`replies`). No es una nota
nueva escrita por el agente ni un comentario incrustado: es un parche anclado a
un trozo del cuerpo que el dueño acepta o rechaza.

El almacenamiento es una carpeta hermana del fichero, un JSON por sugerencia:

    Notas/Proyectos.md
    Notas/Proyectos.md.sugerencias/<sugerencia-id>.json

Sidecar y no dentro del `.md` porque el fichero tiene que seguir siendo
Markdown limpio para cualquier otra herramienta (ADR-0003): ni marcas, ni
bloques de comentario, ni un frontmatter con un hilo de mensajes. Y un JSON por
sugerencia —no un único fichero con un array— porque aceptar o rechazar es
reescribir un objeto, no un documento compartido que dos agentes se pisan.

La carpeta termina en `.md.sugerencias` y contiene solo `.json`, así que el
recorrido del vault no la mira: ni el índice ni el vigilante la pueden
confundir con una nota. Aun así se excluye por nombre en `walk_markdown`, que
es la única puerta del recorrido.

Este módulo no sabe de HTTP ni de SQL: rutas, lectura/escritura atómica del
JSON y la aritmética del ancla. El repositorio decide estados y escribe la nota
por la ruta normal del vault.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from app.vault import vaultfs

# La carpeta de sugerencias se nombra pegando el sufijo al fichero de la nota:
# `Notas/X.md` -> `Notas/X.md.sugerencias/`. `SIDECAR_MARK` es como termina
# cualquier carpeta de sidecar, que es lo que hay que reconocer al recorrer el
# vault (un `.md` con ese sufijo no es una nota).
SIDECAR_SUFFIX = ".sugerencias"
SIDECAR_MARK = f".md{SIDECAR_SUFFIX}"

STATUSES = frozenset({"pending", "accepted", "rejected", "orphaned"})
KINDS = frozenset({"edit", "new_page"})

# Topes de tamaño, en bytes UTF-8 (los aplica la API con 413). Un parche es
# pequeño a propósito: si un agente quiere proponer una nota entera, eso es
# `new_page`, con su tope más generoso.
MAX_OLD_TEXT = 8 * 1024
MAX_NEW_TEXT = 8 * 1024
MAX_NEW_PAGE_BODY = 32 * 1024
MAX_RATIONALE = 4 * 1024
MAX_REPLY_TEXT = 4 * 1024
MAX_REASON = 4 * 1024
MAX_TITLE = 200
# `agent` es una etiqueta de autoria (y en `reply`, el nombre de quien responde):
# se escribe en el `.json` del vault, asi que tiene tope como el resto.
MAX_AGENT = 200

# Carpetas del vault que no son notas: la papelera (una sugerencia borrada con
# su nota no se busca), los adjuntos y las carpetas de otras herramientas.
_SKIP_DIRS = {".trash", "_attachments", ".git", ".obsidian", ".stversions"}


class SuggestionError(ValueError):
    """Una operación con sugerencias que no se puede hacer."""


class SuggestionNotFound(SuggestionError):
    """No hay ninguna sugerencia con ese id en el vault."""


class SuggestionUnanchored(SuggestionError):
    """El `old_text` ya no está en el cuerpo de la nota."""


class SuggestionResolved(SuggestionError):
    """La sugerencia ya se aceptó o se rechazó; no se vuelve a resolver."""

    def __init__(self, status: str) -> None:
        super().__init__(f"la sugerencia ya está {status}")
        self.status = status


@dataclass
class Suggestion:
    """Una sugerencia tal y como vive en su `.json` (una por fichero).

    `base_revision` es el `updated` de la nota cuando se creó: sirve para decir
    si el parche se propuso sobre la versión que el dueño tiene delante. No es
    la identidad del ancla —eso es `old_text`—, solo contexto.
    """

    id: str
    page_id: str
    agent: str
    created_at: str
    status: str
    kind: str
    old_text: str = ""
    new_text: str = ""
    rationale: str = ""
    title: Optional[str] = None
    base_revision: Optional[str] = None
    offset: int = 0
    replies: list[dict[str, Any]] = field(default_factory=list)
    decided_at: Optional[str] = None
    reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page_id": self.page_id,
            "agent": self.agent,
            "created_at": self.created_at,
            "status": self.status,
            "kind": self.kind,
            "old_text": self.old_text,
            "new_text": self.new_text,
            "rationale": self.rationale,
            "title": self.title,
            "base_revision": self.base_revision,
            "offset": self.offset,
            "replies": list(self.replies),
            "decided_at": self.decided_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Suggestion":
        return cls(
            id=str(data["id"]),
            page_id=str(data.get("page_id") or ""),
            agent=str(data.get("agent") or ""),
            created_at=str(data.get("created_at") or ""),
            status=str(data.get("status") or "pending"),
            kind=str(data.get("kind") or "edit"),
            old_text=str(data.get("old_text") or ""),
            new_text=str(data.get("new_text") or ""),
            rationale=str(data.get("rationale") or ""),
            title=data.get("title") or None,
            base_revision=data.get("base_revision") or None,
            offset=int(data.get("offset") or 0),
            replies=list(data.get("replies") or []),
            decided_at=data.get("decided_at") or None,
            reason=data.get("reason") or None,
        )


# --------------------------------------------------------------------------
# rutas
# --------------------------------------------------------------------------


def sidecar_rel(note_rel: str) -> str:
    """`Notas/X.md` -> `Notas/X.md.sugerencias` (relativo al vault)."""
    return f"{note_rel}{SIDECAR_SUFFIX}"


def sidecar_abs(root: str, note_rel: str) -> str:
    """Ruta absoluta de la carpeta de sugerencias de una nota.

    Pasa por `vaultfs.safe_rel`: `note_rel` sale del índice (o de un recorrido
    del disco), y ninguna ruta construida desde ahí puede salirse del vault.
    """
    return os.path.join(root, vaultfs.safe_rel(root, sidecar_rel(note_rel)))


def file_abs(root: str, note_rel: str, suggestion_id: str) -> str:
    """Ruta absoluta del `.json` de una sugerencia."""
    return os.path.join(sidecar_abs(root, note_rel), f"{suggestion_id}.json")


def in_sidecar(rel_path: str) -> bool:
    """¿La ruta cae dentro de una carpeta de sugerencias (`*.md.sugerencias/`)?

    Lo usan la papelera y el borrado: un `.json` del sidecar no es una nota
    suelta, ni al listar ni al restaurar. Se comprueba sobre cualquier
    componente porque la ruta puede venir relativa al vault o a `.trash/`.
    """
    parts = rel_path.replace(os.sep, "/").split("/")
    return any(part.endswith(SIDECAR_MARK) for part in parts)


def note_rel_of(sidecar_rel_path: str) -> Optional[str]:
    """Ruta de la nota a la que pertenece una carpeta de sidecar (o None)."""
    rel = sidecar_rel_path.replace(os.sep, "/")
    if not rel.endswith(SIDECAR_SUFFIX):
        return None
    return rel[: -len(SIDECAR_SUFFIX)]


# --------------------------------------------------------------------------
# lectura y escritura
# --------------------------------------------------------------------------


def write(root: str, suggestion: Suggestion, note_rel: str) -> str:
    """Escribe el JSON de la sugerencia (atómico, como el resto del vault)."""
    path = file_abs(root, note_rel, suggestion.id)
    text = json.dumps(
        suggestion.as_dict(), ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    vaultfs.atomic_write(path, text)
    return path


def read(path: str) -> Optional[Suggestion]:
    """Lee un `.json` de sugerencia. Un fichero ilegible no tumba un listado."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("id"):
        return None
    return Suggestion.from_dict(data)


def list_for(
    root: str, note_rel: str, status: Optional[str] = None
) -> list[Suggestion]:
    """Sugerencias de una nota, las más recientes primero. `status` filtra."""
    folder = sidecar_abs(root, note_rel)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []          # no hay carpeta: la nota no tiene sugerencias
    found: list[Suggestion] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        suggestion = read(os.path.join(folder, name))
        if suggestion is None:
            continue
        if status is not None and suggestion.status != status:
            continue
        found.append(suggestion)
    found.sort(key=lambda s: (s.created_at, s.id), reverse=True)
    return found


def find(root: str, suggestion_id: str) -> Optional[tuple[Suggestion, str]]:
    """Busca una sugerencia por id en todo el vault.

    Las rutas de la API (`/suggestions/{sid}/accept`) no llevan la nota, así
    que hay que recorrer las carpetas de sidecar: son pocas y con un nivel de
    ficheros, y el recorrido no entra en ellas. La papelera se salta: una
    sugerencia que se fue con su nota está borrada.
    """
    root_abs = os.path.abspath(root)
    for dirpath, dirnames, _files in os.walk(root_abs):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        sidecars = [name for name in dirnames if name.endswith(SIDECAR_MARK)]
        # Dentro de una carpeta de sidecar solo hay `.json`: no se desciende.
        for name in sidecars:
            dirnames.remove(name)
        for name in sidecars:
            path = os.path.join(dirpath, name, f"{suggestion_id}.json")
            if not os.path.isfile(path):
                continue
            suggestion = read(path)
            if suggestion is None:
                continue
            rel_dir = os.path.relpath(os.path.join(dirpath, name), root_abs)
            note_rel = note_rel_of(rel_dir)
            if note_rel is None:                     # pragma: no cover
                continue
            return suggestion, note_rel
    return None


# --------------------------------------------------------------------------
# el ancla
# --------------------------------------------------------------------------


def occurrences(body: str, old_text: str) -> list[int]:
    """Todas las posiciones donde aparece `old_text` en `body`."""
    if not old_text:
        return []
    found: list[int] = []
    start = body.find(old_text)
    while start != -1:
        found.append(start)
        start = body.find(old_text, start + 1)
    return found


def anchored_at(body: str, old_text: str, offset: int = 0) -> Optional[int]:
    """Dónde aplicar el parche, o `None` si el ancla ya no está.

    Una aparición: ahí. Varias: la más cercana a `offset` (el punto del cuerpo
    sobre el que el agente propuso el cambio); a igual distancia, la primera,
    para que el resultado no dependa del orden del recorrido. Es la regla del
    ADR-0003: el ancla es el texto, y `offset` solo desempata.
    """
    positions = occurrences(body, old_text)
    if not positions:
        return None
    reference = max(offset or 0, 0)
    return min(positions, key=lambda pos: (abs(pos - reference), pos))


def apply(
    body: str, old_text: str, new_text: str, offset: int = 0
) -> Optional[tuple[str, int]]:
    """Sustituye `old_text` por `new_text` en la aparición anclada.

    Devuelve `(cuerpo nuevo, posición aplicada)` o `None` si el ancla
    desapareció. Una sola sustitución: aceptar una sugerencia no cambia las
    demás apariciones del mismo texto (el agente propuso un sitio concreto).
    """
    position = anchored_at(body, old_text, offset)
    if position is None:
        return None
    return (
        f"{body[:position]}{new_text}{body[position + len(old_text):]}",
        position,
    )
