"""Puente entre el vault (ficheros `.md`) y el índice SQLite, para notas.

Contrato que implementa (ADR-0001 y ROADMAP §2):

- Guardar una nota = escribir el `.md` y, en la misma operación, actualizar el
  índice con lo que dice el fichero. **El fichero manda**: si el proceso muere
  entre las dos cosas, el índice se corrige en el siguiente arranque o con
  `POST /reindex`.
- Renombrar = renombrar el fichero (el título ES el nombre) y, si tiene
  subpáginas, su carpeta. Mover de carpeta = mover el fichero (y su carpeta).
  El `id` del frontmatter no se toca nunca: es la identidad.
- Borrar = mover a `.trash/<fecha UTC>/<ruta original>`, con la carpeta de
  subpáginas incluida (si no, quedarían ficheros huérfanos que el reindexado
  leería como notas sueltas, justo lo que la cascada del índice ya no haría).
- Reindexar = leer el vault y reescribir las filas de tipo `note` del índice en
  una sola transacción. El resto de filas (`database`, `database_row`) no se
  tocan: en P1.2a las bases y sus filas todavía viven solo en el índice.

Un fichero sin `id` en el frontmatter (creado a mano) recibe un UUID derivado
de su ruta, determinista entre reindexados; al guardarlo desde la app se
materializa ese mismo `id` en el frontmatter, así que a partir de ahí sobrevive
a renombrados y movimientos.

Claves de frontmatter que gestiona la app además de las del ADR (`id`, `icon`,
`description`, `created`, `updated`): `created_by` y `header_image`. Sin ellas,
un reindexado perdería el autor y la cabecera, que hoy la API devuelve; el ADR
permite claves extra y se preservan al reescribir.
"""

from __future__ import annotations

import contextlib
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
class _NoteRecord:
    id: str
    workspace_id: str
    parent_id: Optional[str]
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
            self.id, self.workspace_id, self.parent_id, "note", self.title,
            self.icon, self.description, self.created_by, self.header_image,
            self.body, self.path, self.mtime, self.size, self.content_hash,
            self.created_at, self.updated_at,
        )


NOTE_COLUMNS = (
    "id, workspace_id, parent_id, type, title, icon, description, created_by,"
    " header_image_path, body, path, mtime, size, content_hash, created_at,"
    " updated_at"
)

INSERT_NOTE = f"INSERT INTO pages ({NOTE_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"

