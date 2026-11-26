"""Una base se puede crear directamente en Privado (sin pasar por Compartido)."""


def _seccion(client, page_id):
    arbol = client.get("/pages/tree").json()
    pila = list(arbol if isinstance(arbol, list) else arbol.get("nodes", []))
    while pila:
        n = pila.pop()
        if str(n.get("id")) == str(page_id):
            return n.get("section")
        pila.extend(n.get("children") or [])
    return None


def test_base_vacia_y_de_plantilla_en_privado(client):
    vacia = client.post("/databases", json={"title": "Mía", "schema_def": [], "section": "private"})
    assert vacia.status_code == 200, vacia.text
    tareas = client.post("/databases/from-template", json={"template": "tareas", "title": "Mis tareas privadas", "section": "private"})
    assert tareas.status_code == 200, tareas.text
    compartida = client.post("/databases", json={"title": "Común", "schema_def": [], "section": "shared"}).json()
    assert _seccion(client, vacia.json()["page_id"]) == "private"
    assert _seccion(client, tareas.json()["page_id"]) == "private"
    assert _seccion(client, compartida["page_id"]) == "shared"
