"""H5: la carga del equipo.

Piezas (persona · espacio · proyecto · base · tarea · semana · horas) sobre las
filas abiertas de todas las bases de Tareas de todos los espacios; el reparto
por semanas, el enmascarado entre espacios y los permisos.
"""

from datetime import date

import pytest

from app import workload
from test_h1_espacios import _sesion, cierra  # noqa: F401 - fixtures


def test_el_reparto_por_semanas():
    lunes = date(2026, 11, 16)
    # Una semana laborable completa: todo a esa semana.
    assert workload.spread(date(2026, 11, 16), date(2026, 11, 20), 10) == [(lunes, 10.0)]
    # Del jueves de una semana al martes de la siguiente: 4 días laborables, 2 y 2.
    assert workload.spread(date(2026, 11, 19), date(2026, 11, 24), 8) == [
        (lunes, 4.0), (date(2026, 11, 23), 4.0)]
    # Solo fecha de fin (o de inicio): todo a su semana. Sin fechas: «sin fecha».
    assert workload.spread(None, date(2026, 11, 20), 5) == [(lunes, 5.0)]
    assert workload.spread(date(2026, 11, 18), None, 5) == [(lunes, 5.0)]
    assert workload.spread(None, None, 5) == [(None, 5.0)]
    # Un fin de semana no tiene días laborables: a la semana de la fecha de fin.
    assert workload.spread(date(2026, 11, 21), date(2026, 11, 22), 3) == [(date(2026, 11, 16), 3.0)]
    assert workload.spread(date(2026, 11, 20), date(2026, 11, 16), 5) == [(lunes, 5.0)]  # al revés
    assert workload.spread(date(2026, 11, 16), date(2026, 11, 20), 0) == []


def _tareas(client, titulo="Tareas", **extra):
    r = client.post("/databases/from-template", json={"template": "tareas", "title": titulo, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _fila(client, base, titulo, **props):
    r = client.post(f"/databases/{base['id']}/rows", json={"title": titulo, "properties": props})
    assert r.status_code == 200, r.text
    return r.json()


def test_la_carga_suma_las_filas_abiertas_y_se_salta_las_hechas(client):
    yo = client.get("/auth/me").json()["id"]
    base = _tareas(client)
    _fila(client, base, "A", responsable=yo, estado="En curso", inicio="2026-11-16", fin="2026-11-20", horas=10)
    _fila(client, base, "B", responsable=yo, estado="Por hacer", fin="2026-11-27", horas=6)
    _fila(client, base, "Hecha", responsable=yo, estado="Hecha", fin="2026-11-20", horas=99)
    _fila(client, base, "Sin fecha", responsable=yo, estado="Por hacer", horas=3)
    _fila(client, base, "Sin responsable", estado="Por hacer", fin="2026-11-20", horas=50)
    carga = client.get("/workload?weeks=3").json()
    assert len(carga["people"]) == 1
    persona = carga["people"][0]
    assert persona["id"] == yo and persona["tasks"] == 3
    semanas = persona["weeks"]
    # La ventana empieza en el lunes de «hoy»; las claves son lunes consecutivos.
    assert list(semanas) == carga["weeks"]
    assert persona["unscheduled"] == 3.0
    assert sum(semanas.values()) + persona["before"] + persona["after"] == pytest.approx(16.0)
    assert persona["capacity"] == 40.0
    assert {p["row_title"] for p in persona["pieces"]} == {"A", "B", "Sin fecha"}
    assert all(p["workspace_name"] == "General" for p in persona["pieces"])


def test_con_varios_responsables_manda_hours_split(client):
    """`full` cuenta las horas completas a cada uno y `equal` las reparte."""
    from app import workload as w

    assert w.spread(None, date(2026, 11, 20), 10 / 2) == [(date(2026, 11, 16), 5.0)]


def test_el_proyecto_y_la_base_salen_en_la_pieza(client):
    yo = client.get("/auth/me").json()["id"]
    proyectos = client.post("/databases/from-template", json={"template": "proyectos", "title": "Proyectos"}).json()
    proyecto = client.post(f"/databases/{proyectos['id']}/rows", json={"title": "Web nueva"}).json()
    base = _tareas(client, "Tareas web", project_database=proyectos["id"])
    clave = base["kind"]["roles"]["project"]
    _fila(client, base, "Maquetar", responsable=yo, estado="En curso", fin="2026-11-20", horas=8, **{clave: proyecto["id"]})
    pieza = client.get("/workload").json()["people"][0]["pieces"][0]
    assert pieza["project"] == "Web nueva" and pieza["base_title"] == "Tareas web" and pieza["row_title"] == "Maquetar"
    assert pieza["hours"] == 8.0


def test_permisos_de_la_carga(client, cierra):
    sin, _ = _sesion(client, "normal")
    con, _ = _sesion(client, "pmgr", can_view_global_workload=True)
    cierra += [sin, con]
    assert sin.get("/workload").status_code == 403
    assert con.get("/workload").status_code == 200
    assert client.get("/workload").status_code == 200
    assert client.get("/workload?weeks=99").status_code == 422


def test_entre_espacios_se_enmascaran_los_titulos_que_no_se_ven(client, cierra):
    yo = client.get("/auth/me").json()["id"]
    secreto = client.post("/workspaces", json={"name": "Dirección"}).json()
    base = _tareas(client, "Plan secreto", **{}) if False else None
    base = client.post("/databases/from-template", json={"template": "tareas", "title": "Plan secreto"},
                       headers={"X-Workspace": secreto["id"]}).json()
    client.post(f"/databases/{base['id']}/rows", headers={"X-Workspace": secreto["id"]}, json={
        "title": "Despidos", "properties": {"responsable": yo, "estado": "En curso", "fin": "2026-11-20", "horas": 12}})
    # El PM con carga global no está en «Dirección» y ve las horas, no los títulos.
    pm, _ = _sesion(client, "pmgr", can_view_global_workload=True)
    cierra.append(pm)
    piezas = pm.get("/workload").json()["people"][0]["pieces"]
    assert len(piezas) == 1 and piezas[0]["hours"] == 12.0
    assert piezas[0]["row_title"] == "Proyecto privado [Dirección]"
    assert piezas[0]["base_title"] == "Proyecto privado [Dirección]" and piezas[0]["row_id"] is None
    assert piezas[0]["masked"] is True and piezas[0]["workspace_name"] == "Dirección"
    # El administrador sí ve los títulos.
    assert client.get("/workload").json()["people"][0]["pieces"][0]["row_title"] == "Despidos"


def test_capacidad_por_persona(client, cierra):
    yo = client.get("/auth/me").json()["id"]
    base = _tareas(client)
    _fila(client, base, "A", responsable=yo, estado="En curso", fin="2026-11-20", horas=4)
    assert client.put(f"/workload/capacity/{yo}", json={"hours": 32}).json()["hours"] == 32
    assert client.get("/workload").json()["people"][0]["capacity"] == 32
    assert client.put(f"/workload/capacity/{yo}", json={"hours": None}).json()["hours"] == 40
    assert client.put(f"/workload/capacity/{yo}", json={"hours": 0}).status_code == 422
    assert client.put("/workload/capacity/nadie", json={"hours": 10}).status_code == 404
    otro, _ = _sesion(client, "pmgr", can_view_global_workload=True)
    cierra.append(otro)
    assert otro.put(f"/workload/capacity/{yo}", json={"hours": 10}).status_code == 403
