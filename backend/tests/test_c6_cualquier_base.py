"""Plan de la carga, C6: cualquier base cuenta para la carga («workload»)."""

from __future__ import annotations


def _ok(r, code=None):
    assert r.status_code in ((code,) if code else (200, 201)), f"{r.status_code} {r.text}"
    return r.json()


def test_una_base_de_tickets_cuenta_con_solo_el_responsable(client):
    yo = _ok(client.get("/auth/me"))["id"]
    general = next(w for w in _ok(client.get("/workspaces")) if w["name"] == "General")
    base = _ok(client.post("/databases", json={"title": "Tickets", "schema_def": [
        {"name": "Asignado", "type": "person"},
        {"name": "Prioridad", "type": "select", "options": ["Alta", "Baja"]},
        {"name": "Fecha", "type": "date"},
    ]}))
    claves = {p["name"]: p["key"] for p in base["schema_def"]}
    convertida = _ok(client.put(f"/databases/{base['id']}/kind", json={
        "type": "workload", "roles": {"assignee": claves["Asignado"], "end": claves["Fecha"]},
    }))
    assert convertida["kind"]["type"] == "workload"
    # No se ha creado ninguna columna: con el responsable basta.
    assert len(convertida["schema_def"]) == 3
    # «Número de tareas»: cada ticket, 2 h.
    _ok(client.patch(f"/workspaces/{general['id']}", json={"settings": {"workload": {"unit": "tasks", "hours_per_unit": 2}}}))
    for titulo in ("Caída del correo", "Impresora"):
        _ok(client.post(f"/databases/{base['id']}/rows", json={"title": titulo, "properties": {
            claves["Asignado"]: yo, claves["Fecha"]: "2026-11-18",
        }}))
    d = _ok(client.get("/workload/grid", params={"scale": "day", "start": "2026-11-18", "periods": 1}))
    assert d["rows"][0]["hours"]["2026-11-18"] == 4.0
    assert {t["base_title"] for t in d["rows"][0]["cells"]["2026-11-18"]} == {"Tickets"}
    # La columna del responsable queda protegida, como en Tareas.
    sin_resp = [p for p in convertida["schema_def"] if p["key"] != claves["Asignado"]]
    assert client.patch(f"/databases/{base['id']}", json={"schema_def": sin_resp}).status_code == 400


def test_sin_persona_se_crea_el_responsable(client):
    base = _ok(client.post("/databases", json={"title": "Turnos", "schema_def": [{"name": "Día", "type": "date"}]}))
    convertida = _ok(client.put(f"/databases/{base['id']}/kind", json={"type": "workload"}))
    tipos = [p["type"] for p in convertida["schema_def"]]
    assert "person" in tipos and convertida["kind"]["roles"]["assignee"]
