#!/usr/bin/env python3
"""Migración de una vez: las bases y filas que solo están en el índice, a ficheros.

Escribe en el vault, con el formato del ADR-0001:

- `Bases/<slug>.md` por cada fila `databases` del índice, con `type: database`,
  `id`, `icon`, `schema` y `views` en el frontmatter (el `id` es el de la
  **página** de la base: desde P2 la base y su página son la misma identidad);
- `Bases/<slug>/<fila>.md` por cada fila `rows`, con las propiedades que el
  índice ya tenía coaccionadas y sin `title` (el título es el nombre del
  fichero).

Es **idempotente**: si el `id` ya está en algún `.md` del vault, ese elemento se
salta (y el nombre de su carpeta se respeta, para poder colgar ahí las filas).
Se puede ejecutar dos veces sin duplicar ficheros ni pisar renombrados hechos a
mano.

Una base que cuelga de una nota se escribe en la carpeta de hijas de esa nota
(la jerarquía son carpetas, D4), si la nota ya está en el vault; si no, en
`Bases/` y se avisa: la nota tiene que migrarse antes
(`migrate_index_to_vault.py`).

Cambio de identidad asumido: el `id` de la base pasa a ser el de su página, así
que el `database_id` que devolvía la API cambia una vez. Las filas conservan el
suyo y sus enlaces no se tocan.

Uso:
    python3 backend/scripts/migrate_databases_to_vault.py \\
        --index /data/index.sqlite --vault /data/vault [--reindex] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.vault import names, reader, vaultfs, writer  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", default=os.environ.get("INDEX_PATH", "/data/index.sqlite"),
                        help="índice SQLite de origen (por defecto $INDEX_PATH o /data/index.sqlite)")
    parser.add_argument("--vault", default=os.environ.get("VAULT_PATH", "/data/vault"),
                        help="raíz del vault (por defecto $VAULT_PATH o /data/vault)")
    parser.add_argument("--reindex", action="store_true",
                        help="reconstruye el índice desde el vault al terminar")
    parser.add_argument("--dry-run", action="store_true",
                        help="dice lo que haría, sin escribir nada")
    return parser.parse_args(argv)


def load_databases(conn: sqlite3.Connection) -> list[dict]:
    """Bases del índice con su página, su schema, sus vistas y sus filas."""
    conn.row_factory = sqlite3.Row
    bases: list[dict] = []
    rows = conn.execute(
        "SELECT d.id AS id, d.page_id AS page_id, d.schema_json AS schema_json,"
        " p.title AS title, p.icon AS icon, p.description AS description,"
        " p.parent_id AS parent_id, p.created_at AS created_at,"
        " p.updated_at AS updated_at, p.created_by AS created_by,"
        " p.path AS path"
        " FROM databases d JOIN pages p ON p.id = d.page_id"
        " ORDER BY p.created_at, p.rowid"
    ).fetchall()
    for row in rows:
        base = dict(row)
        base["schema"] = json.loads(base["schema_json"] or "[]")
        base["views"] = [
            {
                "id": view["id"],
                "name": view["name"],
                "type": view["type"],
                "config": json.loads(view["config_json"] or "{}"),
            }
            for view in conn.execute(
                "SELECT id, name, type, config_json FROM views"
                " WHERE database_id = ? ORDER BY rowid",
                (base["id"],),
            )
        ]
        base["rows"] = [
            {
                "id": r["id"],
                "title": r["title"],
                "properties": json.loads(r["properties_json"] or "{}"),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in conn.execute(
                "SELECT r.id AS id, r.properties_json AS properties_json,"
                " p.title AS title, p.created_at AS created_at, p.updated_at AS updated_at"
                " FROM rows r JOIN pages p ON p.id = r.id"
                " WHERE r.database_id = ? ORDER BY p.created_at, p.rowid",
                (base["id"],),
            )
        ]
        bases.append(base)
    return bases


def existing_paths(vault: str) -> dict[str, str]:
    """Ruta de cada página que ya está en el vault, por `id`."""
    if not os.path.isdir(vault):
        return {}
    loaded = reader.load(vault)
    return {page.id: page.path for page in loaded.pages.values() if page.id}


def folder_for(base: dict, vault: str, note_paths: dict[str, str]) -> tuple[str, list[str]]:
    """Carpeta de la base: `Bases/` o la de hijas de su nota, si está en el vault."""
    warnings: list[str] = []
    parent_id = base["parent_id"]
    if not parent_id:
        return reader.DATABASES_DIR, warnings
    parent_path = note_paths.get(parent_id)
    if not parent_path:
        warnings.append(
            f"{base['title']!r}: cuelga de una página que no está en el vault "
            f"({parent_id}); se escribe en {reader.DATABASES_DIR}/ (migra las notas antes)"
        )
        return reader.DATABASES_DIR, warnings
    return parent_path[:-3], warnings


def write_database(vault: str, base: dict, folder: str, stem: str,
                   dry_run: bool) -> str:
    """Escribe `folder/<stem>.md`. Devuelve la ruta relativa."""
    rel_dir = vaultfs.safe_rel(vault, folder) if folder else ""
    rel = f"{rel_dir}/{stem}.md" if rel_dir else f"{stem}.md"
    text = writer.database_markdown(
        id_=base["page_id"],
        icon=base["icon"],
        schema=base["schema"],
        views=base["views"],
        created=base["created_at"],
        updated=base["updated_at"],
        description=base["description"],
        created_by=base["created_by"],
    )
    if not dry_run:
        os.makedirs(os.path.join(vault, rel_dir) if rel_dir else vault, exist_ok=True)
        vaultfs.atomic_write(os.path.join(vault, rel), text)
        os.makedirs(os.path.join(vault, rel[:-3]), exist_ok=True)
    return rel


def write_row(vault: str, base: dict, folder: str, row: dict, stem: str,
              dry_run: bool) -> str:
    rel_dir = vaultfs.safe_rel(vault, folder)
    rel = f"{rel_dir}/{stem}.md"
    text = writer.row_markdown(
        id_=row["id"],
        properties=row["properties"],
        created=row["created_at"],
        updated=row["updated_at"],
    )
    if not dry_run:
        os.makedirs(os.path.join(vault, rel_dir), exist_ok=True)
        vaultfs.atomic_write(os.path.join(vault, rel), text)
    return rel


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    index_path = Path(args.index)
    vault = args.vault

    if not index_path.exists():
        print(f"No existe el índice: {index_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    try:
        bases = load_databases(conn)
        note_paths = {
            row["id"]: row["path"]
            for row in conn.execute(
                "SELECT id, path FROM pages WHERE type = 'note' AND path IS NOT NULL"
            )
        }
    except sqlite3.DatabaseError as exc:
        print(f"No se pudo leer el índice {index_path}: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    present = existing_paths(vault)
    counters = {
        "bases_leidas": len(bases),
        "bases_escritas": 0,
        "bases_saltadas": 0,
        "filas_escritas": 0,
        "filas_saltadas": 0,
        "vistas_escritas": 0,
        "errores": 0,
    }
    warnings: list[str] = []

    if not bases:
        print("El índice no tiene bases de datos: no hay nada que migrar.")
        return 0

    for base in bases:
        folder, avisos = folder_for(base, vault, note_paths)
        warnings.extend(avisos)

        if base["page_id"] in present:
            # Ya migrada: se respeta el nombre que tenga en disco.
            rel = present[base["page_id"]]
            folder = os.path.dirname(rel)
            stem = os.path.basename(rel)[:-3]
            counters["bases_saltadas"] += 1
        else:
            stem, san_notes = names.sanitize(base["title"] or "Sin título")
            warnings.extend(f"{base['title']!r}: {nota}" for nota in san_notes)
            directory = os.path.join(vault, folder) if folder else vault
            if not args.dry_run:
                os.makedirs(directory, exist_ok=True)
            stem, collided = names.unique_stem(directory, stem)
            if collided:
                warnings.append(
                    f"{base['title']!r}: ya hay un fichero con ese nombre en "
                    f"{folder}/; se escribe como {stem!r}"
                )
            try:
                rel = write_database(vault, base, folder, stem, args.dry_run)
                counters["bases_escritas"] += 1
                counters["vistas_escritas"] += len(base["views"])
                if args.dry_run:
                    print(f"  (dry-run) {rel}")
            except OSError as exc:
                counters["errores"] += 1
                print(f"  error escribiendo {folder}/{stem}.md: {exc}", file=sys.stderr)
                continue

        row_folder = rel[:-3]
        for row in base["rows"]:
            if row["id"] in present:
                counters["filas_saltadas"] += 1
                continue
            row_stem, san_notes = names.sanitize(row["title"] or "Sin título")
            warnings.extend(f"fila {row['title']!r}: {nota}" for nota in san_notes)
            directory = os.path.join(vault, row_folder)
            if not args.dry_run:
                os.makedirs(directory, exist_ok=True)
            row_stem, collided = names.unique_stem(directory, row_stem)
            if collided:
                warnings.append(
                    f"fila {row['title']!r}: ya hay un fichero con ese nombre en "
                    f"{row_folder}/; se escribe como {row_stem!r}"
                )
            try:
                row_rel = write_row(vault, base, row_folder, row, row_stem, args.dry_run)
                counters["filas_escritas"] += 1
                if args.dry_run:
                    print(f"  (dry-run) {row_rel}")
            except OSError as exc:
                counters["errores"] += 1
                print(f"  error escribiendo {row_folder}/{row_stem}.md: {exc}", file=sys.stderr)

    for warning in dict.fromkeys(warnings):
        print(f"  aviso: {warning}")

    print("Migración de bases de datos al vault" + (" (dry-run)" if args.dry_run else ""))
    print(f"  índice : {index_path}")
    print(f"  vault  : {vault}")
    for key, value in counters.items():
        print(f"  {key:16}: {value}")

    if args.reindex and not args.dry_run:
        from app.index import open_index
        from app.vault.store import VaultStore

        conn = open_index(str(index_path))
        try:
            stats = VaultStore(vault, conn).rebuild()
        finally:
            conn.close()
        print(
            f"  reindexado      : {stats.notes} notas, {stats.databases} bases, "
            f"{stats.rows} filas, {stats.views} vistas, {stats.links} enlaces, "
            f"{len(stats.warnings)} avisos, {stats.seconds:.2f} s"
        )
        for warning in stats.warnings[:20]:
            print(f"    aviso: {warning}")

    return 0 if counters["errores"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
