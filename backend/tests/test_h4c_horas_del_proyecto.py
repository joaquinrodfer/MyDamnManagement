"""H4 (3/3): las tareas de un proyecto y sus totales; tareas entre espacios."""

import pytest

from test_h1_espacios import _sesion, cierra  # noqa: F401 - fixtures


def _proyectos(client, titulo="Proyectos", **extra):
    return client.post("/databases/from-template", json={"template": "proyectos", "title": titulo, "section": "shared", **extra}).json()


def _tareas(client, proyectos, titulo="Tareas", headers=None):
    return client.post("/databases/from-template", json={"template": "tareas", "title": titulo, "section": "shared",
                       "project_database": proyectos["id"]}, headers=headers or {}).json()


def _fila(client, base, titulo, headers=None, **props):
    r = client.post(f"/databases/{base['id']}/rows", json={"title": titulo, "properties": props}, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


def test_horas_y_avance_de_un_proyecto(client):
    # Proyecto con estimación.
    proyectos = _proyectos(client)
    client.put(f"/databases/{proyectos['id']}/kind", json={"type": "projects", "estimate": True})
    proyectos = client.get(f"/databases/{proyectos['id']}").json()
    est = proyectos["kind"]["roles"]["estimate"]
    web = _fila(client, proyectos, "Web nueva", **{est: 120})
    otro = _fila(client, proyectos, "Otro proyecto")
    base = _tareas(client, proyectos)
    clave = base["kind"]["roles"]["project"]
    _fila(client, base, "Maquetar", **{clave: web["id"]}, estado="En curso", horas=40, fin="2026-12-01")
    _fila(client, base, "API", **{clave: web["id"]}, estado="Hecha", horas=30)
    _fila(client, base, "Tests", **{clave: web["id"]}, estado="Por hacer", horas=25)
    _fila(client, base, "De otro", **{clave: otro["id"]}, estado="En curso", horas=99)
    _fila(client, base, "Suelta (sin proyecto)", estado="En curso", horas=7)
    r = client.get(f"/databases/{proyectos['id']}/rows/{web['id']}/tasks").json()
    assert r["project"]["title"] == "Web nueva"
    assert r["totals"] == {"count": 3, "closed": 1, "hours": 95.0, "hours_open": 65.0, "estimate": 120.0}
    # Las abiertas primero (por fecha), las hechas al final.
    assert [t["title"] for t in r["tasks"]] == ["Maquetar", "Tests", "API"]
    assert [t["closed"] for t in r["tasks"]] == [False, False, True]
    assert client.get(f"/databases/{proyectos['id']}/rows/{otro['id']}/tasks").json()["totals"]["hours"] == 99.0


def test_solo_las_bases_de_proyectos_tienen_tareas(client):
    normal = client.post("/databases", json={"title": "N", "schema_def": []}).json()
    fila = client.post(f"/databases/{normal['id']}/rows", json={"title": "x"}).json()
    assert client.get(f"/databases/{normal['id']}/rows/{fila['id']}/tasks").status_code == 400


def test_lo_que_no_se_ve_cuenta_pero_sale_sin_titulo(client, cierra):
    proyectos = _proyectos(client)
    web = _fila(client, proyectos, "Web")
    base = _tareas(client, proyectos)
    clave = base["kind"]["roles"]["project"]
    _fila(client, base, "Pública", **{clave: web["id"]}, horas=10)
    privada = client.post("/databases/from-template", json={"template": "tareas", "title": "Reservada", "section": "private",
                          "project_database": proyectos["id"]}).json()
    _fila(client, privada, "Secreta", **{privada["kind"]["roles"]["project"]: web["id"]}, horas=5)
    ana, _ = _sesion(client, "anita")
    cierra.append(ana)
    # Ana ve el proyecto (compartido) pero no la base privada: las horas cuentan, el título no.
    r = ana.get(f"/databases/{proyectos['id']}/rows/{web['id']}/tasks").json()
    assert r["totals"]["hours"] == 15.0 and r["totals"]["count"] == 2
    secreta = next(t for t in r["tasks"] if t["masked"])
    assert secreta["title"] == "Tarea privada" and secreta["id"] is None and secreta["base_title"] is None
    assert any(t["title"] == "Pública" for t in r["tasks"])


def test_tareas_de_otro_espacio_solo_si_el_proyecto_lo_acepta(client):
    otro = client.post("/workspaces", json={"name": "Desarrollo"}).json()
    proyectos = _proyectos(client)  # en General
    web = _fila(client, proyectos, "Web")
    h = {"X-Workspace": otro["id"]}
    tareas = _tareas(client, proyectos, "Tareas dev", headers=h)  # base en «Desarrollo»
    clave = tareas["kind"]["roles"]["project"]
    # El espacio del proyecto no acepta tareas de fuera: no se puede colgar.
    r = client.post(f"/databases/{tareas['id']}/rows", json={"title": "Dev", "properties": {clave: web["id"]}}, headers=h)
    assert r.status_code == 400 and "no acepta tareas de fuera" in r.json()["detail"]
    # Con la opción encendida, sí, y salen en el proyecto.
    ajustes = client.patch(f"/workspaces/{'195c0189-0143-59ae-9c83-e4ef4fc86a51'}", json={"settings": {"accept_foreign_tasks": True}})
    assert ajustes.status_code == 200 and ajustes.json()["settings"]["accept_foreign_tasks"] is True
    fila = _fila(client, tareas, "Dev", headers=h, **{clave: web["id"]}, horas=12)
    r = client.get(f"/databases/{proyectos['id']}/rows/{web['id']}/tasks").json()
    assert [t["title"] for t in r["tasks"]] == ["Dev"] and r["totals"]["hours"] == 12.0
    # Y si se apaga, dejan de contar en el proyecto (la tarea sigue existiendo).
    client.patch("/workspaces/195c0189-0143-59ae-9c83-e4ef4fc86a51", json={"settings": {"accept_foreign_tasks": False}})
    assert client.get(f"/databases/{proyectos['id']}/rows/{web['id']}/tasks").json()["totals"]["count"] == 0
    assert fila["id"]
