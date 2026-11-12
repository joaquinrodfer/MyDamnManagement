"""Ancho y orden de las columnas, POR VISTA: lo que se arrastra, se guarda.

La tarjeta pide cuatro cosas que se comprueban aquí sin navegador:

1. un ancho y un orden escritos en la entrada de la vista sobreviven al `.md`,
   al índice y al reindexado (con el ancho como entero, no como texto);
2. renombrar una propiedad se lleva su ancho y su sitio, y borrarla los deja
   fuera: la vista no puede hablar de claves que ya no existen;
3. lo que llegue mal escrito en el frontmatter (`widths: {empresa: ancho}`, un
   `order` que no es lista) se descarta con un aviso, no rompe la vista;
4. el escritor de frontmatter baja mapas de enteros anidados en una vista y los
   vuelve a leer (el round trip, que es de lo que depende todo lo demás).
"""

from __future__ import annotations

import re

from app.vault import fm, reader


def crear_database(client, **cuerpo) -> dict:
    respuesta = client.post("/databases", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_vista(client, database_id, **cuerpo) -> dict:
    respuesta = client.post(f"/databases/{database_id}/views", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def frontmatter(path) -> dict:
    raw, _cuerpo = fm.split_document(path.read_text(encoding="utf-8"))
    assert raw is not None, f"{path} no tiene frontmatter"
    return fm.parse(raw).data


def vista_en_fichero(vault_path, nombre="Tabla") -> dict:
    datos = frontmatter(vault_path / "Bases" / "Proyectos.md")
    return {v["name"]: v for v in datos["views"]}[nombre]


def vista_en_api(client, database_id, nombre="Tabla") -> dict:
    vistas = client.get(f"/databases/{database_id}/views").json()
    return {v["name"]: v for v in vistas}[nombre]


BASE = {
    "title": "Proyectos",
    "schema_def": [
        {"key": "empresa", "name": "Empresa", "type": "text"},
        {"key": "valor", "name": "Valor", "type": "number"},
        {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
    ],
}


# --------------------------------------------------------------------------
# 1. el formato: el ancho es un entero y el orden, una lista de claves
# --------------------------------------------------------------------------


def test_el_escritor_baja_mapas_de_enteros_y_los_vuelve_a_leer():
    """El round trip del que depende todo: un mapa de enteros dentro de la vista.

    Hasta ahora `widths` no existía; esto fija que un `dict` de enteros que va
    dentro de un item de secuencia se escribe como bloque (`widths:` con una
    línea por clave) y se relee como mapa, no como el `repr` de Python ni como
    una construcción opaca.
    """
    raw = "title: Proyectos\n"
    nuevo, avisos = fm.update(
        raw,
        {
            "views": [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "name": "Tabla",
                    "type": "table",
                    "widths": {"empresa": 180, "valor": 90},
                    "order": ["empresa", "valor", "fase"],
                }
            ]
        },
    )
    assert avisos == []
    assert re.search(r"(?m)^    widths:$", nuevo), nuevo
    assert re.search(r"(?m)^      empresa: 180$", nuevo), nuevo
    assert "order: [empresa, valor, fase]" in nuevo
    leido = fm.parse(nuevo).data["views"][0]
    # El parser no infiere tipos (a propósito): el entero vuelve como el texto
    # `"180"`. Convertirlo es cosa del modelo, y eso se comprueba abajo.
    assert leido["widths"] == {"empresa": "180", "valor": "90"}
    assert leido["order"] == ["empresa", "valor", "fase"]


def test_la_vista_guarda_widths_y_order_y_el_indice_los_da_como_enteros(
    client, vault_path
):
    base = crear_database(client, **BASE)
    crear_vista(
        client,
        base["id"],
        name="Tabla",
        type="table",
        config={
            "widths": {"empresa": 180, "valor": 90},
            "order": ["valor", "empresa", "fase"],
        },
    )

    guardada = vista_en_fichero(vault_path)
    assert guardada["id"]
    assert guardada["widths"] == {"empresa": "180", "valor": "90"}
    assert guardada["order"] == ["valor", "empresa", "fase"]

    # La API dice lo mismo que el fichero, con el ancho ya como número.
    leida = vista_en_api(client, base["id"])
    assert leida["config"]["widths"] == {"empresa": 180, "valor": 90}
    assert leida["config"]["order"] == ["valor", "empresa", "fase"]


def test_reindexar_reconstruye_el_mismo_ancho_y_el_mismo_orden(client, vault_path):
    base = crear_database(client, **BASE)
    crear_vista(
        client,
        base["id"],
        name="Tabla",
        type="table",
        config={"widths": {"empresa": 180}, "order": ["valor", "empresa"]},
    )
    assert client.post("/reindex").status_code == 200
    leida = vista_en_api(client, base["id"])
    assert leida["config"]["widths"] == {"empresa": 180}
    assert leida["config"]["order"] == ["valor", "empresa"]


# --------------------------------------------------------------------------
# 2. renombrar y borrar propiedades: el ancho y el sitio viajan
# --------------------------------------------------------------------------


def test_renombrar_una_propiedad_se_lleva_su_ancho_y_su_sitio(client, vault_path):
    base = crear_database(client, **BASE)
    crear_vista(
        client,
        base["id"],
        name="Tabla",
        type="table",
        config={
            "widths": {"empresa": 180, "valor": 90, "title": 240},
            "order": ["valor", "empresa", "fase"],
            "visible_properties": ["empresa", "valor"],
        },
    )

    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {"key": "empresa", "name": "Compañía", "type": "text"},
                {"key": "valor", "name": "Valor", "type": "number"},
                {
                    "key": "fase",
                    "name": "Fase",
                    "type": "select",
                    "options": ["lead", "ganado"],
                },
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text

    guardada = vista_en_fichero(vault_path)
    assert guardada["widths"] == {"compania": "180", "valor": "90", "title": "240"}
    assert "empresa" not in guardada["widths"]
    assert guardada["order"] == ["valor", "compania", "fase"]
    assert guardada["visible_properties"] == ["compania", "valor"]

    leida = vista_en_api(client, base["id"])
    assert leida["config"]["widths"] == {"compania": 180, "valor": 90, "title": 240}
    assert leida["config"]["order"] == ["valor", "compania", "fase"]

    # Y el reindexado no deshace la migración.
    assert client.post("/reindex").status_code == 200
    assert vista_en_api(client, base["id"])["config"]["order"] == ["valor", "compania", "fase"]


def test_borrar_una_propiedad_quita_su_ancho_y_su_sitio(client, vault_path):
    base = crear_database(client, **BASE)
    crear_vista(
        client,
        base["id"],
        name="Tabla",
        type="table",
        config={"widths": {"empresa": 180, "fase": 120}, "order": ["fase", "empresa"]},
    )
    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {"key": "empresa", "name": "Empresa", "type": "text"},
                {"key": "valor", "name": "Valor", "type": "number"},
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text

    guardada = vista_en_fichero(vault_path)
    assert guardada["widths"] == {"empresa": "180"}
    assert guardada["order"] == ["empresa"]


# --------------------------------------------------------------------------
# 3. lo que llega mal escrito se descarta con un aviso
# --------------------------------------------------------------------------


def test_widths_que_no_son_anchos_y_order_que_no_es_lista_se_ignoran():
    avisos: list[str] = []
    vistas = reader.views_from_frontmatter(
        [
            {
                "name": "Tabla",
                "type": "table",
                "widths": {"empresa": "ancho", "valor": "0", "fase": "120"},
                "order": "empresa",
            }
        ],
        "00000000-0000-0000-0000-000000000000",
        "Bases/Proyectos.md",
        avisos,
    )
    config = vistas[0]["config"]
    assert config["widths"] == {"fase": 120}
    assert "order" not in config
    assert any("ancho no válido" in aviso for aviso in avisos), avisos
    assert any("'order'" in aviso for aviso in avisos), avisos


def test_widths_que_no_es_mapa_y_order_con_huecos():
    avisos: list[str] = []
    vistas = reader.views_from_frontmatter(
        [
            {
                "name": "Tabla",
                "type": "table",
                "widths": 180,
                "order": ["empresa", "", None, "valor"],
            }
        ],
        "00000000-0000-0000-0000-000000000000",
        "Bases/Proyectos.md",
        avisos,
    )
    config = vistas[0]["config"]
    assert "widths" not in config
    assert config["order"] == ["empresa", "valor"]
    assert any("'widths'" in aviso for aviso in avisos), avisos


def test_una_vista_sin_widths_ni_order_no_estrena_claves(client, vault_path):
    base = crear_database(client, **BASE)
    crear_vista(client, base["id"], name="Tabla", type="table")
    guardada = vista_en_fichero(vault_path)
    assert "widths" not in guardada
    assert "order" not in guardada
    # Y quitar la configuración de una vista que la tenía la borra del `.md`
    # (no se queda un `widths: {}` vacío, que no diría nada).
    vista = vista_en_api(client, base["id"])
    respuesta = client.patch(
        f"/databases/{base['id']}/views/{vista['id']}",
        json={"config": {}},
    )
    assert respuesta.status_code == 200, respuesta.text
    datos = frontmatter(vault_path / "Bases" / "Proyectos.md")
    assert datos["views"][0].get("widths") is None
