"""Índices de consulta añadidos tras medir (páginas ordenadas por fechas).

`pages_created` y `pages_updated` existen porque `GET /pages` (ORDER BY
created_at) y `list_recent_pages` del panel (ORDER BY updated_at LIMIT n)
pasaban de SCAN + TEMP B-TREE a usar el índice, con 12.200 páginas: 173,6 ->
17,4 ms y 2,4 -> 0,02 ms (medido; ver el comentario en `index.SCHEMA_SQL`).

Comprueban que los dos índices existen tras un arranque en limpio y tras
reconstruir borrando el fichero, y que el plan de las dos consultas los usa:
que existan no sirve de nada si el optimizador no los elige.
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


def test_version_de_esquema_sin_migracion_para_los_indices(client, index_path):
    """No hay migración que aplicar: los índices se describen en SCHEMA_SQL y
    el fichero se puede rehacer desde el vault, así que `user_version` sigue
    siendo 2 (subirlo obligaría a migrar índices existentes por un cambio que
    es reconstruible)."""
    info = indice.ensure_index(str(index_path))
    assert info.version == indice.SCHEMA_VERSION == 2
    assert 3 not in indice.MIGRATIONS
