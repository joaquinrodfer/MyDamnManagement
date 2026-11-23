"""Chat: editar y borrar tienen un plazo y dejan rastro."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from test_ch1_chat import _canal, _ok, gente  # noqa: F401 - fixture

from app import chat


def test_editar_deja_las_versiones_anteriores(client, gente):
    ana = gente["ana"]
    canal = _canal(ana, "Desarrollo")
    msg = _ok(ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "Hola"}))
    assert msg["edits"] == 0 and msg["changeable_until"] > msg["created_at"]
    m1 = _ok(ana.patch(f"/chat/messages/{msg['id']}", json={"body": "Hola a todos"}))
    m2 = _ok(ana.patch(f"/chat/messages/{msg['id']}", json={"body": "Hola a todo el equipo"}))
    assert m1["edits"] == 1 and m2["edits"] == 2
    # Lo mismo otra vez no cuenta como edición.
    assert _ok(ana.patch(f"/chat/messages/{msg['id']}", json={"body": "Hola a todo el equipo"}))["edits"] == 2
    # Quien ve la conversación ve el rastro.
    rastro = _ok(client.get(f"/chat/messages/{msg['id']}/edits"))["edits"]
    assert [e["body"] for e in rastro] == ["Hola", "Hola a todos"]
    listado = _ok(client.get(f"/chat/conversations/{canal['id']}/messages"))["messages"]
    assert listado[-1]["edits"] == 2
    # Quien no la ve, no.
    assert gente["carla"].get(f"/chat/messages/{msg['id']}/edits").status_code == 404


def test_borrar_deja_el_hueco_y_quita_el_rastro(client, gente):
    ana = gente["ana"]
    canal = _canal(ana, "Desarrollo")
    msg = _ok(ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "Uno"}))
    _ok(ana.patch(f"/chat/messages/{msg['id']}", json={"body": "Dos"}))
    borrado = _ok(ana.delete(f"/chat/messages/{msg['id']}"))
    assert borrado["deleted"] and borrado["body"] == "" and borrado["deleted_at"]
    assert borrado["edits"] == 0
    assert _ok(ana.get(f"/chat/messages/{msg['id']}/edits"))["edits"] == []


def test_pasado_el_plazo_no_se_cambia(client, gente, index_path):
    import sqlite3

    ana = gente["ana"]
    canal = _canal(ana, "Desarrollo")
    msg = _ok(ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "Antiguo"}))
    viejo = (datetime.now(timezone.utc) - chat.CHANGE_WINDOW - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(index_path) as conn:  # envejece el mensaje
        conn.execute("UPDATE chat_messages SET created_at = ? WHERE id = ?", (viejo, msg["id"]))
    r = ana.patch(f"/chat/messages/{msg['id']}", json={"body": "Nuevo"})
    assert r.status_code == 409 and "15 minutos" in r.json()["detail"]
    assert ana.delete(f"/chat/messages/{msg['id']}").status_code == 409
    listado = _ok(ana.get(f"/chat/conversations/{canal['id']}/messages"))["messages"]
    assert listado[-1]["body"] == "Antiguo"


def test_changeable():
    ahora = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    msg = {"created_at": (ahora - timedelta(minutes=14)).isoformat()}
    assert chat.changeable(msg, ahora)
    msg = {"created_at": (ahora - timedelta(minutes=16)).isoformat()}
    assert not chat.changeable(msg, ahora)
