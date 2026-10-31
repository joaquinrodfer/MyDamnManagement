"""Script de migración Postgres -> SQLite: idempotencia, contadores y avisos.

No hay Postgres en este contenedor, así que lo que se prueba es la mitad que sí
se puede probar aquí: la escritura en el índice (con un dump con la forma que
devuelve la lectura de Postgres), que repetirla no duplica nada, y que sin
Postgres accesible el script lo dice y no escribe. La lectura real de Postgres
queda sin ejecutar y así se declara en el resumen de la tarjeta.
"""

import importlib.util
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_postgres_to_sqlite.py"


def _cargar_modulo():
    spec = importlib.util.spec_from_file_location("migracion_mdm", SCRIPT)
    modulo = importlib.util.module_from_spec(spec)
    # `dataclass` busca su módulo en sys.modules al resolver anotaciones.
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


migracion = _cargar_modulo()


def _dump():
    ws = str(uuid.uuid4())
    db_page = str(uuid.uuid4())
    db_id = str(uuid.uuid4())
    row_id = str(uuid.uuid4())
    nota = str(uuid.uuid4())
    creado = "2026-10-01T10:00:00+00:00"
    dump = migracion.Dump(
        workspaces=[{"id": ws, "name": "default", "created_at": creado}],
        pages=[
            {
                "id": db_page, "workspace_id": ws, "parent_id": None, "type": "database",
                "title": "Contactos", "icon": "🗄", "description": None, "created_by": "Tú",
                "header_image_path": None, "body": "", "path": None, "mtime": None,
                "size": None, "content_hash": None, "created_at": creado, "updated_at": creado,
            },
            {
                "id": row_id, "workspace_id": ws, "parent_id": None, "type": "database_row",
                "title": "Acme", "icon": None, "description": None, "created_by": None,
                "header_image_path": None, "body": "", "path": None, "mtime": None,
                "size": None, "content_hash": None, "created_at": creado, "updated_at": creado,
            },
            {
                "id": nota, "workspace_id": ws, "parent_id": None, "type": "note",
                "title": "Reunión de equipo", "icon": "📄", "description": "una línea",
                "created_by": "Tú", "header_image_path": None,
                "body": "Hablamos del [[Contactos]] y del presupuesto", "path": None,
                "mtime": None, "size": None, "content_hash": None,
                "created_at": creado, "updated_at": creado,
            },
        ],
        rows=[{
            "id": row_id, "database_id": db_id,
            "properties_json": '{"empresa": "Acme S.L."}', "path": None,
        }],
        databases=[{
            "id": db_id, "page_id": db_page,
            "schema_json": '[{"key": "empresa", "name": "Empresa", "type": "text"}]',
        }],
        views=[{
            "id": str(uuid.uuid4()), "database_id": db_id, "name": "Tabla",
            "type": "table", "config_json": "{}",
        }],
        links=[{
            "id": str(uuid.uuid4()), "from_id": nota, "to_id": db_page,
            "kind": "wikilink", "target_title": "Contactos",
        }],
    )
    return dump


