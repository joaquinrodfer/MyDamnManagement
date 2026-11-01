"""P10a: sugerencias de agentes — sidecar, anclaje y API.

Lo que se fija aquí es el contrato del modelo, no la interfaz (que va en su
tarjeta): una sugerencia es un parche (`old_text` -> `new_text`) guardado en
`<nota>.md.sugerencias/<id>.json`, el `.md` **no** cambia al proponer, y
aceptar aplica el parche sobre el cuerpo actual, por la ruta normal del vault.

Los tres casos que deciden la aceptación:

1. el ancla sigue estando -> se aplica;
2. el ancla ya no está -> 409 `{"error": "desanclada"}` y `orphaned`;
3. el ancla aparece dos veces -> se aplica la más cercana a `offset` y la otra
   aparición se queda como estaba.
"""

from pathlib import Path

from app.vault import fm


# --------------------------------------------------------------------------
# ayudas
# --------------------------------------------------------------------------


def crear_nota(client, title="Proyecto", body="") -> dict:
    respuesta = client.post("/pages", json={"title": title, "body_markdown": body})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def sugerir(client, page_id, **cuerpo):
    return client.post(f"/pages/{page_id}/suggestions", json=cuerpo)


def sidecar_de(vault_path: Path, titulo: str) -> Path:
    return vault_path / "Notas" / f"{titulo}.md.sugerencias"


def cuerpo_en_disco(vault_path: Path, titulo: str) -> str:
    texto = (vault_path / "Notas" / f"{titulo}.md").read_text(encoding="utf-8")
    raw, cuerpo = fm.split_document(texto)
    assert raw is not None, "el `.md` tiene que seguir teniendo frontmatter"
    return cuerpo.strip("\n")


# --------------------------------------------------------------------------
# crear y listar
# --------------------------------------------------------------------------


def test_crear_y_listar_filtrando_por_estado(client, vault_path):
    nota = crear_nota(client, body="Hola mundo")
    creada = sugerir(
        client,
        nota["id"],
        agent="agente-1",
        kind="edit",
        old_text="Hola mundo",
        new_text="Hola, mundo",
        rationale="falta la coma",
    )
    assert creada.status_code == 200, creada.text
    sugerencia = creada.json()
    assert sugerencia["status"] == "pending"
    assert sugerencia["kind"] == "edit"
    assert sugerencia["page_id"] == nota["id"]
    assert sugerencia["agent"] == "agente-1"
    assert sugerencia["base_revision"] == nota["updated_at"]
    assert sugerencia["replies"] == []

    # Un fichero por sugerencia, en la carpeta hermana de la nota.
    fichero = sidecar_de(vault_path, "Proyecto") / f"{sugerencia['id']}.json"
    assert fichero.is_file(), sorted(p.name for p in sidecar_de(vault_path, "Proyecto").iterdir())

    rechazada = sugerir(
        client, nota["id"], agent="agente-2", old_text="Hola mundo", new_text="Adiós"
    ).json()
    client.post(f"/suggestions/{rechazada['id']}/reject", json={"reason": "no"})

    todas = client.get(f"/pages/{nota['id']}/suggestions").json()
    assert {s["id"] for s in todas} == {sugerencia["id"], rechazada["id"]}

    pendientes = client.get(f"/pages/{nota['id']}/suggestions?status=pending").json()
    assert [s["id"] for s in pendientes] == [sugerencia["id"]]
    rechazadas = client.get(f"/pages/{nota['id']}/suggestions?status=rejected").json()
    assert [s["id"] for s in rechazadas] == [rechazada["id"]]
    assert rechazadas[0]["reason"] == "no"

    # Listar una nota que no existe es 404, no una lista vacía.
    assert client.get(
        "/pages/00000000-0000-0000-0000-000000000000/suggestions"
    ).status_code == 404


