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

from app.vault import fm, reader, suggestions, writer


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


def test_mover_la_nota_de_carpeta_se_lleva_sus_sugerencias(client, vault_path):
    madre = crear_nota(client, title="Madre", body="")
    hija = crear_nota(client, title="Hija", body="texto")
    sugerencia = sugerir(client, hija["id"], old_text="texto", new_text="otro").json()

    respuesta = client.patch(f"/pages/{hija['id']}", json={"parent_id": madre["id"]})
    assert respuesta.status_code == 200, respuesta.text

    movida = vault_path / "Notas" / "Madre" / "Hija.md.sugerencias"
    assert not sidecar_de(vault_path, "Hija").exists()
    assert (movida / f"{sugerencia['id']}.json").is_file()
    assert [s["id"] for s in client.get(f"/pages/{hija['id']}/suggestions").json()] == [
        sugerencia["id"]
    ]


def test_reject_sin_cuerpo_tambien_vale(client):
    nota = crear_nota(client, body="texto")
    sugerencia = sugerir(client, nota["id"], old_text="texto", new_text="otro").json()
    respuesta = client.post(f"/suggestions/{sugerencia['id']}/reject")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["status"] == "rejected"
    assert respuesta.json()["reason"] is None


# --------------------------------------------------------------------------
# ronda 2: la papelera, las filas y los topes en cualquier `kind`
# --------------------------------------------------------------------------


def crear_fila(client, titulo="Fila", base="Base") -> tuple[dict, dict]:
    database = client.post("/databases", json={"title": base, "schema_def": []})
    assert database.status_code == 200, database.text
    fila = client.post(
        f"/databases/{database.json()['id']}/rows", json={"title": titulo}
    )
    assert fila.status_code == 200, fila.text
    return database.json(), fila.json()


def test_la_papelera_lista_la_nota_una_vez_y_no_restaura_un_sidecar_suelto(client, vault_path):
    """Nota + 2 sugerencias borradas = **una** entrada, y el `.json` no se restaura solo."""
    nota = crear_nota(client, title="ConHilos", body="texto")
    una = sugerir(client, nota["id"], old_text="texto", new_text="otro").json()
    dos = sugerir(client, nota["id"], old_text="texto", new_text="otro más").json()

    assert client.delete(f"/pages/{nota['id']}").status_code == 204

    entradas = client.get("/trash").json()
    assert [e["original_path"] for e in entradas] == ["Notas/ConHilos.md"], (
        "el sidecar no es una nota suelta: no puede salir como entrada de papelera"
    )

    # Restaurar un `.json` por separado se rechaza: dejaría un sidecar huérfano.
    suelto = next(vault_path.glob(".trash/*/Notas/ConHilos.md.sugerencias/*.json"))
    rel_suelto = suelto.relative_to(vault_path).as_posix()
    rechazo = client.post("/trash/restore", json={"path": rel_suelto})
    assert rechazo.status_code == 400, rechazo.text
    assert "sugerencia" in rechazo.json()["detail"]
    assert suelto.is_file()
    assert not (vault_path / "Notas" / "ConHilos.md.sugerencias").exists()

    # Y restaurar la nota devuelve las dos con ella.
    vuelta = client.post("/trash/restore", json={"path": entradas[0]["path"]})
    assert vuelta.status_code == 200, vuelta.text
    en_sitio = sorted(p.name for p in sidecar_de(vault_path, "ConHilos").iterdir())
    assert en_sitio == sorted([f"{una['id']}.json", f"{dos['id']}.json"])


def test_renombrar_una_fila_se_lleva_sus_sugerencias(client, vault_path):
    database, fila = crear_fila(client, titulo="Fila")
    sugerencia = sugerir(
        client,
        fila["id"],
        kind="new_page",
        title="Nota propuesta",
        new_text="cuerpo propuesto",
    ).json()
    viejo = vault_path / "Bases" / "Base" / "Fila.md.sugerencias"
    assert (viejo / f"{sugerencia['id']}.json").is_file()

    renombrada = client.patch(
        f"/databases/{database['id']}/rows/{fila['id']}",
        json={"title": "Fila renombrada"},
    )
    assert renombrada.status_code == 200, renombrada.text

    nuevo = vault_path / "Bases" / "Base" / "Fila renombrada.md.sugerencias"
    assert not viejo.exists()
    assert (nuevo / f"{sugerencia['id']}.json").is_file()
    # Y la sigue viendo su nota: si el sidecar se queda atrás, el listado sale [].
    listada = client.get(f"/pages/{fila['id']}/suggestions").json()
    assert [s["id"] for s in listada] == [sugerencia["id"]]


