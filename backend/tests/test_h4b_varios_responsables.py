"""H4 (2/3): varios responsables por tarea (`person` con `multiple`).

Una propiedad `person` puede admitir varias personas por celda. Lo que cambia:
la validación, cómo se leen y se listan (`people`), Mis tareas, los filtros y
la carga (`hours_split`).
"""

from datetime import date, timedelta

import pytest

from test_h1_espacios import _sesion, cierra  # noqa: F401 - fixtures


def _tareas(client, titulo="Tareas"):
    return client.post("/databases/from-template", json={"template": "tareas", "title": titulo, "section": "shared"}).json()


def test_la_plantilla_de_tareas_admite_varios_responsables(client):
    base = _tareas(client)
    resp = next(p for p in base["schema_def"] if p["key"] == "responsable")
    assert resp["type"] == "person" and resp["multiple"] in (True, "true") and resp["name"] == "Responsables"


def test_una_celda_con_varias_personas(client, cierra):
    ana, a = _sesion(client, "anita")
    beto, b = _sesion(client, "betito")
    cierra += [ana, beto]
    base = _tareas(client)
    fila = client.post(f"/databases/{base['id']}/rows", json={"title": "Entre dos", "properties": {"responsable": [a["id"], b["id"], a["id"]]}}).json()
    assert fila["properties"]["responsable"] == [a["id"], b["id"]]  # sin repetir, en orden
    assert [p["display_name"] for p in fila["people"]["responsable"]] == ["Anita", "Betito"]
    # Una sola persona (texto) también vale y se guarda como lista.
    otra = client.post(f"/databases/{base['id']}/rows", json={"title": "Una", "properties": {"responsable": a["id"]}}).json()
    assert otra["properties"]["responsable"] == [a["id"]]
    # Usuarios que no existen se rechazan; vaciar la celda la deja sin asignar.
    mala = client.post(f"/databases/{base['id']}/rows", json={"title": "X", "properties": {"responsable": [a["id"], "nadie"]}})
    assert mala.status_code == 400 and "no existe" in mala.json()["detail"]
    vacia = client.patch(f"/databases/{base['id']}/rows/{fila['id']}", json={"properties": {"responsable": []}}).json()
    assert vacia["properties"].get("responsable") in (None, [])


def test_una_persona_sola_sigue_siendo_una_sola(client, cierra):
    ana, a = _sesion(client, "anita")
    cierra.append(ana)
    base = client.post("/databases", json={"title": "Normal", "schema_def": [{"name": "Quién", "type": "person"}]}).json()
    ok = client.post(f"/databases/{base['id']}/rows", json={"title": "A", "properties": {"quien": a["id"]}})
    assert ok.status_code == 200 and ok.json()["properties"]["quien"] == a["id"]
    assert client.post(f"/databases/{base['id']}/rows", json={"title": "B", "properties": {"quien": [a["id"]]}}).status_code == 400


def test_no_se_cambia_si_admite_varias_personas(client):
    base = _tareas(client)
    esquema = [dict(p, multiple=False) if p["key"] == "responsable" else p for p in base["schema_def"]]
    r = client.patch(f"/databases/{base['id']}", json={"schema_def": esquema})
    assert r.status_code == 400 and "varias personas" in r.json()["detail"]
    # Sin tocar `multiple`, se conserva al guardar el resto del esquema.
    mismo = [{k: v for k, v in p.items() if k != "multiple"} if p["key"] == "responsable" else p for p in base["schema_def"]]
    ok = client.patch(f"/databases/{base['id']}", json={"schema_def": mismo})
    assert ok.status_code == 200 and next(p for p in ok.json()["schema_def"] if p["key"] == "responsable")["multiple"] in (True, "true")


def test_mis_tareas_incluye_las_compartidas(client, cierra):
    ana, a = _sesion(client, "anita")
    cierra.append(ana)
    base = _tareas(client)
    yo = client.get("/auth/me").json()["id"]
    client.post(f"/databases/{base['id']}/rows", json={"title": "De las dos", "properties": {"responsable": [yo, a["id"]], "estado": "En curso"}})
    client.post(f"/databases/{base['id']}/rows", json={"title": "Solo mía", "properties": {"responsable": yo, "estado": "En curso"}})
    suyas = {t["title"] for t in ana.get("/my-tasks").json()["tasks"]}
    mias = {t["title"] for t in client.get("/my-tasks").json()["tasks"]}
    assert suyas == {"De las dos"} and mias == {"De las dos", "Solo mía"}


def test_filtrar_y_ordenar_por_varias_personas(client, cierra):
    ana, a = _sesion(client, "anita")
    beto, b = _sesion(client, "betito")
    cierra += [ana, beto]
    base = _tareas(client)
    for titulo, quienes in (("AB", [a["id"], b["id"]]), ("B", [b["id"]]), ("Nadie", [])):
        client.post(f"/databases/{base['id']}/rows", json={"title": titulo, "properties": {"responsable": quienes}})
    vista = client.post(f"/databases/{base['id']}/views", json={"name": "De Anita", "type": "table", "config": {
        "filters": [{"key": "responsable", "op": "eq", "value": a["id"]}]}}).json()
    filas = client.get(f"/databases/{base['id']}/rows", params={"view": vista["id"]}).json()
    assert [f["title"] for f in filas] == ["AB"]
    vacias = client.post(f"/databases/{base['id']}/views", json={"name": "Sin", "type": "table", "config": {
        "filters": [{"key": "responsable", "op": "is_empty"}]}}).json()
    assert [f["title"] for f in client.get(f"/databases/{base['id']}/rows", params={"view": vacias["id"]}).json()] == ["Nadie"]


def test_la_carga_reparte_segun_hours_split(client, cierra):
    ana, a = _sesion(client, "anita")
    beto, b = _sesion(client, "betito")
    cierra += [ana, beto]
    base = _tareas(client)
    fin = (date.today() + timedelta(days=0)).isoformat()
    client.post(f"/databases/{base['id']}/rows", json={"title": "Compartida", "properties": {
        "responsable": [a["id"], b["id"]], "estado": "En curso", "fin": fin, "horas": 10}})

    def horas(carga):
        return {p["id"]: sum(carga_p["hours"] for carga_p in p["pieces"]) for p in carga["people"]}

    completas = horas(client.get("/workload").json())
    assert completas == {a["id"]: 10.0, b["id"]: 10.0}  # `full`: las horas completas a cada uno
    client.put(f"/databases/{base['id']}/kind", json={"type": "tasks", "options": {"hours_split": "equal"}})
    assert horas(client.get("/workload").json()) == {a["id"]: 5.0, b["id"]: 5.0}


def test_exportar_y_reindexar_con_varias_personas(client, cierra):
    ana, a = _sesion(client, "anita")
    cierra.append(ana)
    base = _tareas(client)
    fila = client.post(f"/databases/{base['id']}/rows", json={"title": "Persistente", "properties": {"responsable": [a["id"]]}}).json()
    assert client.post("/reindex?force=true").status_code == 200
    leida = client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()
    assert leida["properties"]["responsable"] == [a["id"]]
