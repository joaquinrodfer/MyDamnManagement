"""H2: Compartido y Privado, mover entre espacios e invitados.

- una página de la raíz está en Compartido (todo el espacio) o en Privado (solo
  su propietario); se crea en una u otra y se pasa de una a otra;
- una página (con lo que cuelga de ella, filas incluidas) se mueve a otro
  espacio y queda en su raíz; solo la mueve su propietario y hace falta poder
  editar en el destino;
- compartir con alguien de fuera del espacio lo hace invitado: ve esa página y
  sus hijas, no es miembro y le aparece en «compartido conmigo».
"""

import pytest

from test_h1_espacios import _aplanar, _sesion, cierra  # noqa: F401 - fixtures


def _secciones(c, ws):
    arbol = c.get("/pages/tree", headers={"X-Workspace": ws}).json()
    return {n["title"]: n.get("section") for n in arbol}


def _titulos(c, ws):
    return {n["title"] for n in _aplanar(c.get("/pages/tree", headers={"X-Workspace": ws}).json())}


@pytest.fixture
def espacio(client):
    return client.post("/workspaces", json={"name": "Equipo"}).json()


def test_crear_en_compartido_y_en_privado(client, cierra, espacio):
    ws = espacio["id"]
    h = {"X-Workspace": ws}
    client.post("/pages", json={"title": "Para todos", "section": "shared"}, headers=h)
    client.post("/pages", json={"title": "Solo mía"}, headers=h)
    client.post("/pages", json={"title": "Mía también", "section": "private"}, headers=h)
    assert _secciones(client, ws) == {"Para todos": "shared", "Solo mía": "private", "Mía también": "private"}
    # Otro miembro ve lo compartido y no lo privado.
    c, _ = _sesion(client, "beto", workspaces=[{"id": ws, "role": "editor"}])
    cierra.append(c)
    assert _secciones(c, ws) == {"Para todos": "shared"}
    # Lo compartido lo puede editar; lo de otro no lo ve.
    pagina = next(n for n in c.get("/pages/tree", headers=h).json() if n["title"] == "Para todos")
    assert c.patch(f"/pages/{pagina['id']}", json={"description": "hola"}).status_code == 200


def test_una_base_nace_compartida(client, espacio):
    ws = espacio["id"]
    client.post("/databases", json={"title": "Tareas", "section": "shared", "schema_def": []}, headers={"X-Workspace": ws})
    assert _secciones(client, ws) == {"Tareas": "shared"}


def test_pasar_de_privado_a_compartido_y_volver(client, cierra, espacio):
    ws = espacio["id"]
    h = {"X-Workspace": ws}
    pagina = client.post("/pages", json={"title": "Cambia"}, headers=h).json()
    c, _ = _sesion(client, "ana", workspaces=[{"id": ws, "role": "editor"}])
    cierra.append(c)
    assert "Cambia" not in _titulos(c, ws)
    assert client.patch(f"/pages/{pagina['id']}", json={"section": "shared"}).status_code == 200
    assert _secciones(client, ws)["Cambia"] == "shared" and "Cambia" in _titulos(c, ws)
    assert client.patch(f"/pages/{pagina['id']}", json={"section": "private"}).status_code == 200
    assert _secciones(client, ws)["Cambia"] == "private" and "Cambia" not in _titulos(c, ws)
    # Solo el propietario cambia con quién se comparte.
    client.patch(f"/pages/{pagina['id']}", json={"section": "shared"})
    assert c.patch(f"/pages/{pagina['id']}", json={"section": "private"}).status_code == 403


def test_mover_a_otro_espacio_lleva_lo_que_cuelga(client, cierra, espacio):
    origen = client.post("/workspaces", json={"name": "Origen"}).json()["id"]
    destino = espacio["id"]
    ho = {"X-Workspace": origen}
    madre = client.post("/pages", json={"title": "Madre", "section": "shared"}, headers=ho).json()
    hija = client.post("/pages", json={"title": "Hija", "parent_id": madre["id"]}, headers=ho).json()
    base = client.post("/databases", json={"title": "Datos", "parent_id": madre["id"], "schema_def": []}, headers=ho).json()
    fila = client.post(f"/databases/{base['id']}/rows", json={"title": "Fila"}, headers=ho).json()
    c, _ = _sesion(client, "dest", workspaces=[{"id": destino, "role": "editor"}])
    cierra.append(c)

    r = client.patch(f"/pages/{madre['id']}", json={"workspace_id": destino, "section": "shared"})
    assert r.status_code == 200, r.text
    assert r.json()["workspace_id"] == destino
    assert client.get(f"/pages/{hija['id']}").json()["workspace_id"] == destino
    assert client.get(f"/pages/{fila['id']}").json()["workspace_id"] == destino
    assert "Madre" not in _titulos(client, origen) and {"Madre", "Hija", "Datos"} <= _titulos(client, destino)
    assert _secciones(client, destino)["Madre"] == "shared"
    # Los del destino la ven (compartida con su espacio) y ya no es del origen.
    assert "Madre" in _titulos(c, destino)


