"""El índice en sí: versión de esquema, FTS5, corrupción y arranque.

Sin Postgres y sin Docker: se abre un fichero en un directorio temporal y se
comprueba lo que el criterio de hecho de P1.1 pide sobre el índice.
"""

import sqlite3

import pytest

from app import index as indice
from app.index import IndexSchemaError, ensure_index, open_index
from app.repository import Repository


def _pagina(repo: Repository, titulo: str, cuerpo: str = ""):
    return repo.create_page(title=titulo, body_markdown=cuerpo)


def test_esquema_versionado_y_fts5(index_path):
    info = ensure_index(str(index_path))
    assert info.version == indice.SCHEMA_VERSION == 1
    assert info.recreated is True  # no existía

    conn = sqlite3.connect(index_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

        tablas = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"pages", "databases", "rows", "links", "pages_fts"} <= tablas

        fts = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'pages_fts'"
        ).fetchone()[0]
        assert "content = 'pages'" in fts  # external content: no duplica el texto
        assert "remove_diacritics 2" in fts
    finally:
        conn.close()

    # Abrirlo otra vez no lo rehace.
    otra = ensure_index(str(index_path))
    assert otra.recreated is False


def test_fts_se_actualiza_sin_reconstruir(index_path):
    conn = open_index(str(index_path))
    repo = Repository(conn)
    try:
        page = _pagina(repo, "Reunión", "hablamos del presupuesto")
        assert [p.title for p in repo.search("reunion")] == ["Reunión"]
        repo.update_page(page.id, title="Reunión anual")
        assert [p.title for p in repo.search("anual")] == ["Reunión anual"]
        assert repo.search("reunion")[0].title == "Reunión anual"
        repo.update_page(page.id, body_markdown="otra cosa")
        assert repo.search("presupuesto") == []
        repo.delete_page(page.id)
        assert repo.search("reunion") == []
    finally:
        repo.close()


def test_fichero_corrupto_se_aparta_y_se_rehace(index_path):
    index_path.write_bytes(b"esto no es una base de datos" * 100)

    info = ensure_index(str(index_path))
    assert info.recreated is True
    assert "ilegible" in info.reason

    # El original no se borra: se aparta con marca de tiempo.
    apartados = list(index_path.parent.glob("indice.sqlite.corrupto-*"))
    assert len(apartados) == 1
    assert apartados[0].read_bytes().startswith(b"esto no es")

    # Y el índice nuevo funciona.
    conn = open_index(str(index_path))
    repo = Repository(conn)
    try:
        _pagina(repo, "Después del susto")
    finally:
        repo.close()
    conn = open_index(str(index_path))
    repo = Repository(conn)
    try:
        assert [p.title for p in repo.list_pages()] == ["Después del susto"]
    finally:
        repo.close()


def test_la_app_arranca_con_un_indice_ilegible(index_path, monkeypatch):
    """El criterio literal: un fichero corrupto se detecta al arrancar y se
    rehace; uvicorn no se cae por encontrarse basura donde va el índice."""
    from fastapi.testclient import TestClient

    from app.config import settings

    index_path.write_bytes(b"basura, no soy una base de datos" * 20)
    monkeypatch.setattr(settings, "index_path", str(index_path))

    from app.main import app

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/pages").json() == []
        assert client.get("/status").json()["services"]["db"]["ok"] is True

    assert list(index_path.parent.glob("indice.sqlite.corrupto-*"))


def test_indice_de_version_mas_nueva_no_se_toca(index_path):
    ensure_index(str(index_path))
    conn = sqlite3.connect(index_path)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()

    with pytest.raises(IndexSchemaError) as excinfo:
        open_index(str(index_path))
    assert "99" in str(excinfo.value)

    # No se ha borrado ni recreado: la versión sigue siendo la del fichero.
    conn = sqlite3.connect(index_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 99
    conn.close()


def test_indice_sin_fts5_se_rechaza(index_path):
    ensure_index(str(index_path))
    conn = sqlite3.connect(index_path)
    conn.execute("DROP TABLE pages_fts")
    conn.commit()
    conn.close()

    import os

    os.utime(index_path, None)  # fuerza a mirarlo otra vez
    with pytest.raises(IndexSchemaError):
        open_index(str(index_path))


def test_fts_query_escapa_sintaxis_del_usuario():
    # Solo pasan caracteres de palabra: los operadores de FTS5 no llegan.
    assert indice.fts_query('hola "mundo" OR NEAR(x)') == '"hola"* "mundo"* "OR"* "NEAR"* "x"*'
    assert indice.fts_query("") == ""
    assert indice.fts_query("  ,,, ") == ""