def test_renombrar_la_base_se_lleva_las_sugerencias_de_la_propia_base(client, vault_path):
    database = client.post("/databases", json={"title": "Base", "schema_def": []})
    assert database.status_code == 200, database.text
    base = database.json()
    sugerencia = sugerir(
        client, base["id"], kind="new_page", title="Otra", new_text="cuerpo"
    ).json()
    assert (vault_path / "Bases" / "Base.md.sugerencias" / f"{sugerencia['id']}.json").is_file()

    renombrada = client.patch(f"/databases/{base['id']}", json={"title": "Base dos"})
    assert renombrada.status_code == 200, renombrada.text

    assert not (vault_path / "Bases" / "Base.md.sugerencias").exists()
    assert (
        vault_path / "Bases" / "Base dos.md.sugerencias" / f"{sugerencia['id']}.json"
    ).is_file()
    assert [s["id"] for s in client.get(f"/pages/{base['id']}/suggestions").json()] == [
        sugerencia["id"]
    ]


def test_borrar_una_fila_se_lleva_sus_sugerencias_a_la_papelera(client, vault_path):
    database, fila = crear_fila(client, titulo="Fila")
    sugerencia = sugerir(
        client, fila["id"], kind="new_page", title="Otra", new_text="cuerpo"
    ).json()

    assert client.delete(f"/databases/{database['id']}/rows/{fila['id']}").status_code == 204

    assert not (vault_path / "Bases" / "Base" / "Fila.md.sugerencias").exists()
    en_papelera = list(
        vault_path.glob(".trash/*/Bases/Base/Fila.md.sugerencias/*.json")
    )
    assert [p.name for p in en_papelera] == [f"{sugerencia['id']}.json"]
    # La fila es una entrada de papelera; sus sugerencias, no.
    assert [e["original_path"] for e in client.get("/trash").json()] == [
        "Bases/Base/Fila.md"
    ]


def test_los_topes_valen_en_cualquier_kind_y_para_agent_y_reason(client):
    nota = crear_nota(client, body="texto")

    # `title` tiene tope también en un `edit` (el ADR no distingue).
    assert sugerir(
        client, nota["id"], old_text="texto", new_text="otro", title="T" * 201
    ).status_code == 413
    # `agent` es autoría, pero se escribe en el `.json`: mismo tope.
    assert sugerir(
        client, nota["id"], agent="a" * 201, old_text="texto", new_text="otro"
    ).status_code == 413
    # Los 200 bytes justos sí caben.
    assert sugerir(
        client, nota["id"], old_text="texto", new_text="otro", title="T" * 200
    ).status_code == 200

    sugerencia = sugerir(client, nota["id"], old_text="texto", new_text="otro").json()
    # El motivo del rechazo y el nombre de quien responde también van al vault.
    assert client.post(
        f"/suggestions/{sugerencia['id']}/reject",
        json={"reason": "x" * (4 * 1024 + 1)},
    ).status_code == 413
    assert client.post(
        f"/suggestions/{sugerencia['id']}/reply",
        json={"text": "hola", "author": "a" * 201},
    ).status_code == 413
    assert client.post(
        f"/suggestions/{sugerencia['id']}/reject",
        json={"reason": "x" * (4 * 1024)},
    ).status_code == 200


# --------------------------------------------------------------------------
# ronda 3: todas las operaciones que mueven o borran un `.md`
# --------------------------------------------------------------------------


