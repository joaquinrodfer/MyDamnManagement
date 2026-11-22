"""Plan de la carga, C8: plantillas por tipo de equipo."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import effort


def _ok(r, code=None):
    assert r.status_code in ((code,) if code else (200, 201)), f"{r.status_code} {r.text}"
    return r.json()


def test_todas_las_plantillas_son_validas():
    assert len(effort.TEMPLATES) >= 5
    for t in effort.TEMPLATES:
        assert effort.validate(t["settings"])["unit"] == t["settings"]["unit"]


def test_aplicar_una_plantilla(client):
    from app.main import app

    lista = _ok(client.get("/workload/templates"))
    assert {t["key"] for t in lista} >= {"agency", "software", "consulting", "operations", "no_estimates"}
    dev = _ok(client.post("/workspaces", json={"name": "Consultora"}))
    w = _ok(client.post(f"/workspaces/{dev['id']}/workload-template", json={"template": "consulting"}))
    assert w["settings"]["workload"]["unit"] == "days"
    assert w["settings"]["workload"]["under"] == 40
    assert client.post(f"/workspaces/{dev['id']}/workload-template", json={"template": "nada"}).status_code == 404
    acciones = _ok(client.get("/audit", params={"action": "workspace.updated"}))["items"]
    assert any((a.get("metadata") or {}).get("workload_template") == "consulting" for a in acciones)
    # Quien no gestiona el espacio no puede.
    _ok(client.post("/auth/users", json={"username": "ana", "display_name": "Ana", "password": "contrasena-ana"}))
    with TestClient(app) as ana:
        _ok(ana.post("/auth/login", json={"username": "ana", "password": "contrasena-ana"}))
        r = ana.post(f"/workspaces/{dev['id']}/workload-template", json={"template": "agency"})
        assert r.status_code in (403, 404)
