"""M3: una sugerencia nueva (o decidida) se anuncia por `/events`.

El defecto que tapa esto: con una nota abierta y el panel cerrado, un agente
proponía (`POST /agent/suggestions`) y el dueño no se enteraba hasta cambiar de
nota y volver. El canal SSE ya existía para las capturas y para el vigilante,
pero nadie publicaba nada al crear o decidir una sugerencia.

Lo que se fija aquí:

1. crear una sugerencia publica un aviso `suggestion` con su `page_id`, su
   `suggestion_id` y la acción (`created`, u `orphaned` si nace desanclada);
2. aceptar y rechazar publican la acción que toca, y aceptar sigue publicando
   además el `page` de siempre (el `.md` cambió);
3. aceptar una sugerencia que ya no encaja avisa `orphaned` aunque responda 409;
4. `/events` manda el evento con **su** nombre (`event: suggestion`), no con el
   `page` fijo de antes.

El bus, en estos tests, es el de verdad salvo en los que cuentan avisos: ahí se
sustituye `bus.publish` por una lista, que es exactamente lo que hace el
endpoint cuando reparte.
"""

import asyncio
import json

import pytest

from app.config import settings
from app.events import bus

TOKEN = "tok-de-hitos"
HEADERS = {"X-Agent-Token": TOKEN}


@pytest.fixture(autouse=True)
def agentes(monkeypatch):
    """El token del agente de estos tests."""
    monkeypatch.setattr(settings, "agent_tokens", f"{TOKEN}:agente-hitos")


@pytest.fixture()
def eventos(monkeypatch):
    """Todo lo que se publica en el bus, en orden, sin repartirlo a nadie."""
    vistos = []
    monkeypatch.setattr(bus, "publish", vistos.append)
    return vistos


# --------------------------------------------------------------------------
# ayudas
# --------------------------------------------------------------------------


def crear_nota(client, title="Hitos", body="Primer hito\nSegundo hito") -> dict:
    respuesta = client.post("/pages", json={"title": title, "body_markdown": body})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def proponer(client, page_id, **cuerpo):
    """`POST /agent/suggestions`, la puerta del agente."""
    cuerpo.setdefault("kind", "edit")
    cuerpo.setdefault("page_id", page_id)
    return client.post("/agent/suggestions", json=cuerpo, headers=HEADERS)


def avisos(eventos, action=None) -> list[dict]:
    """Los eventos `suggestion` publicados (opcionalmente, de una acción)."""
    encontrados = [e for e in eventos if e.get("type") == "suggestion"]
    if action is not None:
        encontrados = [e for e in encontrados if e.get("action") == action]
    return encontrados


# --------------------------------------------------------------------------
# 1. crear publica el aviso
# --------------------------------------------------------------------------


def test_crear_una_sugerencia_por_la_puerta_del_agente_publica_el_aviso(
    client, eventos
):
    nota = crear_nota(client)

    respuesta = proponer(
        client,
        nota["id"],
        old_text="Primer hito",
        new_text="Primer hito del año",
        rationale="más concreto",
    )
    assert respuesta.status_code == 200, respuesta.text

    encontrados = avisos(eventos, "created")
    assert len(encontrados) == 1, eventos
    aviso = encontrados[0]
    assert aviso["page_id"] == nota["id"]
    assert aviso["suggestion_id"] == respuesta.json()["id"]
    assert aviso["path"] == "Notas/Hitos.md"
    # Un solo `suggestion` y ningún `page`: proponer no toca el `.md`, así que
    # la nota abierta no tiene por qué recargarse.
    assert [e for e in eventos if e.get("type") == "page"] == []


def test_crear_por_la_puerta_del_dueno_tambien_publica_el_aviso(client, eventos):
    nota = crear_nota(client)

    respuesta = client.post(
        f"/pages/{nota['id']}/suggestions",
        json={
            "agent": "agente-hitos",
            "kind": "edit",
            "old_text": "Segundo hito",
            "new_text": "Segundo hito, ya cerrado",
        },
    )
    assert respuesta.status_code == 200, respuesta.text

    encontrados = avisos(eventos, "created")
    assert len(encontrados) == 1, eventos
    assert encontrados[0]["suggestion_id"] == respuesta.json()["id"]
    assert encontrados[0]["page_id"] == nota["id"]


