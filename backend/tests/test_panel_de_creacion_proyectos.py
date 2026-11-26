"""Panel de ajustes al crear Proyectos o Tareas, y la columna «Tareas».

Al crear una base de Proyectos o de Tareas se pueden elegir sus ajustes de una
vez (estimación, reparto de horas, columna de avance), y la base de Proyectos
enseña en su tabla cuántas tareas de cada proyecto están hechas.
"""

from test_h1_espacios import _sesion, cierra  # noqa: F401 - fixtures


def _crear(client, plantilla, **extra):
    r = client.post("/databases/from-template", json={"template": plantilla, "section": "shared", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _fila(client, base, titulo, **props):
    r = client.post(f"/databases/{base['id']}/rows", json={"title": titulo, "properties": props})
    assert r.status_code == 200, r.text
    return r.json()


def test_proyectos_con_estimacion_y_sin_columna_de_tareas(client):
    base = _crear(client, "proyectos", title="P", estimate=True, options={"tasks_column": False})
    clave = base["kind"]["roles"]["estimate"]
    columna = next(p for p in base["schema_def"] if p["key"] == clave)
    assert columna["type"] == "number"
    assert base["kind"]["options"] == {"tasks_column": False}
    # Por defecto, ni estimación ni la opción escrita (la columna se ve).
    normal = _crear(client, "proyectos", title="Q")
    assert "estimate" not in normal["kind"]["roles"] and normal["kind"]["options"] == {}


def test_tareas_con_reparto_de_horas(client):
    base = _crear(client, "tareas", title="T", options={"hours_split": "equal", "spread": "end"})
    assert base["kind"]["options"] == {"hours_split": "equal", "spread": "end"}


def test_opciones_que_no_valen(client):
    for plantilla, opciones in (("tareas", {"hours_split": "mucho"}), ("proyectos", {"hours_split": "full"}),
                                ("proyectos", {"tasks_column": "sí"})):
        r = client.post("/databases/from-template", json={"template": plantilla, "options": opciones})
        assert r.status_code == 400, (plantilla, opciones)


def test_avance_de_cada_proyecto(client):
    proyectos = _crear(client, "proyectos", title="Proyectos")
    tareas = _crear(client, "tareas", title="Tareas de Proyectos", project_database=proyectos["id"])
    roles = tareas["kind"]["roles"]
    web = _fila(client, proyectos, "Web")
    app = _fila(client, proyectos, "App")
    vacio = _fila(client, proyectos, "Sin tareas")
    _fila(client, tareas, "Maquetar", **{roles["project"]: web["id"], roles["hours"]: 4, roles["status"]: "Hecha"})
    _fila(client, tareas, "Textos", **{roles["project"]: web["id"], roles["hours"]: 2})
    _fila(client, tareas, "Login", **{roles["project"]: app["id"]})
    _fila(client, tareas, "Suelta")

    r = client.get(f"/databases/{proyectos['id']}/project-progress")
    assert r.status_code == 200, r.text
    avance = r.json()
    assert avance[web["id"]] == {"count": 2, "closed": 1, "hours": 6.0, "hours_open": 2.0}
    assert avance[app["id"]]["count"] == 1 and avance[app["id"]]["closed"] == 0
    assert vacio["id"] not in avance
    # Coincide con el detalle del proyecto.
    detalle = client.get(f"/databases/{proyectos['id']}/rows/{web['id']}/tasks").json()["totals"]
    assert (detalle["count"], detalle["closed"], detalle["hours"]) == (2, 1, 6.0)


def test_el_avance_solo_es_de_bases_de_proyectos(client):
    tareas = _crear(client, "tareas", title="T")
    assert client.get(f"/databases/{tareas['id']}/project-progress").status_code == 400
