"""Bloque 1 antes de un equipo: enlaces de invitación y de contraseña nueva,
redirección a HTTPS y restaurar una copia."""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager

import pytest

from app.config import settings


def _ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def _token(link):
    assert link["path"].startswith("/enlace/")
    return link["path"].removeprefix("/enlace/")


@contextmanager
def _anonimo():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_invitar_a_un_espacio(client):
    ws = client.post("/workspaces", json={"name": "Diseño"}).json()
    link = _ok(client.post("/auth/invites", json={"workspace_id": ws["id"], "role": "viewer", "display_name": "Ana"}), 201)
    tok = _token(link)
    assert [i["id"] for i in _ok(client.get("/auth/invites"))] == [link["id"]]
    with _anonimo() as anon:
        # La página es pública y no se cachea.
        r = anon.get(link["path"])
        assert r.status_code == 200 and "Notemil" in r.text and r.headers["cache-control"] == "no-store"
        info = _ok(anon.get(f"/auth/link/{tok}"))
        assert info == {"kind": "invite", "expires_at": link["expires_at"], "display_name": "Ana",
                        "role": "viewer", "workspace_name": "Diseño"}
        assert anon.post(f"/auth/link/{tok}", json={"username": "ana", "password": "corta"}).status_code == 400
        yo = _ok(anon.post(f"/auth/link/{tok}", json={"username": "ana", "display_name": "Ana", "password": "contrasena-ana"}), 201)
        assert yo["username"] == "ana"
        # Ya dentro: es lectora de «Diseño».
        espacios = _ok(anon.get("/auth/me/permissions"))["workspaces"]
        assert {(e["name"], e["role"]) for e in espacios} >= {("Diseño", "viewer")}
        # Un solo uso.
        assert anon.get(f"/auth/link/{tok}").status_code == 404
        assert anon.post(f"/auth/link/{tok}", json={"username": "otra", "password": "contrasena-x"}).status_code == 404
    assert _ok(client.get("/auth/invites")) == []


def test_un_fallo_no_gasta_la_invitacion(client):
    tok = _token(_ok(client.post("/auth/invites", json={}), 201))
    with _anonimo() as anon:
        # Usuario repetido: error y el enlace sigue valiendo.
        assert anon.post(f"/auth/link/{tok}", json={"username": "prueba", "password": "contrasena-x"}).status_code == 400
        _ok(anon.get(f"/auth/link/{tok}"))


def test_anular_una_invitacion_y_permisos(client):
    link = _ok(client.post("/auth/invites", json={}), 201)
    assert client.delete(f"/auth/invites/{link['id']}").status_code == 204
    with _anonimo() as anon:
        assert anon.get(f"/auth/link/{_token(link)}").status_code == 404
        # Sin sesión no se invita; y un no-admin sin «Gestionar personas», tampoco.
        assert anon.post("/auth/invites", json={}).status_code == 401
    _ok(client.post("/auth/users", json={"username": "beto", "display_name": "Beto", "password": "contrasena-beto"}), 201)
    with _anonimo() as beto:
        _ok(beto.post("/auth/login", json={"username": "beto", "password": "contrasena-beto"}))
        assert beto.post("/auth/invites", json={}).status_code == 403
        assert beto.post("/auth/users/x/reset-link").status_code == 403


def test_enlace_de_contrasena_nueva(client):
    beto = _ok(client.post("/auth/users", json={"username": "beto", "display_name": "Beto", "password": "contrasena-beto"}), 201)
    viejo = _ok(client.post(f"/auth/users/{beto['id']}/reset-link"), 201)
    link = _ok(client.post(f"/auth/users/{beto['id']}/reset-link"), 201)
    with _anonimo() as sesion_vieja, _anonimo() as anon:
        _ok(sesion_vieja.post("/auth/login", json={"username": "beto", "password": "contrasena-beto"}))
        # Solo vale el último.
        assert anon.get(f"/auth/link/{_token(viejo)}").status_code == 404
        info = _ok(anon.get(f"/auth/link/{_token(link)}"))
        assert info["kind"] == "reset" and info["username"] == "beto"
        _ok(anon.post(f"/auth/link/{_token(link)}", json={"password": "otra-contrasena"}))
        assert _ok(anon.get("/auth/me"))["username"] == "beto"
        # Las otras sesiones se cierran y la contraseña vieja ya no vale.
        assert sesion_vieja.get("/auth/me").status_code == 401
        assert anon.post("/auth/login", json={"username": "beto", "password": "contrasena-beto"}).status_code == 401
        _ok(anon.post("/auth/login", json={"username": "beto", "password": "otra-contrasena"}))


