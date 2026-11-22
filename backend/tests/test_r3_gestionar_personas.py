"""Plan de roles, R3: «Gestionar personas» y la privacidad de las ausencias.

- `GET /worktime/people`: a quién gestiono (todos, o los de mis espacios).
- En la carga, `manageable` dice a quién puedo abrir la «Jornada».
- El tipo y la nota de una ausencia solo los ven la persona y quien la gestiona:
  la carga solo cuenta días («Ausente»), nunca el tipo.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest


def _ok(r, code=200):
    assert r.status_code == code, f"{r.status_code} {r.text}"
    return r.json() if r.content and code != 204 else None


@pytest.fixture()
def gente(client):
    from fastapi.testclient import TestClient

    from app.main import app

    ids = {"admin": _ok(client.get("/auth/me"))["id"]}
    for nombre in ("ana", "beto", "carla"):
        ids[nombre] = _ok(client.post("/auth/users", json={
            "username": nombre, "display_name": nombre.title(), "password": f"contrasena-{nombre}",
        }), 201)["id"]

    @contextmanager
    def como(nombre):
        with TestClient(app) as c:
            _ok(c.post("/auth/login", json={"username": nombre, "password": f"contrasena-{nombre}"}))
            yield c

    with como("ana") as ana, como("beto") as beto, como("carla") as carla:
        yield {"ana": ana, "beto": beto, "carla": carla, "ids": ids}


def test_a_quien_gestiono(client, gente):
    ana, beto, ids = gente["ana"], gente["beto"], gente["ids"]
    assert _ok(beto.get("/worktime/people")) == {"all": False, "people": []}
    dev = _ok(client.post("/workspaces", json={"name": "Desarrollo"}), 201)
    for quien in ("ana", "beto"):
        _ok(client.put(f"/workspaces/{dev['id']}/members/user/{ids[quien]}", json={"role": "editor"}))
    _ok(client.post("/grants", json={
        "capability": "manage_people", "subject_type": "user", "subject_id": ids["ana"], "workspace_id": dev["id"],
    }), 201)
    gestiono = _ok(ana.get("/worktime/people"))
    assert gestiono["all"] is False
    # Quien creó el espacio también es miembro (el administrador, «Tú»).
    assert {p["display_name"] for p in gestiono["people"]} == {"Ana", "Beto", "Tú"}
    # Todos los espacios: todos.
    assert len(_ok(client.get("/worktime/people"))["people"]) == 4


def test_la_carga_dice_a_quien_puedo_abrir_la_jornada(client, gente):
    ana, ids = gente["ana"], gente["ids"]
    base = client.post("/databases/from-template", json={"template": "tareas", "title": "Tareas"}).json()
    for quien in ("beto", "carla"):
        r = client.post(f"/databases/{base['id']}/rows", json={
            "title": f"Tarea de {quien}", "properties": {"responsable": ids[quien], "estado": "Por hacer", "horas": 4},
        })
        assert r.status_code == 200, r.text
    dev = _ok(client.post("/workspaces", json={"name": "Soporte"}), 201)
    _ok(client.put(f"/workspaces/{dev['id']}/members/user/{ids['beto']}", json={"role": "editor"}))
    for cap, donde in (("view_workload", None), ("manage_people", dev["id"])):
        _ok(client.post("/grants", json={
            "capability": cap, "subject_type": "user", "subject_id": ids["ana"], "workspace_id": donde,
        }), 201)
    informe = _ok(ana.get("/workload"))
    marcas = {p["id"]: p["manageable"] for p in informe["people"]}
    assert marcas == {ids["beto"]: True, ids["carla"]: False}


def test_el_tipo_de_ausencia_no_sale_de_quien_la_gestiona(client, gente):
    ana, beto, carla, ids = gente["ana"], gente["beto"], gente["carla"], gente["ids"]
    _ok(beto.post("/absences", json={"start": "2026-12-01", "end": "2026-12-03", "kind": "baja", "note": "gripe"}), 201)
    # Carla no la gestiona: ni la lista ni la ficha.
    assert carla.get("/absences", params={"user_id": ids["beto"]}).status_code == 403
    assert carla.get(f"/worktime/users/{ids['beto']}").status_code == 403
    # Ana ve la carga de todos, pero no gestiona a nadie: en la carga no hay tipo ni nota.
    _ok(client.post("/grants", json={
        "capability": "view_workload", "subject_type": "user", "subject_id": ids["ana"],
    }), 201)
    texto = json.dumps(_ok(ana.get("/workload", params={"weeks": 26})))
    assert "baja" not in texto and "gripe" not in texto
    assert ana.get("/absences", params={"user_id": ids["beto"]}).status_code == 403
    # Con «Gestionar personas», sí.
    _ok(client.post("/grants", json={
        "capability": "manage_people", "subject_type": "user", "subject_id": ids["ana"],
    }), 201)
    lista = _ok(ana.get("/absences", params={"user_id": ids["beto"]}))["absences"]
    assert [(a["kind"], a["note"]) for a in lista] == [("baja", "gripe")]
