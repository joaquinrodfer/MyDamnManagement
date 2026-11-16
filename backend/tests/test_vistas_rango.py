"""El rango de un calendario: `start_property` y `end_property` en la vista.

Lo que fija esta tarjeta (del lado del servidor):

1. una vista de calendario puede declarar el rango con dos claves de propiedad
   de fecha, y se guarda en el frontmatter de `Bases/<slug>.md` como cualquier
   otra clave de la vista;
2. el rango se valida contra el `schema` de la base: una clave que no existe o
   una propiedad que no es fecha es un 400 que dice cuál (`end_property` es
   opcional: solo inicio = filas de un día);
3. renombrar una de esas propiedades migra la clave dentro del `config` de la
   vista, y borrarla quita la clave (como ya pasaba con `group_by` y
   `date_property`).

El dibujo de la barra es del frontend; aquí solo se fija el contrato.
"""

from __future__ import annotations

from app.vault import fm


def crear_base(client, *, tipo_calendario: str = "date") -> dict:
    respuesta = client.post(
        "/databases",
        json={
            "title": "Plan",
            "schema_def": [
                {"key": "inicio", "name": "Inicio", "type": "date"},
                {"key": "entrega", "name": "Entrega", "type": "date"},
                {"key": "nota", "name": "Nota", "type": "text"},
            ],
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_vista(client, database_id, **cuerpo) -> dict:
    respuesta = client.post(f"/databases/{database_id}/views", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def config_de_vista(vault_path, nombre: str) -> dict:
    """El `config` de una vista tal y como queda escrito en `Bases/<slug>.md`.

    En el fichero las claves del `config` van **planas** dentro de la vista
    (`date_property`, `start_property`…) y el id/nombre/tipo son suyos: aquí se
    separa lo que es configuración de lo que es identidad.
    """
    crudo, _cuerpo = fm.split_document((vault_path / "Bases" / "Plan.md").read_text("utf-8"))
    datos = fm.parse(crudo or "").data
    vista = next(v for v in datos["views"] if v["name"] == nombre)
    return {k: v for k, v in vista.items() if k not in ("id", "name", "type")}


# ---------------------------------------------------------------------------


def test_una_vista_de_calendario_guarda_el_rango_en_el_md(client, vault_path):
    base = crear_base(client)
    vista = crear_vista(
        client,
        base["id"],
        name="Plan",
        type="calendar",
        config={"start_property": "inicio", "end_property": "entrega"},
    )
    assert vista["config"] == {"start_property": "inicio", "end_property": "entrega"}
    # El `.md` de la base es la verdad: el rango está escrito allí.
    assert config_de_vista(vault_path, "Plan") == {
        "start_property": "inicio",
        "end_property": "entrega",
    }

    # Un rango puede ser de solo inicio: la fila dura un día.
    solo_inicio = crear_vista(
        client,
        base["id"],
        name="Hitos",
        type="calendar",
        config={"start_property": "inicio"},
    )
    assert solo_inicio["config"] == {"start_property": "inicio"}

    # Y una vista sin rango sigue siendo la de siempre.
    sin_rango = crear_vista(
        client, base["id"], name="Agenda", type="calendar", config={"date_property": "entrega"}
    )
    assert sin_rango["config"] == {"date_property": "entrega"}


def test_el_rango_se_puede_cambiar_con_un_patch(client, vault_path):
    base = crear_base(client)
    vista = crear_vista(
        client,
        base["id"],
        name="Plan",
        type="calendar",
        config={"start_property": "inicio", "end_property": "entrega"},
    )
    respuesta = client.patch(
        f"/databases/{base['id']}/views/{vista['id']}",
        json={"config": {"start_property": "entrega"}},
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["config"] == {"start_property": "entrega"}
    assert config_de_vista(vault_path, "Plan") == {"start_property": "entrega"}


def test_una_clave_de_rango_que_no_existe_es_un_400(client):
    base = crear_base(client)
    respuesta = client.post(
        f"/databases/{base['id']}/views",
        json={
            "name": "Plan",
            "type": "calendar",
            "config": {"start_property": "arranque"},
        },
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "arranque" in respuesta.json()["detail"]


def test_una_propiedad_que_no_es_fecha_no_vale_como_rango(client):
    base = crear_base(client)
    respuesta = client.post(
        f"/databases/{base['id']}/views",
        json={
            "name": "Plan",
            "type": "calendar",
            "config": {"start_property": "inicio", "end_property": "nota"},
        },
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "Nota" in respuesta.json()["detail"]
    # Y no se ha creado nada: la base sigue con las vistas que tenía.
    vistas = client.get(f"/databases/{base['id']}/views").json()
    assert [v["name"] for v in vistas] != ["Plan"]


def test_el_inicio_y_el_fin_no_pueden_ser_la_misma_propiedad(client):
    base = crear_base(client)
    respuesta = client.post(
        f"/databases/{base['id']}/views",
        json={
            "name": "Plan",
            "type": "calendar",
            "config": {"start_property": "inicio", "end_property": "inicio"},
        },
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "misma propiedad" in respuesta.json()["detail"]


def test_el_patch_tambien_valida_el_rango(client):
    base = crear_base(client)
    vista = crear_vista(
        client, base["id"], name="Plan", type="calendar", config={"start_property": "inicio"}
    )
    respuesta = client.patch(
        f"/databases/{base['id']}/views/{vista['id']}",
        json={"config": {"start_property": "inicio", "end_property": "no_existe"}},
    )
    assert respuesta.status_code == 400, respuesta.text
    # La vista sigue como estaba: validar antes de escribir.
    vistas = client.get(f"/databases/{base['id']}/views").json()
    assert next(v for v in vistas if v["name"] == "Plan")["config"] == {
        "start_property": "inicio"
    }


def test_renombrar_una_fecha_migra_el_rango_de_la_vista(client, vault_path):
    base = crear_base(client)
    crear_vista(
        client,
        base["id"],
        name="Plan",
        type="calendar",
        config={"start_property": "inicio", "end_property": "entrega"},
    )
    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {"key": "inicio", "name": "Arranque", "type": "date"},
                {"key": "entrega", "name": "Entrega", "type": "date"},
                {"key": "nota", "name": "Nota", "type": "text"},
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    config = config_de_vista(vault_path, "Plan")
    assert config["start_property"] == "arranque"
    assert config["end_property"] == "entrega"


def test_borrar_una_fecha_quita_el_rango_de_la_vista(client, vault_path):
    base = crear_base(client)
    crear_vista(
        client,
        base["id"],
        name="Plan",
        type="calendar",
        config={"start_property": "inicio", "end_property": "entrega"},
    )
    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {"key": "inicio", "name": "Inicio", "type": "date"},
                {"key": "nota", "name": "Nota", "type": "text"},
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    config = config_de_vista(vault_path, "Plan")
    assert config == {"start_property": "inicio"}
