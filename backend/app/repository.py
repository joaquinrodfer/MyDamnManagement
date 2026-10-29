"""Capa de repositorio: todo el acceso al índice pasa por aquí.

Los routers ya no ven SQL ni sesiones: piden y escriben a través de `Repository`,
y desde P1.2a guardar una nota escribe además su fichero `.md` (`VaultNotes`,
`app/vault/store.py`) y deja el índice diciendo lo mismo que el fichero. El
objetivo de P1.1 era exactamente este: cambiar la fuente de la verdad sin tocar
las rutas.

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
from app.vault.store import UNSET, VaultNotes


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _uuid(value: Optional[str]) -> Optional[uuid.UUID]:
    return uuid.UUID(value) if value else None


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


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

    Desde P1.2a las **notas** viven en ficheros `.md`: los métodos de página
    delegan en `VaultNotes`, que escribe el fichero y deja el índice diciendo lo
    mismo que el fichero. Las bases de datos y sus filas siguen solo en el
    índice (P2), así que conservan el camino antiguo.
    """

    def __init__(self, conn: sqlite3.Connection, vault: VaultNotes | None = None):
        self.conn = conn
        self.vault = vault or VaultNotes(settings.vault_path, conn)

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

    def reindex(self, force: bool = False):
        """Reconstruye las notas del índice desde el vault (ADR D1).

        `force=True` permite que un vault ausente o vacío deje el índice a
        cero: es la salida explícita cuando el vaciado es intencionado, no el
        camino normal (ver `VaultNotes.rebuild`).
        """
        return self.vault.rebuild(allow_empty=force)

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

        workspace_id = self.ensure_workspace()
        page_id = str(uuid.uuid4())
        now = _iso(_now())
        with self._tx():
            self.conn.execute(
                "INSERT INTO pages (id, workspace_id, parent_id, type, title, icon,"
                " description, created_by, body, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    page_id,
                    str(workspace_id),
                    str(parent_id) if parent_id else None,
                    PageType(type).value,
                    title,
                    icon,
                    description,
                    created_by,
                    body_markdown or "",
                    now,
                    now,
                ),
            )
            sync_wikilinks(self.conn, page_id, str(workspace_id), body_markdown)
        return self.get_page(page_id)

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
            return self.get_page(page_id)

        sets: list[str] = []
        params: list[Any] = []
        for field_name, column in (("title", "title"), ("icon", "icon"),
                                   ("description", "description")):
            if field_name in changes:
                sets.append(f"{column} = ?")
                params.append(changes[field_name])
        if "body" in changes:
            sets.append("body = ?")
            params.append(changes["body"])

        sets.append("updated_at = ?")
        params.append(_iso(_now()))
        params.append(str(page_id))

        with self._tx():
            self.conn.execute(f"UPDATE pages SET {', '.join(sets)} WHERE id = ?", params)
            if "body" in changes:
                sync_wikilinks(
                    self.conn, str(page_id), str(current.workspace_id), changes["body"]
                )
        return self.get_page(page_id)

    def set_header_image(self, page_id: uuid.UUID, path: Optional[str]) -> Optional[Page]:
        current = self.get_page(page_id)
        if current is None:
            return None
        if current.type == PageType.note:
            # La cabecera se guarda también en el frontmatter: si solo viviera
            # en el índice, el primer reindexado se la llevaría por delante.
            self.vault.save(page_id, header_image=path)
            return self.get_page(page_id)
        with self._tx():
            self.conn.execute(
                "UPDATE pages SET header_image_path = ?, updated_at = ? WHERE id = ?",
                (path, _iso(_now()), str(page_id)),
            )
        return self.get_page(page_id)

    def delete_page(self, page_id: uuid.UUID) -> bool:
        """Borra la página y todo lo que cuelga de ella.

        Para una **nota**, borrar es mover a la papelera: su `.md` y su carpeta
        de subpáginas van a `.trash/<fecha UTC>/<ruta original>` y la fila del
        índice desaparece con la cascada de siempre.

        Las filas de una database son páginas que **no** cuelgan por
        `parent_id`: al borrar la página de la database, la cascada del esquema
        se lleva la fila `databases`, sus vistas y las entradas de `rows`, pero
        no las páginas de esas filas (la FK va de `rows.id` a `pages.id`, así
        que borrar la entrada no borra la página). Por eso se borran aquí
        explícitamente, en el mismo sitio para las dos rutas: DELETE /pages y
        DELETE /databases dejan exactamente el mismo estado, sin páginas
        huérfanas fuera del árbol.
        """
        current = self.get_page(page_id)
        if current is None:
            return False
        if current.type == PageType.note:
            return self.vault.delete(page_id)

        with self._tx():
            self.conn.execute(
                "DELETE FROM pages WHERE id IN ("
                " SELECT r.id FROM rows r JOIN databases d ON d.id = r.database_id"
                " WHERE d.page_id = ?)",
                (str(page_id),),
            )
            cur = self.conn.execute("DELETE FROM pages WHERE id = ?", (str(page_id),))
        return cur.rowcount > 0

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
        workspace_id = self.ensure_workspace()
        page_id = str(uuid.uuid4())
        database_id = str(uuid.uuid4())
        now = _iso(_now())
        with self._tx():
            self.conn.execute(
                "INSERT INTO pages (id, workspace_id, parent_id, type, title, icon,"
                " created_by, body, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
                (
                    page_id,
                    str(workspace_id),
                    str(parent_id) if parent_id else None,
                    PageType.database.value,
                    title,
                    icon,
                    created_by,
                    now,
                    now,
                ),
            )
            self.conn.execute(
                "INSERT INTO databases (id, page_id, schema_json) VALUES (?, ?, ?)",
                (database_id, page_id, _dump(schema_def)),
            )
            for view in views or []:
                self.conn.execute(
                    "INSERT INTO views (id, database_id, name, type, config_json)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        database_id,
                        view["name"],
                        view["type"],
                        _dump(view.get("config") or {}),
                    ),
                )
        return self.get_database(database_id)

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
        sets: list[str] = []
        params: list[Any] = []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if icon is not None:
            sets.append("icon = ?")
            params.append(icon)
        if not sets and schema_def is None:
            return current
        with self._tx():
            if sets:
                sets.append("updated_at = ?")
                params.append(_iso(_now()))
                params.append(str(current.page_id))
                self.conn.execute(
                    f"UPDATE pages SET {', '.join(sets)} WHERE id = ?", params
                )
            if schema_def is not None:
                # Nota (v1): las filas existentes no se revalidan contra el
                # schema nuevo; pueden quedar con propiedades que ya no están
                # definidas hasta que se vuelvan a guardar.
                self.conn.execute(
                    "UPDATE databases SET schema_json = ? WHERE id = ?",
                    (_dump(schema_def), str(database_id)),
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
        workspace_id = self.ensure_workspace()
        row_id = str(uuid.uuid4())
        now = _iso(_now())
        with self._tx():
            self.conn.execute(
                "INSERT INTO pages (id, workspace_id, type, title, icon, body,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, '', ?, ?)",
                (
                    row_id,
                    str(workspace_id),
                    PageType.database_row.value,
                    title,
                    icon,
                    now,
                    now,
                ),
            )
            self.conn.execute(
                "INSERT INTO rows (id, database_id, properties_json) VALUES (?, ?, ?)",
                (row_id, str(database_id), _dump(properties or {})),
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
        sets: list[str] = []
        params: list[Any] = []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if icon is not None:
            sets.append("icon = ?")
            params.append(icon)
        merged: Optional[dict[str, Any]] = None
        if properties is not None:
            # Merge parcial, como antes: lo que no llega se conserva.
            merged = {**(current.properties or {}), **properties}
        if not sets and merged is None:
            return current
        now = _iso(_now())
        with self._tx():
            if sets:
                sets.append("updated_at = ?")
                params.append(now)
                params.append(str(row_id))
                self.conn.execute(
                    f"UPDATE pages SET {', '.join(sets)} WHERE id = ?", params
                )
            if merged is not None:
                self.conn.execute(
                    "UPDATE rows SET properties_json = ? WHERE id = ?",
                    (_dump(merged), str(row_id)),
                )
                if not sets:
                    self.conn.execute(
                        "UPDATE pages SET updated_at = ? WHERE id = ?",
                        (now, str(row_id)),
                    )
        return self.get_row(database_id, row_id)

    def delete_row(self, database_id: uuid.UUID, row_id: uuid.UUID) -> bool:
        if self.get_row(database_id, row_id) is None:
            return False
        return self.delete_page(row_id)

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

    def create_view(
        self,
        database_id: uuid.UUID,
        *,
        name: str,
        type: ViewType,
        config: Optional[dict[str, Any]] = None,
    ) -> View:
        view_id = str(uuid.uuid4())
        with self._tx():
            self.conn.execute(
                "INSERT INTO views (id, database_id, name, type, config_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (view_id, str(database_id), name, ViewType(type).value, _dump(config or {})),
            )
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
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if type is not None:
            sets.append("type = ?")
            params.append(ViewType(type).value)
        if config is not None:
            sets.append("config_json = ?")
            params.append(_dump(config))
        if not sets:
            return current
        params.append(str(view_id))
        with self._tx():
            self.conn.execute(f"UPDATE views SET {', '.join(sets)} WHERE id = ?", params)
        return self.get_view(database_id, view_id)

    def delete_view(self, database_id: uuid.UUID, view_id: uuid.UUID) -> bool:
        with self._tx():
            cur = self.conn.execute(
                "DELETE FROM views WHERE id = ? AND database_id = ?",
                (str(view_id), str(database_id)),
            )
        return cur.rowcount > 0

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
