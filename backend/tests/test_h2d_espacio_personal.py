"""Cada persona tiene su «Espacio de <nombre>» (POST /workspaces/personal)."""

from test_h1_espacios import _sesion, cierra  # noqa: F401 - fixtures


def test_se_crea_una_vez_y_va_el_primero(client, cierra):
    antes = client.get("/workspaces").json()
    lista = client.post("/workspaces/personal").json()
    assert len(lista) == len(antes) + 1
    mio = lista[0]
    me = client.get("/auth/me").json()
    assert mio["personal_of"] == me["id"] and mio["name"].startswith("Espacio de ") and mio["member"] is True
    assert mio["role"] == "admin"
    # Idempotente.
    assert len(client.post("/workspaces/personal").json()) == len(lista)


def test_cada_persona_el_suyo_y_no_ve_el_ajeno(client, cierra):
    c, u = _sesion(client, "ana")
    cierra.append(c)
    lista = c.post("/workspaces/personal").json()
    assert [w["name"] for w in lista if w["personal_of"]] == ["Espacio de Ana"]
    # El administrador no lo ve: ni en sus espacios ni entre los que gestiona
    # (un espacio personal no lo gestiona nadie más; plan de roles, R1).
    admin = client.post("/workspaces/personal").json()
    assert not any(w["personal_of"] == u["id"] for w in admin)
    gestionables = client.get("/workspaces", params={"manage": 1}).json()
    assert not any(w["personal_of"] == u["id"] for w in gestionables)
