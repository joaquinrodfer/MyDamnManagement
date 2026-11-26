"""Permisos por usuario: crear espacios e invitar (por defecto, retirables),
salir de un espacio y la ficha de permisos de cada persona."""

from test_h1_espacios import _sesion, cierra  # noqa: F401 - fixtures


def _grant_id(client, user_id, capability):
    ficha = client.get(f"/users/{user_id}/access").json()
    cap = next(c for c in ficha["capabilities"] if c["key"] == capability)
    return next(g["id"] for g in cap["grants"] if g["via"] == "user")


def test_todos_crean_espacios_por_defecto_y_se_puede_retirar(client, cierra):
    ana, datos = _sesion(client, "ana")
    cierra.append(ana)
    r = ana.post("/workspaces", json={"name": "Equipo de Ana"})
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "admin"
    client.delete(f"/grants/{_grant_id(client, datos['id'], 'create_workspaces')}")
    assert ana.post("/workspaces", json={"name": "Otro"}).status_code == 403


def test_invitar_a_sus_espacios_y_retirarlo(client, cierra):
    ana, datos = _sesion(client, "ana")
    luis, dluis = _sesion(client, "luis")
    eva, deva = _sesion(client, "eva")
    cierra.extend([ana, luis, eva])
    wid = ana.post("/workspaces", json={"name": "De Ana"}).json()["id"]
    assert ana.put(f"/workspaces/{wid}/members/user/{dluis['id']}", json={"role": "editor"}).status_code == 200
    # Invitación por enlace a su espacio: también.
    assert ana.post("/auth/invites", json={"workspace_id": wid, "role": "editor"}).status_code == 201
    client.delete(f"/grants/{_grant_id(client, datos['id'], 'invite_people')}")
    assert ana.put(f"/workspaces/{wid}/members/user/{deva['id']}", json={"role": "editor"}).status_code == 403
    assert ana.post("/auth/invites", json={"workspace_id": wid, "role": "editor"}).status_code == 403
    # Cambiar el rol de quien ya está sigue siendo administrar su espacio.
    assert ana.put(f"/workspaces/{wid}/members/user/{dluis['id']}", json={"role": "viewer"}).status_code == 200


def test_salir_de_un_espacio(client, cierra):
    ana, _ = _sesion(client, "ana")
    luis, dluis = _sesion(client, "luis")
    cierra.extend([ana, luis])
    wid = ana.post("/workspaces", json={"name": "De Ana"}).json()["id"]
    ana.put(f"/workspaces/{wid}/members/user/{dluis['id']}", json={"role": "editor"})
    # La única administradora no puede irse.
    r = ana.post(f"/workspaces/{wid}/leave")
    assert r.status_code == 400 and "único administrador" in r.json()["detail"]
    assert luis.post(f"/workspaces/{wid}/leave").status_code == 204
    assert wid not in {w["id"] for w in luis.get("/workspaces").json()}
    assert luis.post(f"/workspaces/{wid}/leave").status_code == 404
    # Del personal no se sale.
    personal = next(w for w in ana.post("/workspaces/personal").json() if w.get("personal_of"))
    assert ana.post(f"/workspaces/{personal['id']}/leave").status_code == 400


def test_ficha_de_permisos_de_una_persona(client, cierra):
    ana, datos = _sesion(client, "ana")
    cierra.append(ana)
    ficha = client.get(f"/users/{datos['id']}/access").json()
    assert ficha["user"]["username"] == "ana"
    tiene = {c["key"] for c in ficha["capabilities"] if c["grants"]}
    assert tiene == {"create_workspaces", "invite_people"}
    assert {c["key"] for c in ficha["capabilities"]} >= {"manage_people", "backups"}
    assert any(w["name"] for w in ficha["workspaces"])
    # Solo el administrador la ve.
    assert ana.get(f"/users/{datos['id']}/access").status_code == 403
