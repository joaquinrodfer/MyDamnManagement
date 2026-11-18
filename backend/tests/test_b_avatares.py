"""Bloque B: avatares de usuario, autor de las filas y color de cursor por nota.

- Cada usuario tiene un avatar de los de la app (`auth.AVATARS`): lo elige al
  crearse (o se le da uno estable) y lo cambia él mismo o un admin.
- El avatar sale donde sale la persona: `/auth/me`, `/people`, las celdas de
  persona y la autoría de una página (`created_by_avatar`).
- Una fila dice quién la creó, como una nota (y no es una propiedad).
- En una sala, cada persona tiene un color de cursor que no tiene nadie más
  dentro de esa nota.
"""

from fastapi.testclient import TestClient

from app import auth
from test_h4_colaboracion import entrar, nota, servidor  # noqa: F401 - fixtures


def test_avatar_por_defecto_estable_y_de_la_lista(client):
    me = client.get("/auth/me").json()
    assert me["avatar"] in auth.AVATARS
    assert auth.default_avatar("alguien") == auth.default_avatar("alguien")


def test_alta_con_avatar_y_avatar_no_valido(client):
    r = client.post("/auth/users", json={
        "username": "ana", "display_name": "Ana", "password": "contrasena-ana", "avatar": "zorro",
    })
    assert r.status_code == 201, r.text
    assert r.json()["avatar"] == "zorro"
    malo = client.post("/auth/users", json={
        "username": "beto", "display_name": "Beto", "password": "contrasena-beto", "avatar": "../x",
    })
    assert malo.status_code == 400
    # Un admin se lo cambia a otro.
    ana = r.json()
    r = client.patch(f"/auth/users/{ana['id']}", json={"avatar": "buho"})
    assert r.json()["avatar"] == "buho"
    # Y está en `/people` (el selector de una celda de persona).
    gente = {p["username"]: p for p in client.get("/people").json()}
    assert gente["ana"]["avatar"] == "buho"


def test_cada_uno_cambia_su_avatar(client):
    r = client.patch("/auth/me", json={"avatar": "panda"})
    assert r.status_code == 200, r.text
    assert r.json()["avatar"] == "panda"
    nombre = r.json()["display_name"]
    # Cambiar el avatar no toca el nombre (antes el nombre era obligatorio).
    assert client.get("/auth/me").json()["display_name"] == nombre
    assert client.patch("/auth/me", json={"avatar": "dragon"}).status_code == 400


def test_la_autoria_lleva_el_avatar(client):
    client.patch("/auth/me", json={"avatar": "rana"})
    pagina = client.post("/pages", json={"title": "Mía"}).json()
    assert pagina["created_by_avatar"] == "rana"
    assert client.get(f"/pages/{pagina['id']}").json()["created_by_avatar"] == "rana"


def test_la_celda_de_persona_lleva_el_avatar(client):
    ana = client.post("/auth/users", json={
        "username": "ana", "display_name": "Ana", "password": "contrasena-ana", "avatar": "koala",
    }).json()
    base = client.post("/databases", json={"title": "B", "schema_def": [{"name": "Quién", "type": "person"}]}).json()
    clave = base["schema_def"][0]["key"]
    fila = client.post(f"/databases/{base['id']}/rows", json={"title": "F", "properties": {clave: ana["id"]}}).json()
    assert fila["people"][clave]["avatar"] == "koala"


def test_una_fila_dice_quien_la_creo(client):
    me = client.get("/auth/me").json()
    base = client.post("/databases", json={"title": "Autoría", "schema_def": [{"name": "Notas", "type": "text"}]}).json()
    fila = client.post(f"/databases/{base['id']}/rows", json={"title": "Con autor"}).json()
    pagina = client.get(f"/pages/{fila['id']}").json()
    assert pagina["created_by"] == me["display_name"]
    assert pagina["created_by_avatar"] == me["avatar"]
    # No es una propiedad de la fila.
    assert "created_by" not in client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()["properties"]
    # Y sobrevive a guardar la fila y a reindexar (está en su `.md`).
    client.patch(f"/databases/{base['id']}/rows/{fila['id']}", json={"properties": {"notas": "x"}})
    assert client.post("/reindex?force=true").status_code == 200
    assert client.get(f"/pages/{fila['id']}").json()["created_by"] == me["display_name"]


# --------------------------------------------------------------------------
# color de cursor por nota
# --------------------------------------------------------------------------


def _usuario(servidor, nombre):
    from test_h4_colaboracion import Sesion

    servidor.req("POST", "/auth/users", {
        "username": nombre, "display_name": nombre.title(), "password": f"contrasena-{nombre}",
    }, expect=201)
    s = Sesion(servidor.base)
    s.req("POST", "/auth/login", {"username": nombre, "password": f"contrasena-{nombre}"}, expect=200)
    return s


def test_cada_persona_tiene_su_color_en_la_nota(servidor):
    admin = servidor
    pid = nota(admin, "hola")
    me = admin.req("GET", "/auth/me")
    # Compartida con todos para que los demás puedan entrar.
    otros = [_usuario(admin, n) for n in ("ana", "beto", "carla")]
    entradas = [{"subject_type": "user", "subject_id": me["id"], "level": "owner"}]
    for s in otros:
        entradas.append({"subject_type": "user", "subject_id": s.req("GET", "/auth/me")["id"], "level": "edit"})
    admin.req("PUT", f"/acl/page/{pid}", {"inherit": False, "entries": entradas}, expect=200)

    salas = [admin.sala(pid)] + [s.sala(pid) for s in otros]
    try:
        for ws in salas:
            entrar(ws)
        # La última en entrar ve a las demás, cada una con su color.
        ultima = salas[-1]
        ultima.send({"type": "presence", "anchor": 0, "head": 0, "version": 0})
        colores = {}
        for ws in salas[:-1]:
            ws.send({"type": "presence", "anchor": 1, "head": 1, "version": 0})
        while len(colores) < 3:
            msg = ultima.recv()
            if msg["type"] == "presence" and not msg.get("gone"):
                colores[msg["user"]["id"]] = msg["user"]["color"]
        assert len(set(colores.values())) == 3, colores
        # Y el avatar va con la presencia (el nombre del cursor lo enseña).
        from app.routers.collab import rooms

        sala = next(iter(rooms._rooms.values()))
        assert len({p.color for p in sala.peers.values()}) == 4
        assert all(p.avatar in auth.AVATARS for p in sala.peers.values())
    finally:
        for ws in salas:
            ws.close()


def test_la_misma_persona_en_dos_pestanas_tiene_el_mismo_color(servidor):
    admin = servidor
    pid = nota(admin, "x")
    with admin.sala(pid) as a, admin.sala(pid) as b:
        entrar(a)
        entrar(b)
        from app.routers.collab import rooms

        sala = rooms._rooms[pid]
        assert len({p.color for p in sala.peers.values()}) == 1


def test_sin_sesion_no_hay_avatar(client):
    from app.main import app

    with TestClient(app) as anonimo:
        assert anonimo.get("/auth/me").status_code == 401


def test_los_avatares_se_ven_sin_sesion_y_nada_mas(client):
    from app.main import app

    with TestClient(app) as anonimo:
        assert anonimo.get("/avatars/gato.svg").status_code == 200
        assert anonimo.get("/avatars/%2e%2e/app.js").status_code in (401, 404)
