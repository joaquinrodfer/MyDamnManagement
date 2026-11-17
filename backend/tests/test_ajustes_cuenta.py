"""`PATCH /auth/me`: cada usuario cambia su propio nombre visible (ajustes)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _crear(client, username: str, display_name: str) -> dict:
    respuesta = client.post(
        "/auth/users",
        json={"username": username, "display_name": display_name, "password": "contrasena-de-prueba"},
    )
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def test_el_admin_cambia_su_nombre(client):
    antes = client.get("/auth/me").json()
    respuesta = client.patch("/auth/me", json={"display_name": "Otro nombre"})
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["display_name"] == "Otro nombre"
    assert respuesta.json()["id"] == antes["id"], "el id no cambia"
    assert client.get("/auth/me").json()["display_name"] == "Otro nombre"


def test_un_usuario_normal_cambia_el_suyo_y_solo_el_suyo(client, acceso):
    ana = _crear(client, "ana", "Ana")
    yo = client.get("/auth/me").json()
    with TestClient(app) as ajena:
        acceso(ajena)
        ajena.post("/auth/logout")
        assert ajena.post(
            "/auth/login", json={"username": "ana", "password": "contrasena-de-prueba"}
        ).status_code == 200
        respuesta = ajena.patch("/auth/me", json={"display_name": "Ana María"})
        assert respuesta.status_code == 200, respuesta.text
        assert respuesta.json()["id"] == ana["id"]
        assert respuesta.json()["display_name"] == "Ana María"
    # El admin sigue como estaba.
    assert client.get("/auth/me").json()["display_name"] == yo["display_name"]


def test_un_nombre_vacio_es_un_400(client):
    respuesta = client.patch("/auth/me", json={"display_name": "   "})
    assert respuesta.status_code == 400, respuesta.text


def test_sin_sesion_es_un_401(client):
    client.post("/auth/logout")
    assert client.patch("/auth/me", json={"display_name": "X"}).status_code == 401
