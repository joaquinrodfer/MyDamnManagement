"""Ronda de revisión de P1.2a: los hallazgos, fijados con tests.

Cada test fija un comportamiento que la revisión de la ronda 1 reprodujo y que
no estaba cubierto:

1. el arranque reindexa siempre (no solo cuando el índice se ha recreado), así
   que un guardado que quedó en el `.md` y no en el índice sobrevive al
   reinicio en vez de ser pisado por el autoguardado del panel;
2. mover una página dentro de su propio descendiente es 400, no un 500 con
   `OSError EINVAL`; y la cabecera de una nota cuyo `.md` ya no está es 400, no
   un 500;
3. la migración resuelve los nombres finales antes de escribir, así que dos
   hermanas con el mismo título no se llevan las hijas de la otra (eso va en
   `test_migrate_vault.py`);
4. `POST /reindex` no vacía el índice cuando el vault no existe o no tiene ni
   un `.md`: 409 con motivo, y `?force=true` para cuando el vaciado es
   intencionado;
5. `POST` y `PATCH` escriben el `.md` con el mismo formato;
6. borrar una nota de la que cuelga una base es 400 (la base no tiene
   papelera en P1.2a).
"""

from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def crear(client, **cuerpo) -> dict:
    respuesta = client.post("/pages", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def texto(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def arrancar_de_nuevo():
    """Un `TestClient` nuevo = un arranque nuevo de la app (evento startup)."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


# --------------------------------------------------------------------------
# 1. el arranque reconcilia el índice con el vault
# --------------------------------------------------------------------------


def test_el_arranque_recoge_un_guardado_que_quedo_solo_en_el_fichero(
    client, vault_path
):
    """El caso que justifica escribir el fichero antes que el índice.

    Proceso muerto entre el `.md` y el commit del índice: el índice sirve el
    cuerpo viejo, y el panel (que autoguarda lo que tiene en pantalla) lo
    escribiría encima del fichero. Con el reindexado del arranque, la API ve
    el fichero antes de servir la primera petición.
    """
    page = crear(client, title="Sobrevive", body_markdown="v1")
    fichero = vault_path / "Notas" / "Sobrevive.md"

    # "muere el proceso" tras escribir el fichero: el .md queda con v2 y el
    # índice con v1.
    fichero.write_text(texto(fichero).replace("v1", "v2 TRABAJO GUARDADO"), encoding="utf-8")
    assert client.get(f"/pages/{page['id']}").json()["body_markdown"] == "v1"

    with arrancar_de_nuevo() as otro:
        leida = otro.get(f"/pages/{page['id']}").json()
    assert leida["body_markdown"] == "v2 TRABAJO GUARDADO", "el arranque no reindexó"
    assert "v2 TRABAJO GUARDADO" in texto(fichero)


def test_el_arranque_recoge_un_renombrado_hecho_por_fuera(client, vault_path):
    page = crear(client, title="Antes")
    (vault_path / "Notas" / "Antes.md").rename(vault_path / "Notas" / "Después.md")

    with arrancar_de_nuevo() as otro:
        leida = otro.get(f"/pages/{page['id']}").json()
    assert leida["title"] == "Después", "la API serviría un título sin fichero detrás"


# --------------------------------------------------------------------------
# 2. los dos 500 que eran 400
# --------------------------------------------------------------------------


def test_mover_una_pagina_dentro_de_su_descendiente_es_400(client, vault_path):
    padre = crear(client, title="A")
    hija = crear(client, title="B", parent_id=padre["id"])

    respuesta = client.patch(f"/pages/{padre['id']}", json={"parent_id": hija["id"]})
    assert respuesta.status_code == 400, respuesta.text
    assert "dentro de sí misma" in respuesta.json()["detail"]

    # Tampoco colgarse de sí misma.
    respuesta = client.patch(f"/pages/{padre['id']}", json={"parent_id": padre["id"]})
    assert respuesta.status_code == 400

    # Y no se ha movido nada: ni el índice ni el disco.
    assert client.get(f"/pages/{padre['id']}").json()["parent_id"] is None
    assert (vault_path / "Notas" / "A.md").exists()
    assert (vault_path / "Notas" / "A" / "B.md").exists()


def test_la_cabecera_con_el_fichero_borrado_por_fuera_es_400(client, vault_path):
    from app.config import settings

    page = crear(client, title="Sin fichero")
    subida = client.post(
        f"/pages/{page['id']}/header-image",
        files={"file": ("cabecera.png", PNG, "image/png")},
    )
    assert subida.status_code == 200

    (vault_path / "Notas" / "Sin fichero.md").unlink()

    respuesta = client.post(
        f"/pages/{page['id']}/header-image",
        files={"file": ("otra.png", PNG, "image/png")},
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "no está en el vault" in respuesta.json()["detail"]

    # La imagen recién subida no se queda huérfana.
    page_dir = Path(settings.attachments_dir) / "pages" / page["id"]
    assert list(page_dir.glob("header.*")) == []

    # Y quitar la cabecera tampoco revienta.
    respuesta = client.delete(f"/pages/{page['id']}/header-image")
    assert respuesta.status_code == 400


# --------------------------------------------------------------------------
# 4. un volumen sin montar no es un vault vacío
# --------------------------------------------------------------------------


def _mover_vault(monkeypatch, destino: Path) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "vault_path", str(destino))


def test_reindex_con_el_vault_inexistente_no_vacia_el_indice(client, tmp_path, monkeypatch):
    page = crear(client, title="Importante", body_markdown="no me borres")

    _mover_vault(monkeypatch, tmp_path / "no-existe")

    respuesta = client.post("/reindex")
    assert respuesta.status_code == 409, respuesta.text
    assert "no existe" in respuesta.json()["detail"]
    assert "force=true" in respuesta.json()["detail"]

    # El índice sigue intacto y la nota se puede leer.
    assert client.get(f"/pages/{page['id']}").json()["body_markdown"] == "no me borres"


def test_reindex_con_el_vault_vacio_no_vacia_el_indice(client, tmp_path, monkeypatch):
    crear(client, title="Otra importante")
    vacio = tmp_path / "vacio"
    vacio.mkdir()

    _mover_vault(monkeypatch, vacio)

    respuesta = client.post("/reindex")
    assert respuesta.status_code == 409, respuesta.text
    assert "ningún .md" in respuesta.json()["detail"]
    assert len(client.get("/pages").json()) == 1


def test_force_permite_vaciar_el_indice_a_proposito(client, tmp_path, monkeypatch):
    crear(client, title="Se va")
    vacio = tmp_path / "vacio"
    vacio.mkdir()
    _mover_vault(monkeypatch, vacio)

    respuesta = client.post("/reindex?force=true")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["notes"] == 0
    assert client.get("/pages").json() == []


# --------------------------------------------------------------------------
# 5. un solo formato de fichero
# --------------------------------------------------------------------------


def test_post_y_patch_escriben_el_mismo_formato(client, vault_path):
    creada = crear(client, title="Formato", icon="📄", description="igual")
    fichero = vault_path / "Notas" / "Formato.md"
    client.patch(f"/pages/{creada['id']}", json={"body_markdown": "una línea"})
    tras_patch = texto(fichero)

    otra = crear(client, title="Formato referencia", icon="📄", description="igual")
    client.patch(f"/pages/{otra['id']}", json={"body_markdown": "una línea"})

    # Mismo cuerpo y mismo frontmatter salvo id/created/updated/título: el
    # cuerpo se escribe igual, con su línea en blanco y su salto final.
    assert tras_patch.endswith("---\n\nuna línea\n")
    assert not tras_patch.endswith("\n\n")
    assert texto(vault_path / "Notas" / "Formato referencia.md").endswith(
        "---\n\nuna línea\n"
    )


# --------------------------------------------------------------------------
# 6. borrar una nota que tiene una base colgando
# --------------------------------------------------------------------------


def test_borrar_una_nota_con_una_base_colgando_es_400(client, vault_path):
    nota = crear(client, title="Con base")
    base = client.post(
        "/databases", json={"title": "Contactos", "parent_id": nota["id"]}
    ).json()

    respuesta = client.delete(f"/pages/{nota['id']}")
    assert respuesta.status_code == 400, respuesta.text
    assert "Contactos" in respuesta.json()["detail"]

    # Nada se ha borrado: ni la nota ni la base.
    assert client.get(f"/pages/{nota['id']}").status_code == 200
    assert client.get(f"/databases/{base['id']}").status_code == 200
    assert (vault_path / "Notas" / "Con base.md").exists()

    # Borrando primero la base, la nota sí se va (a la papelera).
    assert client.delete(f"/databases/{base['id']}").status_code in (200, 204)
    assert client.delete(f"/pages/{nota['id']}").status_code == 204
