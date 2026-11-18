"""3.0, hito 1: `POST /export`, el vault entero desde el índice, y su ida y vuelta.

Es la red de seguridad de la 3.0 (ADR-0005, etapa 1): antes de que la verdad
pase a la base de datos (hito 2) tiene que existir un export que reconstruya el
vault **desde el índice** y una prueba que demuestre que no pierde nada.

La prueba de ida y vuelta:

1. se monta un vault variado por la API (notas anidadas, enlaces, bases con
   todos los tipos de propiedad, vistas con su configuración, plantillas,
   regla de cerrado, una base incrustada en una nota, una imagen de cabecera,
   algo en la papelera) y a mano (claves de frontmatter que la app no conoce,
   comentarios YAML, una nota sin frontmatter);
2. se exporta con `POST /export`;
3. el export es **byte a byte** el vault original, fichero a fichero;
4. se reindexa el export en un índice NUEVO y se compara con el original:
   mismo número de páginas, mismos cuerpos, mismas filas, mismos enlaces, y
   además la misma huella completa (`index.dump_hash`);
5. el `.zip` se abre y se lee con la biblioteca estándar, sin la app, y trae
   exactamente lo mismo que la carpeta.
"""

import io
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app import index as indice
from app.config import settings
from app.repository import Repository
from app.vault.store import VaultStore

PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
)

