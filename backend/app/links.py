"""Resolución de wikilinks contra el índice.

Vive aparte de `repository.py` porque la usan los dos lados de P1.2: el
repositorio (guardado de una nota) y `app/vault/store.py` (reindexado desde los
ficheros). Si estuviera en `repository`, el puente del vault tendría que
importar el repositorio y habría un ciclo.

La resolución sigue siendo la de siempre: por título, sin distinguir mayúsculas
en todo Unicode, dentro del mismo workspace y enlazando a **todas** las páginas
con ese título.
"""

from __future__ import annotations

import sqlite3
import unicodedata
import uuid

from app.enums import LinkKind
from app.wikilinks import extract_titles

# Namespace fijo para ids de enlaces (uuid5): el índice tiene que ser
# reconstruible de forma idéntica, así que nada de uuid4 aquí.
_LINK_NAMESPACE = uuid.UUID("a3d1c0d2-3a0e-5b9e-8f5c-2c7b1f0d9e21")


def fold_title(title: str) -> str:
    """Forma canónica de un título para compararlo sin distinguir mayúsculas.

    `COLLATE NOCASE` de SQLite solo pliega ASCII (`'REUNIÓN' = 'Reunión'` es
    falso), así que la comparación se hace aquí: `NFKC` compone los acentos
    (para que `ó` precompuesta y `o` + acento combinado coincidan) y `casefold`
    pliega las mayúsculas en todo Unicode, como el `lower()` de Postgres que
    había antes de P1.1. A propósito **no** se quitan los acentos: `Peña` y
    `Pena` siguen siendo títulos distintos, igual que antes.
    """
    return unicodedata.normalize("NFKC", title).casefold()


def sync_wikilinks(
    conn: sqlite3.Connection, page_id: str, workspace_id: str, body_markdown: str
) -> None:
    """Recalcula los wikilinks salientes de una página a partir de su cuerpo.

    Una fila por página destino, no por forma del enlace: si el cuerpo nombra
    el mismo destino dos veces (`[[REUNIÓN DE EQUIPO]]` y `[[Reunión de
    equipo]]`, o la misma cadena en NFC y en NFD) hay un solo vínculo, como
    cuando la resolución era un `IN` sobre `pages` (que devuelve cada página
    una vez). De las formas se queda la primera del cuerpo, para que dos
    guardados del mismo texto den la misma fila.

    Limitación conocida, igual que antes de P1.1: renombrar una página no
    repara los enlaces ya guardados en otras; se corrigen cuando esa página se
    vuelve a guardar (o, desde P1.2, al reindexar el vault entero).
    """
    conn.execute(
        "DELETE FROM links WHERE from_id = ? AND kind = ?",
        (page_id, LinkKind.wikilink.value),
    )
    titles = extract_titles(body_markdown or "")
    if not titles:
        return
    # Una consulta por guardado: título plegado -> ids de las páginas del
    # workspace (todas, sin `LIMIT`). Una lista porque puede haber repetidos.
    targets: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT id, title FROM pages WHERE workspace_id = ? AND id != ?",
        (workspace_id, page_id),
    ):
        targets.setdefault(fold_title(row["title"]), []).append(row["id"])
    # Destino -> forma del título que se guarda. El `dict` conserva el orden
    # del cuerpo y `setdefault` deja la primera forma encontrada: dos guardados
    # del mismo cuerpo escriben la misma fila (y el `UNIQUE` no sirve para
    # deduplicar aquí, porque `target_title` es distinto en cada forma).
    chosen: dict[str, str] = {}
    for title in titles:
        for target_id in targets.get(fold_title(title), ()):
            chosen.setdefault(target_id, title)
    for target_id, title in chosen.items():
        conn.execute(
            "INSERT INTO links (id, from_id, to_id, kind, target_title)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                link_id(page_id, target_id, LinkKind.wikilink.value, title),
                page_id,
                target_id,
                LinkKind.wikilink.value,
                title,
            ),
        )


def link_id(from_id: str, to_id: str, kind: str, target_title: str) -> str:
    """Id determinista de un enlace.

    Con `uuid4()` el contenido del índice dependía del azar y dos reindexados
    del mismo vault daban huellas distintas (el ADR exige que sean idénticos).
    Un uuid5 sobre la clave natural del enlace —la que ya era UNIQUE en el
    esquema— es estable y sigue siendo único.
    """
    return str(uuid.uuid5(_LINK_NAMESPACE, f"{from_id}|{to_id}|{kind}|{target_title}"))
