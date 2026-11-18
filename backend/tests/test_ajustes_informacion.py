"""Ajustes > Información: `GET /about`.

Para cualquiera con sesión: versión (la que pone el CI en la imagen), estado de
la API y de la base, arranque. Para los administradores, además, las cifras de
la instalación. Un usuario normal no ve las cifras.
"""

import pytest

from app.routers import about


@pytest.fixture(autouse=True)
def _sin_cache_de_version(monkeypatch):
    monkeypatch.setattr(about, "_VERSION", None)


def test_la_version_sale_de_la_imagen(client, monkeypatch):
    monkeypatch.setenv("MDM_VERSION", "edge")
    monkeypatch.setenv("MDM_REVISION", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setenv("MDM_BUILT", "2026-11-18T10:00:00Z")
    info = client.get("/about").json()
    assert info["version"]["version"] == "edge"
    assert info["version"]["revision_short"] == "0123456"
    assert info["version"]["built_at"] == "2026-11-18T10:00:00Z"
    assert info["version"]["schema_version"] >= 12
    assert info["api"]["ok"] is True and info["db"]["ok"] is True
    assert info["db"]["journal_mode"] == "wal"
    assert info["server"]["startup"] == "ok"


def test_sin_imagen_dice_desarrollo(client, monkeypatch):
    for var in ("MDM_VERSION", "MDM_REVISION", "MDM_BUILT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(about, "_git", lambda *a: None)
    v = client.get("/about").json()["version"]
    assert v["version"] == "desarrollo" and v["revision"] is None


def test_el_administrador_ve_las_cifras(client):
    for i in range(3):
        assert client.post("/pages", json={"title": f"N{i}", "body_markdown": "[[N0]]"}).status_code == 200
    base = client.post("/databases", json={"title": "B", "schema_def": []}).json()
    client.post(f"/databases/{base['id']}/rows", json={"title": "F"})
    a = client.get("/about").json()["admin"]
    assert a["counts"]["notes"] == 3 and a["counts"]["databases"] == 1 and a["counts"]["rows"] == 1
    assert a["counts"]["users"] == 1 and a["counts"]["admins"] == 1
    assert a["counts"]["links"] >= 2
    assert a["activity"]["created_7d"] == 5
    assert len(a["by_month"]) == 12  # los 12 meses, también los que van a cero
    assert sum(m["note"] for m in a["by_month"]) == 3
    assert a["top_creators"][0]["count"] == 4  # 3 notas + 1 base (las filas no)
    assert a["sharing"]["private"] == 4 and a["sharing"]["shared"] == 0
    assert a["storage"]["index_bytes"] > 0
    assert set(a["collab"]) >= {"rooms", "peers", "persist_ms_p95"}


def test_un_usuario_normal_no_ve_las_cifras(client):
    from fastapi.testclient import TestClient

    from app.main import app

    client.post("/auth/users", json={"username": "ana", "display_name": "Ana", "password": "contrasena-ana"})
    with TestClient(app) as ana:
        assert ana.post("/auth/login", json={"username": "ana", "password": "contrasena-ana"}).status_code == 200
        info = ana.get("/about").json()
        assert "admin" not in info
        assert info["you"] == {"name": "Ana", "is_admin": False}
        assert info["api"]["ok"] is True


def test_sin_sesion_no_hay_informacion(client):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonimo:
        assert anonimo.get("/about").status_code == 401
