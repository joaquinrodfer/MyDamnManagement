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
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

from app import acl, auth, closed, index as index_mod, templates as template_defs
from app import workspaces as ws_mod
from app.vault import store as store_mod
from app.config import settings
from app.enums import LinkKind, PageType, ViewType
from app.links import fold_title, sync_wikilinks  # noqa: F401 - API histórica del módulo
from app.properties import DEFAULT_TITLE_NAME, plan_schema
from app.vault.reader import PERSON
from app.vault import suggestions
from app.vault.store import (
    EMPTY_VAULT_HINT_HTTP,
    UNSET,
    VaultError,
    VaultStore,
    VersionConflict,
)
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


def _plegar(value: Any) -> str:
    """Nombre de usuario comparable: NFC, sin espacios de sobra, casefold."""
    return unicodedata.normalize("NFC", str(value)).strip().casefold()


# Cuántos ids caben en una consulta de resolución de relaciones. SQLite admite
# miles de parámetros, pero el tope es del motor (32766 en las versiones que la
# app soporta) y no queremos depender de él. Trocear sigue sin depender del
# número de filas de la respuesta: una tabla de 20 filas resuelve sus títulos
# con una sola consulta.
_REFS_CHUNK = 900


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
    # Cuántas veces se ha escrito (3.0, hito 2): lo que `If-Match` compara.
    version: int = 1


@dataclass(frozen=True)
class Database:
    id: uuid.UUID
    page_id: uuid.UUID
    title: str
    icon: Optional[str]
    schema_def: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    # La columna del título: cómo se llama y si la tabla la enseña. Una base
    # sin estas claves en su `.md` sale con «Título» y visible.
    title_name: str = DEFAULT_TITLE_NAME
    title_hidden: bool = False
    # Plantillas de fila de la base (`row_templates:` del `.md`): nombre,
    # valores por defecto y cuerpo inicial. Se copian al índice al leer el
    # fichero, así que una base escrita a mano también las trae.
    row_templates: list[dict[str, Any]] = field(default_factory=list)
    # Qué cuenta como «fila cerrada»: `{property, values}`, o `None` si la base
    # no declara estado. Es la regla EFECTIVA: el bloque `closed:` del `.md` o,
    # si no lo hay, la que implican los marcadores `done` viejos
    # (`app.closed.effective`). `closed_from_done` dice que viene de ahí, para
    # que la interfaz pueda explicar que se está leyendo el formato anterior.
    closed: Optional[dict[str, Any]] = None
    closed_from_done: bool = False


@dataclass(frozen=True)
class View:
    id: uuid.UUID
    database_id: uuid.UUID
    name: str
    type: ViewType
    config: dict[str, Any]


PAGE_FIELDS = (
    "id, workspace_id, parent_id, type, title, icon, description, created_by,"
    " header_image_path, body, created_at, updated_at, version"
)
PAGE_FIELDS_P = ", ".join("p." + name.strip() for name in PAGE_FIELDS.split(","))

# Página con su fila de `rows` si la hay (filas de database). Un LEFT JOIN
# porque `pages` no guarda `database_id` ni `properties`: eso vive en `rows`.
SELECT_PAGE = (
    f"SELECT {PAGE_FIELDS_P}, r.database_id AS database_id,"
    " r.properties_json AS properties_json"
    " FROM pages p LEFT JOIN rows r ON r.id = p.id"
)


class AuthorColors(dict):
    """`display_name -> color` (lo de siempre) con `avatars`: `display_name ->
    avatar`. Es un `dict` para que quien solo quiere el color no cambie."""

    avatars: dict[str, str]


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
        version=row["version"] if "version" in keys else 1,
    )


