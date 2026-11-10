"""Borrar una vista de una base: `DELETE /databases/{id}/views/{view_id}`.

La vista no guarda filas: es una forma de mirar la base (filtro, orden, tipo).
Borrar una vista reescribe el frontmatter de `Bases/<slug>.md` con las que
quedan y no toca ni una fila. La última no se borra (409): es la pestaña con la
que la base se abre y una base sin vistas solo es un estado de creación.

Este módulo fija las dos fronteras: la de la última vista y la de una base con
tres, que se queda con dos y con las filas intactas.
"""

from pathlib import Path

SCHEMA = [
    {"key": "empresa", "name": "Empresa", "type": "text"},
    {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
]


def crear_base(client) -> dict:
    respuesta = client.post(
        "/databases", json={"title": "Contactos", "schema_def": SCHEMA}
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_vista(client, database_id, nombre, tipo="table", config=None) -> dict:
    respuesta = client.post(
        f"/databases/{database_id}/views",
        json={"name": nombre, "type": tipo, "config": config or {}},
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id, titulo) -> dict:
    respuesta = client.post(
        f"/databases/{database_id}/rows", json={"title": titulo, "properties": {}}
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def views_del_md(vault_path: Path) -> list[dict]:
    """Las vistas que el `.md` de la base dice tener (la verdad es el fichero)."""
    from app.vault import fm

    fichero = vault_path / "Bases" / "Contactos.md"
    raw, _cuerpo = fm.split_document(fichero.read_text(encoding="utf-8"))
    assert raw is not None, "la base no tiene frontmatter"
    return fm.parse(raw).data.get("views") or []


def test_borrar_la_unica_vista_es_409_y_el_md_no_cambia(client, vault_path):
    base = crear_base(client)
    unica = crear_vista(client, base["id"], "Tablero", "board", {"group_by": "fase"})
    antes = views_del_md(vault_path)
    assert [v["id"] for v in antes] == [unica["id"]]

    respuesta = client.delete(f"/databases/{base['id']}/views/{unica['id']}")

    assert respuesta.status_code == 409, respuesta.text
    # El motivo se dice, no es un 409 mudo.
    assert "última vista" in respuesta.json()["detail"]
    assert views_del_md(vault_path) == antes
    assert [v["id"] for v in client.get(f"/databases/{base['id']}/views").json()] == [
        unica["id"]
    ]


def test_borrar_una_de_tres_deja_dos_en_el_md_y_las_filas_intactas(client, vault_path):
    base = crear_base(client)
    primera = crear_vista(client, base["id"], "Tabla", "table")
    segunda = crear_vista(
        client,
        base["id"],
        "Ganadas",
        "board",
        {"group_by": "fase", "filters": [{"key": "fase", "op": "eq", "value": "ganado"}]},
    )
    tercera = crear_vista(client, base["id"], "Lista", "list")
    fila = crear_fila(client, base["id"], "Acme")

    respuesta = client.delete(f"/databases/{base['id']}/views/{segunda['id']}")

    assert respuesta.status_code == 204, respuesta.text
    quedan = views_del_md(vault_path)
    assert [v["name"] for v in quedan] == ["Tabla", "Lista"]
    assert [v["id"] for v in quedan] == [primera["id"], tercera["id"]]
    # Las vistas que quedan conservan lo suyo: borrar una no reescribe las otras.
    assert quedan[1]["type"] == "list"
    # Y la fila sigue donde estaba, con su `.md`: los datos son de la base.
    assert client.get(f"/databases/{base['id']}/rows/{fila['id']}").status_code == 200
    assert (vault_path / "Bases" / "Contactos" / f"{fila['id'][:8]}.md").exists()


def test_borrar_una_vista_que_no_existe_es_404(client):
    base = crear_base(client)
    crear_vista(client, base["id"], "Tabla", "table")
    otra_base = crear_base(client)
    ajena = crear_vista(client, otra_base["id"], "De otra base", "table")

    # Una vista de OTRA base no se borra desde esta: 404, no un 204 silencioso.
    respuesta = client.delete(f"/databases/{base['id']}/views/{ajena['id']}")

    assert respuesta.status_code == 404, respuesta.text
    assert [v["name"] for v in client.get(f"/databases/{otra_base['id']}/views").json()] == [
        "De otra base"
    ]
