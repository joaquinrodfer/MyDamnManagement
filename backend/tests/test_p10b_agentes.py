"""P10b: la puerta de los agentes — token, contexto y canal de vuelta.

Lo que se fija aquí:

1. **Identidad.** Sin token o con uno malo, 401; sin `AGENT_TOKENS`, las rutas
   no existen (404). El `agent` de una sugerencia sale del token aunque el
   cuerpo diga otro, y el token no aparece en ningún log.
2. **Contexto.** `GET /agent/context` devuelve la nota con sus backlinks, sus
   wikilinks (y si resuelven), su hilo de sugerencias —resueltas incluidas— y
   el `brief` de `Agentes/<nombre>.md`. El cuerpo se corta y se dice.
3. **Propuesta y vuelta.** `POST /agent/suggestions` propone (con topes: 413
   de tamaño, 429 de pendientes) y `GET /agent/suggestions` cuenta lo que el
   dueño decidió, filtrable por estado y por última actividad.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import repository
from app.config import settings

TOKENS = "tok-de-hitos:agente-hitos,tok-de-revision:agente-revision"
HITOS = {"X-Agent-Token": "tok-de-hitos"}
REVISION = {"X-Agent-Token": "tok-de-revision"}


@pytest.fixture(autouse=True)
def agentes(monkeypatch):
    """Tokens configurados para el test; quien los quiera fuera, los vacía."""
    monkeypatch.setattr(settings, "agent_tokens", TOKENS)


# --------------------------------------------------------------------------
# ayudas
# --------------------------------------------------------------------------


def crear_nota(client, title="Proyecto", body="", parent_id=None) -> dict:
    payload = {"title": title, "body_markdown": body}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    respuesta = client.post("/pages", json=payload)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_brief(client, nombre="agente-hitos", texto="Propón hitos, no adjetivos."):
    """La nota de instrucciones del agente, creada **desde la app**.

    Crear la nota «Agentes» en la raíz y esta como hija deja el fichero en
    `Notas/Agentes/<nombre>.md`, que es donde nace una nota nueva de la app.
    """
    carpeta = crear_nota(client, title="Agentes")
    return crear_nota(client, title=nombre, body=texto, parent_id=carpeta["id"])


def proponer(client, page_id, headers=HITOS, **cuerpo):
    cuerpo.setdefault("kind", "edit")
    cuerpo.setdefault("page_id", page_id)
    return client.post("/agent/suggestions", json=cuerpo, headers=headers)


# --------------------------------------------------------------------------
# 1. identidad
# --------------------------------------------------------------------------


def test_sin_token_es_401(client):
    nota = crear_nota(client)
    assert client.get(f"/agent/context?page_id={nota['id']}").status_code == 401
    assert client.post("/agent/suggestions", json={"page_id": nota["id"]}).status_code == 401
    assert client.get("/agent/suggestions").status_code == 401


def test_token_invalido_es_401_y_no_distingue_del_ausente(client):
    nota = crear_nota(client)
    sin = client.get(f"/agent/context?page_id={nota['id']}")
    malo = client.get(
        f"/agent/context?page_id={nota['id']}", headers={"X-Agent-Token": "no-existe"}
    )
    assert sin.status_code == malo.status_code == 401
    # Mismo mensaje: no se filtra si el token existía.
    assert sin.json() == malo.json()


def test_sin_agentes_configurados_las_rutas_no_existen(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_tokens", "")
    nota = crear_nota(client)
    for headers in ({}, HITOS, {"X-Agent-Token": "cualquiera"}):
        respuesta = client.get(f"/agent/context?page_id={nota['id']}", headers=headers)
        assert respuesta.status_code == 404, (headers, respuesta.text)
    assert client.get("/agent/suggestions", headers=HITOS).status_code == 404
    assert (
        client.post(
            "/agent/suggestions", json={"page_id": nota["id"]}, headers=HITOS
        ).status_code
        == 404
    )


def test_el_agente_sale_del_token_aunque_el_cuerpo_diga_otro(client):
    nota = crear_nota(client, body="Hola mundo")
    respuesta = proponer(
        client,
        nota["id"],
        agent="otro-agente",          # se ignora: lo pone el token
        old_text="Hola mundo",
        new_text="Hola, mundo",
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["agent"] == "agente-hitos"

    # Cada token es su propio agente.
    otro = proponer(
        client, nota["id"], headers=REVISION, agent="agente-hitos",
        old_text="mundo", new_text="planetilla",
    )
    assert otro.status_code == 200, otro.text
    assert otro.json()["agent"] == "agente-revision"


def test_token_valido_en_las_tres_rutas(client):
    nota = crear_nota(client, body="una nota")
    assert client.get("/agent/suggestions", headers=HITOS).status_code == 200
    assert proponer(client, nota["id"], old_text="una nota", new_text="otra").status_code == 200


# --------------------------------------------------------------------------
# 2. leer contexto
# --------------------------------------------------------------------------


def test_contexto_trae_nota_enlaces_backlinks_y_brief(client):
    # El orden importa: un wikilink se resuelve al guardar la página en la que
    # está, así que `Proyecto` tiene que existir antes de que `Enlazante` la
    # nombre en su cuerpo.
    destino = crear_nota(client, title="Destino", body="lo que sea")
    brief = crear_brief(client)
    nota = crear_nota(
        client,
        title="Proyecto",
        body="Empieza aquí.\n\nVer [[Destino]] y [[No existe|un alias]].",
    )
    enlazante = crear_nota(client, title="Enlazante", body="ver [[Proyecto]]")
    assert enlazante["id"] and destino["id"] and brief["id"]

    respuesta = client.get(f"/agent/context?page_id={nota['id']}", headers=HITOS)
    assert respuesta.status_code == 200, respuesta.text
    contexto = respuesta.json()

    assert contexto["page"]["id"] == nota["id"]
    assert contexto["page"]["title"] == "Proyecto"
    assert contexto["page"]["path"] == "Notas/Proyecto.md"
    assert contexto["page"]["type"] == "note"
    assert contexto["body"] == "Empieza aquí.\n\nVer [[Destino]] y [[No existe|un alias]]."
    assert contexto["body_truncated"] is False

    assert contexto["backlinks"] == [{"id": enlazante["id"], "title": "Enlazante"}]
    assert contexto["links"] == [
        {"title": "Destino", "exists": True},
        {"title": "No existe", "exists": False},
    ]
    assert contexto["brief"] == "Propón hitos, no adjetivos."
    assert contexto["suggestions"] == []


def test_contexto_por_path_y_sus_errores(client):
    nota = crear_nota(client, body="cuerpo")
    respuesta = client.get("/agent/context?path=Notas/Proyecto.md", headers=HITOS)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["page"]["id"] == nota["id"]

    # Una ruta fuera del vault no se resuelve: 400, no un 500.
    fuera = client.get("/agent/context?path=../fuera.md", headers=HITOS)
    assert fuera.status_code == 400
    # Ni con las dos formas ni con ninguna: 400.
    assert client.get(f"/agent/context?path=Notas/Proyecto.md&page_id={nota['id']}",
                      headers=HITOS).status_code == 400
    assert client.get("/agent/context", headers=HITOS).status_code == 400
    # Una nota que no está: 404.
    assert client.get("/agent/context?path=Notas/Nada.md", headers=HITOS).status_code == 404


def test_contexto_incluye_una_rechazada_con_su_respuesta(client):
    nota = crear_nota(client, body="Hola mundo")
    sugerencia = proponer(
        client, nota["id"], old_text="Hola mundo", new_text="Adiós",
        rationale="prueba",
    ).json()
    client.post(f"/suggestions/{sugerencia['id']}/reject", json={"reason": "no me gusta"})
    client.post(
        f"/suggestions/{sugerencia['id']}/reply",
        json={"text": "mejor con el original"},
    )

    contexto = client.get(f"/agent/context?page_id={nota['id']}", headers=HITOS).json()
    [vuelta] = contexto["suggestions"]
    assert vuelta["id"] == sugerencia["id"]
    assert vuelta["status"] == "rejected"
    assert vuelta["reason"] == "no me gusta"
    assert vuelta["agent"] == "agente-hitos"
    assert vuelta["path"] == "Notas/Proyecto.md"
    assert vuelta["old_text"] == "Hola mundo"
    assert vuelta["new_text"] == "Adiós"
    assert [r["text"] for r in vuelta["replies"]] == ["mejor con el original"]
    assert vuelta["replies"][0]["author"]


def test_contexto_recorta_el_cuerpo_y_lo_dice(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_context_max_body", 100)
    nota = crear_nota(client, body="á" * 200)     # 400 bytes UTF-8
    contexto = client.get(f"/agent/context?page_id={nota['id']}", headers=HITOS).json()
    assert contexto["body_truncated"] is True
    assert contexto["body"] == "á" * 50            # 100 bytes, cortado por carácter
    assert len(contexto["body"].encode("utf-8")) == 100


def test_brief_a_mano_en_la_raiz_del_vault(client, vault_path):
    """`Agentes/<nombre>.md` en la raíz también cuenta (la ruta de la tarjeta)."""
    (vault_path / "Agentes").mkdir()
    (vault_path / "Agentes" / "agente-hitos.md").write_text(
        "---\nid: 8f14e45f-ea3a-4e2a-9d5f-000000000001\n---\n\nSé breve.\n",
        encoding="utf-8",
    )
    assert client.post("/reindex").status_code == 200
    nota = crear_nota(client, body="x")
    contexto = client.get(f"/agent/context?page_id={nota['id']}", headers=HITOS).json()
    assert contexto["brief"].strip() == "Sé breve."


def test_contexto_sin_brief_es_null(client):
    nota = crear_nota(client)
    assert client.get(f"/agent/context?page_id={nota['id']}", headers=HITOS).json()["brief"] is None


# --------------------------------------------------------------------------
# 3. proponer
# --------------------------------------------------------------------------


def test_nueva_pagina_propuesta(client):
    nota = crear_nota(client)
    respuesta = proponer(
        client, nota["id"], kind="new_page", title="Hitos", new_text="# Hitos\n- uno"
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["kind"] == "new_page"
    assert respuesta.json()["agent"] == "agente-hitos"

    contexto = client.get(f"/agent/context?page_id={nota['id']}", headers=HITOS).json()
    assert [(s["kind"], s["title"], s["status"]) for s in contexto["suggestions"]] == [
        ("new_page", "Hitos", "pending")
    ]


def test_la_sugerencia_nace_orphaned_si_el_ancla_no_esta(client):
    nota = crear_nota(client, body="Hola mundo")
    respuesta = proponer(client, nota["id"], old_text="no está", new_text="x")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["status"] == "orphaned"


def test_tamanos_413_y_nota_inexistente_404(client):
    nota = crear_nota(client, body="Hola")
    grande = "x" * (8 * 1024 + 1)
    assert proponer(client, nota["id"], old_text=grande, new_text="y").status_code == 413
    assert proponer(client, nota["id"], old_text="Hola", new_text=grande).status_code == 413
    assert proponer(
        client, nota["id"], kind="new_page", title="T", new_text="x" * (32 * 1024 + 1)
    ).status_code == 413
    assert proponer(
        client, "00000000-0000-0000-0000-000000000000", old_text="a", new_text="b"
    ).status_code == 404


def test_tope_de_pendientes_por_nota_429(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_max_pending_note", 2)
    nota = crear_nota(client, body="Hola mundo")
    for i in range(2):
        assert proponer(client, nota["id"], old_text="Hola", new_text=f"Adiós {i}").status_code == 200

    tercera = proponer(client, nota["id"], old_text="Hola", new_text="otra")
    assert tercera.status_code == 429, tercera.text
    assert "tope 2" in tercera.json()["detail"]

    # El tope es de la NOTA: otro agente tampoco puede añadir ruido.
    otra = proponer(
        client, nota["id"], headers=REVISION, old_text="Hola", new_text="otra"
    )
    assert otra.status_code == 429

    # Resolver una la libra: aceptar la primera deja sitio.
    pendientes = client.get(f"/pages/{nota['id']}/suggestions?status=pending").json()
    assert client.post(f"/suggestions/{pendientes[0]['id']}/accept").status_code == 200
    assert proponer(client, nota["id"], old_text="Adiós", new_text="Venga").status_code == 200


def test_tope_de_pendientes_por_agente_429(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_max_pending_note", 10)
    monkeypatch.setattr(settings, "agent_max_pending_agent", 2)
    una = crear_nota(client, title="Una", body="cuerpo")
    otra = crear_nota(client, title="Otra", body="cuerpo")
    tercera = crear_nota(client, title="Tercera", body="cuerpo")
    for nota in (una, otra):
        assert proponer(client, nota["id"], old_text="cuerpo", new_text="otro").status_code == 200

    # En una nota distinta y sin llegar a su tope: el que muerde es el del agente.
    respuesta = proponer(client, tercera["id"], old_text="cuerpo", new_text="otro")
    assert respuesta.status_code == 429, respuesta.text
    assert "agente-hitos" in respuesta.json()["detail"]
    # Y el otro agente no paga el tope del primero.
    assert proponer(
        client, tercera["id"], headers=REVISION, old_text="cuerpo", new_text="otro"
    ).status_code == 200


def test_una_sugerencia_deja_una_linea_en_el_log_sin_el_texto(client, caplog):
    nota = crear_nota(client, body="Hola mundo")
    with caplog.at_level("INFO", logger="mdm.agents"):
        respuesta = proponer(
            client, nota["id"], old_text="Hola mundo", new_text="SECRETO-DEL-PARCHE"
        )
    assert respuesta.status_code == 200, respuesta.text
    lineas = [r.getMessage() for r in caplog.records if "sugerencia" in r.getMessage()]
    assert len(lineas) == 1, lineas
    linea = lineas[0]
    assert respuesta.json()["id"] in linea
    assert "agente-hitos" in linea
    assert "Notas/Proyecto.md" in linea
    # Ni el texto del parche ni el token: el log dice quién y cuánto, no qué.
    assert "SECRETO-DEL-PARCHE" not in linea
    assert "tok-de-hitos" not in caplog.text


def test_el_token_no_aparece_en_ningun_log(client, caplog):
    nota = crear_nota(client, body="Hola mundo")
    with caplog.at_level("DEBUG"):
        client.get(f"/agent/context?page_id={nota['id']}")                       # 401
        client.get(f"/agent/context?page_id={nota['id']}", headers=HITOS)        # 200
        client.get("/agent/context", headers={"X-Agent-Token": "tok-de-hitos-malo"})
        proponer(client, nota["id"], old_text="Hola mundo", new_text="Adiós")
        client.get("/agent/suggestions", headers=HITOS)
    assert "tok-de-hitos" not in caplog.text
    assert "tok-de-revision" not in caplog.text
    for record in caplog.records:
        assert "tok-de-hitos" not in record.getMessage()
        assert "no-existe" not in record.getMessage()


# --------------------------------------------------------------------------
# 4. ver lo decidido
# --------------------------------------------------------------------------


def test_solo_devuelve_las_del_agente_del_token(client):
    nota = crear_nota(client, body="Hola mundo")
    mia = proponer(client, nota["id"], old_text="Hola", new_text="Adiós").json()
    ajena = proponer(
        client, nota["id"], headers=REVISION, old_text="mundo", new_text="planeta"
    ).json()

    de_hitos = client.get("/agent/suggestions", headers=HITOS).json()
    assert [s["id"] for s in de_hitos] == [mia["id"]]
    assert de_hitos[0]["path"] == "Notas/Proyecto.md"

    de_revision = client.get("/agent/suggestions", headers=REVISION).json()
    assert [s["id"] for s in de_revision] == [ajena["id"]]


def test_lo_aceptado_por_el_dueno_se_ve_por_el_agente(client):
    nota = crear_nota(client, body="Hola mundo")
    sugerencia = proponer(
        client, nota["id"], old_text="Hola mundo", new_text="Hola, mundo",
        rationale="falta la coma",
    ).json()
    assert client.get("/agent/suggestions?status=accepted", headers=HITOS).json() == []

    assert client.post(f"/suggestions/{sugerencia['id']}/accept").status_code == 200
    client.post(
        f"/suggestions/{sugerencia['id']}/reply", json={"text": "aplicada, gracias"}
    )

    aceptadas = client.get("/agent/suggestions?status=accepted", headers=HITOS).json()
    [vuelta] = aceptadas
    assert vuelta["id"] == sugerencia["id"]
    assert vuelta["status"] == "accepted"
    assert vuelta["decided_at"] is not None
    assert [r["text"] for r in vuelta["replies"]] == ["aplicada, gracias"]
    assert client.get("/agent/suggestions?status=pending", headers=HITOS).json() == []
    assert client.get("/agent/suggestions?status=rejected", headers=HITOS).json() == []


def test_since_filtra_por_la_ultima_actividad(client, monkeypatch):
    reloj = [datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(repository, "_now", lambda: reloj[0])
    nota = crear_nota(client, body="Hola mundo")

    primera = proponer(client, nota["id"], old_text="Hola", new_text="Adiós").json()

    # El dueño responde una hora después: eso es «lo último que pasó».
    reloj[0] += timedelta(hours=1)
    client.post(f"/suggestions/{primera['id']}/reply", json={"text": "lo miro"})

    # Y una segunda sugerencia, otra hora después.
    reloj[0] += timedelta(hours=1)
    segunda = proponer(client, nota["id"], old_text="mundo", new_text="planetilla").json()

    def ids(desde):
        respuesta = client.get("/agent/suggestions", params={"since": desde}, headers=HITOS)
        assert respuesta.status_code == 200, respuesta.text
        return {s["id"] for s in respuesta.json()}

    # Antes de todo: las dos.
    assert ids("2026-01-01T00:00:00Z") == {primera["id"], segunda["id"]}
    # Entre la creación de la primera (10:00) y la respuesta (11:00): la
    # primera sigue contando, porque su última actividad es la respuesta.
    assert ids("2026-01-01T10:45:00Z") == {primera["id"], segunda["id"]}
    # Pasada la respuesta, la primera deja de ser novedad; la segunda (12:00) sí.
    assert ids("2026-01-01T11:30:00Z") == {segunda["id"]}
    # Después de todo: ninguna.
    assert ids("2026-01-01T13:00:00Z") == set()
    # Sin `Z`, con desfase explícito, también vale.
    assert ids("2026-01-01T13:00:00+00:00") == set()
    # Un `since` que no es una fecha es 400, no una lista vacía silenciosa.
    assert client.get("/agent/suggestions?since=ayer", headers=HITOS).status_code == 400


def test_status_desconocido_es_422(client):
    assert client.get("/agent/suggestions?status=inventado", headers=HITOS).status_code == 422


def test_un_sidecar_roto_no_tumba_el_contexto(client, vault_path):
    """Un `.json` tocado a mano no puede dejar la nota sin contexto."""
    nota = crear_nota(client, title="Hitos", body="Hola mundo")
    buena = proponer(client, nota["id"], old_text="Hola", new_text="Adiós").json()
    carpeta = vault_path / "Notas" / "Hitos.md.sugerencias"
    (carpeta / "basura.json").write_text("{no es json", encoding="utf-8")
    (carpeta / "sin-fechas.json").write_text(
        json.dumps({"id": "11111111-1111-1111-1111-111111111111",
                    "agent": "agente-hitos", "status": "pending"}),
        encoding="utf-8",
    )

    contexto = client.get(f"/agent/context?page_id={nota['id']}", headers=HITOS)
    assert contexto.status_code == 200, contexto.text
    por_id = {s["id"]: s for s in contexto.json()["suggestions"]}
    assert buena["id"] in por_id
    # La ilegible se descarta; la legible sin fecha ni `page_id` válido sale
    # con `null` en los dos, no con un 500 de validación.
    sin_fechas = por_id["11111111-1111-1111-1111-111111111111"]
    assert sin_fechas["created_at"] is None
    assert sin_fechas["page_id"] is None

    listado = client.get("/agent/suggestions", headers=HITOS)
    assert listado.status_code == 200, listado.text
