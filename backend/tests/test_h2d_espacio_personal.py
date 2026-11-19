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
    # El superadministrador lo ve pero no es miembro.
    admin = client.post("/workspaces/personal").json()
    ajeno = next(w for w in admin if w["personal_of"] == u["id"])
    assert ajeno["member"] is False
