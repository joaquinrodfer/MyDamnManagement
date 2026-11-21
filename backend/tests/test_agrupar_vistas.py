"""«Agrupar por» en tabla, lista y tablero: `group_by` en la vista.

El servidor no agrupa (eso lo pinta el navegador), pero sí comprueba que una
agrupación nueva nombra una propiedad que existe y que puede agrupar, y deja
en paz la de una vista vieja cuando se guarda otra cosa.
"""

from __future__ import annotations


def _base(client) -> dict:
    r = client.post(
        "/databases",
        json={
            "title": "Tareas",
            "schema_def": [
                {"key": "estado", "name": "Estado", "type": "select", "options": ["Abierto", "Hecho"]},
                {"key": "etiquetas", "name": "Etiquetas", "type": "multiselect", "options": ["a", "b"]},
                {"key": "vence", "name": "Vence", "type": "date"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_una_tabla_y_una_lista_se_crean_agrupadas(client):
    base = _base(client)
    for tipo in ("table", "list"):
        r = client.post(
            f"/databases/{base['id']}/views",
            json={"name": f"Por estado {tipo}", "type": tipo, "config": {"group_by": "estado"}},
        )
        assert r.status_code == 200, r.text
        assert r.json()["config"]["group_by"] == "estado"


def test_cambiar_y_quitar_la_agrupacion(client):
    base = _base(client)
    vista = client.post(f"/databases/{base['id']}/views", json={"name": "Tabla", "type": "table", "config": {}}).json()

    r = client.patch(f"/databases/{base['id']}/views/{vista['id']}", json={"config": {"group_by": "vence"}})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["group_by"] == "vence"

    r = client.patch(f"/databases/{base['id']}/views/{vista['id']}", json={"config": {}})
    assert r.status_code == 200, r.text
    assert "group_by" not in r.json()["config"]


def test_no_se_agrupa_por_lo_que_no_existe_ni_por_una_multiseleccion(client):
    base = _base(client)
    r = client.post(
        f"/databases/{base['id']}/views",
        json={"name": "Mal", "type": "table", "config": {"group_by": "no_existe"}},
    )
    assert r.status_code == 400
    assert "no existe" in r.json()["detail"]

    vista = client.post(f"/databases/{base['id']}/views", json={"name": "Tabla", "type": "table", "config": {}}).json()
    r = client.patch(f"/databases/{base['id']}/views/{vista['id']}", json={"config": {"group_by": "etiquetas"}})
    assert r.status_code == 400
    assert "varios grupos" in r.json()["detail"]


def test_una_vista_vieja_guarda_otras_cosas_aunque_su_agrupacion_ya_no_valga(client):
    """Una vista guardada antes de la regla (o editada a mano en el `.md`)
    sigue pudiendo guardar sus anchos: el 400 es solo para lo que se cambia."""
    from app.views import validar_agrupacion

    schema = [{"key": "etiquetas", "name": "Etiquetas", "type": "multiselect"}]
    vieja = {"group_by": "etiquetas"}
    assert validar_agrupacion({"group_by": "etiquetas", "widths": {"title": 200}}, schema, vieja) is None
    assert validar_agrupacion({"group_by": "etiquetas"}, schema, {}) is not None
    # Una relación múltiple tampoco, ni aunque el `.md` diga `multiple: "true"`.
    rel = [{"key": "r", "name": "Rel", "type": "relation", "multiple": "true"}]
    assert validar_agrupacion({"group_by": "r"}, rel) is not None
    assert validar_agrupacion({"group_by": "r"}, [{"key": "r", "name": "Rel", "type": "relation", "multiple": "false"}]) is None
