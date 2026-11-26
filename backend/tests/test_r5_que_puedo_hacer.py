"""Plan de roles, R5: «Qué puedo hacer» (`GET /auth/me/permissions`)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _ok(r, code=200):
    assert r.status_code == code, f"{r.status_code} {r.text}"
    return r.json() if r.content and code != 204 else None


def test_mis_espacios_y_permisos_con_su_origen(client):
    from app.main import app

    ana_id = _ok(client.post("/auth/users", json={
        "username": "ana", "display_name": "Ana", "password": "contrasena-ana",
    }), 201)["id"]
    dev = _ok(client.post("/workspaces", json={"name": "Desarrollo"}), 201)
    equipo = _ok(client.post("/groups", json={"name": "Equipo"}), 201)
    _ok(client.put(f"/groups/{equipo['id']}/members", json={"user_ids": [ana_id]}))
    _ok(client.put(f"/workspaces/{dev['id']}/members/group/{equipo['id']}", json={"role": "viewer"}))
    _ok(client.post("/grants", json={
        "capability": "view_workload", "subject_type": "group", "subject_id": equipo["id"], "workspace_id": dev["id"],
    }), 201)
    _ok(client.post("/grants", json={"capability": "view_audit", "subject_type": "user", "subject_id": ana_id}), 201)

    with TestClient(app) as ana:
        _ok(ana.post("/auth/login", json={"username": "ana", "password": "contrasena-ana"}))
        _ok(ana.post("/workspaces/personal"))
        yo = _ok(ana.get("/auth/me/permissions"))
    assert yo["is_admin"] is False
    espacios = {w["name"]: w for w in yo["workspaces"]}
    assert espacios["Desarrollo"]["role"] == "viewer" and espacios["Desarrollo"]["via"] == "group"
    assert espacios["Desarrollo"]["group_name"] == "Equipo"
    assert espacios["General"]["via"] == "user"
    assert all(not w["name"].startswith("Espacio de") for w in yo["workspaces"])  # el personal no sale
    caps = {c["key"]: c for c in yo["capabilities"]}
    assert set(caps) == {"view_workload", "view_audit", "create_workspaces", "invite_people"}
    assert caps["view_workload"]["grants"] == [
        {"workspace_id": dev["id"], "workspace_name": "Desarrollo", "via": "group", "group_name": "Equipo"}
    ]
    assert caps["view_audit"]["grants"][0]["via"] == "user"
    assert caps["view_audit"]["grants"][0]["workspace_id"] is None