SCHEMA = [
    {"key": "empresa", "name": "Empresa", "type": "text"},
    {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado", "perdido"]},
    {"key": "tags", "name": "Etiquetas", "type": "multiselect", "options": ["a", "b", "c"]},
    {"key": "importe", "name": "Importe", "type": "number"},
    {"key": "cierre", "name": "Cierre", "type": "date"},
    {"key": "firmado", "name": "Firmado", "type": "checkbox"},
    {"key": "web", "name": "Web", "type": "url"},
]


@pytest.fixture()
def export_dir(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "exports"
    monkeypatch.setattr(settings, "export_dir", str(path))
    return path


def _ok(respuesta, codigo=200):
    assert respuesta.status_code == codigo, respuesta.text
    return respuesta.json() if respuesta.content and codigo != 204 else None


def montar_vault(client, vault_path: Path) -> dict:
    """Un vault con un poco de todo. Devuelve ids útiles para las aserciones."""
    raiz = _ok(client.post("/pages", json={
        "title": "Proyecto Ñandú", "icon": "🦤", "description": "La raíz: «comillas» y acentos",
        "body_markdown": "# Proyecto\n\nVer [[Hija]] y [[Contactos]].\n\n- [ ] tarea\n- [x] hecha",
    }))
    hija = _ok(client.post("/pages", json={
        "title": "Hija", "parent_id": raiz["id"],
        "body_markdown": "Texto de la hija.\n\nVuelve a [[Proyecto Ñandú]].",
    }))
    _ok(client.post("/pages", json={
        "title": "Nieta: con / barra", "parent_id": hija["id"], "body_markdown": "```python\nprint('hola')\n```",
    }))
    base = _ok(client.post("/databases", json={"title": "Contactos", "schema_def": SCHEMA}))
    otra = _ok(client.post("/databases", json={"title": "Empresas", "schema_def": [
        {"key": "pais", "name": "País", "type": "text"},
    ]}))
    acme = _ok(client.post(f"/databases/{otra['id']}/rows", json={"title": "Acme", "properties": {"pais": "ES"}}))
    # Relación a la otra base (después de crear filas en ella).
    _ok(client.patch(f"/databases/{base['id']}", json={"schema_def": SCHEMA + [
        {"key": "empresa_rel", "name": "Empresa (rel)", "type": "relation",
         "target_database": otra["id"], "multiple": True},
    ]}))
    filas = []
    for i, fase in enumerate(["lead", "ganado", "perdido"]):
        fila = _ok(client.post(f"/databases/{base['id']}/rows", json={
            "title": f"Contacto {i}",
            "properties": {
                "empresa": f"Empresa {i}", "fase": fase, "tags": ["a", "c"][: i + 1] if i < 2 else [],
                "importe": i * 1000.5, "cierre": f"2026-11-{10 + i}", "firmado": i % 2 == 0,
                "web": "https://example.com/x?y=1|2", "empresa_rel": [acme["id"]],
            },
        }))
        filas.append(fila)
    _ok(client.patch(f"/databases/{base['id']}/rows/{filas[0]['id']}", json={
        "body_markdown": "Cuerpo de la fila con [[Hija]].",
    }))
    _ok(client.post(f"/databases/{base['id']}/views", json={
        "name": "Tablero", "type": "board",
        "config": {"group_by": "fase", "sort": [{"property": "importe", "direction": "desc"}]},
    }))
    _ok(client.post(f"/databases/{base['id']}/views", json={
        "name": "Anchos", "type": "table",
        "config": {"widths": {"title": 300, "empresa": 180}, "visible_properties": ["empresa", "fase"]},
    }))
    _ok(client.put(f"/databases/{base['id']}/closed", json={"property": "fase", "values": ["ganado", "perdido"]}))
    _ok(client.put(f"/databases/{base['id']}/row-templates", json={"row_templates": [
        {"name": "Lead nuevo", "properties": {"fase": "lead"}, "body": "Llamar el lunes."},
    ]}))
    # Una base incrustada en una nota: la valla viaja tal cual.
    _ok(client.post("/pages", json={
        "title": "Panel", "body_markdown":
            f"Arriba.\n\n```mdm-database\nid: {base['id']}\nview: Tablero\ntitle: Contactos\n```\n\nAbajo.",
    }))
    # Imagen de cabecera: un adjunto binario en `_attachments/`.
    _ok(client.post(f"/pages/{raiz['id']}/header-image",
                    files={"file": ("x.png", io.BytesIO(PNG_1x1), "image/png")}))
    # Algo en la papelera.
    borrada = _ok(client.post("/pages", json={"title": "Borrada", "body_markdown": "adiós"}))
    _ok(client.delete(f"/pages/{borrada['id']}"), 204)

    # A mano: claves que la app no conoce, comentarios y estilo YAML propios, y
    # una nota sin frontmatter. Se indexan con `POST /reindex`.
    notas = vault_path / "Notas"
    notas.mkdir(exist_ok=True)
    (notas / "Escrita a mano.md").write_text(
        "---\n"
        "id: 3f1d2c4b-5a6e-4f70-8a9b-0c1d2e3f4a5b\n"
        "# un comentario que nadie gestiona\n"
        "aliases: [uno, \"dos, tres\"]\n"
        "custom:\n"
        "  anidado: true\n"
        "  lista:\n"
        "    - x\n"
        "    - y\n"
        "created: 2026-01-02T03:04:05+00:00\n"
        "updated: 2026-01-02T03:04:05+00:00\n"
        "---\n"
        "\n"
        "Con [[Hija]] y espacios al final   \n"
        "\ty tabuladores.\n",
        encoding="utf-8",
    )
    (notas / "Sin frontmatter.md").write_text("Solo texto, sin `---`.\n", encoding="utf-8")
    _ok(client.post("/reindex"))
    return {"raiz": raiz, "base": base, "filas": filas}


def _files(root: Path) -> dict[str, bytes]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def _reindexar(vault: Path, index_file: Path) -> None:
    indice.ensure_index(str(index_file))
    conn = indice.connect(str(index_file))
    try:
        Repository(conn, VaultStore(str(vault), conn)).reindex()
    finally:
        conn.close()


def _q(index_file: Path, sql: str) -> list:
    conn = sqlite3.connect(str(index_file))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_ida_y_vuelta_del_export(client, vault_path, index_path, export_dir, tmp_path):
    montar_vault(client, vault_path)

    respuesta = client.post("/export", json={"format": "folder"})
    stats = _ok(respuesta)
    destino = Path(stats["destination"])
    assert destino.parent == export_dir
    assert stats["unindexed"] == []
    assert stats["databases"] == 2 and stats["rows"] == 4
    assert stats["files"] >= 2  # la cabecera y lo de la papelera

    # 3) Byte a byte, fichero a fichero: el export ES el vault.
    original, exportado = _files(vault_path), _files(destino)
    assert sorted(original) == sorted(exportado)
    distintos = [rel for rel in original if original[rel] != exportado[rel]]
    assert distintos == []
    assert any(rel.startswith("_attachments/") for rel in exportado)
    assert any(rel.startswith(".trash/") for rel in exportado)
    texto = exportado["Notas/Escrita a mano.md"].decode()
    assert "# un comentario que nadie gestiona" in texto and "  lista:\n    - x" in texto

    # 4) Borrar el índice y reconstruirlo DESDE EL EXPORT da lo mismo.
    nuevo = tmp_path / "desde-el-export.sqlite"
    _reindexar(destino, nuevo)
    for sql in (
        "SELECT count(*) FROM pages",
        "SELECT id, type, path, body, frontmatter FROM pages ORDER BY id",
        "SELECT id, database_id, properties_json, path FROM rows ORDER BY id",
        "SELECT from_id, to_id, kind, target_title FROM links ORDER BY 1, 2, 3, 4",
        "SELECT id, page_id, schema_json FROM databases ORDER BY id",
        "SELECT id, database_id, name, type, config_json FROM views ORDER BY id",
        "SELECT * FROM database_closed ORDER BY 1",
        "SELECT * FROM database_row_templates ORDER BY 1",
    ):
        assert _q(index_path, sql) == _q(nuevo, sql), sql
    assert _q(nuevo, "SELECT count(*) FROM links")[0][0] >= 4
    # La huella completa del índice (incluidos `mtime`, `size` y `content_hash`
    # de cada página, y el texto indexado para la búsqueda).
    assert indice.dump_hash(str(index_path)) == indice.dump_hash(str(nuevo))


def test_el_zip_se_lee_sin_la_app(client, vault_path, export_dir):
    montar_vault(client, vault_path)
    carpeta = Path(_ok(client.post("/export", json={"format": "folder"}))["destination"])

    respuesta = client.post("/export")  # zip por defecto
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.headers["content-type"] == "application/zip"
    assert "attachment" in respuesta.headers["content-disposition"]
    assert respuesta.headers["x-export-unindexed"] == "0"

    zf = zipfile.ZipFile(io.BytesIO(respuesta.content))
    assert zf.testzip() is None
    contenido = {info.filename: zf.read(info) for info in zf.infolist() if not info.is_dir()}
    assert contenido == _files(carpeta)
    # Texto legible tal cual, con su frontmatter y sus enlaces.
    raiz = contenido["Notas/Proyecto Ñandú.md"].decode("utf-8")
    assert raiz.startswith("---\nid: ") and "[[Hija]]" in raiz
    # El zip temporal no se queda en el servidor.
    assert [p for p in export_dir.iterdir() if p.suffix == ".zip"] == []


def test_export_no_pierde_lo_que_el_indice_no_conoce(client, vault_path, index_path, export_dir):
    """Un documento que la base tiene pero el índice no se exporta y se DICE.

    Hito 2: la verdad son los documentos de la base (`documents`). Uno que no
    está en `pages` (no se pudo indexar) sale igual en el export y se cuenta en
    `unindexed`; la ida y vuelta exige que la lista esté vacía. (En el hito 1
    este test ponía el `.md` en el disco: el disco era la verdad.)
    """
    _ok(client.post("/pages", json={"title": "Normal", "body_markdown": "x"}))
    conn = sqlite3.connect(index_path)
    conn.execute(
        "INSERT INTO documents (path, text, mtime, size) VALUES (?, ?, 0, 0)",
        ("Notas/Fantasma.md", "---\nid: x\n---\n\nno indexada\n"),
    )
    conn.commit()
    conn.close()
    stats = _ok(client.post("/export", json={"format": "folder"}))
    assert stats["unindexed"] == ["Notas/Fantasma.md"]
    assert (Path(stats["destination"]) / "Notas" / "Fantasma.md").exists()


def test_solo_un_administrador_exporta(client, export_dir, acceso):
    _ok(client.post("/auth/users", json={
        "username": "marta", "display_name": "Marta", "password": "contrasena-de-marta",
    }), 201)
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as marta:
        _ok(marta.post("/auth/login", json={"username": "marta", "password": "contrasena-de-marta"}))
        assert marta.post("/export").status_code == 403
        assert marta.post("/export", json={"format": "folder"}).status_code == 403
    assert not export_dir.exists() or list(export_dir.iterdir()) == []


def test_export_sin_sesion_no_entra(client, export_dir):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonimo:
        assert anonimo.post("/export").status_code == 401


def test_destino_no_vacio_y_dentro_del_vault(vault_path, index_path, tmp_path):
    from app.vault_export import ExportError, export_to_folder

    indice.ensure_index(str(index_path))
    conn = indice.connect(str(index_path))
    try:
        ocupado = tmp_path / "ocupado"
        ocupado.mkdir()
        (ocupado / "algo.txt").write_text("x")
        with pytest.raises(ExportError):
            export_to_folder(conn, str(vault_path), str(ocupado))
        with pytest.raises(ExportError):
            export_to_folder(conn, str(vault_path), str(vault_path / "dentro"))
    finally:
        conn.close()


def test_pages_frontmatter_migra_desde_v8(index_path):
    """Un índice v8 (sin `pages.frontmatter`) gana la columna al abrirse."""
    indice.ensure_index(str(index_path))
    conn = sqlite3.connect(str(index_path))
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    conn.close()
    indice._ready.clear()
    info = indice.ensure_index(str(index_path))
    assert info.version == indice.SCHEMA_VERSION
    columnas = [r[1] for r in _q(index_path, "PRAGMA table_info(pages)")]
    assert "frontmatter" in columnas


def test_linea_de_ordenes(client, vault_path, index_path, tmp_path):
    from app.vault_export import main

    montar_vault(client, vault_path)
    destino = tmp_path / "cli.zip"
    assert main([str(destino), "--index", str(index_path), "--vault", str(vault_path)]) == 0
    with zipfile.ZipFile(destino) as zf:
        assert zf.testzip() is None
        assert "Notas/Hija.md" in {Path(n).as_posix() for n in zf.namelist()} or any(
            n.endswith("/Hija.md") for n in zf.namelist()
        )
