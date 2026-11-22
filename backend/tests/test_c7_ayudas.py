"""Plan de la carga, C7: ayudas para planificar."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient


def _ok(r, code=None):
    assert r.status_code in ((code,) if code else (200, 201)), f"{r.status_code} {r.text}"
    return r.json()


def test_la_semana_que_viene_y_lo_no_estimado(client):
    from app.due import server_today

    yo = _ok(client.get("/auth/me"))["id"]
    base = _ok(client.post("/databases/from-template", json={"template": "tareas", "title": "Tareas"}))
    hoy = server_today()
    lunes_que_viene = hoy - timedelta(days=hoy.weekday()) + timedelta(days=7)
    viernes = lunes_que_viene + timedelta(days=4)
    for titulo, props in (
        ("Mudanza", {"inicio": lunes_que_viene.isoformat(), "fin": viernes.isoformat(), "horas": 60}),
        ("Pendiente", {"fin": viernes.isoformat()}),
    ):
        _ok(client.post(f"/databases/{base['id']}/rows", json={
            "title": titulo, "properties": {"responsable": yo, "estado": "Por hacer", **props},
        }))
    semana = _ok(client.get("/dashboard"))["week"]
    assert semana["me"]["next"]["week"] == lunes_que_viene.isoformat()
    assert semana["me"]["next"]["hours"] == 60.0
    assert semana["me"]["next"]["capacity"] == 40.0
    assert semana["me"]["unestimated"] == 1
    assert semana["team"][0]["next_hours"] == 60.0


def test_la_carga_para_elegir_responsable_respeta_los_permisos(client):
    from app.main import app

    yo = _ok(client.get("/auth/me"))["id"]
    ana = _ok(client.post("/auth/users", json={"username": "ana", "display_name": "Ana", "password": "contrasena-ana"}))
    todos = _ok(client.get("/workload/week"))
    assert {yo, ana["id"]} <= set(todos["people"])
    assert todos["people"][ana["id"]] == {"hours": 0.0, "capacity": todos["people"][ana["id"]]["capacity"]}
    with TestClient(app) as c:
        _ok(c.post("/auth/login", json={"username": "ana", "password": "contrasena-ana"}))
        suya = _ok(c.get("/workload/week"))
    # Sin «Ver carga», solo la suya.
    assert set(suya["people"]) == {ana["id"]}
