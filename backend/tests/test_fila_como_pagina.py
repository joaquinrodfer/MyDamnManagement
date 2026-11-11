"""Filas como página: el cuerpo de la fila vive en el mismo `.md`.

Lo que se fija aquí (tarjeta «Filas como página»):

- `GET /databases/{id}/rows/{row}` devuelve el **cuerpo** de la fila; la lista
  (`GET .../rows`) no, que una tabla de cien filas no arrastra cien cuerpos;
- `PATCH` acepta `body_markdown` y lo escribe en el cuerpo del **mismo** `.md`
  (frontmatter + cuerpo), sin mover el fichero y reindexando en la operación;
- un `PATCH` que no manda cuerpo (una celda, el título) **no lo borra**: el
  texto de una fila es del usuario;
- reindexar el vault reconstruye lo mismo: el cuerpo y el fichero quedan byte a
  byte como estaban (nada de perder el cuerpo al reindexar).
"""

from __future__ import annotations

import hashlib
import re

from app.vault import fm

SCHEMA = [
    {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
    {"key": "importe", "name": "Importe", "type": "number"},
]

CUERPO = "## Notas de la reunión\n\n- quedamos el martes\n- llevo los números\n"


def crear_base(client, titulo: str = "Contactos") -> dict:
    respuesta = client.post("/databases", json={"title": titulo, "schema_def": SCHEMA})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id: str, **cuerpo) -> dict:
    respuesta = client.post(f"/databases/{database_id}/rows", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def fichero_de_la_fila(vault_path, row_id: str):
    """La ruta del `.md` de la fila: `Bases/<slug>/<id8>.md`."""
    coincidencias = list(vault_path.rglob(f"{row_id[:8]}.md"))
    assert len(coincidencias) == 1, [str(p) for p in vault_path.rglob("*.md")]
    return coincidencias[0]


def sha1(path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def frontmatter(path) -> dict:
    """El frontmatter del `.md`, leído con el mismo parser que el índice."""
    raw, _cuerpo = fm.split_document(path.read_text(encoding="utf-8"))
    assert raw is not None, f"{path} no tiene frontmatter"
    return fm.parse(raw).data


def test_la_fila_trae_su_cuerpo_y_la_lista_no(client):
    base = crear_base(client)
    fila = crear_fila(client, base["id"], title="Acme", properties={"fase": "lead"})

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}", json={"body_markdown": CUERPO}
    )
    assert respuesta.status_code == 200, respuesta.text
    # El PATCH de una fila sigue siendo `RowRead`: el cuerpo no vuelve por ahí,
    # que es el camino de las celdas.
    assert "body_markdown" not in respuesta.json()

    sola = client.get(f"/databases/{base['id']}/rows/{fila['id']}")
    assert sola.status_code == 200, sola.text
    assert sola.json()["body_markdown"] == CUERPO.rstrip("\n")
    # Lo demás de la fila sigue viniendo igual que en la tabla.
    assert sola.json()["title"] == "Acme"
    assert sola.json()["properties"] == {"fase": "lead"}

    lista = client.get(f"/databases/{base['id']}/rows")
    assert lista.status_code == 200
    assert "body_markdown" not in lista.json()[0]


def test_el_cuerpo_se_escribe_en_el_mismo_fichero_sin_moverlo(client, vault_path):
    base = crear_base(client)
    fila = crear_fila(client, base["id"], title="Acme", properties={"importe": 12})
    ruta = fichero_de_la_fila(vault_path, fila["id"])
    antes = ruta.read_text(encoding="utf-8")
    assert ruta.name == f"{fila['id'][:8]}.md"
    assert re.fullmatch(r"[0-9a-f]{8}\.md", ruta.name)

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"body_markdown": CUERPO},
    )
    assert respuesta.status_code == 200, respuesta.text

    # Mismo fichero, mismo nombre: el cuerpo va al cuerpo y las propiedades al
    # frontmatter (el `importe` que ya estaba no se pierde).
    assert fichero_de_la_fila(vault_path, fila["id"]) == ruta
    despues = ruta.read_text(encoding="utf-8")
    assert despues != antes
    assert despues.startswith("---\n")
    assert "importe: 12" in despues
    assert CUERPO.rstrip("\n") in despues
    assert despues.split("---\n", 2)[2].strip() == CUERPO.strip()