def sidecars_huerfanos(vault_path: Path) -> list[str]:
    """Carpetas `*.md.sugerencias/` cuyo `.md` no está a su lado.

    Es el invariante de P10a: el sidecar no es una nota, así que no puede
    sobrevivir a la suya. Se recorre el vault **y** `.trash/`: también en la
    papelera el `.json` tiene que estar junto al `.md` del que salió.
    """
    huerfanos: list[str] = []
    for carpeta in sorted(vault_path.rglob("*.md.sugerencias")):
        if not carpeta.is_dir():
            continue
        nota = carpeta.with_name(carpeta.name[: -len(".sugerencias")])
        if not nota.is_file():
            huerfanos.append(carpeta.relative_to(vault_path).as_posix())
    return huerfanos


# Una función por operación: crea su propio mundo, la ejecuta y comprueba
# dónde quedó el sidecar. Todas se pasan por la tabla de arriba abajo.

def _op_renombrar_nota(client, vault_path):
    nota = crear_nota(client, title="OpRenombra", body="texto")
    sugerencia = sugerir(client, nota["id"], old_text="texto", new_text="otro").json()
    respuesta = client.patch(f"/pages/{nota['id']}", json={"title": "OpRenombra dos"})
    assert respuesta.status_code == 200, respuesta.text
    assert (sidecar_de(vault_path, "OpRenombra dos") / f"{sugerencia['id']}.json").is_file()


def _op_mover_nota_de_carpeta(client, vault_path):
    madre = crear_nota(client, title="OpMadre", body="")
    hija = crear_nota(client, title="OpHija", body="texto")
    sugerencia = sugerir(client, hija["id"], old_text="texto", new_text="otro").json()
    respuesta = client.patch(f"/pages/{hija['id']}", json={"parent_id": madre["id"]})
    assert respuesta.status_code == 200, respuesta.text
    movido = vault_path / "Notas" / "OpMadre" / "OpHija.md.sugerencias"
    assert (movido / f"{sugerencia['id']}.json").is_file()


def _op_renombrar_fila(client, vault_path):
    database, fila = crear_fila(client, titulo="OpFila", base="OpBase")
    sugerencia = sugerir(
        client, fila["id"], kind="new_page", title="Nueva", new_text="cuerpo"
    ).json()
    respuesta = client.patch(
        f"/databases/{database['id']}/rows/{fila['id']}", json={"title": "OpFila dos"}
    )
    assert respuesta.status_code == 200, respuesta.text
    movido = vault_path / "Bases" / "OpBase" / "OpFila dos.md.sugerencias"
    assert (movido / f"{sugerencia['id']}.json").is_file()


def _op_renombrar_base(client, vault_path):
    base = client.post("/databases", json={"title": "OpBase dos", "schema_def": []}).json()
    sugerencia = sugerir(
        client, base["id"], kind="new_page", title="Nueva", new_text="cuerpo"
    ).json()
    respuesta = client.patch(f"/databases/{base['id']}", json={"title": "OpBase tres"})
    assert respuesta.status_code == 200, respuesta.text
    movido = vault_path / "Bases" / "OpBase tres.md.sugerencias"
    assert (movido / f"{sugerencia['id']}.json").is_file()


def _op_borrar_nota(client, vault_path):
    nota = crear_nota(client, title="OpBorra", body="texto")
    sugerir(client, nota["id"], old_text="texto", new_text="otro")
    assert client.delete(f"/pages/{nota['id']}").status_code == 204
    assert list(vault_path.glob(".trash/*/Notas/OpBorra.md.sugerencias/*.json"))


def _op_borrar_nota_con_subarbol(client, vault_path):
    madre = crear_nota(client, title="OpMadreBorra", body="")
    hija = crear_nota(client, title="OpHijaBorra", body="texto")
    assert client.patch(
        f"/pages/{hija['id']}", json={"parent_id": madre["id"]}
    ).status_code == 200
    sugerir(client, hija["id"], old_text="texto", new_text="otro")
    assert client.delete(f"/pages/{madre['id']}").status_code == 204
    assert list(
        vault_path.glob(".trash/*/Notas/OpMadreBorra/OpHijaBorra.md.sugerencias/*.json")
    )


