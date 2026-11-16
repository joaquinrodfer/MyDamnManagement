"""Subtareas: una fila puede ser hija de otra fila de su misma base.

Lo que se fija aquí, que es lo que la tarjeta pide:

1. la hija guarda el **id** de su madre en el frontmatter de su `.md` (no su
   título) y la API devuelve `parent_id`;
2. el contador de la madre («1 de 3») cuenta sus hijas DIRECTAS y las cerradas,
   con la regla de `done` que declara el schema;
3. los ciclos se rechazan con un 400 que se entiende: ella misma, hija de su
   hija, y un ciclo de tres;
4. el reindexado reconstruye la jerarquía desde los ficheros;
5. el nivel de la vista (`parent_level`) se guarda en el `.md` y decide si se
   ven solo las madres o todas, con las hijas detrás de su madre;
6. la fila abierta como página trae sus subtareas, y el grafo dibuja la arista
   madre -> hija sin escribir nada en `links`.
"""

from pathlib import Path

from app.vault import fm

SCHEMA = [
    {"key": "paso_de", "name": "Subtarea de", "type": "parent_row"},
    {
        "key": "estado",
        "name": "Estado",
        "type": "select",
        "options": ["Pendiente", "En curso", "Hecho"],
        "done": ["Hecho"],
    },
    {"key": "fecha", "name": "Fecha", "type": "date"},
]


