"""P9: `GET /graph` -- el grafo local y el global.

Lo que se fija aquí, con un vault pequeño y real (la API escribe los `.md`):

1. local a 1 y a 2 saltos: el nodo centro, sus vecinos y **solo** las aristas
   que quedan dentro de esa vecindad (una arista hacia fuera colgaría).
2. global con tope: nodos por grado, aristas con los dos extremos dentro del
   corte, y `truncated` diciendo la verdad.
3. vault vacío: grafo vacío, no un error.
4. enlaces rotos: se cuentan y no aparece ningún nodo fantasma.
"""

import uuid


def crear(client, title, body=""):
    respuesta = client.post("/pages", json={"title": title, "body_markdown": body})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def guardar_cuerpo(client, page_id, body):
    """El cuerpo se guarda después de crear todas las notas: un wikilink solo
    resuelve contra páginas que ya existen."""
    respuesta = client.patch(f"/pages/{page_id}", json={"body_markdown": body})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def grafo(client, **params):
    respuesta = client.get("/graph", params=params)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def por_titulo(datos):
    return {nodo["title"]: nodo for nodo in datos["nodes"]}


def aristas_tituladas(datos):
    """Aristas como pares de títulos, para no comparar uuids a mano."""
    titulos = {nodo["id"]: nodo["title"] for nodo in datos["nodes"]}
    return sorted((titulos[e["source"]], titulos[e["target"]]) for e in datos["edges"])


def cadena(client):
    """A -> B -> C -> D -> E, con A y B apuntando también a C."""
    notas = {nombre: crear(client, nombre) for nombre in "ABCDE"}
    guardar_cuerpo(client, notas["A"]["id"], "[[B]] y [[C]]")
    guardar_cuerpo(client, notas["B"]["id"], "[[C]]")
    guardar_cuerpo(client, notas["C"]["id"], "[[D]]")
    guardar_cuerpo(client, notas["D"]["id"], "[[E]]")
    return notas


def test_local_depth_1_es_la_nota_y_sus_vecinos(client):
    notas = cadena(client)

    datos = grafo(client, mode="local", page_id=notas["A"]["id"])

    assert set(por_titulo(datos)) == {"A", "B", "C"}, "D está a 2 saltos, no a 1"
    assert aristas_tituladas(datos) == [("A", "B"), ("A", "C"), ("B", "C")]
    assert datos["truncated"] is False
    assert datos["broken"] == 0


def test_local_depth_2_alcanza_los_vecinos_de_los_vecinos(client):
    notas = cadena(client)

    datos = grafo(client, mode="local", page_id=notas["A"]["id"], depth=2)

    assert set(por_titulo(datos)) == {"A", "B", "C", "D"}
    # C -> D entra (los dos extremos están), D -> E NO: E queda fuera.
    assert aristas_tituladas(datos) == [
        ("A", "B"),
        ("A", "C"),
        ("B", "C"),
        ("C", "D"),
    ]


def test_local_el_grado_no_depende_del_modo(client):
    notas = cadena(client)

    local = por_titulo(grafo(client, mode="local", page_id=notas["C"]["id"], depth=2))
    global_ = por_titulo(grafo(client, mode="global"))

    assert local["C"]["degree"] == global_["C"]["degree"] == 3  # A->C, B->C, C->D
    assert local["A"]["degree"] == 2  # A->B, A->C
    assert local["D"]["degree"] == 2  # C->D, D->E


def test_global_corta_por_grado_y_no_deja_aristas_colgando(client):
    cadena(client)

    datos = grafo(client, mode="global", limit=3)

    # Grados: B=2, C=2, D=2, A=1, E=1.
    assert set(por_titulo(datos)) == {"B", "C", "D"}
    assert datos["truncated"] is True
    assert aristas_tituladas(datos) == [("B", "C"), ("C", "D")]
    ids = {nodo["id"] for nodo in datos["nodes"]}
    for arista in datos["edges"]:
        assert arista["source"] in ids and arista["target"] in ids, (
            "una arista con un extremo fuera del corte apuntaría a la nada"
        )


def test_global_sin_tope_no_trunca(client):
    cadena(client)

    datos = grafo(client, mode="global", limit=10)

    assert len(datos["nodes"]) == 5
    assert len(datos["edges"]) == 5
    assert datos["truncated"] is False


def test_vault_vacio_devuelve_grafo_vacio(client):
    datos = grafo(client, mode="global")

    assert datos == {"nodes": [], "edges": [], "truncated": False, "broken": 0}


def test_enlaces_rotos_se_cuentan_y_no_se_pintan(client):
    origen = crear(client, "Origen")
    crear(client, "Destino")
    guardar_cuerpo(
        client, origen["id"], "[[Destino]], [[Fantasma]] y otra vez [[fantasma]]"
    )

    datos = grafo(client, mode="local", page_id=origen["id"])

    assert set(por_titulo(datos)) == {"Origen", "Destino"}
    assert "Fantasma" not in por_titulo(datos), "un nodo fantasma sin título ni cuerpo"
    assert datos["broken"] == 1, "el mismo título plegado en la misma nota cuenta una vez"
    assert aristas_tituladas(datos) == [("Origen", "Destino")]


def test_los_enlaces_rotos_de_un_vecino_tambien_cuentan(client):
    origen = crear(client, "Origen")
    vecino = crear(client, "Vecino")
    guardar_cuerpo(client, origen["id"], "[[Vecino]]")
    guardar_cuerpo(client, vecino["id"], "[[No existe]]")

    datos = grafo(client, mode="local", page_id=origen["id"])

    assert datos["broken"] == 1, "el grafo local incluye al vecino; sus rotos también"


def test_una_base_de_datos_enlazada_es_un_nodo_mas(client):
    nota = crear(client, "Nota")
    base = client.post("/databases", json={"title": "Contactos", "schema_def": []})
    assert base.status_code == 200, base.text
    guardar_cuerpo(client, nota["id"], "[[Contactos]]")

    datos = grafo(client, mode="local", page_id=nota["id"])

    nodos = por_titulo(datos)
    assert nodos["Contactos"]["type"] == "database"
    assert nodos["Nota"]["type"] == "note"
    assert aristas_tituladas(datos) == [("Nota", "Contactos")]


def test_local_exige_page_id(client):
    respuesta = client.get("/graph", params={"mode": "local"})
    assert respuesta.status_code == 422


def test_local_con_pagina_inexistente_es_404(client):
    respuesta = client.get(
        "/graph", params={"mode": "local", "page_id": str(uuid.uuid4())}
    )
    assert respuesta.status_code == 404


def test_limites_de_los_parametros(client):
    nota = crear(client, "Nota")

    assert client.get(
        "/graph", params={"mode": "local", "page_id": nota["id"], "depth": 3}
    ).status_code == 422, "depth máximo 2"
    assert client.get(
        "/graph", params={"mode": "global", "limit": 801}
    ).status_code == 422, "limit máximo 800"
    assert client.get("/graph", params={"mode": "otro"}).status_code == 422
    assert client.get("/graph").status_code == 422, "mode es obligatorio"
