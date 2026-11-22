"""Plan de la carga, C4 (unidad de esfuerzo) y C5 (reglas por espacio y base)."""

from __future__ import annotations

from datetime import date

import pytest

from app import effort, worktime


def _ok(r, code=None):
    assert r.status_code in ((code,) if code else (200, 201)), f"{r.status_code} {r.text}"
    return r.json()


def test_pasar_a_horas():
    cfg = effort.validate({})
    assert effort.to_hours(cfg, 6) == (6, True)
    assert effort.to_hours(cfg, None) == (0, False)
    dias = effort.validate({"unit": "days"})
    assert effort.to_hours(dias, 2) == (16, True)
    puntos = effort.validate({"unit": "points", "hours_per_unit": 3, "default_hours": 2})
    assert effort.to_hours(puntos, 5) == (15, True)
    assert effort.to_hours(puntos, "") == (2, False)
    tallas = effort.validate({"unit": "sizes"})
    assert effort.to_hours(tallas, None, "L") == (8, True)
    assert effort.to_hours(tallas, 3, None) == (3, True)  # sin talla, las horas
    tareas = effort.validate({"unit": "tasks", "hours_per_unit": 2})
    assert effort.to_hours(tareas, None) == (2, True)
    with pytest.raises(ValueError):
        effort.validate({"unit": "litros"})
    with pytest.raises(ValueError):
        effort.validate({"warn": 120, "over": 100})
    # Lo roto en el espacio no rompe la carga: por defecto.
    assert effort.of_settings('{"workload": {"unit": "litros"}}') == effort.DEFAULTS


def test_reparto_en_el_tiempo(client):
    from app.config import settings
    from app.index import open_index

    conn = open_index(settings.index_path)
    try:
        cal = worktime.Calendar(conn)
    finally:
        conn.close()
    lunes, viernes = date(2026, 11, 16), date(2026, 11, 20)
    assert cal.spread("x", lunes, viernes, 10, "start") == [(lunes, 10)]
    assert cal.spread("x", lunes, date(2026, 11, 22), 10, "end") == [(viernes, 10)]
    assert cal.spread("x", lunes, date(2026, 11, 22), 10, "due") == [(date(2026, 11, 22), 10)]
    assert len(cal.spread("x", lunes, viernes, 10, "uniform")) == 5


@pytest.fixture()
def base(client):
    yo = _ok(client.get("/auth/me"))["id"]
    b = _ok(client.post("/databases/from-template", json={"template": "tareas", "title": "Tareas"}))
    general = next(w for w in _ok(client.get("/workspaces")) if w["name"] == "General")
    return {"yo": yo, "base": b, "general": general}


def _fila(client, base, titulo, **props):
    _ok(client.post(f"/databases/{base['id']}/rows", json={"title": titulo, "properties": props}))


def test_la_unidad_del_espacio_cambia_las_horas(client, base):
    yo, b, g = base["yo"], base["base"], base["general"]
    _fila(client, b, "Diseño", responsable=yo, estado="Por hacer", horas=2)
    _fila(client, b, "Sin estimar", responsable=yo, estado="Por hacer")
    r = client.patch(f"/workspaces/{g['id']}", json={"settings": {"workload": {"unit": "litros"}}})
    assert r.status_code == 400
    _ok(client.patch(f"/workspaces/{g['id']}", json={"settings": {"workload": {
        "unit": "days", "default_hours": 3, "warn": 80, "over": 120, "under": 20,
    }}}))
    d = _ok(client.get("/workload/grid", params={"workspace": g["id"]}))
    fila = d["rows"][0]
    assert fila["unscheduled"] == 19.0  # 2 días = 16 h, más 3 h por la no estimada
    assert [t["row_title"] for t in d["gaps"]["unestimated"]] == ["Sin estimar"]
    assert d["thresholds"] == {"warn": 80, "over": 120, "under": 20}
    assert d["unit"]["key"] == "days" and d["unit"]["hours"] == 8
    # Sin filtro de espacio, el semáforo de siempre.
    assert _ok(client.get("/workload/grid"))["thresholds"] == {"warn": 70, "over": 100, "under": 0}


def test_reparto_por_dedicacion_y_en_el_tiempo(client, base):
    yo, b, g = base["yo"], base["base"], base["general"]
    otra = _ok(client.post("/auth/users", json={"username": "ana", "display_name": "Ana", "password": "contrasena-ana"}))
    ws2 = _ok(client.post("/workspaces", json={"name": "Otro"}))
    _ok(client.put(f"/worktime/users/{yo}/dedication", json={"dedication": {g["id"]: 75, ws2["id"]: 25}}))
    _ok(client.put(f"/worktime/users/{otra['id']}/dedication", json={"dedication": {g["id"]: 25, ws2["id"]: 75}}))
    schema = b["kind"]
    r = client.put(f"/databases/{b['id']}/kind", json={
        "type": "tasks", "roles": schema["roles"], "options": {"hours_split": "dedication", "spread": "end"},
    })
    assert r.status_code == 200, r.text
    assert r.json()["kind"]["options"] == {"hours_split": "dedication", "spread": "end"}
    _fila(client, b, "Juntos", responsable=[yo, otra["id"]], estado="Por hacer",
          inicio="2026-11-16", fin="2026-11-20", horas=8)
    d = _ok(client.get("/workload/grid", params={"scale": "day", "start": "2026-11-16", "periods": 5}))
    filas = {f["id"]: f for f in d["rows"]}
    assert filas[yo]["hours"]["2026-11-20"] == 6.0  # 75 % el último día
    assert filas[otra["id"]]["hours"]["2026-11-20"] == 2.0
    assert filas[yo]["hours"]["2026-11-16"] == 0
    assert client.put(f"/databases/{b['id']}/kind", json={
        "type": "tasks", "roles": schema["roles"], "options": {"spread": "luego"},
    }).status_code == 400


def test_tallas_con_la_columna_talla(client, base):
    yo, b, g = base["yo"], base["base"], base["general"]
    esquema = b["schema_def"] + [{"name": "Talla", "type": "select", "options": ["S", "M", "L"]}]
    col = _ok(client.patch(f"/databases/{b['id']}", json={"schema_def": esquema}))
    clave = next(p["key"] for p in col["schema_def"] if p["name"] == "Talla")
    roles = {**b["kind"]["roles"], "size": clave}
    _ok(client.put(f"/databases/{b['id']}/kind", json={"type": "tasks", "roles": roles}))
    _ok(client.patch(f"/workspaces/{g['id']}", json={"settings": {"workload": {"unit": "sizes", "sizes": {"S": 2, "M": 5, "L": 10}}}}))
    _fila(client, b, "Mediana", responsable=yo, estado="Por hacer", **{clave: "M"})
    d = _ok(client.get("/workload/grid"))
    assert d["rows"][0]["unscheduled"] == 5.0
