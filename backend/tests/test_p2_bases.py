"""P2: las bases de datos y sus filas viven en ficheros del vault.

Cada test corre sobre un vault vacío en un directorio temporal (`vault_path`) y
un índice propio, como el resto de la suite. Lo que se fija aquí es el criterio
de hecho de la tarjeta:

1. crear una base desde la plantilla CRM escribe `Bases/<slug>.md` con su
   `schema` y sus `views`, y su carpeta de filas;
2. editar una propiedad de una fila cambia el `.md` de la fila (y no escribe
   `title`: el título es el nombre del fichero);
3. renombrar una fila cambia el nombre del fichero y conserva su `id`;
4. borrar el índice y arrancar reconstruye bases, filas y vistas con la misma
   huella de volcado, tabla a tabla;
5. un `.md` de fila huérfano se avisa y no tumba el reindexado.
"""

from pathlib import Path

from app import index as indice

CRM_SCHEMA = [
    {"key": "empresa", "name": "Empresa", "type": "text"},
    {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
    {"key": "valor", "name": "Valor", "type": "number"},
]


def crear_database(client, **cuerpo) -> dict:
    respuesta = client.post("/databases", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id, **cuerpo) -> dict:
    respuesta = client.post(f"/databases/{database_id}/rows", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def texto(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def frontmatter(path: Path) -> dict:
    """El bloque YAML del fichero, con el parser de la app (sin dependencias)."""
    from app.vault import fm

    raw, _cuerpo = fm.split_document(texto(path))
    assert raw is not None, f"{path} no tiene frontmatter"
    return fm.parse(raw).data


def arrancar_de_nuevo():
    """Un `TestClient` nuevo = un arranque nuevo de la app (evento startup)."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def huella(index_path: Path) -> tuple[str, dict]:
    return indice.dump_hash(str(index_path))


def reindexar_y_comparar_huella(client, index_path: Path) -> None:
    """Nada vive solo en el índice: reindexar no cambia ni una fila."""
    antes_hash, antes_cuentas = huella(index_path)
    respuesta = client.post("/reindex")
    assert respuesta.status_code == 200, respuesta.text
    despues_hash, despues_cuentas = huella(index_path)
    assert despues_cuentas == antes_cuentas, (
        "el índice dice cosas que los ficheros no dicen: "
        f"{antes_cuentas} != {despues_cuentas}"
    )
    assert despues_hash == antes_hash, (
        "el índice reconstruido no es idéntico al escrito por la API"
    )


# --------------------------------------------------------------------------
# 1. la base es un fichero, y crear desde plantilla lo escribe
# --------------------------------------------------------------------------


def test_crear_una_base_desde_la_plantilla_escribe_sus_ficheros(client, index_path, vault_path):
    base = client.post("/databases/from-template", json={"template": "crm"}).json()

    fichero = vault_path / "Bases" / "Contactos.md"
    assert fichero.exists(), "crear una base tiene que crear su .md"
    assert (vault_path / "Bases" / "Contactos").is_dir(), "falta la carpeta de filas"

    datos = frontmatter(fichero)
    assert datos["type"] == "database"
    assert datos["id"] == base["page_id"] == base["id"]
    assert [p["key"] for p in datos["schema"]] == [
        "empresa", "fase", "valor", "proximo_contacto",
    ]
    # Las opciones de un select viajan con el fichero.
    assert datos["schema"][1]["options"] == [
        "lead", "contactado", "propuesta", "ganado", "perdido",
    ]
    # Las vistas van en el frontmatter, con su id y su `group_by` al mismo nivel
    # (ROADMAP §2.3).
    assert [v["name"] for v in datos["views"]] == ["Pipeline", "Tabla"]
    assert datos["views"][0]["type"] == "board"
    assert datos["views"][0]["group_by"] == "fase"

    vistas = client.get(f"/databases/{base['id']}/views").json()
    assert {v["id"] for v in vistas} == {v["id"] for v in datos["views"]}
    assert {v["name"]: v["config"] for v in vistas}["Pipeline"] == {"group_by": "fase"}

    # Los temporales de la escritura atómica no se quedan colgando.
    assert list((vault_path / "Bases").glob("*.tmp-*")) == []
    assert list((vault_path / "Bases").glob(".*.tmp-*")) == []

    reindexar_y_comparar_huella(client, index_path)


def test_el_schema_y_las_vistas_se_editan_en_el_fichero(client, vault_path):
    base = crear_database(client, title="Contactos", schema_def=CRM_SCHEMA)
    fichero = vault_path / "Bases" / "Contactos.md"

    nuevo_schema = CRM_SCHEMA + [{"key": "nota", "name": "Nota", "type": "text"}]
    assert client.patch(
        f"/databases/{base['id']}", json={"schema_def": nuevo_schema}
    ).status_code == 200
    assert [p["key"] for p in frontmatter(fichero)["schema"]][-1] == "nota"

    vista = client.post(
        f"/databases/{base['id']}/views",
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
    datos = frontmatter(fichero)
    assert datos["views"][-1]["id"] == vista["id"]
    assert datos["views"][-1]["filters"] == [{"key": "fase", "op": "eq", "value": "ganado"}]
    assert datos["views"][-1]["sort"] == {"key": "valor", "dir": "desc"}

    client.patch(f"/databases/{base['id']}/views/{vista['id']}", json={"name": "Cerradas"})
    assert frontmatter(fichero)["views"][-1]["name"] == "Cerradas"

    client.delete(f"/databases/{base['id']}/views/{vista['id']}")
    assert [v["name"] for v in frontmatter(fichero)["views"]] == []


# --------------------------------------------------------------------------
# 2. las filas son ficheros con las propiedades en el frontmatter
# --------------------------------------------------------------------------


def test_crear_una_fila_escribe_su_fichero_sin_title(client, vault_path):
    base = crear_database(client, title="Contactos", schema_def=CRM_SCHEMA)
    fila = crear_fila(
        client, base["id"], title="Acme", properties={"empresa": "Acme S.L.", "fase": "lead"}
    )

    fichero = vault_path / "Bases" / "Contactos" / "Acme.md"
    assert fichero.exists()

    datos = frontmatter(fichero)
    assert datos["id"] == fila["id"]
    assert datos["empresa"] == "Acme S.L."
    assert datos["fase"] == "lead"
    assert "title" not in datos, "el título de una fila es el nombre del fichero"

    # Las propiedades sin valor no se escriben (`valor` y `proximo_contacto` no
    # llegaron): `null` volvería del parser como la cadena "null".
    assert "valor" not in datos and "proximo_contacto" not in datos
    assert "null" not in texto(fichero)


def test_editar_una_propiedad_cambia_el_md(client, vault_path):
    base = crear_database(client, title="Contactos", schema_def=CRM_SCHEMA)
    fila = crear_fila(
        client,
        base["id"],
        title="Acme",
        properties={"empresa": "Acme S.L.", "fase": "lead", "valor": 1200},
    )
    fichero = vault_path / "Bases" / "Contactos" / "Acme.md"
    assert frontmatter(fichero)["valor"] == "1200"

    editada = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"fase": "ganado", "valor": 1500.0}},
    ).json()
    assert editada["properties"] == {
        "empresa": "Acme S.L.", "fase": "ganado", "valor": 1500.0,
    }

    datos = frontmatter(fichero)
    assert datos["fase"] == "ganado"
    assert datos["valor"] == "1500"
    assert datos["empresa"] == "Acme S.L.", "un PATCH parcial no borra lo que no llega"
    assert "title" not in datos

    # Y vaciar una propiedad la quita del fichero, no la deja a `null`.
    client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}", json={"properties": {"valor": None}}
    )
    assert "valor" not in frontmatter(fichero)


def test_renombrar_una_fila_cambia_el_fichero_y_conserva_el_id(client, vault_path):
    base = crear_database(client, title="Contactos", schema_def=CRM_SCHEMA)
    fila = crear_fila(client, base["id"], title="Acme", properties={"fase": "lead"})
    carpeta = vault_path / "Bases" / "Contactos"

    renombrada = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}", json={"title": "Acme (cerrada)"}
    ).json()

    assert not (carpeta / "Acme.md").exists()
    nuevo = carpeta / "Acme (cerrada).md"
    assert nuevo.exists()
    assert renombrada["id"] == fila["id"], "renombrar no cambia la identidad"
    assert renombrada["database_id"] == base["id"]
    datos = frontmatter(nuevo)
    assert datos["id"] == fila["id"]
    assert datos["fase"] == "lead", "las propiedades no se pierden al renombrar"


def test_renombrar_la_base_renombra_su_carpeta_de_filas(client, vault_path):
    base = crear_database(client, title="Contactos", schema_def=CRM_SCHEMA)
    fila = crear_fila(client, base["id"], title="Acme", properties={"fase": "lead"})

    client.patch(f"/databases/{base['id']}", json={"title": "Clientes"})

    assert (vault_path / "Bases" / "Clientes.md").exists()
    assert (vault_path / "Bases" / "Clientes" / "Acme.md").exists()
    assert not (vault_path / "Bases" / "Contactos.md").exists()
    assert not (vault_path / "Bases" / "Contactos").exists()

    # El id de la fila no se toca y la base sigue sirviendo sus filas.
    filas = client.get(f"/databases/{base['id']}/rows").json()
    assert [(f["id"], f["title"]) for f in filas] == [(fila["id"], "Acme")]


# --------------------------------------------------------------------------
# 3. índice derivado: borrarlo y arrancar deja lo mismo, tabla a tabla
# --------------------------------------------------------------------------


def test_borrar_el_indice_reconstruye_bases_filas_y_vistas_igual(client, index_path):
    base = client.post("/databases/from-template", json={"template": "crm"}).json()
    for titulo, props in (
        ("Acme", {"empresa": "Acme S.L.", "fase": "lead", "valor": 12000}),
        ("Globex", {"empresa": "Globex", "fase": "ganado", "valor": 500}),
        ("Initech", {"empresa": "Initech", "fase": "contactado"}),
    ):
        crear_fila(client, base["id"], title=titulo, properties=props)
    # Una edición y una vista más: lo que escribe el usuario también se rehace.
    filas = client.get(f"/databases/{base['id']}/rows").json()
    client.patch(
        f"/databases/{base['id']}/rows/{filas[0]['id']}", json={"properties": {"fase": "ganado"}}
    )
    client.post(
        f"/databases/{base['id']}/views",
        json={"name": "Ganadas", "type": "board", "config": {"group_by": "fase"}},
    )

    antes_hash, antes_cuentas = huella(index_path)
    assert antes_cuentas["databases"] == 1
    assert antes_cuentas["rows"] == 3
    assert antes_cuentas["views"] == 3
    assert antes_cuentas["pages"] == 4          # la base + tres filas

    for suffix in ("", "-wal", "-shm"):
        Path(str(index_path) + suffix).unlink(missing_ok=True)

    with arrancar_de_nuevo() as otro:
        assert otro.get(f"/databases/{base['id']}").status_code == 200
        assert len(otro.get(f"/databases/{base['id']}/rows").json()) == 3
        assert [v["name"] for v in otro.get(f"/databases/{base['id']}/views").json()] == [
            "Pipeline", "Tabla", "Ganadas",
        ]

    despues_hash, despues_cuentas = huella(index_path)
    assert despues_cuentas == antes_cuentas, "huella distinta tabla a tabla"
    assert despues_hash == antes_hash, "el índice reconstruido no es idéntico"


def test_ningun_endpoint_de_bases_escribe_solo_en_el_indice(client, index_path, vault_path):
    """Cada escritura deja el índice reconstruible desde los ficheros.

    Se comprueba después de CADA paso: si un endpoint escribiera solo en el
    índice, el reindexado lo borraría y la huella cambiaría.
    """
    base = crear_database(client, title="Tareas", schema_def=CRM_SCHEMA)
    reindexar_y_comparar_huella(client, index_path)

    fila = crear_fila(client, base["id"], title="Una", properties={"fase": "lead"})
    reindexar_y_comparar_huella(client, index_path)

    client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}", json={"properties": {"fase": "ganado"}}
    )
    reindexar_y_comparar_huella(client, index_path)

    client.patch(f"/databases/{base['id']}/rows/{fila['id']}", json={"title": "Otra"})
    reindexar_y_comparar_huella(client, index_path)

    vista = client.post(
        f"/databases/{base['id']}/views", json={"name": "Tablero", "type": "board"}
    ).json()
    reindexar_y_comparar_huella(client, index_path)

    client.patch(f"/databases/{base['id']}/views/{vista['id']}", json={"name": "Tablero 2"})
    reindexar_y_comparar_huella(client, index_path)

    client.patch(
        f"/databases/{base['id']}", json={"schema_def": CRM_SCHEMA, "title": "Tareas 2026"}
    )
    reindexar_y_comparar_huella(client, index_path)

    client.delete(f"/databases/{base['id']}/rows/{fila['id']}")
    reindexar_y_comparar_huella(client, index_path)

    # Y la cabecera de la base también vive en su frontmatter.
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    subida = client.post(
        f"/pages/{base['page_id']}/header-image",
        files={"file": ("cabecera.png", png, "image/png")},
    )
    assert subida.status_code == 200, subida.text
    assert "header_image" in texto(vault_path / "Bases" / "Tareas 2026.md")
    reindexar_y_comparar_huella(client, index_path)


# --------------------------------------------------------------------------
# 4. papelera y filas huérfanas
# --------------------------------------------------------------------------


def test_borrar_una_base_manda_sus_ficheros_a_la_papelera(client, vault_path):
    base = crear_database(client, title="Contactos", schema_def=CRM_SCHEMA)
    crear_fila(client, base["id"], title="Acme", properties={"fase": "lead"})

    assert client.delete(f"/databases/{base['id']}").status_code == 204

    assert not (vault_path / "Bases" / "Contactos.md").exists()
    assert not (vault_path / "Bases" / "Contactos").exists()
    assert client.get(f"/databases/{base['id']}").status_code == 404
    assert client.get("/pages").json() == []

    entradas = client.get("/trash").json()
    rutas = {e["original_path"] for e in entradas}
    assert rutas == {"Bases/Contactos.md", "Bases/Contactos/Acme.md"}

    # Y se puede volver a su sitio: la base sola, sin su fila.
    base_en_papelera = next(e for e in entradas if e["original_path"] == "Bases/Contactos.md")
    restaurada = client.post("/trash/restore", json={"path": base_en_papelera["path"]})
    assert restaurada.status_code == 200, restaurada.text
    assert (vault_path / "Bases" / "Contactos.md").exists()
    assert client.get(f"/databases/{base['id']}").status_code == 200
    assert client.get(f"/databases/{base['id']}/rows").json() == []


def test_borrar_una_fila_solo_mueve_su_fichero(client, vault_path):
    base = crear_database(client, title="Contactos", schema_def=CRM_SCHEMA)
    fila = crear_fila(client, base["id"], title="Acme", properties={"fase": "lead"})
    crear_fila(client, base["id"], title="Globex", properties={"fase": "lead"})

    assert client.delete(f"/databases/{base['id']}/rows/{fila['id']}").status_code == 204

    assert not (vault_path / "Bases" / "Contactos" / "Acme.md").exists()
    assert (vault_path / "Bases" / "Contactos" / "Globex.md").exists()
    assert [f["title"] for f in client.get(f"/databases/{base['id']}/rows").json()] == ["Globex"]
    assert {e["original_path"] for e in client.get("/trash").json()} == {
        "Bases/Contactos/Acme.md"
    }


def test_una_fila_huerfana_se_avisa_y_no_aborta_el_reindexado(client, vault_path):
    """Su `Bases/<slug>.md` no existe: el fichero manda, pero se dice."""
    client.post("/pages", json={"title": "Normal"})
    huerfana = vault_path / "Bases" / "Fantasma" / "Suelta.md"
    huerfana.parent.mkdir(parents=True)
    huerfana.write_text(
        "---\nid: 11111111-2222-3333-4444-555555555555\nfase: lead\n---\n\nsuelta\n",
        encoding="utf-8",
    )

    respuesta = client.post("/reindex")
    assert respuesta.status_code == 200, respuesta.text
    avisos = " ".join(respuesta.json()["warning_sample"])
    assert "fila huérfana" in avisos
    assert "Bases/Fantasma" in avisos

    # No se pierde: se indexa como nota (el fichero manda) y el fichero sigue.
    assert huerfana.exists()
    titulos = {p["title"] for p in client.get("/pages").json()}
    assert titulos == {"Normal", "Suelta"}
    assert client.get("/status").json()["drift"]["changed"] == 0


# --------------------------------------------------------------------------
# 5. errores del vault son 400, y una base también puede nacer por /pages
# --------------------------------------------------------------------------


def test_si_el_fichero_de_la_base_desaparece_es_400_y_no_500(client, vault_path):
    """El índice dice que hay base; el disco no. Se dice qué hacer, no 500."""
    client.post("/pages", json={"title": "Otra cosa"})     # el vault no se queda vacío
    base = crear_database(client, title="Contactos", schema_def=CRM_SCHEMA)
    (vault_path / "Bases" / "Contactos.md").unlink()

    fila = client.post(f"/databases/{base['id']}/rows", json={"title": "Acme"})
    assert fila.status_code == 400, fila.text
    assert "no está en el vault" in fila.json()["detail"]

    edicion = client.patch(f"/databases/{base['id']}", json={"title": "Otra"})
    assert edicion.status_code == 400, edicion.text

    # Y se arregla reindexando: la base era un fichero y ya no está.
    assert client.post("/reindex").status_code == 200
    assert client.get(f"/databases/{base['id']}").status_code == 404


def test_una_base_tambien_nace_por_post_pages(client, vault_path):
    """`POST /pages` con `type: database` escribe la base, no una página suelta."""
    page = client.post("/pages", json={"title": "Suelta", "type": "database"})
    assert page.status_code == 200, page.text
    fichero = vault_path / "Bases" / "Suelta.md"
    assert fichero.exists()
    assert "type: database" in texto(fichero)

    base = client.get(f"/databases/{page.json()['id']}")
    assert base.status_code == 200
    assert base.json()["schema_def"] == []
    assert base.json()["page_id"] == page.json()["id"]


def test_una_fila_no_se_crea_suelta(client):
    respuesta = client.post("/pages", json={"title": "Huerfana", "type": "database_row"})
    assert respuesta.status_code == 400, respuesta.text
    assert "nace en su base" in respuesta.json()["detail"]


# --------------------------------------------------------------------------
# 6. los ficheros escritos a mano también son bases
# --------------------------------------------------------------------------

def test_una_base_escrita_a_mano_se_indexa_con_su_schema_y_sus_vistas(client, vault_path):
    """El formato del ADR se lee sin la app: `id: v1` no es UUID y aun así vale."""
    (vault_path / "Bases").mkdir(exist_ok=True)
    (vault_path / "Bases" / "Contactos.md").write_text(
        "---\n"
        "id: 9b1d2c3e-0000-4000-8000-000000000001\n"
        "type: database\n"
        "icon: \"🗄\"\n"
        "schema:\n"
        "  - key: empresa\n"
        "    name: Empresa\n"
        "    type: text\n"
        "  - key: fase\n"
        "    name: Fase\n"
        "    type: select\n"
        "    options: [Lead, Negociación, Cerrada]\n"
        "views:\n"
        "  - id: v1\n"
        "    name: Tablero\n"
        "    type: board\n"
        "    group_by: fase\n"
        "---\n",
        encoding="utf-8",
    )
    (vault_path / "Bases" / "Contactos").mkdir(exist_ok=True)
    (vault_path / "Bases" / "Contactos" / "Acme.md").write_text(
        "---\nid: 3a7f0000-0000-4000-8000-000000000002\nempresa: Acme S.L.\nfase: Negociación\n---\n\n"
        "Notas libres de la fila.\n",
        encoding="utf-8",
    )

    assert client.post("/reindex").status_code == 200

    bases = client.get("/databases").json()
    assert [(b["title"], b["icon"]) for b in bases] == [("Contactos", "🗄")]
    assert [p["key"] for p in bases[0]["schema_def"]] == ["empresa", "fase"]

    vistas = client.get(f"/databases/{bases[0]['id']}/views").json()
    assert [(v["name"], v["type"], v["config"]) for v in vistas] == [
        ("Tablero", "board", {"group_by": "fase"}),
    ]

    filas = client.get(f"/databases/{bases[0]['id']}/rows").json()
    assert [(f["title"], f["properties"]) for f in filas] == [
        ("Acme", {"empresa": "Acme S.L.", "fase": "Negociación"}),
    ]
    assert filas[0]["created_at"] is not None
