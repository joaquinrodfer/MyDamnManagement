"""Una base de Tareas con una relación a Proyectos sin el rol «Proyecto»
marcado (bases de antes, o convertidas sin marcarlo): la carga y las tareas
del proyecto la usan igual."""

from test_h4c_horas_del_proyecto import _fila, _proyectos
from test_h4d_columna_proyecto import _con_relacion, _tareas_sin_proyecto


def test_la_relacion_a_proyectos_hace_de_proyecto(client):
    proyectos = _proyectos(client)
    web = _fila(client, proyectos, "Web nueva")
    base = _con_relacion(client, _tareas_sin_proyecto(client), proyectos, nombre="a")
    assert "project" not in base["kind"]["roles"]
    clave = next(p["key"] for p in base["schema_def"] if p["name"] == "a")
    roles = base["kind"]["roles"]
    yo = client.get("/auth/me").json()["id"]
    _fila(client, base, "Maquetar", **{clave: web["id"], roles["assignee"]: [yo], roles["hours"]: 8,
                                       roles["start"]: "2026-11-16", roles["end"]: "2026-11-16"})
    d = client.get("/workload/grid", params={"start": "2026-11-16", "group": "project"}).json()
    assert [r["name"] for r in d["rows"]] == ["Web nueva"]
    r = client.get(f"/databases/{proyectos['id']}/rows/{web['id']}/tasks").json()
    assert [t["title"] for t in r["tasks"]] == ["Maquetar"]


def test_con_dos_relaciones_a_proyectos_no_se_adivina(client):
    proyectos = _proyectos(client)
    web = _fila(client, proyectos, "Web nueva")
    base = _con_relacion(client, _tareas_sin_proyecto(client), proyectos, nombre="a")
    base = _con_relacion(client, base, proyectos, nombre="b")
    clave = next(p["key"] for p in base["schema_def"] if p["name"] == "a")
    roles = base["kind"]["roles"]
    yo = client.get("/auth/me").json()["id"]
    _fila(client, base, "Maquetar", **{clave: web["id"], roles["assignee"]: [yo], roles["hours"]: 8,
                                       roles["start"]: "2026-11-16", roles["end"]: "2026-11-16"})
    d = client.get("/workload/grid", params={"start": "2026-11-16", "group": "project"}).json()
    assert [r["name"] for r in d["rows"]] == ["Sin proyecto"]