def test_una_sugerencia_que_nace_desanclada_se_anuncia_como_orphaned(client, eventos):
    """`old_text` que no está en el cuerpo: nace `orphaned`, y eso es novedad."""
    nota = crear_nota(client)

    respuesta = proponer(
        client,
        nota["id"],
        old_text="Un texto que no está en la nota",
        new_text="otra cosa",
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["status"] == "orphaned"

    encontrados = avisos(eventos, "orphaned")
    assert len(encontrados) == 1, eventos
    assert encontrados[0]["suggestion_id"] == respuesta.json()["id"]
    assert encontrados[0]["page_id"] == nota["id"]


# --------------------------------------------------------------------------
# 2. decidir publica el aviso (y aceptar no se come el `page`)
# --------------------------------------------------------------------------


def test_aceptar_publica_el_aviso_y_el_page_del_md(client, eventos, vault_path):
    nota = crear_nota(client)
    propuesta = proponer(
        client,
        nota["id"],
        old_text="Primer hito",
        new_text="Primer hito del año",
    ).json()
    eventos.clear()

    respuesta = client.post(f"/suggestions/{propuesta['id']}/accept")
    assert respuesta.status_code == 200, respuesta.text

    aceptadas = avisos(eventos, "accepted")
    assert len(aceptadas) == 1, eventos
    assert aceptadas[0]["suggestion_id"] == propuesta["id"]
    assert aceptadas[0]["page_id"] == nota["id"]

    # El `.md` cambió: el aviso `page` de siempre sigue saliendo, ahora junto al
    # de la sugerencia.
    paginas = [e for e in eventos if e.get("type") == "page"]
    assert len(paginas) == 1, eventos
    assert paginas[0]["action"] == "modified"
    assert paginas[0]["page_id"] == nota["id"]

    # Y el texto del parche está en el fichero, que es lo que relee la pestaña.
    escrito = (vault_path / "Notas" / "Hitos.md").read_text(encoding="utf-8")
    assert "Primer hito del año" in escrito


def test_rechazar_publica_el_aviso_sin_tocar_el_md(client, eventos):
    nota = crear_nota(client)
    propuesta = proponer(
        client, nota["id"], old_text="Primer hito", new_text="Primer hito del año"
    ).json()
    eventos.clear()

    respuesta = client.post(f"/suggestions/{propuesta['id']}/reject")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["status"] == "rejected"

    rechazadas = avisos(eventos, "rejected")
    assert len(rechazadas) == 1, eventos
    assert rechazadas[0]["suggestion_id"] == propuesta["id"]
    assert rechazadas[0]["page_id"] == nota["id"]
    # Rechazar no escribe en el `.md`: ningún aviso `page`.
    assert [e for e in eventos if e.get("type") == "page"] == []


def test_aceptar_una_sugerencia_desanclada_avisa_orphaned(client, eventos):
    """409 `desanclada`: la sugerencia se queda huérfana y la pestaña lo ve."""
    nota = crear_nota(client)
    propuesta = proponer(
        client, nota["id"], old_text="Primer hito", new_text="Primer hito del año"
    ).json()
    # La nota cambia por debajo: el ancla desaparece.
    assert (
        client.patch(
            f"/pages/{nota['id']}", json={"body_markdown": "Otra cosa entera"}
        ).status_code
        == 200
    )
    eventos.clear()

    respuesta = client.post(f"/suggestions/{propuesta['id']}/accept")
    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json() == {"error": "desanclada"}

    huerfanas = avisos(eventos, "orphaned")
    assert len(huerfanas) == 1, eventos
    assert huerfanas[0]["suggestion_id"] == propuesta["id"]
    assert huerfanas[0]["page_id"] == nota["id"]

    # El `.md` no se ha tocado: no hay aviso `page`.
    assert [e for e in eventos if e.get("type") == "page"] == []


# --------------------------------------------------------------------------
# 3. `/events` manda el evento con su propio nombre
# --------------------------------------------------------------------------


def test_el_stream_nombra_el_evento_suggestion():
    """El nombre de la línea `event:` sale del `type`, no del `page` fijo."""
    from app.routers.events import stream_events

    identificador = "33333333-3333-4333-8333-333333333333"
    sugerencia = "66666666-6666-4666-8666-666666666666"

    async def escenario():
        respuesta = await stream_events()
        assert respuesta.media_type == "text/event-stream"
        generador = respuesta.body_iterator
        apertura = await asyncio.wait_for(anext(generador), 5.0)
        bus.publish(
            {
                "type": "suggestion",
                "action": "created",
                "page_id": identificador,
                "suggestion_id": sugerencia,
                "path": "Notas/Hitos.md",
            }
        )
        chunk = await asyncio.wait_for(anext(generador), 5.0)
        await generador.aclose()
        return apertura, chunk

    apertura, chunk = asyncio.run(escenario())

    assert apertura == ": conectado\n\n"
    lineas = chunk.strip().splitlines()
    assert lineas[0] == "event: suggestion", chunk
    assert json.loads(lineas[1][len("data: ") :]) == {
        "type": "suggestion",
        "action": "created",
        "page_id": identificador,
        "suggestion_id": sugerencia,
        "path": "Notas/Hitos.md",
    }
