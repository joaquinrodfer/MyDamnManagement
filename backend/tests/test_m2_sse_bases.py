"""M2: una base de datos borrada o cambiada se anuncia por `/events`.

El defecto que tapa esto: con una nota abierta que incrusta una base (bloque
`` ```mdm-database ``), borrar la base desde otra pestaña dejaba la tabla vieja
en pantalla hasta reabrir la nota; renombrarla o cambiar sus filas tampoco se
veía. El canal SSE ya existía (páginas, sugerencias), pero nadie publicaba los
cambios de una base.

Lo que se fija aquí:

1. borrar publica `type: database`, `action: deleted` y el `database_id` de la
   valla —lo justo para que el bloque pase a «Base no encontrada» sin tocar el
   documento—;
2. renombrar (o cambiar el esquema/vistas) publica `updated`, que es lo que
   repinta el título;
3. alta, cambio y borrado de fila publican `rows`, para repintar la tabla;
4. un DELETE que no borra nada (404) no publica;
5. `/events` manda el aviso con **su** nombre (`event: database`), no con el
   `page` de siempre.

Como en los tests de M3, el bus es el de verdad salvo en los que cuentan
avisos: ahí se sustituye `bus.publish` por una lista, que es exactamente lo que
hace el endpoint cuando reparte.
"""

import asyncio
import json
import uuid

import pytest

from app.events import bus

SCHEMA = [
    {"key": "empresa", "name": "Empresa", "type": "text"},
    {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
]


@pytest.fixture()
def eventos(monkeypatch):
    """Todo lo que se publica en el bus, en orden, sin repartirlo a nadie."""
    vistos = []
    monkeypatch.setattr(bus, "publish", vistos.append)
    return vistos


# --------------------------------------------------------------------------
# ayudas
# --------------------------------------------------------------------------


def crear_base(client, title="Contactos") -> dict:
    respuesta = client.post("/databases", json={"title": title, "schema_def": SCHEMA})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id, title="Acme") -> dict:
    respuesta = client.post(
        f"/databases/{database_id}/rows",
        json={"title": title, "properties": {}},
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def avisos(eventos, action=None) -> list[dict]:
    """Los eventos `database` publicados (opcionalmente, de una acción)."""
    encontrados = [e for e in eventos if e.get("type") == "database"]
    if action is not None:
        encontrados = [e for e in encontrados if e.get("action") == action]
    return encontrados


# --------------------------------------------------------------------------
# 1. borrar la base: el aviso que apaga la tabla vieja
# --------------------------------------------------------------------------


def test_borrar_una_base_publica_deleted_con_su_id(client, eventos):
    base = crear_base(client)
    eventos.clear()

    respuesta = client.delete(f"/databases/{base['id']}")

    assert respuesta.status_code == 204
    borradas = avisos(eventos, "deleted")
    assert len(borradas) == 1, eventos
    assert borradas[0] == {
        "type": "database",
        "action": "deleted",
        "database_id": base["id"],
    }


def test_borrar_una_base_que_no_existe_no_publica_nada(client, eventos):
    eventos.clear()

    respuesta = client.delete(f"/databases/{uuid.uuid4()}")

    assert respuesta.status_code == 404
    assert avisos(eventos) == []


# --------------------------------------------------------------------------
# 2. renombrar / esquema / vistas: `updated`
# --------------------------------------------------------------------------


def test_renombrar_publica_updated(client, eventos):
    base = crear_base(client)
    eventos.clear()

    respuesta = client.patch(
        f"/databases/{base['id']}", json={"title": "Contactos 2027"}
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["title"] == "Contactos 2027"
    cambios = avisos(eventos, "updated")
    assert len(cambios) == 1, eventos
    assert cambios[0]["database_id"] == base["id"]


def test_cambiar_las_vistas_publica_updated(client, eventos):
    base = crear_base(client)
    eventos.clear()

    vista = client.post(
        f"/databases/{base['id']}/views",
        json={"name": "Tablero", "type": "board", "config": {}},
    )
    assert vista.status_code == 200, vista.text
    assert [e["database_id"] for e in avisos(eventos, "updated")] == [base["id"]]

    eventos.clear()
    assert (
        client.delete(f"/databases/{base['id']}/views/{vista.json()['id']}").status_code
        == 204
    )
    assert [e["database_id"] for e in avisos(eventos, "updated")] == [base["id"]]


# --------------------------------------------------------------------------
# 3. filas: `rows`
# --------------------------------------------------------------------------


def test_alta_cambio_y_borrado_de_fila_publican_rows(client, eventos):
    base = crear_base(client)
    eventos.clear()

    fila = crear_fila(client, base["id"], "Acme")
    assert [e["action"] for e in avisos(eventos)] == ["rows"]

    eventos.clear()
    assert (
        client.patch(
            f"/databases/{base['id']}/rows/{fila['id']}",
            json={"properties": {"fase": "ganado"}},
        ).status_code
        == 200
    )
    assert [e["action"] for e in avisos(eventos)] == ["rows"]

    eventos.clear()
    assert (
        client.delete(f"/databases/{base['id']}/rows/{fila['id']}").status_code == 204
    )
    filas = avisos(eventos, "rows")
    assert len(filas) == 1, eventos
    assert filas[0]["database_id"] == base["id"]


def test_una_nota_normal_no_publica_avisos_de_base(client, eventos):
    """Control: editar una nota (sin bases) no inventa eventos `database`."""
    nota = client.post(
        "/pages", json={"title": "Nota", "body_markdown": "texto"}
    ).json()
    eventos.clear()

    client.patch(f"/pages/{nota['id']}", json={"body_markdown": "texto nuevo"})

    assert avisos(eventos) == []


# --------------------------------------------------------------------------
# 4. `/events` manda el evento con su propio nombre
# --------------------------------------------------------------------------


def test_el_stream_nombra_el_evento_database():
    from app.routers.events import stream_events

    identificador = "77777777-7777-4777-8777-777777777777"

    async def escenario():
        respuesta = await stream_events()
        assert respuesta.media_type == "text/event-stream"
        generador = respuesta.body_iterator
        apertura = await asyncio.wait_for(anext(generador), 5.0)
        bus.publish(
            {
                "type": "database",
                "action": "deleted",
                "database_id": identificador,
            }
        )
        chunk = await asyncio.wait_for(anext(generador), 5.0)
        await generador.aclose()
        return apertura, chunk

    apertura, chunk = asyncio.run(escenario())

    assert apertura == ": conectado\n\n"
    lineas = chunk.strip().splitlines()
    assert lineas[0] == "event: database", chunk
    assert json.loads(lineas[1][len("data: ") :]) == {
        "type": "database",
        "action": "deleted",
        "database_id": identificador,
    }
