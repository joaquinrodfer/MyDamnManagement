"""Filas por id: el fichero es `<id8>.md` y el título es una clave del frontmatter.

Lo que se fija aquí (tarjeta «Filas por id»):

- al crear una fila su fichero se llama con los 8 primeros caracteres hex de su
  `id` y su título va en `title` del frontmatter;
- `PATCH` del título **no mueve el fichero**: solo cambia el `.md`;
- la migración de un vault con el título por nombre de fichero: 200 filas → 200
  renombradas, 0 errores y el índice reconstruido con los mismos títulos;
- los enlaces no se rompen: `[[Acme]]` sigue resolviendo a la fila después de
  migrarla, y una fila sin `title` (frontmatter viejo) resuelve por el nombre
  del fichero.
"""

from __future__ import annotations

import re
import uuid

from app.vault import fm, names
from scripts.migrate_row_files_to_ids import main as migrar

BASE_ID = "11111111-1111-4111-8111-111111111111"
SCHEMA = [{"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]}]


def frontmatter(path) -> dict:
    raw, _cuerpo = fm.split_document(path.read_text(encoding="utf-8"))
    assert raw is not None, f"{path} no tiene frontmatter"
    return fm.parse(raw).data


def escribir_base(vault_path) -> None:
    """`Bases/Contactos.md` con su carpeta de filas, como un vault anterior."""
    carpeta = vault_path / "Bases"
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / "Contactos.md").write_text(
        "---\n"
        f"id: {BASE_ID}\n"
        "type: database\n"
        "icon: 🗄\n"
        "schema:\n"
        "  - key: fase\n"
        "    name: Fase\n"
        "    type: select\n"
        "    options: [lead, ganado]\n"
        "views: []\n"
        "---\n",
        encoding="utf-8",
    )
    (carpeta / "Contactos").mkdir(exist_ok=True)


def escribir_fila(carpeta, titulo: str, row_id: str, fase: str = "lead"):
    """Una fila «vieja»: el título es el nombre del fichero y no hay `title`."""
    fichero = carpeta / f"{titulo}.md"
    fichero.write_text(f"---\nid: {row_id}\nfase: {fase}\n---\n\n", encoding="utf-8")
    return fichero


def fila(client, database_id: str, **cuerpo) -> dict:
    respuesta = client.post(f"/databases/{database_id}/rows", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


# --------------------------------------------------------------------------
# 1. crear y renombrar
# --------------------------------------------------------------------------


def test_crear_una_fila_la_nombra_por_su_id_y_guarda_el_title(client, vault_path):
    base = client.post("/databases", json={"title": "Contactos", "schema_def": SCHEMA}).json()
    creada = fila(client, base["id"], title="Acme", properties={"fase": "lead"})

    carpeta = vault_path / "Bases" / "Contactos"
    ruta = carpeta / f"{creada['id'][:8]}.md"
    assert ruta.exists(), sorted(p.name for p in carpeta.iterdir())
    assert re.fullmatch(r"[0-9a-f]{8}\.md", ruta.name), ruta.name

    datos = frontmatter(ruta)
    assert datos["id"] == creada["id"]
    assert datos["title"] == "Acme"
    assert datos["fase"] == "lead"


def test_renombrar_una_fila_no_mueve_el_fichero(client, vault_path):
    base = client.post("/databases", json={"title": "Contactos", "schema_def": SCHEMA}).json()
    creada = fila(client, base["id"], title="Acme", properties={"fase": "lead"})
    carpeta = vault_path / "Bases" / "Contactos"
    ruta = carpeta / f"{creada['id'][:8]}.md"
    nombre_antes = ruta.name

    for titulo in ("Acme SA", "Acme SA (cerrada)"):
        respuesta = client.patch(
            f"/databases/{base['id']}/rows/{creada['id']}", json={"title": titulo}
        )
        assert respuesta.status_code == 200, respuesta.text
        assert respuesta.json()["title"] == titulo
        # Mismo fichero y ningún ` (2)` ni título en la ruta.
        assert ruta.name == nombre_antes
        assert sorted(p.name for p in carpeta.iterdir()) == [nombre_antes]
        assert frontmatter(ruta)["title"] == titulo
        assert frontmatter(ruta)["fase"] == "lead", "las propiedades no se tocan"

    leida = client.get(f"/databases/{base['id']}/rows/{creada['id']}").json()
    assert leida["title"] == "Acme SA (cerrada)"


# --------------------------------------------------------------------------
# 2. el nombre por id: colisión y detección de «ya migrada»
# --------------------------------------------------------------------------


def test_el_nombre_por_id_se_alarga_si_los_8_primeros_chocan(tmp_path):
    row_id = "abcdef12-1111-4111-8111-111111111111"
    (tmp_path / "abcdef12.md").write_text("ocupado", encoding="utf-8")

    assert names.row_stem(tmp_path, row_id) == ("abcdef1211", True)
    # Ignorando el fichero propio (lo que hace la migración) vuelve a 8.
    assert names.row_stem(tmp_path, row_id, ignore={"abcdef12"}) == ("abcdef12", False)

    assert names.is_row_stem("abcdef12", row_id)
    assert names.is_row_stem("abcdef1211", row_id)
    assert not names.is_row_stem("Acme", row_id)


# --------------------------------------------------------------------------
# 3. los enlaces no se rompen (antes y después de migrar)
# --------------------------------------------------------------------------


def test_los_enlaces_siguen_resolviendo_al_migrar_una_fila(client, vault_path, index_path):
    escribir_base(vault_path)
    carpeta = vault_path / "Bases" / "Contactos"
    acme_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "acme"))
    acme = escribir_fila(carpeta, "Acme", acme_id)

    notas = vault_path / "Notas"
    notas.mkdir(exist_ok=True)
    otra_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "otra-nota"))
    (notas / "Otra nota.md").write_text(
        f"---\nid: {otra_id}\n---\n\n", encoding="utf-8"
    )
    duena_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "duena"))
    (notas / "Dueña.md").write_text(
        f"---\nid: {duena_id}\n---\n\nVer [[Acme]] y [[Otra nota]].\n", encoding="utf-8"
    )

    assert client.post("/reindex").status_code == 200

    # ANTES: la fila no tiene `title` y se resuelve por el nombre del fichero.
    # El enlace a una nota normal resuelve igual, sin tocar nada.
    assert [p["title"] for p in client.get(f"/pages/{acme_id}/backlinks").json()] == ["Dueña"]
    assert [p["title"] for p in client.get(f"/pages/{otra_id}/backlinks").json()] == ["Dueña"]
    assert [f["title"] for f in client.get(f"/databases/{BASE_ID}/rows").json()] == ["Acme"]

    # La migración escribe `title: Acme` y renombra a `<id8>.md`.
    assert migrar(["--vault", str(vault_path), "--index", str(index_path)]) == 0
    nueva = carpeta / f"{acme_id[:8]}.md"
    assert nueva.exists(), sorted(p.name for p in carpeta.iterdir())
    assert not acme.exists()
    assert frontmatter(nueva)["title"] == "Acme"

    assert client.post("/reindex").status_code == 200

    # DESPUÉS: el mismo `[[Acme]]` resuelve a la fila por su `title`.
    assert [p["title"] for p in client.get(f"/pages/{acme_id}/backlinks").json()] == ["Dueña"]
    assert [p["title"] for p in client.get(f"/pages/{otra_id}/backlinks").json()] == ["Dueña"]
    assert [f["title"] for f in client.get(f"/databases/{BASE_ID}/rows").json()] == ["Acme"]


