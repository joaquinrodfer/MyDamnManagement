"""Las filas de una base son páginas normales.

Una fila ya era un `.md` con cuerpo; lo que cambia es que se abre por el mismo
camino que una nota (`GET`/`PATCH /pages/{id}`, la sala de edición en vivo, la
cabecera, los backlinks) y no por una vista aparte. Lo que se fija aquí:

- `/pages/{id}` de una fila dice de qué base es (`database_id`), que es con lo
  que la página pide el esquema y pinta las propiedades;
- guardar el título, el icono o el cuerpo por `/pages` escribe la fila (y la
  tabla lo ve), sin tocar sus propiedades;
- la tabla de la base se entera (`database`/`rows` por `/events`) cuando
  cambia algo que enseña, y no con cada guardado del cuerpo;
- la fila tiene sala de edición en vivo, con los permisos de su base;
- una propiedad nueva creada desde la fila es de la base: la tienen todas.
"""

import time
import uuid

import pytest

from app.events import bus
from test_h4_colaboracion import (  # noqa: F401 - fixtures
    Sesion,
    _cierre,
    beto,
    entrar,
    esperar_cuerpo,
    servidor,
)


def _base_con_fila(cliente, **fila):
    base = cliente.post(
        "/databases",
        json={"title": f"Tareas {uuid.uuid4().hex[:6]}", "schema_def": [{"name": "Estado", "type": "text"}]},
    ).json()
    creada = cliente.post(
        f"/databases/{base['id']}/rows",
        json={"title": "Comprar pan", "properties": {"estado": "pendiente"}, **fila},
    ).json()
    return base, creada


def test_la_fila_se_lee_como_una_pagina(client):
    base, fila = _base_con_fila(client)
    pagina = client.get(f"/pages/{fila['id']}")
    assert pagina.status_code == 200
    datos = pagina.json()
    assert datos["type"] == "database_row"
    assert datos["database_id"] == base["id"]
    assert datos["title"] == "Comprar pan"
    assert pagina.headers["ETag"] == f'"{datos["version"]}"'
    # Una nota no tiene base.
    nota = client.post("/pages", json={"title": "Suelta"}).json()
    assert nota["database_id"] is None


def test_guardar_la_fila_por_pages_no_toca_sus_propiedades(client):
    base, fila = _base_con_fila(client)
    r = client.patch(
        f"/pages/{fila['id']}",
        json={"title": "Comprar pan y leche", "icon": "🥖", "description": "", "body_markdown": "- [ ] pan\n"},
    )
    assert r.status_code == 200, r.text
    leida = client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()
    assert leida["title"] == "Comprar pan y leche"
    assert leida["icon"] == "🥖"
    assert leida["body_markdown"].rstrip("\n") == "- [ ] pan"
    assert leida["properties"]["estado"] == "pendiente"
    # Y una fila no se mueve de carpeta: su sitio lo decide su base.
    nota = client.post("/pages", json={"title": "Destino"}).json()
    assert client.patch(f"/pages/{fila['id']}", json={"parent_id": nota["id"]}).status_code == 400


def test_la_tabla_se_entera_del_titulo_y_no_de_cada_guardado_del_cuerpo(client, monkeypatch):
    base, fila = _base_con_fila(client)
    avisos = []
    monkeypatch.setattr(bus, "publish", lambda ev: avisos.append(ev))
    client.patch(f"/pages/{fila['id']}", json={"title": "Comprar pan", "body_markdown": "uno"})
    assert avisos == [], "solo el cuerpo: la tabla no enseña el cuerpo"
    client.patch(f"/pages/{fila['id']}", json={"title": "Otro nombre"})
    assert {"type": "database", "action": "rows", "database_id": base["id"]} in avisos