def test_mover_pide_poder_editar_en_el_destino_y_ser_propietario(client, cierra, espacio):
    lectura = client.post("/workspaces", json={"name": "Solo lectura"}).json()["id"]
    pagina = client.post("/pages", json={"title": "Viaja"}).json()
    jefe, _ = _sesion(client, "jefa", workspaces=[{"id": lectura, "role": "viewer"}])
    cierra.append(jefe)
    # El admin sí puede (es superadministrador); un viewer del destino no puede mover a él.
    propia = jefe.post("/pages", json={"title": "Suya"}, headers={"X-Workspace": lectura})
    assert propia.status_code == 403  # ni crea en un espacio de solo lectura
    otro, ou = _sesion(client, "otra", workspaces=[{"id": espacio["id"], "role": "editor"}, {"id": lectura, "role": "viewer"}])
    cierra.append(otro)
    suya = otro.post("/pages", json={"title": "Suya"}, headers={"X-Workspace": espacio["id"]}).json()
    assert otro.patch(f"/pages/{suya['id']}", json={"workspace_id": lectura}).status_code == 403
    assert otro.patch(f"/pages/{suya['id']}", json={"workspace_id": "00000000-0000-0000-0000-000000000000"}).status_code == 404
    # Una página que no es suya: no la mueve.
    assert otro.patch(f"/pages/{pagina['id']}", json={"workspace_id": espacio["id"]}).status_code in (403, 404)


def test_un_invitado_ve_esa_pagina_y_sus_hijas(client, cierra, espacio):
    ws = espacio["id"]
    h = {"X-Workspace": ws}
    madre = client.post("/pages", json={"title": "Proyecto X", "section": "shared"}, headers=h).json()
    hija = client.post("/pages", json={"title": "Tarea de X", "parent_id": madre["id"]}, headers=h).json()
    client.post("/pages", json={"title": "Otra del espacio", "section": "shared"}, headers=h)
    me = client.get("/auth/me").json()
    # Gabi no está en el espacio: ni lo ve ni entra.
    g, gabi = _sesion(client, "gabi", workspaces=[])
    cierra.append(g)
    assert g.get(f"/pages/{madre['id']}").status_code == 404
    assert g.get("/pages/shared-with-me").json() == []
    r = client.put(f"/acl/page/{madre['id']}", json={"inherit": False, "entries": [
        {"subject_type": "user", "subject_id": me["id"], "level": "owner"},
        {"subject_type": "workspace", "subject_id": ws, "level": "edit"},
        {"subject_type": "user", "subject_id": gabi["id"], "level": "view"},
    ]})
    assert r.status_code == 200, r.text
    por_nombre = {e["subject_id"]: e for e in r.json()["entries"]}
    assert por_nombre[gabi["id"]]["guest"] is True
    assert por_nombre[me["id"]]["guest"] is False
    # Ve esa página y su hija, con el espacio al que pertenecen; nada más.
    compartido = g.get("/pages/shared-with-me").json()
    assert [n["title"] for n in compartido] == ["Proyecto X"]
    assert compartido[0]["workspace"] == {"id": ws, "name": "Equipo"}
    assert [n["title"] for n in compartido[0]["children"]] == ["Tarea de X"]
    assert g.get(f"/pages/{madre['id']}").status_code == 200
    assert g.get(f"/pages/{hija['id']}").status_code == 200
    assert g.get("/workspaces").json() == []  # no es miembro
    assert all(n["title"] != "Otra del espacio" for n in _aplanar(compartido))
    # Y el superadministrador no es invitado de nada.
    assert client.get("/pages/shared-with-me").json() == []


def test_mover_conserva_a_los_invitados_y_quita_el_espacio_de_origen(client, cierra, espacio):
    origen = client.post("/workspaces", json={"name": "De origen"}).json()["id"]
    destino = espacio["id"]
    pagina = client.post("/pages", json={"title": "Con invitado", "section": "shared"}, headers={"X-Workspace": origen}).json()
    me = client.get("/auth/me").json()
    g, gabi = _sesion(client, "gabi", workspaces=[])
    cierra.append(g)
    client.put(f"/acl/page/{pagina['id']}", json={"inherit": False, "entries": [
        {"subject_type": "user", "subject_id": me["id"], "level": "owner"},
        {"subject_type": "workspace", "subject_id": origen, "level": "edit"},
        {"subject_type": "user", "subject_id": gabi["id"], "level": "view"},
    ]})
    assert client.patch(f"/pages/{pagina['id']}", json={"workspace_id": destino, "section": "private"}).status_code == 200
    entradas = client.get(f"/acl/page/{pagina['id']}").json()["entries"]
    assert {(e["subject_type"], e["subject_id"]) for e in entradas} == {("user", me["id"]), ("user", gabi["id"])}
    assert g.get(f"/pages/{pagina['id']}").status_code == 200  # el invitado sigue viéndola
