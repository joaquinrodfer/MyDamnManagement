#!/usr/bin/env python3
"""Migración de una vez: las notas del índice actual pasan a ficheros `.md`.

Escribe en el vault una nota por cada fila `type='note'` del índice, con el
formato del ADR-0001 (`Notas/<carpeta>/<título>.md`, frontmatter con `id`,
`icon`, `description`, `created`, `updated`) y la jerarquía de `parent_id`
convertida en carpetas. Después, con `--reindex`, reconstruye el índice desde
el vault para que las dos cosas coincidan.

Es **idempotente**: si el `id` ya está en el vault (en el frontmatter de algún
`.md`), esa nota se salta. Se puede ejecutar dos veces sin duplicar ficheros.

Los nombres se resuelven **antes** de escribir: la carpeta de una nota es la de
su padre más el nombre que el padre ha acabado teniendo en disco (no su título
saneado, que cambia si dos hermanas se llaman igual). Las notas que ya estaban
en el vault conservan el nombre que tienen.

Lo que no hace: no borra nada del índice (el reindexado lo pone al día) y no
toca las bases de datos ni sus filas, que siguen en el índice (P2).

Uso:
    python3 backend/scripts/migrate_index_to_vault.py \
        --index /data/index.sqlite --vault /data/vault [--reindex] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.vault import names, reader  # noqa: E402
from app.vault.store import NOTES_DIR, write_note_file  # noqa: E402


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


def load_notes(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, parent_id, title, icon, description, body, created_by,"
        " header_image_path, created_at, updated_at"
        " FROM pages WHERE type = 'note'"
        " ORDER BY created_at, rowid"
    ).fetchall()
    return [dict(r) for r in rows]


def existing_paths(vault: str) -> dict[str, str]:
    """Ruta relativa de cada nota que ya está en el vault, por `id`.

    Son las que un reejecutado se salta, pero su nombre y su carpeta siguen
    contando: son la base de la carpeta de sus hijas (y así la segunda pasada
    coloca las nuevas igual que lo habría hecho la primera).
    """
    if not os.path.isdir(vault):
        return {}
    loaded = reader.load(vault)
    return {
        page.id: page.path
        for page in loaded.pages.values()
        if page.id and not page.synthetic_id
    }


def plan_files(
    notes: list[dict], vault: str, existing: dict[str, str]
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Resuelve (carpeta, nombre final) de cada nota ANTES de escribir ninguna.

    La carpeta de una nota es la de su padre más el nombre **que el padre acaba
    teniendo en disco**. Calculada desde el título saneado, con dos hermanas
    «Misma» las hijas de la segunda acababan en `Notas/Misma/`, la carpeta de
    la primera (donde el reindexado las colgaba de la nota equivocada).

    Si algún antecesor no es una nota (una base de datos, que en P1.2a no vive
    en ficheros), la nota va a `Notas/` y se avisa: mejor una nota plana y
    visible que una ruta que el reindexado no sabría volver a deducir.
    """
    by_id = {n["id"]: n for n in notes}
    plan: dict[str, tuple[str, str]] = {}
    claimed: dict[str, set[str]] = {}
    warnings: list[str] = []

    def claim(folder: str, stem: str) -> None:
        key = names.nfc(stem).casefold()
        claimed.setdefault(folder, set()).update({key, f"{key}.md"})

    def resolve(note: dict, seen: set[str]) -> tuple[str, str]:
        note_id = note["id"]
        if note_id in plan:
            return plan[note_id]
        if note_id in existing:
            folder = os.path.dirname(existing[note_id])
            stem = os.path.basename(existing[note_id])[:-3]
            plan[note_id] = (folder, stem)
            claim(folder, stem)
            return plan[note_id]

        parent_id = note["parent_id"]
        if not parent_id:
            folder = NOTES_DIR
        elif parent_id in seen:
            warnings.append(f"{note_id}: ciclo de padres; va a {NOTES_DIR}/")
            folder = NOTES_DIR
        elif parent_id not in by_id:
            warnings.append(
                f"{note_id} ({note['title']!r}): el padre {parent_id} no es una "
                f"nota (¿una base de datos?); va a {NOTES_DIR}/"
            )
            folder = NOTES_DIR
        else:
            seen.add(note_id)
            parent_folder, parent_stem = resolve(by_id[parent_id], seen)
            folder = f"{parent_folder}/{parent_stem}"

        stem, _ = names.sanitize(note["title"] or "Sin título")
        directory = os.path.join(vault, folder)
        stem, collided = names.unique_stem(directory, stem, claimed.get(folder))
        if collided:
            warnings.append(
                f"{note_id} ({note['title']!r}): ya hay un fichero con ese nombre "
                f"en {folder}/; se escribe como {stem!r}"
            )
        plan[note_id] = (folder, stem)
        claim(folder, stem)
        return plan[note_id]

    for note in notes:
        resolve(note, {note["id"]})
    return plan, warnings


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    index_path = Path(args.index)
    vault = args.vault

    if not index_path.exists():
        print(f"No existe el índice: {index_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    try:
        notes = load_notes(conn)
    except sqlite3.DatabaseError as exc:
        print(f"No se pudo leer el índice {index_path}: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    present = existing_paths(vault)
    plan, warnings = plan_files(notes, vault, present)

    counters = {"leidas": len(notes), "escritas": 0, "saltadas": 0, "errores": 0,
                "renombradas": 0}
    for note in notes:
        title = note["title"] or "Sin título"
        folder, stem = plan[note["id"]]
        rel = f"{folder}/{stem}.md"
        sanitized = names.sanitize(title)[0]
        if sanitized != title:
            counters["renombradas"] += 1
            print(f"  aviso: '{title}' -> '{sanitized}' (el nombre del fichero es el título)")
        if note["id"] in present:
            counters["saltadas"] += 1
            continue
        if args.dry_run:
            print(f"  (dry-run) {rel}")
            counters["escritas"] += 1
            continue
        try:
            written, _notes = write_note_file(
                vault,
                folder=folder,
                title=title,
                stem=stem,
                page_id=note["id"],
                icon=note["icon"],
                description=note["description"],
                body=note["body"] or "",
                created=note["created_at"],
                updated=note["updated_at"],
                created_by=note["created_by"],
                header_image=note["header_image_path"],
            )
            counters["escritas"] += 1
            if written != rel:
                print(f"  aviso: '{rel}' se escribió como '{written}' (colisión de nombre)")
        except OSError as exc:
            counters["errores"] += 1
            print(f"  error escribiendo {rel}: {exc}", file=sys.stderr)

    for warning in warnings:
        print(f"  aviso: {warning}")

    print("Migración de notas al vault")
    print(f"  índice : {index_path}")
    print(f"  vault  : {vault}")
    for key, value in counters.items():
        print(f"  {key:12}: {value}")

    if args.reindex and not args.dry_run:
        from app.index import open_index
        from app.vault.store import VaultStore

        conn = open_index(str(index_path))
        try:
            stats = VaultStore(vault, conn).rebuild()
        finally:
            conn.close()
        print(f"  reindexado  : {stats.notes} notas, {stats.links} enlaces, "
              f"{len(stats.warnings)} avisos, {stats.seconds:.2f} s")
        for warning in stats.warnings[:20]:
            print(f"    aviso: {warning}")

    return 0 if counters["errores"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
