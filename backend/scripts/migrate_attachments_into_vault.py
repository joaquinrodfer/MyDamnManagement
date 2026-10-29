#!/usr/bin/env python3
"""Migración de una vez: los adjuntos que estaban fuera del vault pasan dentro.

Hasta P1.2b las imágenes de cabecera se guardaban en un volumen aparte
(`/app/attachments/pages/<id>/header.<ext>`) y el vault solo tenía los `.md`:
copiar el vault no se llevaba las imágenes. Desde P1.2b viven en
`_attachments/<año>/<mes>/header-<id>.<ext>`, que es lo que este script deja
hecho:

1. **Mueve** cada fichero de `--source` al vault. La carpeta de año/mes sale del
   `mtime` del fichero (cuándo se subió, no cuándo se migra) y el nombre es el
   que usa la app desde P1.2b, así que un adjunto migrado y uno nuevo no se
   distinguen.
2. **Reescribe** la clave `header_image` del frontmatter de las notas que
   apuntaban a `/files/pages/<id>/…` para que apunten a la ruta nueva. Incluye
   los `.md` que están en `.trash/<sello>/`: una nota borrada antes de migrar
   se queda en la papelera con la URL vieja, y al restaurarla la imagen daría
   404 (y el adjunto migrado quedaría sin ninguna referencia viva).
3. **Actualiza** `pages.header_image_path` en el índice, si existe.

Es **idempotente**: la segunda pasada no mueve ni reescribe nada. Y si se corta
a mitad, la siguiente reconcilia — las referencias se resuelven también
buscando la cabecera ya movida por el `id` de la página, así que no depende de
que el paso 1 y el 2 hayan ido juntos.

Reescribir el frontmatter cambia el `mtime`, así que justo después de migrar
`GET /status` da deriva (`modified`) igual al número de notas tocadas: el índice
sigue sirviendo lo mismo (la URL no se pinta en las listas), pero se puede
cerrar del todo con `--reindex`.

Uso:
    python3 backend/scripts/migrate_attachments_into_vault.py \\
        --source /app/attachments --vault /data/vault --index /data/index.sqlite \\
        [--reindex] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.vault import attachments, fm, vaultfs  # noqa: E402
from app.vault.reader import IGNORED_DIRS, SKIP_FILES  # noqa: E402

# Ruta de un adjunto en el esquema viejo: `pages/<id>/<nombre>`.
LEGACY_SOURCE_RE = re.compile(r"^pages/(?P<page_id>[^/]+)/(?P<name>[^/]+)$")

# URL con la que se servía: `/files/pages/<id>/header.png`.
LEGACY_URL_RE = re.compile(r"^/files/pages/(?P<page_id>[^/]+)/(?P<name>[^/]+)$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        default=os.environ.get("LEGACY_ATTACHMENTS_DIR", "/app/attachments"),
        help="carpeta vieja de adjuntos (por defecto /app/attachments)",
    )
    parser.add_argument(
        "--vault",
        default=os.environ.get("VAULT_PATH", "/data/vault"),
        help="raíz del vault (por defecto $VAULT_PATH o /data/vault)",
    )
    parser.add_argument(
        "--index",
        default=os.environ.get("INDEX_PATH", "/data/index.sqlite"),
        help="índice SQLite (por defecto $INDEX_PATH o /data/index.sqlite); "
        "si no existe, solo se tocan los ficheros",
    )
    parser.add_argument(
        "--reindex", action="store_true",
        help="reconstruye el índice desde el vault al terminar (el frontmatter "
             "reescrito cambia el mtime: sin esto, `/status` da deriva)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="dice lo que haría, sin escribir nada"
    )
    return parser.parse_args(argv)


def _posix(path: Path) -> str:
    return str(path).replace(os.sep, "/")


def _month_of(path: Path) -> str:
    """`<año>/<mes>` UTC del `mtime`: cuándo se subió, no cuándo se migra."""
    cuando = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return attachments.month_folder(cuando)


def _target(vault: Path, src: Path, page_id: str | None, name: str) -> Path:
    """Destino de un fichero del origen, sin resolver colisiones todavía."""
    carpeta = vault / attachments.ATTACHMENTS_DIR / _month_of(src)
    if page_id:
        return carpeta / f"{attachments.header_stem(page_id)}{Path(name).suffix}"
    return carpeta / name


def _same_bytes(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                bloque_a, bloque_b = fa.read(1 << 16), fb.read(1 << 16)
                if bloque_a != bloque_b:
                    return False
                if not bloque_a:
                    return True
    except OSError:
        return False


def _free_target(dst: Path) -> Path:
    """`x.png` -> `x (2).png`, `x (3).png`… si ya hay otro contenido."""
    base, ext = os.path.splitext(dst)
    n = 2
    while dst.exists():
        dst = Path(f"{base} ({n}){ext}")
        n += 1
    return dst


def migrate_files(
    source: Path, vault: Path, *, dry_run: bool, counters: dict, warnings: list[str]
) -> dict[str, str]:
    """Mueve los ficheros y devuelve {URL vieja: URL nueva} para las referencias."""
    movidas: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames.sort()
        for name in sorted(filenames):
            src = Path(dirpath) / name
            rel_source = _posix(src.relative_to(source))
            match = LEGACY_SOURCE_RE.match(rel_source)
            page_id = match.group("page_id") if match else None

            try:
                dst = _target(vault, src, page_id, name)
            except OSError as exc:  # mtime ilegible
                counters["errores"] += 1
                print(f"  error leyendo {src}: {exc}", file=sys.stderr)
                continue

            if dst.exists() and _same_bytes(src, dst):
                # Ya estaba migrado (o se copió a mano): el origen es un
                # duplicado y se retira. Así la segunda pasada es un no-op.
                counters["ya_estaban"] += 1
                print(f"  ya en el vault: {_posix(dst.relative_to(vault))}")
                if not dry_run:
                    src.unlink()
                if page_id:
                    movidas[f"/files/pages/{page_id}/{name}"] = attachments.url_for(
                        _posix(dst.relative_to(vault))
                    )
                continue

            if dst.exists():
                dst = _free_target(dst)

            rel_dst = _posix(dst.relative_to(vault))
            counters["movidos"] += 1
            print(f"  {rel_source} -> {rel_dst}")
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))   # puede cruzar volúmenes
            if page_id:
                movidas[f"/files/pages/{page_id}/{name}"] = attachments.url_for(rel_dst)
            else:
                warnings.append(
                    f"{rel_source} no venía de `pages/<id>/`: se ha movido a "
                    f"{rel_dst}, pero ninguna nota lo referencia por nombre"
                )
    return movidas


def _prune_empty(path: Path) -> None:
    """Borra las subcarpetas vacías que deja el origen, de abajo arriba.

    La raíz de `--source` NO se borra: así una segunda pasada sigue siendo un
    no-op con su resumen a cero en vez de fallar por «no existe la carpeta».
    """
    if not path.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        if Path(dirpath) == path or filenames:
            continue
        for name in dirnames:
            sub = Path(dirpath) / name
            if sub.is_dir() and not any(sub.iterdir()):
                sub.rmdir()
        try:
            Path(dirpath).rmdir()
        except OSError:
            pass


def _find_moved(vault: Path, page_id: str) -> str | None:
    """Ruta en el vault de una cabecera ya movida, buscada por el id de página."""
    encontradas = attachments.find_header(str(vault), page_id)
    return encontradas[0] if encontradas else None


def _resolve_new_url(vault: Path, old_url: str, movidas: dict[str, str]) -> str | None:
    if old_url in movidas:
        return movidas[old_url]
    match = LEGACY_URL_RE.match(old_url)
    if match is None:
        return None
    rel = _find_moved(vault, match.group("page_id"))
    return attachments.url_for(rel) if rel else None


def _iter_markdown(vault: Path):
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in sorted(filenames):
            if name.lower().endswith(".md") and name not in SKIP_FILES:
                yield Path(dirpath) / name


def _iter_trash_markdown(vault: Path):
    """Los `.md` de `.trash/<sello>/`, que `_iter_markdown` no visita.

    Reescritos sólo en el frontmatter: el fichero NO se mueve de la papelera.
    La alternativa (resolver la URL vieja al restaurar) dejaría el mismo
    trabajo para cada restauración futura y el adjunto migrado huérfano hasta
    entonces; arreglarlo aquí lo hace una vez, al migrar, que es cuando se
    mueve el fichero al que apunta.
    """
    trash = vault / ".trash"
    if trash.is_dir():
        yield from _iter_markdown(trash)


def _fix_header_reference(
    vault: Path, path: Path, movidas: dict[str, str], *, dry_run: bool,
    counters: dict, warnings: list[str], counter: str,
) -> None:
    """Una nota: si su `header_image` es del esquema viejo, lo pasa al nuevo."""
    try:
        texto = path.read_text(encoding="utf-8", newline="")
    except (OSError, UnicodeDecodeError) as exc:
        counters["errores"] += 1
        print(f"  error leyendo {path}: {exc}", file=sys.stderr)
        return
    if "/files/pages/" not in texto:
        return
    try:
        raw, _body = fm.split_document(texto)
        data = fm.parse(raw).data if raw is not None else {}
    except fm.FrontmatterError as exc:
        warnings.append(f"{_posix(path.relative_to(vault))}: frontmatter ilegible ({exc})")
        return
    old_url = data.get("header_image")
    if not isinstance(old_url, str) or not old_url.startswith("/files/pages/"):
        return
    new_url = _resolve_new_url(vault, old_url, movidas)
    if new_url is None:
        warnings.append(
            f"{_posix(path.relative_to(vault))}: apunta a {old_url!r} y no hay "
            "ningún fichero que le corresponda en el vault; se deja como está"
        )
        return
    counters[counter] += 1
    print(f"  {_posix(path.relative_to(vault))}: {old_url} -> {new_url}")
    if dry_run:
        return
    nuevo, _avisos = fm.rewrite(texto, {"header_image": new_url})
    vaultfs.atomic_write(str(path), nuevo)


def migrate_notes(
    vault: Path, movidas: dict[str, str], *, dry_run: bool, counters: dict, warnings: list[str]
) -> None:
    """Reescribe `header_image` en el frontmatter de las notas que lo necesiten.

    Dos pasadas: las notas vivas (`notas_actualizadas`) y las que están en la
    papelera (`papelera_actualizadas`). Las de la papelera se reescriben igual
    porque la migración mueve **su** adjunto: si no, el fichero quedaría sin
    referencia viva y al restaurar la nota la cabecera daría 404.
    """
    for path in _iter_markdown(vault):
        _fix_header_reference(
            vault, path, movidas, dry_run=dry_run, counters=counters,
            warnings=warnings, counter="notas_actualizadas",
        )
    for path in _iter_trash_markdown(vault):
        _fix_header_reference(
            vault, path, movidas, dry_run=dry_run, counters=counters,
            warnings=warnings, counter="papelera_actualizadas",
        )


def migrate_index(
    index_path: Path, vault: Path, movidas: dict[str, str], *, dry_run: bool,
    counters: dict, warnings: list[str],
) -> None:
    """Actualiza `pages.header_image_path` para las filas con la URL vieja."""
    if not index_path.exists():
        warnings.append(f"no hay índice en {index_path}: solo se han tocado los ficheros")
        return
    conn = sqlite3.connect(index_path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            filas = conn.execute(
                "SELECT id, header_image_path FROM pages"
                " WHERE header_image_path LIKE '/files/pages/%'"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            warnings.append(f"no se pudo leer el índice {index_path}: {exc}")
            return
        for fila in filas:
            old_url = fila["header_image_path"]
            new_url = _resolve_new_url(vault, old_url, movidas)
            if new_url is None:
                warnings.append(
                    f"índice: la página {fila['id']} apunta a {old_url!r} y no hay "
                    "fichero que le corresponda; se deja como está"
                )
                continue
            counters["indice_actualizado"] += 1
            print(f"  índice {fila['id']}: {old_url} -> {new_url}")
            if not dry_run:
                conn.execute(
                    "UPDATE pages SET header_image_path = ? WHERE id = ?",
                    (new_url, fila["id"]),
                )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = Path(args.source)
    vault = Path(args.vault)
    index_path = Path(args.index)

    if not source.is_dir():
        print(f"No existe la carpeta de adjuntos vieja: {source}", file=sys.stderr)
        print("  (si ya la migraste, no hay nada que hacer)", file=sys.stderr)
        return 2
    if not vault.is_dir():
        print(f"No existe la raíz del vault: {vault}", file=sys.stderr)
        return 2

    counters = {
        "movidos": 0,
        "ya_estaban": 0,
        "notas_actualizadas": 0,
        "papelera_actualizadas": 0,
        "indice_actualizado": 0,
        "errores": 0,
    }
    warnings: list[str] = []

    print(f"Adjuntos viejos : {source}")
    print(f"Vault           : {vault}")
    print(f"Índice          : {index_path}")
    print()

    movidas = migrate_files(source, vault, dry_run=args.dry_run, counters=counters, warnings=warnings)
    migrate_notes(vault, movidas, dry_run=args.dry_run, counters=counters, warnings=warnings)
    migrate_index(
        index_path, vault, movidas, dry_run=args.dry_run, counters=counters, warnings=warnings
    )
    if not args.dry_run:
        _prune_empty(source)

    for warning in warnings:
        print(f"  aviso: {warning}")
    print()
    print("Migración de adjuntos al vault" + (" (dry-run)" if args.dry_run else ""))
    for key, value in counters.items():
        print(f"  {key:21}: {value}")

    if args.reindex and not args.dry_run:
        from app.index import open_index
        from app.vault.store import VaultError, VaultStore

        conn = open_index(str(index_path))
        try:
            # Sin `allow_empty`: el mismo guardia que el arranque y `POST
            # /reindex` (un `--vault` equivocado no vacía el índice). Si el
            # índice ya no tiene notas, el guardia no estorba.
            stats = VaultStore(str(vault), conn).rebuild()
        except VaultError as exc:
            print(f"  aviso: no se ha reindexado: {exc}", file=sys.stderr)
            print(
                "  (los ficheros ya están migrados; el índice no se ha tocado: "
                "revisa --vault/--index o reindexa a mano después)",
                file=sys.stderr,
            )
            return 1
        finally:
            conn.close()
        print(
            f"  reindexado           : {stats.notes} notas, {stats.links} enlaces, "
            f"{len(stats.warnings)} avisos, {stats.seconds:.2f} s"
        )
        for warning in stats.warnings[:20]:
            print(f"    aviso: {warning}")
    return 0 if counters["errores"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