def _database(conn: sqlite3.Connection, row: sqlite3.Row) -> Database:
    page = conn.execute(
        "SELECT title, icon, created_at, updated_at FROM pages WHERE id = ?",
        (row["page_id"],),
    ).fetchone()
    title = conn.execute(
        "SELECT name, hidden FROM database_titles WHERE database_id = ?",
        (row["id"],),
    ).fetchone()
    schema_def = _load(row["schema_json"], [])
    # La regla efectiva se decide aquí y en un solo sitio (`app.closed`): el
    # bloque `closed:` del `.md` manda; sin él, los `done` viejos del schema.
    declared = conn.execute(
        "SELECT rule_json FROM database_closed WHERE database_id = ?", (row["id"],)
    ).fetchone()
    rule, source = closed.effective(
        _load(declared["rule_json"], None) if declared is not None else None,
        schema_def,
    )
    plantillas = conn.execute(
        "SELECT templates_json FROM database_row_templates WHERE database_id = ?",
        (row["id"],),
    ).fetchone()
    return Database(
        id=uuid.UUID(row["id"]),
        page_id=uuid.UUID(row["page_id"]),
        title=page["title"],
        icon=page["icon"],
        schema_def=schema_def,
        created_at=_dt(page["created_at"]),
        updated_at=_dt(page["updated_at"]),
        title_name=(title["name"] or DEFAULT_TITLE_NAME) if title else DEFAULT_TITLE_NAME,
        title_hidden=bool(title["hidden"]) if title else False,
        closed=rule,
        closed_from_done=source == closed.SOURCE_DONE,
        row_templates=_load(plantillas["templates_json"], []) if plantillas else [],
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

    def __init__(
        self,
        conn: sqlite3.Connection,
        vault: VaultStore | None = None,
        user: Any = None,
        workspace_id: str | None = None,
    ):
        self.conn = conn
        self.vault = vault or VaultStore(settings.vault_path, conn)
        # El espacio activo de la petición (plan H1): los listados se limitan a
        # él y lo que se crea sin padre va a él. `None`: todo lo que ve `user`.
        self.workspace_id = workspace_id
        self.vault.active_workspace = workspace_id
        # Quién pregunta (3.0, hito 3). `None` = el sistema (arranque, scripts):
        # lo ve todo. Los listados se filtran con `acl.visible_clause`.
        self.user = user

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- interno

    @contextlib.contextmanager
    def _tx(self) -> Iterator[None]:
        # Reentrante y con la proyección del almacén, como `VaultStore._tx`:
        # una operación del repositorio que llama a varias del almacén las
        # confirma juntas (y solo entonces proyecta al disco).
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            self.vault.fs.discard()
            raise
        else:
            self.conn.commit()
            self.vault.fs.flush()

    def _visible(self, alias: str = "p") -> tuple[str, list[Any]]:
        """Condición SQL «lo ve quien pregunta» (3.0, hito 3)."""
        return acl.visible_clause(self.user, alias, self.workspace_id)

    def _own_if_root(self, resource_type: str, page_id: Any, parent_id: Any) -> None:
        """Una página nueva en la raíz es de quien la crea (y solo suya)."""
        if parent_id or self.user is None:
            return
        acl.make_owner(self.conn, resource_type, page_id, self.user.id)

    def stats(self) -> dict[str, int]:
        return index_mod.count_rows(self.conn)

    def author_colors(self) -> dict[str, str]:
        """`display_name` -> color de los usuarios, para la autoría visible.

        `pages.created_by` guarda el `display_name` (texto, no un id), así que
        la única forma de saber de qué color pintar el punto de un autor es
        resolverlo contra `users` en el servidor: el cliente no puede listar
        usuarios si no es admin. Una consulta por respuesta (la tabla es
        pequeña: una fila por persona del vault), nunca una por página.

        `display_name` no es UNIQUE (la UNIQUE es `username`), así que dos
        usuarios con el mismo nombre comparten color aquí; es la misma
        ambigüedad que ya arrastra `created_by`, que guarda el nombre.
        """
        rows = self.conn.execute("SELECT display_name, username, color, avatar FROM users").fetchall()
        mapa = AuthorColors(
            {row["display_name"]: row["color"] for row in rows if row["color"]}
        )
        # Y el avatar (bloque B): lo que se pinta junto al nombre del autor.
        mapa.avatars = {row["display_name"]: auth.avatar_of(row) for row in rows}
        return mapa

    def user_refs(
        self, ids: Iterable[str]
    ) -> dict[str, tuple[str, str, Optional[str]]]:
        """`id -> (display_name, username, color)` de los usuarios que existan.

        **Una consulta por respuesta**, como `row_refs`: una tabla con veinte
        responsables resuelve sus veinte nombres de una vez. Un id que ya no
        está en `users` simplemente no aparece en el mapa, y quien serializa la
        celda lo pinta como «ya no está»: el `.md` conserva el id y no se toca.
        """
        wanted = [str(one).strip() for one in ids if str(one).strip()]
        if not wanted:
            return {}
        refs: dict[str, tuple[str, str, Optional[str]]] = {}
        for start in range(0, len(wanted), _REFS_CHUNK):
            chunk = wanted[start:start + _REFS_CHUNK]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                "SELECT id, display_name, username, color, avatar FROM users"
                f" WHERE id IN ({marks})",
                chunk,
            ).fetchall()
            for row in rows:
                refs[row["id"]] = (
                    row["display_name"], row["username"], row["color"], auth.avatar_of(row)
                )
        return refs

    def all_users(self) -> list[dict[str, Any]]:
        """Los usuarios de la app por nombre visible: las personas asignables.

        Es la lista del selector de una celda `person` (`GET /people`). No lleva
        hash ni salt: sale de `users` sin las columnas de credenciales.
        """
        rows = self.conn.execute(
            "SELECT id, username, display_name, color, avatar FROM users"
            " ORDER BY display_name COLLATE NOCASE, username"
        ).fetchall()
        return [{**dict(row), "avatar": auth.avatar_of(row)} for row in rows]

    def reindex(
        self, force: bool = False, empty_hint: str | None = None, progress=None,
        source: str = "auto",
    ):
        """Reconstruye las notas del índice desde el vault (ADR D1).

        `force=True` permite que un vault ausente o vacío deje el índice a
        cero: es la salida explícita cuando el vaciado es intencionado, no el
        camino normal (ver `VaultStore.rebuild`). `empty_hint` es el texto que
        nombra esa salida en el mensaje de error, porque no es la misma con el
        servicio en marcha que durante el arranque. `progress` solo lo pasa el
        arranque en segundo plano (P16): informa del avance, no cambia nada.
        """
        return self.vault.rebuild(
            allow_empty=force,
            empty_hint=empty_hint or EMPTY_VAULT_HINT_HTTP,
            progress=progress,
            source=source,
        )

    def migrated(self) -> bool:
        """¿La verdad ya está en la base? (3.0, hito 2; ver `VaultStore.migrated`)."""
        return self.vault.migrated()

    def check_can_rebuild(
        self, *, force: bool = False, empty_hint: str | None = None
    ) -> None:
        """Lo que `reindex` comprueba antes de tocar nada, sin indexar (P16).

        El arranque lo llama en su fase rápida: un vault que no está donde
        dice (o vacío con páginas en el índice) sigue abortando el arranque en
        claro, como antes, en vez de quedarse en segundo plano como un `503`.
        """
        self.vault.check_can_rebuild(
            allow_empty=force, empty_hint=empty_hint or EMPTY_VAULT_HINT_HTTP
        )

    # ----------------------------------------------------------- workspace

    def ensure_workspace(self, name: str = "General") -> uuid.UUID:
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
                "INSERT INTO workspaces (id, name, created_at, slug) VALUES (?, ?, ?, ?)",
                (workspace_id, name, _iso(_now()), "general"),
            )
        return uuid.UUID(workspace_id)

    # -------------------------------------------------------------- pages

    def list_pages(self) -> list[Page]:
        cond, args = self._visible("p")
        rows = self.conn.execute(
            f"SELECT {PAGE_FIELDS_P} FROM pages p WHERE {cond}"
            " ORDER BY p.created_at DESC, p.rowid DESC",
            args,
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
            with self._tx():
                page_id = self.vault.create(
                    title=title,
                    parent_id=parent_id,
                    icon=icon,
                    description=description,
                    body=body_markdown or "",
                    created_by=created_by,
                )
                self._own_if_root("page", page_id, parent_id)
            return self.get_page(page_id)

        if PageType(type) == PageType.database:
            # Una base también nace con su fichero (P2): el `POST /pages` con
            # `type: database` crea la base vacía, no una página sin `databases`
            # detrás que el reindexado no podría reconstruir.
            with self._tx():
                page_id = self.vault.create_database(
                    title=title,
                    schema=[],
                    parent_id=parent_id,
                    icon=icon,
                    description=description,
                    created_by=created_by,
                )
                self._own_if_root("database", page_id, parent_id)
            return self.get_page(page_id)

        raise VaultError(
            "una fila no se crea suelta: nace en su base "
            "(POST /databases/{database_id}/rows)"
        )

    def capture_note(
        self,
        *,
        text: str,
        title: str,
        source: str,
        captured_by: Optional[str],
        folder: str,
        captured_at: Optional[str] = None,
    ) -> Page:
        """Crea una nota de captura en `folder` (la bandeja) y la devuelve.

        Va por `VaultStore.create`, que es el mismo camino que una nota normal
        desde el panel: `.md` atómico, índice en la misma transacción y la
        colisión de título resuelta con sufijo (` (2)`, ` (3)`…) por el propio
        vault. Lo único distinto es la carpeta (la bandeja no es `Notas/`) y
        tres claves de frontmatter que solo tienen sentido en una captura.
        """
        with self._tx():
            page_id = self.vault.create(
                title=title,
                body=text,
                created_by=captured_by,
                folder=folder,
                extra={
                    "captured_at": captured_at or _iso(_now()),
                    "capture_source": source,
                    "captured_by": captured_by,
                },
            )
            # La bandeja es de quien captura: su nota nace suya.
            self._own_if_root("page", page_id, None)
        page = self.get_page(page_id)
        assert page is not None
        return page

    def inbox_pages(self, folder: str) -> list[Page]:
        """Las notas que viven en `folder` (la bandeja), la más reciente primero.

        La API no devuelve `path`, así que el panel no tiene otra forma de
        saber qué está sin clasificar: esta es la lista que pinta la sección
        «Bandeja (N)». Incluye las subcarpetas, si alguien hizo una dentro de
        la bandeja: siguen siendo cosas por procesar.
        """
        prefix = folder.strip("/") + "/"
        cond, args = self._visible("p")
        rows = self.conn.execute(
            SELECT_PAGE
            + f" WHERE p.type = ? AND substr(p.path, 1, ?) = ? AND {cond}"
            " ORDER BY p.created_at DESC, p.rowid DESC",
            (PageType.note.value, len(prefix), prefix, *args),
        ).fetchall()
        return [_page(row) for row in rows]

    def update_page(
        self,
        page_id: uuid.UUID,
        *,
        title: Optional[str] = None,
        icon: Optional[str] = None,
        description: Optional[str] = None,
        body_markdown: Optional[str] = None,
        parent_id: Any = UNSET,
        expected_version: Optional[int] = None,
    ) -> Optional[Page]:
        """Actualiza solo los campos que llegan (None = no tocar), como el PATCH
        de siempre.

        `expected_version` (3.0, hito 2) es el `If-Match` de la petición: si la
        página ya va por otra versión, `VersionConflict` y no se escribe nada.
        Se comprueba aquí (aunque el cambio no cambie nada) y otra vez dentro de
        la transacción del almacén, que es la que cuenta.

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
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict(page_id, current.version, expected_version)

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

        changes["expected_version"] = expected_version
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
        cond, args = self._visible("p")
        me = str(self.user.id) if self.user is not None else ""
        rows = self.conn.execute(
            "SELECT p.id, p.title, p.type, p.icon, p.parent_id,"
            f" {acl.is_shared_sql('p')} AS shared,"
            # Plan H2: en qué sección de la barra lateral va una página de la raíz.
            " EXISTS (SELECT 1 FROM acl s WHERE s.resource_id = p.acl_source"
            "  AND s.subject_type = 'workspace') AS ws_shared,"
            " EXISTS (SELECT 1 FROM acl s WHERE s.resource_id = p.acl_source"
            "  AND s.subject_type = 'user' AND s.subject_id = ? AND s.level = 'owner') AS mine"
            f" FROM pages p WHERE p.type != ? AND {cond}"
            " ORDER BY p.title COLLATE NOCASE, p.title",
            (me, PageType.database_row.value, *args),
        ).fetchall()
        return self._build_tree(rows, sections=True)

    def guest_tree(self) -> list[dict[str, Any]]:
        """Lo que me han compartido en espacios que no son los míos (invitado).

        Cada raíz trae el espacio al que pertenece (`workspace`); lo de dentro
        sale con el permiso que ya heredaba. El superadministrador no es
        invitado de nada: lista vacía.
        """
        user = self.user
        if user is None or getattr(user, "is_admin", False):
            return []
        cond, args = acl.visible_clause(user, "p", None)
        member_sql, member_args = ws_mod.member_workspaces_sql(user.id)
        rows = self.conn.execute(
            "SELECT p.id, p.title, p.type, p.icon, p.parent_id, 0 AS shared,"
            " p.workspace_id AS workspace_id, w.name AS workspace_name"
            " FROM pages p JOIN workspaces w ON w.id = p.workspace_id"
            f" WHERE p.type != ? AND {cond} AND p.workspace_id NOT IN ({member_sql})"
            " ORDER BY p.title COLLATE NOCASE, p.title",
            (PageType.database_row.value, *args, *member_args),
        ).fetchall()
        return self._build_tree(rows, sections=False)

    def _build_tree(self, rows, *, sections: bool) -> list[dict[str, Any]]:
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
                # 3.0, hito 3: si alguien más que sus propietarios la ve. La
                # barra lateral pinta un candado en las que no (privadas).
                "shared": bool(r["shared"]),
                "children": [],
            }
            for r in rows
        }
        roots: list[dict[str, Any]] = []
        system = self.user is None or getattr(self.user, "is_admin", False)
        for r in rows:
            node = nodes[r["id"]]
            parent = r["parent_id"]
            if parent and parent in nodes:
                nodes[parent]["children"].append(node)
                continue
            if sections:
                # Compartido (con todo el espacio), Privado (mío y de nadie más),
                # «con_migo» (me lo han compartido) u «otros» (solo el superadmin).
                if r["ws_shared"]:
                    node["section"] = "shared"
                elif r["mine"]:
                    node["section"] = "private"
                else:
                    node["section"] = "others" if system else "with_me"
            elif "workspace_name" in r.keys():
                node["workspace"] = {"id": r["workspace_id"], "name": r["workspace_name"]}
            roots.append(node)
        return roots

    # -------------------------------------- Compartido / Privado / mover (H2)

    def set_section(self, page_id: uuid.UUID | str, section: str) -> None:
        """Una página de la raíz pasa a Compartido (todo el espacio) o a Privado.

        Privado: solo sus propietarios (se quitan el espacio, los grupos y las
        personas con las que estuviera compartida). Compartido: lo que tenía y,
        además, todo el espacio en edición.
        """
        row = self.conn.execute(
            "SELECT type, parent_id, workspace_id FROM pages WHERE id = ?", (str(page_id),)
        ).fetchone()
        if row is None:
            raise VaultError("la página no existe")
        if row["parent_id"] or row["type"] == "database_row":
            raise VaultError("solo una página de la raíz está en Compartido o en Privado")
        kind = "database" if row["type"] == "database" else "page"
        current = acl.entries(self.conn, page_id) if acl.declared(self.conn, page_id) else []
        keep = [e for e in current if e.subject_type != "workspace"]
        owners = [e for e in keep if e.subject_type == "user" and e.level == "owner"]
        if not owners and self.user is not None:
            keep.append(acl.Entry("user", str(self.user.id), "owner"))
        if section == "private":
            new = [e for e in keep if e.subject_type == "user" and e.level == "owner"]
        else:
            new = keep + [acl.Entry("workspace", row["workspace_id"], "edit")]
        with self._tx():
            acl.set_entries(self.conn, kind, page_id, new)
            acl.refresh_sources(self.conn)

    def move_to_workspace(
        self, page_id: uuid.UUID | str, workspace_id: str, section: str | None = None
    ) -> int:
        """Mueve una página (y todo lo que cuelga de ella) a otro espacio.

        Queda en la raíz del destino. Sus permisos propios se rehacen: se
        mantienen las personas y los grupos (los invitados siguen siéndolo), se
        quita el espacio de origen y, si va a Compartido, entra el del destino.
        """
        row = self.conn.execute(
            "SELECT type, parent_id, workspace_id FROM pages WHERE id = ?", (str(page_id),)
        ).fetchone()
        if row is None:
            raise VaultError("la página no existe")
        if row["type"] == "database_row":
            raise VaultError("una fila se mueve con su base")
        if row["workspace_id"] == workspace_id and not row["parent_id"]:
            if section:
                self.set_section(page_id, section)
            return 0
        kind = "database" if row["type"] == "database" else "page"
        with self._tx():
            was_shared = False
            if acl.declared(self.conn, page_id):
                was_shared = any(e.subject_type == "workspace" for e in acl.entries(self.conn, page_id))
            if row["parent_id"]:
                self.update_page(uuid.UUID(str(page_id)), parent_id=None)
            moved = store_mod.set_workspace_for_subtree(self.conn, page_id, workspace_id)
            current = acl.entries(self.conn, page_id) if acl.declared(self.conn, page_id) else []
            new = [e for e in current if e.subject_type != "workspace"]
            if not any(e.subject_type == "user" and e.level == "owner" for e in new) and self.user is not None:
                new.append(acl.Entry("user", str(self.user.id), "owner"))
            if (section or ("shared" if was_shared else "private")) == "shared":
                new.append(acl.Entry("workspace", workspace_id, "edit"))
            acl.set_entries(self.conn, kind, page_id, new)
            acl.refresh_sources(self.conn)
        return moved

    def backlinks(self, page_id: uuid.UUID) -> list[Page]:
        """Páginas que enlazan a esta: con un wikilink o incrustando una base.

        Desde P17.6 un bloque ```mdm-database cuenta como enlace, así que la
        página de la base lista la nota que la incrusta igual que si llevara su
        `[[título]]`. El `GROUP BY` deja una fila por página aunque la nota la
        enlace de las dos formas; el grafo, en cambio, pinta las dos aristas.
        """
        cond, args = self._visible("p")
        rows = self.conn.execute(
            f"SELECT {PAGE_FIELDS_P} FROM links l JOIN pages p ON p.id = l.from_id"
            f" WHERE l.to_id = ? AND l.kind IN (?, ?) AND {cond}"
            " GROUP BY p.id ORDER BY p.title COLLATE NOCASE",
            (str(page_id), LinkKind.wikilink.value, LinkKind.embed.value, *args),
        ).fetchall()
        return [_page(r) for r in rows]

    # ------------------------------------------------ sugerencias (P10a)

    def page_path(self, page_id: uuid.UUID | str) -> Optional[str]:
        """Ruta del `.md` de una página en el vault (o None si no está)."""
        row = self.conn.execute(
            "SELECT path FROM pages WHERE id = ?", (str(page_id),)
        ).fetchone()
        return row["path"] if row is not None else None

    def page_by_path(self, rel_path: str) -> Optional[Page]:
        """La página que vive en `rel_path` (ruta relativa al vault), o None.

        Es la puerta de `GET /agent/context?path=...`: un agente que lee el
        vault por su cuenta conoce rutas, no ids. La ruta tiene que ser la del
        índice (`Notas/Proyectos.md`) tal cual; no se adivina con prefijos,
        porque `Notas/X.md` y `X.md` son dos notas distintas si existen las dos.
        Quien llama normaliza antes (`vaultfs.safe_rel`).
        """
        row = self.conn.execute(
            SELECT_PAGE + " WHERE p.path = ?", (rel_path,)
        ).fetchone()
        return _page(row) if row else None

    def folded_titles(self, workspace_id: uuid.UUID | str) -> set[str]:
        """Títulos del workspace plegados con `fold_title`, para los wikilinks.

        `GET /agent/context` necesita decir, por cada `[[Título]]` del cuerpo,
        si apunta a una página existente. Se pliega igual que al guardar
        (`fold_title`: NFKC + `casefold`, sin quitar acentos), así que «existe»
        en el contexto y «crea un backlink» en el guardado dicen lo mismo.
        """
        return {
            fold_title(row["title"])
            for row in self.conn.execute(
                "SELECT title FROM pages WHERE workspace_id = ?",
                (str(workspace_id),),
            )
        }

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

    # -------------------------------------- sugerencias por agente (P10b)

    def unresolved_counts(self, page_id: uuid.UUID, agent: str) -> tuple[int, int]:
        """`(sin resolver en la nota, sin resolver del agente en todo el vault)`.

        Los dos topes de P10b. El primero es de la nota (da igual quién las
        proponga): es lo que el dueño ve al abrirla. El segundo es del agente:
        un bucle que reparte parches por veinte notas distintas tampoco pasa.
        «Sin resolver» incluye `pending` y `orphaned` (ver
        `suggestions.counts_unresolved`): una sugerencia sin ancla sigue
        esperando decisión y no puede esquivar el tope.
        """
        note_rel = self.page_path(page_id)
        if note_rel is None:        # pragma: no cover - ya comprobado antes
            return (0, 0)
        return suggestions.counts_unresolved(self.vault.root, note_rel, agent)

    def list_agent_suggestions(
        self, agent: str, status: Optional[str] = None
    ) -> list[tuple[str, Suggestion]]:
        """Sugerencias de un agente en todo el vault, `[(ruta_de_la_nota, sug)]`.

        La ruta de la nota sale del sitio donde está el sidecar, así que
        responde «dónde» sin una consulta al índice por sugerencia.
        """
        if status is not None and status not in suggestions.STATUSES:
            raise SuggestionError(f"status desconocido: {status!r}")
        return suggestions.list_for_agent(self.vault.root, agent, status)

    # ---------------------------------------------------------- databases

    def list_databases(self) -> list[Database]:
        cond, args = self._visible("p")
        rows = self.conn.execute(
            "SELECT d.id, d.page_id, d.schema_json FROM databases d"
            f" JOIN pages p ON p.id = d.page_id WHERE {cond} ORDER BY d.rowid",
            args,
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
        closed_rule: Optional[dict[str, Any]] = None,
        row_templates: Optional[list[dict[str, Any]]] = None,
    ) -> Database:
        """Crea `Bases/<slug>.md` (y sus vistas en el frontmatter) y la indexa.

        `closed_rule` es la regla de qué cuenta como fila cerrada, ya validada
        contra el schema (`app.closed.validate`); se escribe como bloque
        `closed:` y no como marcadores `done`. `row_templates` son las
        plantillas de fila que trae la base (ya validadas contra el schema), y
        se escriben en su frontmatter: la plantilla es parte de la base.
        """
        with self._tx():
            database_id = self.vault.create_database(
                title=title,
                schema=schema_def,
                views=views,
                parent_id=parent_id,
                icon=icon,
                created_by=created_by,
                closed=closed_rule,
                row_templates=row_templates,
            )
            self._own_if_root("database", database_id, parent_id)
        return self.get_database(uuid.UUID(database_id))

    def set_closed(
        self, database_id: uuid.UUID, rule: Optional[dict[str, Any]]
    ) -> Optional[Database]:
        """Escribe (o retira) la regla de qué cuenta como fila cerrada.

        `rule` llega ya validada (`app.closed.validate`): aquí solo se escribe.
        La escritura es **una**: el bloque `closed:` nuevo y el `schema` sin los
        marcadores `done` viejos, para que no queden dos declaraciones. Retirar
        la regla (`None`) tampoco deja marcadores detrás: quitar el `closed` de
        una base no puede volver a cerrar filas por un `done` olvidado.

        No toca ninguna fila: la regla se lee del `.md` de la base al contar, así
        que cambiar los valores recalcula contadores sin reescribir los `.md` de
        las filas.
        """
        current = self.get_database(database_id)
        if current is None:
            return None
        self.vault.save_database(
            current.page_id,
            schema=closed.without_legacy(current.schema_def),
            closed=rule,
        )
        return self.get_database(database_id)

    def set_row_templates(
        self, database_id: uuid.UUID, templates: list[dict[str, Any]]
    ) -> Optional[Database]:
        """Reescribe las plantillas de fila de la base en su `.md`.

        `templates` llega ya validado contra el schema (`app.templates`): aquí
        solo se escribe. Una lista vacía retira la clave del frontmatter. No
        toca ninguna fila: las plantillas no son datos, son valores por defecto
        para el alta.
        """
        current = self.get_database(database_id)
        if current is None:
            return None
        self.vault.save_database(current.page_id, row_templates=list(templates))
        return self.get_database(database_id)

    def update_database(
        self,
        database_id: uuid.UUID,
        *,
        title: Optional[str] = None,
        icon: Optional[str] = None,
        schema_def: Optional[list[dict[str, Any]]] = None,
        title_name: Optional[str] = None,
        title_hidden: Optional[bool] = None,
    ) -> Optional[Database]:
        """Actualiza la base, migrando lo que dependa de una propiedad renombrada.

        El diálogo manda el **nombre** de cada propiedad y la clave que tenía
        (la identidad de la fila); `plan_schema` devuelve el schema final y qué
        claves cambian de nombre. Un renombrado escribe la clave nueva en
        **todas** las filas y en las vistas que la nombran (`group_by`,
        `date_property`, `start_property`, `end_property`, `filters`, `sort`,
        `visible_properties`): un rename
        que deja filas con la clave vieja es peor que no poder renombrar.

        Borrar una propiedad **no** borra los valores de las filas (el `.md` es
        del usuario): solo desaparece del schema y de las vistas que la usan.
        Las filas siguen abriendo; al volver a guardar una fila se filtran las
        claves que ya no están en el schema, como antes.
        """
        current = self.get_database(database_id)
        if current is None:
            return None

        renames: dict[str, str] = {}
        deleted: list[str] = []
        final_schema: Optional[list[dict[str, Any]]] = None
        conversiones: dict[str, tuple[str, str]] = {}
        sustituciones: dict[str, dict[str, str]] = {}
        if schema_def is not None:
            final_schema, renames, deleted = plan_schema(current.schema_def, schema_def)
            conversiones = self._person_conversions(
                current.schema_def, schema_def, final_schema
            )
            # Validar **antes de la primera escritura**: la conversión puede
            # rechazar el guardado entero (un valor que no casa con ningún
            # usuario), y entonces ni las filas ni el schema se tocan. Si se
            # comprobara después del renombrado, el `.md` quedaría con las
            # claves nuevas mientras el schema sigue con las viejas.
            if conversiones:
                sustituciones = self._plan_person_conversion(database_id, conversiones)

        if renames:
            # Las filas primero: si una no se puede migrar, el `.md` de la base
            # todavía dice el schema viejo y el renombrado se puede reintentar
            # entero (el segundo intento no encuentra nada que cambiar en las
            # filas ya migradas). El vault puede ajustar la clave de destino
            # (una clave que solo sobrevive como valor huérfano de una
            # propiedad borrada no se pisa): el mapa que vuelve es el de verdad,
            # y es el que hay que escribir en el schema y en las vistas.
            ajustados = self.vault.rename_row_properties(
                database_id,
                renames,
                names={
                    str(p["key"]): str(p.get("name") or "")
                    for p in current.schema_def
                    if isinstance(p, dict) and p.get("key")
                },
                reserved={
                    str(p["key"])
                    for p in list(current.schema_def or []) + list(final_schema or [])
                    if isinstance(p, dict) and p.get("key")
                }
                # El destino de cada renombrado no está «ocupado por otra»: es
                # la clave que se está poniendo ahora.
                - set(renames.values()),
            )
            if ajustados != renames:
                traducir = {nuevo: ajustados[viejo] for viejo, nuevo in renames.items()}
                final_schema = [
                    {**p, "key": traducir.get(p.get("key"), p.get("key"))}
                    for p in (final_schema or [])
                ]
                sustituciones = {
                    traducir.get(clave, clave): valores
                    for clave, valores in sustituciones.items()
                }
            renames = ajustados

        if sustituciones:
            # El `.md` de cada fila pasa a guardar el id del usuario **antes**
            # de que el schema diga `person`, y con la clave ya renombrada (la
            # conversión se validó leyendo el `.md` con la clave vieja).
            self.vault.rewrite_property_values(database_id, sustituciones)

        views: Optional[list[dict[str, Any]]] = None
        if renames or deleted:
            views = self._views_with_keys(
                database_id, {**renames, **{key: None for key in deleted}}
            )

        # La regla de «cerrada» sigue a su propiedad: si se renombra, el bloque
        # `closed:` pasa a la clave nueva; si la propiedad desaparece o deja de
        # ser una selección/casilla, la base deja de declarar estado (una regla
        # que nombra una columna que ya no cierra nada mentiría en el `.md`), y
        # si cambian las opciones se quedan solo los valores que siguen
        # existiendo. Guardar el resto de la base no puede fallar por la regla:
        # aquí se ajusta en silencio, no se valida con un 400 (nadie la está
        # editando en esta petición).
        #
        # Solo se propaga la regla DECLARADA en el `.md`: un `done` viejo no se
        # materializa como `closed:` al guardar el schema. Quien quita ese
        # marcador manda `done: false` y la base tiene que dejar de cerrar
        # filas, no quedarse con la regla convertida que nadie ha escrito.
        closed_update: Any = UNSET
        if current.closed and not current.closed_from_done and schema_def is not None:
            clave = str(current.closed.get(closed.PROPERTY_KEY) or "")
            clave = renames.get(clave, clave)
            if clave in deleted:
                closed_update = None
            else:
                final = {
                    str(p.get("key")): p
                    for p in (final_schema or [])
                    if isinstance(p, dict) and p.get("key")
                }
                prop = final.get(clave)
                tipo = closed.type_of(prop) if prop else ""
                if prop is None or tipo not in closed.STATUS_TYPES:
                    closed_update = None
                elif tipo == "checkbox":
                    closed_update = {closed.PROPERTY_KEY: clave, closed.VALUES_KEY: []}
                else:
                    # La grafía manda la opción, no la regla: si «Hecho» pasa a
                    # «hecho», el valor que se guarda es el nuevo o el `.md`
                    # quedaría con un valor que la celda ya no puede tener.
                    canonica = {
                        str(o).strip().casefold(): str(o).strip()
                        for o in (prop.get("options") or [])
                        if str(o).strip()
                    }
                    valores: list[str] = []
                    for v in current.closed.get(closed.VALUES_KEY) or []:
                        real = canonica.get(str(v).strip().casefold())
                        if real and real not in valores:
                            valores.append(real)
                    closed_update = (
                        {closed.PROPERTY_KEY: clave, closed.VALUES_KEY: valores}
                        if valores
                        else None
                    )

        # Las plantillas de fila siguen al schema igual que las vistas: un
        # rename traduce sus claves, una propiedad borrada deja de aparecer y un
        # valor que el tipo nuevo ya no admite se retira. En silencio, como la
        # regla `closed`: nadie está editando la plantilla en esta petición, y
        # guardar el resto de la base no puede fallar por ella. Sin esto el `.md`
        # se queda con plantillas que mienten, que en el editor son invisibles
        # (su clave ya no está en el schema) y que el alta rechazaría sin decir
        # por qué.
        row_templates_update: Any = UNSET
        if schema_def is not None and current.row_templates:
            ajustadas = template_defs.adapt_row_templates(
                current.row_templates, final_schema or [], renames
            )
            if ajustadas != current.row_templates:
                row_templates_update = ajustadas

        self.vault.save_database(
            current.page_id,
            title=UNSET if title is None else title,
            icon=UNSET if icon is None else icon,
            schema=UNSET if final_schema is None else final_schema,
            views=UNSET if views is None else views,
            title_name=UNSET if title_name is None else title_name,
            title_hidden=UNSET if title_hidden is None else title_hidden,
            closed=closed_update,
            row_templates=row_templates_update,
        )
        if schema_def is not None:
            # El índice de una fila se filtra contra el schema de su base: si
            # el juego de claves cambia (rename, alta o borrado) hay que releer
            # los `.md`, o las filas se quedarían sin los valores de la clave
            # nueva. Cambiar solo el tipo no reindexa: el `.md` no se toca y el
            # índice sigue como estaba hasta que se guarde una fila (lo de
            # siempre). Una propiedad que se vuelve a añadir con la misma clave
            # recupera así los valores que ya estaban en el fichero.
            antes = {p.get("key") for p in current.schema_def if isinstance(p, dict)}
            despues = {p["key"] for p in final_schema or []}
            if antes != despues or conversiones:
                self.vault.reindex_rows(database_id)
        return self.get_database(database_id)

    def _person_conversions(
        self,
        current: list[dict[str, Any]],
        requested: list[dict[str, Any]],
        final_schema: list[dict[str, Any]],
    ) -> dict[str, tuple[str, str]]:
        """Claves que pasan de `text` a `person` en esta petición.

        Devuelve `{clave_final: (clave_actual, nombre visible)}`. La clave final
        es la que ha decidido `plan_schema` (puede ser un renombrado: el diálogo
        manda el nombre nuevo y la clave vieja), y la actual es la que hay que
        leer en los `.md` antes de escribir nada. El nombre va en los mensajes,
        que el usuario no ve claves.

        Solo se admite desde `text`: convertir una selección, una fecha o una
        relación a persona no es lo que pide la tarjeta y no hay valores que
        casar. Los valores se comprueban en `_plan_person_conversion`.
        """
        actuales = {
            str(prop.get("key")): prop
            for prop in current
            if isinstance(prop, dict) and prop.get("key")
        }
        conversiones: dict[str, tuple[str, str]] = {}
        for item, final in zip(requested, final_schema):
            if not isinstance(item, dict) or final.get("type") != PERSON:
                # `type` llega como `PropertyType` (un `str`-Enum): se compara
                # con el valor, no con `str(...)`, que daría «PropertyType.person».
                continue
            origen = actuales.get(str(item.get("key") or ""))
            if origen is None or origen.get("type") == PERSON:
                continue
            nombre = str(final.get("name") or origen.get("name") or final.get("key"))
            if origen.get("type") != "text":
                raise VaultError(
                    f"«{nombre}»: solo se puede convertir a Persona una propiedad de "
                    "texto. Cambia antes su tipo a Texto, o créala nueva."
                )
            conversiones[str(final["key"])] = (str(origen["key"]), nombre)
        return conversiones

    def _plan_person_conversion(
        self, database_id: uuid.UUID, conversiones: dict[str, tuple[str, str]]
    ) -> dict[str, dict[str, str]]:
        """Valida la conversión y prepara las sustituciones, **sin escribir**.

        `conversiones` va de clave final a `(clave actual, nombre visible)`.
        Se leen los `.md` con la clave actual (el renombrado aún no ha
        ocurrido) y se devuelve, por clave final, `{valor escrito: id de
        usuario}`.

        Un valor casa con un usuario por su `display_name` o por su `username`,
        sin mayúsculas (el nombre visible lo escribe una persona; el @usuario,
        no). Si un texto casara con dos usuarios distintos, gana el **nombre
        visible** y, entre iguales, el primero por nombre visible y @usuario
        (el orden de `all_users`): el casado es determinista y no se adivina
        nada más. Si **algún** valor de **alguna** fila no casa, se lanza
        `VaultError` con el nombre visible de la propiedad y los valores que
        sobran: no se convierte nada y el `.md` se queda como estaba.

        La comprobación ocurre antes de la primera escritura de
        `update_database` (renombrados incluidos), así que un rechazo no deja
        el vault a medias entre dos esquemas.
        """
        usuarios = self.all_users()
        por_nombre: dict[str, str] = {}
        # Dos pasadas para que el casado no dependa del orden: primero los
        # nombres visibles (lo que el dueño escribió a mano) y después los
        # @usuario de quien no haya casado ya por nombre.
        for campo in ("display_name", "username"):
            for usuario in usuarios:
                if usuario.get(campo):
                    por_nombre.setdefault(_plegar(usuario[campo]), str(usuario["id"]))

        crudos = self.vault.row_property_values(
            database_id, {actual: nombre for actual, nombre in conversiones.values()}
        )
        sustituciones: dict[str, dict[str, str]] = {}
        for clave, (actual, nombre) in conversiones.items():
            mapa: dict[str, str] = {}
            desconocidos: list[str] = []
            for valor in crudos.get(actual, []):
                user_id = por_nombre.get(_plegar(valor))
                if user_id is None:
                    desconocidos.append(valor)
                else:
                    mapa[_plegar(valor)] = user_id
            if desconocidos:
                lista = ", ".join(f"«{valor}»" for valor in desconocidos[:5])
                if len(desconocidos) > 5:
                    lista += f" y {len(desconocidos) - 5} más"
                raise VaultError(
                    f"«{nombre}»: {lista} no casa con ningún usuario de la app, así "
                    "que no se convierte en Persona (no se ha tocado ninguna fila). "
                    "Crea esos usuarios o corrige los valores, y vuelve a intentarlo."
                )
            sustituciones[clave] = mapa
        return sustituciones

    def _views_with_keys(
        self, database_id: uuid.UUID, mapping: dict[str, Optional[str]]
    ) -> list[dict[str, Any]]:
        """Las vistas de la base con las claves de propiedad traducidas.

        `mapping` va de clave vieja a nueva, y a `None` cuando la propiedad ha
        desaparecido: una vista que agrupa por una propiedad borrada se queda
        sin `group_by` (y sin ese filtro u orden), que es un estado que la
        interfaz ya sabe enseñar. Es la misma forma del fichero que usa
        `_views_with`, para que guardar la base y migrar las vistas sea una
        sola escritura del frontmatter.
        """
        def mapped(value: Any) -> Any:
            return mapping.get(value, value) if isinstance(value, str) else value

        views = self._views_with(database_id)
        for view in views:
            config = dict(view.get("config") or {})
            for field in ("group_by", "date_property", "start_property", "end_property"):
                if field not in config:
                    continue
                new = mapped(config[field])
                if new is None:
                    config.pop(field)
                else:
                    config[field] = new
            filters = config.get("filters")
            if isinstance(filters, list):
                kept = []
                for item in filters:
                    if isinstance(item, dict) and isinstance(item.get("key"), str):
                        new = mapped(item["key"])
                        if new is None:
                            continue  # el filtro hablaba de una propiedad borrada
                        item = {**item, "key": new}
                    kept.append(item)
                if kept:
                    config["filters"] = kept
                else:
                    config.pop("filters", None)
            # El orden admite las dos formas: una sola clave (`{key, dir}`,
            # la de las vistas antiguas) o varias en prioridad (`[{key, dir}]`,
            # la de la interfaz). Se traduce cada clave y se conserva la forma
            # que tenía el fichero: renombrar una propiedad no es motivo para
            # reescribir la vista con otro formato.
            sort = config.get("sort")
            if isinstance(sort, dict):
                claves_orden = [sort]
            elif isinstance(sort, list):
                claves_orden = sort
            else:
                claves_orden = []
            traducidas: list[Any] = []
            for item in claves_orden:
                if isinstance(item, dict) and isinstance(item.get("key"), str):
                    new = mapped(item["key"])
                    if new is None:
                        continue  # el orden hablaba de una propiedad borrada
                    item = {**item, "key": new}
                traducidas.append(item)
            if not traducidas:
                config.pop("sort", None)
            elif isinstance(sort, dict):
                config["sort"] = traducidas[0]
            else:
                config["sort"] = traducidas
            visible = config.get("visible_properties")
            if isinstance(visible, list):
                config["visible_properties"] = [
                    mapped(key) for key in visible if mapped(key) is not None
                ]
            # El ancho y el sitio de cada columna son de la vista y hablan de
            # claves de propiedad: renombrar una se lleva su `widths` y su
            # puesto en `order`, y borrarla deja las dos cosas fuera (una
            # columna que ya no existe no ocupa un hueco). El ancho de la
            # columna del título va con la clave `title`, que no es de ninguna
            # propiedad y por eso `mapped` no la toca nunca.
            widths = config.get("widths")
            if isinstance(widths, dict):
                traducidos = {
                    nuevo: ancho
                    for clave, ancho in widths.items()
                    if (nuevo := mapped(clave)) is not None
                }
                if traducidos:
                    config["widths"] = traducidos
                else:
                    config.pop("widths", None)
            order = config.get("order")
            if isinstance(order, list):
                sitios = [mapped(clave) for clave in order if isinstance(clave, str)]
                sitios = [clave for clave in sitios if clave is not None]
                if sitios:
                    config["order"] = sitios
                else:
                    config.pop("order", None)
            view["config"] = config
        return views

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

    def database_ids(self) -> set[str]:
        """Los ids de todas las bases del índice, de una vez.

        Lo usa la serialización de una base para saber si la `target_database`
        de una propiedad `relation` sigue existiendo. Es una consulta, no una
        por propiedad: la tabla tiene una fila por base del vault.
        """
        return {row["id"] for row in self.conn.execute("SELECT id FROM databases")}

    def row_refs(self, ids: Iterable[str]) -> dict[str, tuple[str, str]]:
        """`id -> (título, id de su base)` de las filas que existan.

        **Una consulta por respuesta**, no una por celda ni una por fila: es lo
        que evita el N+1 al devolver una tabla con relaciones (veinte filas con
        relación resuelven sus veinte títulos en un solo `IN (...)`). Los ids que
        ya no existen simplemente no aparecen en el mapa, y quien serializa los
        pinta con `title: null` (fila borrada).

        Se piden a `rows` (y no a `pages`) a propósito: solo una fila de una base
        puede ser el destino de una relación, y así una página suelta con el
        mismo id no se colaría como si fuera una fila.
        """
        wanted = [str(one) for one in ids]
        if not wanted:
            return {}
        refs: dict[str, tuple[str, str]] = {}
        for start in range(0, len(wanted), _REFS_CHUNK):
            chunk = wanted[start:start + _REFS_CHUNK]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                "SELECT p.id AS id, p.title AS title, r.database_id AS database_id"
                " FROM rows r JOIN pages p ON p.id = r.id"
                f" WHERE r.id IN ({marks})",
                chunk,
            ).fetchall()
            for row in rows:
                refs[row["id"]] = (row["title"], row["database_id"])
        return refs

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
        body: str = "",
        created_by: Optional[str] = None,
    ) -> Page:
        """Crea la fila. `body` es su cuerpo markdown inicial (plantilla de fila)."""
        row_id = self.vault.create_row(
            database_id, title=title, properties=properties or {}, icon=icon,
            body=body, created_by=created_by,
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
        body_markdown: Any = UNSET,
        expected_version: Optional[int] = None,
    ) -> Optional[Page]:
        """Escribe la fila (frontmatter y/o cuerpo) y reindexa el MISMO `.md`.

        `body_markdown` es `UNSET` cuando el PATCH no manda cuerpo: guardar una
        celda no puede borrar lo que el usuario tenga escrito en la fila. `""`
        sí vacía el cuerpo. La ruta no cambia (el fichero se llama por el `id`
        de la fila), así que renombrar tampoco mueve nada.
        """
        current = self.get_row(database_id, row_id)
        if current is None:
            return None
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict(row_id, current.version, expected_version)
        merged: Optional[dict[str, Any]] = None
        if properties is not None:
            # Solo lo que cambia llega al escritor: el PATCH de una celda escribe
            # esa clave y **nada más** (B2). Antes se mezclaba el mapa completo
            # que había en el índice (`{**current.properties, **properties}`) y
            # reescribía todas las propiedades del fichero, así que un valor que
            # el índice no supo leer —`valor: A-12` en una propiedad `number`
            # queda como `null`— desaparecía del `.md` al guardar cualquier otra
            # celda. El merge con lo que dice el fichero lo hace `fm.rewrite`,
            # que sustituye la línea de esa clave y deja el resto byte a byte.
            # `None` sigue significando «borra esa clave», como antes.
            merged = dict(properties)
        if merged is None and title is None and icon is None and body_markdown is UNSET:
            return current
        self.vault.save_row(
            row_id,
            title=UNSET if title is None else title,
            icon=UNSET if icon is None else icon,
            properties=UNSET if merged is None else merged,
            body=body_markdown,
            expected_version=expected_version,
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
        cond, args = self._visible("p")
        rows = self.conn.execute(
            f"SELECT {PAGE_FIELDS_P} FROM pages_fts JOIN pages p"
            " ON p.rowid = pages_fts.rowid"
            f" WHERE pages_fts MATCH ? AND {cond}"
            " ORDER BY bm25(pages_fts), p.title COLLATE NOCASE LIMIT ?",
            (query, *args, limit),
        ).fetchall()
        return [_page(r) for r in rows]

    # ---------------------------------------------------------- dashboard

    def list_recent_pages(self, limit: int) -> list[Page]:
        cond, args = self._visible("p")
        rows = self.conn.execute(
            f"SELECT {PAGE_FIELDS_P} FROM pages p WHERE p.type != ? AND {cond}"
            " ORDER BY p.updated_at DESC, p.rowid DESC LIMIT ?",
            (PageType.database_row.value, *args, limit),
        ).fetchall()
        return [_page(r) for r in rows]
