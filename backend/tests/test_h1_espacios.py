"""H1: espacios de trabajo, grupos y roles.

- el espacio de siempre se llama General y todos están en él;
- crear, renombrar y borrar espacios (solo el superadministrador crea y borra,
  un administrador del espacio lo renombra y gestiona sus miembros);
- el aislamiento: quien no está en un espacio no ve nada de él, y la cabecera
  `X-Workspace` limita los listados al espacio activo;
- los roles: un `viewer` no pasa de lectura por el espacio ni crea en la raíz;
- los grupos dan acceso a un espacio o a una página;
- `can_view_global_workload` es un permiso aparte del de administrador.
"""

import pytest
from fastapi.testclient import TestClient

from app import workspaces
from app.index import DEFAULT_WORKSPACE_ID


def _sesion(client, username, **extra):
    r = client.post("/auth/users", json={
        "username": username, "display_name": username.title(), "password": f"contrasena-{username}", **extra,
    })
    assert r.status_code == 201, r.text
    from app.main import app

    c = TestClient(app)
    c.__enter__()
    assert c.post("/auth/login", json={"username": username, "password": f"contrasena-{username}"}).status_code == 200
    return c, r.json()


@pytest.fixture
def cierra():
    abiertos = []
    yield abiertos
    for c in abiertos:
        c.__exit__(None, None, None)


def _compartir_con_espacio(client, pagina, workspace_id, nivel="edit"):
    me = client.get("/auth/me").json()
    r = client.put(f"/acl/page/{pagina['id']}", json={"inherit": False, "entries": [
        {"subject_type": "user", "subject_id": me["id"], "level": "owner"},
        {"subject_type": "workspace", "subject_id": workspace_id, "level": nivel},
    ]})
    assert r.status_code == 200, r.text


def test_el_espacio_de_siempre_es_general_y_todos_estan(client):
    lista = client.get("/workspaces").json()
    assert [w["name"] for w in lista] == ["General"]
    general = lista[0]
    assert general["id"] == DEFAULT_WORKSPACE_ID and general["slug"] == "general" and general["is_default"]
    miembros = client.get(f"/workspaces/{general['id']}/members").json()
    assert {m["name"] for m in miembros} >= {client.get("/auth/me").json()["display_name"]}


def test_un_usuario_nuevo_entra_en_general_como_editor(client, cierra):
    c, usuario = _sesion(client, "ana")
    cierra.append(c)
    roles = {m["subject_id"]: m["role"] for m in client.get(f"/workspaces/{DEFAULT_WORKSPACE_ID}/members").json()}
    assert roles[usuario["id"]] == "editor"
    assert [w["name"] for w in c.get("/workspaces").json()] == ["General"]


def test_crear_renombrar_y_borrar_espacios(client, cierra):
    nuevo = client.post("/workspaces", json={"name": "Investigación", "icon": "🔬"})
    assert nuevo.status_code == 201, nuevo.text
    ws = nuevo.json()
    assert ws["slug"] == "investigacion" and ws["role"] == "admin"
    assert client.post("/workspaces", json={"name": "investigación"}).status_code == 409
    r = client.patch(f"/workspaces/{ws['id']}", json={"name": "I+D"})
    assert r.json()["name"] == "I+D" and r.json()["slug"] == "i-d"
    # Solo el superadministrador crea y borra.
    c, _ = _sesion(client, "beto")
    cierra.append(c)
    assert c.post("/workspaces", json={"name": "Otro"}).status_code == 403
    assert c.delete(f"/workspaces/{ws['id']}").status_code == 403
    # Con páginas dentro no se borra; vacío sí; y nunca el único.
    client.post("/pages", json={"title": "Dentro"}, headers={"X-Workspace": ws["id"]})
    assert client.delete(f"/workspaces/{ws['id']}").status_code == 409
    vacio = client.post("/workspaces", json={"name": "Vacío"}).json()
    assert client.delete(f"/workspaces/{vacio['id']}").status_code == 204
    assert client.delete(f"/workspaces/{ws['id']}").status_code == 409
    assert client.get("/workspaces").json()[0]["pages"] >= 0


