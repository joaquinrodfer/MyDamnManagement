"""Genera un vault sintético para medir (arranque, export, memoria).

Mismo perfil que el de las medidas de P16 (docs/MEDICIONES-P16.md): notas de
~1,2 KB repartidas en 40 carpetas, con frontmatter, fechas y dos
`[[wikilinks]]` por nota. Opcionalmente, bases de datos con filas, para que el
export y la ida y vuelta pasen también por `schema`, `views` y propiedades.

Uso:
    python scripts/gen_vault.py DESTINO --notes 12000 [--databases 5 --rows 200]

Determinista (semilla fija): dos ejecuciones dan los mismos bytes, salvo que
cambie el código.
"""

import argparse
import os
import random
import uuid

LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim "
    "veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea "
    "commodo consequat. "
)


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dest")
    ap.add_argument("--notes", type=int, default=12000)
    ap.add_argument("--folders", type=int, default=40)
    ap.add_argument("--databases", type=int, default=0)
    ap.add_argument("--rows", type=int, default=200)
    args = ap.parse_args()
    rng = random.Random(16)
    os.makedirs(args.dest, exist_ok=True)
    titles = [f"Nota {i:05d}" for i in range(args.notes)]
    for i, title in enumerate(titles):
        folder = f"Carpeta {i % args.folders:02d}"
        os.makedirs(os.path.join(args.dest, folder), exist_ok=True)
        a, b = rng.sample(titles, 2)
        day = 1 + i % 28
        body = (
            f"# {title}\n\n{LOREM * 3}\n\nVer [[{a}]] y [[{b}]].\n\n"
            f"- uno\n- dos\n- tres\n\n{LOREM * 2}"
        )
        text = (
            f"---\nid: {_uuid(rng)}\ncreated: 2026-10-{day:02d}T10:00:00+00:00\n"
            f"updated: 2026-10-{day:02d}T11:00:00+00:00\n---\n\n{body}\n"
        )
        with open(os.path.join(args.dest, folder, f"{title}.md"), "w", encoding="utf-8") as fh:
            fh.write(text)
    if args.databases:
        os.makedirs(os.path.join(args.dest, "Bases"), exist_ok=True)
    for d in range(args.databases):
        name = f"Base {d:02d}"
        db_id = _uuid(rng)
        view_id = _uuid(rng)
        text = (
            f"---\nid: {db_id}\ntype: database\n"
            "created: 2026-10-01T10:00:00+00:00\nupdated: 2026-10-01T10:00:00+00:00\n"
            "schema:\n"
            "  - key: estado\n    name: Estado\n    type: select\n    options: [Pendiente, Hecho]\n"
            "  - key: puntos\n    name: Puntos\n    type: number\n"
            f"views:\n  - id: {view_id}\n    name: Tabla\n    type: table\n"
            "---\n\nUna base de prueba.\n"
        )
        with open(os.path.join(args.dest, "Bases", f"{name}.md"), "w", encoding="utf-8") as fh:
            fh.write(text)
        rows_dir = os.path.join(args.dest, "Bases", name)
        os.makedirs(rows_dir, exist_ok=True)
        for r in range(args.rows):
            row_id = _uuid(rng)
            estado = "Hecho" if r % 3 == 0 else "Pendiente"
            text = (
                f"---\nid: {row_id}\ntitle: Fila {r:04d}\n"
                "created: 2026-10-02T10:00:00+00:00\nupdated: 2026-10-02T10:00:00+00:00\n"
                f"estado: {estado}\npuntos: {r % 13}\n---\n\nCuerpo de la fila {r}.\n"
            )
            with open(os.path.join(rows_dir, f"{row_id}.md"), "w", encoding="utf-8") as fh:
                fh.write(text)


if __name__ == "__main__":
    main()
