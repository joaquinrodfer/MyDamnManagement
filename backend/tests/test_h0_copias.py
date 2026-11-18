"""H0: copias de seguridad.

- una copia lleva el índice (consistente), los adjuntos y un manifiesto, y se
  puede restaurar: arrancar con ese índice da las mismas notas;
- solo la ve y la descarga un administrador;
- las programadas van a `BACKUP_DIR`, se podan a las N últimas y tocan según
  la frecuencia;
- antes de migrar el esquema se guarda el índice tal como estaba.
"""

import sqlite3
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

from app import backup, index
from app.config import settings


@pytest.fixture
def carpeta(tmp_path, monkeypatch):
    destino = tmp_path / "copias"
    monkeypatch.setattr(settings, "backup_dir", str(destino))
    monkeypatch.setattr(settings, "export_dir", str(tmp_path / "exports"))
    return destino


def test_descargar_y_restaurar(client, carpeta, tmp_path, vault_path):
    nota = client.post("/pages", json={"title": "Para la copia", "body_markdown": "texto"}).json()
    (vault_path / "_attachments").mkdir(exist_ok=True)
    (vault_path / "_attachments" / "adjunto.txt").write_text("hola")

    r = client.post("/backups/download")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/gzip")
    fichero = tmp_path / "bajada.tar.gz"
    fichero.write_bytes(r.content)

    with tarfile.open(fichero) as tar:
        nombres = set(tar.getnames())
        assert {"manifest.json", "index.sqlite", "vault/_attachments/adjunto.txt"} <= nombres
        destino = tmp_path / "restaurado"
        tar.extractall(destino, filter="data")
    manifiesto = backup.read_manifest(str(fichero))
    assert manifiesto["schema_version"] == index.SCHEMA_VERSION
    assert manifiesto["pages"] >= 1 and manifiesto["users"] >= 1

    # Restaurar = abrir ese índice: es un índice válido y trae la nota.
    info = index.ensure_index(str(destino / "index.sqlite"))
    assert info.version == index.SCHEMA_VERSION
    conn = sqlite3.connect(destino / "index.sqlite")
    assert conn.execute("SELECT count(*) FROM pages WHERE title = 'Para la copia'").fetchone()[0] == 1
    conn.close()
    assert (destino / "vault" / "_attachments" / "adjunto.txt").read_text() == "hola"
    assert nota["id"]


def test_solo_un_administrador(client, carpeta):
    client.post("/auth/users", json={"username": "ana", "display_name": "Ana", "password": "contrasena-ana"})
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as ana:
        assert ana.post("/auth/login", json={"username": "ana", "password": "contrasena-ana"}).status_code == 200
        assert ana.get("/backups").status_code == 403
        assert ana.post("/backups/download").status_code == 403
        assert ana.put("/backups/settings", json={"frequency": "daily", "keep": 3}).status_code == 403
        assert ana.post("/backups/run").status_code == 403


def test_programadas_se_guardan_y_se_podan(client, carpeta):
    estado = client.get("/backups").json()
    assert estado["configured"] and estado["frequency"] == "off" and estado["files"] == []

    assert client.put("/backups/settings", json={"frequency": "daily", "keep": 2}).json()["frequency"] == "daily"
    # Tres copias con nombres de segundos distintos: se quedan las 2 últimas.
    carpeta.mkdir(exist_ok=True)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        backup.make_backup(str(carpeta / backup.file_name(base + timedelta(days=i))), kind="programada")
    assert len(backup.listing()) == 3
    hecho = client.post("/backups/run").json()
    nombres = [f["name"] for f in hecho["files"]]
    assert len(nombres) == 2 and hecho["made"]["name"] in nombres
    # Se descarga una guardada, y solo por su nombre.
    assert client.get(f"/backups/files/{nombres[0]}").status_code == 200
    assert client.get("/backups/files/..%2Findex.sqlite").status_code == 404
    assert client.get("/backups/files/otra-cosa.tar.gz").status_code == 404


def test_sin_backup_dir_no_hay_programadas(client, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", "")
    assert client.get("/backups").json()["configured"] is False
    assert client.put("/backups/settings", json={"frequency": "daily", "keep": 3}).status_code == 409
    assert client.post("/backups/run").status_code == 409
    assert client.put("/backups/settings", json={"frequency": "off", "keep": 3}).status_code == 200


def test_cuando_toca(client, carpeta):
    from app.index import open_index

    conn = open_index(settings.index_path)
    try:
        assert backup.is_due(conn) is False  # apagado
        client.put("/backups/settings", json={"frequency": "weekly", "keep": 3})
        assert backup.is_due(conn) is True  # nunca se ha hecho
        client.post("/backups/run")
        ahora = datetime.now(timezone.utc)
        assert backup.is_due(conn, ahora + timedelta(days=6)) is False
        assert backup.is_due(conn, ahora + timedelta(days=7, minutes=1)) is True
    finally:
        conn.close()


def test_el_programador_hace_la_copia_cuando_toca(client, carpeta):
    client.put("/backups/settings", json={"frequency": "daily", "keep": 5})
    assert backup.BackupScheduler().tick() is True
    assert len(backup.listing()) == 1
    assert backup.BackupScheduler().tick() is False  # acaba de hacerse


def test_copia_del_indice_antes_de_migrar(tmp_path, monkeypatch):
    """Un índice de una versión vieja se guarda tal cual antes de migrarlo."""
    monkeypatch.setattr(settings, "backup_dir", "")
    ruta = tmp_path / "viejo.sqlite"
    index.ensure_index(str(ruta))
    conn = sqlite3.connect(ruta)
    conn.execute("PRAGMA user_version = 12")  # como si estuviera una versión atrás
    conn.execute("ALTER TABLE users DROP COLUMN avatar")
    conn.commit()
    conn.close()
    index._ready.clear()

    info = index.ensure_index(str(ruta))
    assert info.version == index.SCHEMA_VERSION
    copia = tmp_path / f"viejo.sqlite.antes-de-v{index.SCHEMA_VERSION}-desde-v12"
    assert copia.exists()
    # La copia es la de antes: sigue en la versión vieja y sin la columna nueva.
    c = sqlite3.connect(copia)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 12
    assert "avatar" not in [r[1] for r in c.execute("PRAGMA table_info(users)")]
    c.close()
