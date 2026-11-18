"""H3: el estado con grupos.

Las opciones de una selección se organizan en grupos que define el equipo
(Pendiente, En curso, Hecho…) y cada grupo dice si cuenta como cerrado. Lo que
miran Mis tareas, los contadores de subtareas y los filtros sigue siendo
`values`, que sale de los grupos cerrados: reorganizar los grupos no rompe nada.
"""

import pytest

GRUPOS = [
    {"name": "Pendiente", "closed": False, "options": ["Por hacer", "Bloqueada", "En revisión"]},
    {"name": "En curso", "closed": False, "options": ["Haciendo"]},
    {"name": "Hecho", "closed": True, "options": ["Hecha", "Descartada"]},
]
OPCIONES = ["Por hacer", "Bloqueada", "En revisión", "Haciendo", "Hecha", "Descartada"]


@pytest.fixture
def base(client):
    b = client.post("/databases", json={"title": "Tareas", "schema_def": [
        {"name": "Estado", "type": "select", "options": OPCIONES},
    ]}).json()
    return b, b["schema_def"][0]["key"]


def _poner(client, base_id, clave):
    r = client.put(f"/databases/{base_id}/closed", json={"property": clave, "groups": GRUPOS})
    assert r.status_code == 200, r.text
    return r.json()


def test_los_grupos_se_guardan_y_values_sale_de_los_cerrados(client, base):
    b, clave = base
    cerrada = _poner(client, b["id"], clave)["closed"]
    assert cerrada["values"] == ["Hecha", "Descartada"]
    assert [(g["name"], g["closed"]) for g in cerrada["groups"]] == [
        ("Pendiente", False), ("En curso", False), ("Hecho", True),
    ]
    # Se lee igual al volver a pedirla.
    assert client.get(f"/databases/{b['id']}").json()["closed"] == cerrada


def test_las_filas_se_cierran_por_su_grupo(client, base):
    b, clave = base
    _poner(client, b["id"], clave)
    estados = {"Bloqueada": False, "Haciendo": False, "Hecha": True, "Descartada": True}
    for opcion, cerrada in estados.items():
        fila = client.post(f"/databases/{b['id']}/rows", json={"title": opcion, "properties": {clave: opcion}}).json()
        assert fila["closed"] is cerrada, opcion


def test_reorganizar_los_grupos_no_rompe_el_contador(client, base):
    b, clave = base
    _poner(client, b["id"], clave)
    padre = client.post(f"/databases/{b['id']}/rows", json={"title": "Madre", "properties": {clave: "Haciendo"}}).json()
    for estado in ("Hecha", "Bloqueada", "Descartada"):
        client.post(f"/databases/{b['id']}/rows", json={"title": estado, "properties": {clave: estado}})
    # «Bloqueada» pasa a un grupo cerrado «Detenida»: se recalcula sin tocar filas.
    nuevos = [
        {"name": "Abierto", "closed": False, "options": ["Por hacer", "En revisión", "Haciendo"]},
        {"name": "Detenida", "closed": True, "options": ["Bloqueada", "Hecha", "Descartada"]},
    ]
    r = client.put(f"/databases/{b['id']}/closed", json={"property": clave, "groups": nuevos})
    assert r.json()["closed"]["values"] == ["Bloqueada", "Hecha", "Descartada"]
    fila = client.post(f"/databases/{b['id']}/rows", json={"title": "Otra", "properties": {clave: "Bloqueada"}}).json()
    assert fila["closed"] is True
    assert padre["id"]


@pytest.mark.parametrize("grupos,fragmento", [
    ([{"name": "A", "closed": True, "options": ["No existe"]}], "no es un valor"),
    ([{"name": "A", "closed": True, "options": ["Hecha"]}, {"name": "B", "closed": False, "options": ["Hecha"]}], "dos grupos"),
    ([{"name": "A", "closed": True, "options": ["Hecha"]}, {"name": "a", "closed": False, "options": []}], "dos grupos llamados"),
    ([{"name": " ", "closed": True, "options": ["Hecha"]}], "necesita un nombre"),
    ([{"name": "Solo abiertas", "closed": False, "options": ["Hecha"]}], "marca como cerrado"),
    ([{"name": "Cerrado vacío", "closed": True, "options": []}], "marca como cerrado"),
])
def test_grupos_que_no_valen(client, base, grupos, fragmento):
    b, clave = base
    r = client.put(f"/databases/{b['id']}/closed", json={"property": clave, "groups": grupos})
    assert r.status_code == 400 and fragmento in r.json()["detail"]
    assert client.get(f"/databases/{b['id']}").json()["closed"] is None  # no se escribió nada


def test_una_casilla_no_tiene_grupos(client):
    b = client.post("/databases", json={"title": "Check", "schema_def": [{"name": "Hecho", "type": "checkbox"}]}).json()
    r = client.put(f"/databases/{b['id']}/closed", json={"property": b["schema_def"][0]["key"], "groups": GRUPOS})
    assert r.status_code == 400


def test_sin_grupos_todo_sigue_como_antes(client, base):
    b, clave = base
    r = client.put(f"/databases/{b['id']}/closed", json={"property": clave, "values": ["Hecha"]})
    assert r.status_code == 200 and r.json()["closed"]["groups"] == []
    assert r.json()["closed"]["values"] == ["Hecha"]


def test_los_grupos_sobreviven_a_guardar_y_reindexar(client, base):
    b, clave = base
    antes = _poner(client, b["id"], clave)["closed"]
    client.patch(f"/databases/{b['id']}", json={"title": "Tareas renombradas"})
    assert client.post("/reindex?force=true").status_code == 200
    assert client.get(f"/databases/{b['id']}").json()["closed"] == antes


def test_crear_la_base_con_grupos(client):
    r = client.post("/databases", json={
        "title": "Con grupos",
        "schema_def": [{"name": "Estado", "type": "select", "options": OPCIONES}],
        "closed": {"property": "estado", "groups": GRUPOS},
    })
    assert r.status_code == 200, r.text
    assert r.json()["closed"]["values"] == ["Hecha", "Descartada"]
