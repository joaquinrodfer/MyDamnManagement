"""Migración índice -> vault de las bases y sus filas: idempotencia y contadores.

El índice se construye a mano con la forma que dejaba P1.1 (bases y filas solo
en SQLite, con el `id` de la base distinto del de su página), que es el estado
del que hay que migrar. No hace falta Postgres ni red.
"""

import importlib.util
import json
import sqlite3
import sys
import uuid
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "migrate_databases_to_vault.py"
)

AHORA = "2026-10-01T10:00:00+00:00"


def _cargar_modulo():
    spec = importlib.util.spec_from_file_location("migracion_bases", SCRIPT)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


migracion = _cargar_modulo()


def _crear_indice(path: Path, *, con_nota: bool = False) -> dict[str, str]:
    """Índice con una base, dos filas, dos vistas y (opcional) su nota padre."""
    from app.index import DEFAULT_WORKSPACE_ID, ensure_index, open_index

    ensure_index(str(path))
    conn = open_index(str(path))
    ids = {
        "nota": str(uuid.uuid4()),
        "base_pagina": str(uuid.uuid4()),
        "base": str(uuid.uuid4()),
        "vista1": str(uuid.uuid4()),
        "vista2": str(uuid.uuid4()),
        "fila1": str(uuid.uuid4()),
        "fila2": str(uuid.uuid4()),
    }
    try:
        conn.execute(
            "INSERT INTO workspaces (id, name, created_at) VALUES (?, 'default', ?)",
            (DEFAULT_WORKSPACE_ID, AHORA),
        )
        if con_nota:
            conn.execute(
                "INSERT INTO pages (id, workspace_id, type, title, body, path,"
                " created_at, updated_at) VALUES (?, ?, 'note', 'Proyectos', '',"
                " 'Notas/Proyectos.md', ?, ?)",
                (ids["nota"], DEFAULT_WORKSPACE_ID, AHORA, AHORA),
            )
        conn.execute(
            "INSERT INTO pages (id, workspace_id, parent_id, type, title, icon, body,"
            " created_at, updated_at) VALUES (?, ?, ?, 'database', 'Contactos', '📇', '', ?, ?)",
            (
                ids["base_pagina"],
                DEFAULT_WORKSPACE_ID,
                ids["nota"] if con_nota else None,
                AHORA,
                AHORA,
            ),
        )
        conn.execute(
            "INSERT INTO databases (id, page_id, schema_json) VALUES (?, ?, ?)",
            (
                ids["base"],
                ids["base_pagina"],
                json.dumps(
                    [
                        {"key": "empresa", "name": "Empresa", "type": "text"},
                        {"key": "fase", "name": "Fase", "type": "select",
                         "options": ["lead", "ganado"]},
                        {"key": "valor", "name": "Valor", "type": "number"},
                    ],
                    ensure_ascii=False,
                ),
            ),
        )
        for vista, nombre, tipo, config in (
            (ids["vista1"], "Pipeline", "board", {"group_by": "fase"}),
            (ids["vista2"], "Tabla", "table", {}),
        ):
            conn.execute(
                "INSERT INTO views (id, database_id, name, type, config_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (vista, ids["base"], nombre, tipo, json.dumps(config)),
            )
        for fila, titulo, props in (
            (ids["fila1"], "Acme", {"empresa": "Acme S.L.", "fase": "lead", "valor": 1200.0}),
            (ids["fila2"], "Globex", {"empresa": "Globex", "fase": "ganado"}),
        ):
            conn.execute(
                "INSERT INTO pages (id, workspace_id, type, title, body, created_at, updated_at)"
                " VALUES (?, ?, 'database_row', ?, '', ?, ?)",
                (fila, DEFAULT_WORKSPACE_ID, titulo, AHORA, AHORA),
            )
            conn.execute(
                "INSERT INTO rows (id, database_id, properties_json) VALUES (?, ?, ?)",
                (fila, ids["base"], json.dumps(props, ensure_ascii=False)),
            )
    finally:
        conn.close()
    return ids


def _migrar(index: Path, vault: Path, *extra: str) -> int:
    return migracion.main(["--index", str(index), "--vault", str(vault), *extra])


def _frontmatter(path: Path) -> dict:
    from app.vault import fm

    raw, _cuerpo = fm.split_document(path.read_text(encoding="utf-8"))
    return fm.parse(raw).data


def test_la_migracion_escribe_la_base_sus_vistas_y_sus_filas(tmp_path, vault_path, capsys):
    index = tmp_path / "indice.sqlite"
    vault = vault_path
    ids = _crear_indice(index)

    assert _migrar(index, vault) == 0
    salida = capsys.readouterr().out
    assert "bases_escritas  : 1" in salida, salida
    assert "filas_escritas  : 2" in salida, salida
    assert "vistas_escritas : 2" in salida, salida

    base = vault / "Bases" / "Contactos.md"
    assert base.exists()
    assert (vault / "Bases" / "Contactos").is_dir()
    datos = _frontmatter(base)
    assert datos["type"] == "database"
    assert datos["id"] == ids["base_pagina"], "el id de la base es el de su página"
    assert [p["key"] for p in datos["schema"]] == ["empresa", "fase", "valor"]
    assert [v["name"] for v in datos["views"]] == ["Pipeline", "Tabla"]
    assert datos["views"][0]["group_by"] == "fase"
    assert datos["views"][0]["id"] == ids["vista1"]

    fila = vault / "Bases" / "Contactos" / "Acme.md"
    assert fila.exists()
    propios = _frontmatter(fila)
    assert propios["id"] == ids["fila1"]
    assert propios["empresa"] == "Acme S.L."
    assert propios["fase"] == "lead"
    assert propios["valor"] == "1200"
    assert "title" not in propios

    assert (vault / "Bases" / "Contactos" / "Globex.md").exists()


