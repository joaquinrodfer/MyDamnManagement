"""Arrastrar en el panel: orden entre hermanas, cambio de madre, de sección y de
espacio (`POST /pages/{id}/move`)."""

import pytest

from test_h1_espacios import _aplanar, _sesion, cierra  # noqa: F401 - fixtures


def _raices(c, ws=None):
    h = {"X-Workspace": ws} if ws else {}
    return [n["title"] for n in c.get("/pages/tree", headers=h).json()]


def _nueva(c, titulo, **kw):
    return c.post("/pages", json={"title": titulo, **kw}).json()


def test_reordenar_raices(client):
    a, b, c_ = (_nueva(client, t, section="shared") for t in ("A", "B", "C"))
    assert _raices(client) == ["A", "B", "C"]
    assert client.post(f"/pages/{c_['id']}/move", json={"before_id": a["id"], "section": "shared"}).status_code == 204
    assert _raices(client) == ["C", "A", "B"]
    assert client.post(f"/pages/{c_['id']}/move", json={"section": "shared"}).status_code == 204
    assert _raices(client) == ["A", "B", "C"]


def test_anidar_y_sacar(client):
    a, b = _nueva(client, "A", section="shared"), _nueva(client, "B", section="shared")
    assert client.post(f"/pages/{b['id']}/move", json={"parent_id": a["id"]}).status_code == 204
    arbol = client.get("/pages/tree").json()
    assert [n["title"] for n in arbol] == ["A"] and arbol[0]["children"][0]["title"] == "B"
    assert client.post(f"/pages/{b['id']}/move", json={"section": "shared"}).status_code == 204
    assert _raices(client) == ["A", "B"]


def test_no_se_cuelga_de_si_misma_ni_de_una_hija(client):
    a = _nueva(client, "A", section="shared")
    h = _nueva(client, "H", parent_id=a["id"])
    assert client.post(f"/pages/{a['id']}/move", json={"parent_id": a["id"]}).status_code == 400
    assert client.post(f"/pages/{a['id']}/move", json={"parent_id": h["id"]}).status_code == 400


def test_orden_de_hijas(client):
    a = _nueva(client, "A", section="shared")
    h1, h2, h3 = (_nueva(client, t, parent_id=a["id"]) for t in ("H1", "H2", "H3"))
    client.post(f"/pages/{h3['id']}/move", json={"parent_id": a["id"], "before_id": h1["id"]})
    hijas = client.get("/pages/tree").json()[0]["children"]
    assert [n["title"] for n in hijas] == ["H3", "H1", "H2"]


def test_entre_privado_y_compartido_y_entre_espacios(client):
    ws = client.post("/workspaces", json={"name": "Equipo"}).json()["id"]
    p = _nueva(client, "Mía")
    assert client.post(f"/pages/{p['id']}/move", json={"section": "private"}).status_code == 204
    n = next(x for x in client.get("/pages/tree").json() if x["title"] == "Mía")
    assert n["section"] == "private"
    assert client.post(f"/pages/{p['id']}/move", json={"workspace_id": ws, "section": "shared"}).status_code == 204
    n = next(x for x in client.get("/pages/tree").json() if x["title"] == "Mía")
    assert n["workspace_id"] == ws and n["section"] == "shared"
    # Colgarla de una página de otro espacio la lleva allí.
    q = _nueva(client, "Otra", section="shared")
    assert client.post(f"/pages/{q['id']}/move", json={"parent_id": p["id"]}).status_code == 204
    assert client.get(f"/pages/{q['id']}").json()["workspace_id"] == ws


def test_quien_no_edita_no_mueve(client, cierra):
    a = _nueva(client, "A", section="shared")
    c, _ = _sesion(client, "ana")
    cierra.append(c)
    privada = _nueva(client, "Privada")
    assert c.post(f"/pages/{privada['id']}/move", json={"before_id": a["id"]}).status_code == 404
