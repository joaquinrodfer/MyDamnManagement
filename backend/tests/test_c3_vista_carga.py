"""Plan de la carga, C3: la rejilla fila × periodo (`GET /workload/grid`)."""

from __future__ import annotations

from datetime import date

import pytest

from app import workload_grid


def _ok(r, code=None):
    assert r.status_code in ((code,) if code else (200, 201)), f"{r.status_code} {r.text}"
    return r.json()


def test_los_periodos():
    lunes = date(2026, 11, 16)
    assert [k for k, _a, _b in workload_grid.periods("day", date(2026, 11, 30), 3)] == ["2026-11-30", "2026-12-01", "2026-12-02"]
    semanas = workload_grid.periods("week", date(2026, 11, 19), 2)
    assert semanas[0] == ("2026-11-16", lunes, date(2026, 11, 22))
    meses = workload_grid.periods("month", date(2026, 11, 19), 3)
    assert [k for k, _a, _b in meses] == ["2026-11", "2026-12", "2027-01"]
    assert meses[1][2] == date(2026, 12, 31)


@pytest.fixture()
def tareas(client):
    yo = _ok(client.get("/auth/me"))["id"]
    base = _ok(client.post("/databases/from-template", json={"template": "tareas", "title": "Tareas"}))
    def fila(titulo, **props):
        _ok(client.post(f"/databases/{base['id']}/rows", json={"title": titulo, "properties": props}))
    # Lunes 16 a viernes 20 de noviembre: 10 h, 2 por día.
    fila("Informe", responsable=yo, estado="Por hacer", inicio="2026-11-16", fin="2026-11-20", horas=10)
    fila("Llamada", responsable=yo, estado="Por hacer", fin="2026-11-18", horas=3)
    fila("Sin fecha", responsable=yo, estado="Por hacer", horas=4)
    fila("Sin estimar", responsable=yo, estado="Por hacer", fin="2026-11-19")
    return yo


def test_por_dias_con_capacidad_y_detalle(client, tareas):
    d = _ok(client.get("/workload/grid", params={"scale": "day", "start": "2026-11-16", "periods": 7}))
    assert [p["key"] for p in d["periods"]][:2] == ["2026-11-16", "2026-11-17"]
    yo = d["rows"][0]
    assert yo["kind"] == "person" and yo["id"] == tareas
    assert yo["hours"]["2026-11-16"] == 2.0
    assert yo["hours"]["2026-11-18"] == 5.0  # 2 del informe + 3 de la llamada
    assert yo["capacity"]["2026-11-18"] == 8.0
    assert yo["capacity"]["2026-11-21"] == 0.0  # sábado
    assert {t["row_title"] for t in yo["cells"]["2026-11-18"]} == {"Informe", "Llamada"}
    assert yo["unscheduled"] == 4.0
    assert d["gaps"]["unscheduled_hours"] == 4.0
    assert [t["row_title"] for t in d["gaps"]["unestimated"]] == ["Sin estimar"]


def test_por_mes_y_por_espacio(client, tareas):
    d = _ok(client.get("/workload/grid", params={"scale": "month", "start": "2026-11-01", "periods": 2, "group": "workspace"}))
    assert [p["key"] for p in d["periods"]] == ["2026-11", "2026-12"]
    general = d["rows"][0]
    assert general["name"] == "General" and general["capacity"] is None
    assert general["hours"]["2026-11"] == 13.0


def test_por_proyecto_y_por_grupo(client, tareas):
    d = _ok(client.get("/workload/grid", params={"start": "2026-11-16", "group": "project"}))
    assert [r["name"] for r in d["rows"]] == ["Sin proyecto"]
    g = _ok(client.post("/groups", json={"name": "Diseño"}))
    _ok(client.put(f"/groups/{g['id']}/members", json={"user_ids": [tareas]}))
    d = _ok(client.get("/workload/grid", params={"start": "2026-11-16", "group": "group"}))
    fila = d["rows"][0]
    assert fila["name"] == "Diseño" and fila["capacity"]["2026-11-16"] == 40.0
    assert fila["hours"]["2026-11-16"] == 13.0


def test_mismos_permisos_que_la_carga(client, tareas):
    from fastapi.testclient import TestClient

    from app.main import app

    _ok(client.post("/auth/users", json={"username": "ana", "display_name": "Ana", "password": "contrasena-ana"}))
    with TestClient(app) as ana:
        _ok(ana.post("/auth/login", json={"username": "ana", "password": "contrasena-ana"}))
        assert ana.get("/workload/grid").status_code == 403
    assert client.get("/workload/grid", params={"scale": "año"}).status_code == 422
