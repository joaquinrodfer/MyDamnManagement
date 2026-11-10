"""Atributos legibles: solo el nombre visible y la clave snake_case por detrás.

Lo que fija esta tarjeta:

1. el diálogo pide **un** nombre («Fecha de entrega») y la clave se deriva
   (`fecha_de_entrega`), con sufijo si choca;
2. renombrar una propiedad migra **todas** las filas y las vistas que la
   nombran (`group_by`, `date_property`, `filters`, `sort`,
   `visible_properties`) en la misma operación;
3. borrar una propiedad la quita del schema y de las vistas, sin tocar los
   valores del `.md` de las filas (el fichero es del usuario);
4. la columna del título es una columna más: se renombra (`title_name`) y se
   quita (`title_hidden`), y una base nueva nace sin ninguna propiedad;
5. una base con claves viejas (`proximo_contacto`) sigue abriendo, y solo
   cambia cuando alguien renombra esa propiedad.
"""

from __future__ import annotations

import re

from app.properties import plan_schema, slug_key


def crear_database(client, **cuerpo) -> dict:
    respuesta = client.post("/databases", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id, **cuerpo) -> dict:
    respuesta = client.post(f"/databases/{database_id}/rows", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_vista(client, database_id, **cuerpo) -> dict:
    respuesta = client.post(f"/databases/{database_id}/views", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def texto(path) -> str:
    return path.read_text(encoding="utf-8")


def frontmatter(path) -> dict:
    from app.vault import fm

    raw, _cuerpo = fm.split_document(texto(path))
    assert raw is not None, f"{path} no tiene frontmatter"
    return fm.parse(raw).data


def claves_de_frontmatter(fichero) -> list[str]:
    return re.findall(r"(?m)^([A-Za-z_][A-Za-z0-9_]*):", texto(fichero))


def fichero_de_fila(vault_path, fila, base="Contactos"):
    return vault_path / "Bases" / base / f"{fila['id'][:8]}.md"


# ---------------------------------------------------------------------------
# 1. la clave se deriva del nombre
# ---------------------------------------------------------------------------


def test_la_clave_se_deriva_del_nombre_visible():
    assert slug_key("Fecha de entrega", set()) == "fecha_de_entrega"
    assert slug_key("Número", set()) == "numero"
    assert slug_key("Próximo contacto", set()) == "proximo_contacto"
    assert slug_key("  ¿Ya está?  ", set()) == "ya_esta"
    assert slug_key("···", set()) == "propiedad"
    assert slug_key("1ª opción", set()) == "p_1a_opcion"
    # Dos nombres iguales no comparten clave: sufijo numérico.
    assert slug_key("Fecha", {"fecha"}) == "fecha_2"
    assert slug_key("Fecha", {"fecha", "fecha_2"}) == "fecha_3"


def test_una_propiedad_se_crea_con_solo_el_nombre(client, vault_path):
    base = crear_database(
        client,
        title="Pedidos",
        schema_def=[
            {"name": "Fecha de entrega", "type": "date"},
            {"name": "Fecha de entrega", "type": "text"},
        ],
    )
    assert base["schema_def"] == [
        {"key": "fecha_de_entrega", "name": "Fecha de entrega", "type": "date", "options": None},
        {"key": "fecha_de_entrega_2", "name": "Fecha de entrega", "type": "text", "options": None},
    ]
    datos = frontmatter(vault_path / "Bases" / "Pedidos.md")
    assert [p["key"] for p in datos["schema"]] == ["fecha_de_entrega", "fecha_de_entrega_2"]
    assert [p["name"] for p in datos["schema"]] == ["Fecha de entrega", "Fecha de entrega"]


def test_el_plan_no_normaliza_una_clave_que_nadie_ha_renombrado():
    """Guardar el schema sin cambiar nombres no toca claves viejas."""
    actual = [{"key": "proximo_contacto", "name": "Contacto", "type": "text"}]
    pedido = [{"key": "proximo_contacto", "name": "Contacto", "type": "text"}]
    final, renombres, borradas = plan_schema(actual, pedido)
    assert [p["key"] for p in final] == ["proximo_contacto"]
    assert renombres == {} and borradas == []


def test_renombrar_no_cuenta_como_borrar():
    """La clave vieja de un rename no es una propiedad borrada."""
    actual = [{"key": "fase", "name": "Fase", "type": "text"}]
    final, renombres, borradas = plan_schema(
        actual, [{"key": "fase", "name": "Estado", "type": "text"}]
    )
    assert renombres == {"fase": "estado"} and borradas == []
    assert [p["key"] for p in final] == ["estado"]


# ---------------------------------------------------------------------------
# 2. renombrar migra filas y vistas
# ---------------------------------------------------------------------------

SCHEMA = [
    {"key": "empresa", "name": "Empresa", "type": "text"},
    {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
    {"key": "fecha", "name": "Fecha", "type": "date"},
]


def test_renombrar_migra_las_tres_filas_y_las_tres_vistas(
    client, vault_path, index_path
):
    from test_p2_bases import reindexar_y_comparar_huella

    base = crear_database(client, title="Contactos", schema_def=SCHEMA)
    filas = [
        crear_fila(
            client,
            base["id"],
            title=f"Fila {n}",
            properties={"empresa": f"Empresa {n}", "fase": "lead", "fecha": "2026-11-10"},
        )
        for n in range(1, 4)
    ]
    tablero = crear_vista(
        client,
        base["id"],
        name="Pipeline",
        type="board",
        config={
            "group_by": "fase",
            "filters": [{"key": "fase", "op": "eq", "value": "lead"}],
        },
    )
    calendario = crear_vista(
        client,
        base["id"],
        name="Agenda",
        type="calendar",
        config={"date_property": "fecha"},
    )
    tabla = crear_vista(
        client,
        base["id"],
        name="Ordenada",
        type="table",
        config={
            "sort": {"key": "fecha", "dir": "desc"},
            "visible_properties": ["empresa", "fase", "fecha"],
        },
    )
    assert {tablero["name"], calendario["name"], tabla["name"]} == {
        "Pipeline",
        "Agenda",
        "Ordenada",
    }

    # El diálogo manda la clave que tenía cada fila y el nombre nuevo.
    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {"key": "empresa", "name": "Empresa", "type": "text"},
                {
                    "key": "fase",
                    "name": "Estado",
                    "type": "select",
                    "options": ["lead", "ganado"],
                },
                {"key": "fecha", "name": "Fecha de entrega", "type": "date"},
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    renombrada = respuesta.json()
    assert [p["key"] for p in renombrada["schema_def"]] == [
        "empresa",
        "estado",
        "fecha_de_entrega",
    ]

    # (a) las filas: cada `.md` con la clave nueva y el mismo valor, sin la vieja.
    for fila, numero in zip(filas, range(1, 4)):
        fichero = fichero_de_fila(vault_path, fila)
        claves = claves_de_frontmatter(fichero)
        assert "estado" in claves and "fecha_de_entrega" in claves
        assert "fase" not in claves and "fecha" not in claves
        datos = frontmatter(fichero)
        assert datos["estado"] == "lead"
        assert datos["fecha_de_entrega"] == "2026-11-10"
        assert datos["empresa"] == f"Empresa {numero}"

    # Ni una aparición de las claves viejas en ningún `.md` del vault.
    for fichero in vecindario(vault_path):
        t = texto(fichero)
        assert re.search(r"(?m)^\s*fase\s*:", t) is None, fichero
        assert re.search(r"(?m)^\s*fecha\s*:", t) is None, fichero
        assert re.search(r"(?m)^\s*group_by:\s*fase\s*$", t) is None, fichero
        assert re.search(r"(?m)^\s*date_property:\s*fecha\s*$", t) is None, fichero

    # (b) el frontmatter de la base: schema y vistas.
    datos_base = frontmatter(vault_path / "Bases" / "Contactos.md")
    assert [p["key"] for p in datos_base["schema"]] == [
        "empresa",
        "estado",
        "fecha_de_entrega",
    ]
    vistas = {v["name"]: v for v in datos_base["views"]}
    assert vistas["Pipeline"]["group_by"] == "estado"
    assert vistas["Pipeline"]["filters"] == [{"key": "estado", "op": "eq", "value": "lead"}]
    assert vistas["Agenda"]["date_property"] == "fecha_de_entrega"
    assert vistas["Ordenada"]["sort"] == {"key": "fecha_de_entrega", "dir": "desc"}
    assert vistas["Ordenada"]["visible_properties"] == [
        "empresa",
        "estado",
        "fecha_de_entrega",
    ]

    # (c) la API: los valores están en la clave nueva y el filtro de la vista
    # sigue encontrando las filas. El índice dice lo mismo que el fichero.
    leidas = client.get(f"/databases/{base['id']}/rows").json()
    assert [r["properties"]["estado"] for r in leidas] == ["lead", "lead", "lead"]
    assert all("fase" not in r["properties"] for r in leidas)
    del_tablero = client.get(f"/databases/{base['id']}/rows?view={tablero['id']}").json()
    assert len(del_tablero) == 3
    del_calendario = client.get(f"/databases/{base['id']}/rows?view={calendario['id']}").json()
    assert len(del_calendario) == 3
    reindexar_y_comparar_huella(client, index_path)

    # Y abrir la base después de renombrar no pierde nada.
    reabierta = client.get(f"/databases/{base['id']}").json()
    assert [p["name"] for p in reabierta["schema_def"]] == [
        "Empresa",
        "Estado",
        "Fecha de entrega",
    ]


def vecindario(vault_path):
    return sorted(vault_path.rglob("*.md"))


def test_un_rename_que_no_puede_migrar_una_fila_no_deja_nada_a_medias(client, vault_path):
    """Antes de escribir nada se comprueba todo: mejor un 400 que medio rename."""
    base = crear_database(
        client,
        title="Contactos",
        schema_def=[
            {"key": "empresa", "name": "Empresa", "type": "text"},
            {"key": "nota", "name": "Nota", "type": "text"},
        ],
    )
    buena = crear_fila(client, base["id"], title="Buena", properties={"nota": "hola"})
    rota = crear_fila(client, base["id"], title="Rota", properties={"nota": "adios"})
    fichero_roto = fichero_de_fila(vault_path, rota)
    # Un valor que el parser no interpreta (block scalar): no se puede
    # renombrar sin adivinar el valor, y adivinar es perderlo.
    fichero_roto.write_text(
        f"---\nid: {rota['id']}\ntitle: Rota\nnota: |\n  linea uno\n  linea dos\n---\n",
        encoding="utf-8",
    )
    antes_buena = texto(fichero_de_fila(vault_path, buena))
    antes_rota = texto(fichero_roto)

    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {"key": "empresa", "name": "Empresa", "type": "text"},
                {"key": "nota", "name": "Comentario", "type": "text"},
            ]
        },
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "no es legible" in respuesta.json()["detail"]
    # Ni la fila que sí se podía migrar ni el schema de la base se han tocado.
    assert texto(fichero_de_fila(vault_path, buena)) == antes_buena
    assert texto(fichero_roto) == antes_rota
    assert [p["key"] for p in frontmatter(vault_path / "Bases" / "Contactos.md")["schema"]] == [
        "empresa",
        "nota",
    ]


# ---------------------------------------------------------------------------
# 3. borrar una propiedad
# ---------------------------------------------------------------------------


def test_borrar_una_propiedad_la_quita_del_schema_y_de_las_vistas(client, vault_path):
    base = crear_database(client, title="Contactos", schema_def=SCHEMA)
    fila = crear_fila(
        client,
        base["id"],
        title="Una",
        properties={"empresa": "Acme", "fase": "lead", "fecha": "2026-11-10"},
    )
    tablero = crear_vista(
        client,
        base["id"],
        name="Pipeline",
        type="board",
        config={
            "group_by": "fase",
            "filters": [{"key": "fase", "op": "eq", "value": "lead"}],
        },
    )

    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {"key": "empresa", "name": "Empresa", "type": "text"},
                {"key": "fecha", "name": "Fecha", "type": "date"},
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    assert [p["key"] for p in respuesta.json()["schema_def"]] == ["empresa", "fecha"]

    # La vista que agrupaba y filtraba por la propiedad borrada se queda sin
    # esas referencias (sigue abriendo), no rota.
    vistas = client.get(f"/databases/{base['id']}/views").json()
    assert vistas[0]["config"] == {}

    # Los valores del `.md` de la fila NO se borran: el fichero es del usuario.
    datos = frontmatter(fichero_de_fila(vault_path, fila))
    assert datos["fase"] == "lead"

    # Y la fila sigue abriendo y editándose por lo que queda en el schema (el
    # índice ya no ofrece la propiedad borrada, porque el schema manda).
    leidas = client.get(f"/databases/{base['id']}/rows").json()
    assert "fase" not in leidas[0]["properties"]
    assert leidas[0]["properties"]["empresa"] == "Acme"
    guardada = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}", json={"properties": {"empresa": "Acme 2"}}
    )
    assert guardada.status_code == 200, guardada.text


