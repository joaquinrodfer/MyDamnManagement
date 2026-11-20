"""El inicio (dashboard 2.0): mis tareas con su cierre, avisos y recientes.

`GET /dashboard` trae de una vez lo que pinta el inicio: las tareas abiertas del
usuario (lo mismo que `GET /my-tasks`), los últimos avisos sin leer y las
páginas recientes con su portada.
"""

from __future__ import annotations

from datetime import date, timedelta


def _yo(client) -> str:
    return client.get("/auth/me").json()["id"]


def _base_de_tareas(client, tipado: bool = True) -> dict:
    base = client.post(
        "/databases",
        json={
            "title": "Tareas",
            "schema_def": [
                {"key": "responsable", "name": "Responsable", "type": "person"},
                {"key": "estado", "name": "Estado", "type": "select", "options": ["Abierto", "En curso", "Hecho"]},
                {"key": "vence", "name": "Vence", "type": "date"},
            ],
            "closed": {"property": "estado", "values": ["hecho"]},
        },
    )
    assert base.status_code == 200, base.text
    base = base.json()
    if tipado:
        r = client.put(
            f"/databases/{base['id']}/kind",
            json={"type": "projects", "roles": {"assignee": "responsable", "status": "estado", "end": "vence"}},
        )
        assert r.status_code == 200, r.text
    return base


def _fila(client, base, titulo, **props):
    r = client.post(
        f"/databases/{base['id']}/rows",
        json={"title": titulo, "properties": {"responsable": _yo(client), **props}},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_el_inicio_trae_mis_tareas_con_su_cierre(client):
    base = _base_de_tareas(client)
    hace_tres = (date.today() - timedelta(days=3)).isoformat()
    fila = _fila(client, base, "Entregar informe", estado="En curso", vence=hace_tres)

    datos = client.get("/dashboard").json()

    tareas = datos["tasks"]
    assert tareas["counts"]["overdue"] == 1
    [tarea] = tareas["tasks"]
    assert tarea["title"] == "Entregar informe"
    assert tarea["due"] == "overdue"
    # El valor de cierre es la opción real («Hecho»), no lo que guarda la regla.
    assert tarea["close"] == {"key": "estado", "value": "Hecho"}

    # Marcarla hecha desde el inicio es escribir ese valor en la fila.
    r = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {tarea["close"]["key"]: tarea["close"]["value"]}},
    )
    assert r.status_code == 200, r.text
    assert client.get("/dashboard").json()["tasks"]["count"] == 0


def test_en_una_base_libre_la_fecha_pasada_no_es_vencida(client):
    base = _base_de_tareas(client, tipado=False)
    _fila(client, base, "Idea vieja", estado="Abierto", vence=(date.today() - timedelta(days=3)).isoformat())

    tareas = client.get("/dashboard").json()["tasks"]

    assert tareas["counts"]["overdue"] == 0
    assert [t["due"] for t in tareas["tasks"]] == ["past"]


def test_el_inicio_trae_avisos_y_recientes(client):
    nota = client.post("/pages", json={"title": "Plan", "type": "note"})
    assert nota.status_code in (200, 201), nota.text

    datos = client.get("/dashboard").json()

    assert set(datos["notifications"]) == {"unread", "items"}
    assert isinstance(datos["mentions_unread"], int)
    reciente = next(p for p in datos["recent_pages"] if p["title"] == "Plan")
    assert "header_image" in reciente and "header_position" in reciente
