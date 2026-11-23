"""Pentest del 2026-11-23: lo que se encontró y se arregló, y lo que ya estaba
bien y no debe romperse (accesos cruzados entre usuarios, CSRF, cabeceras)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _ok(r, code=None):
    assert r.status_code == code if code else r.status_code in (200, 201), r.text
    return r.json() if r.content else None


def _login(client, username):
    u = _ok(client.post("/auth/users", json={"username": username, "display_name": username.title(),
                                             "password": f"contrasena-{username}"}), 201)
    from app.main import app

    c = TestClient(app)
    c.__enter__()
    _ok(c.post("/auth/login", json={"username": username, "password": f"contrasena-{username}"}))
    return c, u


@pytest.fixture
def mundo(client):
    ana, ana_u = _login(client, "ana")
    eve, eve_u = _login(client, "eve")
    wa = _ok(client.post("/workspaces", json={"name": "Secreto"}))
    _ok(client.put(f"/workspaces/{wa['id']}/members/user/{ana_u['id']}", json={"role": "admin"}))
    we = _ok(client.post("/workspaces", json={"name": "De Eve"}))
    _ok(client.put(f"/workspaces/{we['id']}/members/user/{eve_u['id']}", json={"role": "admin"}))
    HA, HE = {"X-Workspace": wa["id"]}, {"X-Workspace": we["id"]}
    nota = _ok(ana.post("/pages", json={"title": "NOTA-SECRETA", "body_markdown": "cuerpo-secreto", "section": "shared"}, headers=HA))
    da = _ok(ana.post("/databases/from-template", json={"template": "tareas", "title": "BASE-SECRETA", "section": "shared"}, headers=HA))
    ra = _ok(ana.post(f"/databases/{da['id']}/rows", json={"title": "FILA-SECRETA"}, headers=HA))
    va = _ok(ana.post(f"/databases/{da['id']}/views", json={"name": "VISTA-SECRETA", "type": "table"}, headers=HA))
    de = _ok(eve.post("/databases/from-template", json={"template": "tareas", "title": "BASE-EVE", "section": "shared"}, headers=HE))
    re_ = _ok(eve.post(f"/databases/{de['id']}/rows", json={"title": "FILA-EVE"}, headers=HE))
    conv = next(c for c in _ok(ana.get("/chat/conversations")) if c["kind"] == "workspace" and c["title"] == "Secreto")
    msg = _ok(ana.post(f"/chat/conversations/{conv['id']}/messages", json={"body": "MENSAJE-SECRETO"}))
    return dict(ana=ana, eve=eve, ana_u=ana_u, eve_u=eve_u, wa=wa, HE=HE, nota=nota, da=da, ra=ra, va=va,
                de=de, re=re_, conv=conv, msg=msg)


SECRETOS = ("NOTA-SECRETA", "cuerpo-secreto", "BASE-SECRETA", "FILA-SECRETA", "VISTA-SECRETA", "MENSAJE-SECRETO")


def test_nadie_lee_ni_toca_lo_ajeno(mundo):
    m = mundo
    eve, HE = m["eve"], m["HE"]
    DA, RA, VA, DE = m["da"]["id"], m["ra"]["id"], m["va"]["id"], m["de"]["id"]
    N, C, M, WA, ANA = m["nota"]["id"], m["conv"]["id"], m["msg"]["id"], m["wa"]["id"], m["ana_u"]["id"]
    casos = [
        ("GET", f"/pages/{N}", None), ("PATCH", f"/pages/{N}", {"title": "x"}), ("DELETE", f"/pages/{N}", None),
        ("GET", f"/databases/{DA}/rows", None),
        ("GET", f"/databases/{DE}/rows/{RA}", None), ("PATCH", f"/databases/{DE}/rows/{RA}", {"title": "pwn"}),
        ("DELETE", f"/databases/{DE}/rows/{RA}", None),
        ("PATCH", f"/databases/{DE}/views/{VA}", {"name": "pwn"}), ("DELETE", f"/databases/{DE}/views/{VA}", None),
        ("GET", f"/chat/conversations/{C}/messages", None), ("POST", f"/chat/conversations/{C}/messages", {"body": "x"}),
        ("POST", f"/chat/conversations/{C}/members", {"user_ids": [m["eve_u"]["id"]]}),
        ("PATCH", f"/chat/messages/{M}", {"body": "pwn"}), ("DELETE", f"/chat/messages/{M}", None),
        ("GET", f"/chat/messages/{M}/edits", None),
        ("PUT", f"/notifications/subscriptions/{N}", {"mode": "all"}), ("PUT", f"/pins/{N}", None),
        ("PATCH", f"/workspaces/{WA}", {"name": "pwn"}),
        ("PUT", f"/workspaces/{WA}/members/user/{m['eve_u']['id']}", {"role": "admin"}),
        ("GET", f"/workspaces/{WA}/webhook", None), ("PUT", f"/worktime/users/{ANA}/schedule", {"hours": [1] * 7}),
        ("PATCH", f"/auth/users/{ANA}", {"display_name": "pwn"}), ("POST", f"/auth/users/{ANA}/reset-link", None),
        ("PUT", f"/acl/note/{N}", {"inherit": False, "entries": [{"subject_type": "user", "subject_id": m["eve_u"]["id"], "level": "owner"}]}),
        ("POST", "/pages", {"title": "hija", "parent_id": N}),
    ]
    for metodo, url, cuerpo in casos:
        r = eve.request(metodo, url, json=cuerpo, headers=HE)
        assert r.status_code >= 400, (metodo, url, r.status_code)
        assert not [s for s in SECRETOS if s in r.text], (metodo, url)
    for url in ("/pages", "/databases", "/search?q=SECRETA", "/graph", "/mytasks", "/dashboard", "/workload/grid",
                "/notifications", "/chat/conversations", "/chat/search?q=MENSAJE", "/presence"):
        r = eve.get(url, headers=HE)
        assert not [s for s in SECRETOS if s in r.text], url


def test_una_relacion_no_lee_filas_ajenas(mundo):
    """Hallazgo: una relación hacia una base ajena dejaba leer el título de sus filas."""
    m = mundo
    eve, HE, DE, RE = m["eve"], m["HE"], m["de"]["id"], m["re"]["id"]
    esquema = _ok(eve.get(f"/databases/{DE}", headers=HE))["schema_def"]
    r = eve.patch(f"/databases/{DE}", json={"schema_def": esquema + [{"name": "Rel", "type": "relation", "target_database": m["da"]["id"]}]}, headers=HE)
    assert r.status_code == 400 and "base destino" in r.json()["detail"]
    # Aunque la relación ya existiera (de cuando se veía), la celda no resuelve lo que no se ve.
    otra = _ok(eve.post("/databases/from-template", json={"template": "tareas", "title": "Otra", "section": "shared"}, headers=HE))
    esquema = _ok(eve.patch(f"/databases/{DE}", json={"schema_def": esquema + [{"name": "Rel", "type": "relation", "target_database": otra["id"]}]}, headers=HE))["schema_def"]
    clave = next(p["key"] for p in esquema if p["name"] == "Rel")
    r = eve.patch(f"/databases/{DE}/rows/{RE}", json={"properties": {clave: m["ra"]["id"]}}, headers=HE)
    assert r.status_code >= 400 and "FILA-SECRETA" not in r.text


def test_no_se_puede_quitar_la_propiedad(mundo):
    """Lo que dejó «qqq» sin dueño en producción ya no se puede hacer."""
    ana, N, ANA = mundo["ana"], mundo["nota"]["id"], mundo["ana_u"]["id"]
    for entradas in ([], [{"subject_type": "user", "subject_id": ANA, "level": "view"}]):
        r = ana.put(f"/acl/note/{N}", json={"inherit": False, "entries": entradas})
        assert r.status_code in (400, 422)


def test_escrituras_desde_otra_web(client):
    assert client.post("/pages", json={"title": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/pages", json={"title": "x"}, headers={"Origin": "null"}).status_code == 403
    assert client.delete("/auth/tokens/1", headers={"Origin": "http://otra.app:8096"}).status_code == 403
    # La propia web, o sin Origin (curl, atajos), sí.
    _ok(client.post("/pages", json={"title": "x"}, headers={"Origin": "http://testserver"}))
    _ok(client.post("/pages", json={"title": "y"}))
    # Solo JSON: un formulario de otra web no crea nada.
    assert client.post("/pages", content="title=x", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 422


def test_cabeceras_de_seguridad(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "script-src 'self';" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert "strict-transport-security" not in r.headers
    r = client.get("/health", headers={"x-forwarded-proto": "https"})
    assert r.headers["strict-transport-security"].startswith("max-age=")
    with TestClient(__import__("app.main", fromlist=["app"]).app) as anon:
        r = anon.get("/login")
        assert "script-src 'self' 'sha256-" in r.headers["content-security-policy"]


def test_limite_de_intentos_de_login(client):
    for i in range(12):
        r = client.post("/auth/login", json={"username": "prueba", "password": f"mala-{i}"})
    assert r.status_code == 429
