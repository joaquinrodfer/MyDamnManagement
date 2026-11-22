"""3.0, hito 3: permisos por nota, heredados por las hijas.

Tres personas: el administrador (`client`, el primer usuario de la suite), y
Ana y Beto, usuarios normales. Lo que se fija:

1. un espacio es privado: lo que crea Ana en la raíz no lo ve nadie más
   (404 en la página y fuera de todos los listados), salvo el administrador;
2. compartir con `view` deja leer y no escribir (403); con `edit`, escribir;
3. la herencia llega a 5 niveles de profundidad;
4. una hija que declara su propio permiso manda (corta la herencia), y
   revocar una entrada explícita corta lo que heredaban las hijas;
5. una fila de una base respeta el permiso de su base (tabla, fila como
   página, PATCH y «Mis tareas»);
6. mover una página cambia de quién hereda, y meter algo en una página pide
   `edit` sobre ella;
7. cambiar permisos pide `owner` y no deja una página sin dueño;
8. lo que existía al migrar queda compartido con el espacio;
9. los avisos de `/events` y el export de una nota no cuentan lo que no se ve.
"""

import sqlite3
import uuid
from contextlib import contextmanager

import pytest

from app import acl
from app.index import DEFAULT_WORKSPACE_ID


def _ok(r, code=200):
    assert r.status_code == code, f"{r.status_code} {r.text}"
    return r.json() if r.content and code != 204 else None


@pytest.fixture()
def gente(client):
    """Da de alta a Ana y Beto (usuarios normales) y abre una sesión para cada uno."""
    from fastapi.testclient import TestClient

    from app.main import app

    ids = {}
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


def compartir(cliente, page_id, entradas, tipo="page"):
    return cliente.put(f"/acl/{tipo}/{page_id}", json={"inherit": False, "entries": entradas})


def owner(uid):
    return {"subject_type": "user", "subject_id": uid, "level": "owner"}


def con(uid, level):
    return {"subject_type": "user", "subject_id": uid, "level": level}


# --------------------------------------------------------------------------
# 1. privado por defecto
# --------------------------------------------------------------------------


def test_lo_de_la_raiz_es_privado_de_quien_lo_crea(client, gente):
    ana, beto = gente["ana"], gente["beto"]
    nota = _ok(ana.post("/pages", json={"title": "Diario de Ana", "body_markdown": "secreto [[Diario de Ana]]"}))
    pid = nota["id"]

    assert _ok(ana.get(f"/pages/{pid}"))["title"] == "Diario de Ana"
    # Beto: 404 (no se filtra que existe) y fuera de todos los listados.
    assert beto.get(f"/pages/{pid}").status_code == 404
    assert beto.patch(f"/pages/{pid}", json={"body_markdown": "x"}).status_code == 404
    assert beto.delete(f"/pages/{pid}").status_code == 404
    assert beto.get(f"/pages/{pid}/export.md").status_code == 404
    assert beto.get(f"/acl/page/{pid}").status_code == 404
    assert pid not in {p["id"] for p in beto.get("/pages").json()}
    assert pid not in {n["id"] for n in beto.get("/pages/tree").json()}
    assert beto.get("/search", params={"q": "secreto"}).json() == []
    assert pid not in {n["id"] for n in beto.get("/graph", params={"mode": "global"}).json()["nodes"]}
    assert beto.get("/graph", params={"mode": "local", "page_id": pid}).status_code == 404
    assert pid not in {p["id"] for p in beto.get("/dashboard").json()["recent_pages"]}
    assert pid in {p["id"] for p in ana.get("/dashboard").json()["recent_pages"]}
    assert pid in {p["id"] for p in ana.get("/pages").json()}
    # El administrador tampoco la ve (plan de roles, R1): gestiona, no lee.
    assert client.get(f"/pages/{pid}").status_code == 404
    assert pid not in {p["id"] for p in client.get("/pages").json()}
    assert client.get("/search", params={"q": "secreto"}).json() == []
    # En el árbol de Ana sale como privada (el candado).
    nodo = next(n for n in ana.get("/pages/tree").json() if n["id"] == pid)
    assert nodo["shared"] is False


