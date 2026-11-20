"""Páginas fijadas: cada quien las suyas, en su orden, solo lo que ve."""

from __future__ import annotations

from contextlib import contextmanager


def _nota(c, titulo):
    r = c.post("/pages", json={"title": titulo, "type": "note"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_fijar_ordenar_y_quitar(client):
    a, b, c = _nota(client, "Uno"), _nota(client, "Dos"), _nota(client, "Tres")
    for pid in (a, b, c):
        assert client.put(f"/pins/{pid}").status_code == 200
    # Fijar dos veces no duplica.
    assert [p["title"] for p in client.put(f"/pins/{a}").json()] == ["Uno", "Dos", "Tres"]

    orden = client.put("/pins", json={"ids": [c, a]}).json()
    assert [p["title"] for p in orden] == ["Tres", "Uno", "Dos"]

    assert [p["title"] for p in client.delete(f"/pins/{a}").json()] == ["Tres", "Dos"]
    assert [p["title"] for p in client.get("/pins").json()] == ["Tres", "Dos"]


def test_no_se_fija_lo_que_no_se_ve_y_cada_quien_tiene_las_suyas(client):
    from fastapi.testclient import TestClient

    from app.main import app

    assert client.post("/auth/users", json={"username": "ana", "display_name": "Ana", "password": "contrasena-ana"}).status_code == 201

    @contextmanager
    def como_ana():
        with TestClient(app) as c:
            assert c.post("/auth/login", json={"username": "ana", "password": "contrasena-ana"}).status_code == 200
            yield c

    mia = _nota(client, "Del admin")
    client.put(f"/pins/{mia}")
    with como_ana() as ana:
        privada = _nota(ana, "Diario de Ana")
        assert ana.put(f"/pins/{privada}").status_code == 200
        assert [p["title"] for p in ana.get("/pins").json()] == ["Diario de Ana"]
        # La nota del administrador está en su espacio privado: Ana no la ve.
        assert ana.put(f"/pins/{mia}").status_code == 404
    assert [p["title"] for p in client.get("/pins").json()] == ["Del admin"]


def test_una_pagina_inexistente_no_se_fija(client):
    assert client.put("/pins/00000000-0000-0000-0000-000000000000").status_code == 404
