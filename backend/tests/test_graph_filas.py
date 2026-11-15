"""Grafo: la pertenencia de una fila a su base es una arista `kind="row"`.

El dueño vio «Michifu» (fila de la base «Gatos») flotando suelto: las aristas
salían solo de `links` y la pertenencia no está ahí, está en `rows.database_id`
-> `databases.page_id`. Lo que se fija aquí:

1. base con filas -> una arista `row` por fila en `GET /graph`, y **ninguna
   fila nueva en `links`** (si se escribieran, los backlinks de la base
   listarían sus 300 filas).
2. el grafo local recorre la pertenencia en los dos sentidos.
3. el grado que aporta la pertenencia va topado y los wikilinks siguen
   contando enteros.
4. borrar una fila quita su arista y solo la suya.
5. lo de siempre sigue igual: wikilink a la base, embed, enlaces rotos y el
   corte del global sin aristas colgando.
"""

import sqlite3


def crear(client, title, body=""):
    respuesta = client.post("/pages", json={"title": title, "body_markdown": body})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_base(client, title):
    respuesta = client.post("/databases", json={"title": title, "schema_def": []})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id, title):
    respuesta = client.post(f"/databases/{database_id}/rows", json={"title": title})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def grafo(client, **params):
    respuesta = client.get("/graph", params=params)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def por_titulo(datos):
    return {nodo["title"]: nodo for nodo in datos["nodes"]}


def aristas_row(datos):
    return [e for e in datos["edges"] if e["kind"] == "row"]


def pares(datos):
    """Aristas como pares de títulos, para no comparar uuids a mano."""
    titulos = {nodo["id"]: nodo["title"] for nodo in datos["nodes"]}
    return sorted((titulos[e["source"]], titulos[e["target"]], e["kind"]) for e in datos["edges"])


def contar_links(index_path):
    """Filas de `links` en el índice: la pertenencia NO puede aparecer aquí."""
    con = sqlite3.connect(index_path)
    try:
        return con.execute("SELECT count(*) FROM links").fetchone()[0]
    finally:
        con.close()


def backlinks(client, page_id):
    respuesta = client.get(f"/pages/{page_id}/backlinks")
    assert respuesta.status_code == 200, respuesta.text
    return [p["title"] for p in respuesta.json()]


def gatos(client):
    """La base del dueño: «Gatos» con tres filas."""
    base = crear_base(client, "Gatos")
    filas = [crear_fila(client, base["id"], f"Fila {n}") for n in "123"]
    return base, filas


# --------------------------------------------------------------- la arista


def test_base_con_tres_filas_da_tres_aristas_row_y_ningun_link_nuevo(
    client, index_path
):
    base, filas = gatos(client)

    datos = grafo(client, mode="global")

    aristas = aristas_row(datos)
    assert len(aristas) == 3
    assert {(e["source"], e["target"]) for e in aristas} == {
        (base["page_id"], fila["id"]) for fila in filas
    }, "la arista va de la base (source) a la fila (target)"
    assert contar_links(index_path) == 0, (
        "la pertenencia se deduce de `rows`/`databases`: escribirla en `links` "
        "haría que los backlinks de la base listaran sus filas"
    )


def test_la_base_y_sus_filas_son_nodos_con_su_tipo(client):
    base, filas = gatos(client)

    nodos = por_titulo(grafo(client, mode="global"))

    assert nodos["Gatos"]["type"] == "database"
    assert nodos["Fila 1"]["type"] == "database_row"
    assert nodos["Gatos"]["id"] == base["page_id"]
    assert nodos["Fila 2"]["id"] == filas[1]["id"]


def test_la_arista_row_sobrevive_al_reindexado(client):
    gatos(client)
    antes = aristas_row(grafo(client, mode="global"))

    assert client.post("/reindex").status_code == 200

    despues = grafo(client, mode="global")
    assert aristas_row(despues) == antes


# ------------------------------------------------------------ grafo local


def test_el_grafo_local_de_la_base_incluye_sus_filas(client):
    base, filas = gatos(client)

    datos = grafo(client, mode="local", page_id=base["page_id"])

    assert set(por_titulo(datos)) == {"Gatos", "Fila 1", "Fila 2", "Fila 3"}
    assert aristas_row(datos) and len(aristas_row(datos)) == 3
    assert datos["truncated"] is False


def test_el_grafo_local_de_una_fila_incluye_su_base(client):
    base, filas = gatos(client)

    datos = grafo(client, mode="local", page_id=filas[0]["id"])

    assert set(por_titulo(datos)) == {"Gatos", "Fila 1"}, "la fila no es un nodo suelto"
    assert pares(datos) == [("Gatos", "Fila 1", "row")]