UPSERT_NOTE = INSERT_NOTE + """
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
    skipped: int = 0
    links: int = 0
    seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "notes": self.notes,
            "skipped_files": self.skipped,
            "links": self.links,
            "seconds": round(self.seconds, 3),
            "warnings": len(self.warnings),
            "warning_sample": self.warnings[:20],
        }


# --------------------------------------------------------------------------
# el puente
# --------------------------------------------------------------------------


class VaultNotes:
    """Operaciones de notas sobre el vault, con el índice al día."""

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
        """Crea `Notas/`, `_attachments/` y `.trash/` si faltan."""
        for rel in (NOTES_DIR, "_attachments", ".trash"):
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
                f"la página {page_id} es de tipo {row['type']}: en P1.2a solo las "
                "notas viven en ficheros (las bases y sus filas son P2)"
            )
        if not row["path"]:
            raise VaultError(f"la nota {page_id} no tiene ruta en el índice")
        return row

    def _folder_for_parent(self, parent_id: Any) -> str:
        """Carpeta donde vive una nota cuyo padre es `parent_id`.

        La jerarquía son carpetas (D4): `Notas/Proyectos.md` tiene sus hijas en
        `Notas/Proyectos/`. Un padre que no es nota no tiene carpeta propia en
        P1.2a: devolver una ruta inventada dejaría una nota que el reindexado
        no podría volver a colgar de su padre.
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
                "en P1.2a una nota solo puede colgar de otra nota: las bases de "
                "datos y sus filas todavía viven solo en el índice (P2)"
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
        page_id: Optional[str] = None,
    ) -> _NoteRecord:
        body = canonical_body(page.body)
        created = _parse_iso(page.frontmatter.get("created")) or _from_mtime(page.mtime)
        updated = _parse_iso(page.frontmatter.get("updated")) or created
        return _NoteRecord(
            id=page_id or _as_uuid(page.id) or derived_id(page.path),
            workspace_id=workspace_id,
            parent_id=parent_id,
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

    def _upsert(self, record: _NoteRecord) -> None:
        try:
            self.conn.execute(UPSERT_NOTE, record.values())
        except sqlite3.IntegrityError:
            # Fila huérfana con la misma ruta (fichero borrado por fuera y aún
            # sin reindexar): la del fichero manda, se aparta la otra.
            self.conn.execute(
                "DELETE FROM pages WHERE path = ? AND id <> ?",
                (record.path, record.id),
            )
            self.conn.execute(UPSERT_NOTE, record.values())

    def _index_file(self, rel: str, parent_id: Optional[str]) -> str:
        """Lee el fichero recién escrito y deja el índice diciendo lo mismo."""
        abs_path = os.path.join(self.root, vaultfs.safe_rel(self.root, rel))
        page = reader.read_file(abs_path, rel)
        workspace_id = self._workspace()
        record = self._record_from_page(page, workspace_id, parent_id)
        self._upsert(record)
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
        size = len(old_prefix)
        self.conn.execute(
            "UPDATE pages SET path = ? || substr(path, ?) WHERE substr(path, 1, ?) = ?",
            (new_prefix + "/", size + 1, size, old_prefix + "/"),
        )

    # --------------------------------------------------------------- borrar

    def delete(self, page_id: Any) -> bool:
        """Mueve la nota (y su carpeta de subpáginas) a la papelera."""
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

    def list_trash(self) -> list[str]:
        """Rutas (relativas al vault) de lo que hay en `.trash/`."""
        trash = os.path.join(self.root, ".trash")
        found: list[str] = []
        for dirpath, _dirs, names in os.walk(trash):
            for name in names:
                full = os.path.join(dirpath, name)
                found.append(os.path.relpath(full, self.root).replace(os.sep, "/"))
        return sorted(found)

    def restore(self, trash_rel: str) -> str:
        """Devuelve un fichero de la papelera a su sitio. Devuelve su ruta."""
        dst = vaultfs.restore(self.root, trash_rel)
        return os.path.relpath(dst, self.root).replace(os.sep, "/")

    # ------------------------------------------------------------ reindexar

    def _refuse_suspicious_vault(
        self, hint: str = EMPTY_VAULT_HINT_HTTP
    ) -> None:
        """No perder notas del índice porque el vault no esté donde dice.

        `hint` nombra la salida que tiene el llamador (HTTP o arranque): el
        mensaje no debe recomendar un endpoint que no puede servir.
        """
        index_notes = self.conn.execute(
            "SELECT count(*) FROM pages WHERE type = 'note'"
        ).fetchone()[0]
        if index_notes == 0:
            return
        motivo = None
        if not os.path.isdir(self.root):
            motivo = f"la raíz del vault {self.root!r} no existe"
        elif not _has_markdown(self.root):
            motivo = f"el vault {self.root!r} no tiene ningún .md"
        if motivo is None:
            return
        raise VaultError(
            f"{motivo} y el índice tiene {index_notes} notas: un volumen sin "
            "montar (o un VAULT_PATH equivocado) no se puede leer como un vault "
            "vacío, y reindexar así borraría las notas del índice. "
            f"{hint}"
        )

    def rebuild(
        self, *, allow_empty: bool = False, empty_hint: str = EMPTY_VAULT_HINT_HTTP
    ) -> RebuildStats:
        """Reconstruye del vault todas las filas de notas del índice.

        Con `allow_empty=False` (lo normal) se niega a vaciar el índice cuando
        el vault no existe o no tiene ni un `.md`. La salida explícita depende
        del llamador y la nombra `empty_hint`: `POST /reindex?force=true` con el
        servicio vivo, `VAULT_ALLOW_EMPTY=1` en el arranque.

        Con `allow_empty=True` las filas `note` se van (el vault manda), pero
        las bases, filas y vistas del índice no se borran: quedan sin padre
        hasta P2, que les dará fichero.
        """
        started = time.perf_counter()
        stats = RebuildStats()
        if not allow_empty:
            self._refuse_suspicious_vault(empty_hint)
        vault = reader.load(self.root)
        stats.warnings.extend(vault.warnings)

        notes: list[reader.Page] = []
        for page in vault.pages.values():
            if page.type == "note":
                notes.append(page)
            else:
                stats.skipped += 1
                stats.warnings.append(
                    f"{page.path}: type '{page.type}' no se indexa todavía "
                    "(bases y filas son P2)"
                )
        # Padres antes que hijas: la FK `parent_id` es inmediata.
        notes.sort(key=lambda p: (p.path.count("/"), p.path))

        ids_by_path: dict[str, str] = {}
        for page in notes:
            ids_by_path[page.path] = _as_uuid(page.id) or derived_id(page.path)

        def parent_of(page: reader.Page) -> Optional[str]:
            folder = os.path.dirname(page.path)
            if not folder:
                return None
            return ids_by_path.get(f"{folder}.md")

        for page in notes:
            if _as_uuid(page.id) is None:
                stats.warnings.append(
                    f"{page.path}: sin 'id' utilizable en el frontmatter; id derivado "
                    "de la ruta (se materializa al guardarla)"
                )
            folder = os.path.dirname(page.path)
            if folder and folder != NOTES_DIR and not folder.startswith(NOTES_DIR + "/"):
                stats.warnings.append(
                    f"{page.path}: está fuera de {NOTES_DIR}/ (se indexa igual: el "
                    "fichero manda)"
                )

        workspace_id = self._workspace()
        records = [
            self._record_from_page(page, workspace_id, parent_of(page), ids_by_path[page.path])
            for page in notes
        ]

        with self._tx():
            # Las bases y sus filas no se reindexan (P2): se apartan de la
            # cascada mientras se reconstruyen las notas y se vuelven a colgar
            # de su padre si ese padre sigue existiendo.
            orphan_parents = [
                (r["id"], r["parent_id"])
                for r in self.conn.execute(
                    "SELECT id, parent_id FROM pages WHERE type <> 'note'"
                )
            ]
            self.conn.execute("UPDATE pages SET parent_id = NULL WHERE type <> 'note'")
            self.conn.execute("DELETE FROM pages WHERE type = 'note'")

            self.conn.executemany(UPSERT_NOTE, [r.values() for r in records])
            # FTS5 con `content='pages'`: se regenera desde la tabla de
            # contenido para que dos reindexados den el mismo índice.
            self.conn.execute("INSERT INTO pages_fts(pages_fts) VALUES('rebuild')")

            note_ids = {r.id for r in records}
            for page_id, parent_id in orphan_parents:
                if parent_id and parent_id in note_ids:
                    self.conn.execute(
                        "UPDATE pages SET parent_id = ? WHERE id = ?",
                        (parent_id, page_id),
                    )

            # Enlaces: se recalculan enteros desde los cuerpos (los de las
            # notas vienen del fichero; los de una base, de su fila).
            self.conn.execute("DELETE FROM links")
            for record in records:
                sync_wikilinks(self.conn, record.id, workspace_id, record.body)
            for row in self.conn.execute(
                "SELECT id, workspace_id, body FROM pages WHERE type <> 'note'"
            ).fetchall():
                sync_wikilinks(self.conn, row["id"], row["workspace_id"], row["body"])

        stats.notes = len(records)
        stats.links = self.conn.execute("SELECT count(*) FROM links").fetchone()[0]
        stats.seconds = time.perf_counter() - started
        return stats