# --------------------------------------------------------------------------
# 2. compartir: ver y editar
# --------------------------------------------------------------------------


def test_compartir_para_ver_deja_leer_y_no_escribir(gente):
    ana, beto, ids = gente["ana"], gente["beto"], gente["ids"]
    pid = _ok(ana.post("/pages", json={"title": "Plan"}))["id"]
    _ok(compartir(ana, pid, [owner(ids["ana"]), con(ids["beto"], "view")]))

    assert _ok(beto.get(f"/pages/{pid}"))["title"] == "Plan"
    assert beto.patch(f"/pages/{pid}", json={"body_markdown": "x"}).status_code == 403
    assert beto.post("/pages", json={"title": "Hija", "parent_id": pid}).status_code == 403
    assert next(n for n in ana.get("/pages/tree").json() if n["id"] == pid)["shared"] is True
    leido = _ok(beto.get(f"/acl/page/{pid}"))
    assert leido["your_level"] == "view" and leido["inherit"] is False
    assert {e["name"] for e in leido["entries"]} == {"Ana", "Beto"}

    _ok(compartir(ana, pid, [owner(ids["ana"]), con(ids["beto"], "edit")]))
    assert _ok(beto.patch(f"/pages/{pid}", json={"body_markdown": "de Beto"}))["body_markdown"] == "de Beto"
    hija = _ok(beto.post("/pages", json={"title": "Hija", "parent_id": pid}))
    # La hija hereda: Ana (propietaria de la madre) también la ve.
    assert _ok(ana.get(f"/pages/{hija['id']}"))["title"] == "Hija"


def test_compartir_con_el_espacio(gente):
    ana, beto = gente["ana"], gente["beto"]
    pid = _ok(ana.post("/pages", json={"title": "Para todos"}))["id"]
    _ok(compartir(ana, pid, [
        owner(gente["ids"]["ana"]),
        {"subject_type": "workspace", "subject_id": DEFAULT_WORKSPACE_ID, "level": "view"},
    ]))
    assert _ok(beto.get(f"/pages/{pid}"))["title"] == "Para todos"
    assert beto.patch(f"/pages/{pid}", json={"title": "No"}).status_code == 403


# --------------------------------------------------------------------------
# 3 y 4. herencia a 5 niveles; declarar corta; revocar corta
# --------------------------------------------------------------------------


def _cadena(ana, niveles=5):
    raiz = _ok(ana.post("/pages", json={"title": "N0"}))
    ids = [raiz["id"]]
    for n in range(1, niveles + 1):
        ids.append(_ok(ana.post("/pages", json={"title": f"N{n}", "parent_id": ids[-1]}))["id"])
    return ids


def test_la_herencia_llega_a_cinco_niveles(gente):
    ana, beto, ids = gente["ana"], gente["beto"], gente["ids"]
    cadena = _cadena(ana)
    assert all(beto.get(f"/pages/{p}").status_code == 404 for p in cadena)

    _ok(compartir(ana, cadena[0], [owner(ids["ana"]), con(ids["beto"], "edit")]))
    for p in cadena:
        assert beto.get(f"/pages/{p}").status_code == 200
    assert _ok(beto.patch(f"/pages/{cadena[5]}", json={"body_markdown": "hondo"}))["body_markdown"] == "hondo"
    assert _ok(beto.get(f"/acl/page/{cadena[5]}"))["source_id"] == cadena[0]


