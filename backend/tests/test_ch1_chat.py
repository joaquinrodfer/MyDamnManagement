"""Plan del chat, CH1 (canales de espacio) y CH2 (mensajes directos)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest


def _ok(r, code=None):
    assert r.status_code in ((code,) if code else (200, 201, 204)), f"{r.status_code} {r.text}"
    return r.json() if r.content else None


@pytest.fixture()
def gente(client):
    from fastapi.testclient import TestClient

    from app.main import app

    ids = {"admin": _ok(client.get("/auth/me"))["id"]}
    for nombre in ("ana", "beto", "carla"):
        ids[nombre] = _ok(client.post("/auth/users", json={
            "username": nombre, "display_name": nombre.title(), "password": f"contrasena-{nombre}",
        }))["id"]
    dev = _ok(client.post("/workspaces", json={"name": "Desarrollo"}))
    _ok(client.put(f"/workspaces/{dev['id']}/members/user/{ids['ana']}", json={"role": "editor"}))
    _ok(client.put(f"/workspaces/{dev['id']}/members/user/{ids['beto']}", json={"role": "viewer"}))

    @contextmanager
    def como(nombre):
        with TestClient(app) as c:
            _ok(c.post("/auth/login", json={"username": nombre, "password": f"contrasena-{nombre}"}))
            yield c

    with como("ana") as ana, como("beto") as beto, como("carla") as carla:
        yield {"ana": ana, "beto": beto, "carla": carla, "ids": ids, "dev": dev}


def _canal(c, nombre):
    return next((x for x in _ok(c.get("/chat/conversations")) if x["kind"] == "workspace" and x["title"] == nombre), None)


def test_el_canal_del_espacio_es_de_sus_editores(client, gente):
    ana, beto, carla, dev = gente["ana"], gente["beto"], gente["carla"], gente["dev"]
    canal = _canal(ana, "Desarrollo")
    assert canal is not None and canal["min_role"] == "editor"
    # Beto es lector: por defecto no lo ve. Carla no es miembro.
    assert _canal(beto, "Desarrollo") is None
    assert _canal(carla, "Desarrollo") is None
    assert beto.get(f"/chat/conversations/{canal['id']}/messages").status_code == 404
    assert carla.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "hola"}).status_code == 404

    msg = _ok(ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "Hola, equipo"}))
    assert msg["author"]["display_name"] == "Ana"
    # El administrador (miembro como admin) lo lee y lo tiene sin leer.
    admin_canal = _canal(client, "Desarrollo")
    assert admin_canal["unread"] == 1 and admin_canal["last"]["body"] == "Hola, equipo"
    assert _ok(client.get("/chat/unread"))["unread"] >= 1
    _ok(client.post(f"/chat/conversations/{canal['id']}/read", json={}))
    assert _canal(client, "Desarrollo")["unread"] == 0

    # Ajuste del espacio: que lo vean también los lectores.
    assert client.patch(f"/workspaces/{dev['id']}", json={"settings": {"chat_min_role": "nadie"}}).status_code == 400
    _ok(client.patch(f"/workspaces/{dev['id']}", json={"settings": {"chat_min_role": "viewer"}}))
    assert [m["body"] for m in _ok(beto.get(f"/chat/conversations/{canal['id']}/messages"))["messages"]] == ["Hola, equipo"]


def test_editar_y_borrar_solo_el_autor(client, gente):
    ana = gente["ana"]
    canal = _canal(ana, "Desarrollo")
    msg = _ok(ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "primera versión"}))
    assert client.patch(f"/chat/messages/{msg['id']}", json={"body": "no es mío"}).status_code == 403
    editado = _ok(ana.patch(f"/chat/messages/{msg['id']}", json={"body": "segunda versión"}))
    assert editado["body"] == "segunda versión" and editado["edited_at"]
    borrado = _ok(ana.delete(f"/chat/messages/{msg['id']}"))
    assert borrado["deleted"] and borrado["body"] == ""
    assert ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "  "}).status_code == 400


def test_paginas_hacia_atras(client, gente):
    ana = gente["ana"]
    canal = _canal(ana, "Desarrollo")
    for i in range(5):
        _ok(ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": f"m{i}"}))
    ultimos = _ok(ana.get(f"/chat/conversations/{canal['id']}/messages", params={"limit": 2}))["messages"]
    assert [m["body"] for m in ultimos] == ["m3", "m4"]
    antes = _ok(ana.get(f"/chat/conversations/{canal['id']}/messages", params={"limit": 2, "before": ultimos[0]["id"]}))
    assert [m["body"] for m in antes["messages"]] == ["m1", "m2"]


def test_mensajes_directos(client, gente):
    ana, beto, carla, ids = gente["ana"], gente["beto"], gente["carla"], gente["ids"]
    d = _ok(ana.post("/chat/direct", json={"user_ids": [ids["beto"]]}))
    assert d["kind"] == "direct" and d["title"] == "Beto"
    # El mismo par reutiliza la conversación.
    assert _ok(beto.post("/chat/direct", json={"user_ids": [ids["ana"]]}))["id"] == d["id"]
    _ok(ana.post(f"/chat/conversations/{d['id']}/messages", json={"body": "¿Comemos?"}))
    assert next(c for c in _ok(beto.get("/chat/conversations")) if c["id"] == d["id"])["unread"] == 1
    # Ni Carla ni el administrador lo ven.
    assert carla.get(f"/chat/conversations/{d['id']}/messages").status_code == 404
    assert client.get(f"/chat/conversations/{d['id']}/messages").status_code == 404
    # Se añade a Carla y luego Beto se va.
    _ok(ana.post(f"/chat/conversations/{d['id']}/members", json={"user_ids": [ids["carla"]]}))
    assert len(_ok(carla.get(f"/chat/conversations/{d['id']}/messages"))["messages"]) == 1
    _ok(beto.delete(f"/chat/conversations/{d['id']}/members/me"))
    assert beto.get(f"/chat/conversations/{d['id']}/messages").status_code == 404
    assert ana.post("/chat/direct", json={"user_ids": []}).status_code == 400
    assert ana.post("/chat/direct", json={"user_ids": ["no-existe"]}).status_code == 404


def test_los_eventos_solo_llegan_a_quien_ve_la_conversacion(client, gente):
    from types import SimpleNamespace

    from app.routers.events import _le_toca

    ev = {"type": "chat", "action": "message", "conversation_id": "x", "recipients": [gente["ids"]["ana"]]}
    assert _le_toca(SimpleNamespace(id=gente["ids"]["ana"]), ev)
    assert not _le_toca(SimpleNamespace(id=gente["ids"]["beto"]), ev)
    assert not _le_toca(None, ev)


def test_menciones_avisan_solo_a_quien_ve_la_conversacion(client, gente):
    ana, beto, carla, ids = gente["ana"], gente["beto"], gente["carla"], gente["ids"]
    canal = _canal(ana, "Desarrollo")
    # Beto es lector (no ve el canal) y Carla no es del espacio: no reciben aviso.
    _ok(ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "@prueba @beto @carla mirad esto"}))
    avisos = _ok(client.get("/notifications"))["items"]
    mencion = next(a for a in avisos if a["type"] == "chat_mention")
    assert mencion["conversation_id"] == canal["id"] and "Ana te ha mencionado en # Desarrollo" in mencion["message"]
    for c in (beto, carla):
        assert not [a for a in _ok(c.get("/notifications"))["items"] if a["type"] == "chat_mention"]
    # En un directo, sí.
    d = _ok(ana.post("/chat/direct", json={"user_ids": [ids["beto"]]}))
    _ok(ana.post(f"/chat/conversations/{d['id']}/messages", json={"body": "@beto ¿lo ves?"}))
    assert [a for a in _ok(beto.get("/notifications"))["items"] if a["type"] == "chat_mention"]


def test_adjuntos(client, gente):
    ana, carla = gente["ana"], gente["carla"]
    canal = _canal(ana, "Desarrollo")
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6300010000050001"
                        "0d0a2db40000000049454e44ae426082")
    subido = _ok(ana.post(f"/chat/conversations/{canal['id']}/files", files={"file": ("foto.png", png, "image/png")}))
    assert subido["type"] == "image" and subido["url"].startswith("/files/")
    assert carla.post(f"/chat/conversations/{canal['id']}/files", files={"file": ("x.png", png, "image/png")}).status_code == 404
    msg = _ok(ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "", "attachments": [subido]}))
    assert msg["attachments"][0]["name"] == "foto.png"
    assert ana.get(subido["url"]).status_code == 200
    mal = ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "x", "attachments": [{"url": "https://malo"}]})
    assert mal.status_code == 400
