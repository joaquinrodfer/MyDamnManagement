"""P17.6: una base incrustada es un enlace, y las bases viven en el árbol.

Dos cosas del feedback del dueño del 9-nov:

1. Un bloque ```mdm-database tiene que contar como enlace: arista página ->
   base en el grafo, la base lista la nota en sus backlinks, y todo eso se
   reconstruye igual al reindexar. El bloque lleva el id de la fila `databases`
   (no el `page_id`), así que el extractor lo lee y `links.py` lo resuelve.
2. La sección «Bases de datos» del panel sobra: las bases son páginas del
   árbol. Eso es interfaz y no se prueba aquí (la suite de Python no pinta el
   panel); lo que sí se fija es que una base sin ninguna nota que la incruste
   sigue siendo una página más de `/pages/tree`.

Los tests del extractor son de texto puro; los demás pasan por la API, con el
vault y el índice de siempre.
"""

from pathlib import Path

from app import index as indice
from app.wikilinks import extract_database_ids

BASE_ID = "7a3f9c21-4b0e-4c8a-9f31-2d5e6b7a8c90"
OTRA_ID = "1b2c3d4e-5f60-4a7b-8c9d-0e1f2a3b4c5d"


def crear(client, **cuerpo) -> dict:
    respuesta = client.post("/pages", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_database(client, **cuerpo) -> dict:
    respuesta = client.post("/databases", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def bloque(database_id: str, *, view: str | None = None, title: str | None = None) -> str:
    lineas = ["```mdm-database", f"id: {database_id}"]
    if view:
        lineas.append(f"view: {view}")
    if title:
        lineas.append(f"title: {title}")
    lineas.append("```")
    return "\n".join(lineas)


def backlinks(client, page_id: str) -> list[str]:
    return [p["title"] for p in client.get(f"/pages/{page_id}/backlinks").json()]


def aristas(client, page_id: str) -> list[tuple[str, str, str]]:
    respuesta = client.get(f"/graph?mode=local&page_id={page_id}&depth=1")
    assert respuesta.status_code == 200, respuesta.text
    return [
        (e["source"], e["target"], e["kind"]) for e in respuesta.json()["edges"]
    ]


def huella(index_path: Path) -> tuple[str, dict]:
    return indice.dump_hash(str(index_path))


# --------------------------------------------------------------------------
# 1. el extractor de texto
# --------------------------------------------------------------------------


def test_extractor_lee_el_id_de_un_bloque_cerrado():
    cuerpo = f"Antes\n\n{bloque(BASE_ID, view='Tablero')}\n\nDespués"
    assert extract_database_ids(cuerpo) == [BASE_ID]


def test_extractor_admite_crlf_comillas_y_mayusculas():
    """El `.md` se escribe a mano: CRLF, comillas y un uuid en mayúsculas son
    el mismo bloque que serializa el editor."""
    cuerpo = "\r\n".join(
        ["```mdm-database", f'id: "{BASE_ID.upper()}"', "view: 'Tablero'", "```"]
    )
    assert extract_database_ids(cuerpo) == [BASE_ID]


def test_extractor_descarta_ids_invalidos_sin_reventar():
    """Un bloque a medio escribir no puede tumbar un guardado ni un reindexado."""
    assert extract_database_ids(bloque("no-es-un-uuid")) == []
    assert extract_database_ids(bloque("")) == []
    assert extract_database_ids("```mdm-database\nview: Tabla\n```") == []
    assert extract_database_ids("```mdm-database\nesto no es yaml\n```") == []
    assert extract_database_ids("```mdm-database\nid:\n```") == []
    assert extract_database_ids("```mdm-database\n```") == []
    assert extract_database_ids("") == []


def test_extractor_ignora_bloques_sin_cerrar_y_las_vallas_de_otro_lenguaje():
    """Lo mismo que ve el editor: sin cierre no hay bloque, y dentro de un
    ```js el formato documentado es texto, no una base."""
    sin_cerrar = f"# Cómo se escribe\n\n```mdm-database\nid: {BASE_ID}\n\nsigue el texto"
    assert extract_database_ids(sin_cerrar) == []

    documentado = f"```js\n```mdm-database\nid: {BASE_ID}\n```\n```"
    assert extract_database_ids(documentado) == []


def test_extractor_dos_bloques_misma_base_es_un_enlace_y_dos_bases_son_dos():
    """El bloque se puede repetir; el enlace es por base (destino), no por bloque."""
    assert extract_database_ids(f"{bloque(BASE_ID)}\n\n{bloque(BASE_ID)}") == [BASE_ID]
    assert extract_database_ids(f"{bloque(BASE_ID)}\n\n{bloque(OTRA_ID)}") == [
        BASE_ID,
        OTRA_ID,
    ]


# --------------------------------------------------------------------------
# 2. la arista en el índice: backlinks y grafo
# --------------------------------------------------------------------------


def test_una_base_incrustada_es_un_enlace(client):
    base = crear_database(client, title="Contactos", schema_def=[])
    nota = crear(
        client, title="Proyecto Alfa", body_markdown=f"Ficha:\n\n{bloque(base['id'])}\n"
    )

    # La base (su página) lista la nota entre sus backlinks.
    assert backlinks(client, base["page_id"]) == ["Proyecto Alfa"]

    # Y el grafo las une, en la dirección página -> base.
    arista = (nota["id"], base["page_id"], "embed")
    assert arista in aristas(client, nota["id"])
    assert arista in aristas(client, base["page_id"])

    # El mismo vínculo sobrevive a un reindexado completo.
    assert client.post("/reindex").status_code == 200
    assert backlinks(client, base["page_id"]) == ["Proyecto Alfa"]
    assert arista in aristas(client, nota["id"])


def test_quitar_el_bloque_quita_la_arista(client):
    base = crear_database(client, title="Contactos", schema_def=[])
    nota = crear(client, title="Proyecto", body_markdown=bloque(base["id"]))
    assert backlinks(client, base["page_id"]) == ["Proyecto"]

    client.patch(f"/pages/{nota['id']}", json={"body_markdown": "Ya no la incrusto."})

    assert backlinks(client, base["page_id"]) == []
    assert aristas(client, nota["id"]) == []


def test_un_id_invalido_o_una_base_que_no_existe_no_crea_arista(client):
    """La nota se guarda igual (200), sin arista y sin reventar el índice."""
    inexistente = "00000000-0000-0000-0000-000000000001"
    nota = crear(
        client,
        title="Nota rara",
        body_markdown=f"{bloque('no-es-un-uuid')}\n\n{bloque(inexistente)}\n",
    )

    assert aristas(client, nota["id"]) == []
    assert client.post("/reindex").status_code == 200
    assert aristas(client, nota["id"]) == []


def test_dos_bloques_de_bases_distintas_son_dos_aristas(client):
    primera = crear_database(client, title="Contactos", schema_def=[])
    segunda = crear_database(client, title="Tareas", schema_def=[])
    nota = crear(
        client,
        title="Con dos bases",
        body_markdown=f"{bloque(primera['id'])}\n\n{bloque(segunda['id'])}\n",
    )

    assert backlinks(client, primera["page_id"]) == ["Con dos bases"]
    assert backlinks(client, segunda["page_id"]) == ["Con dos bases"]
    assert (nota["id"], primera["page_id"], "embed") in aristas(client, nota["id"])
    assert (nota["id"], segunda["page_id"], "embed") in aristas(client, nota["id"])


def test_dos_bloques_de_la_misma_base_son_una_arista_y_un_backlink(client):
    base = crear_database(client, title="Contactos", schema_def=[])
    nota = crear(
        client,
        title="La repite",
        body_markdown=f"{bloque(base['id'])}\n\n{bloque(base['id'], view='Tabla')}\n",
    )

    assert backlinks(client, base["page_id"]) == ["La repite"]
    assert aristas(client, nota["id"]).count((nota["id"], base["page_id"], "embed")) == 1


def test_wikilink_y_bloque_a_la_misma_base_no_duplican_el_backlink(client):
    """Dos formas de enlazar la misma base: dos aristas en el grafo, una fila en
    la lista de backlinks (es la misma nota enlazando)."""
    base = crear_database(client, title="Contactos", schema_def=[])
    nota = crear(
        client,
        title="Por las dos vías",
        body_markdown=f"[[Contactos]]\n\n{bloque(base['id'])}\n",
    )

    assert backlinks(client, base["page_id"]) == ["Por las dos vías"]
    kinds = {
        kind
        for source, target, kind in aristas(client, nota["id"])
        if (source, target) == (nota["id"], base["page_id"])
    }
    assert kinds == {"wikilink", "embed"}


# --------------------------------------------------------------------------
# 3. índice borrado: el arranque lo reconstruye desde los ficheros
# --------------------------------------------------------------------------


def test_borrar_el_indice_y_arrancar_reconstruye_la_arista(client, index_path, acceso):
    base = crear_database(client, title="Contactos", schema_def=[])
    nota = crear(client, title="Proyecto", body_markdown=bloque(base["id"]))
    assert backlinks(client, base["page_id"]) == ["Proyecto"]

    antes_hash, antes_cuentas = huella(index_path)
    assert antes_cuentas["links"] == 1

    for suffix in ("", "-wal", "-shm"):
        Path(str(index_path) + suffix).unlink(missing_ok=True)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as otro:
        acceso(otro)  # el índice se borró: usuarios incluidos
        assert backlinks(otro, base["page_id"]) == ["Proyecto"]
        assert (nota["id"], base["page_id"], "embed") in aristas(otro, nota["id"])

    despues_hash, despues_cuentas = huella(index_path)
    assert despues_cuentas == antes_cuentas, "huella distinta tabla a tabla"
    assert despues_hash == antes_hash, "el índice reconstruido no es idéntico"


# --------------------------------------------------------------------------
# 4. la base sigue siendo una página del árbol (sin sección en el panel)
# --------------------------------------------------------------------------


def test_una_base_sin_nadie_que_la_incruste_sigue_en_el_arbol(client):
    """Borrar la sección «Bases de datos» no puede dejar bases huérfanas: sin
    ninguna nota que la incruste, la base se alcanza por `/pages/tree`."""
    base = crear_database(client, title="Suelta", schema_def=[])

    def planas(nodos):
        for nodo in nodos:
            yield nodo
            yield from planas(nodo.get("children") or [])

    arbol = list(planas(client.get("/pages/tree").json()))
    fila = next((n for n in arbol if n["id"] == base["page_id"]), None)
    assert fila is not None, "una base sin incrustar tiene que estar en el árbol"
    assert fila["type"] == "database"
    assert fila["database_id"] == base["id"]
