"""Contrato de la API sobre SQLite, con TestClient y un índice temporal.

Cubre lo que antes solo se podía probar con Postgres levantado: páginas
(crear/leer/renombrar/borrar), árbol, wikilinks y backlinks, databases desde
plantilla, filas, vistas con filtro y orden, búsqueda sin acentos y dashboard.
Es el primer paso de P6 (tests que corren en un contenedor sin servicios).
"""

from datetime import date, timedelta

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def crear(client, **cuerpo) -> dict:
    respuesta = client.post("/pages", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_database(client, **cuerpo) -> dict:
    respuesta = client.post("/databases", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def test_health_y_status(client):
    assert client.get("/health").json() == {"status": "ok"}

    estado = client.get("/status").json()
    assert estado["services"]["api"]["ok"] is True
    assert estado["services"]["db"]["ok"] is True
    assert estado["services"]["db"]["engine"] == "sqlite"
    assert estado["services"]["db"]["schema_version"] == 1
    assert estado["services"]["db"]["counts"]["pages"] == 0
    assert estado["checked_at"].endswith("+00:00")


def test_pagina_crear_leer_renombrar_borrar(client):
    creada = crear(client, title="Reunión con Acme", body_markdown="Primera versión")
    assert creada["type"] == "note"
    assert creada["icon"] == "📄"  # icono por defecto de la API
    assert creada["created_by"] == "Tú"
    assert creada["description"] is None
    assert creada["body_markdown"] == "Primera versión"
    assert creada["created_at"].endswith("Z") or creada["created_at"].endswith("+00:00")

    listado = client.get("/pages").json()
    assert [p["id"] for p in listado] == [creada["id"]]

    leida = client.get(f"/pages/{creada['id']}").json()
    assert leida["title"] == "Reunión con Acme"

    renombrada = client.patch(
        f"/pages/{creada['id']}", json={"title": "Reunión con Acme (cerrada)"}
    ).json()
    assert renombrada["title"] == "Reunión con Acme (cerrada)"
    assert renombrada["body_markdown"] == "Primera versión"  # el PATCH no lo pisa
    assert renombrada["updated_at"] >= creada["updated_at"]

    # Guardar solo el cuerpo también mueve updated_at (regresión del paquete de bugs).
    solo_cuerpo = client.patch(
        f"/pages/{creada['id']}", json={"body_markdown": "Segunda versión"}
    ).json()
    assert solo_cuerpo["body_markdown"] == "Segunda versión"
    assert solo_cuerpo["updated_at"] >= renombrada["updated_at"]

    assert client.delete(f"/pages/{creada['id']}").status_code == 204
    assert client.get(f"/pages/{creada['id']}").status_code == 404
    assert client.get("/pages").json() == []


def test_pagina_inexistente_da_404(client):
    assert client.get("/pages/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.patch(
        "/pages/00000000-0000-0000-0000-000000000000", json={"title": "x"}
    ).status_code == 404
    assert client.delete("/pages/00000000-0000-0000-0000-000000000000").status_code == 404


def test_arbol_con_padres_e_hijos(client):
    padre = crear(client, title="Proyectos")
    hijo = crear(client, title="MyDamnManagement", parent_id=padre["id"])
    nieto = crear(client, title="Bugs", parent_id=hijo["id"])

    arbol = client.get("/pages/tree").json()
    assert [n["id"] for n in arbol] == [padre["id"]]
    assert [n["id"] for n in arbol[0]["children"]] == [hijo["id"]]
    assert [n["id"] for n in arbol[0]["children"][0]["children"]] == [nieto["id"]]
    assert arbol[0]["children"][0]["parent_id"] == padre["id"]
    assert arbol[0]["database_id"] is None

    # Borrar el padre arrastra hijo y nieto (cascada encadenada por parent_id).
    assert client.delete(f"/pages/{padre['id']}").status_code == 204
    assert client.get("/pages/tree").json() == []
    assert client.get(f"/pages/{hijo['id']}").status_code == 404
    assert client.get(f"/pages/{nieto['id']}").status_code == 404


def test_wikilinks_y_backlinks(client):
    destino = crear(client, title="Proyecto X")
    origen = crear(
        client,
        title="Nota de origen",
        body_markdown="Ver [[Proyecto X]] y también [[Proyecto X|el proyecto]].",
    )

    backlinks = client.get(f"/pages/{destino['id']}/backlinks").json()
    assert [p["title"] for p in backlinks] == ["Nota de origen"]

    # Quitar el enlace del cuerpo lo quita de backlinks.
    client.patch(f"/pages/{origen['id']}", json={"body_markdown": "Ya sin enlaces"})
    assert client.get(f"/pages/{destino['id']}/backlinks").json() == []

    # Un enlace a una página que no existe no crea backlink ni revienta.
    crear(client, title="Otra", body_markdown="[[No existe]]")
    assert client.get(f"/pages/{destino['id']}/backlinks").json() == []


def test_busqueda_sin_acentos_y_por_cuerpo(client):
    crear(
        client,
        title="Reunión de equipo",
        body_markdown="Se habló del presupuesto anual y del calendario.",
    )
    crear(client, title="Otra nota", body_markdown="Nada que ver")

    por_titulo = client.get("/search", params={"q": "reunion"}).json()
    assert [p["title"] for p in por_titulo] == ["Reunión de equipo"]

    por_cuerpo = client.get("/search", params={"q": "PRESUPUESTO"}).json()
    assert [p["title"] for p in por_cuerpo] == ["Reunión de equipo"]

    # Prefijo: lo que el lematizador de Postgres resolvía como "presupuestos".
    assert client.get("/search", params={"q": "presupu"}).json()

    assert client.get("/search", params={"q": "inexistente"}).json() == []

    # Y al editar el cuerpo, la búsqueda ve el texto nuevo (triggers de FTS5).
    page_id = por_titulo[0]["id"]
    client.patch(f"/pages/{page_id}", json={"body_markdown": "Ahora habla de otro tema"})
    assert client.get("/search", params={"q": "presupuesto"}).json() == []
    assert client.get("/search", params={"q": "otro tema"}).json()


def test_database_filas_y_vistas(client):
    database = crear_database(
        client,
        title="Contactos",
        schema_def=[
            {"key": "empresa", "name": "Empresa", "type": "text"},
            {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
            {"key": "valor", "name": "Valor", "type": "number"},
        ],
    )
    assert database["title"] == "Contactos"
    assert database["schema_def"][1]["options"] == ["lead", "ganado"]
    db_id = database["id"]

    # El árbol expone el id de la database (no el de la página) para navegar.
    arbol = client.get("/pages/tree").json()
    assert arbol[0]["type"] == "database"
    assert arbol[0]["database_id"] == db_id

    fila = client.post(
        f"/databases/{db_id}/rows",
        json={"title": "Acme", "properties": {"empresa": "Acme S.L.", "fase": "lead", "valor": "1200"}},
    ).json()
    assert fila["properties"]["valor"] == 1200.0  # number se coacciona
    assert fila["database_id"] == db_id

    otra = client.post(
        f"/databases/{db_id}/rows",
        json={"title": "Globex", "properties": {"fase": "lead", "valor": "500"}},
    ).json()

    # Propiedad no declarada en el schema: 400, como antes.
    malo = client.post(
        f"/databases/{db_id}/rows", json={"title": "X", "properties": {"inventada": 1}}
    )
    assert malo.status_code == 400

    # PATCH de fila: merge parcial (lo que no llega se conserva).
    editada = client.patch(
        f"/databases/{db_id}/rows/{fila['id']}", json={"properties": {"fase": "ganado"}}
    ).json()
    assert editada["properties"] == {"empresa": "Acme S.L.", "fase": "ganado", "valor": 1200.0}

    # Renombrar la fila no toca sus propiedades.
    renombrada = client.patch(
        f"/databases/{db_id}/rows/{fila['id']}", json={"title": "Acme (cerrada)"}
    ).json()
    assert renombrada["title"] == "Acme (cerrada)"
    assert renombrada["properties"]["fase"] == "ganado"

    # Vista con filtro y orden: los aplica el backend.
    vista = client.post(
        f"/databases/{db_id}/views",
        json={
            "name": "Ganadas",
            "type": "board",
            "config": {
                "group_by": "fase",
                "filters": [{"key": "fase", "op": "eq", "value": "ganado"}],
                "sort": {"key": "valor", "dir": "desc"},
            },
        },
    ).json()
    assert vista["type"] == "board"

    filtradas = client.get(f"/databases/{db_id}/rows", params={"view": vista["id"]}).json()
    assert [f["title"] for f in filtradas] == ["Acme (cerrada)"]

    client.patch(
        f"/databases/{db_id}/views/{vista['id']}", json={"name": "Ganadas 2026"}
    )
    assert [v["name"] for v in client.get(f"/databases/{db_id}/views").json()] == ["Ganadas 2026"]

    # Vista de otra database: 404, no se aplica a ciegas.
    otro_db = crear_database(client, title="Otra")
    assert client.get(
        f"/databases/{otro_db['id']}/rows", params={"view": vista["id"]}
    ).status_code == 404

    # Borrar la database arrastra páginas de fila, filas y vistas.
    assert client.delete(f"/databases/{db_id}").status_code == 204
    assert client.get(f"/databases/{db_id}").status_code == 404
    assert client.get(f"/pages/{fila['id']}").status_code == 404
    assert client.get(f"/pages/{otra['id']}").status_code == 404
    assert client.get("/pages").json()[0]["title"] == "Otra"


def test_borrar_pagina_de_database_arrastra_sus_filas(client):
    database = crear_database(client, title="Tareas")
    fila = client.post(f"/databases/{database['id']}/rows", json={"title": "Una"}).json()

    # El frontend borra databases por su database_id, pero DELETE /pages/{id}
    # (borrado en lote del árbol) usa el id de la página: las dos vías tienen
    # que dejar el mismo estado, sin filas huérfanas.
    assert client.delete(f"/pages/{database['page_id']}").status_code == 204
    assert client.get(f"/databases/{database['id']}").status_code == 404
    assert client.get(f"/pages/{fila['id']}").status_code == 404
    assert client.get("/pages").json() == []


def test_plantilla_crm_y_dashboard(client):
    plantillas = client.get("/databases/templates").json()
    assert {p["key"] for p in plantillas} == {"crm", "tasks"}

    crm = client.post("/databases/from-template", json={"template": "crm"}).json()
    assert crm["title"] == "Contactos"
    assert {p["key"] for p in crm["schema_def"]} == {
        "empresa",
        "fase",
        "valor",
        "proximo_contacto",
    }
    vistas = client.get(f"/databases/{crm['id']}/views").json()
    assert {v["name"] for v in vistas} == {"Pipeline", "Tabla"}

    hoy = date.today().isoformat()
    proximo = (date.today() + timedelta(days=3)).isoformat()
    client.post(
        f"/databases/{crm['id']}/rows",
        json={"title": "Acme", "properties": {"fase": "lead", "proximo_contacto": hoy}},
    )
    client.post(
        f"/databases/{crm['id']}/rows",
        json={"title": "Globex", "properties": {"fase": "lead", "proximo_contacto": proximo}},
    )
    client.post(
        f"/databases/{crm['id']}/rows",
        json={"title": "Lejano", "properties": {"proximo_contacto": "2099-01-01"}},
    )

    dashboard = client.get("/dashboard").json()
    assert dashboard["recent_pages"][0]["title"] == "Contactos"
    assert dashboard["recent_pages"][0]["database_id"] == crm["id"]

    fechas = dashboard["upcoming_dates"]
    assert [f["row_title"] for f in fechas] == ["Acme", "Globex"]
    assert [f["days_from_today"] for f in fechas] == [0, 3]
    assert fechas[0]["property_name"] == "Próximo contacto"
    assert fechas[0]["database_title"] == "Contactos"


def test_imagen_de_cabecera(client):
    page = crear(client, title="Con cabecera")

    subida = client.post(
        f"/pages/{page['id']}/header-image",
        files={"file": ("cabecera.png", PNG, "image/png")},
    )
    assert subida.status_code == 200
    assert subida.json()["header_image_path"] == f"/files/pages/{page['id']}/header.png"

    # Solo valen imágenes de verdad: un SVG (o HTML) disfrazado, no.
    rechazada = client.post(
        f"/pages/{page['id']}/header-image",
        files={"file": ("x.svg", b"<svg></svg>", "image/svg+xml")},
    )
    assert rechazada.status_code == 415

    borrada = client.delete(f"/pages/{page['id']}/header-image")
    assert borrada.status_code == 200
    assert borrada.json()["header_image_path"] is None


def test_el_indice_sobrevive_a_una_peticion_nueva(client, index_path):
    crear(client, title="Persistente")
    # Otra petición = otra conexión al mismo fichero: los datos están en disco,
    # no en memoria del proceso.
    assert index_path.exists()
    assert [p["title"] for p in client.get("/pages").json()] == ["Persistente"]
