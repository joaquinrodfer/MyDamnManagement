#!/usr/bin/env python3
"""Migración de una vez: las filas pasan a llamarse por su `id`.

Hasta ahora el nombre del fichero de una fila **era** su título
(`Bases/Contactos/Acme.md`), y por eso el título no se podía renombrar sin
mover el fichero y arriesgar los enlaces. Ahora el fichero se llama por los 8
primeros caracteres hex de su `id` (`Bases/Contactos/7f3a91c2.md`) y el título
vive en el frontmatter (`title`), donde se renombra sin tocar la ruta.

Para cada fila del vault:

- si el fichero ya se llama por su id (`<id8>.md`, o una versión más larga) no
  se toca: es lo que hace idempotente a la migración (`ya_estaban`);
- si el fichero se llama por su título, se escribe `title: <ese título>` en el
  frontmatter **antes** de mover y se renombra a `<id8>.md` (`renombradas`);
- si el frontmatter ya trae `title`, se respeta y no se inventa otro (no cuenta
  como `titulo_escrito`).

Los enlaces no se rompen: `[[Acme]]` resuelve por el `title` del frontmatter
(`app/links.py`), que es exactamente el que tenía el fichero por nombre. Una
fila sin `title` (frontmatter viejo) sigue resolviendo por el nombre del
fichero, así que un vault a medio migrar se lee igual.

Escritura atómica (`vaultfs.atomic_write` + `suggestions.move_with_note`, que
mueve también el sidecar de sugerencias) y el índice se reconstruye al final
desde el vault. `--dry-run` no escribe nada.

Uso:
    python3 backend/scripts/migrate_row_files_to_ids.py \\
        --vault /data/vault [--index /data/index.sqlite] [--reindex] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.vault import fm, names, reader, suggestions, vaultfs  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vault", default=os.environ.get("VAULT_PATH", "/data/vault"),
                        help="raíz del vault (por defecto $VAULT_PATH o /data/vault)")
    parser.add_argument("--index", default=os.environ.get("INDEX_PATH", "/data/index.sqlite"),
                        help="índice SQLite a reconstruir (por defecto $INDEX_PATH o /data/index.sqlite)")
    parser.add_argument("--reindex", action="store_true",
                        help="reconstruye el índice desde el vault al terminar")
    parser.add_argument("--dry-run", action="store_true",
                        help="dice lo que haría, sin escribir nada")
    return parser.parse_args(argv)


def title_from_filename(rel: str) -> str:
    return names.nfc(os.path.basename(rel)[:-3])


def frontmatter_title(text: str) -> str | None:
    """`title` del frontmatter si es un texto no vacío (si no, None)."""
    try:
        raw, _body = fm.split_document(text)
    except fm.FrontmatterError:
        return None
    if raw is None:
        return None
    value = fm.parse(raw).data.get("title")
    if isinstance(value, str) and value.strip():
        return names.nfc(value.strip())
    return None


def migrate_row(vault: str, row: reader.Page, *, dry_run: bool,
                counters: dict[str, int], warnings: list[str]) -> None:
    """Migra una fila: `title` en el frontmatter y `<id8>.md` como nombre."""
    rel = row.path
    abs_old = os.path.join(vault, vaultfs.safe_rel(vault, rel))
    stem = os.path.basename(rel)[:-3]
    if names.is_row_stem(stem, row.id):
        counters["ya_estaban"] += 1
        if frontmatter_title_of_row(row) is not None:
            return
        warnings.append(
            f"{rel}: se llama por su id y no tiene 'title' en el frontmatter; "
            "el título que tenía el fichero no se puede recuperar"
        )
        return

    try:
        with open(abs_old, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
    except OSError as exc:
        counters["errores"] += 1
        print(f"  error leyendo {rel}: {exc}", file=sys.stderr)
        return

    existing = frontmatter_title(text)
    title = existing or title_from_filename(rel)
    if existing is None:
        try:
            new_text, _avisos = fm.rewrite(text, {"title": title})
        except fm.FrontmatterError as exc:
            counters["errores"] += 1
            print(f"  error reescribiendo {rel}: {exc}", file=sys.stderr)
            return
    else:
        new_text = text

    folder = os.path.dirname(abs_old)
    target, extended = names.row_stem(folder, row.id, ignore={stem})
    new_rel = (f"{os.path.dirname(rel)}/{target}.md" if os.path.dirname(rel)
               else f"{target}.md")
    if extended:
        warnings.append(
            f"{rel}: los 8 primeros caracteres del id ya estaban ocupados en la "
            f"carpeta; se usa '{target}'"
        )

    if dry_run:
        if existing is None:
            counters["titulo_escrito"] += 1
        counters["renombradas"] += 1
        print(f"  (dry-run) {rel} -> {new_rel}  (title: {title!r})")
        return

    try:
        if new_text != text:
            vaultfs.atomic_write(abs_old, new_text)
            counters["titulo_escrito"] += 1
        # Mueve el `.md` y su sidecar de sugerencias, como cualquier renombrado.
        suggestions.move_with_note(vault, rel, new_rel)
    except OSError as exc:
        counters["errores"] += 1
        print(f"  error moviendo {rel} -> {new_rel}: {exc}", file=sys.stderr)
        return
    counters["renombradas"] += 1


def frontmatter_title_of_row(row: reader.Page) -> str | None:
    value = row.frontmatter.get("title")
    if isinstance(value, str) and value.strip():
        return names.nfc(value.strip())
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    vault = args.vault
    if not os.path.isdir(vault):
        print(f"No existe el vault: {vault}", file=sys.stderr)
        return 2

    loaded = reader.load(vault)
    rows = sorted(loaded.rows, key=lambda page: page.path)
    counters = {"filas": len(rows), "renombradas": 0, "ya_estaban": 0,
                "titulo_escrito": 0, "errores": 0}
    warnings: list[str] = list(loaded.warnings)

    for row in rows:
        if row.synthetic_id or not row.frontmatter.get("id"):
            # Sin `id` en el frontmatter su identidad sale de la ruta
            # (`path:<ruta>`): renombrar el fichero le cambiaría el id. Se deja
            # como está y se avisa; se arregla escribiéndole un id a mano.
            warnings.append(
                f"{row.path}: fila sin 'id' en el frontmatter (id derivado de la "
                "ruta): no se renombra, porque el renombrado le cambiaría la "
                "identidad. Escríbele un 'id' y vuelve a ejecutar la migración"
            )
            continue
        migrate_row(vault, row, dry_run=args.dry_run, counters=counters,
                    warnings=warnings)

    for warning in dict.fromkeys(warnings):
        print(f"  aviso: {warning}")

    print("Migración de ficheros de fila al id" + (" (dry-run)" if args.dry_run else ""))
    print(f"  vault : {vault}")
    for key, value in counters.items():
        print(f"  {key:16}: {value}")

    if args.reindex and not args.dry_run:
        from app.index import open_index
        from app.vault.store import VaultStore

        conn = open_index(args.index)
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