# ---------------------------------------------------------------------------
# 4. la columna del título
# ---------------------------------------------------------------------------


def test_la_columna_del_titulo_se_renombra_y_se_quita(client, vault_path, index_path):
    from test_p2_bases import reindexar_y_comparar_huella

    base = crear_database(client, title="Pedidos", schema_def=[])
    assert base["title_name"] == "Título"
    assert base["title_hidden"] is False
    fichero = vault_path / "Bases" / "Pedidos.md"
    assert "title_name" not in texto(fichero)

    renombrada = client.patch(f"/databases/{base['id']}", json={"title_name": "Pedido"})
    assert renombrada.status_code == 200, renombrada.text
    assert renombrada.json()["title_name"] == "Pedido"
    assert frontmatter(fichero)["title_name"] == "Pedido"
    reindexar_y_comparar_huella(client, index_path)

    quitada = client.patch(f"/databases/{base['id']}", json={"title_hidden": True})
    assert quitada.status_code == 200, quitada.text
    assert quitada.json()["title_hidden"] is True
    # El parser del frontmatter no infiere tipos: el booleano se escribe `true`.
    assert frontmatter(fichero)["title_hidden"] in (True, "true")
    reindexar_y_comparar_huella(client, index_path)

    # Volver a lo de siempre quita las claves del `.md` (no deja ruido).
    vuelta = client.patch(
        f"/databases/{base['id']}", json={"title_name": "Título", "title_hidden": False}
    )
    assert vuelta.json()["title_name"] == "Título"
    assert vuelta.json()["title_hidden"] is False
    claves = claves_de_frontmatter(fichero)
    assert "title_name" not in claves and "title_hidden" not in claves
    reindexar_y_comparar_huella(client, index_path)


