"""H4: elegir a mano qué columna hace de «Proyecto» en una base de Tareas."""

from test_h4c_horas_del_proyecto import _proyectos  # noqa: F401


def _tareas_sin_proyecto(client):
    return client.post("/databases/from-template", json={"template": "tareas", "title": "Tareas", "section": "shared"}).json()


def _con_relacion(client, base, destino, nombre="Cliente final"):
    esquema = list(base["schema_def"]) + [
        {"name": nombre, "type": "relation", "target_database": destino["id"], "multiple": False}
    ]
    r = client.patch(f"/databases/{base['id']}", json={"schema_def": esquema})
    assert r.status_code == 200, r.text
    return r.json()


def test_elegir_la_columna_de_proyecto_y_quitarla(client):
    proyectos = _proyectos(client)
    base = _tareas_sin_proyecto(client)
    assert "project" not in base["kind"]["roles"]
    base = _con_relacion(client, base, proyectos)
    clave = next(p["key"] for p in base["schema_def"] if p["name"] == "Cliente final")
    r = client.put(f"/databases/{base['id']}/kind", json={"type": "tasks", "roles": {"project": clave}})
    assert r.status_code == 200, r.text
    assert r.json()["kind"]["roles"]["project"] == clave
    # Quitar el rol (opcional): la columna se queda, ya sin ser «Proyecto».
    r = client.put(f"/databases/{base['id']}/kind", json={"type": "tasks", "roles": {"project": ""}})
    assert r.status_code == 200 and "project" not in r.json()["kind"]["roles"]
    assert any(p["key"] == clave for p in r.json()["schema_def"])
    # Los obligatorios no se pueden quitar: se vuelve a mapear el que había.
    r = client.put(f"/databases/{base['id']}/kind", json={"type": "tasks", "roles": {"end": ""}})
    assert r.status_code == 200 and r.json()["kind"]["roles"]["end"]


def test_una_relacion_a_una_base_que_no_es_de_proyectos_se_rechaza(client):
    otra = client.post("/databases", json={"title": "Clientes", "schema_def": [], "section": "shared"}).json()
    base = _con_relacion(client, _tareas_sin_proyecto(client), otra)
    clave = next(p["key"] for p in base["schema_def"] if p["name"] == "Cliente final")
    r = client.put(f"/databases/{base['id']}/kind", json={"type": "tasks", "roles": {"project": clave}})
    assert r.status_code == 400 and "Proyectos" in r.json()["detail"]
