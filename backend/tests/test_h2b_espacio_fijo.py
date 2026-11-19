"""Una página no cambia de espacio por guardarse sin la cabecera `X-Workspace`
(el guardado de emergencia, el WebSocket de colaboración o un cliente externo no
la mandan): el espacio es el de la página, no el que pida cada petición."""

import pytest

from test_h1_espacios import _sesion, cierra  # noqa: F401 - fixtures


def _ws(client, pid):
    return client.get(f"/pages/{pid}").json()["workspace_id"]


@pytest.fixture
def espacio(client):
    return client.post("/workspaces", json={"name": "Equipo"}).json()["id"]


def test_renombrar_sin_cabecera_no_la_mueve_a_general(client, espacio):
    p = client.post("/pages", json={"title": "Mi nota", "section": "shared"}, headers={"X-Workspace": espacio}).json()
    assert _ws(client, p["id"]) == espacio
    assert client.patch(f"/pages/{p['id']}", json={"title": "Otra"}).status_code == 200
    assert _ws(client, p["id"]) == espacio


def test_renombrar_una_base_y_una_hija_sin_cabecera(client, espacio):
    h = {"X-Workspace": espacio}
    d = client.post("/databases", json={"title": "Base", "section": "shared", "schema_def": []}, headers=h).json()
    n = client.post("/pages", json={"title": "Raíz"}, headers=h).json()
    hija = client.post("/pages", json={"title": "Hija", "parent_id": n["id"]}, headers=h).json()
    client.patch(f"/pages/{d['id']}", json={"title": "Base 2"})
    client.patch(f"/pages/{n['id']}", json={"title": "Raíz 2"})
    client.patch(f"/pages/{hija['id']}", json={"title": "Hija 2"})
    assert [_ws(client, x["id"]) for x in (d, n, hija)] == [espacio] * 3


def test_sacar_una_hija_a_la_raiz_y_duplicar_sin_cabecera(client, espacio):
    h = {"X-Workspace": espacio}
    n = client.post("/pages", json={"title": "Madre"}, headers=h).json()
    hija = client.post("/pages", json={"title": "Hija", "parent_id": n["id"]}, headers=h).json()
    assert client.patch(f"/pages/{hija['id']}", json={"parent_id": None}).status_code == 200
    assert _ws(client, hija["id"]) == espacio
    assert client.patch(f"/pages/{hija['id']}", json={"parent_id": n["id"], "title": "Hija bis"}).status_code == 200
    assert _ws(client, hija["id"]) == espacio
