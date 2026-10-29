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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
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
