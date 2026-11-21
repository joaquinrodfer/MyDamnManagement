"""Jornada, dedicación, festivos y ausencias (plan de la carga, C1).

La capacidad de cada persona se calcula día a día: su jornada, menos los
festivos (comunes y de sus espacios) y sus ausencias. Las horas de una tarea se
reparten en los días que esa persona trabaja.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import worktime
from app.index import open_index

LUNES = date(2026, 11, 16)


@pytest.fixture
def conn(tmp_path):
    return open_index(str(tmp_path / "i.sqlite"))


def test_la_jornada_por_defecto_y_la_de_antes(conn):
    from app import index as index_mod

    assert worktime.schedule_of(conn, "u1") == [8, 8, 8, 8, 8, 0, 0]
    # La capacidad semanal que se guardaba antes vale como jornada de L a V.
    index_mod.set_meta(conn, worktime.META_CAPACITY, '{"u1": 30}')
    assert worktime.schedule_of(conn, "u1") == [6, 6, 6, 6, 6, 0, 0]
    # Una jornada propia la sustituye y olvida lo de antes.
    worktime.set_schedule(conn, "u1", [8, 8, 8, 8, 3, 0, 0])
    assert worktime.schedule_of(conn, "u1") == [8, 8, 8, 8, 3, 0, 0]
    worktime.set_schedule(conn, "u1", None)
    assert worktime.schedule_of(conn, "u1") == [8, 8, 8, 8, 8, 0, 0]
    with pytest.raises(ValueError):
        worktime.set_schedule(conn, "u1", [8, 8, 8])
    with pytest.raises(ValueError):
        worktime.set_schedule(conn, "u1", [25, 0, 0, 0, 0, 0, 0])


def test_festivos_y_ausencias_recortan_la_semana(conn):
    worktime.add_holiday(conn, None, "2026-11-17", "Fiesta común")
    worktime.add_absence(conn, "u1", "2026-11-19", "2026-11-20", "vacaciones")
    cal = worktime.Calendar(conn)
    semana = cal.week("u1", LUNES)
    assert semana == {"capacity": 16.0, "nominal": 40.0, "holidays": 1, "absences": 2}
    # Una ausencia parcial resta horas, no el día.
    worktime.add_absence(conn, "u1", "2026-11-16", None, "personal", hours=3)
    assert worktime.Calendar(conn).available("u1", LUNES) == 5.0


def test_el_festivo_de_un_espacio_solo_cuenta_a_sus_miembros(conn):
    conn.execute("INSERT INTO workspaces (id, name, created_at) VALUES ('w1', 'Oficina', '2026-01-01')")
    worktime.add_holiday(conn, "w1", "2026-11-18", "Fiesta local")
    assert worktime.Calendar(conn).holiday("u1", date(2026, 11, 18)) is None
    conn.execute(
        "INSERT INTO workspace_members (workspace_id, subject_type, subject_id, role) VALUES ('w1', 'user', 'u1', 'editor')"
    )
    assert worktime.Calendar(conn).holiday("u1", date(2026, 11, 18)) == "Fiesta local"


def test_las_horas_se_reparten_en_los_dias_que_trabaja(conn):
    worktime.set_schedule(conn, "u1", [8, 8, 0, 8, 4, 0, 0])  # el miércoles no trabaja
    worktime.add_holiday(conn, None, "2026-11-17", "Fiesta")
    cal = worktime.Calendar(conn)
    reparto = dict(cal.spread("u1", LUNES, date(2026, 11, 22), 20))
    # Lunes 8, jueves 8, viernes 4 → 20 h en proporción.
    assert reparto == {LUNES: 8.0, date(2026, 11, 19): 8.0, date(2026, 11, 20): 4.0}
    # De vacaciones todo el rango: se reparte igual en su jornada (se ve la sobrecarga).
    worktime.add_absence(conn, "u1", LUNES, "2026-11-22", "vacaciones")
    cal = worktime.Calendar(conn)
    assert sum(h for _, h in cal.spread("u1", LUNES, date(2026, 11, 22), 20)) == pytest.approx(20)
    # Una sola fecha: todo ese día. Sin fechas: sin día.
    assert cal.spread("u1", None, date(2026, 11, 21), 5) == [(date(2026, 11, 21), 5)]
    assert cal.spread("u1", None, None, 5) == [(None, 5)]


def test_la_dedicacion_no_pasa_del_100(conn):
    conn.execute("INSERT INTO workspaces (id, name, created_at) VALUES ('w1', 'A', '2026-01-01')")
    conn.execute("INSERT INTO workspaces (id, name, created_at) VALUES ('w2', 'B', '2026-01-01')")
    assert worktime.set_dedication(conn, "u1", {"w1": 60, "w2": 40}) == {"w1": 60, "w2": 40}
    cal = worktime.Calendar(conn)
    assert cal.dedication("u1", "w1") == 60 and cal.dedication("u1", "w2") == 40
    with pytest.raises(ValueError):
        worktime.set_dedication(conn, "u1", {"w1": 70, "w2": 40})
    with pytest.raises(ValueError):
        worktime.set_dedication(conn, "u1", {"nada": 10})
    worktime.set_dedication(conn, "u1", {})
    assert worktime.Calendar(conn).dedication("u1", "w1") == 100


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def _gente(client):
    from app.main import app

    r = client.post("/auth/users", json={"username": "bea", "display_name": "Bea", "password": "contrasena-bea"})
    assert r.status_code == 201, r.text

    @contextmanager
    def como():
        with TestClient(app) as c:
            assert c.post("/auth/login", json={"username": "bea", "password": "contrasena-bea"}).status_code == 200
            yield c

    return r.json()["id"], como


def test_solo_el_administrador_cambia_la_jornada(client):
    bea, como = _gente(client)
    r = client.put(f"/worktime/users/{bea}/schedule", json={"hours": [7, 7, 7, 7, 7, 0, 0]})
    assert r.status_code == 200, r.text
    assert r.json()["weekly"] == 35 and r.json()["custom"] is True
    with como() as c:
        assert c.get(f"/worktime/users/{bea}").json()["schedule"] == [7, 7, 7, 7, 7, 0, 0]
        assert c.put(f"/worktime/users/{bea}/schedule", json={"hours": [9] * 7}).status_code == 403
        yo = client.get("/auth/me").json()["id"]
        assert c.get(f"/worktime/users/{yo}").status_code == 403


def test_cada_uno_registra_sus_ausencias(client):
    bea, como = _gente(client)
    yo = client.get("/auth/me").json()["id"]
    with como() as c:
        r = c.post("/absences", json={"start": "2026-12-22", "end": "2026-12-31", "kind": "vacaciones"})
        assert r.status_code == 201, r.text
        mia = r.json()
        assert mia["user_id"] == bea and mia["label"] == "Vacaciones"
        # No puede apuntar ni borrar las de otro.
        assert c.post("/absences", json={"user_id": yo, "start": "2026-12-01"}).status_code == 403
        assert c.post("/absences", json={"start": "2026-12-05", "end": "2026-12-01"}).status_code == 400
        assert c.post("/absences", json={"start": "2026-12-05", "kind": "fiesta"}).status_code == 400
        assert [a["id"] for a in c.get("/absences").json()["absences"]] == [mia["id"]]
    # El administrador ve y borra las de cualquiera.
    assert [a["id"] for a in client.get(f"/absences?user_id={bea}").json()["absences"]] == [mia["id"]]
    assert client.delete(f"/absences/{mia['id']}").status_code == 204


def test_festivos_comunes_solo_el_administrador(client):
    bea, como = _gente(client)
    r = client.post("/holidays", json={"day": "2026-12-08", "name": "Inmaculada"})
    assert r.status_code == 201, r.text
    with como() as c:
        datos = c.get("/holidays?year=2026").json()
        assert [h["name"] for h in datos["holidays"]] == ["Inmaculada"] and datos["can_edit"] is False
        assert c.post("/holidays", json={"day": "2026-12-09"}).status_code == 403
        assert c.delete(f"/holidays/{r.json()['id']}").status_code == 403
    assert client.delete(f"/holidays/{r.json()['id']}").status_code == 204


def test_la_carga_mide_contra_la_semana_real(client):
    yo = client.get("/auth/me").json()["id"]
    lunes = date.today().toordinal() - date.today().weekday()
    lunes = date.fromordinal(lunes)
    client.post("/absences", json={"start": lunes.isoformat(), "end": date.fromordinal(lunes.toordinal() + 1).isoformat()})
    base = client.post("/databases/from-template", json={"template": "tareas", "title": "Tareas"}).json()
    client.post(
        f"/databases/{base['id']}/rows",
        json={"title": "A", "properties": {"responsable": yo, "estado": "Por hacer", "inicio": lunes.isoformat(),
                                           "fin": date.fromordinal(lunes.toordinal() + 4).isoformat(), "horas": 9}},
    )
    persona = client.get("/workload?weeks=1").json()["people"][0]
    semana = lunes.isoformat()
    assert persona["capacity"] == 40 and persona["capacity_weeks"][semana] == 24
    assert persona["time_off"][semana] == {"holidays": 0, "absences": 2}
    # Las 9 h caen entre miércoles y viernes: toda la semana sigue sumando 9.
    assert persona["weeks"][semana] == pytest.approx(9)
    assert client.get("/dashboard").json()["week"]["me"]["capacity"] == 24
