"""Deriva entre el índice y el disco: qué ha cambiado por fuera del reindexado.

El ADR-0001 promete que la deriva sea visible (`/status`): sin vigilante de
ficheros (D2), editar un `.md` por fuera no se nota hasta que alguien reindexa,
y hasta entonces la app sirve el cuerpo del índice. Esto la cuenta sin leer un
solo cuerpo: se compara la huella que el índice guardó de cada nota
(`mtime` + tamaño) con la del fichero en disco, en una sola pasada de
`scandir` —que en un sistema de ficheros normal trae el `stat` gratis— más una
consulta al índice.

Lo que cuenta, y por qué:

- `modified`: el fichero está en los dos sitios pero con otro `mtime` o tamaño.
- `added`: hay un `.md` en el vault que el índice no conoce.
- `missing`: el índice tiene una nota cuyo `.md` ya no está.

`changed` es la suma: los tres casos se resuelven reindexando.

Cuenta todos los `.md` indexados: notas, bases de datos y filas. Desde P2 no
queda ningún fichero del vault que el índice ignore a propósito, así que un
`added` es siempre un fichero que la app todavía no conoce (una base o una fila
creada a mano, una nota nueva de otro editor) y desaparece al reindexar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app import index as index_mod
from app.vault import reader

# Los `mtime` se guardan como REAL (doble) en SQLite; el mismo `stat` leído dos
# veces da el mismo valor, pero se compara con margen para no contar como
# deriva lo que es ruido de redondeo en un sistema de ficheros de red.
MTIME_TOLERANCE = 1e-6


@dataclass
class Drift:
    modified: int = 0
    added: int = 0
    missing: int = 0
    checked: int = 0
    last_reindex_at: Optional[str] = None

    @property
    def changed(self) -> int:
        return self.modified + self.added + self.missing

    def as_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "modified": self.modified,
            "added": self.added,
            "missing": self.missing,
            "checked": self.checked,
            "last_reindex_at": self.last_reindex_at,
        }


def scan(root: str, conn) -> Drift:
    """Compara el índice con el vault. Un `stat` por fichero, sin leer cuerpos."""
    rows = conn.execute(
        "SELECT path, mtime, size FROM pages WHERE path IS NOT NULL"
    ).fetchall()
    indexed = {row["path"]: (row["mtime"], row["size"]) for row in rows}

    drift = Drift(
        checked=len(indexed),
        last_reindex_at=index_mod.get_meta(conn, index_mod.META_LAST_REINDEX),
    )

    seen: set[str] = set()
    for rel, st in reader.walk_markdown(root):
        seen.add(rel)
        known = indexed.get(rel)
        if known is None:
            drift.added += 1
            continue
        known_mtime, known_size = known
        if (
            known_mtime is None
            or known_size is None
            or abs(st.st_mtime - known_mtime) > MTIME_TOLERANCE
            or st.st_size != known_size
        ):
            drift.modified += 1

    drift.missing = len(indexed.keys() - seen)
    return drift
