"""Puente entre el vault (ficheros `.md`) y el índice SQLite.

Contrato que implementa (ADR-0001 y ROADMAP §2):

- Guardar una página = escribir el `.md` y, en la misma operación, actualizar el
  índice con lo que dice el fichero. **El fichero manda**: si el proceso muere
  entre las dos cosas, el índice se corrige en el siguiente arranque o con
  `POST /reindex`.
- Renombrar = renombrar el fichero (el título ES el nombre) y, si tiene
  subpáginas, su carpeta. Mover de carpeta = mover el fichero (y su carpeta).
  El `id` del frontmatter no se toca nunca: es la identidad.
- Borrar = mover a `.trash/<fecha UTC>/<ruta original>`, con la carpeta de
  subpáginas incluida (si no, quedarían ficheros huérfanos que el reindexado
  leería como notas sueltas, justo lo que la cascada del índice ya no haría).
- Reindexar = leer el vault y reescribir el índice entero en una sola
  transacción: notas, bases de datos, sus filas y sus vistas.

Desde P2 las **bases de datos** también son ficheros: `Bases/<slug>.md` con
`type: database` y el `schema` y las `views` en el frontmatter, y sus filas en
`Bases/<slug>/<fila>.md`, con las propiedades en el frontmatter y sin `title`
(el título de una fila es el nombre del fichero). Una base que cuelga de una
nota vive en la carpeta de hijas de esa nota, porque la jerarquía son carpetas
(D4) y así el reindexado puede volver a deducir su `parent_id`. El `id` de la
base es el suyo y el de su página a la vez: un fichero, una identidad.

Un fichero sin `id` en el frontmatter (creado a mano) recibe un UUID derivado
de su ruta, determinista entre reindexados; al guardarlo desde la app se
materializa ese mismo `id` en el frontmatter, así que a partir de ahí sobrevive
a renombrados y movimientos.

Claves de frontmatter que gestiona la app además de las del ADR (`id`, `icon`,
`description`, `created`, `updated`): `created_by` y `header_image`, y en las
filas `created`/`updated`. Sin ellas, un reindexado perdería el autor, la
cabecera y las fechas, que hoy la API devuelve; el ADR permite claves extra y
se preservan al reescribir.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from app import index as index_mod
from app.links import sync_wikilinks
from app.vault import fm, names, reader, vaultfs, writer

log = logging.getLogger("mdm.vault")

# Carpeta de las páginas dentro del vault (ADR: `Notas/...`).
NOTES_DIR = "Notas"

# Carpeta de las bases de datos que no cuelgan de una nota (ADR:
# `Bases/Contactos.md`). Las filas viven en la carpeta hermana del fichero de su
# base, así que se mueven con ella.
DATABASES_DIR = "Bases"

# Namespace de los ids derivados de la ruta: ficheros sin `id` en el
# frontmatter. Fijo para que dos reindexados den el mismo id.
_ID_NAMESPACE = uuid.UUID("0b6f6b4e-6b0e-5f0a-9a43-7d1f4f6a2c11")

# Salida de la guarda «el vault está vacío pero el índice tiene notas». Depende
# del llamador: por HTTP el servicio está vivo y se puede forzar con
# `?force=true`; en el arranque no (el endpoint es el servicio que no arranca),
# y la salida es una variable de entorno.
EMPTY_VAULT_HINT_HTTP = (
    "Corrige la ruta y reinténtalo, o pide POST /reindex?force=true si de verdad "
    "has vaciado el vault a mano."
)
EMPTY_VAULT_HINT_STARTUP = (
    "Corrige VAULT_PATH y vuelve a arrancar; si el vault vacío es intencionado, "
    "arranca una vez con VAULT_ALLOW_EMPTY=1: el índice se queda con 0 notas y "
    "conserva las bases, filas y vistas (que quedan sin padre). Quita la "
    "variable después."
)


class _Unset:
    """Centinela para distinguir «no lo toques» de «ponlo a NULL»."""

    def __repr__(self) -> str:  # pragma: no cover - solo para depurar
        return "UNSET"


UNSET = _Unset()


class VaultError(ValueError):
    """Una operación del vault que no se puede hacer (ruta, padre, etc.)."""


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------


def now_iso() -> str:
    """Instante actual en ISO 8601 con offset UTC (formato del ADR)."""
    return datetime.now(timezone.utc).isoformat()


def canonical_body(raw: str) -> str:
    """Cuerpo canónico: sin el salto separador ni los finales de línea.

    Al escribir se pone un `\\n` entre el `---` que cierra el frontmatter y el
    cuerpo, y otro al final; al leer se quitan para que el `body_markdown` que
    devuelve la API sea exactamente el que se guardó (si no, el índice escrito
    por la API y el reconstruido desde el fichero no coincidirían).
    """
    if raw.startswith("\n"):
        raw = raw[1:]
    return raw.rstrip("\n")


def stored_body(raw: str) -> str:
    """Cuerpo tal y como queda escrito en el fichero.

    Delega en `writer.body_block`, el mismo criterio que usa
    `writer.page_markdown` al crear: una línea en blanco tras el `---` de
    cierre (solo una, aunque el cuerpo ya empiece por salto) y un `\n` final.
    Si no, el mismo documento cambiaría de bytes según se hubiera creado o
    editado, y habría dos escritores con dos formatos para un solo contrato.
    """
    return writer.body_block(canonical_body(raw))


def derived_id(rel_path: str) -> str:
    """UUID estable a partir de la ruta (fichero sin `id` en frontmatter)."""
    key = "mdm-vault:" + rel_path.replace(os.sep, "/")
    return str(uuid.uuid5(_ID_NAMESPACE, key))


def _as_uuid(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError):
        return None


def _dump(value: Any) -> str:
    """JSON canónico de una columna `*_json` del índice.

    `sort_keys` para que la misma estructura dé la misma cadena venga del
    orden que venga: la huella del índice compara filas, no diccionarios.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: Optional[str], default: Any) -> Any:
    return json.loads(value) if value else default


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _from_mtime(mtime: float) -> datetime:
    return datetime.fromtimestamp(mtime, tz=timezone.utc)


