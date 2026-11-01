"""Capa de repositorio: todo el acceso al índice pasa por aquí.

Los routers ya no ven SQL ni sesiones: piden y escriben a través de `Repository`,
y desde P1.2a guardar una nota escribe además su fichero `.md` (`VaultStore`,
`app/vault/store.py`) y deja el índice diciendo lo mismo que el fichero. Desde
P2 lo mismo vale para las bases de datos y sus filas. El objetivo de P1.1 era
exactamente este: cambiar la fuente de la verdad sin tocar las rutas.

Se usa `sqlite3` crudo (decisión y argumentos en
`docs/ADR-0002-indice-sqlite.md`). Los registros que devuelve (`Page`,
`Database`, `View`) son `dataclass` inmutables con los mismos nombres de campo
que usaba el modelo de SQLAlchemy, para que los serializadores y los routers no
tengan que traducir nada.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from app import index as index_mod
from app.config import settings
from app.enums import LinkKind, PageType, ViewType
from app.links import fold_title, sync_wikilinks  # noqa: F401 - API histórica del módulo
from app.vault import suggestions
from app.vault.store import EMPTY_VAULT_HINT_HTTP, UNSET, VaultError, VaultStore
from app.vault.suggestions import (
    Suggestion,
    SuggestionError,
    SuggestionNotFound,
    SuggestionResolved,
    SuggestionUnanchored,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _uuid(value: Optional[str]) -> Optional[uuid.UUID]:
    return uuid.UUID(value) if value else None


def _load(value: Optional[str], default: Any) -> Any:
    return json.loads(value) if value else default


@dataclass(frozen=True)
class Page:
    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    type: PageType
    created_at: datetime
    updated_at: datetime
    parent_id: Optional[uuid.UUID] = None
    database_id: Optional[uuid.UUID] = None  # solo filas (type=database_row)
    icon: Optional[str] = None
    description: Optional[str] = None
    created_by: Optional[str] = None
    header_image_path: Optional[str] = None
    properties: dict[str, Any] = field(default_factory=dict)
    body_markdown: str = ""


@dataclass(frozen=True)
class Database:
    id: uuid.UUID
    page_id: uuid.UUID
    title: str
    icon: Optional[str]
    schema_def: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class View:
    id: uuid.UUID
    database_id: uuid.UUID
    name: str
    type: ViewType
    config: dict[str, Any]


PAGE_FIELDS = (
    "id, workspace_id, parent_id, type, title, icon, description, created_by,"
    " header_image_path, body, created_at, updated_at"
)
PAGE_FIELDS_P = ", ".join("p." + name.strip() for name in PAGE_FIELDS.split(","))

# Página con su fila de `rows` si la hay (filas de database). Un LEFT JOIN
# porque `pages` no guarda `database_id` ni `properties`: eso vive en `rows`.
SELECT_PAGE = (
    f"SELECT {PAGE_FIELDS_P}, r.database_id AS database_id,"
    " r.properties_json AS properties_json"
    " FROM pages p LEFT JOIN rows r ON r.id = p.id"
)


def _page(row: sqlite3.Row) -> Page:
    keys = row.keys()
    return Page(
        id=uuid.UUID(row["id"]),
        workspace_id=uuid.UUID(row["workspace_id"]),
        title=row["title"],
        type=PageType(row["type"]),
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
        parent_id=_uuid(row["parent_id"]),
        database_id=_uuid(row["database_id"]) if "database_id" in keys else None,
        icon=row["icon"],
        description=row["description"],
        created_by=row["created_by"],
        header_image_path=row["header_image_path"],
        properties=_load(row["properties_json"], {}) if "properties_json" in keys else {},
        body_markdown=row["body"],
    )


def _database(conn: sqlite3.Connection, row: sqlite3.Row) -> Database:
    page = conn.execute(
        "SELECT title, icon, created_at, updated_at FROM pages WHERE id = ?",
        (row["page_id"],),
    ).fetchone()
    return Database(
        id=uuid.UUID(row["id"]),
        page_id=uuid.UUID(row["page_id"]),
        title=page["title"],
        icon=page["icon"],
        schema_def=_load(row["schema_json"], []),
        created_at=_dt(page["created_at"]),
        updated_at=_dt(page["updated_at"]),
    )


def _view(row: sqlite3.Row) -> View:
    return View(
        id=uuid.UUID(row["id"]),
        database_id=uuid.UUID(row["database_id"]),
        name=row["name"],
        type=ViewType(row["type"]),
        config=_load(row["config_json"], {}),
    )


class Repository:
    """Acceso al índice y al vault. Una instancia por petición; `close()` al salir.

    Desde P1.2a las **notas** viven en ficheros `.md`, y desde P2 las **bases de
    datos y sus filas** también: los métodos de página, base, fila y vista
    delegan en `VaultStore`, que escribe el fichero y deja el índice diciendo lo
    mismo que el fichero. Ningún camino escribe solo en el índice.
    """

    def __init__(self, conn: sqlite3.Connection, vault: VaultStore | None = None):
        self.conn = conn
        self.vault = vault or VaultStore(settings.vault_path, conn)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- interno

    @contextlib.contextmanager
    def _tx(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def stats(self) -> dict[str, int]:
        return index_mod.count_rows(self.conn)

    def reindex(self, force: bool = False, empty_hint: str | None = None):
        """Reconstruye las notas del índice desde el vault (ADR D1).

        `force=True` permite que un vault ausente o vacío deje el índice a
        cero: es la salida explícita cuando el vaciado es intencionado, no el
        camino normal (ver `VaultStore.rebuild`). `empty_hint` es el texto que
        nombra esa salida en el mensaje de error, porque no es la misma con el
        servicio en marcha que durante el arranque.
        """
        return self.vault.rebuild(
            allow_empty=force,
            empty_hint=empty_hint or EMPTY_VAULT_HINT_HTTP,
        )

    # ----------------------------------------------------------- workspace

    def ensure_workspace(self, name: str = "default") -> uuid.UUID:
        row = self.conn.execute(
            "SELECT id FROM workspaces ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is not None:
            return uuid.UUID(row["id"])
        # Id fijo (`DEFAULT_WORKSPACE_ID`): un índice reconstruido tiene que
        # dar las mismas filas, y con uuid4 cambiaría el workspace_id de todas.
        workspace_id = index_mod.DEFAULT_WORKSPACE_ID
        with self._tx():
            self.conn.execute(
                "INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)",
                (workspace_id, name, _iso(_now())),
            )
        return uuid.UUID(workspace_id)

    # -------------------------------------------------------------- pages

    def list_pages(self) -> list[Page]:
        rows = self.conn.execute(
            f"SELECT {PAGE_FIELDS} FROM pages ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
        return [_page(r) for r in rows]

    def get_page(self, page_id: uuid.UUID | str) -> Optional[Page]:
        row = self.conn.execute(
            SELECT_PAGE + " WHERE p.id = ?", (str(page_id),)
        ).fetchone()
        return _page(row) if row else None

    def create_page(
        self,
        *,
        title: str,
        type: PageType = PageType.note,
        parent_id: Optional[uuid.UUID] = None,
        icon: Optional[str] = None,
        description: Optional[str] = None,
        created_by: Optional[str] = None,
        body_markdown: str = "",
    ) -> Page:
        if PageType(type) == PageType.note:
            page_id = self.vault.create(
                title=title,
                parent_id=parent_id,
                icon=icon,
                description=description,
                body=body_markdown or "",
                created_by=created_by,
            )
            return self.get_page(page_id)

        if PageType(type) == PageType.database:
            # Una base también nace con su fichero (P2): el `POST /pages` con
            # `type: database` crea la base vacía, no una página sin `databases`
            # detrás que el reindexado no podría reconstruir.
            page_id = self.vault.create_database(
                title=title,
                schema=[],
                parent_id=parent_id,
                icon=icon,
                description=description,
                created_by=created_by,
            )
            return self.get_page(page_id)

        raise VaultError(
            "una fila no se crea suelta: nace en su base "
            "(POST /databases/{database_id}/rows)"
        )

    def update_page(
        self,
        page_id: uuid.UUID,
        *,
        title: Optional[str] = None,
        icon: Optional[str] = None,
        description: Optional[str] = None,
        body_markdown: Optional[str] = None,
        parent_id: Any = UNSET,
    ) -> Optional[Page]:
        """Actualiza solo los campos que llegan (None = no tocar), como el PATCH
        de siempre.

        Un escalar que llega con el valor que ya tenía **no** cuenta como
        cambio: `updated_at` no se mueve y, desde P1.2a, tampoco se reescribe el
        `.md` (el PATCH viejo tampoco movía `updated_at`, y mudarlo cambiaba el
        orden de «Páginas recientes» sin que nadie hubiera tocado nada). El
        cuerpo sí lo mueve siempre que llegue, aunque sea idéntico: es lo que
        hacía el `update_page` anterior (que forzaba `updated_at` al guardar el
        cuerpo) y lo que espera el panel.
        """
        current = self.get_page(page_id)
        if current is None:
            return None

        changes: dict[str, Any] = {}
        if title is not None and title != current.title:
            changes["title"] = title
        if icon is not None and icon != current.icon:
            changes["icon"] = icon
        if description is not None and description != current.description:
            changes["description"] = description
        if body_markdown is not None:
            changes["body"] = body_markdown
        if parent_id is not UNSET:
            new_parent = str(parent_id) if parent_id else None
            current_parent = str(current.parent_id) if current.parent_id else None
            if new_parent != current_parent:
                changes["parent_id"] = new_parent

        if not changes:
            return current

        if current.type == PageType.note:
            self.vault.save(page_id, **changes)
        elif current.type == PageType.database:
            # Mismo camino que PATCH /databases/{id}: la base es un fichero y su
            # título es el nombre, así que renombrar mueve el fichero (y sus
            # filas) y el índice se actualiza leyéndolo.
            self.vault.save_database(page_id, **changes)
        else:
            if "parent_id" in changes:
                raise VaultError(
                    "una fila no se mueve de carpeta: su sitio lo decide su base"
                )
            self.vault.save_row(page_id, **changes)
        return self.get_page(page_id)

    def set_header_image(self, page_id: uuid.UUID, path: Optional[str]) -> Optional[Page]:
        current = self.get_page(page_id)
        if current is None:
            return None
        if current.type == PageType.note:
            # La cabecera se guarda también en el frontmatter: si solo viviera
            # en el índice, el primer reindexado se la llevaría por delante.
            self.vault.save(page_id, header_image=path)
        elif current.type == PageType.database:
            self.vault.save_database(page_id, header_image=path)
        else:
            self.vault.save_row(page_id, header_image=path)
        return self.get_page(page_id)

    def delete_page(self, page_id: uuid.UUID) -> bool:
        """Borra la página y todo lo que cuelga de ella.

        Para una **nota**, borrar es mover a la papelera: su `.md` y su carpeta
        de subpáginas van a `.trash/<fecha UTC>/<ruta original>` y la fila del
        índice desaparece con la cascada de siempre.

        Para una **base de datos** (P2) es lo mismo: su `.md` y su carpeta de
        filas van a la papelera, y sus filas, vistas y enlaces se van del
        índice. Para una **fila**, solo su fichero.

        Las filas de una database son páginas que **no** cuelgan por
        `parent_id`: al borrar la página de la database, la cascada del esquema
        se lleva la fila `databases`, sus vistas y las entradas de `rows`, pero
        no las páginas de esas filas (la FK va de `rows.id` a `pages.id`, así
        que borrar la entrada no borra la página). Por eso `VaultStore` las
        borra explícitamente, en el mismo sitio para las dos rutas: DELETE
        /pages y DELETE /databases dejan exactamente el mismo estado, sin
        páginas huérfanas fuera del árbol.
        """
        current = self.get_page(page_id)
        if current is None:
            return False
        if current.type == PageType.note:
            return self.vault.delete(page_id)
        if current.type == PageType.database:
            return self.vault.delete_database(page_id)
        return self.vault.delete_row(page_id)

    def tree(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, title, type, icon, parent_id FROM pages"
            " WHERE type != ? ORDER BY title COLLATE NOCASE, title",
            (PageType.database_row.value,),
        ).fetchall()
        database_by_page = {
            r["page_id"]: r["id"]
            for r in self.conn.execute("SELECT id, page_id FROM databases")
        }
        nodes = {
            r["id"]: {
                "id": uuid.UUID(r["id"]),
                "title": r["title"],
                "type": PageType(r["type"]),
                "icon": r["icon"],
                "parent_id": _uuid(r["parent_id"]),
                "database_id": _uuid(database_by_page.get(r["id"])),
                "children": [],
            }
            for r in rows
        }
        roots: list[dict[str, Any]] = []
        for r in rows:
            node = nodes[r["id"]]
            parent = r["parent_id"]
            if parent and parent in nodes:
                nodes[parent]["children"].append(node)
            else:
                roots.append(node)
        return roots

    def backlinks(self, page_id: uuid.UUID) -> list[Page]:
        rows = self.conn.execute(
            f"SELECT {PAGE_FIELDS_P} FROM links l JOIN pages p ON p.id = l.from_id"
            " WHERE l.to_id = ? AND l.kind = ? ORDER BY p.title COLLATE NOCASE",
            (str(page_id), LinkKind.wikilink.value),
        ).fetchall()
        return [_page(r) for r in rows]

    # ------------------------------------------------ sugerencias (P10a)

    def page_path(self, page_id: uuid.UUID | str) -> Optional[str]:
        """Ruta del `.md` de una página en el vault (o None si no está)."""
        row = self.conn.execute(
            "SELECT path FROM pages WHERE id = ?", (str(page_id),)
        ).fetchone()
        return row["path"] if row is not None else None

    def create_suggestion(
        self,
        page_id: uuid.UUID,
        *,
        agent: str,
        kind: str = "edit",
        old_text: str = "",
        new_text: str = "",
        rationale: str = "",
        title: Optional[str] = None,
        offset: Optional[int] = None,
    ) -> Optional[Suggestion]:
        """Guarda una sugerencia en el sidecar de la nota. None si no hay nota.

        `old_text` se comprueba **contra el cuerpo de ahora**: si ya no está, la
        sugerencia no se rechaza —el agente pudo basarse en una versión
        anterior— sino que nace `orphaned`, que es lo que la interfaz necesita
        para enseñarla como «ya no encaja» sin perderla ni mentir sobre ella.
        """
        page = self.get_page(page_id)
        if page is None:
            return None
        note_rel = self.page_path(page_id)
        if note_rel is None:        # pragma: no cover - una nota siempre tiene ruta
            raise VaultError(f"la nota {page_id} no tiene ruta en el índice")
        if kind not in suggestions.KINDS:
            raise SuggestionError(f"kind desconocido: {kind!r}")

        body = page.body_markdown or ""
        status = "pending"
        if kind == "edit":
            if not old_text:
                raise SuggestionError("una sugerencia 'edit' necesita old_text")
            positions = suggestions.occurrences(body, old_text)
            if offset is None:
                # Sin pista del agente, el ancla empieza en la primera
                # aparición: el mismo sitio que elegiría aceptar sin offset.
                offset = positions[0] if positions else 0
            if not positions:
                status = "orphaned"
        else:
            if not (title or "").strip():
                raise SuggestionError("una sugerencia 'new_page' necesita title")
            old_text = ""
            offset = 0

        suggestion = suggestions.Suggestion(
            id=str(uuid.uuid4()),
            page_id=str(page.id),
            agent=agent,
            created_at=_iso(_now()),
            status=status,
            kind=kind,
            old_text=old_text,
            new_text=new_text,
            rationale=rationale,
            title=title,
            base_revision=_iso(page.updated_at) if page.updated_at else None,
            offset=max(int(offset or 0), 0),
        )
        suggestions.write(self.vault.root, suggestion, note_rel)
        return suggestion

    def list_suggestions(
        self, page_id: uuid.UUID, status: Optional[str] = None
    ) -> Optional[list[Suggestion]]:
        """Sugerencias de una nota, las más recientes primero. None si no hay nota."""
        if self.get_page(page_id) is None:
            return None
        if status is not None and status not in suggestions.STATUSES:
            raise SuggestionError(f"status desconocido: {status!r}")
        note_rel = self.page_path(page_id)
        if note_rel is None:        # pragma: no cover
            return []
        return suggestions.list_for(self.vault.root, note_rel, status)

    def get_suggestion(self, suggestion_id: str) -> Optional[Suggestion]:
        found = suggestions.find(self.vault.root, suggestion_id)
        return found[0] if found is not None else None

    def accept_suggestion(self, suggestion_id: str) -> dict[str, Any]:
        """Aplica el parche si el ancla sigue estando.

        - `edit`: busca `old_text` en el cuerpo **actual**; si aparece varias
          veces elige la más cercana a `offset`; si no aparece, deja la
          sugerencia `orphaned` y lanza `SuggestionUnanchored`.
        - `new_page`: crea la nota con el título y el cuerpo propuestos.

        La escritura va por `update_page`/`create_page`, que es la ruta de un
        guardado normal: `.md` atómico, índice al día y el fichero nuevo donde
        el reindexado lo volvería a encontrar. Devuelve `{suggestion, page,
        created, stale}`.
        """
        found = suggestions.find(self.vault.root, suggestion_id)
        if found is None:
            raise SuggestionNotFound(suggestion_id)
        suggestion, note_rel = found
        if suggestion.status in ("accepted", "rejected"):
            raise SuggestionResolved(suggestion.status)

        stale = False
        if suggestion.kind == "new_page":
            page = self.create_page(
                title=(suggestion.title or "Sin título").strip() or "Sin título",
                icon=settings.default_icon,
                created_by=suggestion.agent or None,
                body_markdown=suggestion.new_text,
            )
            created = True
        else:
            page = self.get_page(suggestion.page_id)
            if page is None or self.page_path(suggestion.page_id) is None:
                self._orphan(suggestion, note_rel)
                raise SuggestionUnanchored(suggestion_id)
            stale = bool(suggestion.base_revision) and (
                _iso(page.updated_at) != suggestion.base_revision
            )
            applied = suggestions.apply(
                page.body_markdown or "",
                suggestion.old_text,
                suggestion.new_text,
                suggestion.offset,
            )
            if applied is None:
                self._orphan(suggestion, note_rel)
                raise SuggestionUnanchored(suggestion_id)
            new_body, _position = applied
            page = self.update_page(
                uuid.UUID(suggestion.page_id), body_markdown=new_body
            )
            created = False

        suggestion.status = "accepted"
        suggestion.decided_at = _iso(_now())
        suggestions.write(self.vault.root, suggestion, note_rel)
        return {
            "suggestion": suggestion,
            "page": page,
            "created": created,
            "stale": stale,
        }

    def reject_suggestion(
        self, suggestion_id: str, reason: Optional[str] = None
    ) -> Suggestion:
        """Marca `rejected`, con el motivo si lo hay. El `.md` no se toca."""
        found = suggestions.find(self.vault.root, suggestion_id)
        if found is None:
            raise SuggestionNotFound(suggestion_id)
        suggestion, note_rel = found
        if suggestion.status in ("accepted", "rejected"):
            raise SuggestionResolved(suggestion.status)
        suggestion.status = "rejected"
        suggestion.decided_at = _iso(_now())
        suggestion.reason = reason or None
        suggestions.write(self.vault.root, suggestion, note_rel)
        return suggestion

    def reply_to_suggestion(
        self, suggestion_id: str, *, text: str, author: Optional[str] = None
    ) -> Suggestion:
        """Añade una entrada al hilo del dueño (el canal de vuelta al agente)."""
        if not (text or "").strip():
            raise SuggestionError("la respuesta no puede estar vacía")
        found = suggestions.find(self.vault.root, suggestion_id)
        if found is None:
            raise SuggestionNotFound(suggestion_id)
        suggestion, note_rel = found
        suggestion.replies.append(
            {
                "author": author or settings.default_user_name,
                "text": text,
                "at": _iso(_now()),
            }
        )
        suggestions.write(self.vault.root, suggestion, note_rel)
        return suggestion

    def _orphan(self, suggestion: Suggestion, note_rel: str) -> None:
        """Deja escrito que el ancla ya no está, antes de contarlo por error."""
        suggestion.status = "orphaned"
        suggestions.write(self.vault.root, suggestion, note_rel)

    # ---------------------------------------------------------- databases

    def list_databases(self) -> list[Database]:
        rows = self.conn.execute(
            "SELECT id, page_id, schema_json FROM databases ORDER BY rowid"
        ).fetchall()
        return [_database(self.conn, r) for r in rows]

    def get_database(self, database_id: uuid.UUID) -> Optional[Database]:
        row = self.conn.execute(
            "SELECT id, page_id, schema_json FROM databases WHERE id = ?",
            (str(database_id),),
        ).fetchone()
        return _database(self.conn, row) if row else None

    def create_database(
        self,
        *,
        title: str,
        schema_def: list[dict[str, Any]],
        parent_id: Optional[uuid.UUID] = None,
        icon: Optional[str] = None,
        created_by: Optional[str] = None,
        views: Optional[list[dict[str, Any]]] = None,
    ) -> Database:
        """Crea `Bases/<slug>.md` (y sus vistas en el frontmatter) y la indexa."""
        return self.get_database(uuid.UUID(self.vault.create_database(
            title=title,
            schema=schema_def,
            views=views,
            parent_id=parent_id,
            icon=icon,
            created_by=created_by,
        )))

    def update_database(
        self,
        database_id: uuid.UUID,
        *,
        title: Optional[str] = None,
        icon: Optional[str] = None,
        schema_def: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[Database]:
        current = self.get_database(database_id)
        if current is None:
            return None
        # Nota (v1): las filas existentes no se revalidan contra el schema
        # nuevo; pueden quedar con propiedades que ya no están definidas hasta
        # que se vuelvan a guardar (y entonces se filtran al escribir).
        self.vault.save_database(
            current.page_id,
            title=UNSET if title is None else title,
            icon=UNSET if icon is None else icon,
            schema=UNSET if schema_def is None else schema_def,
        )
        return self.get_database(database_id)

    def delete_database(self, database_id: uuid.UUID) -> bool:
        current = self.get_database(database_id)
        if current is None:
            return False
        return self.delete_page(current.page_id)

    # --------------------------------------------------------------- rows

    def list_rows(self, database_id: uuid.UUID) -> list[Page]:
        rows = self.conn.execute(
            SELECT_PAGE + " WHERE r.database_id = ? ORDER BY p.created_at, p.rowid",
            (str(database_id),),
        ).fetchall()
        return [_page(r) for r in rows]

    def get_row(self, database_id: uuid.UUID, row_id: uuid.UUID) -> Optional[Page]:
        row = self.conn.execute(
            SELECT_PAGE + " WHERE r.database_id = ? AND r.id = ?",
            (str(database_id), str(row_id)),
        ).fetchone()
        return _page(row) if row else None

    def create_row(
        self,
        database_id: uuid.UUID,
        *,
        title: str,
        icon: Optional[str] = None,
        properties: Optional[dict[str, Any]] = None,
    ) -> Page:
        row_id = self.vault.create_row(
            database_id, title=title, properties=properties or {}, icon=icon
        )
        return self.get_row(database_id, uuid.UUID(row_id))

    def update_row(
        self,
        database_id: uuid.UUID,
        row_id: uuid.UUID,
        *,
        title: Optional[str] = None,
        icon: Optional[str] = None,
        properties: Optional[dict[str, Any]] = None,
    ) -> Optional[Page]:
        current = self.get_row(database_id, row_id)
        if current is None:
            return None
        merged: Optional[dict[str, Any]] = None
        if properties is not None:
            # Merge parcial, como antes: lo que no llega se conserva. Un valor
            # `None` borra la propiedad del frontmatter.
            merged = {**(current.properties or {}), **properties}
        if merged is None and title is None and icon is None:
            return current
        self.vault.save_row(
            row_id,
            title=UNSET if title is None else title,
            icon=UNSET if icon is None else icon,
            properties=UNSET if merged is None else merged,
        )
        return self.get_row(database_id, row_id)

    def delete_row(self, database_id: uuid.UUID, row_id: uuid.UUID) -> bool:
        if self.get_row(database_id, row_id) is None:
            return False
        return self.vault.delete_row(row_id)

    # -------------------------------------------------------------- views

    def list_views(self, database_id: uuid.UUID) -> list[View]:
        rows = self.conn.execute(
            "SELECT id, database_id, name, type, config_json FROM views"
            " WHERE database_id = ? ORDER BY rowid",
            (str(database_id),),
        ).fetchall()
        return [_view(r) for r in rows]

    def get_view(self, database_id: uuid.UUID, view_id: uuid.UUID) -> Optional[View]:
        row = self.conn.execute(
            "SELECT id, database_id, name, type, config_json FROM views"
            " WHERE id = ? AND database_id = ?",
            (str(view_id), str(database_id)),
        ).fetchone()
        return _view(row) if row else None

    def _views_with(self, database_id: uuid.UUID, **overrides) -> list[dict[str, Any]]:
        """Las vistas de la base en la forma del fichero, con una sustituida."""
        views: list[dict[str, Any]] = []
        for view in self.list_views(database_id):
            entry = {
                "id": str(view.id),
                "name": view.name,
                "type": view.type.value,
                "config": view.config or {},
            }
            if entry["id"] == overrides.get("id"):
                entry.update({k: v for k, v in overrides.items() if v is not None})
            views.append(entry)
        return views

    def _database_page_id(self, database_id: uuid.UUID) -> uuid.UUID:
        """La página (fichero) de la base. Desde P2 es el mismo id; la consulta
        deja el camino preparado para un índice anterior al reindexado."""
        row = self.conn.execute(
            "SELECT page_id FROM databases WHERE id = ?", (str(database_id),)
        ).fetchone()
        if row is None:
            raise VaultError(f"la base de datos {database_id} no existe")
        return uuid.UUID(row["page_id"])

    def create_view(
        self,
        database_id: uuid.UUID,
        *,
        name: str,
        type: ViewType,
        config: Optional[dict[str, Any]] = None,
    ) -> View:
        """Añade la vista al frontmatter de la base y al índice, en una operación."""
        view_id = str(uuid.uuid4())
        views = self._views_with(database_id)
        views.append({
            "id": view_id,
            "name": name,
            "type": str(getattr(type, "value", type)),
            "config": config or {},
        })
        self.vault.save_database(self._database_page_id(database_id), views=views)
        return self.get_view(database_id, uuid.UUID(view_id))

    def update_view(
        self,
        database_id: uuid.UUID,
        view_id: uuid.UUID,
        *,
        name: Optional[str] = None,
        type: Optional[ViewType] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> Optional[View]:
        current = self.get_view(database_id, view_id)
        if current is None:
            return None
        if name is None and type is None and config is None:
            return current
        views = self._views_with(
            database_id,
            id=str(view_id),
            name=name,
            type=str(getattr(type, "value", type)) if type is not None else None,
            config=config,
        )
        self.vault.save_database(self._database_page_id(database_id), views=views)
        return self.get_view(database_id, view_id)

    def delete_view(self, database_id: uuid.UUID, view_id: uuid.UUID) -> bool:
        if self.get_view(database_id, view_id) is None:
            return False
        views = [v for v in self._views_with(database_id) if v["id"] != str(view_id)]
        self.vault.save_database(self._database_page_id(database_id), views=views)
        return True

    # ------------------------------------------------------------- search

    def search(self, text: str, limit: int = 50) -> list[Page]:
        query = index_mod.fts_query(text)
        if not query:
            return []
        rows = self.conn.execute(
            f"SELECT {PAGE_FIELDS_P} FROM pages_fts JOIN pages p"
            " ON p.rowid = pages_fts.rowid"
            " WHERE pages_fts MATCH ? ORDER BY bm25(pages_fts), p.title COLLATE NOCASE"
            " LIMIT ?",
            (query, limit),
        ).fetchall()
        return [_page(r) for r in rows]

    # ---------------------------------------------------------- dashboard

    def list_recent_pages(self, limit: int) -> list[Page]:
        rows = self.conn.execute(
            f"SELECT {PAGE_FIELDS} FROM pages WHERE type != ?"
            " ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (PageType.database_row.value, limit),
        ).fetchall()
        return [_page(r) for r in rows]
