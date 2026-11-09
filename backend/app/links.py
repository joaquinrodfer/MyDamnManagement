"""Resolución de los enlaces del cuerpo contra el índice.

Vive aparte de `repository.py` porque la usan los dos lados de P1.2: el
repositorio (guardado de una nota) y `app/vault/store.py` (reindexado desde los
ficheros). Si estuviera en `repository`, el puente del vault tendría que
importar el repositorio y habría un ciclo.

Hay dos clases de enlace, y las dos se recalculan en el mismo sitio (guardar,
vigilante, reindexar):

- **wikilink** (`[[Título]]`): se resuelve por título, sin distinguir mayúsculas
  en todo Unicode, dentro del mismo workspace y enlazando a **todas** las
  páginas con ese título.
- **embed** (```mdm-database): la base incrustada en el cuerpo. El bloque lleva
  el id de la fila `databases`, así que aquí se resuelve a la **página** de esa
  base; el grafo y los backlinks son entre páginas.
"""

from __future__ import annotations

import sqlite3
import unicodedata
import uuid

from app.enums import LinkKind
from app.wikilinks import extract_database_ids, extract_titles

# Namespace fijo para ids de enlaces (uuid5): el índice tiene que ser
# reconstruible de forma idéntica, así que nada de uuid4 aquí.
_LINK_NAMESPACE = uuid.UUID("a3d1c0d2-3a0e-5b9e-8f5c-2c7b1f0d9e21")

# La página que representa la base incrustada. `databases.id` no es el id de
# página (son dos UUID distintos): este JOIN es toda la resolución. La base
# tiene que ser del mismo workspace que la nota que la incrusta, y una página no
# se enlaza a sí misma, igual que con los wikilinks.
_EMBED_TARGET_SQL = (
    "SELECT d.page_id FROM databases d JOIN pages p ON p.id = d.page_id"
    " WHERE d.id = ? AND p.workspace_id = ? AND p.id != ?"
)


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
    """Recalcula los enlaces salientes de una página a partir de su cuerpo.

    Son dos: los wikilinks de siempre y las bases incrustadas. Se borran y se
    reinsertan los dos en cada pasada, porque el cuerpo puede haber perdido
    cualquiera de ellos.

    **Wikilinks.** Una fila por página destino, no por forma del enlace: si el
    cuerpo nombra el mismo destino dos veces (`[[REUNIÓN DE EQUIPO]]` y
    `[[Reunión de equipo]]`, o la misma cadena en NFC y en NFD) hay un solo
    vínculo, como cuando la resolución era un `IN` sobre `pages` (que devuelve
    cada página una vez). De las formas se queda la primera del cuerpo, para
    que dos guardados del mismo texto den la misma fila.

    **Bases incrustadas.** Una fila por base distinta: dos bloques que
    incrustan la misma base son un solo enlace (el bloque se repite, la
    relación no); dos bases distintas son dos. `target_title` guarda el id de
    la base — es lo que dice el bloque y no cambia al renombrarla, así que el
    id determinista de la fila también se queda quieto.

    Limitación conocida, igual que antes de P1.1: renombrar una página no
    repara los wikilinks ya guardados en otras; se corrigen cuando esa página
    se vuelve a guardar (o, desde P1.2, al reindexar el vault entero). Con las
    bases no pasa: se resuelven por id.
    """
    conn.execute(
        "DELETE FROM links WHERE from_id = ? AND kind IN (?, ?)",
        (page_id, LinkKind.wikilink.value, LinkKind.embed.value),
    )
    _sync_title_links(conn, page_id, workspace_id, body_markdown)
    _sync_embedded_databases(conn, page_id, workspace_id, body_markdown)


def _sync_title_links(
    conn: sqlite3.Connection, page_id: str, workspace_id: str, body_markdown: str
) -> None:
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


def _sync_embedded_databases(
    conn: sqlite3.Connection, page_id: str, workspace_id: str, body_markdown: str
) -> None:
    """Arista página -> base por cada bloque ```mdm-database del cuerpo.

    Un id que no existe (base borrada, copiada de otro vault, escrito a mano)
    no produce arista y no es un error: el bloque se queda en el documento y la
    app lo pinta como «Base no encontrada».
    """
    for database_id in extract_database_ids(body_markdown or ""):
        row = conn.execute(
            _EMBED_TARGET_SQL, (database_id, str(workspace_id), page_id)
        ).fetchone()
        if row is None:
            continue
        target_id = row["page_id"]
        conn.execute(
            "INSERT INTO links (id, from_id, to_id, kind, target_title)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                link_id(page_id, target_id, LinkKind.embed.value, database_id),
                page_id,
                target_id,
                LinkKind.embed.value,
                database_id,
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