def test_el_grafo_local_de_una_nota_que_enlaza_la_base_no_arrastra_las_filas(
    client,
):
    """A un salto, la nota ve la base; las filas están a dos."""
    base, _ = gatos(client)
    nota = crear(client, "Nota")
    respuesta = client.patch(f"/pages/{nota['id']}", json={"body_markdown": "[[Gatos]]"})
    assert respuesta.status_code == 200, respuesta.text

    un_salto = por_titulo(grafo(client, mode="local", page_id=nota["id"]))
    dos_saltos = por_titulo(grafo(client, mode="local", page_id=nota["id"], depth=2))

    assert set(un_salto) == {"Nota", "Gatos"}
    assert set(dos_saltos) == {"Nota", "Gatos", "Fila 1", "Fila 2", "Fila 3"}


# ------------------------------------------------------------------ grados


def test_el_grado_de_la_pertenencia_va_topado_y_los_wikilinks_no(
    client,
):
    """12 filas no pueden hacer de su base el nodo más grande del vault."""
    base = crear_base(client, "Multitud")
    for n in range(12):
        crear_fila(client, base["id"], f"Fila {n}")
    # Dos wikilinks de verdad: esos cuentan enteros.
    for titulo in ("Una", "Otra"):
        nota = crear(client, titulo)
        respuesta = client.patch(
            f"/pages/{nota['id']}", json={"body_markdown": "[[Multitud]]"}
        )
        assert respuesta.status_code == 200, respuesta.text

    global_ = por_titulo(grafo(client, mode="global"))
    local = por_titulo(grafo(client, mode="local", page_id=base["page_id"]))

    # 5 de tope por pertenencia + 2 wikilinks (no 12 + 2).
    assert global_["Multitud"]["degree"] == local["Multitud"]["degree"] == 7
    assert global_["Fila 7"]["degree"] == 1


# ------------------------------------------------- las filas no son enlaces


def test_los_backlinks_de_la_base_siguen_sin_listar_filas(client):
    base, filas = gatos(client)
    nota = crear(client, "Proyecto")
    respuesta = client.patch(f"/pages/{nota['id']}", json={"body_markdown": "[[Gatos]]"})
    assert respuesta.status_code == 200, respuesta.text

    assert backlinks(client, base["page_id"]) == ["Proyecto"], (
        "solo la nota que enlaza la base; las filas no son backlinks"
    )
    assert backlinks(client, filas[0]["id"]) == [], "una fila no tiene enlaces escritos"


def test_un_wikilink_a_la_base_y_la_pertenencia_conviven(client):
    base, filas = gatos(client)
    nota = crear(client, "Nota")
    respuesta = client.patch(f"/pages/{nota['id']}", json={"body_markdown": "[[Gatos]]"})
    assert respuesta.status_code == 200, respuesta.text

    datos = grafo(client, mode="local", page_id=nota["id"], depth=2)

    kinds = {e["kind"] for e in datos["edges"]}
    assert kinds == {"wikilink", "row"}
    assert pares(datos) == [
        ("Gatos", "Fila 1", "row"),
        ("Gatos", "Fila 2", "row"),
        ("Gatos", "Fila 3", "row"),
        ("Nota", "Gatos", "wikilink"),
    ]


# --------------------------------------------------------------- borrados


def test_borrar_una_fila_quita_su_arista_y_solo_esa(client):
    base, filas = gatos(client)

    respuesta = client.delete(f"/databases/{base['id']}/rows/{filas[1]['id']}")
    assert respuesta.status_code == 204, respuesta.text

    datos = grafo(client, mode="global")
    assert {(e["source"], e["target"]) for e in aristas_row(datos)} == {
        (base["page_id"], filas[0]["id"]),
        (base["page_id"], filas[2]["id"]),
    }
    assert filas[1]["id"] not in {n["id"] for n in datos["nodes"]}

    local = grafo(client, mode="local", page_id=base["page_id"])
    assert set(por_titulo(local)) == {"Gatos", "Fila 1", "Fila 3"}


def test_borrar_la_base_quita_las_aristas_de_sus_filas(client):
    base, filas = gatos(client)

    assert client.delete(f"/databases/{base['id']}").status_code == 204

    datos = grafo(client, mode="global")
    assert aristas_row(datos) == []
    # Las filas se van con la base (`rows` cae en cascada): nodos sueltos no
    # tienen por qué quedar.
    assert not {"Fila 1", "Fila 2", "Fila 3"} & set(por_titulo(datos))


# ------------------------------------------------------------------ recorte


def test_el_corte_del_global_no_deja_aristas_row_colgando(client):
    base, filas = gatos(client)

    datos = grafo(client, mode="global", limit=1)

    assert [n["title"] for n in datos["nodes"]] == ["Gatos"], "el de más grado entra"
    assert datos["edges"] == [], "las filas quedaron fuera: sus aristas también"
    assert datos["truncated"] is True