def _read_text(abs_path: str) -> tuple[str, list[str]]:
    """Lee un `.md` con la misma tolerancia que el reindexado.

    Si se va a reescribir un fichero con CRLF o BOM y no se normaliza aquí, el
    resultado mezcla saltos de línea y el YAML queda con `\\r` dentro de cada
    línea. Latin-1 no puede tumbar el guardado: se lee con reemplazo y se avisa
    (el reindexado ya lo hace así; el guardado no podía ser más frágil).
    """
    warnings: list[str] = []
    with open(abs_path, "r", encoding="utf-8", newline="") as fh:
        try:
            text = fh.read()
        except UnicodeDecodeError:
            warnings.append("no es UTF-8 válido: leído con reemplazo (U+FFFD)")
            with open(abs_path, "r", encoding="utf-8", errors="replace", newline="") as fh2:
                text = fh2.read()
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        warnings.append("saltos CRLF normalizados a \\n")
    if text.startswith("\ufeff"):
        text = text[1:]
        warnings.append("BOM UTF-8 eliminado")
    return text, warnings


def _prune_empty_dirs(abs_dir: str) -> None:
    """Borra los directorios vacíos que deja un borrado, de abajo arriba."""
    if not os.path.isdir(abs_dir):
        return
    for dirpath, dirnames, filenames in os.walk(abs_dir, topdown=False):
        if filenames:
            continue
        for name in dirnames:
            sub = os.path.join(dirpath, name)
            if os.path.isdir(sub) and not os.listdir(sub):
                os.rmdir(sub)
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


# --------------------------------------------------------------------------
# escritura en el vault (sin tocar el índice)
# --------------------------------------------------------------------------


def write_note_file(
    root: str,
    *,
    folder: str,
    title: str,
    page_id: str,
    icon: Optional[str] = None,
    description: Optional[str] = None,
    body: str = "",
    created: Optional[str] = None,
    updated: Optional[str] = None,
    created_by: Optional[str] = None,
    header_image: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
    stem: Optional[str] = None,
) -> tuple[str, list[str]]:
    """Escribe un `.md` nuevo. Devuelve (ruta relativa al vault, avisos).

    No toca el índice a propósito: lo usa también el script de migración, que
    escribe el vault entero y deja que el reindexado pueble el índice.

    `stem` fija el nombre del fichero sin pasar por `unique_stem`: lo usa la
    migración, que resuelve antes los nombres de todo el árbol (si no, la
    carpeta de las hijas se calculaba con el título del padre y no con el
    nombre realmente escrito).
    """
    rel_folder = vaultfs.safe_rel(root, folder) if folder else ""
    directory = os.path.join(root, rel_folder) if rel_folder else root
    os.makedirs(directory, exist_ok=True)

    notes: list[str] = []
    if stem is None:
        stem, san_notes = names.sanitize(title)
        notes.extend(san_notes)
        stem, collided = names.unique_stem(directory, stem)
        if collided:
            notes.append(f"colisión en {rel_folder or '.'}: nombre final {stem!r}")
    else:
        stem = names.nfc(stem)

    rel = f"{rel_folder}/{stem}.md" if rel_folder else f"{stem}.md"
    extra_keys = dict(extra or {})
    if created_by:
        extra_keys.setdefault("created_by", created_by)
    if header_image:
        extra_keys.setdefault("header_image", header_image)

    text = writer.page_markdown(
        id_=page_id,
        icon=icon,
        description=description,
        body=canonical_body(body),
        created=created,
        updated=updated,
        extra=extra_keys,
    )
    vaultfs.atomic_write(os.path.join(root, rel), text)
    return rel, notes


# --------------------------------------------------------------------------
# registro de una nota en el índice
# --------------------------------------------------------------------------


@dataclass
class _PageRecord:
    """Una página de cualquier tipo, con lo que guarda el índice de ella.

    Es la misma forma para notas, bases y filas: el esquema de `pages` no
    distingue tipos, y lo que cambia (schema/views de una base, propiedades y
    `database_id` de una fila) va en sus tablas (`databases`, `views`, `rows`).
    """

    id: str
    workspace_id: str
    parent_id: Optional[str]
    type: str
    title: str
    icon: Optional[str]
    description: Optional[str]
    created_by: Optional[str]
    header_image: Optional[str]
    body: str
    path: str
    mtime: float
    size: int
    content_hash: str
    created_at: str
    updated_at: str

    def values(self) -> tuple:
        return (
            self.id, self.workspace_id, self.parent_id, self.type, self.title,
            self.icon, self.description, self.created_by, self.header_image,
            self.body, self.path, self.mtime, self.size, self.content_hash,
            self.created_at, self.updated_at,
        )


PAGE_COLUMNS = (
    "id, workspace_id, parent_id, type, title, icon, description, created_by,"
    " header_image_path, body, path, mtime, size, content_hash, created_at,"
    " updated_at"
)

INSERT_PAGE = f"INSERT INTO pages ({PAGE_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"

UPSERT_PAGE = INSERT_PAGE + """
ON CONFLICT(id) DO UPDATE SET
    workspace_id = excluded.workspace_id,
    parent_id = excluded.parent_id,
    type = excluded.type,
    title = excluded.title,
    icon = excluded.icon,
    description = excluded.description,
    created_by = excluded.created_by,
    header_image_path = excluded.header_image_path,
    body = excluded.body,
    path = excluded.path,
    mtime = excluded.mtime,
    size = excluded.size,
    content_hash = excluded.content_hash,
    created_at = excluded.created_at,
    updated_at = excluded.updated_at
"""


def _has_markdown(root: str) -> bool:
    """¿Hay algún `.md` en el vault? (la papelera cuenta: son ficheros del usuario)"""
    for _dirpath, _dirs, files in os.walk(root):
        if any(name.lower().endswith(".md") for name in files):
            return True
    return False


@dataclass
class RebuildStats:
    notes: int = 0
    databases: int = 0
    rows: int = 0
    views: int = 0
    skipped: int = 0
    links: int = 0
    seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    reindexed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "notes": self.notes,
            "databases": self.databases,
            "rows": self.rows,
            "views": self.views,
            "skipped_files": self.skipped,
            "links": self.links,
            "seconds": round(self.seconds, 3),
            "warnings": len(self.warnings),
            "warning_sample": self.warnings[:20],
            "reindexed_at": self.reindexed_at,
        }