def crear_base(client, schema=None, title="Plan"):
    respuesta = client.post(
        "/databases", json={"title": title, "schema_def": schema or SCHEMA}
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id, title, properties=None):
    respuesta = client.post(
        f"/databases/{database_id}/rows",
        json={"title": title, "properties": properties or {}},
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def parchear_fila(client, database_id, row_id, properties):
    return client.patch(
        f"/databases/{database_id}/rows/{row_id}", json={"properties": properties}
    )


def crear_vista(client, database_id, nombre, config=None, tipo="table"):
    respuesta = client.post(
        f"/databases/{database_id}/views",
        json={"name": nombre, "type": tipo, "config": config or {}},
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def filas(client, database_id, vista=None):
    url = f"/databases/{database_id}/rows"
    if vista:
        url += f"?view={vista}"
    respuesta = client.get(url)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def md_de_fila(vault_path: Path, base="Plan", row_id: str = "") -> str:
    """El `.md` de una fila: `Bases/<base>/<id8>.md`."""
    carpeta = vault_path / "Bases" / base
    ficheros = list(carpeta.glob(f"{row_id[:8]}*.md"))
    assert len(ficheros) == 1, [f.name for f in carpeta.glob("*.md")]
    return ficheros[0].read_text(encoding="utf-8")


def plan_con_hijas(client):
    """Una madre con tres subtareas; la tercera está cerrada. Devuelve el mapa."""
    base = crear_base(client)
    madre = crear_fila(client, base["id"], "Lanzar la web")
    hija = crear_fila(client, base["id"], "Textos", {"paso_de": madre["id"]})
    otra = crear_fila(
        client,
        base["id"],
        "Diseño",
        {"paso_de": madre["id"], "estado": "En curso", "fecha": "2026-12-01"},
    )
    cerrada = crear_fila(
        client,
        base["id"],
        "Dominio",
        {"paso_de": madre["id"], "estado": "Hecho"},
    )
    return {
        "base": base,
        "madre": madre,
        "hija": hija,
        "otra": otra,
        "cerrada": cerrada,
    }


# ------------------------------------------------------------- la celda

def test_la_hija_guarda_el_id_de_su_madre_en_el_frontmatter(client, vault_path):
    plan = plan_con_hijas(client)
    texto = md_de_fila(vault_path, row_id=plan["hija"]["id"])
    assert f"paso_de: {plan['madre']['id']}" in texto
    # El id, no el título: renombrar la madre no puede romper el vínculo.
    assert "paso_de: Lanzar la web" not in texto

    leida = client.get(
        f"/databases/{plan['base']['id']}/rows/{plan['hija']['id']}"
    ).json()
    assert leida["parent_id"] == plan["madre"]["id"]
    assert leida["properties"]["paso_de"] == plan["madre"]["id"]


def test_cambiar_el_tipo_del_estado_suelta_lo_que_marcaba_cerrada(client):
    plan = plan_con_hijas(client)
    base = plan["base"]["id"]
    respuesta = client.patch(
        f"/databases/{base}",
        json={
            "schema_def": [
                {"key": "paso_de", "name": "Subtarea de", "type": "parent_row"},
                {"key": "estado", "name": "Estado", "type": "text"},
                {"key": "fecha", "name": "Fecha", "type": "date"},
            ]
        },
    )
    # Guardar la base no puede fallar porque el marcador ya no quepa: se suelta.
    assert respuesta.status_code == 200, respuesta.text
    assert all("done" not in p for p in respuesta.json()["schema_def"])
    leidas = {r["id"]: r for r in filas(client, base)}
    assert leidas[plan["madre"]["id"]]["children"] == {"total": 3, "closed": 0}


def test_guardar_la_base_conserva_el_marcador_done(client, vault_path):
    plan = plan_con_hijas(client)
    base = plan["base"]["id"]
    respuesta = client.patch(
        f"/databases/{base}",
        json={
            "schema_def": [
                {"key": "paso_de", "name": "Subtarea de", "type": "parent_row"},
                {
                    "key": "estado",
                    "name": "Estado",
                    "type": "select",
                    "options": ["Pendiente", "En curso", "Hecho"],
                },
                {"key": "fecha", "name": "Fecha", "type": "date"},
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    estado = next(p for p in respuesta.json()["schema_def"] if p["key"] == "estado")
    assert estado["done"] == ["Hecho"]
    assert "done: [Hecho]" in (vault_path / "Bases" / "Plan.md").read_text(encoding="utf-8")
    # Y el contador sigue contando la cerrada.
    leidas = {r["id"]: r for r in filas(client, base)}
    assert leidas[plan["madre"]["id"]]["children"] == {"total": 3, "closed": 1}


def test_un_done_imposible_se_rechaza_con_el_motivo(client):
    respuesta = client.post(
        "/databases",
        json={
            "title": "Marcador imposible",
            "schema_def": [
                {"key": "nota", "name": "Nota", "type": "text", "done": True},
            ],
        },
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "solo tiene sentido en una casilla o una selección" in respuesta.json()["detail"]

    respuesta = client.post(
        "/databases",
        json={
            "title": "Selección sin lista",
            "schema_def": [
                {
                    "key": "estado",
                    "name": "Estado",
                    "type": "select",
                    "options": ["Sí", "No"],
                    "done": True,
                },
            ],
        },
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "la lista de valores" in respuesta.json()["detail"]


def test_la_madre_tiene_que_ser_de_la_misma_base(client):
    plan = plan_con_hijas(client)
    otra_base = crear_base(client, title="Otra")
    ajena = crear_fila(client, otra_base["id"], "De otra base")

    respuesta = parchear_fila(
        client, plan["base"]["id"], plan["hija"]["id"], {"paso_de": ajena["id"]}
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "no es de esta base" in respuesta.json()["detail"]

    # Un id que no existe tampoco vale, y el mensaje lo dice con el nombre
    # visible de la propiedad, no con su clave.
    respuesta = parchear_fila(
        client,
        plan["base"]["id"],
        plan["hija"]["id"],
        {"paso_de": "7a1b4c5d-0000-4000-8000-000000000000"},
    )
    assert respuesta.status_code == 400
    assert "«Subtarea de»" in respuesta.json()["detail"]


def test_quitar_la_madre_devuelve_la_fila_a_la_raiz(client, vault_path):
    plan = plan_con_hijas(client)
    respuesta = parchear_fila(client, plan["base"]["id"], plan["hija"]["id"], {"paso_de": None})
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["parent_id"] is None
    # La celda vacía no se escribe en el `.md` (como cualquier celda vacía).
    assert "paso_de" not in md_de_fila(vault_path, row_id=plan["hija"]["id"])
    leidas = {r["id"]: r for r in filas(client, plan["base"]["id"])}
    assert leidas[plan["madre"]["id"]]["children"] == {"total": 2, "closed": 1}


def test_una_celda_de_subtarea_no_admite_listas_ni_titulos(client):
    plan = plan_con_hijas(client)
    respuesta = parchear_fila(
        client, plan["base"]["id"], plan["hija"]["id"], {"paso_de": [plan["madre"]["id"]]}
    )
    assert respuesta.status_code == 400
    assert "UNA fila madre" in respuesta.json()["detail"]

    respuesta = parchear_fila(
        client, plan["base"]["id"], plan["hija"]["id"], {"paso_de": plan["madre"]["title"]}
    )
    assert respuesta.status_code == 400
    assert "no es el id de una fila" in respuesta.json()["detail"]


def test_una_base_solo_puede_tener_una_propiedad_de_subtareas(client):
    respuesta = client.post(
        "/databases",
        json={
            "title": "Dos jerarquías",
            "schema_def": [
                {"key": "paso_de", "name": "Subtarea de", "type": "parent_row"},
                {"key": "parte_de", "name": "Parte de", "type": "parent_row"},
            ],
        },
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "una propiedad de subtareas" in respuesta.json()["detail"]


# ------------------------------------------------------------- ciclos

def test_una_fila_no_puede_ser_su_propia_madre(client):
    plan = plan_con_hijas(client)
    respuesta = parchear_fila(
        client, plan["base"]["id"], plan["madre"]["id"], {"paso_de": plan["madre"]["id"]}
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "su propia madre" in respuesta.json()["detail"]


def test_una_madre_no_puede_colgar_de_su_hija(client):
    plan = plan_con_hijas(client)
    respuesta = parchear_fila(
        client, plan["base"]["id"], plan["madre"]["id"], {"paso_de": plan["hija"]["id"]}
    )
    assert respuesta.status_code == 400, respuesta.text
    detalle = respuesta.json()["detail"]
    assert "ciclo" in detalle
    assert "Lanzar la web" in detalle and "Textos" in detalle


def test_un_ciclo_de_tres_se_rechaza(client):
    plan = plan_con_hijas(client)
    base = plan["base"]["id"]
    nieta = crear_fila(client, base, "Nieta", {"paso_de": plan["hija"]["id"]})

    # madre -> hija -> nieta; colgar la madre de la nieta cerraría el círculo.
    respuesta = parchear_fila(client, base, plan["madre"]["id"], {"paso_de": nieta["id"]})
    assert respuesta.status_code == 400, respuesta.text
    detalle = respuesta.json()["detail"]
    assert "ciclo" in detalle
    # El mensaje dice POR DÓNDE vuelve el círculo, con títulos y en orden.
    assert "Nieta → Textos → Lanzar la web" in detalle

    # Y la jerarquía sigue como estaba: nada se escribió a medias.
    leida = client.get(f"/databases/{base}/rows/{plan['madre']['id']}").json()
    assert leida["parent_id"] is None
    nieta_leida = client.get(f"/databases/{base}/rows/{nieta['id']}").json()
    assert nieta_leida["parent_id"] == plan["hija"]["id"]


# ------------------------------------------------------------- contador

def test_la_madre_cuenta_sus_hijas_y_las_cerradas(client):
    plan = plan_con_hijas(client)
    leidas = {r["id"]: r for r in filas(client, plan["base"]["id"])}

    madre = leidas[plan["madre"]["id"]]
    assert madre["children"] == {"total": 3, "closed": 1}
    assert madre["parent_id"] is None

    hija = leidas[plan["hija"]["id"]]
    assert hija["children"] == {"total": 0, "closed": 0}
    assert hija["parent_id"] == plan["madre"]["id"]


def test_el_contador_cuenta_solo_las_hijas_directas(client):
    plan = plan_con_hijas(client)
    base = plan["base"]["id"]
    crear_fila(client, base, "Nieta", {"paso_de": plan["hija"]["id"], "estado": "Hecho"})
    leidas = {r["id"]: r for r in filas(client, base)}
    assert leidas[plan["madre"]["id"]]["children"] == {"total": 3, "closed": 1}
    assert leidas[plan["hija"]["id"]]["children"] == {"total": 1, "closed": 1}


def test_una_casilla_marcada_tambien_cierra(client):
    base = crear_base(
        client,
        schema=[
            {"key": "paso_de", "name": "Subtarea de", "type": "parent_row"},
            {"key": "hecho", "name": "Hecho", "type": "checkbox", "done": True},
        ],
        title="Casillas",
    )
    madre = crear_fila(client, base["id"], "Madre")
    crear_fila(client, base["id"], "Una", {"paso_de": madre["id"], "hecho": True})
    crear_fila(client, base["id"], "Dos", {"paso_de": madre["id"], "hecho": False})
    leidas = {r["id"]: r for r in filas(client, base["id"])}
    assert leidas[madre["id"]]["children"] == {"total": 2, "closed": 1}


def test_sin_propiedad_marcada_nada_cuenta_como_cerrado(client):
    base = crear_base(
        client,
        schema=[{"key": "paso_de", "name": "Subtarea de", "type": "parent_row"}],
        title="Sin estado",
    )
    madre = crear_fila(client, base["id"], "Madre")
    crear_fila(client, base["id"], "Una", {"paso_de": madre["id"]})
    leidas = {r["id"]: r for r in filas(client, base["id"])}
    assert leidas[madre["id"]]["children"] == {"total": 1, "closed": 0}


# ------------------------------------------------------------- reindexado

def test_el_reindexado_reconstruye_la_jerarquia(client, vault_path):
    plan = plan_con_hijas(client)
    antes = {
        fila["title"]: (fila["parent_id"], fila["children"])
        for fila in filas(client, plan["base"]["id"])
    }
    texto_antes = md_de_fila(vault_path, row_id=plan["hija"]["id"])

    assert client.post("/reindex").status_code == 200

    despues = {
        fila["title"]: (fila["parent_id"], fila["children"])
        for fila in filas(client, plan["base"]["id"])
    }
    assert despues == antes
    assert despues["Textos"][0] == plan["madre"]["id"]
    assert despues["Lanzar la web"][1] == {"total": 3, "closed": 1}
    # El fichero no se ha tocado al reindexar.
    assert md_de_fila(vault_path, row_id=plan["hija"]["id"]) == texto_antes


# ------------------------------------------------------------- nivel de vista

def test_la_tabla_y_el_tablero_ensanan_solo_madres_por_defecto(client):
    plan = plan_con_hijas(client)
    base = plan["base"]["id"]
    tabla = crear_vista(client, base, "Tabla", tipo="table")
    tablero = crear_vista(client, base, "Tablero", {"group_by": "estado"}, tipo="board")

    assert [r["title"] for r in filas(client, base, tabla["id"])] == ["Lanzar la web"]
    assert [r["title"] for r in filas(client, base, tablero["id"])] == ["Lanzar la web"]
    # Sin vista («Todas») se ve la base entera: el nivel es de la vista.
    assert len(filas(client, base)) == 4


def test_sin_vista_las_hijas_tambien_van_detras_de_su_madre(client):
    base = crear_base(client, title="Dos ramas")
    una = crear_fila(client, base["id"], "Una")
    otra = crear_fila(client, base["id"], "Otra")
    crear_fila(client, base["id"], "Hija de Una", {"paso_de": una["id"]})
    crear_fila(client, base["id"], "Hija de Otra", {"paso_de": otra["id"]})
    assert [r["title"] for r in filas(client, base["id"])] == [
        "Una",
        "Hija de Una",
        "Otra",
        "Hija de Otra",
    ]


def test_la_lista_ensena_todas_las_hijas_detras_de_su_madre(client):
    plan = plan_con_hijas(client)
    base = plan["base"]["id"]
    lista = crear_vista(client, base, "Lista", tipo="list")
    assert [r["title"] for r in filas(client, base, lista["id"])] == [
        "Lanzar la web",
        "Textos",
        "Diseño",
        "Dominio",
    ]


def test_el_nivel_de_la_vista_se_guarda_en_el_md(client, vault_path):
    plan = plan_con_hijas(client)
    base = plan["base"]["id"]
    vista = crear_vista(client, base, "Tabla", tipo="table")

    respuesta = client.patch(
        f"/databases/{base}/views/{vista['id']}", json={"config": {"parent_level": "all"}}
    )
    assert respuesta.status_code == 200, respuesta.text

    texto = (vault_path / "Bases" / "Plan.md").read_text(encoding="utf-8")
    assert "parent_level: all" in texto
    assert [r["title"] for r in filas(client, base, vista["id"])] == [
        "Lanzar la web",
        "Textos",
        "Diseño",
        "Dominio",
    ]

    # Y sobrevive a un reindexado: el `.md` es la verdad.
    assert client.post("/reindex").status_code == 200
    leidas = client.get(f"/databases/{base}/views").json()
    assert next(v for v in leidas if v["id"] == vista["id"])["config"]["parent_level"] == "all"
    assert len(filas(client, base, vista["id"])) == 4


def test_el_orden_de_la_vista_manda_entre_ramas_pero_no_dentro(client):
    plan = plan_con_hijas(client)
    base = plan["base"]["id"]
    # Dos madres con una hija cada una, y un orden que pondría la hija antes.
    otra = crear_fila(client, base, "Otra madre", {"fecha": "2026-01-01"})
    suya = crear_fila(
        client, base, "Su hija", {"paso_de": otra["id"], "fecha": "2026-01-02"}
    )
    vista = crear_vista(
        client, base, "Por fecha", {"sort": [{"key": "fecha", "dir": "asc"}], "parent_level": "all"}
    )
    titulos = [r["title"] for r in filas(client, base, vista["id"])]
    # La rama que manda por fecha va primero (Otra madre, 01-01) y su hija
    # (01-02) la sigue aunque «Textos» no tenga fecha; las sin fecha, al final.
    assert titulos[0] == "Otra madre"
    assert titulos[1] == "Su hija"
    assert titulos.index("Lanzar la web") < titulos.index("Textos")
    assert suya["parent_id"] == otra["id"]


# ------------------------------------------------------------- fila como página

def test_la_pagina_de_la_madre_trae_sus_subtareas(client):
    plan = plan_con_hijas(client)
    pagina = client.get(
        f"/databases/{plan['base']['id']}/rows/{plan['madre']['id']}"
    ).json()

    assert [s["title"] for s in pagina["subtasks"]] == ["Textos", "Diseño", "Dominio"]
    diseno = next(s for s in pagina["subtasks"] if s["title"] == "Diseño")
    assert diseno["properties"]["estado"] == "En curso"
    assert diseno["properties"]["fecha"] == "2026-12-01"
    assert diseno["parent_id"] == plan["madre"]["id"]
    assert pagina["children"] == {"total": 3, "closed": 1}
    # «Cerrada» lo dice el servidor con la regla de la base: la fila del estado
    # «Hecho» está cerrada y la de «En curso» no.
    assert diseno["closed"] is False
    assert next(s for s in pagina["subtasks"] if s["title"] == "Dominio")["closed"] is True
    assert pagina["closed"] is False

    # Una hija no arrastra a sus hermanas: solo SUS subtareas, y dice de quién
    # es (para la miga de pan de su página).
    hija = client.get(f"/databases/{plan['base']['id']}/rows/{plan['hija']['id']}").json()
    assert hija["subtasks"] == []
    assert hija["parent_id"] == plan["madre"]["id"]
    assert hija["parent_title"] == "Lanzar la web"


def test_una_subtarea_se_crea_ya_enlazada_desde_la_pagina(client, vault_path):
    plan = plan_con_hijas(client)
    nueva = crear_fila(client, plan["base"]["id"], "Nueva", {"paso_de": plan["madre"]["id"]})
    pagina = client.get(
        f"/databases/{plan['base']['id']}/rows/{plan['madre']['id']}"
    ).json()
    assert [s["title"] for s in pagina["subtasks"]][-1] == "Nueva"
    assert pagina["children"]["total"] == 4
    assert f"paso_de: {plan['madre']['id']}" in md_de_fila(
        vault_path, row_id=nueva["id"]
    )


# ------------------------------------------------------------- grafo

def test_el_grafo_dibuja_la_arista_madre_hija(client):
    plan = plan_con_hijas(client)
    datos = client.get("/graph", params={"mode": "global"}).json()
    pares = {
        (e["source"], e["target"], e["kind"]) for e in datos["edges"]
    }
    assert (plan["madre"]["id"], plan["hija"]["id"], "subtask") in pares
    # La arista NO es un enlace escrito: `links` no tiene esa fila (si no, los
    # backlinks de la madre listarían sus subtareas).
    assert (plan["madre"]["id"], plan["hija"]["id"], "wikilink") not in pares

    local = client.get(
        "/graph", params={"mode": "local", "page_id": plan["hija"]["id"], "depth": 1}
    ).json()
    ids = {n["id"] for n in local["nodes"]}
    assert plan["madre"]["id"] in ids


# ------------------------------------------------------------- lo de siempre

def test_una_base_sin_jerarquia_responde_igual_que_antes(client):
    base = crear_base(
        client,
        schema=[
            {"key": "valor", "name": "Valor", "type": "number"},
        ],
        title="Sin jerarquía",
    )
    for titulo, valor in [("Alfa", 2), ("Bravo", 1), ("Charlie", 3)]:
        crear_fila(client, base["id"], titulo, {"valor": valor})
    vista = crear_vista(client, base["id"], "Ordenada", {"sort": [{"key": "valor", "dir": "asc"}]})
    filas_vista = filas(client, base["id"], vista["id"])
    assert [r["title"] for r in filas_vista] == ["Bravo", "Alfa", "Charlie"]
    assert all(r["parent_id"] is None and r["children"] == {"total": 0, "closed": 0} for r in filas_vista)


def test_la_exportacion_no_pone_el_id_de_la_madre_como_columna(client):
    plan = plan_con_hijas(client)
    nota = client.post(
        "/pages",
        json={
            "title": "Plan en una nota",
            "body_markdown": (
                "```mdm-database\n"
                f"id: {plan['base']['id']}\n"
                "```\n"
            ),
        },
    )
    assert nota.status_code == 200, nota.text
    exportado = client.get(f"/pages/{nota.json()['id']}/export.md").text
    assert "Lanzar la web" in exportado
    assert plan["madre"]["id"] not in exportado
