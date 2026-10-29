"""Migración índice -> vault: los nombres se resuelven ANTES de escribir.

El fallo que fija este módulo (revisión P1.2a, ronda 1): la carpeta de una nota
se calculaba con el título saneado de su padre, pero el padre podía acabar
escrito como `Título (2).md`. Con dos hermanas del mismo título, las hijas de
la segunda acababan en `Notas/Título/`, la carpeta de la primera, y tras el
reindexado colgaban de la nota equivocada.
"""

import importlib.util
import sys
import uuid
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "migrate_index_to_vault.py"
)

AHORA = "2026-10-01T10:00:00+00:00"


def _cargar_modulo():
    spec = importlib.util.spec_from_file_location("migracion_vault", SCRIPT)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


migracion = _cargar_modulo()


def _crear_indice(path: Path, filas: list[tuple[str, str, str | None]]) -> dict[str, str]:
    """Crea el índice con las notas dadas: (clave, título, clave del padre)."""
    from app.index import DEFAULT_WORKSPACE_ID, ensure_index, open_index

    ensure_index(str(path))
    conn = open_index(str(path))
    ids: dict[str, str] = {clave: str(uuid.uuid4()) for clave, _t, _p in filas}
    try:
        conn.execute(
            "INSERT INTO workspaces (id, name, created_at) VALUES (?, 'default', ?)",
            (DEFAULT_WORKSPACE_ID, AHORA),
        )
        for clave, titulo, padre in filas:
            conn.execute(
                "INSERT INTO pages (id, workspace_id, parent_id, type, title, icon,"
                " description, created_by, body, created_at, updated_at)"
                " VALUES (?, ?, ?, 'note', ?, '📄', NULL, 'Tú', '', ?, ?)",
                (
                    ids[clave],
                    DEFAULT_WORKSPACE_ID,
                    ids[padre] if padre else None,
                    titulo,
                    AHORA,
                    AHORA,
                ),
            )
    finally:
        conn.close()
    return ids


def test_dos_hermanas_con_el_mismo_titulo_no_se_llevan_las_hijas(tmp_path, capsys):
    from app.index import dump_hash, open_index

    index_path = tmp_path / "indice.sqlite"
    ids = _crear_indice(
        index_path,
        [
            ("primera", "Misma", None),
            ("segunda", "Misma", None),
            ("hija", "Hija", "segunda"),
        ],
    )

    vault = tmp_path / "vault"
    codigo = migracion.main(
        ["--index", str(index_path), "--vault", str(vault), "--reindex"]
    )
    assert codigo == 0, capsys.readouterr()

    notas = vault / "Notas"
    assert (notas / "Misma.md").exists()
    assert (notas / "Misma (2).md").exists()
    assert (notas / "Misma (2)" / "Hija.md").exists(), "la hija colgó del padre equivocado"
    assert not (notas / "Misma" / "Hija.md").exists()

    # Y el índice, reconstruido desde el vault, cuelga la hija de la segunda.
    conn = open_index(str(index_path))
    try:
        padre = conn.execute(
            "SELECT parent_id FROM pages WHERE id = ?", (ids["hija"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert padre == ids["segunda"]

    # La segunda pasada no escribe nada y no cambia el índice.
    antes, cuentas = dump_hash(str(index_path))
    codigo = migracion.main(
        ["--index", str(index_path), "--vault", str(vault), "--reindex"]
    )
    assert codigo == 0
    salida = capsys.readouterr().out
    assert "escritas    : 0" in salida
    assert "saltadas    : 3" in salida
    assert dump_hash(str(index_path))[0] == antes
    assert sorted(p.name for p in notas.iterdir()) == [
        "Misma (2)",
        "Misma (2).md",
        "Misma.md",
    ]
    assert cuentas["pages"] == 3