def test_una_hija_que_declara_corta_la_herencia_y_volver_a_heredar_la_devuelve(gente):
    ana, beto, ids = gente["ana"], gente["beto"], gente["ids"]
    cadena = _cadena(ana)
    _ok(compartir(ana, cadena[0], [owner(ids["ana"]), con(ids["beto"], "view")]))

    # N3 declara: solo Ana. Desde N3 para abajo, Beto ya no ve nada.
    _ok(compartir(ana, cadena[3], [owner(ids["ana"])]))
    vistas = [beto.get(f"/pages/{p}").status_code for p in cadena]
    assert vistas == [200, 200, 200, 404, 404, 404]
    assert next(n for n in _aplanar(ana.get("/pages/tree").json()) if n["id"] == cadena[3])["shared"] is False

    # Deja de declarar: vuelve a heredar de N0.
    _ok(ana.put(f"/acl/page/{cadena[3]}", json={"inherit": True}))
    assert [beto.get(f"/pages/{p}").status_code for p in cadena] == [200] * 6


def test_revocar_la_entrada_explicita_corta_lo_heredado(gente):
    ana, beto, ids = gente["ana"], gente["beto"], gente["ids"]
    cadena = _cadena(ana)
    _ok(compartir(ana, cadena[0], [owner(ids["ana"]), con(ids["beto"], "edit")]))
    assert beto.get(f"/pages/{cadena[5]}").status_code == 200

    _ok(compartir(ana, cadena[0], [owner(ids["ana"])]))
    assert [beto.get(f"/pages/{p}").status_code for p in cadena] == [404] * 6


def _aplanar(nodos):
    for n in nodos:
        yield n
        yield from _aplanar(n.get("children", []))


# --------------------------------------------------------------------------
# 5. filas de una base
# --------------------------------------------------------------------------


def test_una_fila_respeta_el_permiso_de_su_base(gente):
    ana, beto, ids = gente["ana"], gente["beto"], gente["ids"]
    base = _ok(ana.post("/databases", json={"title": "Tareas", "schema_def": [
        {"key": "quien", "name": "Responsable", "type": "person"},
        {"key": "n", "name": "N", "type": "number"},
    ]}))
    fila = _ok(ana.post(f"/databases/{base['id']}/rows", json={
        "title": "Para Beto", "properties": {"quien": ids["beto"], "n": 1},
    }))
    url = f"/databases/{base['id']}/rows/{fila['id']}"

    assert beto.get(f"/databases/{base['id']}").status_code == 404
    assert beto.get(f"/databases/{base['id']}/rows").status_code == 404
    assert beto.get(url).status_code == 404
    assert beto.get(f"/pages/{fila['id']}").status_code == 404
    assert beto.patch(url, json={"properties": {"n": 2}}).status_code == 404
    assert base["id"] not in {d["id"] for d in beto.get("/databases").json()}
    assert beto.get("/my-tasks").json()["count"] == 0

    _ok(compartir(ana, base["id"], [owner(ids["ana"]), con(ids["beto"], "view")], tipo="database"))
    assert [f["title"] for f in _ok(beto.get(f"/databases/{base['id']}/rows"))] == ["Para Beto"]
    assert _ok(beto.get(f"/pages/{fila['id']}"))["title"] == "Para Beto"
    assert beto.patch(url, json={"properties": {"n": 2}}).status_code == 403
    assert beto.post(f"/databases/{base['id']}/rows", json={"title": "No"}).status_code == 403
    assert beto.get("/my-tasks").json()["count"] == 1
    # Una fila nueva (creada después de compartir) también hereda de su base.
    otra = _ok(ana.post(f"/databases/{base['id']}/rows", json={"title": "Nueva"}))
    assert beto.get(f"/pages/{otra['id']}").status_code == 200


# --------------------------------------------------------------------------
# 6. mover y meter dentro
# --------------------------------------------------------------------------


