"""Columnas desde la cabecera: el contrato de vista en el que se apoya el menú.

El menú del `<th>` no inventa datos: ocultar una columna escribe
`visible_properties` en la entrada `views` del `.md` de la base, y renombrar o
borrar una propiedad pasa por el `PATCH /databases/{id}` de siempre (el que ya
usa el diálogo «Propiedades»), que migra las filas y las vistas.

Este módulo fija lo que la interfaz da por hecho y no puede comprobar sola:

1. `visible_properties` de una vista se guarda en el `.md`, sobrevive a una
   recarga y a un reindexado, y no toca ni una fila;
2. quitar `visible_properties` devuelve la vista a «todas», que es el estado
   con el que se pinta la pestaña «Todas (sin vista)»;
3. renombrar una columna desde la cabecera traduce la lista de la vista;
4. borrar una columna la quita de la lista y del schema, y **deja los valores
   en el `.md` de cada fila** (lo que dice la confirmación del menú).
"""

from pathlib import Path

SCHEMA = [
    {"key": "empresa", "name": "Empresa", "type": "text"},
    {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
    {"key": "fecha", "name": "Fecha", "type": "date"},
]


def crear_base(client) -> dict:
    respuesta = client.post("/databases", json={"title": "Contactos", "schema_def": SCHEMA})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id, titulo, properties) -> dict:
    respuesta = client.post(
        f"/databases/{database_id}/rows", json={"title": titulo, "properties": properties}
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_vista(client, database_id, nombre, config=None) -> dict:
    respuesta = client.post(
        f"/databases/{database_id}/views",
        json={"name": nombre, "type": "table", "config": config or {}},
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def fichero_de_base(vault_path: Path) -> Path:
    return vault_path / "Bases" / "Contactos.md"


def fichero_de_fila(vault_path: Path, fila: dict) -> Path:
    return vault_path / "Bases" / "Contactos" / f"{fila['id'][:8]}.md"


def vistas_del_md(vault_path: Path) -> dict[str, dict]:
    from app.vault import fm

    raw, _cuerpo = fm.split_document(fichero_de_base(vault_path).read_text(encoding="utf-8"))
    assert raw is not None, "la base no tiene frontmatter"
    return {v["name"]: v for v in (fm.parse(raw).data.get("views") or [])}


def test_ocultar_una_columna_es_visible_properties_y_sobrevive_al_reindexado(
    client, vault_path, index_path
):
    base = crear_base(client)
    crear_fila(client, base["id"], "Acme", {"empresa": "Acme", "fase": "lead", "fecha": "2026-11-10"})
    crear_fila(client, base["id"], "Globex", {"empresa": "Globex", "fase": "ganado"})
    vista = crear_vista(client, base["id"], "Tabla")
    crear_vista(client, base["id"], "Otra")

    filas_antes = {
        f.name: f.read_text(encoding="utf-8")
        for f in (vault_path / "Bases" / "Contactos").glob("*.md")
    }

    # «Ocultar esta columna» en la vista Tabla: la lista explícita de las que
    # quedan, en el orden en que se ven.
    respuesta = client.patch(
        f"/databases/{base['id']}/views/{vista['id']}",
        json={"config": {"visible_properties": ["empresa", "fecha"]}},
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["config"]["visible_properties"] == ["empresa", "fecha"]

    # (a) el `.md` de la base lo dice, y la otra vista no se ha tocado.
    vistas = vistas_del_md(vault_path)
    assert vistas["Tabla"]["visible_properties"] == ["empresa", "fecha"]
    assert "visible_properties" not in vistas["Otra"]

    # (b) ocultar una columna no toca los ficheros de las filas.
    filas_despues = {
        f.name: f.read_text(encoding="utf-8")
        for f in (vault_path / "Bases" / "Contactos").glob("*.md")
    }
    assert filas_despues == filas_antes

    # (c) el índice se rehace del `.md`: la vista sigue ocultando lo mismo.
    assert client.post("/reindex?force=true").status_code == 200
    vistas_api = {v["name"]: v for v in client.get(f"/databases/{base['id']}/views").json()}
    assert vistas_api["Tabla"]["config"]["visible_properties"] == ["empresa", "fecha"]
    assert "visible_properties" not in vistas_api["Otra"]["config"]

    # (d) «Todas» no es una vista: el esquema sigue teniendo las tres columnas.
    assert [p["key"] for p in client.get(f"/databases/{base['id']}").json()["schema_def"]] == [
        "empresa",
        "fase",
        "fecha",
    ]


def test_mostrar_todas_es_quitar_la_lista(client, vault_path):
    base = crear_base(client)
    vista = crear_vista(client, base["id"], "Tabla", {"visible_properties": ["empresa"]})

    # «Mostrar todas»: el estado «todas» es la ausencia de la clave, no una
    # lista con las tres (una lista vacía también se lee como «todas», pero
    # dejarla escrita hace creer que hay una selección).
    respuesta = client.patch(
        f"/databases/{base['id']}/views/{vista['id']}",
        json={"config": {"sort": {"key": "empresa", "dir": "asc"}}},
    )
    assert respuesta.status_code == 200, respuesta.text
    vistas = vistas_del_md(vault_path)
    assert "visible_properties" not in vistas["Tabla"]
    assert vistas["Tabla"]["sort"] == {"key": "empresa", "dir": "asc"}


def test_renombrar_desde_la_cabecera_traduce_la_lista_de_la_vista(client, vault_path):
    base = crear_base(client)
    fila = crear_fila(
        client,
        base["id"],
        "Acme",
        {"empresa": "Acme", "fase": "lead", "fecha": "2026-11-10"},
    )
    crear_vista(client, base["id"], "Tabla", {"visible_properties": ["empresa", "fecha"]})

    # El menú manda el schema con las claves que había y el nombre nuevo: el
    # mismo PATCH que el diálogo, sin migración propia en el navegador.
    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {"key": "empresa", "name": "Compañía", "type": "text"},
                {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
                {"key": "fecha", "name": "Fecha", "type": "date"},
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    assert [p["key"] for p in respuesta.json()["schema_def"]] == ["compania", "fase", "fecha"]

    # La vista enseña la columna renombrada, no una clave que ya no existe.
    vistas = vistas_del_md(vault_path)
    assert vistas["Tabla"]["visible_properties"] == ["compania", "fecha"]

    # La fila tiene el valor con la clave nueva y sin la vieja.
    datos = fichero_de_fila(vault_path, fila).read_text(encoding="utf-8")
    assert "compania: Acme" in datos
    assert "empresa:" not in datos


def test_borrar_la_columna_deja_los_valores_en_el_md(client, vault_path):
    base = crear_base(client)
    fila = crear_fila(
        client,
        base["id"],
        "Acme",
        {"empresa": "Acme", "fase": "lead", "fecha": "2026-11-10"},
    )
    crear_vista(client, base["id"], "Tabla", {"visible_properties": ["empresa", "fase"]})

    # Borrar la propiedad es mandar el schema sin ella: el servidor la quita de
    # las vistas y NO toca los valores (lo que promete la confirmación).
    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {"key": "empresa", "name": "Empresa", "type": "text"},
                {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    assert [p["key"] for p in respuesta.json()["schema_def"]] == ["empresa", "fase"]
    assert vistas_del_md(vault_path)["Tabla"]["visible_properties"] == ["empresa", "fase"]

    datos = fichero_de_fila(vault_path, fila).read_text(encoding="utf-8")
    assert "fecha: 2026-11-10" in datos, "el valor borrado del schema sigue en el .md"
