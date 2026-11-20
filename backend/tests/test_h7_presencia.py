"""H7: presencia. Quién está en línea, en qué página (solo si se puede leer) y su estado."""

import pytest

from app import presence
from test_h1_espacios import _sesion, cierra  # noqa: F401 - fixtures


@pytest.fixture(autouse=True)
def limpio():
    presence.reset()
    yield
    presence.reset()


def test_el_latido_pone_a_alguien_en_linea_y_caduca():
    assert presence.beat("u1", "p1", now=100.0) is True       # recién llegado: avisa
    assert presence.beat("u1", "p1", now=110.0) is False      # misma página: no
    assert presence.beat("u1", "p2", now=120.0) is True       # cambió de página
    assert presence.online(now=130.0) == {"u1": "p2"}
    assert presence.online(now=120.0 + presence.TTL + 1) == {}  # sin latido, deja de estar
    assert presence.beat("u1", None, now=500.0) is True       # vuelve a conectarse


def test_en_que_pagina_esta_solo_lo_ve_quien_puede_leerla(client, cierra):
    ana, u = _sesion(client, "anita")
    cierra.append(ana)
    compartida = client.post("/pages", json={"title": "Pública", "section": "shared"}).json()
    privada = client.post("/pages", json={"title": "Reservada", "section": "private"}).json()
    yo = client.get("/auth/me").json()
    # El administrador mira la privada; Anita, la compartida.
    assert client.post("/presence/heartbeat", json={"page_id": privada["id"]}).status_code == 204
    assert ana.post("/presence/heartbeat", json={"page_id": compartida["id"]}).status_code == 204
    vista_de_ana = {p["id"]: p for p in ana.get("/presence").json()["online"]}
    assert vista_de_ana[u["id"]]["viewing"]["title"] == "Pública" and vista_de_ana[u["id"]]["you"] is True
    assert vista_de_ana[yo["id"]]["viewing"] is None  # la privada no la ve: solo «en línea»
    vista_del_admin = {p["id"]: p for p in client.get("/presence").json()["online"]}
    assert vista_del_admin[yo["id"]]["viewing"]["title"] == "Reservada"
    assert vista_del_admin[u["id"]]["viewing"]["title"] == "Pública"


def test_solo_se_ve_a_quien_comparte_espacio(client, cierra):
    ws = client.post("/workspaces", json={"name": "Otro"}).json()["id"]
    ana, u = _sesion(client, "anita")
    solo, v = _sesion(client, "solitario", workspaces=[{"id": ws, "role": "editor"}])
    cierra += [ana, solo]
    ana.post("/presence/heartbeat", json={})
    solo.post("/presence/heartbeat", json={})
    # Anita (en General) no ve al solitario (solo en «Otro»); el administrador ve a los dos.
    assert [p["id"] for p in ana.get("/presence").json()["online"]] == [u["id"]]
    assert {p["id"] for p in client.get("/presence").json()["online"]} == {u["id"], v["id"]}


def test_estado_manual(client, cierra):
    ana, u = _sesion(client, "anita")
    cierra.append(ana)
    assert ana.get("/presence").json()["me"] == {"status": "available", "text": ""}
    assert ana.put("/presence/status", json={"status": "focus"}).json() == {"status": "focus", "text": ""}
    assert ana.put("/presence/status", json={"status": "custom", "text": "Cerrando la release"}).json()["text"] == "Cerrando la release"
    assert ana.put("/presence/status", json={"status": "custom", "text": "  "}).status_code == 400
    assert ana.put("/presence/status", json={"status": "volando"}).status_code == 422
    ana.post("/presence/heartbeat", json={})
    en_linea = ana.get("/presence").json()["online"][0]
    assert en_linea["status"] == "custom" and en_linea["status_text"] == "Cerrando la release"
    assert ana.put("/presence/status", json={"status": "available"}).json()["status"] == "available"
    # Y salir quita a alguien de la lista.
    ana.post("/presence/leave")
    assert ana.get("/presence").json()["online"] == []


def test_panel_de_miembros_por_espacio(client, cierra):
    ws = client.post("/workspaces", json={"name": "Otro"}).json()["id"]
    ana, u = _sesion(client, "anita")
    solo, v = _sesion(client, "solitario", workspaces=[{"id": ws, "role": "editor"}])
    cierra += [ana, solo]
    ana.post("/presence/heartbeat", json={})
    ana.put("/presence/status", json={"status": "meeting"})
    # Anita ve sus espacios compartidos (no los personales) con sus miembros,
    # conectados o no; al solitario no lo ve porque no comparten espacio.
    espacios = ana.get("/presence").json()["workspaces"]
    assert all(not e["name"].startswith("Espacio de") for e in espacios)
    general = next(e for e in espacios if e["name"] == "General")
    miembros = {m["id"]: m for m in general["members"]}
    assert miembros[u["id"]]["online"] is True and miembros[u["id"]]["status"] == "meeting" and miembros[u["id"]]["you"]
    assert v["id"] not in miembros
    assert all(e["name"] != "Otro" for e in espacios)
    # El administrador sí ve «Otro», con el solitario desconectado.
    otro = next(e for e in client.get("/presence").json()["workspaces"] if e["name"] == "Otro")
    assert {m["id"]: m["online"] for m in otro["members"]}[v["id"]] is False
