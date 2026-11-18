"""Dónde viven los `.md`: en el disco (hasta la 2.x) o en la base (3.0, hito 2).

ADR-0005: la verdad de las notas pasa a la base de datos y el Markdown queda
como formato de escritura y de exportación. El almacén (`VaultStore`) sabe
mucho de rutas —carpeta de hijas, carpeta de filas, papelera con sello,
colisiones de nombre sin distinguir mayúsculas— y todo eso es contrato del
vault que no cambia. Lo único que cambia es **dónde** está cada fichero, así
que el almacén habla con uno de estos dos objetos, que tienen los mismos verbos:

- `DiskFS`: el disco, como siempre. Lo usan los scripts de migración, la
  importación y el arranque de un vault que aún no se ha migrado.
- `DocsFS`: la tabla `documents` del índice (`path`, `text`, `mtime`,
  `size`). Cada `.md` del vault —papelera incluida— es una fila con su texto
  entero. Escribir es un `INSERT`, mover es un `UPDATE` de la ruta, y todo va
  en la misma transacción que el índice: guardar una nota ya no son dos pasos
  (fichero y luego índice) que pueden quedarse a medias.

Con `mirror=True` (`VAULT_PROJECTION=1`) `DocsFS` además **proyecta** cada
cambio al disco **después** de confirmar la transacción: el `.md` se sigue
pudiendo abrir con otro editor, y el vigilante importa lo que se toque por
fuera. La base manda: si escribir la proyección falla, se registra y se sigue.

Las rutas que reciben los métodos son ABSOLUTAS (dentro de `root`), como las
que el almacén ya construye para el disco: así el almacén no cambia de forma
de hablar, solo de interlocutor. Los adjuntos (`_attachments/`) y las
sugerencias de los agentes (`*.md.sugerencias/`) no son `.md` del vault y se
quedan siempre en disco.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from app.vault import reader, suggestions, vaultfs

log = logging.getLogger("mdm.vault")

TRASH_DIR = ".trash"


@dataclass(frozen=True)
class DocStat:
    """Lo que el recorrido del vault necesita de cada `.md` (como un `stat`)."""

    st_mtime: float
    st_size: int


# --------------------------------------------------------------------------
# disco
# --------------------------------------------------------------------------


class DiskFS:
    """El vault en disco: los verbos de siempre, sin cambios."""

    in_db = False
    mirror = False

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def isfile(self, path: str) -> bool:
        return os.path.isfile(path)

    def isdir(self, path: str) -> bool:
        return os.path.isdir(path)

    def listdir(self, path: str) -> list[str]:
        return os.listdir(path)

    def makedirs(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def read_text(self, path: str) -> tuple[str, list[str]]:
        return reader.read_disk_text(path)

    def read_page(self, path: str, rel: str) -> reader.Page:
        return reader.read_file(path, rel)

    def write(self, path: str, text: str) -> None:
        vaultfs.atomic_write(path, text)

    def move(self, src: str, dst: str) -> None:
        vaultfs.move(src, dst)

    def move_with_note(self, src_rel: str, dst_rel: str) -> bool:
        return suggestions.move_with_note(self.root, src_rel, dst_rel)

    def walk_files(self, folder: str) -> list[str]:
        """Rutas absolutas de los ficheros bajo `folder` (orden de `os.walk`)."""
        out: list[str] = []
        if not os.path.isdir(folder):
            return out
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames.sort()
            for name in sorted(filenames):
                out.append(os.path.join(dirpath, name))
        return out

    def walk_markdown(self) -> list[tuple[str, os.stat_result]]:
        return reader.walk_markdown(self.root)

    def size(self, path: str) -> int:
        return os.path.getsize(path)

    def trash_dir_for(self) -> str:
        return vaultfs.trash_dir_for(self.root)

    def restore(self, trash_rel: str) -> str:
        return vaultfs.restore(self.root, trash_rel)

    def prune_empty_dirs(self, folder: str) -> None:
        _prune_disk_dirs(folder)

    def has_markdown(self) -> bool:
        for _dirpath, _dirs, files in os.walk(self.root):
            if any(f.lower().endswith(".md") for f in files):
                return True
        return False

    # Sin transacción propia: el disco no la tiene.
    def flush(self) -> None:
        return None

    def discard(self) -> None:
        return None


def _prune_disk_dirs(abs_dir: str) -> None:
    """Borra las carpetas vacías de `abs_dir` hacia abajo (y la propia)."""
    if not os.path.isdir(abs_dir):
        return
    for dirpath, _dirnames, _filenames in os.walk(abs_dir, topdown=False):
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass


# --------------------------------------------------------------------------
# base de datos
# --------------------------------------------------------------------------


class DocsFS:
    """El vault en la tabla `documents` del índice (3.0, hito 2)."""

    in_db = True

    def __init__(self, conn: sqlite3.Connection, root: str, mirror: bool = False):
        self.conn = conn
        self.root = os.path.abspath(root)
        self.mirror = mirror
        # Cambios pendientes de proyectar al disco cuando se confirme la
        # transacción: ("write", rel, text, mtime) | ("move", src, dst) |
        # ("delete", rel).
        self._pending: list[tuple] = []

    # ----------------------------------------------------------- rutas

    def rel(self, path: str) -> str:
        """Ruta relativa (con '/') de una absoluta dentro del vault."""
        path = os.path.abspath(path)
        if path == self.root:
            return ""
        if not path.startswith(self.root + os.sep):
            raise vaultfs.PathEscape(f"ruta fuera del vault: {path!r} (vault: {self.root!r})")
        return os.path.relpath(path, self.root).replace(os.sep, "/")

    def _abs(self, rel: str) -> str:
        return os.path.join(self.root, rel.replace("/", os.sep)) if rel else self.root

    @staticmethod
    def _range(prefix: str) -> tuple[str, str]:
        """Rango de claves de los documentos bajo la carpeta `prefix`.

        '/' es U+002F y '0' es U+0030: `prefix/` <= ruta < `prefix0` son
        exactamente las rutas que empiezan por `prefix/`, y la consulta usa la
        clave primaria (sin `LIKE`, que no usaría el índice).
        """
        return prefix + "/", prefix + "0"

    def _under(self, rel_dir: str) -> list[str]:
        if not rel_dir:
            return [r[0] for r in self.conn.execute("SELECT path FROM documents ORDER BY path")]
        lo, hi = self._range(rel_dir)
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT path FROM documents WHERE path >= ? AND path < ? ORDER BY path", (lo, hi)
            )
        ]

    # ----------------------------------------------------------- lectura

    def isfile(self, path: str) -> bool:
        return self._row(self.rel(path)) is not None

    def isdir(self, path: str) -> bool:
        rel = self.rel(path)
        if rel == "":
            return True
        lo, hi = self._range(rel)
        return (
            self.conn.execute(
                "SELECT 1 FROM documents WHERE path >= ? AND path < ? LIMIT 1", (lo, hi)
            ).fetchone()
            is not None
        )

    def exists(self, path: str) -> bool:
        return self.isfile(path) or self.isdir(path)

    def listdir(self, path: str) -> list[str]:
        rel = self.rel(path)
        start = len(rel) + 1 if rel else 0
        names: set[str] = set()
        for p in self._under(rel):
            names.add(p[start:].split("/", 1)[0])
        if self.mirror:
            # Con proyección, un nombre ocupado en el disco por un fichero que
            # la base aún no conoce (puesto a mano, sin importar) también está
            # ocupado: proyectar encima lo pisaría.
            try:
                names.update(os.listdir(path))
            except OSError:
                pass
        return sorted(names)

    def makedirs(self, path: str) -> None:
        # Las carpetas no existen por sí solas en la base: existen porque hay
        # documentos dentro. En la proyección sí se crean, para que el disco
        # tenga la estructura de siempre.
        if self.mirror:
            os.makedirs(path, exist_ok=True)

    def _row(self, rel: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT path, text, mtime, size FROM documents WHERE path = ?", (rel,)
        ).fetchone()

    def read_text(self, path: str) -> tuple[str, list[str]]:
        row = self._row(self.rel(path))
        if row is None:
            raise FileNotFoundError(path)
        return row[1], []

    def read_page(self, path: str, rel: str) -> reader.Page:
        row = self._row(self.rel(path))
        if row is None:
            raise FileNotFoundError(path)
        return reader.parse_page(row[1], rel, row[2], row[3])

    def size(self, path: str) -> int:
        row = self._row(self.rel(path))
        if row is None:
            raise FileNotFoundError(path)
        return row[3]

    def walk_files(self, folder: str) -> list[str]:
        return [self._abs(p) for p in self._under(self.rel(folder))]

    def walk_markdown(self) -> list[tuple[str, DocStat]]:
        """Los `.md` que se indexan: todo menos la papelera."""
        lo, hi = self._range(TRASH_DIR)
        rows = self.conn.execute(
            "SELECT path, mtime, size FROM documents"
            " WHERE NOT (path >= ? AND path < ?) ORDER BY path",
            (lo, hi),
        )
        return [(r[0], DocStat(r[1], r[2])) for r in rows]

    def has_markdown(self) -> bool:
        lo, hi = self._range(TRASH_DIR)
        return (
            self.conn.execute(
                "SELECT 1 FROM documents WHERE NOT (path >= ? AND path < ?) LIMIT 1", (lo, hi)
            ).fetchone()
            is not None
        )

    # ----------------------------------------------------------- escritura

    def write(self, path: str, text: str, mtime: Optional[float] = None) -> None:
        rel = self.rel(path)
        if not rel.lower().endswith(".md"):
            raise ValueError(f"en la base solo viven los .md del vault: {rel!r}")
        mtime = time.time() if mtime is None else mtime
        size = len(text.encode("utf-8"))
        self.conn.execute(
            "INSERT INTO documents (path, text, mtime, size) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET text = excluded.text,"
            " mtime = excluded.mtime, size = excluded.size",
            (rel, text, mtime, size),
        )
        if self.mirror:
            self._pending.append(("write", rel, text, mtime))

    def delete(self, path: str) -> None:
        rel = self.rel(path)
        self.conn.execute("DELETE FROM documents WHERE path = ?", (rel,))
        if self.mirror:
            self._pending.append(("delete", rel))

    def move(self, src: str, dst: str) -> None:
        """Mueve un documento o una carpeta entera (todo lo que cuelga de ella)."""
        src_rel, dst_rel = self.rel(src), self.rel(dst)
        if self._row(src_rel) is not None:
            if self._row(dst_rel) is not None:
                raise FileExistsError(dst)
            self.conn.execute("UPDATE documents SET path = ? WHERE path = ?", (dst_rel, src_rel))
        else:
            lo, hi = self._range(src_rel)
            size = len(src_rel) + 1
            self.conn.execute(
                "UPDATE documents SET path = ? || substr(path, ?) WHERE path >= ? AND path < ?",
                (dst_rel + "/", size + 1, lo, hi),
            )
        if self.mirror:
            self._pending.append(("move", src_rel, dst_rel))

    def move_with_note(self, src_rel: str, dst_rel: str) -> bool:
        """El `.md` en la base y su carpeta de sugerencias en el disco, juntos."""
        moved = self._row(src_rel) is not None
        if moved:
            self.move(self._abs(src_rel), self._abs(dst_rel))
        suggestions.move_sidecar(self.root, src_rel, dst_rel)
        return moved

    def trash_dir_for(self) -> str:
        """Sello de papelera libre, con el mismo formato que en disco."""
        base = vaultfs.trash_dir_for(self.root)  # el nombre, con sufijo si en disco choca
        if not self.isdir(base):
            return base
        n = 2
        while self.isdir(f"{base}-{n}"):
            n += 1
        return f"{base}-{n}"

    def restore(self, trash_rel: str) -> str:
        """Igual que `vaultfs.restore`, sobre la base. Devuelve la ruta absoluta."""
        src_rel = vaultfs.safe_rel(self.root, trash_rel)
        parts = src_rel.split("/")
        if len(parts) < 3 or parts[0] != TRASH_DIR:
            raise ValueError("ruta de papelera sin ruta original")
        rel_original = vaultfs.safe_rel(self.root, "/".join(parts[2:]))
        if self._row(src_rel) is None:
            raise FileNotFoundError(f"no hay ningún fichero en {trash_rel!r}")
        if self.exists(self._abs(rel_original)) or (
            self.mirror and os.path.exists(self._abs(rel_original))
        ):
            raise vaultfs.TrashConflict(
                f"la ruta original {rel_original!r} ya está ocupada en el vault: "
                "mueve o borra lo que hay ahí antes de restaurar"
            )
        partes = rel_original.split("/")
        for i in range(1, len(partes)):
            prefijo = "/".join(partes[:i])
            if self._row(prefijo) is not None or (
                self.mirror and os.path.isfile(self._abs(prefijo))
            ):
                raise vaultfs.TrashConflict(
                    f"no se puede restaurar {rel_original!r}: {prefijo!r} es un fichero, "
                    "no una carpeta; mueve o borra ese fichero antes de restaurar"
                )
        self.move(self._abs(src_rel), self._abs(rel_original))
        return self._abs(rel_original)

    def prune_empty_dirs(self, folder: str) -> None:
        # En la base no hay carpetas vacías; en la proyección se podan después.
        if self.mirror:
            self._pending.append(("prune", self.rel(folder)))

    # ----------------------------------------------------------- importar

    def import_disk_file(self, rel: str, warnings: Optional[list[str]] = None) -> bool:
        """Mete en la base el `.md` del disco en `rel` (o lo quita si ya no está).

        Lo usan la migración del primer arranque, `POST /reindex` con la
        proyección encendida y el vigilante. Se guarda normalizado (mismo
        contrato que al leer) con el `mtime` del fichero: así el índice y la
        proyección dicen la misma huella y el vigilante no lo vuelve a ver
        como cambiado. Devuelve True si el fichero estaba.
        """
        path = self._abs(rel)
        try:
            st = os.stat(path)
            text, avisos = reader.read_disk_text(path)
        except FileNotFoundError:
            self.conn.execute("DELETE FROM documents WHERE path = ?", (rel,))
            return False
        if warnings is not None:
            # Lo que se normalizó al leer (Latin-1, CRLF, BOM) solo se ve aquí:
            # en la base ya entra limpio y releerlo no avisaría de nada.
            warnings.extend(f"{rel}: {aviso}" for aviso in avisos)
        size = len(text.encode("utf-8"))
        self.conn.execute(
            "INSERT INTO documents (path, text, mtime, size) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET text = excluded.text,"
            " mtime = excluded.mtime, size = excluded.size",
            (rel, text, st.st_mtime, size),
        )
        if self.mirror and size != st.st_size:
            # El fichero no estaba normalizado (CRLF, BOM): la proyección se
            # reescribe ya normalizada para que su huella coincida con la base.
            self._pending.append(("write", rel, text, st.st_mtime))
        return True

    def import_disk_vault(self, warnings: Optional[list[str]] = None) -> int:
        """Sustituye los documentos por los `.md` del disco (papelera incluida).

        Una carpeta que no se puede leer **revienta** (no se salta): saltarla
        en silencio sería migrar un vault a medias y dar por buena la base.
        """

        def no_se_puede_leer(exc: OSError) -> None:
            raise exc

        found: list[str] = []
        if not os.path.isdir(self.root):
            # Raíz inexistente = vault vacío (como `reader.walk_markdown`). Que
            # eso no vacíe un índice con páginas lo decide antes
            # `_refuse_suspicious_vault`, con su mensaje.
            self.conn.execute("DELETE FROM documents")
            return 0
        for dirpath, dirnames, filenames in os.walk(self.root, onerror=no_se_puede_leer):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in (".git", ".obsidian", ".stversions", "_attachments")
                and not d.endswith(suggestions.SIDECAR_MARK)
            )
            for name in sorted(filenames):
                if name.lower().endswith(".md") and name not in reader.SKIP_FILES:
                    full = os.path.join(dirpath, name)
                    found.append(os.path.relpath(full, self.root).replace(os.sep, "/"))
        self.conn.execute("DELETE FROM documents")
        for rel in found:
            self.import_disk_file(rel, warnings)
        return len(found)

    # ----------------------------------------------------------- proyección

    def flush(self) -> None:
        """Proyecta al disco lo confirmado. Nunca lanza: la base ya manda."""
        pending, self._pending = self._pending, []
        for op in pending:
            try:
                self._apply(op)
            except Exception:  # noqa: BLE001 - la proyección no puede tumbar un guardado
                log.exception("no se pudo proyectar al disco: %r", op[:3])

    def discard(self) -> None:
        self._pending = []

    def _apply(self, op: tuple) -> None:
        kind = op[0]
        if kind == "write":
            _rel, text, mtime = op[1], op[2], op[3]
            path = self._abs(_rel)
            vaultfs.atomic_write(path, text)
            os.utime(path, (mtime, mtime))
        elif kind == "delete":
            path = self._abs(op[1])
            if os.path.isfile(path):
                os.unlink(path)
        elif kind == "move":
            src, dst = self._abs(op[1]), self._abs(op[2])
            if os.path.exists(src) and not os.path.exists(dst):
                vaultfs.move(src, dst)
            elif os.path.isdir(src) and os.path.isdir(dst):
                # Carpeta destino ya creada (makedirs de la proyección): se
                # vacía la vieja dentro de la nueva.
                for name in os.listdir(src):
                    vaultfs.move(os.path.join(src, name), os.path.join(dst, name))
                _prune_disk_dirs(src)
        elif kind == "prune":
            _prune_disk_dirs(self._abs(op[1]))
