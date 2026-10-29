"""P1.2a: los ficheros `.md` son la verdad para las notas.

Cada test corre sobre un vault vacío en un directorio temporal (fixture
`vault_path` de `conftest.py`) y un índice propio. Lo que se comprueba aquí es
el criterio de hecho de la tarjeta: guardar escribe el fichero, renombrar
renombra el fichero conservando el `id`, borrar manda a la papelera, el
reindexado reconstruye el índice idéntico, y un fichero raro no rompe nada.
"""

import uuid
from pathlib import Path

from app import index as indice

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def crear(client, **cuerpo) -> dict:
    respuesta = client.post("/pages", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def cuerpo_de(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 1. escritura
# --------------------------------------------------------------------------


def test_guardar_una_nota_escribe_el_fichero(client, vault_path):
    page = crear(
        client,
        title="Reunión con Acme",
        description="una línea opcional",
        body_markdown="Primera versión",
    )
    fichero = vault_path / "Notas" / "Reunión con Acme.md"
    assert fichero.exists(), "guardar una nota tiene que crear su .md"

    texto = cuerpo_de(fichero)
    assert texto.startswith("---\n")
    assert f"id: {page['id']}" in texto          # identidad estable
    assert "icon:" in texto                      # el icono por defecto de la API
    assert "description: \"una línea opcional\"" in texto
    assert "created:" in texto and "updated:" in texto
    assert texto.endswith("---\n\nPrimera versión\n")   # cuerpo Markdown tal cual
    # Los temporales de la escritura atómica no se quedan colgando.
    assert list(fichero.parent.glob("*.tmp-*")) == []

    # Guardar por la API reescribe el mismo fichero (no crea otro).
    client.patch(f"/pages/{page['id']}", json={"body_markdown": "Segunda versión"})
    assert "Segunda versión" in cuerpo_de(fichero)
    assert "Primera versión" not in cuerpo_de(fichero)
    assert len(list((vault_path / "Notas").iterdir())) == 1

    # Y una lectura posterior devuelve lo mismo que se guardó.
    leida = client.get(f"/pages/{page['id']}").json()
    assert leida["body_markdown"] == "Segunda versión"


def test_el_vault_tiene_la_estructura_del_adr(client, vault_path):
    crear(client, title="Una")
    assert (vault_path / "Notas").is_dir()
    assert (vault_path / "_attachments").is_dir()
    assert (vault_path / ".trash").is_dir()


def test_el_indice_vive_fuera_del_vault(client, index_path, vault_path):
    crear(client, title="Fuera")
    assert index_path.exists()
    assert vault_path not in index_path.parents
    # En el vault no hay ni rastro del índice (ni del WAL).
    assert [p.name for p in vault_path.iterdir() if p.suffix == ".sqlite"] == []


# --------------------------------------------------------------------------
# 2. renombrado y movimiento
# --------------------------------------------------------------------------


def test_renombrar_renombra_el_fichero_y_conserva_el_id(client, vault_path):
    page = crear(client, title="Idea")
    nota = vault_path / "Notas" / "Idea.md"
    assert nota.exists()

    renombrada = client.patch(
        f"/pages/{page['id']}", json={"title": "Idea buena"}
    ).json()
    assert renombrada["id"] == page["id"]          # el id no cambia
    assert not nota.exists()
    nueva = vault_path / "Notas" / "Idea buena.md"
    assert nueva.exists()
    assert f"id: {page['id']}" in cuerpo_de(nueva)

    # Tras reindexar, la mismas página (mismo id) con el título nuevo.
    assert client.post("/reindex").json()["notes"] == 1
    leida = client.get(f"/pages/{page['id']}").json()
    assert leida["title"] == "Idea buena"


def test_renombrar_mueve_la_carpeta_de_subpaginas(client, vault_path):
    padre = crear(client, title="Proyectos")
    hija = crear(client, title="MyDamnManagement", parent_id=padre["id"])
    assert (vault_path / "Notas" / "Proyectos" / "MyDamnManagement.md").exists()

    client.patch(f"/pages/{padre['id']}", json={"title": "Trabajo"})

    assert not (vault_path / "Notas" / "Proyectos").exists()
    assert (vault_path / "Notas" / "Trabajo" / "MyDamnManagement.md").exists()
    # La hija sigue colgando del padre y con su id.
    assert client.get(f"/pages/{hija['id']}").json()["parent_id"] == padre["id"]
    # Y el árbol no se rompe al reindexar: la ruta es la que manda.
    client.post("/reindex")
    arbol = client.get("/pages/tree").json()
    assert [n["id"] for n in arbol] == [padre["id"]]
    assert [n["id"] for n in arbol[0]["children"]] == [hija["id"]]


def test_mover_de_carpeta_cambia_la_ruta(client, vault_path):
    padre = crear(client, title="Caja")
    hija = crear(client, title="Suelta", parent_id=padre["id"])
    assert (vault_path / "Notas" / "Caja" / "Suelta.md").exists()

    # `parent_id: null` explícito = subir a la raíz.
    movida = client.patch(f"/pages/{hija['id']}", json={"parent_id": None}).json()
    assert movida["parent_id"] is None
    assert not (vault_path / "Notas" / "Caja" / "Suelta.md").exists()
    assert (vault_path / "Notas" / "Suelta.md").exists()

    # Y al revés, vuelve a la carpeta del padre.
    client.patch(f"/pages/{hija['id']}", json={"parent_id": padre["id"]})
    assert (vault_path / "Notas" / "Caja" / "Suelta.md").exists()

    # Un PATCH que no menciona `parent_id` no la mueve.
    client.patch(f"/pages/{hija['id']}", json={"body_markdown": "otra cosa"})
    assert (vault_path / "Notas" / "Caja" / "Suelta.md").exists()


def test_no_se_puede_colgar_una_nota_de_una_base_de_datos(client):
    base = client.post("/databases", json={"title": "Contactos"}).json()
    respuesta = client.post(
        "/pages", json={"title": "Hija", "parent_id": base["page_id"]}
    )
    assert respuesta.status_code == 400
    assert "solo puede colgar de otra nota" in respuesta.json()["detail"]

    # Y un padre que no existe tampoco es un 500.
    respuesta = client.post(
        "/pages",
        json={"title": "Huerfana", "parent_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert respuesta.status_code == 400


def test_el_titulo_no_escapa_del_vault(client, vault_path):
    page = crear(client, title="../../fuera/Y también: raro*")
    fichero = vault_path / "Notas" / f"{page['title']}.md"
    assert fichero.exists()
    assert page["title"] not in ("", ".", "..")
    assert "/" not in page["title"] and "\\" not in page["title"]
    # Nada fuera de Notas/.
    assert not (vault_path.parent / "fuera").exists()


def test_dos_notas_con_el_mismo_titulo_en_la_misma_carpeta_no_se_pisan(client, vault_path):
    primera = crear(client, title="Duplicada", body_markdown="uno")
    segunda = crear(client, title="Duplicada", body_markdown="dos")

    assert primera["title"] == "Duplicada"
    assert segunda["title"] == "Duplicada (2)", "el nombre del fichero es el título"
    assert (vault_path / "Notas" / "Duplicada.md").exists()
    assert (vault_path / "Notas" / "Duplicada (2).md").exists()
    assert client.get(f"/pages/{primera['id']}").json()["body_markdown"] == "uno"
    assert client.get(f"/pages/{segunda['id']}").json()["body_markdown"] == "dos"


# --------------------------------------------------------------------------
# 3. borrado y papelera
# --------------------------------------------------------------------------


def test_borrar_manda_a_la_papelera_y_se_puede_restaurar(client, vault_path):
    from app.config import settings
    from app.index import open_index
    from app.vault.store import VaultStore

    padre = crear(client, title="Proyectos")
    hija = crear(client, title="Bugs", parent_id=padre["id"])

    assert client.delete(f"/pages/{padre['id']}").status_code == 204
    assert client.get(f"/pages/{padre['id']}").status_code == 404
    assert client.get(f"/pages/{hija['id']}").status_code == 404
    assert not (vault_path / "Notas" / "Proyectos.md").exists()
    assert not (vault_path / "Notas" / "Proyectos").exists()

    # Está en `.trash/<fecha>/<ruta original>`, con la carpeta de hijas.
    papeleras = [p for p in (vault_path / ".trash").iterdir() if p.is_dir()]
    assert len(papeleras) == 1
    assert (papeleras[0] / "Notas" / "Proyectos.md").exists()
    assert (papeleras[0] / "Notas" / "Proyectos" / "Bugs.md").exists()

    conn = open_index(settings.index_path)
    try:
        vault = VaultStore(settings.vault_path, conn)
        en_papelera = vault.list_trash()
        assert [e.path for e in en_papelera] == [
            f".trash/{papeleras[0].name}/Notas/Proyectos.md",
            f".trash/{papeleras[0].name}/Notas/Proyectos/Bugs.md",
        ]
        assert [e.original_path for e in en_papelera] == [
            "Notas/Proyectos.md",
            "Notas/Proyectos/Bugs.md",
        ]
        # La fecha sale del sello de la carpeta de papelera, no de metadatos.
        assert all(e.deleted_at is not None for e in en_papelera)
        for entry in en_papelera:
            vault.restore(entry.path)
    finally:
        conn.close()

    assert (vault_path / "Notas" / "Proyectos.md").exists()
    assert (vault_path / "Notas" / "Proyectos" / "Bugs.md").exists()

    # Y el reindexado las devuelve con el mismo id (no son páginas nuevas).
    client.post("/reindex")
    assert client.get(f"/pages/{padre['id']}").json()["title"] == "Proyectos"
    assert client.get(f"/pages/{hija['id']}").json()["parent_id"] == padre["id"]


def test_borrar_una_nota_cuyo_fichero_ya_no_esta_no_revienta(client, vault_path):
    """Si el .md se borró por fuera, DELETE sigue dejando el índice limpio."""
    page = crear(client, title="Efímera")
    (vault_path / "Notas" / "Efímera.md").unlink()

    assert client.delete(f"/pages/{page['id']}").status_code == 204
    assert client.get(f"/pages/{page['id']}").status_code == 404


# --------------------------------------------------------------------------
# 4. reindexado
# --------------------------------------------------------------------------


def _hash(index_path: Path) -> str:
    return indice.dump_hash(str(index_path))[0]


def test_borrar_el_indice_y_arrancar_da_el_mismo_indice(client, index_path, vault_path):
    """El criterio: índice derivado, reconstruible, idéntico (hash de volcado)."""
    from fastapi.testclient import TestClient

    padre = crear(client, title="Proyectos", body_markdown="raíz")
    crear(client, title="Otra nota", icon="📌", description="con icono")
    hija = crear(
        client,
        title="MyDamnManagement",
        parent_id=padre["id"],
        body_markdown="Ver [[Proyectos]] y [[Otra nota]]",
    )

    antes_hash, antes_cuentas = indice.dump_hash(str(index_path))
    assert antes_cuentas["pages"] == 3
    assert antes_cuentas["links"] == 2

    for suffix in ("", "-wal", "-shm"):
        Path(str(index_path) + suffix).unlink(missing_ok=True)

    from app.main import app

    with TestClient(app) as otro:
        assert [p["id"] for p in otro.get("/pages").json()] != []
        assert otro.get(f"/pages/{hija['id']}").json()["parent_id"] == padre["id"]

    despues_hash, despues_cuentas = indice.dump_hash(str(index_path))
    assert despues_cuentas == antes_cuentas
    assert despues_hash == antes_hash, "el índice reconstruido no es idéntico"


def test_reindexar_dos_veces_es_idempotente(client, index_path):
    crear(client, title="Una", body_markdown="[[Dos]]")
    crear(client, title="Dos")
    primero = client.post("/reindex").json()
    hash_uno = _hash(index_path)
    segundo = client.post("/reindex").json()
    assert segundo["notes"] == primero["notes"] == 2
    assert _hash(index_path) == hash_uno


def test_editar_el_fichero_por_fuera_y_reindexar(client, vault_path):
    page = crear(client, title="Borrador", body_markdown="sin terminar")
    fichero = vault_path / "Notas" / "Borrador.md"

    # Se edita el .md a mano (sin la app)…
    fichero.write_text(
        cuerpo_de(fichero).replace("sin terminar", "terminado a mano"),
        encoding="utf-8",
    )
    assert client.get(f"/pages/{page['id']}").json()["body_markdown"] == "sin terminar"

    # …y el reindexado lo recoge. El id del frontmatter sigue mandando.
    assert client.post("/reindex").json()["notes"] == 1
    leida = client.get(f"/pages/{page['id']}").json()
    assert leida["body_markdown"] == "terminado a mano"

    # Renombrar el fichero por fuera cambia el título y conserva el id.
    (vault_path / "Notas" / "Borrador.md").rename(vault_path / "Notas" / "Final.md")
    client.post("/reindex")
    assert client.get(f"/pages/{page['id']}").json()["title"] == "Final"


def test_ficheros_raros_no_rompen_el_reindexado(client, vault_path):
    """CRLF, BOM, Latin-1 y frontmatter sin cerrar: aviso y a seguir."""
    normal = crear(client, title="Normal")

    notas = vault_path / "Notas"
    # CRLF + BOM
    (notas / "Windows.md").write_bytes(
        "\ufeff---\r\nid: 11111111-1111-1111-1111-111111111111\r\n---\r\n\r\ndesde notepad\r\n".encode("utf-8")
    )
    # Latin-1 de verdad (0xF1 no es UTF-8 válido)
    (notas / "Latin1.md").write_bytes(
        b"---\nid: 22222222-2222-2222-2222-222222222222\n---\n\npe\xf1a y \xf1and\xfa\n"
    )
    # Frontmatter abierto y sin cerrar
    (notas / "Roto.md").write_text("---\nid: 33333333-3333-3333-3333-333333333333\nsin cerrar\n", encoding="utf-8")
    # Sin frontmatter ninguno
    (notas / "A mano.md").write_text("solo cuerpo, sin metadatos\n", encoding="utf-8")

    respuesta = client.post("/reindex")
    assert respuesta.status_code == 200, respuesta.text
    datos = respuesta.json()
    assert datos["notes"] == 5
    assert datos["warnings"] > 0
    avisos = " · ".join(datos["warning_sample"])
    assert "UTF-8" in avisos or "U+FFFD" in avisos     # Latin-1
    assert "CRLF" in avisos                            # CRLF
    assert "BOM" in avisos                             # BOM
    assert "frontmatter roto" in avisos                # sin cerrar

    # Todas legibles y con id válido (la app no puede devolver ids que no sean UUID).
    paginas = client.get("/pages").json()
    assert len(paginas) == 5
    for pagina in paginas:
        uuid.UUID(pagina["id"])
    titulos = {p["title"] for p in paginas}
    assert {"Normal", "Windows", "Latin1", "Roto", "A mano"} <= titulos

    # La nota normal (con su id real) sigue siendo la misma después del susto.
    assert client.get(f"/pages/{normal['id']}").json()["title"] == "Normal"


def test_nota_creada_a_mano_sin_id_se_materializa_al_guardar(client, vault_path):
    fichero = vault_path / "Notas" / "A mano.md"
    fichero.write_text("---\ndescription: hecha a mano\n---\n\ncuerpo inicial\n", encoding="utf-8")

    creado = client.post("/reindex").json()
    assert creado["notes"] == 1

    paginas = client.get("/pages").json()
    assert len(paginas) == 1
    page = paginas[0]
    assert page["description"] == "hecha a mano"
    uuid.UUID(page["id"])  # el id derivado de la ruta es un UUID de verdad

    guardada = client.patch(
        f"/pages/{page['id']}", json={"body_markdown": "cuerpo guardado"}
    ).json()
    assert guardada["id"] == page["id"]
    texto = cuerpo_de(fichero)
    assert f"id: {page['id']}" in texto, "al guardar se materializa el id en el frontmatter"

    # Y a partir de aquí sobrevive a un renombrado (como cualquier otra).
    client.patch(f"/pages/{page['id']}", json={"title": "Ya con nombre"})
    client.post("/reindex")
    assert client.get(f"/pages/{page['id']}").json()["title"] == "Ya con nombre"


def test_la_cabecera_de_la_nota_sobrevive_al_reindexado(client, vault_path):
    page = crear(client, title="Con cabecera")
    subida = client.post(
        f"/pages/{page['id']}/header-image",
        files={"file": ("cabecera.png", PNG, "image/png")},
    )
    assert subida.status_code == 200
    ruta = subida.json()["header_image_path"]
    assert f"header_image: {ruta}" in cuerpo_de(vault_path / "Notas" / "Con cabecera.md")

    client.post("/reindex")
    assert client.get(f"/pages/{page['id']}").json()["header_image_path"] == ruta


# --------------------------------------------------------------------------
# 5. nada escribe solo en el índice (P2: también bases y filas)
# --------------------------------------------------------------------------


def test_una_base_y_sus_filas_viven_en_ficheros_y_el_reindexado_las_rehace(
    client, index_path, vault_path
):
    """P2: la base es `Bases/<slug>.md` y cada fila un `.md` en su carpeta.

    Eran lo último que solo vivía en el índice; ahora borrar el índice y
    reconstruirlo deja el mismo contenido (P2 lo comprueba a fondo en
    `test_p2_bases.py`).
    """
    base = client.post(
        "/databases",
        json={"title": "Contactos", "schema_def": [{"key": "empresa", "name": "Empresa", "type": "text"}]},
    ).json()
    fila = client.post(
        f"/databases/{base['id']}/rows", json={"title": "Acme", "properties": {"empresa": "Acme"}}
    ).json()
    crear(client, title="Nota normal")

    assert (vault_path / "Bases" / "Contactos.md").exists()
    assert (vault_path / "Bases" / "Contactos" / "Acme.md").exists()
    assert client.post("/reindex").json()["notes"] == 1

    assert client.get(f"/databases/{base['id']}").json()["title"] == "Contactos"
    filas = client.get(f"/databases/{base['id']}/rows").json()
    assert [f["properties"] for f in filas] == [{"empresa": "Acme"}]
    assert client.get(f"/databases/{base['id']}/rows/{fila['id']}").status_code == 200

    cuentas = indice.dump_hash(str(index_path))[1]
    assert cuentas["databases"] == 1
    assert cuentas["rows"] == 1