def test_no_se_borra_el_unico_espacio(client):
    assert [w["name"] for w in client.get("/workspaces").json()] == ["General"]
    assert client.delete(f"/workspaces/{DEFAULT_WORKSPACE_ID}").status_code == 409


def test_aislamiento_entre_espacios(client, cierra):
    a = client.post("/workspaces", json={"name": "Desarrollo"}).json()
    b = client.post("/workspaces", json={"name": "Dirección"}).json()
    pa = client.post("/pages", json={"title": "Solo de Desarrollo"}, headers={"X-Workspace": a["id"]}).json()
    pb = client.post("/pages", json={"title": "Solo de Dirección"}, headers={"X-Workspace": b["id"]}).json()
    # La página queda en el espacio donde se creó.
    assert client.get(f"/pages/{pa['id']}").json()["id"] == pa["id"]
    titulos = lambda ws: {n["title"] for n in _aplanar(client.get("/pages/tree", headers={"X-Workspace": ws}).json())}  # noqa: E731
    assert "Solo de Desarrollo" in titulos(a["id"]) and "Solo de Dirección" not in titulos(a["id"])
    assert "Solo de Dirección" in titulos(b["id"]) and "Solo de Desarrollo" not in titulos(b["id"])
    # Un espacio que no existe es un 404.
    assert client.get("/pages/tree", headers={"X-Workspace": "no-existe"}).status_code == 404

    # Beto está solo en Desarrollo: ni ve ni encuentra lo de Dirección.
    c, beto = _sesion(client, "beto", workspaces=[{"id": a["id"], "role": "editor"}])
    cierra.append(c)
    assert [w["name"] for w in c.get("/workspaces").json()] == ["Desarrollo"]
    _compartir_con_espacio(client, pa, a["id"])
    _compartir_con_espacio(client, pb, b["id"])
    assert c.get(f"/pages/{pa['id']}").status_code == 200
    assert c.get(f"/pages/{pb['id']}").status_code == 404
    assert c.get("/pages/tree", headers={"X-Workspace": b["id"]}).status_code == 404
    buscado = c.get("/search", params={"q": "Dirección"}).json()
    assert pb["id"] not in str(buscado)


def _aplanar(arbol):
    pila = list(arbol if isinstance(arbol, list) else arbol.get("nodes", arbol.get("tree", [])))
    while pila:
        n = pila.pop()
        yield n
        pila.extend(n.get("children") or [])


def test_un_viewer_solo_lee_lo_compartido_con_el_espacio(client, cierra):
    ws = client.post("/workspaces", json={"name": "Lectura"}).json()
    pagina = client.post("/pages", json={"title": "Común"}, headers={"X-Workspace": ws["id"]}).json()
    _compartir_con_espacio(client, pagina, ws["id"], "edit")
    editor, _ = _sesion(client, "edi", workspaces=[{"id": ws["id"], "role": "editor"}])
    lector, _ = _sesion(client, "lec", workspaces=[{"id": ws["id"], "role": "viewer"}])
    cierra += [editor, lector]
    assert editor.patch(f"/pages/{pagina['id']}", json={"description": "x"}).status_code == 200
    assert lector.get(f"/pages/{pagina['id']}").status_code == 200
    assert lector.patch(f"/pages/{pagina['id']}", json={"description": "y"}).status_code == 403
    # Tampoco crea nada en la raíz del espacio; el editor sí.
    assert lector.post("/pages", json={"title": "No"}, headers={"X-Workspace": ws["id"]}).status_code == 403
    assert editor.post("/pages", json={"title": "Sí"}, headers={"X-Workspace": ws["id"]}).status_code == 200