def test_crear_con_un_ancla_que_no_esta_la_deja_orphaned_sin_rechazarla(client):
    nota = crear_nota(client, body="Texto actual")
    respuesta = sugerir(
        client, nota["id"], old_text="Texto de la versión anterior", new_text="Otro"
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["status"] == "orphaned"
    assert len(client.get(f"/pages/{nota['id']}/suggestions").json()) == 1


# --------------------------------------------------------------------------
# aceptar
# --------------------------------------------------------------------------


def test_aceptar_aplica_el_parche_y_el_md_queda_escrito(client, vault_path):
    nota = crear_nota(client, body="## Hitos\n\n- Uno\n- Dos\n")
    sugerencia = sugerir(
        client,
        nota["id"],
        old_text="- Dos",
        new_text="- Dos\n- Tres",
        rationale="falta el tercer hito",
    ).json()

    aceptada = client.post(f"/suggestions/{sugerencia['id']}/accept")
    assert aceptada.status_code == 200, aceptada.text
    cuerpo = aceptada.json()
    assert cuerpo["suggestion"]["status"] == "accepted"
    assert cuerpo["suggestion"]["decided_at"]
    assert cuerpo["page_id"] == nota["id"]
    assert cuerpo["created"] is False
    assert cuerpo["stale"] is False

    # En disco: el `.md` con el parche, y solo eso.
    assert cuerpo_en_disco(vault_path, "Proyecto") == "## Hitos\n\n- Uno\n- Dos\n- Tres"
    leida = client.get(f"/pages/{nota['id']}").json()
    assert leida["body_markdown"] == "## Hitos\n\n- Uno\n- Dos\n- Tres"

    # El sidecar sigue ahí, con el estado nuevo, y no dentro del `.md`.
    guardada = (
        sidecar_de(vault_path, "Proyecto") / f"{sugerencia['id']}.json"
    ).read_text(encoding="utf-8")
    assert '"status": "accepted"' in guardada
    assert "falta el tercer hito" not in (vault_path / "Notas" / "Proyecto.md").read_text(
        encoding="utf-8"
    )


def test_aceptar_dos_veces_da_409(client):
    nota = crear_nota(client, body="uno dos")
    sugerencia = sugerir(client, nota["id"], old_text="uno", new_text="1").json()
    assert client.post(f"/suggestions/{sugerencia['id']}/accept").status_code == 200
    otra = client.post(f"/suggestions/{sugerencia['id']}/accept")
    assert otra.status_code == 409
    assert otra.json()["error"] == "ya_resuelta"


def test_aceptar_sin_ancla_da_409_y_la_marca_orphaned(client, vault_path):
    nota = crear_nota(client, body="Texto que el agente vio")
    sugerencia = sugerir(
        client, nota["id"], old_text="Texto que el agente vio", new_text="Nuevo"
    ).json()
    # El dueño edita la nota antes de aceptar: el ancla desaparece.
    client.patch(f"/pages/{nota['id']}", json={"body_markdown": "Otra cosa distinta"})

    respuesta = client.post(f"/suggestions/{sugerencia['id']}/accept")
    assert respuesta.status_code == 409
    assert respuesta.json() == {"error": "desanclada"}

    listada = client.get(f"/pages/{nota['id']}/suggestions").json()
    assert listada[0]["status"] == "orphaned"
    # Y el `.md` no se ha tocado.
    assert cuerpo_en_disco(vault_path, "Proyecto") == "Otra cosa distinta"


def test_aceptar_con_dos_apariciones_elige_la_mas_cercana_a_offset(client, vault_path):
    cuerpo = "Repetido\n\nOtra cosa\n\nRepetido\n\nFin"
    nota = crear_nota(client, body=cuerpo)
    posicion = cuerpo.find("Repetido", 1)
    assert posicion > 0

    sugerencia = sugerir(
        client,
        nota["id"],
        old_text="Repetido",
        new_text="Cambiado",
        offset=posicion,
    ).json()
    assert sugerencia["offset"] == posicion

    respuesta = client.post(f"/suggestions/{sugerencia['id']}/accept")
    assert respuesta.status_code == 200, respuesta.text

    esperado = f"{cuerpo[:posicion]}Cambiado{cuerpo[posicion + len('Repetido'):]}"
    assert cuerpo_en_disco(vault_path, "Proyecto") == esperado
    # La otra aparición sigue intacta.
    assert esperado.count("Repetido") == 1
    assert esperado.startswith("Repetido\n")


def test_offset_cercano_a_la_primera_aparicion_cambia_la_primera(client, vault_path):
    cuerpo = "Repetido\n\nOtra cosa\n\nRepetido"
    nota = crear_nota(client, body=cuerpo)
    sugerencia = sugerir(
        client, nota["id"], old_text="Repetido", new_text="Cambiado", offset=0
    ).json()
    assert client.post(f"/suggestions/{sugerencia['id']}/accept").status_code == 200
    assert cuerpo_en_disco(vault_path, "Proyecto") == "Cambiado\n\nOtra cosa\n\nRepetido"


# --------------------------------------------------------------------------
# new_page
# --------------------------------------------------------------------------


def test_new_page_crea_la_nota_propuesta(client, vault_path):
    anfitriona = crear_nota(client, title="Bitácora", body="hoy")
    sugerencia = sugerir(
        client,
        anfitriona["id"],
        kind="new_page",
        title="Hitos de octubre",
        new_text="## Hitos\n\n- Uno\n- Dos",
        rationale="el listado que pediste",
    ).json()
    assert sugerencia["status"] == "pending"
    assert sugerencia["old_text"] == ""
    assert sugerencia["title"] == "Hitos de octubre"

    respuesta = client.post(f"/suggestions/{sugerencia['id']}/accept")
    assert respuesta.status_code == 200, respuesta.text
    resultado = respuesta.json()
    assert resultado["created"] is True

    nueva = client.get(f"/pages/{resultado['page_id']}").json()
    assert nueva["title"] == "Hitos de octubre"
    assert nueva["body_markdown"] == "## Hitos\n\n- Uno\n- Dos"
    assert nueva["created_by"] == "agente"
    assert (vault_path / "Notas" / "Hitos de octubre.md").is_file()
    assert cuerpo_en_disco(vault_path, "Hitos de octubre") == "## Hitos\n\n- Uno\n- Dos"


# --------------------------------------------------------------------------
# el `.md` sigue siendo Markdown limpio y el sidecar no es una nota
# --------------------------------------------------------------------------


def test_el_md_no_contiene_nada_de_la_sugerencia(client, vault_path):
    nota = crear_nota(client, body="Contenido estable")
    original = (vault_path / "Notas" / "Proyecto.md").read_text(encoding="utf-8")
    sugerencia = sugerir(
        client,
        nota["id"],
        agent="agente-x",
        old_text="Contenido estable",
        new_text="Contenido propuesto",
        rationale="un motivo muy reconocible",
    ).json()

    despues = (vault_path / "Notas" / "Proyecto.md").read_text(encoding="utf-8")
    assert despues == original, "proponer no toca el `.md`, ni para dejar una marca"
    for rastro in ("agente-x", "Contenido propuesto", "un motivo muy reconocible", sugerencia["id"]):
        assert rastro not in despues
    assert "sugerenc" not in despues.lower()
    assert cuerpo_en_disco(vault_path, "Proyecto") == "Contenido estable"


def test_la_carpeta_de_sidecars_no_se_indexa_ni_la_recorre_el_vigilante(client, vault_path):
    from app.vault import reader

    nota = crear_nota(client, body="Cuerpo")
    sugerir(client, nota["id"], old_text="Cuerpo", new_text="Cuerpo editado")

    # El índice no la ve como nota.
    titulos = [p["title"] for p in client.get("/pages").json()]
    assert "Proyecto" in titulos
    assert not any("sugerencia" in t for t in titulos)
    assert client.get("/status").json()["drift"]["changed"] == 0

    # Y el recorrido del vault (el que usan el reindexado y el vigilante) no
    # entra en ella: no hay ningún `.md` suyo.
    recorrido = [rel for rel, _st in reader.walk_markdown(str(vault_path))]
    assert recorrido == ["Notas/Proyecto.md"]


# --------------------------------------------------------------------------
# el sidecar acompaña a la nota
# --------------------------------------------------------------------------


def test_renombrar_la_nota_se_lleva_sus_sugerencias(client, vault_path):
    nota = crear_nota(client, title="Original", body="texto")
    sugerencia = sugerir(client, nota["id"], old_text="texto", new_text="otro").json()
    assert (sidecar_de(vault_path, "Original") / f"{sugerencia['id']}.json").is_file()

    respuesta = client.patch(f"/pages/{nota['id']}", json={"title": "Renombrada"})
    assert respuesta.status_code == 200, respuesta.text

    assert not sidecar_de(vault_path, "Original").exists()
    assert (
        sidecar_de(vault_path, "Renombrada") / f"{sugerencia['id']}.json"
    ).is_file()
    # Y la API las sigue encontrando por el id de la página.
    listada = client.get(f"/pages/{nota['id']}/suggestions").json()
    assert [s["id"] for s in listada] == [sugerencia["id"]]
    assert client.post(f"/suggestions/{sugerencia['id']}/accept").status_code == 200


def test_borrar_la_nota_se_lleva_sus_sugerencias_a_la_papelera(client, vault_path):
    nota = crear_nota(client, title="Temporal", body="texto")
    sugerencia = sugerir(client, nota["id"], old_text="texto", new_text="otro").json()

    assert client.delete(f"/pages/{nota['id']}").status_code == 204

    assert not (vault_path / "Notas" / "Temporal.md").exists()
    assert not sidecar_de(vault_path, "Temporal").exists()
    en_papelera = list(vault_path.glob(".trash/*/Notas/Temporal.md.sugerencias/*.json"))
    assert [p.name for p in en_papelera] == [f"{sugerencia['id']}.json"]
    assert client.get(f"/suggestions/{sugerencia['id']}").status_code == 404


def test_restaurar_la_nota_devuelve_tambien_sus_sugerencias(client, vault_path):
    nota = crear_nota(client, title="Vuelve", body="texto")
    sugerencia = sugerir(client, nota["id"], old_text="texto", new_text="otro").json()
    client.delete(f"/pages/{nota['id']}")

    entrada = next(e for e in client.get("/trash").json() if e["original_path"].endswith("Vuelve.md"))
    respuesta = client.post("/trash/restore", json={"path": entrada["path"]})
    assert respuesta.status_code == 200, respuesta.text

    assert (sidecar_de(vault_path, "Vuelve") / f"{sugerencia['id']}.json").is_file()
    assert client.get(f"/suggestions/{sugerencia['id']}").status_code == 200


# --------------------------------------------------------------------------
# hilo y límites
# --------------------------------------------------------------------------


def test_reply_añade_entradas_al_hilo_del_dueno(client):
    nota = crear_nota(client, body="texto")
    sugerencia = sugerir(
        client, nota["id"], old_text="texto", new_text="otro", rationale="por esto"
    ).json()

    respuesta = client.post(
        f"/suggestions/{sugerencia['id']}/reply", json={"text": "me suena bien"}
    )
    assert respuesta.status_code == 200, respuesta.text
    hilo = respuesta.json()["replies"]
    assert [e["text"] for e in hilo] == ["me suena bien"]
    assert hilo[0]["author"] == "Tú"
    assert hilo[0]["at"]

    otra = client.post(
        f"/suggestions/{sugerencia['id']}/reply", json={"text": "pero luego", "author": "Ana"}
    ).json()
    assert [(e["author"], e["text"]) for e in otra["replies"]] == [
        ("Tú", "me suena bien"),
        ("Ana", "pero luego"),
    ]
    # Responder no resuelve la sugerencia.
    assert otra["status"] == "pending"
    assert client.post(
        f"/suggestions/{sugerencia['id']}/reply", json={"text": "   "}
    ).status_code == 400


def test_reject_con_motivo_no_toca_la_nota(client, vault_path):
    nota = crear_nota(client, body="texto")
    original = (vault_path / "Notas" / "Proyecto.md").read_text(encoding="utf-8")
    sugerencia = sugerir(client, nota["id"], old_text="texto", new_text="otro").json()

    respuesta = client.post(
        f"/suggestions/{sugerencia['id']}/reject", json={"reason": "no me convence"}
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["status"] == "rejected"
    assert respuesta.json()["reason"] == "no me convence"
    assert (vault_path / "Notas" / "Proyecto.md").read_text(encoding="utf-8") == original

    # Rechazada no se acepta después.
    assert client.post(f"/suggestions/{sugerencia['id']}/accept").status_code == 409


def test_los_topes_de_tamano_dan_413(client):
    nota = crear_nota(client, body="texto")
    grande = "x" * (8 * 1024 + 1)
    assert sugerir(client, nota["id"], old_text=grande, new_text="y").status_code == 413
    assert sugerir(client, nota["id"], old_text="texto", new_text=grande).status_code == 413
    assert sugerir(
        client,
        nota["id"],
        kind="new_page",
        title="Enorme",
        new_text="x" * (32 * 1024 + 1),
    ).status_code == 413
    # El de `edit` no admite el tamaño de una nota entera.
    assert sugerir(
        client, nota["id"], old_text="texto", new_text="x" * (32 * 1024)
    ).status_code == 413
    # Y una sugerencia que cabe se guarda.
    assert sugerir(client, nota["id"], old_text="texto", new_text="x" * 8000).status_code == 200
