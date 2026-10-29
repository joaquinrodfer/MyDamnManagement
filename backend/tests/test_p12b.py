"""P1.2b: adjuntos en el vault, restaurar desde la papelera y deriva en `/status`.

Los tres puntos del criterio de hecho de la tarjeta, uno por bloque.
"""

import os
import time
from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def crear(client, **cuerpo) -> dict:
    respuesta = client.post("/pages", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def _adjunto(vault_path: Path, url: str) -> Path:
    """La ruta en disco del vault que corresponde a una URL de `/files`."""
    assert url.startswith("/files/"), url
    return vault_path / url[len("/files/"):]


# --------------------------------------------------------------------------
# 1. adjuntos dentro del vault
# --------------------------------------------------------------------------


def test_el_adjunto_acaba_en_el_vault_y_sobrevive_a_borrar_el_indice(
    client, vault_path, index_path
):
    page = crear(client, title="Con cabecera")
    subida = client.post(
        f"/pages/{page['id']}/header-image",
        files={"file": ("cabecera.png", PNG, "image/png")},
    )
    assert subida.status_code == 200, subida.text
    ruta = subida.json()["header_image_path"]

    # La URL **es** la ruta del vault: `_attachments/<año>/<mes>/…`.
    assert ruta.startswith("/files/_attachments/"), ruta
    adjunto = _adjunto(vault_path, ruta)
    assert adjunto.is_file(), f"el adjunto no está en el vault: {adjunto}"
    assert adjunto.read_bytes() == PNG

    # Y la nota lo lleva en el frontmatter, así que no depende del índice.
    texto = (vault_path / "Notas" / "Con cabecera.md").read_text(encoding="utf-8")
    assert f"header_image: {ruta}" in texto

    servido = client.get(ruta)
    assert servido.status_code == 200
    assert servido.headers["content-type"] == "image/png"
    assert servido.headers["x-content-type-options"] == "nosniff"
    assert servido.headers["content-disposition"].startswith("inline;")
    assert servido.content == PNG

    # Índice borrado del disco: el arranque lo reconstruye desde el vault y el
    # adjunto sigue ahí porque nunca estuvo fuera de él.
    from fastapi.testclient import TestClient

    for suffix in ("", "-wal", "-shm"):
        Path(str(index_path) + suffix).unlink(missing_ok=True)

    from app.main import app

    with TestClient(app) as otro:
        assert otro.get(f"/pages/{page['id']}").json()["header_image_path"] == ruta
        assert otro.get(ruta).status_code == 200
        assert otro.get(ruta).content == PNG


def test_quitar_la_cabecera_borra_el_adjunto_del_vault(client, vault_path):
    page = crear(client, title="Con cabecera")
    ruta = client.post(
        f"/pages/{page['id']}/header-image",
        files={"file": ("cabecera.png", PNG, "image/png")},
    ).json()["header_image_path"]
    adjunto = _adjunto(vault_path, ruta)
    assert adjunto.exists()

    borrada = client.delete(f"/pages/{page['id']}/header-image")
    assert borrada.status_code == 200
    assert borrada.json()["header_image_path"] is None
    assert not adjunto.exists(), "el adjunto se queda huérfano en el vault"


def test_solo_se_sirve_como_imagen_lo_que_lo_es(client, vault_path):
    """Los magic bytes mandan: un HTML con nombre de PNG no se renderiza."""
    carpeta = vault_path / "_attachments" / "2026" / "10"
    carpeta.mkdir(parents=True)
    (carpeta / "colado.png").write_bytes(b"<html><script>alert(1)</script></html>")

    servido = client.get("/files/_attachments/2026/10/colado.png")
    assert servido.status_code == 200
    assert servido.headers["content-type"] == "application/octet-stream"
    assert servido.headers["content-disposition"].startswith("attachment;")
    assert servido.headers["x-content-type-options"] == "nosniff"

    # Nada fuera de `_attachments/` se sirve (ni el índice, ni las notas), ni
    # con un nombre hostil que intente salir del vault.
    assert client.get("/files/indice.sqlite").status_code == 404
    assert client.get("/files/Notas/Con cabecera.md").status_code == 404
    assert client.get("/files/_attachments/%2e%2e/%2e%2e/etc/passwd").status_code == 404
    assert client.get("/files/_attachments/../../etc/passwd").status_code == 404
    assert client.get("/files/_attachments/no-existe.png").status_code == 404


# --------------------------------------------------------------------------
# 2. restaurar desde la papelera
# --------------------------------------------------------------------------


def test_restaurar_devuelve_el_fichero_a_su_ruta(client, vault_path):
    page = crear(client, title="Rescatable", body_markdown="no borrar del todo")
    assert client.delete(f"/pages/{page['id']}").status_code == 204
    assert not (vault_path / "Notas" / "Rescatable.md").exists()

    listado = client.get("/trash").json()
    entrada = next(e for e in listado if e["original_path"] == "Notas/Rescatable.md")
    assert entrada["path"].startswith(".trash/")
    assert entrada["kind"] == "note"
    assert entrada["size"] > 0
    # La fecha sale del sello de la carpeta de papelera (UTC).
    sello = entrada["path"].split("/")[1]
    assert entrada["deleted_at"] is not None
    assert entrada["deleted_at"][:10] == sello[:10]

    respuesta = client.post("/trash/restore", json={"path": entrada["path"]})
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["restored_path"] == "Notas/Rescatable.md"
    assert respuesta.json()["reindexed"] is True

    assert (vault_path / "Notas" / "Rescatable.md").exists()
    # Vuelve a la app con el mismo id (el del frontmatter) y su cuerpo, sin
    # esperar a un reindexado a mano.
    vuelta = client.get(f"/pages/{page['id']}")
    assert vuelta.status_code == 200
    assert vuelta.json()["body_markdown"] == "no borrar del todo"
    assert client.get("/trash").json() == []


def test_restaurar_con_la_ruta_ocupada_es_409(client, vault_path):
    page = crear(client, title="Choque")
    assert client.delete(f"/pages/{page['id']}").status_code == 204
    entrada = next(
        e for e in client.get("/trash").json() if e["original_path"] == "Notas/Choque.md"
    )

    # Alguien ha ocupado la ruta original mientras tanto.
    ocupante = vault_path / "Notas" / "Choque.md"
    ocupante.write_text(
        "---\nid: 8f14e45f-ceea-467a-9f1b-0f0a1b2c3d4e\n---\n\nocupando\n",
        encoding="utf-8",
    )

    respuesta = client.post("/trash/restore", json={"path": entrada["path"]})
    assert respuesta.status_code == 409, respuesta.text
    assert "ocupada" in respuesta.json()["detail"]

    # No se inventa un `Choque (2).md` ni se pisa nada.
    assert not (vault_path / "Notas" / "Choque (2).md").exists()
    assert ocupante.read_text(encoding="utf-8").endswith("ocupando\n")
    assert [e["path"] for e in client.get("/trash").json()] == [entrada["path"]]


def test_restaurar_recrea_la_carpeta_intermedia(client, vault_path):
    """Restaurar una subpágina sola: la carpeta se recrea y la nota vuelve sin
    padre (su `.md` padre sigue en la papelera). Es el comportamiento declarado:
    el fichero vuelve a su ruta, el árbol se deduce al reindexar, como siempre."""
    padre = crear(client, title="Proyectos")
    hija = crear(client, title="Bugs", parent_id=padre["id"])
    assert client.delete(f"/pages/{padre['id']}").status_code == 204
    assert not (vault_path / "Notas" / "Proyectos").exists()

    entrada = next(
        e for e in client.get("/trash").json() if e["original_path"] == "Notas/Proyectos/Bugs.md"
    )
    respuesta = client.post("/trash/restore", json={"path": entrada["path"]})
    assert respuesta.status_code == 200, respuesta.text
    assert (vault_path / "Notas" / "Proyectos" / "Bugs.md").is_file()

    vuelta = client.get(f"/pages/{hija['id']}")
    assert vuelta.status_code == 200
    assert vuelta.json()["parent_id"] is None

def test_restaurar_una_ruta_que_no_esta_en_la_papelera_es_404(client):
    respuesta = client.post("/trash/restore", json={"path": ".trash/2026-01-01T00-00-00/No hay.md"})
    assert respuesta.status_code == 404


def test_restaurar_con_un_ancestro_ocupado_por_un_fichero_es_409(client, vault_path):
    """Un fichero donde debería ir una carpeta: 409 con el motivo, no un 500.

    `vaultfs.move` hace `os.makedirs` del directorio destino y eso lanzaba
    `FileExistsError` sin capturar. Nada se pierde (la entrada sigue en la
    papelera y el intruso intacto), pero el cliente recibía un 500 mudo.
    """
    padre = crear(client, title="Proyectos")
    crear(client, title="Bugs", parent_id=padre["id"])
    assert client.delete(f"/pages/{padre['id']}").status_code == 204

    entrada = next(
        e for e in client.get("/trash").json() if e["original_path"] == "Notas/Proyectos/Bugs.md"
    )
    intruso = vault_path / "Notas" / "Proyectos"   # fichero sin extensión, no carpeta
    intruso.write_text("no soy una carpeta\n", encoding="utf-8")

    respuesta = client.post("/trash/restore", json={"path": entrada["path"]})
    assert respuesta.status_code == 409, respuesta.text
    assert "Proyectos" in respuesta.json()["detail"]
    assert "fichero" in respuesta.json()["detail"]

    # Nada se pierde y la entrada sigue en la papelera, lista para reintentar.
    assert intruso.read_text(encoding="utf-8") == "no soy una carpeta\n"
    assert entrada["path"] in [e["path"] for e in client.get("/trash").json()]


# --------------------------------------------------------------------------
# 3. deriva en /status
# --------------------------------------------------------------------------


def test_status_cuenta_la_deriva_y_vuelve_a_cero_tras_reindexar(client, vault_path):
    crear(client, title="Una", body_markdown="cuerpo")
    crear(client, title="Dos")

    estado = client.get("/status").json()
    assert estado["drift"]["ok"] is True
    assert estado["drift"]["changed"] == 0
    assert estado["drift"]["checked"] == 2
    assert estado["drift"]["last_reindex_at"], "el arranque reindexa y lo deja escrito"

    # 1) se edita un `.md` por fuera: otro tamaño y otro mtime…
    fichero = vault_path / "Notas" / "Una.md"
    fichero.write_text(
        fichero.read_text(encoding="utf-8") + "\notra línea por fuera\n", encoding="utf-8"
    )
    os.utime(fichero, (time.time() + 60, time.time() + 60))
    # 2) …y se crea una nota a mano, sin pasar por la API.
    (vault_path / "Notas" / "A mano.md").write_text(
        "---\nid: 8f14e45f-ceea-467a-9f1b-0f0a1b2c3d4e\n---\n\na mano\n", encoding="utf-8"
    )

    deriva = client.get("/status").json()["drift"]
    assert deriva["modified"] == 1
    assert deriva["added"] == 1
    assert deriva["missing"] == 0
    assert deriva["changed"] == 2

    reindex = client.post("/reindex").json()
    assert reindex["notes"] == 3
    assert reindex["reindexed_at"]

    despues = client.get("/status").json()["drift"]
    assert despues["changed"] == 0
    assert despues["checked"] == 3
    assert despues["last_reindex_at"] == reindex["reindexed_at"]


def test_la_deriva_cuenta_solo_el_mtime_y_los_ficheros_borrados(client, vault_path):
    crear(client, title="Solo mtime", body_markdown="mismo contenido")
    crear(client, title="Desaparece")
    assert client.get("/status").json()["drift"]["changed"] == 0

    # Solo cambia el mtime: el contenido es idéntico y aun así el fichero se
    # escribió por fuera (un `touch` basta para que el índice sirva lo viejo).
    fichero = vault_path / "Notas" / "Solo mtime.md"
    futuro = time.time() + 3600
    os.utime(fichero, (futuro, futuro))
    deriva = client.get("/status").json()["drift"]
    assert (deriva["modified"], deriva["added"], deriva["missing"]) == (1, 0, 0), deriva

    # Y un `.md` que ya no está: el índice tiene una nota sin fichero detrás.
    (vault_path / "Notas" / "Desaparece.md").unlink()
    deriva = client.get("/status").json()["drift"]
    assert (deriva["modified"], deriva["added"], deriva["missing"]) == (1, 0, 1), deriva
    assert deriva["changed"] == 2

    client.post("/reindex")
    despues = client.get("/status").json()["drift"]
    assert (despues["changed"], despues["checked"]) == (0, 1), despues