def test_mover_cambia_de_quien_hereda_y_pide_editar_el_destino(gente):
    ana, beto, ids = gente["ana"], gente["beto"], gente["ids"]
    compartida = _ok(ana.post("/pages", json={"title": "Compartida"}))["id"]
    privada = _ok(ana.post("/pages", json={"title": "Privada"}))["id"]
    _ok(compartir(ana, compartida, [owner(ids["ana"]), con(ids["beto"], "edit")]))
    hija = _ok(ana.post("/pages", json={"title": "Viajera", "parent_id": compartida}))["id"]
    nieta = _ok(ana.post("/pages", json={"title": "Nieta", "parent_id": hija}))["id"]
    assert beto.get(f"/pages/{nieta}").status_code == 200

    # Beto no puede meterla en una página que no ve.
    assert beto.patch(f"/pages/{hija}", json={"parent_id": privada}).status_code == 404
    # Ana sí: y con ella se va la nieta.
    _ok(ana.patch(f"/pages/{hija}", json={"parent_id": privada}))
    assert beto.get(f"/pages/{hija}").status_code == 404
    assert beto.get(f"/pages/{nieta}").status_code == 404


# --------------------------------------------------------------------------
# 7. quién cambia permisos
# --------------------------------------------------------------------------


def test_cambiar_permisos_pide_ser_propietario_y_no_deja_sin_dueno(gente):
    ana, beto, ids = gente["ana"], gente["beto"], gente["ids"]
    pid = _ok(ana.post("/pages", json={"title": "Mía"}))["id"]
    _ok(compartir(ana, pid, [owner(ids["ana"]), con(ids["beto"], "edit")]))

    assert compartir(beto, pid, [owner(ids["beto"])]).status_code == 403
    sin_ana = compartir(ana, pid, [con(ids["beto"], "owner")])
    assert sin_ana.status_code == 400 and "propiedad" in sin_ana.json()["detail"]
    assert ana.put(f"/acl/page/{pid}", json={"inherit": True}).status_code == 400
    assert compartir(ana, pid, [owner(ids["ana"]), con(str(uuid.uuid4()), "view")]).status_code == 400
    # Dar propiedad a otro sí se puede (y entonces ese otro decide).
    _ok(compartir(ana, pid, [owner(ids["ana"]), owner(ids["beto"])]))
    _ok(compartir(beto, pid, [owner(ids["ana"]), owner(ids["beto"])]))


def test_solo_el_administrador_toca_la_papelera_y_el_reindexado(client, gente):
    beto = gente["beto"]
    assert beto.get("/trash").status_code == 403
    assert beto.post("/trash/restore", json={"path": ".trash/x/Notas/a.md"}).status_code == 403
    assert beto.post("/reindex").status_code == 403
    assert client.get("/trash").status_code == 200


# --------------------------------------------------------------------------
# 8. lo que existía al migrar
# --------------------------------------------------------------------------


