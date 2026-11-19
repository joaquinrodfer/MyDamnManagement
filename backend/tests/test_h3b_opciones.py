"""El desplegable de una selección: crear opciones, color y grupo (`POST /databases/{id}/options`)."""


def _base_tareas(client):
    return client.post("/databases/from-template", json={"template": "tareas", "title": "T", "section": "shared"}).json()


def _estado(db):
    return next(p for p in db["schema_def"] if p["key"] == db["kind"]["roles"]["status"])


def test_color_de_una_opcion_se_guarda_y_sobrevive_a_guardar_el_schema(client):
    db = _base_tareas(client)
    clave = db["kind"]["roles"]["status"]
    r = client.post(f"/databases/{db['id']}/options", json={"property": clave, "colors": {"En curso": "red"}})
    assert r.status_code == 200, r.text
    assert _estado(r.json())["option_colors"] == {"En curso": "red"}
    # Guardar el schema desde el diálogo de propiedades (sin mandar colores) los conserva.
    esquema = [{k: v for k, v in p.items() if k != "option_colors"} for p in r.json()["schema_def"]]
    r2 = client.patch(f"/databases/{db['id']}", json={"schema_def": esquema})
    assert r2.status_code == 200, r2.text
    assert _estado(r2.json())["option_colors"] == {"En curso": "red"}
    # Quitar el color.
    r3 = client.post(f"/databases/{db['id']}/options", json={"property": clave, "colors": {"En curso": None}})
    assert "option_colors" not in _estado(r3.json())
    assert client.post(f"/databases/{db['id']}/options", json={"property": clave, "colors": {"En curso": "fucsia"}}).status_code == 400


def test_crear_una_opcion_va_a_un_grupo_y_se_puede_mover(client):
    db = _base_tareas(client)
    clave = db["kind"]["roles"]["status"]
    r = client.post(f"/databases/{db['id']}/options", json={"property": clave, "add": "En pruebas", "group": "En curso"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert "En pruebas" in _estado(d)["options"]
    grupos = {g["name"]: g["options"] for g in d["closed"]["groups"]}
    assert "En pruebas" in grupos["En curso"]
    r = client.post(f"/databases/{db['id']}/options", json={"property": clave, "move": "En pruebas", "group": "Hecho"})
    grupos = {g["name"]: g["options"] for g in r.json()["closed"]["groups"]}
    assert "En pruebas" in grupos["Hecho"] and "En pruebas" not in grupos["En curso"]
    assert "En pruebas" in r.json()["closed"]["values"]  # ahora cuenta como hecha


def test_una_selección_sin_grupos_acepta_opciones_y_colores(client):
    db = client.post("/databases", json={"title": "X", "schema_def": [{"name": "Tipo", "type": "multiselect", "options": ["a", "b"]}]}).json()
    clave = db["schema_def"][0]["key"]
    r = client.post(f"/databases/{db['id']}/options", json={"property": clave, "add": "c", "colors": {"c": "green"}})
    assert r.status_code == 200, r.text
    p = r.json()["schema_def"][0]
    assert p["options"] == ["a", "b", "c"] and p["option_colors"] == {"c": "green"}
    assert client.post(f"/databases/{db['id']}/options", json={"property": "nada", "add": "x"}).status_code == 400