def test_una_base_nueva_nace_sin_propiedades(client, vault_path):
    base = crear_database(client, title="Nueva", schema_def=[])
    assert base["schema_def"] == []
    datos = frontmatter(vault_path / "Bases" / "Nueva.md")
    assert datos["schema"] == []
    assert "title_name" not in datos and "title_hidden" not in datos


# ---------------------------------------------------------------------------
# 5. compatibilidad con claves viejas
# ---------------------------------------------------------------------------


def test_una_base_con_clave_rara_sigue_abriendo_y_se_migra_al_renombrarla(client, vault_path):
    carpeta = vault_path / "Bases"
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / "Contactos.md").write_text(
        "---\n"
        "id: 9b1d2c3e-0000-4000-8000-000000000001\n"
        "type: database\n"
        'icon: "🗄"\n'
        "schema:\n"
        "  - key: proximo_contacto\n"
        "    name: Próximo contacto\n"
        "    type: date\n"
        "views: []\n"
        "---\n",
        encoding="utf-8",
    )
    (carpeta / "Contactos").mkdir(exist_ok=True)
    (carpeta / "Contactos" / "3a7f0000.md").write_text(
        "---\nid: 3a7f0000-0000-4000-8000-000000000002\n"
        "title: Acme\nproximo_contacto: 2026-11-20\n---\n",
        encoding="utf-8",
    )
    assert client.post("/reindex").status_code == 200
    base = client.get("/databases").json()[0]
    assert [p["key"] for p in base["schema_def"]] == ["proximo_contacto"]

    # Guardar el schema sin renombrar la propiedad no la toca (compatibilidad).
    igual = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {"key": "proximo_contacto", "name": "Próximo contacto", "type": "date"}
            ]
        },
    )
    assert igual.status_code == 200
    assert "proximo_contacto" in texto(carpeta / "Contactos" / "3a7f0000.md")

    # Renombrarla de verdad sí migra la fila.
    cambio = client.patch(
        f"/databases/{base['id']}",
        json={"schema_def": [{"key": "proximo_contacto", "name": "Siguiente", "type": "date"}]},
    )
    assert cambio.status_code == 200, cambio.text
    assert [p["key"] for p in cambio.json()["schema_def"]] == ["siguiente"]
    datos = frontmatter(carpeta / "Contactos" / "3a7f0000.md")
    assert datos["siguiente"] == "2026-11-20"
    assert "proximo_contacto" not in claves_de_frontmatter(carpeta / "Contactos" / "3a7f0000.md")
