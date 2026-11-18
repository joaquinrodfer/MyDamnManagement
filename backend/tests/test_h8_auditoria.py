"""H8: registro de auditoría. Quién hizo qué, solo para el superadministrador."""

import pytest

from app import audit
from test_h1_espacios import _sesion, cierra  # noqa: F401 - fixtures


def _acciones(client, **q):
    return [(i["action"], i["target_name"]) for i in client.get("/audit", params=q).json()["items"]]


def test_solo_el_superadministrador_ve_la_auditoria(client, cierra):
    otro, _ = _sesion(client, "normal")
    cierra.append(otro)
    assert otro.get("/audit").status_code == 403
    assert client.get("/audit").status_code == 200


def test_se_anotan_las_acciones_criticas(client, cierra, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "export_dir", str(tmp_path / "exp"))
    client.post("/auth/users", json={"username": "ana", "display_name": "Ana", "password": "contrasena-ana"})
    ws = client.post("/workspaces", json={"name": "Dirección"}).json()
    client.patch(f"/workspaces/{ws['id']}", json={"name": "Dirección general"})
    grupo = client.post("/groups", json={"name": "Jefes"}).json()
    client.put(f"/workspaces/{ws['id']}/members/group/{grupo['id']}", json={"role": "editor"})
    pagina = client.post("/pages", json={"title": "Plan", "section": "shared"}, headers={"X-Workspace": ws["id"]}).json()
    me = client.get("/auth/me").json()
    client.put(f"/acl/page/{pagina['id']}", json={"inherit": False, "entries": [
        {"subject_type": "user", "subject_id": me["id"], "level": "owner"},
        {"subject_type": "group", "subject_id": grupo["id"], "level": "view"}]})
    client.get(f"/pages/{pagina['id']}/export.md")
    client.delete(f"/pages/{pagina['id']}")
    base = client.post("/databases", json={"title": "Datos", "schema_def": []}).json()
    client.put(f"/databases/{base['id']}/kind", json={"type": "tasks"})
    client.delete(f"/databases/{base['id']}")
    client.post("/export", json={"format": "folder"})
    acciones = {a for a, _ in _acciones(client, limit=200)}
    assert {
        "user.created", "workspace.created", "workspace.updated", "workspace.member_set", "group.created",
        "permission.changed", "page.exported", "page.deleted", "database.kind_changed",
        "database.deleted", "vault.exported",
    } <= acciones, acciones
    # Cada fila dice quién, cuándo, sobre qué y, si hace falta, un poco de contexto.
    permiso = next(i for i in client.get("/audit?action=permission").json()["items"])
    assert permiso["actor_name"] == me["display_name"] and permiso["target_name"] == "Plan"
    assert permiso["workspace_name"] == "Dirección general" and any("group:" in e for e in permiso["metadata"]["entries"])
    assert permiso["ts"]


def test_filtros_y_paginacion(client):
    for n in ("uno", "dos", "tres"):
        client.post("/workspaces", json={"name": f"Espacio {n}"})
    todo = client.get("/audit?limit=2").json()
    assert todo["total"] >= 3 and len(todo["items"]) == 2 and "workspace.created" in todo["actions"]
    segunda = client.get("/audit?limit=2&offset=2").json()["items"]
    assert {i["id"] for i in segunda}.isdisjoint({i["id"] for i in todo["items"]})
    solo = client.get("/audit?action=workspace.created").json()
    assert solo["total"] == 3 and all(i["action"] == "workspace.created" for i in solo["items"])
    # Por quien lo hizo y por rango de fechas.
    yo = client.get("/auth/me").json()["id"]
    assert client.get(f"/audit?actor={yo}&action=workspace").json()["total"] >= 3
    assert client.get("/audit?actor=nadie").json()["total"] == 0
    assert client.get("/audit?since=2999-01-01").json()["total"] == 0
    assert client.get("/audit?until=2000-01-01").json()["total"] == 0
    assert client.get("/audit?limit=999").status_code == 422


def test_las_copias_tambien_se_anotan(client, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "copias"))
    monkeypatch.setattr(settings, "export_dir", str(tmp_path / "exp"))
    client.post("/backups/download")
    client.put("/backups/settings", json={"frequency": "daily", "keep": 3})
    client.post("/backups/run")
    assert {"backup.downloaded", "backup.settings_changed", "backup.created"} <= {a for a, _ in _acciones(client, limit=200)}


def test_auditar_nunca_rompe_la_accion(client, monkeypatch):
    """Si el registro falla, la acción sigue (y se anota en el log del servidor)."""
    class Roto:
        @staticmethod
        def now(*_a, **_k):
            raise RuntimeError("roto")

    monkeypatch.setattr(audit, "datetime", Roto)
    assert client.post("/workspaces", json={"name": "Sigue adelante"}).status_code == 201
    assert any(w["name"] == "Sigue adelante" for w in client.get("/workspaces").json())
