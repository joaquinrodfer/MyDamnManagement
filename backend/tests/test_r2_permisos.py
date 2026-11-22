"""Plan de roles, R2: permisos que se conceden (`app.grants`).

- Solo el administrador concede y retira; queda en la auditoría.
- Un administrador nuevo no tiene ninguno: auditoría, copias, festivos comunes y
  jornadas de otros le dan 403 hasta que se los den.
- Se conceden a personas o a grupos, en todos los espacios o en uno.
- «Gestionar personas» en un espacio: solo las personas de ese espacio.
- La migración v23 da a los administradores que había todos los permisos.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest


def _ok(r, code=200):
    assert r.status_code == code, f"{r.status_code} {r.text}"
    return r.json() if r.content and code != 204 else None


@pytest.fixture()
def gente(client):
    from fastapi.testclient import TestClient

    from app.main import app

    ids = {"admin": _ok(client.get("/auth/me"))["id"]}
    for nombre, admin in (("ana", False), ("beto", False), ("nuevo", True)):
        alta = _ok(client.post("/auth/users", json={
            "username": nombre, "display_name": nombre.title(), "password": f"contrasena-{nombre}",
            "is_admin": admin,
        }), 201)
        ids[nombre] = alta["id"]

    @contextmanager
    def como(nombre):
        with TestClient(app) as c:
            _ok(c.post("/auth/login", json={"username": nombre, "password": f"contrasena-{nombre}"}))
            yield c

    with como("ana") as ana, como("beto") as beto, como("nuevo") as nuevo:
        yield {"ana": ana, "beto": beto, "nuevo": nuevo, "ids": ids}


def test_el_primer_administrador_los_tiene_todos(client):
    caps = _ok(client.get("/auth/me"))["capabilities"]
    assert caps == {k: "all" for k in ("manage_people", "view_workload", "manage_holidays", "view_audit", "backups")}


def test_un_administrador_nuevo_no_tiene_ninguno(client, gente):
    nuevo, ids = gente["nuevo"], gente["ids"]
    assert _ok(nuevo.get("/auth/me"))["capabilities"] == {}
    assert nuevo.get("/audit").status_code == 403
    assert nuevo.get("/backups").status_code == 403
    assert nuevo.post("/export").status_code == 403
    assert nuevo.get("/trash").status_code == 403
    assert nuevo.get("/workload").status_code == 403
    assert nuevo.post("/holidays", json={"day": "2026-12-25", "name": "Navidad"}).status_code == 403
    assert nuevo.put(f"/worktime/users/{ids['ana']}/schedule", json={"hours": [7] * 5 + [0, 0]}).status_code == 403
    # Pero sigue gestionando usuarios, grupos y espacios, y concede permisos.
    assert nuevo.get("/auth/users").status_code == 200
    assert nuevo.get("/grants").status_code == 200


def test_conceder_y_retirar_queda_en_la_auditoria(client, gente):
    ana, ids = gente["ana"], gente["ids"]
    assert ana.get("/grants").status_code == 403
    assert ana.post("/grants", json={
        "capability": "view_audit", "subject_type": "user", "subject_id": ids["ana"],
    }).status_code == 403

    dado = _ok(client.post("/grants", json={
        "capability": "view_audit", "subject_type": "user", "subject_id": ids["ana"],
    }), 201)
    assert dado["subject_name"] == "Ana" and dado["workspace_id"] is None
    # Repetirlo no duplica.
    otra = _ok(client.post("/grants", json={
        "capability": "view_audit", "subject_type": "user", "subject_id": ids["ana"],
    }), 201)
    assert otra["id"] == dado["id"]
    assert _ok(ana.get("/auth/me"))["capabilities"] == {"view_audit": "all"}
    assert ana.get("/audit").status_code == 200

    _ok(client.delete(f"/grants/{dado['id']}"), 204)
    assert ana.get("/audit").status_code == 403
    acciones = [e["action"] for e in _ok(client.get("/audit", params={"action": "grant."}))["items"]]
    assert acciones.count("grant.added") >= 1 and "grant.removed" in acciones


def test_los_globales_no_se_limitan_a_un_espacio(client, gente):
    ws = _ok(client.post("/workspaces", json={"name": "Ventas"}), 201)
    r = client.post("/grants", json={
        "capability": "backups", "subject_type": "user", "subject_id": gente["ids"]["ana"],
        "workspace_id": ws["id"],
    })
    assert r.status_code == 400
    r = client.post("/grants", json={
        "capability": "nada", "subject_type": "user", "subject_id": gente["ids"]["ana"],
    })
    assert r.status_code == 400
    r = client.post("/grants", json={
        "capability": "view_audit", "subject_type": "user", "subject_id": "no-existe",
    })
    assert r.status_code == 404


def test_por_grupo_y_por_espacio(client, gente):
    ana, beto, ids = gente["ana"], gente["beto"], gente["ids"]
    dev = _ok(client.post("/workspaces", json={"name": "Desarrollo"}), 201)
    _ok(client.put(f"/workspaces/{dev['id']}/members/user/{ids['beto']}", json={"role": "editor"}))
    jefes = _ok(client.post("/groups", json={"name": "Jefes"}), 201)
    _ok(client.put(f"/groups/{jefes['id']}/members", json={"user_ids": [ids["ana"]]}))
    _ok(client.post("/grants", json={
        "capability": "manage_people", "subject_type": "group", "subject_id": jefes["id"],
        "workspace_id": dev["id"],
    }), 201)
    assert _ok(ana.get("/auth/me"))["capabilities"] == {"manage_people": [dev["id"]]}

    # Beto está en Desarrollo: Ana gestiona su jornada y sus ausencias.
    _ok(ana.put(f"/worktime/users/{ids['beto']}/schedule", json={"hours": [7, 7, 7, 7, 7, 0, 0]}))
    _ok(ana.post("/absences", json={"user_id": ids["beto"], "start": "2026-12-01", "kind": "formacion"}), 201)
    assert _ok(ana.get(f"/worktime/users/{ids['beto']}"))["weekly"] == 35
    # El nuevo administrador no está en Desarrollo: no es asunto de Ana.
    assert ana.get(f"/worktime/users/{ids['nuevo']}").status_code == 403
    # Beto, sin permiso, no gestiona a Ana.
    assert beto.get(f"/worktime/users/{ids['ana']}").status_code == 403

    # Sacar a Ana del grupo le quita el permiso.
    _ok(client.put(f"/groups/{jefes['id']}/members", json={"user_ids": []}))
    assert ana.get(f"/worktime/users/{ids['beto']}").status_code == 403
    # Borrar el grupo se lleva sus permisos.
    _ok(client.delete(f"/groups/{jefes['id']}"), 204)
    assert all(g["subject_id"] != jefes["id"] for g in _ok(client.get("/grants"))["grants"])


def test_festivos_de_un_espacio_con_el_permiso(client, gente):
    ana, ids = gente["ana"], gente["ids"]
    dev = _ok(client.post("/workspaces", json={"name": "Soporte"}), 201)
    cuerpo = {"workspace_id": dev["id"], "day": "2026-12-26", "name": "San Esteban"}
    assert ana.post("/holidays", json=cuerpo).status_code == 403
    _ok(client.post("/grants", json={
        "capability": "manage_holidays", "subject_type": "user", "subject_id": ids["ana"],
        "workspace_id": dev["id"],
    }), 201)
    _ok(ana.post("/holidays", json=cuerpo), 201)
    # Los comunes siguen pidiéndolo en todos los espacios.
    assert ana.post("/holidays", json={"day": "2026-12-25", "name": "Navidad"}).status_code == 403


def test_la_migracion_da_a_los_administradores_lo_de_antes(tmp_path, monkeypatch):
    from app import grants, index
    from app.config import settings

    monkeypatch.setattr(settings, "backup_dir", "")
    ruta = tmp_path / "v22.sqlite"
    index.ensure_index(str(ruta))
    c = sqlite3.connect(ruta)
    c.executescript("DROP TABLE grants; PRAGMA user_version = 22;")
    for uid, admin, carga in (("u1", 1, 0), ("u2", 0, 1), ("u3", 0, 0)):
        c.execute(
            "INSERT INTO users (id, username, display_name, password_hash, salt, created_at, is_admin,"
            " can_view_global_workload) VALUES (?, ?, ?, 'h', 's', '2026-01-01', ?, ?)",
            (uid, uid, uid, admin, carga),
        )
    c.commit()
    c.close()
    index._ready.clear()

    assert index.ensure_index(str(ruta)).version == index.SCHEMA_VERSION
    c = sqlite3.connect(ruta)
    c.row_factory = sqlite3.Row
    assert grants.summary(c, "u1") == {k: "all" for k in grants.KEYS}
    assert grants.summary(c, "u2") == {"view_workload": "all"}
    assert grants.summary(c, "u3") == {}
    c.close()