def _op_borrar_fila(client, vault_path):
    database, fila = crear_fila(client, titulo="OpFilaBorra", base="OpBaseBorra")
    sugerir(client, fila["id"], kind="new_page", title="Nueva", new_text="cuerpo")
    assert client.delete(f"/databases/{database['id']}/rows/{fila['id']}").status_code == 204
    assert list(vault_path.glob(".trash/*/Bases/OpBaseBorra/OpFilaBorra.md.sugerencias/*.json"))


def _op_borrar_base(client, vault_path):
    database, fila = crear_fila(client, titulo="OpFilaBase", base="OpBaseBorraDos")
    sugerir(client, fila["id"], kind="new_page", title="Nueva", new_text="cuerpo")
    assert client.delete(f"/databases/{database['id']}").status_code == 204
    assert list(
        vault_path.glob(".trash/*/Bases/OpBaseBorraDos/OpFilaBase.md.sugerencias/*.json")
    )


def _op_restaurar_nota(client, vault_path):
    nota = crear_nota(client, title="OpRestaura", body="texto")
    sugerencia = sugerir(client, nota["id"], old_text="texto", new_text="otro").json()
    assert client.delete(f"/pages/{nota['id']}").status_code == 204
    entrada = next(
        e for e in client.get("/trash").json() if e["original_path"].endswith("OpRestaura.md")
    )
    assert client.post("/trash/restore", json={"path": entrada["path"]}).status_code == 200
    assert (sidecar_de(vault_path, "OpRestaura") / f"{sugerencia['id']}.json").is_file()


def _op_restaurar_subpagina(client, vault_path):
    madre = crear_nota(client, title="OpMadreRestaura", body="")
    hija = crear_nota(client, title="OpHijaRestaura", body="texto")
    assert client.patch(
        f"/pages/{hija['id']}", json={"parent_id": madre["id"]}
    ).status_code == 200
    sugerencia = sugerir(client, hija["id"], old_text="texto", new_text="otro").json()
    assert client.delete(f"/pages/{madre['id']}").status_code == 204
    entrada = next(
        e for e in client.get("/trash").json()
        if e["original_path"] == "Notas/OpMadreRestaura/OpHijaRestaura.md"
    )
    assert client.post("/trash/restore", json={"path": entrada["path"]}).status_code == 200
    vuelto = vault_path / "Notas" / "OpMadreRestaura" / "OpHijaRestaura.md.sugerencias"
    assert (vuelto / f"{sugerencia['id']}.json").is_file()


def _op_renombrar_con_writer(client, vault_path):
    """La vía vieja (`writer.rename_page`), que nadie llama pero existe."""
    nota = crear_nota(client, title="OpWriter", body="texto")
    sugerencia = sugerir(client, nota["id"], old_text="texto", new_text="otro").json()
    rel = "Notas/OpWriter.md"
    pagina = reader.read_file(str(vault_path / rel), rel)
    writer.rename_page(str(vault_path), pagina, "OpWriter dos")
    assert (sidecar_de(vault_path, "OpWriter dos") / f"{sugerencia['id']}.json").is_file()


def _op_borrar_con_writer(client, vault_path):
    nota = crear_nota(client, title="OpWriterBorra", body="texto")
    sugerir(client, nota["id"], old_text="texto", new_text="otro")
    rel = "Notas/OpWriterBorra.md"
    pagina = reader.read_file(str(vault_path / rel), rel)
    destino = writer.delete_page(str(vault_path), pagina)
    assert (vault_path / f"{destino}.sugerencias").is_dir()


def _op_purgar_con_el_helper(client, vault_path):
    """Purga definitiva: hoy no hay endpoint, pero el helper ya la cubre."""
    nota = crear_nota(client, title="OpPurga", body="texto")
    sugerir(client, nota["id"], old_text="texto", new_text="otro")
    assert suggestions.remove_with_note(str(vault_path), "Notas/OpPurga.md") is True
    assert not (vault_path / "Notas" / "OpPurga.md").exists()
    assert not sidecar_de(vault_path, "OpPurga").exists()