def _volcado(index_path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(index_path)
    try:
        return {
            tabla: sorted(
                conn.execute(f"SELECT * FROM {tabla}").fetchall(), key=repr
            )
            for tabla in ("workspaces", "pages", "databases", "views", "rows", "links")
        }
    finally:
        conn.close()


def test_migracion_idempotente_con_contadores(tmp_path):
    index_path = tmp_path / "indice.sqlite"
    dump = _dump()

    primera = migracion.aplicar(str(index_path), dump)
    assert primera.nuevas == {
        "workspaces": 1, "pages": 3, "rows": 1, "databases": 1, "views": 1, "links": 1,
    }
    assert sum(primera.actualizadas.values()) == 0
    assert "nuevas:     3" in primera.resumen() or "pages" in primera.resumen()

    despues_primera = _volcado(index_path)

    segunda = migracion.aplicar(str(index_path), dump)
    assert segunda.nuevas == {}
    assert segunda.actualizadas == {
        "workspaces": 1, "pages": 3, "rows": 1, "databases": 1, "views": 1, "links": 1,
    }
    # Mismo contenido byte a byte la segunda vez (idempotente, no duplica).
    assert _volcado(index_path) == despues_primera

    conn = sqlite3.connect(index_path)
    try:
        assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 3
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        # Los triggers de FTS5 han indexado el texto migrado, sin acentos.
        filas = conn.execute(
            "SELECT p.title FROM pages_fts JOIN pages p ON p.rowid = pages_fts.rowid"
            " WHERE pages_fts MATCH ?", ('"reunion"*',)
        ).fetchall()
        assert [f[0] for f in filas] == ["Reunión de equipo"]
    finally:
        conn.close()


def test_migracion_dry_run_no_escribe(tmp_path):
    index_path = tmp_path / "indice.sqlite"
    contadores = migracion.aplicar(str(index_path), _dump(), dry_run=True)
    assert contadores.nuevas["pages"] == 3
    assert not index_path.exists()


def test_sin_datos_de_postgres_no_toca_el_indice(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MDM_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    index_path = tmp_path / "indice.sqlite"

    codigo = migracion.main(["--index-path", str(index_path)])

    assert codigo == migracion.EXIT_SIN_POSTGRES
    salida = capsys.readouterr()
    assert "No hay Postgres" in salida.err
    assert not index_path.exists()


def test_sin_psycopg2_lo_dice_claro(tmp_path):
    if importlib.util.find_spec("psycopg2") is not None:
        pytest.skip("psycopg2 está instalado en este entorno")

    with pytest.raises(migracion.MigrationError) as excinfo:
        migracion.cargar_desde_postgres("postgresql://mdm:clave@localhost:5432/mdm")

    assert "psycopg2" in str(excinfo.value)
    assert "requirements-migration.txt" in str(excinfo.value)


def test_la_url_se_imprime_sin_contrasena():
    assert migracion._redact("postgresql://mdm:secreto@db:5432/mdm") == (
        "postgresql://mdm:***@db:5432/mdm"
    )


# --------------------------------------------------------------------------
# Rework P1.1: orden de inserción, filas sin destino y errores de datos
# --------------------------------------------------------------------------


def _pagina(ws: str, page_id: str, titulo: str, **extra) -> dict:
    """Una fila de `pages` con la forma que devuelve la lectura de Postgres."""
    fila = {
        "id": page_id, "workspace_id": ws, "parent_id": None, "type": "note",
        "title": titulo, "icon": "📄", "description": None, "created_by": "Tú",
        "header_image_path": None, "body": "", "path": None, "mtime": None,
        "size": None, "content_hash": None,
        "created_at": "2026-10-01T10:00:00+00:00",
        "updated_at": "2026-10-01T10:00:00+00:00",
    }
    fila.update(extra)
    return fila


def _dump_en_arbol():
    """Abuelo > padre > hijo, con el hijo ANTES que su padre en la lista.

    Es el orden que devuelve `SELECT * FROM pages` de Postgres cuando el padre
    se editó después de crear al hijo (su tupla se va al final del heap), así
    que la migración no puede depender del orden en que llegan las filas.
    """
    ws = str(uuid.uuid4())
    abuelo, padre, hijo = (str(uuid.uuid4()) for _ in range(3))
    dump = migracion.Dump(
        workspaces=[{"id": ws, "name": "default", "created_at": "2026-10-01T10:00:00+00:00"}],
        pages=[
            _pagina(ws, hijo, "Nieto", parent_id=padre, body="Vuelta al [[Abuelo]]"),
            _pagina(ws, padre, "Padre", parent_id=abuelo),
            _pagina(ws, abuelo, "Abuelo"),
        ],
        links=[{
            "id": str(uuid.uuid4()), "from_id": hijo, "to_id": abuelo,
            "kind": "wikilink", "target_title": "Abuelo",
        }],
    )
    return dump, (abuelo, padre, hijo)


def test_migracion_con_hijos_antes_que_sus_padres(tmp_path):
    """Un árbol de 3 niveles llega con el hijo primero: no puede abortar."""
    index_path = tmp_path / "indice.sqlite"
    dump, (abuelo, padre, hijo) = _dump_en_arbol()

    primera = migracion.aplicar(str(index_path), dump)
    assert primera.nuevas == {"workspaces": 1, "pages": 3, "links": 1}
    assert sum(primera.actualizadas.values()) == 0

    conn = sqlite3.connect(index_path)
    try:
        padres = dict(conn.execute("SELECT id, parent_id FROM pages"))
        assert padres == {abuelo: None, padre: abuelo, hijo: padre}
        assert conn.execute("SELECT count(*) FROM links").fetchone()[0] == 1
    finally:
        conn.close()

    # Y la segunda pasada no duplica ni cambia nada.
    despues_primera = _volcado(index_path)
    segunda = migracion.aplicar(str(index_path), dump)
    assert segunda.nuevas == {}
    assert segunda.actualizadas == {"workspaces": 1, "pages": 3, "links": 1}
    assert _volcado(index_path) == despues_primera


def test_fila_sin_database_id_se_omite_y_se_cuenta(tmp_path):
    """`rows.database_id` era NULL-able (ON DELETE SET NULL) en el esquema viejo.

    El índice no lo permite: la fila se omite, se cuenta y se avisa con su id,
    en vez de tumbar la migración entera.
    """
    index_path = tmp_path / "indice.sqlite"
    ws = str(uuid.uuid4())
    huerfana = str(uuid.uuid4())
    dump = migracion.Dump(
        workspaces=[{"id": ws, "name": "default", "created_at": "2026-10-01T10:00:00+00:00"}],
        pages=[_pagina(ws, huerfana, "Fila huérfana", type="database_row")],
        rows=[{
            "id": huerfana, "database_id": None,
            "properties_json": "{}", "path": None,
        }],
    )

    contadores = migracion.aplicar(str(index_path), dump)

    assert contadores.omitidas == {"rows": 1}
    assert any(huerfana in aviso for aviso in contadores.avisos)
    assert "OMITIDAS" in contadores.resumen()
    assert "rows: 1" in contadores.resumen()

    conn = sqlite3.connect(index_path)
    try:
        # La página se migra (existía en /pages); su entrada en `rows` no.
        assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM rows").fetchone()[0] == 0
    finally:
        conn.close()


def test_clave_foranea_rota_es_error_de_datos_y_no_escribe(tmp_path):
    """Un enlace a una página que no está ni en el dump ni en el índice."""
    index_path = tmp_path / "indice.sqlite"
    ws = str(uuid.uuid4())
    nota = str(uuid.uuid4())
    fantasma = str(uuid.uuid4())
    dump = migracion.Dump(
        workspaces=[{"id": ws, "name": "default", "created_at": "2026-10-01T10:00:00+00:00"}],
        pages=[_pagina(ws, nota, "Origen")],
        links=[{
            "id": str(uuid.uuid4()), "from_id": nota, "to_id": fantasma,
            "kind": "wikilink", "target_title": "Fantasma",
        }],
    )

    with pytest.raises(migracion.MigrationError) as excinfo:
        migracion.aplicar(str(index_path), dump)

    mensaje = str(excinfo.value)
    assert "links" in mensaje and fantasma in mensaje
    assert "No se ha escrito nada" in mensaje

    conn = sqlite3.connect(index_path)
    try:
        assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM links").fetchone()[0] == 0
    finally:
        conn.close()


def test_un_error_de_datos_sale_con_codigo_3_y_sin_traceback(tmp_path, monkeypatch, capsys):
    """`main()` convierte el fallo en el mensaje de EXIT_ERROR_DATOS."""
    index_path = tmp_path / "indice.sqlite"
    dump, _ = _dump_en_arbol()
    dump.links[0]["to_id"] = str(uuid.uuid4())  # destino inexistente
    monkeypatch.setattr(migracion, "cargar_desde_postgres", lambda dsn: dump)

    codigo = migracion.main([
        "--database-url", "postgresql://mdm:secreto@db:5432/mdm",
        "--index-path", str(index_path),
    ])

    salida = capsys.readouterr()
    assert codigo == migracion.EXIT_ERROR_DATOS
    assert "ERROR:" in salida.err
    assert "links" in salida.err
    assert "secreto" not in salida.out + salida.err