def test_la_migracion_es_idempotente(tmp_path, vault_path, capsys):
    index = tmp_path / "indice.sqlite"
    vault = vault_path
    _crear_indice(index)

    assert _migrar(index, vault) == 0
    capsys.readouterr()
    ficheros_antes = sorted(p.relative_to(vault) for p in vault.rglob("*.md"))

    assert _migrar(index, vault) == 0
    salida = capsys.readouterr().out
    assert "bases_escritas  : 0" in salida, salida
    assert "bases_saltadas  : 1" in salida, salida
    assert "filas_escritas  : 0" in salida, salida
    assert "filas_saltadas  : 2" in salida, salida
    assert sorted(p.relative_to(vault) for p in vault.rglob("*.md")) == ficheros_antes


def test_la_migracion_respeta_el_nombre_que_ya_tiene_el_fichero(tmp_path, vault_path, capsys):
    """Idempotente también con un renombrado hecho a mano: el `id` manda."""
    index = tmp_path / "indice.sqlite"
    vault = vault_path
    ids = _crear_indice(index)

    assert _migrar(index, vault) == 0
    capsys.readouterr()
    original = vault / "Bases" / "Contactos.md"
    renombrada = vault / "Bases" / "Clientes 2026.md"
    original.rename(renombrada)

    assert _migrar(index, vault) == 0
    capsys.readouterr()
    assert renombrada.exists()
    assert not original.exists()
    assert _frontmatter(renombrada)["id"] == ids["base_pagina"]
    # Las filas se siguen colgando de la carpeta de la base, no de la del título.
    assert (vault / "Bases" / "Contactos" / "Acme.md").exists()


def test_con_reindex_el_indice_queda_como_lo_dejaron_los_ficheros(tmp_path, vault_path, capsys):
    from app import index as indice
    from app.index import open_index
    from app.vault.store import VaultStore

    index = tmp_path / "indice.sqlite"
    vault = vault_path
    ids = _crear_indice(index)

    assert _migrar(index, vault, "--reindex") == 0
    salida = capsys.readouterr().out
    assert "reindexado" in salida, salida

    conn = open_index(str(index))
    try:
        bases = [dict(r) for r in conn.execute("SELECT id, page_id FROM databases")]
        filas = [dict(r) for r in conn.execute("SELECT id, database_id FROM rows")]
        vistas = [dict(r) for r in conn.execute("SELECT id, name FROM views")]
        # El id de la base ya es el de su página, y las filas siguen colgando
        # de la base (con el id nuevo).
        assert bases == [{"id": ids["base_pagina"], "page_id": ids["base_pagina"]}]
        assert sorted(f["id"] for f in filas) == sorted([ids["fila1"], ids["fila2"]])
        assert {f["database_id"] for f in filas} == {ids["base_pagina"]}
        assert sorted(v["name"] for v in vistas) == ["Pipeline", "Tabla"]
    finally:
        conn.close()

    # Y un segundo reindexado da la misma huella: no hay nada que se pierda.
    huella = indice.dump_hash(str(index))
    conn = open_index(str(index))
    try:
        VaultStore(str(vault), conn).rebuild()
    finally:
        conn.close()
    assert indice.dump_hash(str(index)) == huella


def test_una_base_que_cuelga_de_una_nota_va_a_su_carpeta(tmp_path, vault_path, capsys):
    index = tmp_path / "indice.sqlite"
    vault = vault_path
    _crear_indice(index, con_nota=True)

    assert _migrar(index, vault) == 0
    capsys.readouterr()
    assert (vault / "Notas" / "Proyectos" / "Contactos.md").exists()
    assert (vault / "Notas" / "Proyectos" / "Contactos" / "Acme.md").exists()


def test_dry_run_no_escribe_nada(tmp_path, vault_path, capsys):
    index = tmp_path / "indice.sqlite"
    vault = vault_path
    _crear_indice(index)

    assert _migrar(index, vault, "--dry-run") == 0
    salida = capsys.readouterr().out
    assert "(dry-run) Bases/Contactos.md" in salida, salida
    assert list(vault.rglob("*.md")) == []


def test_sin_bases_lo_dice_y_no_escribe(tmp_path, vault_path, capsys):
    from app.index import ensure_index

    index = tmp_path / "indice.sqlite"
    vault = vault_path
    ensure_index(str(index))

    assert _migrar(index, vault) == 0
    assert "no tiene bases de datos" in capsys.readouterr().out
    assert list(vault.rglob("*.md")) == []


def test_un_indice_que_no_existe_es_codigo_2(tmp_path, vault_path, capsys):
    vault = vault_path
    assert _migrar(tmp_path / "no-existe.sqlite", vault) == 2
    assert "No existe el índice" in capsys.readouterr().err


def test_un_indice_ajeno_es_codigo_2_y_no_revienta(tmp_path, vault_path, capsys):
    index = tmp_path / "indice.sqlite"
    vault = vault_path
    conn = sqlite3.connect(str(index))
    conn.execute("CREATE TABLE otra_cosa (x TEXT)")
    conn.commit()
    conn.close()

    assert _migrar(index, vault) == 2
    assert "No se pudo leer el índice" in capsys.readouterr().err