def test_ninguna_operacion_del_vault_deja_un_sidecar_sin_su_nota(client, vault_path):
    """La lista completa, y el barrido del vault después de cada una."""
    operaciones = [
        ("renombrar la nota (save)", _op_renombrar_nota),
        ("mover la nota de carpeta (save)", _op_mover_nota_de_carpeta),
        ("renombrar una fila (save_row)", _op_renombrar_fila),
        ("renombrar una base (save_database)", _op_renombrar_base),
        ("borrar una nota (delete)", _op_borrar_nota),
        ("borrar una nota con subárbol (delete)", _op_borrar_nota_con_subarbol),
        ("borrar una fila (delete_row)", _op_borrar_fila),
        ("borrar una base (delete_database)", _op_borrar_base),
        ("restaurar una nota (restore)", _op_restaurar_nota),
        ("restaurar una subpágina (restore)", _op_restaurar_subpagina),
        ("renombrar por writer.rename_page", _op_renombrar_con_writer),
        ("borrar por writer.delete_page", _op_borrar_con_writer),
        ("purgar con remove_with_note", _op_purgar_con_el_helper),
    ]
    for nombre, operacion in operaciones:
        operacion(client, vault_path)
        huerfanos = sidecars_huerfanos(vault_path)
        assert huerfanos == [], f"«{nombre}» dejó un sidecar sin su nota: {huerfanos}"


def test_borrar_una_nota_madre_se_lleva_las_sugerencias_de_sus_hijas(client, vault_path):
    """La cascada del borrado: el `.json` de la hija va con su `.md` y no deja carpeta."""
    madre = crear_nota(client, title="Otra", body="")
    hija = crear_nota(client, title="Hija", body="texto")
    assert client.patch(
        f"/pages/{hija['id']}", json={"parent_id": madre["id"]}
    ).status_code == 200
    sugerencia = sugerir(client, hija["id"], old_text="texto", new_text="otro").json()

    assert client.delete(f"/pages/{madre['id']}").status_code == 204

    # El sidecar de la hija, en la papelera y junto a su `.md`.
    en_papelera = list(
        vault_path.glob(".trash/*/Notas/Otra/Hija.md.sugerencias/*.json")
    )
    assert [p.name for p in en_papelera] == [f"{sugerencia['id']}.json"]
    assert sorted(e["original_path"] for e in client.get("/trash").json()) == [
        "Notas/Otra.md",
        "Notas/Otra/Hija.md",
    ]
    # La carpeta de la madre no sobrevive: sin sidecar huérfano, se poda.
    assert not (vault_path / "Notas" / "Otra").exists()
    # Y una nota nueva con ese título se llama «Otra», no «Otra (2)».
    crear_nota(client, title="Otra")
    assert (vault_path / "Notas" / "Otra.md").is_file()
    assert not (vault_path / "Notas" / "Otra (2).md").exists()
    # La sugerencia se fue con su nota: ya no se sirve por sid.
    assert client.get(f"/suggestions/{sugerencia['id']}").status_code == 404
    assert sidecars_huerfanos(vault_path) == []


def test_restaurar_una_subpagina_devuelve_sus_sugerencias(client, vault_path):
    madre = crear_nota(client, title="Madre", body="")
    hija = crear_nota(client, title="Hija suelta", body="texto")
    assert client.patch(
        f"/pages/{hija['id']}", json={"parent_id": madre["id"]}
    ).status_code == 200
    sugerencia = sugerir(client, hija["id"], old_text="texto", new_text="otro").json()
    assert client.delete(f"/pages/{madre['id']}").status_code == 204

    entrada = next(
        e for e in client.get("/trash").json()
        if e["original_path"] == "Notas/Madre/Hija suelta.md"
    )
    assert client.post("/trash/restore", json={"path": entrada["path"]}).status_code == 200

    vuelto = vault_path / "Notas" / "Madre" / "Hija suelta.md.sugerencias"
    assert (vuelto / f"{sugerencia['id']}.json").is_file()
    assert client.get(f"/suggestions/{sugerencia['id']}").status_code == 200
    assert sidecars_huerfanos(vault_path) == []