def test_renombrar_no_mueve_el_fichero_y_conserva_el_cuerpo(client, vault_path):
    base = crear_base(client)
    fila = crear_fila(client, base["id"], title="Acme")
    client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}", json={"body_markdown": CUERPO}
    )
    ruta = fichero_de_la_fila(vault_path, fila["id"])

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}", json={"title": "Acme Renombrada"}
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["title"] == "Acme Renombrada"

    assert fichero_de_la_fila(vault_path, fila["id"]) == ruta
    texto = ruta.read_text(encoding="utf-8")
    assert frontmatter(ruta)["title"] == "Acme Renombrada"
    assert CUERPO.rstrip("\n") in texto


def test_guardar_una_celda_no_borra_el_cuerpo(client, vault_path):
    base = crear_base(client)
    fila = crear_fila(client, base["id"], title="Acme", properties={"fase": "lead"})
    client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}", json={"body_markdown": CUERPO}
    )

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"fase": "ganado"}},
    )
    assert respuesta.status_code == 200, respuesta.text

    sola = client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()
    assert sola["properties"] == {"fase": "ganado"}
    assert sola["body_markdown"] == CUERPO.rstrip("\n")
    texto = fichero_de_la_fila(vault_path, fila["id"]).read_text(encoding="utf-8")
    assert CUERPO.rstrip("\n") in texto


def test_un_cuerpo_vacio_vacia_el_cuerpo(client, vault_path):
    base = crear_base(client)
    fila = crear_fila(client, base["id"], title="Acme")
    client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}", json={"body_markdown": CUERPO}
    )

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}", json={"body_markdown": ""}
    )
    assert respuesta.status_code == 200, respuesta.text
    assert client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()["body_markdown"] == ""
    texto = fichero_de_la_fila(vault_path, fila["id"]).read_text(encoding="utf-8")
    assert "quedamos el martes" not in texto


def test_reindexar_reconstruye_el_mismo_cuerpo_y_el_mismo_fichero(client, vault_path):
    base = crear_base(client)
    fila = crear_fila(client, base["id"], title="Acme", properties={"fase": "lead"})
    client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}", json={"body_markdown": CUERPO}
    )
    ruta = fichero_de_la_fila(vault_path, fila["id"])
    huella = sha1(ruta)

    respuesta = client.post("/reindex")
    assert respuesta.status_code == 200, respuesta.text

    # Reindexar solo lee: el `.md` no se reescribe y el cuerpo no se pierde.
    assert sha1(ruta) == huella
    sola = client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()
    assert sola["body_markdown"] == CUERPO.rstrip("\n")
    assert sola["title"] == "Acme"
    assert sola["properties"] == {"fase": "lead"}


def test_titulo_y_cuerpo_en_el_mismo_patch(client, vault_path):
    base = crear_base(client)
    fila = crear_fila(client, base["id"], title="Acme")
    ruta = fichero_de_la_fila(vault_path, fila["id"])

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"title": "Acme Renombrada", "body_markdown": CUERPO},
    )
    assert respuesta.status_code == 200, respuesta.text

    assert fichero_de_la_fila(vault_path, fila["id"]) == ruta
    texto = ruta.read_text(encoding="utf-8")
    assert frontmatter(ruta)["title"] == "Acme Renombrada"
    assert CUERPO.rstrip("\n") in texto
    sola = client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()
    assert sola["title"] == "Acme Renombrada"
    assert sola["body_markdown"] == CUERPO.rstrip("\n")