def test_enlace_caducado(client, index_path):
    import sqlite3

    tok = _token(_ok(client.post("/auth/invites", json={}), 201))
    with sqlite3.connect(index_path) as conn:
        conn.execute("UPDATE access_links SET expires_at = '2020-01-01T00:00:00+00:00'")
    with _anonimo() as anon:
        assert anon.get(f"/auth/link/{tok}").status_code == 404


def test_http_directo_va_a_https(client, monkeypatch):
    monkeypatch.setattr(settings, "public_url", "https://equipo.tailnet.ts.net/")
    r = client.get("/login?next=/x", headers={"host": "100.66.181.123:8000"}, follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "https://equipo.tailnet.ts.net/login?next=/x"
    # Lo que llega del proxy con HTTPS, y lo local (healthcheck), no se toca.
    assert client.get("/health", headers={"host": "equipo.tailnet.ts.net", "x-forwarded-proto": "https"}).status_code == 200
    assert client.get("/health", headers={"host": "127.0.0.1:8000"}).status_code == 200
    monkeypatch.setattr(settings, "public_url", "")
    assert client.get("/health", headers={"host": "100.66.181.123:8000"}).status_code == 200


def test_restaurar_una_copia(client, tmp_path, monkeypatch):
    from app import backup

    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "copias"))
    _ok(client.post("/pages", json={"title": "Para restaurar", "body_markdown": "hola"}))
    hecho = _ok(client.post("/backups/run"))
    copia = os.path.join(settings.backup_dir, hecho["made"]["name"])
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import restaurar_copia

    assert restaurar_copia.main([copia, "--comprobar"]) == 0
    destino = tmp_path / "restaurada"
    assert restaurar_copia.main([copia, "--en", str(destino)]) == 0
    assert (destino / "index.sqlite").is_file()
    import sqlite3

    conn = sqlite3.connect(destino / "index.sqlite")
    assert conn.execute("SELECT count(*) FROM pages WHERE title = 'Para restaurar'").fetchone()[0] == 1
    conn.close()
    # Encima de algo, no.
    assert restaurar_copia.main([copia, "--en", str(destino)]) == 1
    assert backup.read_manifest(copia)["pages"] >= 1


def test_restaurar_rechaza_una_copia_tocada(tmp_path):
    import io
    import tarfile

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import restaurar_copia

    mala = tmp_path / "mala.tar.gz"
    with tarfile.open(mala, "w:gz") as tar:
        datos = b"x"
        info = tarfile.TarInfo("../fuera.txt")
        info.size = len(datos)
        tar.addfile(info, io.BytesIO(datos))
    assert restaurar_copia.main([str(mala), "--comprobar"]) == 1
    assert not (tmp_path.parent / "fuera.txt").exists()


def test_gestionar_personas_invita_pero_no_hace_administradores(client):
    ws = client.post("/workspaces", json={"name": "Equipo"}).json()
    beto = _ok(client.post("/auth/users", json={"username": "beto", "display_name": "Beto", "password": "contrasena-beto"}), 201)
    _ok(client.put(f"/workspaces/{ws['id']}/members/user/{beto['id']}", json={"role": "editor"}))
    _ok(client.post("/grants", json={"capability": "manage_people", "subject_type": "user", "subject_id": beto["id"],
                                     "workspace_id": ws["id"]}), 201)
    with _anonimo() as c:
        _ok(c.post("/auth/login", json={"username": "beto", "password": "contrasena-beto"}))
        _ok(c.post("/auth/invites", json={"workspace_id": ws["id"], "role": "viewer"}), 201)
        assert c.post("/auth/invites", json={"workspace_id": ws["id"], "role": "admin"}).status_code == 403
        assert c.post("/auth/invites", json={}).status_code == 403  # sin espacio, solo un admin
        assert len(_ok(c.get("/auth/invites"))) == 1  # ve las suyas