def test_una_propiedad_creada_desde_la_fila_es_de_la_base(client):
    """Como en Notion: añadir una propiedad desde la fila la añade a la base."""
    base, fila = _base_con_fila(client)
    otra = client.post(f"/databases/{base['id']}/rows", json={"title": "Otra"}).json()
    esquema = base["schema_def"] + [{"name": "Prioridad", "type": "select", "options": ["alta", "baja"]}]
    r = client.patch(f"/databases/{base['id']}", json={"schema_def": esquema})
    assert r.status_code == 200, r.text
    clave = r.json()["schema_def"][-1]["key"]
    r = client.patch(f"/databases/{base['id']}/rows/{fila['id']}", json={"properties": {clave: "alta"}})
    assert r.status_code == 200, r.text
    assert client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()["properties"][clave] == "alta"
    # La otra fila tiene la propiedad (vacía): es de la base, no de la fila.
    definicion = client.get(f"/databases/{base['id']}").json()
    assert [p["name"] for p in definicion["schema_def"]] == ["Estado", "Prioridad"]
    assert client.get(f"/databases/{base['id']}/rows/{otra['id']}").json()["properties"].get(clave) is None


# --------------------------------------------------------------------------
# edición en vivo de una fila
# --------------------------------------------------------------------------


def _fila_por_http(admin, cuerpo="hola"):
    base = admin.req("POST", "/databases", {"title": f"B {uuid.uuid4().hex[:6]}", "schema_def": []}, expect=200)
    fila = admin.req("POST", f"/databases/{base['id']}/rows", {"title": "F", "body": cuerpo}, expect=200)
    if cuerpo:
        admin.req("PATCH", f"/pages/{fila['id']}", {"body_markdown": cuerpo}, expect=200)
    return base, fila["id"]


def test_la_fila_tiene_sala_de_edicion_en_vivo(servidor):
    admin = servidor
    base, fid = _fila_por_http(admin, "hola")
    with admin.sala(fid) as a, admin.sala(fid) as b:
        assert entrar(a)["doc"] == "hola"
        entrar(b)
        a.send({"type": "update", "version": 0, "updates": [{"clientID": "A", "changes": [4, [0, " mundo"]]}]})
        assert a.recv()["type"] == "ack"
        assert b.recv()["type"] == "update"
    assert esperar_cuerpo(admin, fid, "hola mundo") == "hola mundo"
    # Guardado en la fila de verdad (el `.md` de la fila), con sus propiedades.
    leida = admin.req("GET", f"/databases/{base['id']}/rows/{fid}", expect=200)
    assert leida["body_markdown"] == "hola mundo"


def test_la_sala_de_una_fila_sigue_los_permisos_de_su_base(servidor, beto):
    admin = servidor
    cliente_beto, beto_id = beto
    base, fid = _fila_por_http(admin, "privada")
    # La base es privada del administrador: Beto no ve ni la fila.
    assert _cierre(cliente_beto, fid) == 4404
    assert cliente_beto.req("GET", f"/pages/{fid}")  # 404 en JSON
    me = admin.req("GET", "/auth/me")
    admin.req("PUT", f"/acl/database/{base['id']}", {"inherit": False, "entries": [
        {"subject_type": "user", "subject_id": me["id"], "level": "owner"},
        {"subject_type": "user", "subject_id": beto_id, "level": "view"},
    ]}, expect=200)
    with cliente_beto.sala(fid) as ws:
        assert entrar(ws)["readonly"] is True


def test_una_base_no_tiene_sala(servidor):
    admin = servidor
    base, _ = _fila_por_http(admin, "")
    pagina_base = admin.req("GET", "/pages/tree")
    pid = next(n["id"] for n in _nodos(pagina_base) if n.get("database_id") == base["id"])
    assert _cierre(admin, pid) == 4404


def _nodos(arbol):
    pila = list(arbol if isinstance(arbol, list) else arbol.get("nodes", arbol.get("tree", [])))
    while pila:
        n = pila.pop()
        yield n
        pila.extend(n.get("children") or [])
