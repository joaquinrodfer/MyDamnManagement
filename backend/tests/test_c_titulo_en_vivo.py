"""Título, descripción e icono en vivo (mensaje `meta` de la sala).

El cuerpo es el documento colaborativo (con rebase); el título, la
descripción y el icono son campos sueltos: el último que escribe gana. Van
por la misma sala para que los demás los vean al momento, y se guardan como
un `PATCH`. Aquí se fija:

- `meta` guarda y reparte: `meta-ack` a quien lo manda (con lo que quedó y la
  versión), `meta` a los demás;
- quien solo puede ver no cambia nada, y lo que no tiene forma de `meta` se
  rechaza sin escribir;
- vale igual para una fila (y su base se entera);
- el árbol de las demás pestañas se entera de un título nuevo (`tree`), y no
  de un guardado del cuerpo.
"""

import uuid

from app.events import bus
from test_h4_colaboracion import (  # noqa: F401 - fixtures
    beto,
    entrar,
    nota,
    servidor,
)


def _respuesta(ws, tipo, timeout=5):
    """El siguiente mensaje de `tipo` (la presencia y demás se saltan)."""
    while True:
        msg = ws.recv(timeout=timeout)
        if msg["type"] == tipo:
            return msg


def test_meta_se_guarda_y_llega_a_los_demas(servidor):
    admin = servidor
    pid = nota(admin, "cuerpo")
    with admin.sala(pid) as a, admin.sala(pid) as b:
        entrar(a)
        entrar(b)
        a.send({"type": "meta", "seq": 1, "fields": {"title": "Título nuevo", "description": "Desc"}})
        ack = _respuesta(a, "meta-ack")
        assert ack["seq"] == 1
        assert ack["fields"]["title"] == "Título nuevo"
        assert ack["fields"]["description"] == "Desc"
        assert isinstance(ack["fields"]["version"], int)
        visto = _respuesta(b, "meta")
        assert visto["fields"]["title"] == "Título nuevo"
        assert visto["user"]["name"]
    pagina = admin.req("GET", f"/pages/{pid}")
    assert pagina["title"] == "Título nuevo"
    assert pagina["description"] == "Desc"
    assert pagina["body_markdown"] == "cuerpo"  # el cuerpo no se toca


def test_el_titulo_sale_como_lo_guarda_el_servidor(servidor):
    """El vault sanea el nombre (corchetes): el ack trae el que quedó."""
    admin = servidor
    pid = nota(admin)
    with admin.sala(pid) as a:
        entrar(a)
        a.send({"type": "meta", "seq": 7, "fields": {"title": "Con [corchetes]"}})
        ack = _respuesta(a, "meta-ack")
        assert ack["fields"]["title"] == admin.req("GET", f"/pages/{pid}")["title"]
        assert "[" not in ack["fields"]["title"]


def test_meta_mal_formado_o_de_solo_lectura_no_escribe(servidor, beto):
    admin = servidor
    cliente_beto, beto_id = beto
    pid = nota(admin)
    titulo = admin.req("GET", f"/pages/{pid}")["title"]
    with admin.sala(pid) as a:
        entrar(a)
        for malo in ({"body_markdown": "x"}, {"title": 5}, {}, "texto", {"title": "x" * 600}):
            a.send({"type": "meta", "seq": 2, "fields": malo})
            assert _respuesta(a, "meta-reject")["seq"] == 2
    me = admin.req("GET", "/auth/me")
    admin.req("PUT", f"/acl/page/{pid}", {"inherit": False, "entries": [
        {"subject_type": "user", "subject_id": me["id"], "level": "owner"},
        {"subject_type": "user", "subject_id": beto_id, "level": "view"},
    ]}, expect=200)
    with cliente_beto.sala(pid) as ws:
        entrar(ws)
        ws.send({"type": "meta", "seq": 3, "fields": {"title": "De Beto"}})
        assert _respuesta(ws, "meta-reject")["reason"] == "readonly"
    assert admin.req("GET", f"/pages/{pid}")["title"] == titulo


def test_meta_de_una_fila(servidor):
    admin = servidor
    base = admin.req("POST", "/databases", {"title": f"B {uuid.uuid4().hex[:6]}", "schema_def": []}, expect=200)
    fila = admin.req("POST", f"/databases/{base['id']}/rows", {"title": "Vieja"}, expect=200)
    with admin.sala(fila["id"]) as a:
        entrar(a)
        a.send({"type": "meta", "seq": 1, "fields": {"title": "Nueva", "icon": "🥖"}})
        assert _respuesta(a, "meta-ack")["fields"]["icon"] == "🥖"
    leida = admin.req("GET", f"/databases/{base['id']}/rows/{fila['id']}")
    assert leida["title"] == "Nueva" and leida["icon"] == "🥖"


def test_el_arbol_se_entera_del_titulo_y_no_del_cuerpo(client, monkeypatch):
    avisos = []
    monkeypatch.setattr(bus, "publish", lambda ev: avisos.append(ev))
    creada = client.post("/pages", json={"title": "Árbol"}).json()
    assert {"type": "tree", "action": "created", "page_id": creada["id"]} in avisos
    avisos.clear()
    client.patch(f"/pages/{creada['id']}", json={"title": "Árbol", "body_markdown": "solo cuerpo"})
    assert [a for a in avisos if a["type"] == "tree"] == []
    client.patch(f"/pages/{creada['id']}", json={"title": "Árbol renombrado"})
    assert {"type": "tree", "action": "changed", "page_id": creada["id"]} in avisos
    avisos.clear()
    client.delete(f"/pages/{creada['id']}")
    assert {"type": "tree", "action": "deleted", "page_id": creada["id"]} in avisos