def test_lo_que_existia_al_migrar_queda_compartido_con_el_espacio(vault_path, index_path, monkeypatch):
    """Un vault de disco que entra por primera vez (migración del hito 2)."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app
    from conftest import iniciar_sesion

    (vault_path / "Notas" / "Vieja").mkdir(parents=True)
    (vault_path / "Notas" / "Vieja.md").write_text("de antes\n", encoding="utf-8")
    (vault_path / "Notas" / "Vieja" / "Hija.md").write_text("hija\n", encoding="utf-8")
    monkeypatch.setattr(settings, "index_path", str(index_path))
    with TestClient(app) as admin:
        iniciar_sesion(admin)
        _ok(admin.post("/auth/users", json={"username": "ana", "display_name": "Ana", "password": "contrasena-ana"}), 201)
        with TestClient(app) as ana:
            _ok(ana.post("/auth/login", json={"username": "ana", "password": "contrasena-ana"}))
            titulos = {p["title"] for p in ana.get("/pages").json()}
            assert {"Vieja", "Hija"} <= titulos
            vieja = next(p for p in ana.get("/pages").json() if p["title"] == "Vieja")
            assert _ok(ana.get(f"/acl/page/{vieja['id']}"))["your_level"] == "edit"


def test_migrar_un_indice_v10_comparte_sus_raices(index_path):
    """La migración 11 sobre un índice que ya tenía páginas."""
    from app import index as indice

    indice.ensure_index(str(index_path))
    conn = indice.connect(str(index_path))
    ws = DEFAULT_WORKSPACE_ID
    conn.execute("INSERT INTO workspaces (id, name, created_at) VALUES (?, 'd', '2026-01-01')", (ws,))
    for pid, parent in (("r1", None), ("h1", "r1")):
        conn.execute(
            "INSERT INTO pages (id, workspace_id, parent_id, type, title, path, created_at, updated_at)"
            " VALUES (?, ?, ?, 'note', ?, ?, '2026-01-01', '2026-01-01')",
            (pid, ws, parent, pid, f"Notas/{pid}.md"),
        )
    conn.execute("DROP TABLE acl")
    conn.execute("DROP TABLE acl_roots")
    conn.execute("PRAGMA user_version = 10")
    conn.close()
    indice._ready.clear()
    indice.ensure_index(str(index_path))
    conn = sqlite3.connect(str(index_path))
    try:
        assert conn.execute("SELECT resource_id, subject_type, level FROM acl").fetchall() == [
            ("r1", "workspace", "edit")
        ]
        assert dict(conn.execute("SELECT id, acl_source FROM pages").fetchall()) == {"r1": "r1", "h1": "r1"}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 9. avisos y export
# --------------------------------------------------------------------------


def test_los_avisos_no_cuentan_lo_que_no_se_ve(index_path, gente):
    from app.auth import User  # noqa: F401 - solo por la forma
    from app.index import connect
    from app.routers.events import _le_toca

    ana, ids = gente["ana"], gente["ids"]
    pid = _ok(ana.post("/pages", json={"title": "Aviso"}))["id"]

    class U:
        def __init__(self, uid, admin=False):
            self.id, self.is_admin = uid, admin

    evento = {"type": "page", "action": "modified", "page_id": pid}
    assert _le_toca(U(ids["ana"]), evento)
    assert not _le_toca(U(ids["beto"]), evento)
    # Al administrador tampoco le llega lo que no ve (plan de roles, R1).
    assert not _le_toca(U("x", admin=True), evento)
    assert _le_toca(U(ids["beto"]), {"type": "page", "action": "modified"})


def test_el_export_de_una_nota_no_materializa_una_base_que_no_ves(gente):
    ana, beto, ids = gente["ana"], gente["beto"], gente["ids"]
    base = _ok(beto.post("/databases", json={"title": "De Beto", "schema_def": []}))
    _ok(beto.post(f"/databases/{base['id']}/rows", json={"title": "Fila secreta"}))
    nota = _ok(ana.post("/pages", json={
        "title": "Con base", "body_markdown": f"```mdm-database\nid: {base['id']}\n```",
    }))
    texto = ana.get(f"/pages/{nota['id']}/export.md").text
    assert "Fila secreta" not in texto
    _ok(compartir(beto, base["id"], [owner(ids["beto"]), con(ids["ana"], "view")], tipo="database"))
    assert "Fila secreta" in ana.get(f"/pages/{nota['id']}/export.md").text


def test_authorize_es_el_unico_punto(gente, index_path):
    """La guarda y los listados preguntan a `acl`; aquí se usa a pelo."""
    from app.index import connect

    ana, ids = gente["ana"], gente["ids"]
    pid = _ok(ana.post("/pages", json={"title": "Directa"}))["id"]

    class U:
        def __init__(self, uid):
            self.id, self.is_admin = uid, False

    conn = connect(str(index_path))
    try:
        assert acl.authorize(conn, U(ids["ana"]), pid, "owner") == "owner"
        with pytest.raises(acl.NotVisible):
            acl.authorize(conn, U(ids["beto"]), pid, "view")
        with pytest.raises(acl.NotVisible):
            acl.authorize(conn, U(ids["beto"]), str(uuid.uuid4()), "view")
    finally:
        conn.close()
