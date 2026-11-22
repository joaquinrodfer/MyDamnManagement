"""Plan del chat, CH4: convertir en tarea, buscar y «está escribiendo…»."""

from __future__ import annotations

from test_ch1_chat import _canal, _ok, gente  # noqa: F401 - fixture


def test_convertir_un_mensaje_en_tarea(client, gente):
    ana, carla = gente["ana"], gente["carla"]
    canal = _canal(ana, "Desarrollo")
    base = _ok(client.post("/databases/from-template", json={"template": "tareas", "title": "Tareas dev"},
                           headers={"X-Workspace": gente["dev"]["id"]}))
    msg = _ok(ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "Revisar el login\ncon calma"}))
    r = _ok(client.post(f"/chat/messages/{msg['id']}/task", json={"database_id": base["id"]}))
    assert r["title"] == "Revisar el login" and r["message"]["task_id"] == r["task_id"]
    fila = _ok(client.get(f"/pages/{r['task_id']}"))
    assert "Desde el chat (# Desarrollo), de Ana." in fila["body_markdown"]
    filas = _ok(client.get(f"/databases/{base['id']}/rows"))
    nueva = next(f for f in filas if f["id"] == r["task_id"])
    resp = base["kind"]["roles"]["assignee"]
    assert gente["ids"]["ana"] in (nueva["properties"][resp] if isinstance(nueva["properties"][resp], list) else [nueva["properties"][resp]])
    # Quien no ve la conversación no puede.
    assert carla.post(f"/chat/messages/{msg['id']}/task", json={"database_id": base["id"]}).status_code == 404


def test_buscar_solo_en_lo_que_veo(client, gente):
    ana, carla, ids = gente["ana"], gente["carla"], gente["ids"]
    canal = _canal(ana, "Desarrollo")
    _ok(ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "El presupuesto del cliente"}))
    d = _ok(ana.post("/chat/direct", json={"user_ids": [ids["carla"]]}))
    _ok(carla.post(f"/chat/conversations/{d['id']}/messages", json={"body": "Presupuesto aprobado"}))
    ana_ve = _ok(ana.get("/chat/search", params={"q": "presupuesto"}))["messages"]
    assert {m["body"] for m in ana_ve} == {"El presupuesto del cliente", "Presupuesto aprobado"}
    carla_ve = _ok(carla.get("/chat/search", params={"q": "presupuesto"}))["messages"]
    assert [m["body"] for m in carla_ve] == ["Presupuesto aprobado"]
    assert carla_ve[0]["conversation_title"] == "Ana"
    # Lo borrado no sale.
    _ok(carla.delete(f"/chat/messages/{carla_ve[0]['id']}"))
    assert _ok(carla.get("/chat/search", params={"q": "presupuesto"}))["messages"] == []


def test_escribiendo(client, gente):
    ana, carla = gente["ana"], gente["carla"]
    canal = _canal(ana, "Desarrollo")
    assert ana.post(f"/chat/conversations/{canal['id']}/typing").status_code == 204
    assert carla.post(f"/chat/conversations/{canal['id']}/typing").status_code == 404
