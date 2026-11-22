"""Plan de roles, R1: el administrador gestiona, pero no lee lo ajeno.

- No ve las páginas privadas de nadie ni los espacios personales ajenos.
- Gestiona los miembros de un espacio en el que no está (si no es personal),
  pero no entra en él: ni lo lista como suyo ni ve sus páginas.
- Lo que llega por fuera sin permisos (un `.md` puesto a mano y reindexado)
  pasa a ser del primer administrador.
- Subir una subpágina a la raíz conserva quién la veía.
"""

from __future__ import annotations

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
    for nombre in ("ana", "beto"):
        alta = _ok(client.post("/auth/users", json={
            "username": nombre, "display_name": nombre.title(), "password": f"contrasena-{nombre}",
        }), 201)
        ids[nombre] = alta["id"]

    @contextmanager
    def como(nombre):
        with TestClient(app) as c:
            _ok(c.post("/auth/login", json={"username": nombre, "password": f"contrasena-{nombre}"}))
            yield c

    with como("ana") as ana, como("beto") as beto:
        yield {"ana": ana, "beto": beto, "ids": ids}


def test_el_admin_no_ve_lo_privado_ni_lo_personal(client, gente):
    ana = gente["ana"]
    nota = _ok(ana.post("/pages", json={"title": "Diario privado", "body_markdown": "algo secreto"}))
    assert client.get(f"/pages/{nota['id']}").status_code == 404
    assert client.get(f"/pages/{nota['id']}/export.md").status_code == 404
    assert client.patch(f"/pages/{nota['id']}", json={"body_markdown": "x"}).status_code == 404
    assert client.get(f"/acl/page/{nota['id']}").status_code == 404
    arbol = client.get("/pages/tree").json()
    assert nota["id"] not in {n["id"] for n in arbol}
    assert not any(n.get("section") == "others" for n in arbol)
    assert client.get("/search", params={"q": "secreto"}).json() == []

    # Su espacio personal no le sale ni como suyo ni como gestionable.
    personal = next(w for w in _ok(ana.post("/workspaces/personal")) if w["personal_of"] == gente["ids"]["ana"])
    assert personal["id"] not in {w["id"] for w in client.get("/workspaces").json()}
    assert personal["id"] not in {w["id"] for w in client.get("/workspaces", params={"manage": 1}).json()}
    # Y no puede meterse en él.
    r = client.put(
        f"/workspaces/{personal['id']}/members/user/{gente['ids']['admin']}", json={"role": "admin"}
    )
    assert r.status_code == 404


def test_gestiona_un_espacio_ajeno_sin_entrar(client, gente):
    ana, beto, ids = gente["ana"], gente["beto"], gente["ids"]
    dev = _ok(client.post("/workspaces", json={"name": "Desarrollo"}), 201)
    _ok(client.put(f"/workspaces/{dev['id']}/members/user/{ids['ana']}", json={"role": "admin"}))
    # El administrador se sale: ya no es miembro.
    _ok(client.delete(f"/workspaces/{dev['id']}/members/user/{ids['admin']}"))

    compartida = _ok(
        ana.post("/pages", json={"title": "Diseño", "body_markdown": "del equipo"}, headers={"X-Workspace": dev["id"]}),
    )
    _ok(ana.patch(f"/pages/{compartida['id']}", json={"section": "shared"}, headers={"X-Workspace": dev["id"]}))

    # No es suyo: no sale en su barra ni puede abrirlo como espacio activo.
    assert dev["id"] not in {w["id"] for w in client.get("/workspaces").json()}
    assert client.get("/pages", headers={"X-Workspace": dev["id"]}).status_code == 404
    # Pero sí entre los que gestiona, por fuera.
    gestionable = next(w for w in client.get("/workspaces", params={"manage": 1}).json() if w["id"] == dev["id"])
    assert gestionable["member"] is False and gestionable["role"] is None
    assert gestionable["pages"] >= 1

    # Gestiona: ve los miembros y mete a Beto.
    miembros = _ok(client.get(f"/workspaces/{dev['id']}/members"))
    assert {m["subject_id"] for m in miembros} == {ids["ana"]}
    _ok(client.put(f"/workspaces/{dev['id']}/members/user/{ids['beto']}", json={"role": "editor"}))
    assert beto.get(f"/pages/{compartida['id']}").status_code == 200
    # Y cambia sus ajustes (nombre).
    _ok(client.patch(f"/workspaces/{dev['id']}", json={"name": "Desarrollo web"}))

    # No lee lo que hay dentro.
    assert client.get(f"/pages/{compartida['id']}").status_code == 404

    # Si se añade a sí mismo, queda en la auditoría y ya lo ve como cualquiera.
    _ok(client.put(f"/workspaces/{dev['id']}/members/user/{ids['admin']}", json={"role": "viewer"}))
    assert client.get(f"/pages/{compartida['id']}").status_code == 200
    eventos = client.get("/audit", params={"limit": 20}).json()
    lista = eventos["items"] if isinstance(eventos, dict) else eventos
    assert any(
        e.get("action") == "workspace.member_set" and e.get("target_id") == ids["admin"] for e in lista
    )


def test_un_usuario_normal_no_gestiona_espacios_ajenos(client, gente):
    dev = _ok(client.post("/workspaces", json={"name": "Ventas"}), 201)
    beto = gente["beto"]
    assert dev["id"] not in {w["id"] for w in beto.get("/workspaces", params={"manage": 1}).json()}
    r = beto.put(f"/workspaces/{dev['id']}/members/user/{gente['ids']['beto']}", json={"role": "admin"})
    assert r.status_code == 404


def test_lo_puesto_a_mano_en_el_disco_es_del_primer_administrador(client, gente, vault_path):
    carpeta = vault_path / "Notas"
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / "Suelta a mano.md").write_text(
        "---\nid: 5b1d2c3e-0000-4000-8000-0000000000aa\ntitle: Suelta a mano\n---\nhola\n",
        encoding="utf-8",
    )
    _ok(client.post("/reindex"))
    pid = "5b1d2c3e-0000-4000-8000-0000000000aa"
    assert _ok(client.get(f"/pages/{pid}"))["title"] == "Suelta a mano"
    entradas = _ok(client.get(f"/acl/page/{pid}"))
    assert {"subject_type": "user", "subject_id": gente["ids"]["admin"], "level": "owner"} in [
        {k: e[k] for k in ("subject_type", "subject_id", "level")} for e in entradas["entries"]
    ]
    assert gente["ana"].get(f"/pages/{pid}").status_code == 404


def test_subir_a_la_raiz_conserva_quien_la_ve(client, gente):
    ana, beto = gente["ana"], gente["beto"]
    madre = _ok(ana.post("/pages", json={"title": "Proyecto"}))
    _ok(ana.patch(f"/pages/{madre['id']}", json={"section": "shared"}))
    hija = _ok(ana.post("/pages", json={"title": "Tarea suelta", "parent_id": madre["id"]}))
    assert beto.get(f"/pages/{hija['id']}").status_code == 200

    movida = _ok(ana.patch(f"/pages/{hija['id']}", json={"parent_id": None}))
    assert movida["parent_id"] is None
    # La siguen viendo los mismos: Ana (dueña) y Beto (por el espacio).
    assert ana.get(f"/pages/{hija['id']}").status_code == 200
    assert beto.get(f"/pages/{hija['id']}").status_code == 200
