"""Export íntegro del vault desde el índice (3.0, hito 1; ADR-0005 etapa 1).

Reconstruye un vault completo, en una carpeta o en un `.zip`, **a partir de la
base de datos**: cada página (nota, base o fila) sale de `pages.frontmatter` +
`pages.body` en su `pages.path`. Es la red de seguridad de la 3.0: mientras este
export exista y su prueba de ida y vuelta pase, volver a «los ficheros son la
verdad» sigue siendo barato.

Qué sale de dónde, dicho claro:

- **Del índice**: todos los `.md` indexados —notas, bases (con `schema`,
  `views`, `row_templates`... en su frontmatter) y filas—, byte a byte con la
  forma con la que los escribe la app (`fm.compose` + `writer.body_block`, el
  mismo contrato que el escritor). Los bloques ```` ```mdm-database ```` van tal
  cual en el cuerpo: esto es un vault, no la exportación «para leer» de una
  página (`app.export`), que los sustituye por tablas.
- **De `documents`** (3.0, hito 2: el vault dentro de la base): los `.md` que
  no son páginas indexadas, que es la papelera (`.trash/`). Si algún documento
  fuera de la papelera no está en el índice (no se pudo indexar), también sale,
  pero se cuenta en `unindexed`: la ida y vuelta exige que sea cero.
- **Del disco del vault**: solo lo que no es un `.md`: `_attachments/`
  (adjuntos binarios) y las carpetas de sugerencias de los agentes
  (`*.md.sugerencias/`). Los `.md` del disco son, como mucho, la proyección: no
  mandan. (En un índice que todavía no se ha migrado —hito 1— los `.md` del
  disco sin indexar se copian y cuentan en `unindexed`, como entonces.)
- **No sale**: `.git/`, `.obsidian/`, `.stversions/` (no son contenido del
  vault), los temporales de escritura (`*.tmp`) y la basura de sistema
  (`.DS_Store`, `Thumbs.db`). Tampoco usuarios, sesiones ni tokens: viven en el
  índice pero no son contenido (son credenciales).

Cada fichero exportado conserva su `mtime` (el de `pages.mtime` para las
páginas): así, reindexar el export da **la misma huella** de índice
(`index.dump_hash`) que el original, que es la comprobación más estricta que
hay.

No carga todos los cuerpos en memoria: recorre un cursor y escribe página a
página, dentro de UNA transacción de lectura (en WAL, una foto coherente del
índice aunque alguien guarde mientras tanto).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from typing import Iterator, Optional

from app.vault import fm, writer
from app.vault.reader import SKIP_FILES

# Carpetas del vault que no se exportan (no son contenido): las de `reader.
# IGNORED_DIRS` salvo la papelera y los adjuntos, que sí lo son.
EXCLUDED_DIRS = {".git", ".obsidian", ".stversions"}


class ExportError(ValueError):
    """El export no se puede hacer (destino inválido, índice sin páginas...)."""


@dataclass
class ExportStats:
    pages: int = 0
    notes: int = 0
    databases: int = 0
    rows: int = 0
    files: int = 0            # ficheros que no son páginas (adjuntos, papelera...)
    bytes: int = 0
    unindexed: list[str] = field(default_factory=list)
    seconds: float = 0.0
    destination: str = ""

    def as_dict(self) -> dict:
        return {
            "pages": self.pages,
            "notes": self.notes,
            "databases": self.databases,
            "rows": self.rows,
            "files": self.files,
            "bytes": self.bytes,
            "unindexed": list(self.unindexed),
            "seconds": round(self.seconds, 3),
            "destination": self.destination,
        }


def page_text(frontmatter: Optional[str], body: str) -> str:
    """El `.md` de una página tal y como lo escribe la app.

    Con frontmatter: `---`, el YAML tal cual, `---`, una línea en blanco, el
    cuerpo y un salto final (`writer.body_block`). Sin frontmatter (un fichero
    escrito a mano que no lo traía), el cuerpo con su salto final.
    """
    body = body or ""
    if frontmatter is None:
        return body + "\n" if body else ""
    return fm.compose(frontmatter, writer.body_block(body))


# --------------------------------------------------------------------------
# qué se exporta
# --------------------------------------------------------------------------


@dataclass
class _Entry:
    rel: str
    mtime: float
    text: Optional[str] = None      # páginas: el `.md` compuesto desde el índice
    source: Optional[str] = None    # resto: ruta absoluta del fichero a copiar


def _iter_pages(conn: sqlite3.Connection, stats: ExportStats) -> Iterator[_Entry]:
    cur = conn.execute(
        "SELECT path, type, frontmatter, body, mtime FROM pages"
        " WHERE path IS NOT NULL ORDER BY path"
    )
    for path, kind, front, body, mtime in cur:
        stats.pages += 1
        if kind == "database":
            stats.databases += 1
        elif kind == "database_row":
            stats.rows += 1
        else:
            stats.notes += 1
        yield _Entry(rel=path, mtime=mtime or 0.0, text=page_text(front, body))


def _iter_documents(conn: sqlite3.Connection, indexed: set[str], stats: ExportStats) -> Iterator[_Entry]:
    """Los documentos que no son páginas indexadas: la papelera (y lo no indexado)."""
    cur = conn.execute("SELECT path, text, mtime FROM documents ORDER BY path")
    for path, text, mtime in cur:
        if path in indexed:
            continue
        if path.split("/", 1)[0] != ".trash":
            stats.unindexed.append(path)
        yield _Entry(rel=path, mtime=mtime or 0.0, text=text)


def _migrated(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'storage'").fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None and row[0] == "db"


def _iter_disk_files(
    vault_root: str, indexed: set[str], stats: ExportStats, skip_markdown: bool = False
) -> Iterator[_Entry]:
    """Lo que no es una página: adjuntos, sugerencias (y, sin migrar, los `.md`)."""
    root = os.path.abspath(vault_root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dirnames[:] = sorted(
            d for d in dirnames
            if not (rel_dir == "" and d in EXCLUDED_DIRS)
            and not os.path.islink(os.path.join(dirpath, d))
        )
        for name in sorted(filenames):
            if name in SKIP_FILES or name.endswith(".tmp"):
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel in indexed:
                continue
            if skip_markdown and name.lower().endswith(".md"):
                continue
            abs_path = os.path.join(dirpath, name)
            if os.path.islink(abs_path) or not os.path.isfile(abs_path):
                continue
            top = rel.split("/", 1)[0]
            if name.lower().endswith(".md") and top != ".trash" and ".md.sugerencias/" not in rel:
                stats.unindexed.append(rel)
            yield _Entry(rel=rel, mtime=os.stat(abs_path).st_mtime, source=abs_path)


def _entries(conn: sqlite3.Connection, vault_root: Optional[str], stats: ExportStats) -> Iterator[_Entry]:
    indexed: set[str] = set()
    for entry in _iter_pages(conn, stats):
        indexed.add(entry.rel)
        yield entry
    migrated = _migrated(conn)
    if migrated:
        for entry in _iter_documents(conn, indexed, stats):
            indexed.add(entry.rel)
            stats.files += 1
            yield entry
    if vault_root and os.path.isdir(vault_root):
        for entry in _iter_disk_files(vault_root, indexed, stats, skip_markdown=migrated):
            stats.files += 1
            yield entry


def _safe_rel(rel: str) -> str:
    """Una ruta del índice no puede salirse del destino (`..`, absolutas)."""
    norm = os.path.normpath(rel.replace("/", os.sep))
    if os.path.isabs(norm) or norm.split(os.sep, 1)[0] == "..":
        raise ExportError(f"ruta fuera del vault en el índice: {rel!r}")
    return norm


# --------------------------------------------------------------------------
# destinos
# --------------------------------------------------------------------------


def export_to_folder(conn: sqlite3.Connection, vault_root: Optional[str], dest: str) -> ExportStats:
    """Escribe el vault en `dest` (que no puede existir, o tiene que estar vacía)."""
    dest = os.path.abspath(dest)
    if vault_root and _inside(dest, os.path.abspath(vault_root)):
        raise ExportError("el destino no puede estar dentro del vault")
    if os.path.exists(dest) and (not os.path.isdir(dest) or os.listdir(dest)):
        raise ExportError(f"el destino ya existe y no está vacío: {dest}")
    os.makedirs(dest, exist_ok=True)
    stats = ExportStats(destination=dest)
    t0 = time.monotonic()
    with _read_snapshot(conn):
        for entry in _entries(conn, vault_root, stats):
            target = os.path.join(dest, _safe_rel(entry.rel))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if entry.text is not None:
                data = entry.text.encode("utf-8")
                with open(target, "wb") as fh:
                    fh.write(data)
                stats.bytes += len(data)
            else:
                shutil.copyfile(entry.source, target)
                stats.bytes += os.path.getsize(target)
            os.utime(target, (entry.mtime, entry.mtime))
    stats.seconds = time.monotonic() - t0
    return stats


def export_to_zip(conn: sqlite3.Connection, vault_root: Optional[str], dest: str) -> ExportStats:
    """Escribe el vault en un `.zip` (deflate, nombres UTF-8, rutas con `/`).

    Se escribe a un temporal al lado y se renombra al final: un zip a medias no
    se queda con el nombre bueno.
    """
    dest = os.path.abspath(dest)
    if os.path.exists(dest):
        raise ExportError(f"el destino ya existe: {dest}")
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    stats = ExportStats(destination=dest)
    t0 = time.monotonic()
    fd, tmp = tempfile.mkstemp(prefix=".export-", suffix=".zip.tmp", dir=os.path.dirname(dest))
    os.close(fd)
    try:
        with _read_snapshot(conn), zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in _entries(conn, vault_root, stats):
                name = _safe_rel(entry.rel).replace(os.sep, "/")
                info = zipfile.ZipInfo(name, date_time=_zip_time(entry.mtime))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                if entry.text is not None:
                    data = entry.text.encode("utf-8")
                    zf.writestr(info, data)
                    stats.bytes += len(data)
                else:
                    with open(entry.source, "rb") as src, zf.open(info, "w") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    stats.bytes += os.path.getsize(entry.source)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    stats.seconds = time.monotonic() - t0
    return stats


def _zip_time(mtime: float) -> tuple:
    t = time.gmtime(max(mtime, 315532800))  # el zip no admite fechas antes de 1980
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class _read_snapshot:
    """Una transacción de lectura: en WAL, una foto fija del índice."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.began = False

    def __enter__(self):
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN")
            self.began = True
        return self

    def __exit__(self, *exc):
        if self.began:
            self.conn.execute("COMMIT")
        return False


# --------------------------------------------------------------------------
# línea de órdenes
# --------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """`python -m app.vault_export --index X --vault Y DESTINO[.zip]`."""
    ap = argparse.ArgumentParser(description="Exporta el vault completo desde el índice.")
    ap.add_argument("dest", help="carpeta (vacía o inexistente) o fichero .zip")
    ap.add_argument("--index", required=True, help="ruta del índice SQLite")
    ap.add_argument("--vault", default=None, help="vault del que copiar adjuntos y papelera")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{args.index}?mode=ro", uri=True)
    try:
        fn = export_to_zip if args.dest.lower().endswith(".zip") else export_to_folder
        stats = fn(conn, args.vault, args.dest)
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    d = stats.as_dict()
    print(
        f"{d['pages']} páginas ({d['notes']} notas, {d['databases']} bases, {d['rows']} filas), "
        f"{d['files']} ficheros más, {d['bytes']} bytes, {d['seconds']} s -> {d['destination']}"
    )
    if d["unindexed"]:
        print(f"aviso: {len(d['unindexed'])} .md del vault no están en el índice", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
