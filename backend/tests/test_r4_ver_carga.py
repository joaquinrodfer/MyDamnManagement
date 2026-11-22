"""Plan de roles, R4 (= C2 del plan de la carga): «Ver carga» por espacio.

- Con el permiso en unos espacios se ve a sus miembros, con todo lo suyo junto
  (lo de otros espacios que no ve, enmascarado).
- `?workspace=` deja solo lo de ese espacio y aplica la dedicación a la capacidad.
- El equipo del inicio sigue la misma regla.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest


def _ok(r, code=200):
    assert r.status_code == code, f"{r.status_code} {r.text}"
    return r.json() if r.content and code != 204 else None


@pytest.fixture()
def escena(client):
    from fastapi.testclient import TestClient

    from app.main import app

    ids = {"admin": _ok(client.get("/auth/me"))["id"]}
    for nombre in ("ana", "beto", "carla"):
        ids[nombre] = _ok(client.post("/auth/users", json={
            "username": nombre, "display_name": nombre.title(), "password": f"contrasena-{nombre}",
        }), 201)["id"]
    dev = _ok(client.post("/workspaces", json={"name": "Desarrollo"}), 201)
    _ok(client.put(f"/workspaces/{dev['id']}/members/user/{ids['beto']}", json={"role": "editor"}))
    # Tareas en General (Beto y Carla) y en Desarrollo (Beto).
    general = _ok(client.post("/databases/from-template", json={"template": "tareas", "title": "Tareas"}))
    desarrollo = _ok(client.post(
        "/databases/from-template", json={"template": "tareas", "title": "Tareas dev"},
        headers={"X-Workspace": dev["id"]},
    ))
    for base, quien, titulo, horas in (
        (general, "beto", "Informe", 4), (general, "carla", "Factura", 6), (desarrollo, "beto", "API", 10),
    ):
        _ok(client.post(f"/databases/{base['id']}/rows", json={
            "title": titulo, "properties": {"responsable": ids[quien], "estado": "Por hacer", "horas": horas},
        }))

    @contextmanager
    def como(nombre):
        with TestClient(app) as c:
            _ok(c.post("/auth/login", json={"username": nombre, "password": f"contrasena-{nombre}"}))
            yield c

    with como("ana") as ana:
        yield {"ana": ana, "ids": ids, "dev": dev}


def _dar(client, ids, donde):
    _ok(client.post("/grants", json={
        "capability": "view_workload", "subject_type": "user", "subject_id": ids["ana"], "workspace_id": donde,
    }), 201)


def test_sin_permiso_no_hay_carga(client, escena):
    assert escena["ana"].get("/workload").status_code == 403


def test_en_un_espacio_ve_a_sus_miembros_con_todo_lo_suyo(client, escena):
    ana, ids, dev = escena["ana"], escena["ids"], escena["dev"]
    _dar(client, ids, dev["id"])
    informe = _ok(ana.get("/workload"))
    assert {p["id"] for p in informe["people"]} == {ids["beto"]}
    beto = informe["people"][0]
    # Combinada: lo de General y lo de Desarrollo, enmascarado porque Ana no lo ve.
    assert beto["unscheduled"] == 14.0
    assert {p["workspace_name"] for p in beto["pieces"]} == {"General", "Desarrollo"}
    assert all(p.get("masked") for p in beto["pieces"])
    assert [w["name"] for w in informe["workspaces"]] == ["Desarrollo"]
    # Otro espacio no se puede pedir.
    general = next(w for w in _ok(client.get("/workspaces")) if w["name"] == "General")
    assert ana.get("/workload", params={"workspace": general["id"]}).status_code == 404


def test_por_espacio_con_la_dedicacion(client, escena):
    ana, ids, dev = escena["ana"], escena["ids"], escena["dev"]
    _dar(client, ids, None)
    general = next(w for w in _ok(client.get("/workspaces")) if w["name"] == "General")
    _ok(client.put(f"/worktime/users/{ids['beto']}/dedication", json={"dedication": {dev["id"]: 60, general["id"]: 40}}))
    solo_dev = _ok(ana.get("/workload", params={"workspace": dev["id"]}))
    assert {p["id"] for p in solo_dev["people"]} == {ids["beto"]}
    beto = solo_dev["people"][0]
    assert beto["unscheduled"] == 10.0
    assert beto["capacity"] == 24.0  # 60 % de 40 h
    assert solo_dev["workspace_id"] == dev["id"]
    # Sin filtro: todos y todo.
    todo = _ok(ana.get("/workload"))
    assert {p["id"] for p in todo["people"]} == {ids["beto"], ids["carla"]}
    assert next(p for p in todo["people"] if p["id"] == ids["beto"])["capacity"] == 40.0


def test_el_equipo_del_inicio_sigue_la_misma_regla(client, escena):
    ana, ids, dev = escena["ana"], escena["ids"], escena["dev"]
    assert _ok(ana.get("/dashboard"))["week"]["team"] is None
    _dar(client, ids, dev["id"])
    equipo = _ok(ana.get("/dashboard"))["week"]["team"]
    assert {p["id"] for p in equipo} == {ids["beto"]}