@dataclass(frozen=True)
class TrashEntry:
    """Un fichero de la papelera, con la ruta de la que salió y cuándo.

    La papelera no guarda metadatos aparte (ADR-0001: el árbol de `.trash/`
    conserva la ruta original, y eso basta para restaurar). La fecha sale del
    nombre de la carpeta de borrado, así que no hay un segundo sitio que se
    pueda desincronizar.
    """

    path: str            # `.trash/<sello>/<ruta original>`, relativo al vault
    original_path: str   # `Notas/Proyectos.md`
    deleted_at: Optional[str]
    size: int

    @property
    def kind(self) -> str:
        return "note" if self.original_path.lower().endswith(".md") else "file"

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "original_path": self.original_path,
            "deleted_at": self.deleted_at,
            "size": self.size,
            "kind": self.kind,
        }


# --------------------------------------------------------------------------
# el puente
# --------------------------------------------------------------------------


class VaultStore:
    """Operaciones del vault (notas, bases y filas), con el índice al día."""

    def __init__(self, root: str, conn: sqlite3.Connection):
        self.root = os.path.abspath(root)
        self.conn = conn

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

    def ensure_structure(self) -> None:
        """Crea `Notas/`, `Bases/`, `_attachments/` y `.trash/` si faltan."""
        for rel in (NOTES_DIR, DATABASES_DIR, "_attachments", ".trash"):
            os.makedirs(os.path.join(self.root, rel), exist_ok=True)

    def _workspace(self) -> str:
        row = self.conn.execute(
            "SELECT id FROM workspaces ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is not None:
            return row["id"]
        # Id fijo: un índice reconstruido tiene que dar las mismas filas.
        workspace_id = index_mod.DEFAULT_WORKSPACE_ID
        self.conn.execute(
            "INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)",
            (workspace_id, "default", now_iso()),
        )
        return workspace_id

    def _note_row(self, page_id: Any) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM pages WHERE id = ?", (str(page_id),)
        ).fetchone()
        if row is None:
            raise VaultError(f"la página {page_id} no existe")
        if row["type"] != "note":
            raise VaultError(
                f"la página {page_id} es de tipo {row['type']}: aquí solo se "
                "guardan notas (las bases y sus filas tienen su propio camino)"
            )
        if not row["path"]:
            raise VaultError(f"la nota {page_id} no tiene ruta en el índice")
        return row

    def _folder_for_parent(self, parent_id: Any) -> str:
        """Carpeta donde vive una nota cuyo padre es `parent_id`.

        La jerarquía son carpetas (D4): `Notas/Proyectos.md` tiene sus hijas en
        `Notas/Proyectos/`. Un padre que no es nota no tiene carpeta propia:
        una nota no cuelga de una base de datos (sus filas no son páginas del
        árbol), y devolver una ruta inventada dejaría una nota que el
        reindexado no podría volver a colgar de su padre.
        """
        if parent_id is None or parent_id == "":
            return NOTES_DIR
        row = self.conn.execute(
            "SELECT type, path FROM pages WHERE id = ?", (str(parent_id),)
        ).fetchone()
        if row is None:
            raise VaultError(f"la página padre {parent_id} no existe")
        if row["type"] != "note" or not row["path"]:
            raise VaultError(
                "una nota solo puede colgar de otra nota: una base de datos y "
                "sus filas no son carpetas de notas"
            )
        return row["path"][:-3]

    def _reject_cycle(self, parent_id: Optional[str], page_id: str) -> None:
        """`VaultError` si el padre propuesto es la página o una descendiente."""
        seen: set[str] = set()
        current = parent_id
        while current:
            if current == page_id:
                raise VaultError(
                    "no se puede mover una página dentro de sí misma ni de una "
                    "de sus subpáginas: su carpeta de hijas acabaría dentro de "
                    "sí misma y el fichero dejaría de estar en el árbol"
                )
            if current in seen:
                return      # ciclo que ya venía en los datos: no lo crea esto
            seen.add(current)
            row = self.conn.execute(
                "SELECT parent_id FROM pages WHERE id = ?", (current,)
            ).fetchone()
            current = row["parent_id"] if row is not None else None

    def _record_from_page(
        self, page: reader.Page, workspace_id: str, parent_id: Optional[str],
        page_id: Optional[str] = None, page_type: Optional[str] = None,
    ) -> _PageRecord:
        body = canonical_body(page.body)
        created = _parse_iso(page.frontmatter.get("created")) or _from_mtime(page.mtime)
        updated = _parse_iso(page.frontmatter.get("updated")) or created
        return _PageRecord(
            id=page_id or _as_uuid(page.id) or derived_id(page.path),
            workspace_id=workspace_id,
            parent_id=parent_id,
            type=page_type or page.type,
            title=page.title,
            icon=page.icon,
            description=page.description,
            created_by=page.frontmatter.get("created_by") or None,
            header_image=page.frontmatter.get("header_image") or None,
            body=body,
            path=page.path,
            mtime=page.mtime,
            size=page.size,
            content_hash=page.content_hash,
            created_at=_iso(created),
            updated_at=_iso(updated),
        )

    def _upsert(self, record: _PageRecord) -> None:
        try:
            self.conn.execute(UPSERT_PAGE, record.values())
        except sqlite3.IntegrityError:
            # Fila huérfana con la misma ruta (fichero borrado por fuera y aún
            # sin reindexar): la del fichero manda, se aparta la otra.
            self.conn.execute(
                "DELETE FROM pages WHERE path = ? AND id <> ?",
                (record.path, record.id),
            )
            self.conn.execute(UPSERT_PAGE, record.values())

    def _index_database(self, page: reader.Page, record: _PageRecord) -> None:
        """Las tablas `databases` y `views` dicen lo que dice el fichero."""
        schema = reader.database_schema(page, page.path, [])
        views = reader.views_from_frontmatter(
            page.frontmatter.get("views"), record.id, page.path, []
        )
        self.conn.execute(
            "INSERT INTO databases (id, page_id, schema_json) VALUES (?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET schema_json = excluded.schema_json",
            (record.id, record.id, _dump(schema)),
        )
        self.conn.execute("DELETE FROM views WHERE database_id = ?", (record.id,))
        for view in views:
            self.conn.execute(
                "INSERT INTO views (id, database_id, name, type, config_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (view["id"], record.id, view["name"], view["type"], _dump(view["config"])),
            )

    def _index_row(self, page: reader.Page, record: _PageRecord, database_id: str) -> None:
        """La tabla `rows`: la base, las propiedades coaccionadas y la ruta."""
        db = self.conn.execute(
            "SELECT schema_json FROM databases WHERE id = ?", (database_id,)
        ).fetchone()
        schema = _load(db["schema_json"], []) if db is not None else []
        properties = reader.row_properties(schema, page.frontmatter)
        self.conn.execute(
            "INSERT INTO rows (id, database_id, properties_json, path) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET database_id = excluded.database_id,"
            " properties_json = excluded.properties_json, path = excluded.path",
            (record.id, database_id, _dump(properties), page.path),
        )

    def _database_of_row(self, rel: str) -> Optional[str]:
        """Id de la base a la que pertenece la fila en `rel`, si es una fila.

        Una fila no lleva `type` en el frontmatter (el título es el nombre del
        fichero y el tipo lo dice su sitio): es el `.md` que está dentro de la
        carpeta hermana de una base. Es la misma regla que aplica
        `reader.load`, aquí contra el índice, para indexar un fichero suelto sin
        releer el vault entero.
        """
        folder = os.path.dirname(rel)
        if not folder:
            return None
        row = self.conn.execute(
            "SELECT id FROM pages WHERE path = ? AND type = 'database'",
            (f"{folder}.md",),
        ).fetchone()
        return row["id"] if row is not None else None

    def _index_file(
        self, rel: str, parent_id: Optional[str], *, database_id: Optional[str] = None
    ) -> str:
        """Lee el fichero recién escrito y deja el índice diciendo lo mismo."""
        abs_path = os.path.join(self.root, vaultfs.safe_rel(self.root, rel))
        page = reader.read_file(abs_path, rel)
        if database_id is None:
            database_id = self._database_of_row(rel)
        workspace_id = self._workspace()
        record = self._record_from_page(
            page, workspace_id, parent_id,
            page_type="database_row" if database_id else None,
        )
        self._upsert(record)
        if page.type == "database":
            self._index_database(page, record)
        elif database_id:
            self._index_row(page, record, database_id)
        sync_wikilinks(self.conn, record.id, workspace_id, record.body)
        return record.id

    # ------------------------------------------------------------- escribir

    def create(
        self,
        *,
        title: str,
        parent_id: Optional[Any] = None,
        icon: Optional[str] = None,
        description: Optional[str] = None,
        body: str = "",
        created_by: Optional[str] = None,
        page_id: Optional[str] = None,
        created: Optional[str] = None,
        updated: Optional[str] = None,
    ) -> str:
        """Crea el `.md` y su fila en el índice. Devuelve el id de la página."""
        self.ensure_structure()
        folder = self._folder_for_parent(parent_id)
        page_id = page_id or str(uuid.uuid4())
        now = now_iso()
        rel, notes = write_note_file(
            self.root,
            folder=folder,
            title=title,
            page_id=page_id,
            icon=icon,
            description=description,
            body=body,
            created=created or now,
            updated=updated or now,
            created_by=created_by,
        )
        with self._tx():
            self._index_file(rel, self._parent_str(parent_id))
        if notes:  # colisiones y saneados: se ven en el log, no rompen nada
            log.info("nota %s: %s", rel, "; ".join(notes))
        return page_id

    def save(
        self,
        page_id: Any,
        *,
        title: Any = UNSET,
        icon: Any = UNSET,
        description: Any = UNSET,
        body: Any = UNSET,
        parent_id: Any = UNSET,
        header_image: Any = UNSET,
    ) -> None:
        """Reescribe la nota (y la renombra/mueve si toca) y actualiza el índice."""
        row = self._note_row(page_id)
        old_rel = row["path"]
        abs_old = os.path.join(self.root, vaultfs.safe_rel(self.root, old_rel))
        if not os.path.exists(abs_old):
            # El fichero se borró por fuera y el índice todavía no lo sabe: se
            # dice qué hacer en vez de reventar con un FileNotFoundError.
            raise VaultError(
                f"el fichero de la nota {page_id} no está en el vault ({old_rel}): "
                "lanza POST /reindex para reconciliar el índice con el disco"
            )
        text, _warnings = _read_text(abs_old)

        old_folder = os.path.dirname(old_rel)
        target_folder = old_folder
        new_parent = row["parent_id"]
        if parent_id is not UNSET:
            new_parent = self._parent_str(parent_id)
            if (new_parent or None) != (row["parent_id"] or None):
                # Mover una página dentro de sí misma (o de una de sus
                # subpáginas) metería su carpeta de hijas dentro de sí misma:
                # `rename()` devuelve EINVAL y el panel recibe un 500 mudo.
                self._reject_cycle(new_parent, str(row["id"]))
                target_folder = self._folder_for_parent(new_parent)

        current_stem = os.path.basename(old_rel)[:-3]
        if title is not UNSET and title is not None:
            desired_stem = names.sanitize(title)[0]
        else:
            desired_stem = current_stem

        if (
            target_folder == old_folder
            and names.nfc(desired_stem).casefold() == names.nfc(current_stem).casefold()
        ):
            # Mismo sitio y mismo nombre salvo mayúsculas/composición: no puede
            # chocar consigo mismo (el `unique_stem` vería su propio fichero).
            final_stem = desired_stem
        else:
            directory = os.path.join(self.root, target_folder) if target_folder else self.root
            final_stem, _collided = names.unique_stem(directory, desired_stem)

        new_rel = f"{target_folder}/{final_stem}.md" if target_folder else f"{final_stem}.md"

        # La carpeta de subpáginas acompaña al fichero: si no, las hijas se
        # quedarían en la ruta vieja y el reindexado las dejaría sin padre.
        old_children = old_rel[:-3]
        new_children = new_rel[:-3]
        folder_moved = new_children != old_children and os.path.isdir(
            os.path.join(self.root, old_children)
        )
        if folder_moved:
            vaultfs.move(
                os.path.join(self.root, old_children),
                os.path.join(self.root, new_children),
            )

        abs_new = os.path.join(self.root, new_rel)
        if new_rel != old_rel:
            vaultfs.move(abs_old, abs_new)

        updates: dict[str, Any] = {"id": str(row["id"]), "updated": now_iso()}
        try:
            raw, _body = fm.split_document(text)
            data = fm.parse(raw).data if raw is not None else {}
        except fm.FrontmatterError:
            data = {}
        if "created" not in data and row["created_at"]:
            updates["created"] = row["created_at"]
        if "created_by" not in data and row["created_by"]:
            updates["created_by"] = row["created_by"]
        if icon is not UNSET and icon is not None:
            updates["icon"] = icon
        if description is not UNSET and description is not None:
            updates["description"] = description
        if header_image is not UNSET:
            updates["header_image"] = header_image  # None borra la clave

        # Mismo formato que `POST` (salto tras el frontmatter y `\n` final).
        new_body = stored_body(body) if body is not UNSET and body is not None else None
        new_text, _warnings = fm.rewrite(text, updates, new_body)
        vaultfs.atomic_write(abs_new, new_text)

        with self._tx():
            self._index_file(new_rel, new_parent)
            if folder_moved:
                self._repath_children(old_children, new_children)

    def _parent_str(self, parent_id: Any) -> Optional[str]:
        if parent_id is None or parent_id == "":
            return None
        return str(parent_id)

    def _non_note_descendants(self, page_id: str) -> list[sqlite3.Row]:
        """Filas que no son nota y cuelgan (a cualquier profundidad) de `page_id`."""
        return self.conn.execute(
            "WITH RECURSIVE sub(id) AS ("
            "  SELECT id FROM pages WHERE id = ?"
            "  UNION"
            "  SELECT p.id FROM pages p JOIN sub s ON p.parent_id = s.id)"
            " SELECT p.id, p.title, p.type FROM pages p JOIN sub s ON p.id = s.id"
            " WHERE p.type <> 'note'",
            (page_id,),
        ).fetchall()

    def _repath_children(self, old_prefix: str, new_prefix: str) -> None:
        """Actualiza la ruta de las descendientes al mover/renombrar su carpeta."""
        size = len(old_prefix) + 1          # con la barra: "Notas/X/" mide eso
        self.conn.execute(
            "UPDATE pages SET path = ? || substr(path, ?) WHERE substr(path, 1, ?) = ?",
            (new_prefix + "/", size + 1, size, old_prefix + "/"),
        )

    # --------------------------------------------------------------- borrar

    def delete(self, page_id: Any) -> bool:
        """Mueve la nota (y su carpeta de subpáginas) a la papelera.

        El adjunto de la cabecera **no** se mueve ni se borra: si la nota se
        restaura, su imagen sigue donde estaba. Barrer los huérfanos que deja un
        borrado definitivo es v1.1.
        """
        row = self.conn.execute(
            "SELECT id, type, path, title FROM pages WHERE id = ?", (str(page_id),)
        ).fetchone()
        if row is None:
            return False
        if row["type"] != "note" or not row["path"]:
            # Bases y filas: siguen solo en el índice (P2), como antes.
            with self._tx():
                self.conn.execute("DELETE FROM pages WHERE id = ?", (str(page_id),))
            return True

        blockers = self._non_note_descendants(str(page_id))
        if blockers:
            # Las bases y sus filas viven solo en el índice (P2): la cascada se
            # las llevaría sin papelera, y la nota sí sería recuperable. Media
            # página restaurada es peor que un 400 con el motivo, y es simétrico
            # con la regla de que una nota no puede colgar de una base.
            detalle = ", ".join(f"«{b['title']}» ({b['type']})" for b in blockers[:3])
            if len(blockers) > 3:
                detalle += f" y {len(blockers) - 3} más"
            raise VaultError(
                f"no se puede borrar «{row['title']}»: cuelga de ella {detalle}, "
                "y en P1.2a eso solo vive en el índice (no habría papelera para "
                "recuperarlo). Bórralo primero."
            )

        rel = row["path"]
        files = [rel]
        children_folder = rel[:-3]
        abs_folder = os.path.join(self.root, children_folder)
        if os.path.isdir(abs_folder):
            for dirpath, _dirs, filenames in os.walk(abs_folder):
                for name in sorted(filenames):
                    if name.lower().endswith(".md"):
                        full = os.path.join(dirpath, name)
                        files.append(os.path.relpath(full, self.root).replace(os.sep, "/"))

        stamp = vaultfs.trash_dir_for(self.root)
        for rel_file in files:
            src = os.path.join(self.root, vaultfs.safe_rel(self.root, rel_file))
            if not os.path.exists(src):
                # Se borró por fuera y el índice aún no lo sabe: no hay nada que
                # mover, pero la fila sí se va (borrar es idempotente).
                continue
            vaultfs.move(src, os.path.join(stamp, rel_file))
        _prune_empty_dirs(abs_folder)

        with self._tx():
            self.conn.execute("DELETE FROM pages WHERE id = ?", (str(page_id),))
        return True

    def list_trash(self) -> list[TrashEntry]:
        """Lo que hay en `.trash/`, con su ruta original y la fecha del borrado.

        Lo más reciente primero. Un `.md` suelto dentro de `.trash/` (sin
        carpeta de fecha, de un `mv` a mano) no se lista: no hay ruta original
        que restaurar, y adivinarla sería inventarse el sitio del fichero.
        """
        trash = os.path.join(self.root, ".trash")
        entries: list[TrashEntry] = []
        for dirpath, dirnames, filenames in os.walk(trash):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                parts = rel.split("/")
                if len(parts) < 3:      # `.trash/<sello>/<ruta original...>`
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue            # desapareció mientras se listaba
                stamp = vaultfs.parse_trash_stamp(parts[1])
                entries.append(
                    TrashEntry(
                        path=rel,
                        original_path="/".join(parts[2:]),
                        deleted_at=_iso(stamp) if stamp else None,
                        size=size,
                    )
                )
        # Lo más reciente primero; dentro del mismo borrado, la ruta original
        # en orden (dos ordenaciones estables: la segunda manda).
        entries.sort(key=lambda e: e.path)
        entries.sort(key=lambda e: e.deleted_at or "", reverse=True)
        return entries

    def restore(self, trash_rel: str) -> str:
        """Devuelve un fichero de la papelera a su sitio. Devuelve su ruta.

        `vaultfs.restore` lanza `TrashConflict` si la ruta original está
        ocupada; aquí se deja subir, que la API lo traduzca a 409 con el motivo.
        """
        dst = vaultfs.restore(self.root, trash_rel)
        return os.path.relpath(dst, self.root).replace(os.sep, "/")

    # ------------------------------------------------------- bases de datos

    def _database_row(self, page_id: Any) -> sqlite3.Row:
        """Fila `pages` de una base de datos (su `path` es el del fichero).

        Desde P2 el `id` de la base y el de su página son el mismo: un fichero,
        una identidad. Así el frontmatter de `Bases/<slug>.md` lleva un solo
        `id` y el reindexado puede reconstruir `databases.id` y
        `databases.page_id` sin un segundo identificador escondido.
        """
        row = self.conn.execute(
            "SELECT * FROM pages WHERE id = ?", (str(page_id),)
        ).fetchone()
        if row is None:
            raise VaultError(f"la base de datos {page_id} no existe")
        if row["type"] != "database":
            raise VaultError(
                f"la página {page_id} es de tipo {row['type']}, no una base de datos"
            )
        if not row["path"]:
            raise VaultError(f"la base {page_id} no tiene ruta en el índice")
        return row

    def _database_folder(self, parent_id: Any) -> str:
        """Carpeta donde vive una base cuyo padre es `parent_id`.

        En la raíz, `Bases/` (ADR: `Bases/Contactos.md`). Colgando de una nota,
        la carpeta de hijas de esa nota: la jerarquía son carpetas (D4) y así el
        reindexado vuelve a deducir el `parent_id` de la ruta. Una base no
        cuelga de otra base: sus filas no son páginas del árbol.
        """
        if parent_id is None or parent_id == "":
            return DATABASES_DIR
        row = self.conn.execute(
            "SELECT type, path FROM pages WHERE id = ?", (str(parent_id),)
        ).fetchone()
        if row is None:
            raise VaultError(f"la página padre {parent_id} no existe")
        if row["type"] != "note" or not row["path"]:
            raise VaultError(
                "una base de datos solo puede colgar de una nota: una base "
                "dentro de otra no existe (v1.1)"
            )
        return row["path"][:-3]

    def _abs(self, rel: str) -> str:
        return os.path.join(self.root, vaultfs.safe_rel(self.root, rel))

    def create_database(
        self,
        *,
        title: str,
        schema: list[dict],
        views: Optional[list[dict]] = None,
        parent_id: Optional[Any] = None,
        icon: Optional[str] = None,
        description: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> str:
        """Escribe `Bases/<slug>.md` (y su carpeta de filas) e indexa. Devuelve su id."""
        self.ensure_structure()
        folder = self._database_folder(parent_id)
        page_id = str(uuid.uuid4())
        entries = [
            writer.view_entry({**view, "id": view.get("id") or str(uuid.uuid4())})
            for view in views or []
        ]
        page, notes = writer.create_database(
            self.root, folder, title, schema, entries,
            icon=icon, db_id=page_id, description=description, created_by=created_by,
        )
        with self._tx():
            self._index_file(page.path, self._parent_str(parent_id))
        if notes:
            log.info("base %s: %s", page.path, "; ".join(notes))
        return page_id

    def save_database(
        self,
        page_id: Any,
        *,
        title: Any = UNSET,
        icon: Any = UNSET,
        description: Any = UNSET,
        schema: Any = UNSET,
        views: Any = UNSET,
        body: Any = UNSET,
        header_image: Any = UNSET,
        parent_id: Any = UNSET,
    ) -> None:
        """Reescribe la base (y la renombra con sus filas si toca) y la indexa.

        Renombrar la base renombra su carpeta de filas: si no, las filas
        quedarían en la carpeta vieja y el reindexado ya no las vería como filas
        de esta base (`Bases/<slug>/` es lo que las define).
        """
        row = self._database_row(page_id)
        old_rel = row["path"]
        abs_old = self._abs(old_rel)
        if not os.path.exists(abs_old):
            raise VaultError(
                f"el fichero de la base {page_id} no está en el vault ({old_rel}): "
                "lanza POST /reindex para reconciliar el índice con el disco"
            )
        text, _warnings = _read_text(abs_old)

        # Mover de carpeta = mover el fichero y su carpeta de filas (D4).
        target_folder = os.path.dirname(old_rel)
        new_parent = row["parent_id"]
        if parent_id is not UNSET:
            new_parent = self._parent_str(parent_id)
            if (new_parent or None) != (row["parent_id"] or None):
                target_folder = self._database_folder(new_parent)

        current_stem = os.path.basename(old_rel)[:-3]
        if title is not UNSET and title is not None:
            desired_stem = names.sanitize(title)[0]
        else:
            desired_stem = current_stem
        if (
            target_folder == os.path.dirname(old_rel)
            and names.nfc(desired_stem).casefold() == names.nfc(current_stem).casefold()
        ):
            final_stem = desired_stem
        else:
            directory = self._abs(target_folder) if target_folder else self.root
            final_stem, _collided = names.unique_stem(directory, desired_stem)
        new_rel = f"{target_folder}/{final_stem}.md" if target_folder else f"{final_stem}.md"

        old_children, new_children = old_rel[:-3], new_rel[:-3]
        folder_moved = new_children != old_children and os.path.isdir(
            self._abs(old_children)
        )
        if folder_moved:
            vaultfs.move(self._abs(old_children), self._abs(new_children))
        if new_rel != old_rel:
            vaultfs.move(abs_old, self._abs(new_rel))

        updates: dict[str, Any] = {"id": str(row["id"]), "updated": now_iso()}
        try:
            raw, _body = fm.split_document(text)
            data = fm.parse(raw).data if raw is not None else {}
        except fm.FrontmatterError:
            data = {}
        if "created" not in data and row["created_at"]:
            updates["created"] = row["created_at"]
        if "created_by" not in data and row["created_by"]:
            updates["created_by"] = row["created_by"]
        if icon is not UNSET and icon is not None:
            updates["icon"] = icon
        if description is not UNSET and description is not None:
            updates["description"] = description
        if header_image is not UNSET:
            updates["header_image"] = header_image  # None borra la clave
        if schema is not UNSET and schema is not None:
            updates["schema"] = writer.schema_entries(schema)
        if views is not UNSET and views is not None:
            updates["views"] = [
                writer.view_entry({**view, "id": view.get("id") or str(uuid.uuid4())})
                for view in views
            ]

        new_body = stored_body(body) if body is not UNSET and body is not None else None
        new_text, _warnings = fm.rewrite(text, updates, new_body)
        vaultfs.atomic_write(self._abs(new_rel), new_text)

        with self._tx():
            self._index_file(new_rel, new_parent)
            if folder_moved:
                self._repath_rows(old_children, new_children)

    def delete_database(self, page_id: Any) -> bool:
        """Mueve la base y todas sus filas a la papelera y limpia el índice."""
        row = self.conn.execute(
            "SELECT id, type, path FROM pages WHERE id = ?", (str(page_id),)
        ).fetchone()
        if row is None or row["type"] != "database":
            return False
        rel = row["path"]
        if rel:
            files = [rel]
            folder = rel[:-3]
            if os.path.isdir(self._abs(folder)):
                for dirpath, _dirs, filenames in os.walk(self._abs(folder)):
                    for name in sorted(filenames):
                        if name.lower().endswith(".md"):
                            full = os.path.join(dirpath, name)
                            files.append(os.path.relpath(full, self.root).replace(os.sep, "/"))
            stamp = vaultfs.trash_dir_for(self.root)
            for rel_file in files:
                src = self._abs(rel_file)
                if not os.path.exists(src):
                    continue
                vaultfs.move(src, os.path.join(stamp, rel_file))
            _prune_empty_dirs(self._abs(folder))

        with self._tx():
            # Las filas son páginas, pero no cuelgan de `parent_id`: la cascada
            # del esquema no se las lleva (la FK va de `rows.id` a `pages.id`).
            self.conn.execute(
                "DELETE FROM pages WHERE id IN ("
                " SELECT r.id FROM rows r WHERE r.database_id = ?)",
                (row["id"],),
            )
            cur = self.conn.execute("DELETE FROM pages WHERE id = ?", (row["id"],))
        return cur.rowcount > 0

    def _repath_rows(self, old_prefix: str, new_prefix: str) -> None:
        """Actualiza la ruta de las filas al mover/renombrar su base."""
        size = len(old_prefix) + 1          # con la barra: "Bases/X/" mide eso
        self.conn.execute(
            "UPDATE pages SET path = ? || substr(path, ?)"
            " WHERE type = 'database_row' AND substr(path, 1, ?) = ?",
            (new_prefix + "/", size + 1, size, old_prefix + "/"),
        )
        self.conn.execute(
            "UPDATE rows SET path = ? || substr(path, ?) WHERE substr(path, 1, ?) = ?",
            (new_prefix + "/", size + 1, size, old_prefix + "/"),
        )

    # --------------------------------------------------------------- filas

    def _row_row(self, page_id: Any) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT p.*, r.database_id AS database_id FROM pages p"
            " LEFT JOIN rows r ON r.id = p.id WHERE p.id = ?",
            (str(page_id),),
        ).fetchone()
        if row is None:
            raise VaultError(f"la fila {page_id} no existe")
        if row["type"] != "database_row" or not row["path"]:
            raise VaultError(f"la página {page_id} no es una fila de una base")
        if not row["database_id"]:
            raise VaultError(
                f"la fila {page_id} no dice a qué base pertenece: lanza POST /reindex"
            )
        return row

    def create_row(
        self,
        database_id: Any,
        *,
        title: str,
        properties: Optional[dict] = None,
        icon: Optional[str] = None,
        body: str = "",
    ) -> str:
        """Escribe `Bases/<slug>/<fila>.md` e indexa. Devuelve el id de la fila."""
        db = self._database_row(database_id)
        row_id = str(uuid.uuid4())
        page, notes = writer.create_row(
            self.root, db["path"][:-3], title, properties or {},
            body=body, row_id=row_id, icon=icon,
        )
        with self._tx():
            self._index_file(page.path, None, database_id=str(db["id"]))
        if notes:
            log.info("fila %s: %s", page.path, "; ".join(notes))
        return row_id

    def save_row(
        self,
        page_id: Any,
        *,
        title: Any = UNSET,
        icon: Any = UNSET,
        description: Any = UNSET,
        body: Any = UNSET,
        properties: Any = UNSET,
        header_image: Any = UNSET,
    ) -> None:
        """Reescribe la fila (renombrándola si toca) y actualiza el índice.

        `properties` llega ya mezclada por el repositorio (PATCH parcial): un
        `None` en un valor borra esa clave del frontmatter, como en las notas.
        """
        row = self._row_row(page_id)
        old_rel = row["path"]
        abs_old = self._abs(old_rel)
        if not os.path.exists(abs_old):
            raise VaultError(
                f"el fichero de la fila {page_id} no está en el vault ({old_rel}): "
                "lanza POST /reindex para reconciliar el índice con el disco"
            )
        text, _warnings = _read_text(abs_old)

        folder = os.path.dirname(old_rel)
        current_stem = os.path.basename(old_rel)[:-3]
        if title is not UNSET and title is not None:
            desired_stem = names.sanitize(title)[0]
        else:
            desired_stem = current_stem
        if names.nfc(desired_stem).casefold() == names.nfc(current_stem).casefold():
            final_stem = desired_stem
        else:
            final_stem, _collided = names.unique_stem(self._abs(folder), desired_stem)
        new_rel = f"{folder}/{final_stem}.md"
        if new_rel != old_rel:
            vaultfs.move(abs_old, self._abs(new_rel))

        updates: dict[str, Any] = {"id": str(row["id"]), "updated": now_iso()}
        try:
            raw, _body = fm.split_document(text)
            data = fm.parse(raw).data if raw is not None else {}
        except fm.FrontmatterError:
            data = {}
        if "created" not in data and row["created_at"]:
            updates["created"] = row["created_at"]
        if icon is not UNSET and icon is not None:
            updates["icon"] = icon
        if description is not UNSET and description is not None:
            updates["description"] = description
        if header_image is not UNSET:
            updates["header_image"] = header_image  # None borra la clave
        if properties is not UNSET and properties is not None:
            # `title` no se escribe nunca: el título es el nombre del fichero.
            updates.update(writer.sin_reservadas(properties, [], "al guardar la fila"))

        new_body = stored_body(body) if body is not UNSET and body is not None else None
        new_text, _warnings = fm.rewrite(text, updates, new_body)
        vaultfs.atomic_write(self._abs(new_rel), new_text)

        with self._tx():
            self._index_file(new_rel, None, database_id=str(row["database_id"]))

    def delete_row(self, page_id: Any) -> bool:
        """Mueve la fila a la papelera y la quita del índice."""
        row = self.conn.execute(
            "SELECT id, type, path FROM pages WHERE id = ?", (str(page_id),)
        ).fetchone()
        if row is None or row["type"] != "database_row":
            return False
        if row["path"]:
            src = self._abs(row["path"])
            if os.path.exists(src):
                vaultfs.move(src, os.path.join(
                    vaultfs.trash_dir_for(self.root), row["path"]))
        with self._tx():
            cur = self.conn.execute("DELETE FROM pages WHERE id = ?", (row["id"],))
        return cur.rowcount > 0

    # ------------------------------------------------------------ reindexar

    def _refuse_suspicious_vault(
        self, hint: str = EMPTY_VAULT_HINT_HTTP
    ) -> None:
        """No perder páginas del índice porque el vault no esté donde dice.

        Desde P2 cuenta cualquier página indexada (notas, bases y filas), no
        solo las notas: reindexar contra un vault vacío se llevaría también las
        bases de datos. `hint` nombra la salida que tiene el llamador (HTTP o
        arranque): el mensaje no debe recomendar un endpoint que no puede
        servir.
        """
        indexed = self.conn.execute("SELECT count(*) FROM pages").fetchone()[0]
        if indexed == 0:
            return
        motivo = None
        if not os.path.isdir(self.root):
            motivo = f"la raíz del vault {self.root!r} no existe"
        elif not _has_markdown(self.root):
            motivo = f"el vault {self.root!r} no tiene ningún .md"
        if motivo is None:
            return
        raise VaultError(
            f"{motivo} y el índice tiene {indexed} páginas: un volumen sin "
            "montar (o un VAULT_PATH equivocado) no se puede leer como un vault "
            "vacío, y reindexar así borraría el índice. "
            f"{hint}"
        )

    def rebuild(
        self, *, allow_empty: bool = False, empty_hint: str = EMPTY_VAULT_HINT_HTTP
    ) -> RebuildStats:
        """Reconstruye el índice entero desde el vault (ADR D1).

        Notas, bases de datos, sus vistas y sus filas: todo lo que está en el
        índice sale de un `.md`, así que se puede tirar el índice sin perder
        nada. Las filas no cuelgan del árbol (`parent_id` NULL, como antes de
        P2): su sitio es la tabla de su base, y la carpeta de la base ya las
        identifica en el disco.

        Con `allow_empty=False` (lo normal) se niega a vaciar el índice cuando
        el vault no existe o no tiene ni un `.md`. La salida explícita depende
        del llamador y la nombra `empty_hint`: `POST /reindex?force=true` con el
        servicio vivo, `VAULT_ALLOW_EMPTY=1` en el arranque.
        """
        started = time.perf_counter()
        stats = RebuildStats()
        if not allow_empty:
            self._refuse_suspicious_vault(empty_hint)
        vault = reader.load(self.root)
        stats.warnings.extend(vault.warnings)

        pages = list(vault.pages.values())
        for page in pages:
            if page.type not in ("note", "database", "database_row"):
                stats.warnings.append(
                    f"{page.path}: type {page.type!r} desconocido; se indexa como nota"
                )
                page.type = "note"
        ids_by_path = {p.path: (_as_uuid(p.id) or derived_id(p.path)) for p in pages}

        def parent_of(page: reader.Page) -> Optional[str]:
            folder = os.path.dirname(page.path)
            if not folder:
                return None
            return ids_by_path.get(f"{folder}.md")

        # Padres antes que hijas: la FK `parent_id` es inmediata.
        pages.sort(key=lambda p: (p.path.count("/"), p.path))
        for page in pages:
            folder = os.path.dirname(page.path)
            if folder and not (
                folder == NOTES_DIR
                or folder.startswith(NOTES_DIR + "/")
                or folder == DATABASES_DIR
                or folder.startswith(DATABASES_DIR + "/")
            ):
                stats.warnings.append(
                    f"{page.path}: está fuera de {NOTES_DIR}/ y {DATABASES_DIR}/ "
                    "(se indexa igual: el fichero manda)"
                )
        workspace_id = self._workspace()
        records = [
            self._record_from_page(
                page, workspace_id,
                None if page.type == "database_row" else parent_of(page),
                ids_by_path[page.path],
            )
            for page in pages
        ]
        by_path = {record.path: record for record in records}

        with self._tx():
            # Todas las páginas están en ficheros: se rehace el árbol entero y
            # las cascadas del esquema se llevan bases, vistas, filas y enlaces.
            self.conn.execute("DELETE FROM pages")
            self.conn.executemany(UPSERT_PAGE, [r.values() for r in records])
            # FTS5 con `content='pages'`: se regenera desde la tabla de
            # contenido para que dos reindexados den el mismo índice.
            self.conn.execute("INSERT INTO pages_fts(pages_fts) VALUES('rebuild')")

            # Bases y filas. El `database_id` de una fila apunta al id que la
            # base tiene en el ÍNDICE (puede ser uno derivado de la ruta si el
            # fichero no traía `id`), no al que traía el lector en memoria.
            db_ids = {
                db.id: ids_by_path[db.path]
                for db in vault.databases.values()
                if db.path in ids_by_path
            }
            for db in vault.databases.values():
                record = by_path.get(db.path)
                if record is not None:
                    self._index_database(db.page, record)
            for row in vault.rows:
                record = by_path.get(row.path)
                database_id = db_ids.get(row.database_id or "")
                if record is None or database_id is None:
                    stats.warnings.append(
                        f"{row.path}: fila sin base en el índice; se queda fuera "
                        "de la tabla de su base"
                    )
                    continue
                self._index_row(row, record, database_id)
            stats.databases = len(vault.databases)
            stats.rows = sum(len(db.rows) for db in vault.databases.values())
            stats.views = self.conn.execute("SELECT count(*) FROM views").fetchone()[0]

            # Enlaces: se recalculan enteros desde los cuerpos (los de una fila
            # también: el `.md` de una fila puede llevar `[[wikilinks]]`).
            self.conn.execute("DELETE FROM links")
            for record in records:
                sync_wikilinks(self.conn, record.id, workspace_id, record.body)

            # Marca del último reindexado. `GET /status` compara el índice con
            # el disco y dice cuánto ha cambiado por fuera desde aquí.
            stats.reindexed_at = now_iso()
            index_mod.set_meta(self.conn, index_mod.META_LAST_REINDEX, stats.reindexed_at)

        stats.notes = sum(1 for record in records if record.type == "note")
        stats.links = self.conn.execute("SELECT count(*) FROM links").fetchone()[0]
        stats.seconds = time.perf_counter() - started
        return stats