def test_un_grupo_da_acceso_a_un_espacio_y_a_una_pagina(client, cierra):
    ws = client.post("/workspaces", json={"name": "Diseño"}).json()
    pagina = client.post("/pages", json={"title": "Del grupo"}, headers={"X-Workspace": ws["id"]}).json()
    c, ana = _sesion(client, "ana", workspaces=[])
    cierra.append(c)
    assert c.get("/workspaces").json() == []
    grupo = client.post("/groups", json={"name": "Diseñadores"}).json()
    assert client.post("/groups", json={"name": "diseñadores"}).status_code == 409
    assert client.put(f"/groups/{grupo['id']}/members", json={"user_ids": [ana["id"]]}).json()["members"] == [ana["id"]]
    # Sin acceso: ni el espacio ni la página.
    assert c.get(f"/pages/{pagina['id']}").status_code == 404
    # El grupo entra al espacio como editor y la página se comparte con el espacio.
    assert client.put(f"/workspaces/{ws['id']}/members/group/{grupo['id']}", json={"role": "editor"}).status_code == 200
    _compartir_con_espacio(client, pagina, ws["id"])
    assert [w["name"] for w in c.get("/workspaces").json()] == ["Diseño"]
    assert c.get(f"/pages/{pagina['id']}").status_code == 200
    # Quitar a Ana del grupo le quita el acceso.
    client.put(f"/groups/{grupo['id']}/members", json={"user_ids": []})
    assert c.get(f"/pages/{pagina['id']}").status_code == 404
    # Una página compartida con el grupo directamente (sin ser del espacio).
    me = client.get("/auth/me").json()
    client.put(f"/groups/{grupo['id']}/members", json={"user_ids": [ana["id"]]})
    client.delete(f"/workspaces/{ws['id']}/members/group/{grupo['id']}")
    r = client.put(f"/acl/page/{pagina['id']}", json={"inherit": False, "entries": [
        {"subject_type": "user", "subject_id": me["id"], "level": "owner"},
        {"subject_type": "group", "subject_id": grupo["id"], "level": "view"},
    ]})
    assert r.status_code == 200, r.text
    assert c.get(f"/pages/{pagina['id']}").status_code == 200
    assert any(e["subject_type"] == "group" and e["name"] == "Diseñadores" for e in r.json()["entries"])
    # Borrar el grupo quita sus permisos.
    assert client.delete(f"/groups/{grupo['id']}").status_code == 204
    assert c.get(f"/pages/{pagina['id']}").status_code == 404


def test_solo_el_administrador_del_espacio_gestiona_sus_miembros(client, cierra):
    ws = client.post("/workspaces", json={"name": "Equipo"}).json()
    jefe, jefe_u = _sesion(client, "jefa", workspaces=[{"id": ws["id"], "role": "admin"}])
    raso, raso_u = _sesion(client, "raso", workspaces=[{"id": ws["id"], "role": "editor"}])
    cierra += [jefe, raso]
    assert raso.put(f"/workspaces/{ws['id']}/members/user/{raso_u['id']}", json={"role": "admin"}).status_code == 403
    assert raso.patch(f"/workspaces/{ws['id']}", json={"name": "Mío"}).status_code == 403
    assert jefe.patch(f"/workspaces/{ws['id']}", json={"name": "Equipo A"}).json()["name"] == "Equipo A"
    assert jefe.put(f"/workspaces/{ws['id']}/members/user/{raso_u['id']}", json={"role": "viewer"}).status_code == 200
    assert jefe.delete(f"/workspaces/{ws['id']}/members/user/{raso_u['id']}").status_code == 200
    assert raso.get("/workspaces").json() == []
    assert jefe.put(f"/workspaces/{ws['id']}/members/user/no-existe", json={"role": "editor"}).status_code == 404


def test_la_carga_global_es_un_permiso_aparte(client, cierra):
    c, u = _sesion(client, "pmgr", can_view_global_workload=True)
    cierra.append(c)
    assert u["can_view_global_workload"] is True and u["is_admin"] is False
    assert c.get("/auth/me").json()["can_view_global_workload"] is True
    otro, ou = _sesion(client, "otro")
    cierra.append(otro)
    assert ou["can_view_global_workload"] is False
    # Un administrador se lo da; un usuario normal no puede dárselo.
    assert client.patch(f"/auth/users/{ou['id']}", json={"can_view_global_workload": True}).json()["can_view_global_workload"] is True
    assert otro.patch(f"/auth/users/{ou['id']}", json={"can_view_global_workload": False}).status_code == 403


