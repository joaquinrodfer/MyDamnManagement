"""El script de migración de adjuntos: mueve, actualiza referencias, es idempotente."""

import re
from pathlib import Path

from app import index as indice
from app.config import settings
from app.vault import fm, vaultfs
from scripts.migrate_attachments_into_vault import main

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def crear(client, **cuerpo) -> dict:
    respuesta = client.post("/pages", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def _apuntar_al_sitio_viejo(client, index_path: Path, fichero: Path, old_url: str) -> None:
    """Deja la nota como la dejaba P1.2a: frontmatter e índice con `/files/pages/…`."""
    texto = fichero.read_text(encoding="utf-8")
    nuevo, _avisos = fm.rewrite(texto, {"header_image": old_url})
    vaultfs.atomic_write(str(fichero), nuevo)

    conn = indice.open_index(str(index_path))
    try:
        conn.execute(
            "UPDATE pages SET header_image_path = ? WHERE path = ?",
            (old_url, fichero.relative_to(fichero.parents[1]).as_posix()),
        )
    finally:
        conn.close()


def _migrar(source: Path, vault_path: Path, index_path: Path, *extra: str) -> int:
    return main(
        [
            "--source", str(source),
            "--vault", str(vault_path),
            "--index", str(index_path),
            *extra,
        ]
    )


def test_la_migracion_mueve_el_adjunto_y_actualiza_las_dos_referencias(
    client, vault_path, index_path, tmp_path, capsys
):
    page = crear(client, title="Con cabecera")
    fichero = vault_path / "Notas" / "Con cabecera.md"
    old_url = f"/files/pages/{page['id']}/header.png"

    source = tmp_path / "attachments"
    viejo = source / "pages" / page["id"] / "header.png"
    viejo.parent.mkdir(parents=True)
    viejo.write_bytes(PNG)
    _apuntar_al_sitio_viejo(client, index_path, fichero, old_url)
    assert client.get(f"/pages/{page['id']}").json()["header_image_path"] == old_url

    assert _migrar(source, vault_path, index_path) == 0
    salida = capsys.readouterr().out
    assert re.search(r"movidos\s*: 1", salida), salida
    assert re.search(r"notas_actualizadas\s*: 1", salida), salida
    assert re.search(r"indice_actualizado\s*: 1", salida), salida
    assert re.search(r"papelera_actualizadas\s*: 0", salida), salida

    # El fichero está en el vault, con el nombre de P1.2b, y el origen vacío.
    cabeceras = list((vault_path / "_attachments").rglob(f"header-{page['id']}.png"))
    assert len(cabeceras) == 1
    assert cabeceras[0].read_bytes() == PNG
    assert not viejo.exists()

    nuevo_url = f"/files/{cabeceras[0].relative_to(vault_path).as_posix()}"
    assert nuevo_url in fichero.read_text(encoding="utf-8")
    assert client.get(nuevo_url).status_code == 200

    # Y el reindexado no se lleva la cabecera por delante (está en el `.md`).
    client.post("/reindex")
    assert client.get(f"/pages/{page['id']}").json()["header_image_path"] == nuevo_url

    # Segunda pasada: nada que mover ni que reescribir.
    assert _migrar(source, vault_path, index_path) == 0
    otra = capsys.readouterr().out
    assert re.search(r"movidos\s*: 0", otra), otra
    assert re.search(r"ya_estaban\s*: 0", otra), otra
    assert re.search(r"notas_actualizadas\s*: 0", otra), otra
    assert len(list((vault_path / "_attachments").rglob(f"header-{page['id']}.*"))) == 1


def test_la_migracion_reconcilia_si_el_fichero_ya_estaba_en_el_vault(
    client, vault_path, index_path, tmp_path, capsys
):
    """Corte entre mover y reescribir: la segunda pasada arregla la referencia."""
    page = crear(client, title="Huérfana de referencia")
    fichero = vault_path / "Notas" / "Huérfana de referencia.md"
    old_url = f"/files/pages/{page['id']}/header.png"
    _apuntar_al_sitio_viejo(client, index_path, fichero, old_url)

    # El fichero ya está en el vault (como si el paso 1 hubiera ido bien)…
    destino = vault_path / "_attachments" / "2026" / "10" / f"header-{page['id']}.png"
    destino.parent.mkdir(parents=True)
    destino.write_bytes(PNG)
    # …y el origen está vacío, pero la referencia sigue siendo la vieja.
    source = tmp_path / "attachments"
    source.mkdir()

    assert _migrar(source, vault_path, index_path) == 0
    salida = capsys.readouterr().out
    assert re.search(r"movidos\s*: 0", salida), salida
    assert re.search(r"notas_actualizadas\s*: 1", salida), salida
    assert re.search(r"indice_actualizado\s*: 1", salida), salida

    nuevo_url = f"/files/_attachments/2026/10/header-{page['id']}.png"
    assert nuevo_url in fichero.read_text(encoding="utf-8")
    assert client.get(f"/pages/{page['id']}").json()["header_image_path"] == nuevo_url
    assert client.get(nuevo_url).status_code == 200


def test_reescribe_tambien_la_cabecera_de_una_nota_que_ya_estaba_en_la_papelera(
    client, vault_path, index_path, tmp_path, capsys
):
    """La papelera existe desde P1.2a: una nota borrada antes de migrar cae aquí.

    Sin reescribir su frontmatter, el adjunto migrado se queda sin ninguna
    referencia viva (huérfano) y al restaurar la nota la imagen da 404.
    """
    page = crear(client, title="Borrada antes de migrar")
    fichero = vault_path / "Notas" / "Borrada antes de migrar.md"
    old_url = f"/files/pages/{page['id']}/header.png"

    source = tmp_path / "attachments"
    viejo = source / "pages" / page["id"] / "header.png"
    viejo.parent.mkdir(parents=True)
    viejo.write_bytes(PNG)
    _apuntar_al_sitio_viejo(client, index_path, fichero, old_url)

    # Se borra ANTES de migrar: el `.md` se va a la papelera con la URL vieja.
    assert client.delete(f"/pages/{page['id']}").status_code == 204
    entrada = next(
        e
        for e in client.get("/trash").json()
        if e["original_path"] == "Notas/Borrada antes de migrar.md"
    )
    en_papelera = vault_path / entrada["path"]
    antes = en_papelera.read_text(encoding="utf-8")
    assert old_url in antes

    assert _migrar(source, vault_path, index_path) == 0
    salida = capsys.readouterr().out
    assert re.search(r"movidos\s*: 1", salida), salida
    # La nota viva no existe; la de la papelera sí, y se cuenta aparte.
    assert re.search(r"notas_actualizadas\s*: 0", salida), salida
    assert re.search(r"papelera_actualizadas\s*: 1", salida), salida
    assert "aviso" not in salida, salida

    cabeceras = list((vault_path / "_attachments").rglob(f"header-{page['id']}.png"))
    assert len(cabeceras) == 1
    nuevo_url = f"/files/{cabeceras[0].relative_to(vault_path).as_posix()}"

    # El fichero sigue en la papelera, en el mismo sello: solo cambió el
    # frontmatter, no su sitio ni su cuerpo.
    assert en_papelera.is_file()
    despues = en_papelera.read_text(encoding="utf-8")
    assert nuevo_url in despues
    assert old_url not in despues
    assert fm.split_document(despues)[1] == fm.split_document(antes)[1]

    # Y restaurar devuelve una nota cuya imagen se sirve, no un 404.
    restaurada = client.post("/trash/restore", json={"path": entrada["path"]})
    assert restaurada.status_code == 200, restaurada.text
    assert restaurada.json()["restored_path"] == "Notas/Borrada antes de migrar.md"
    assert client.get(f"/pages/{page['id']}").json()["header_image_path"] == nuevo_url
    assert client.get(nuevo_url).status_code == 200
    assert client.get(nuevo_url).content == PNG
    assert client.get(old_url).status_code == 404


def test_avisa_de_las_notas_en_la_papelera_cuya_cabecera_no_esta_en_ningun_sitio(
    client, vault_path, index_path, tmp_path, capsys
):
    """Si el fichero no aparece ni en el origen ni en el vault, se avisa y no
    se toca la referencia: no hay nada a lo que apuntar."""
    page = crear(client, title="Sin fichero")
    fichero = vault_path / "Notas" / "Sin fichero.md"
    old_url = f"/files/pages/{page['id']}/header.png"
    _apuntar_al_sitio_viejo(client, index_path, fichero, old_url)
    assert client.delete(f"/pages/{page['id']}").status_code == 204

    source = tmp_path / "attachments"
    source.mkdir()
    assert _migrar(source, vault_path, index_path) == 0
    salida = capsys.readouterr().out
    assert ".trash/" in salida and "Sin fichero.md: apunta a" in salida, salida
    assert old_url in salida, salida
    assert re.search(r"papelera_actualizadas\s*: 0", salida), salida


def test_con_reindex_un_vault_sin_notas_no_vacia_el_indice(
    client, index_path, tmp_path, capsys
):
    """`--reindex` respeta el guardia de siempre: un `--vault` equivocado (o un
    volumen sin montar) no puede dejar el índice vacío."""
    crear(client, title="Viva")
    vacio = tmp_path / "vault-vacio"
    vacio.mkdir()
    source = tmp_path / "attachments"
    source.mkdir()

    assert _migrar(source, vacio, index_path, "--reindex") == 1
    err = capsys.readouterr().err
    assert "no se ha reindexado" in err, err
    assert [p["title"] for p in client.get("/pages").json()] == ["Viva"]


def test_con_reindex_el_indice_queda_al_dia_y_la_deriva_a_cero(
    client, vault_path, index_path, tmp_path, capsys
):
    """`atomic_write` cambia el mtime: sin reindexar, `/status` da deriva."""
    page = crear(client, title="Viva")
    fichero = vault_path / "Notas" / "Viva.md"
    old_url = f"/files/pages/{page['id']}/header.png"
    source = tmp_path / "attachments"
    viejo = source / "pages" / page["id"] / "header.png"
    viejo.parent.mkdir(parents=True)
    viejo.write_bytes(PNG)
    _apuntar_al_sitio_viejo(client, index_path, fichero, old_url)

    assert _migrar(source, vault_path, index_path, "--reindex") == 0
    salida = capsys.readouterr().out
    assert re.search(r"reindexado\s*:", salida), salida

    estado = client.get("/status").json()
    assert estado["drift"]["changed"] == 0, estado["drift"]
    assert estado["drift"]["last_reindex_at"]
    nuevo_url = f"/pages/{page['id']}"
    assert client.get(nuevo_url).json()["header_image_path"].startswith("/files/_attachments/")
