"""Índices de consulta añadidos tras medir (páginas ordenadas por fechas).

`pages_created` y `pages_updated` existen porque `GET /pages` (ORDER BY
created_at) y `list_recent_pages` del panel (ORDER BY updated_at LIMIT n)
pasaban de SCAN + TEMP B-TREE a usar el índice, con 12.200 páginas: 173,6 ->
17,4 ms y 2,4 -> 0,02 ms (medido; ver el comentario en `index.SCHEMA_SQL`).

Comprueban que los dos índices existen tras un arranque en limpio, tras
reconstruir borrando el fichero y —el caso de una instalación ya desplegada—
que un fichero con el esquema antiguo (v2, sin los dos índices) los recibe por
migración sin recrearse. Y que el plan de las consultas que los motivaron los
usa: que existan no sirve de nada si el optimizador no los elige.
"""

import sqlite3

from app import index as indice
from app.index import open_index

INDICES = ("pages_created", "pages_updated")


def _existentes(index_path) -> set[str]:
    conn = sqlite3.connect(index_path)
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND name IN (?, ?)", INDICES
            )
        }
    finally:
        conn.close()


def _plan(index_path, sql: str, params=()) -> list[str]:
    conn = sqlite3.connect(index_path)
    try:
        return [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
    finally:
        conn.close()


def test_indices_de_consulta_en_un_arranque_en_limpio(client, index_path):
    """El arranque crea los dos índices (el `client` arranca la app)."""
    assert _existentes(index_path) == set(INDICES)

    # Y las consultas que los motivaron los usan: sin TEMP B-TREE.
    paginas = _plan(
        index_path,
        "SELECT id FROM pages ORDER BY created_at DESC, rowid DESC",
    )
    assert any("pages_created" in paso for paso in paginas), paginas
    assert not any("TEMP B-TREE" in paso for paso in paginas), paginas

    recientes = _plan(
        index_path,
        "SELECT id FROM pages WHERE type != ?"
        " ORDER BY updated_at DESC, rowid DESC LIMIT 8",
        ("database_row",),
    )
    assert any("pages_updated" in paso for paso in recientes), recientes
    assert not any("TEMP B-TREE" in paso for paso in recientes), recientes


def test_indices_de_consulta_se_reconstruyen_al_borrar_el_fichero(client, index_path):
    """Borrar el índice y pedir de nuevo lo rehace con los índices: son parte
    del esquema, no un paso aparte del arranque."""
    assert _existentes(index_path) == set(INDICES)
    for sufijo in ("", "-wal", "-shm"):
        try:
            (index_path.with_name(index_path.name + sufijo)).unlink()
        except FileNotFoundError:
            pass

    conn = open_index(str(index_path))  # la siguiente petición rehace el fichero
    try:
        assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 0
    finally:
        conn.close()
    assert _existentes(index_path) == set(INDICES)


def test_fichero_v2_existente_gana_los_indices_por_migracion(client, index_path):
    """Camino de actualización, no de arranque en limpio: una instalación ya
    desplegada tiene un `indice.sqlite` en version 2 sin los dos índices, y
    `_inspect()` no vuelve a ejecutar `SCHEMA_SQL`. La migración 3 es la que
    se los tiene que poner sin recrear el fichero."""
    assert _existentes(index_path) == set(INDICES)

    # Dejo el fichero como lo dejó el esquema anterior: version 2 y sin
    # `pages_created` ni `pages_updated`.
    conn = sqlite3.connect(index_path)
    try:
        conn.execute("DROP INDEX pages_created")
        conn.execute("DROP INDEX pages_updated")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()
    assert _existentes(index_path) == set()

    info = indice.ensure_index(str(index_path))
    assert info.recreated is False  # se migra, no se rehace desde cero

    # (a) la version queda en 3 y (b) `sqlite_master` lista los dos índices.
    assert info.version == indice.SCHEMA_VERSION == 3
    assert _existentes(index_path) == set(INDICES)

    # Y la consulta que motivó `pages_created` ya no ordena en un TEMP B-TREE.
    paginas = _plan(
        index_path,
        "SELECT id FROM pages ORDER BY created_at DESC, rowid DESC",
    )
    assert any("pages_created" in paso for paso in paginas), paginas
    assert not any("TEMP B-TREE" in paso for paso in paginas), paginas