def test_un_reindexado_no_mueve_las_paginas_de_espacio(client):
    ws = client.post("/workspaces", json={"name": "Aparte"}).json()
    pagina = client.post("/pages", json={"title": "Se queda"}, headers={"X-Workspace": ws["id"]}).json()
    client.patch(f"/pages/{pagina['id']}", json={"body_markdown": "cambio"})
    assert client.post("/reindex?force=true").status_code == 200
    from app.config import settings
    import sqlite3

    c = sqlite3.connect(settings.index_path)
    assert c.execute("SELECT workspace_id FROM pages WHERE id = ?", (pagina["id"],)).fetchone()[0] == ws["id"]
    c.close()


def test_miembro_por_grupo_toma_el_mejor_rol():
    assert workspaces.best_role("viewer", "admin", None) == "admin"
    assert workspaces.best_role(None) is None


def test_migracion_14_conserva_permisos_y_mete_a_todos_en_general(tmp_path, monkeypatch):
    """Un índice v13 (un solo espacio «default», permisos de usuario y de
    espacio) pasa a la 14: General, todos dentro y los permisos intactos."""
    import sqlite3

    from app import index
    from app.config import settings

    monkeypatch.setattr(settings, "backup_dir", "")
    ruta = tmp_path / "v13.sqlite"
    index.ensure_index(str(ruta))
    c = sqlite3.connect(ruta)
    c.executescript(
        """
        DROP TABLE group_members; DROP TABLE groups; DROP TABLE workspace_members;
        ALTER TABLE workspaces DROP COLUMN slug;
        ALTER TABLE workspaces DROP COLUMN icon;
        ALTER TABLE workspaces DROP COLUMN settings;
        ALTER TABLE users DROP COLUMN can_view_global_workload;
        DROP INDEX acl_subject;
        ALTER TABLE acl RENAME TO acl_nueva;
        CREATE TABLE acl (
            id TEXT PRIMARY KEY,
            subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'workspace')),
            subject_id TEXT NOT NULL,
            resource_type TEXT NOT NULL CHECK (resource_type IN ('page', 'database')),
            resource_id TEXT NOT NULL,
            level TEXT NOT NULL CHECK (level IN ('view', 'edit', 'owner')),
            UNIQUE (resource_id, subject_type, subject_id));
        DROP TABLE acl_nueva;
        CREATE INDEX acl_subject ON acl(subject_type, subject_id);
        PRAGMA user_version = 13;
        """
    )
    c.execute("INSERT INTO workspaces (id, name, created_at) VALUES (?, 'default', '2026-01-01')", (index.DEFAULT_WORKSPACE_ID,))
    c.execute("INSERT INTO users (id, username, display_name, password_hash, salt, created_at, is_admin) VALUES ('u1','jefa','Jefa','h','s','2026-01-01',1)")
    c.execute("INSERT INTO users (id, username, display_name, password_hash, salt, created_at, is_admin) VALUES ('u2','ana','Ana','h','s','2026-01-01',0)")
    c.execute("INSERT INTO acl (id, subject_type, subject_id, resource_type, resource_id, level) VALUES ('a1','workspace',?, 'page','p1','edit')", (index.DEFAULT_WORKSPACE_ID,))
    c.commit()
    c.close()
    index._ready.clear()

    assert index.ensure_index(str(ruta)).version == index.SCHEMA_VERSION == 14
    c = sqlite3.connect(ruta)
    assert c.execute("SELECT name, slug FROM workspaces").fetchall() == [("General", "general")]
    assert dict(c.execute("SELECT subject_id, role FROM workspace_members")) == {"u1": "admin", "u2": "editor"}
    assert c.execute("SELECT subject_type, level FROM acl WHERE id = 'a1'").fetchone() == ("workspace", "edit")
    # La tabla rehecha admite grupos.
    c.execute("INSERT INTO acl (id, subject_type, subject_id, resource_type, resource_id, level) VALUES ('a2','group','g','page','p1','view')")
    assert c.execute("SELECT can_view_global_workload FROM users WHERE id = 'u2'").fetchone() == (0,)
    c.close()