# --------------------------------------------------------------------------
# 4. migración de un vault de 200 filas
# --------------------------------------------------------------------------


def test_migrar_un_vault_de_200_filas_y_el_indice_dice_lo_mismo(
    client, vault_path, index_path, capsys
):
    escribir_base(vault_path)
    carpeta = vault_path / "Bases" / "Contactos"
    titulos = [f"Fila {n:03d}" for n in range(1, 201)]
    for n in range(1, 201):
        escribir_fila(
            carpeta, f"Fila {n:03d}", str(uuid.uuid5(uuid.NAMESPACE_URL, f"fila-{n}"))
        )

    assert migrar(["--vault", str(vault_path), "--index", str(index_path), "--reindex"]) == 0
    salida = capsys.readouterr().out
    assert re.search(r"filas\s*:\s*200", salida), salida
    assert re.search(r"renombradas\s*:\s*200", salida), salida
    assert re.search(r"titulo_escrito\s*:\s*200", salida), salida
    assert re.search(r"errores\s*:\s*0", salida), salida

    # El disco: 200 ficheros con nombre de id y su título en el frontmatter.
    ficheros = sorted(carpeta.iterdir())
    assert len(ficheros) == 200
    assert all(re.fullmatch(r"[0-9a-f]{8}\.md", p.name) for p in ficheros), [
        p.name for p in ficheros[:3]
    ]
    assert all(frontmatter(p)["title"] in titulos for p in ficheros)

    # Y la API, sobre el índice reconstruido, sirve los mismos 200 títulos.
    filas = client.get(f"/databases/{BASE_ID}/rows").json()
    assert len(filas) == 200
    assert sorted(f["title"] for f in filas) == titulos
    assert {f["properties"]["fase"] for f in filas} == {"lead"}

    # Idempotente: la segunda pasada no renombra ni escribe nada.
    assert migrar(["--vault", str(vault_path)]) == 0
    salida = capsys.readouterr().out
    assert re.search(r"renombradas\s*:\s*0", salida), salida
    assert re.search(r"ya_estaban\s*:\s*200", salida), salida
    assert re.search(r"titulo_escrito\s*:\s*0", salida), salida
    assert re.search(r"errores\s*:\s*0", salida), salida
