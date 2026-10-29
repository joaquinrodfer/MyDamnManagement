"""P3: lo que la interfaz manda al editar una fila, con un solo PATCH.

La tabla y el diálogo de fila del tablero guardan por
`PATCH /databases/{id}/rows/{row_id}`. Aquí se fija lo que la UI da por hecho y
el `.md` tiene que cumplir:

1. un PATCH con título y propiedades a la vez renombra el fichero **y** escribe
   los valores (el diálogo del tablero manda los dos campos juntos);
2. renombrar a un título que ya existe no pisa la otra fila (` (2)`), y cada
   fichero conserva su `id`.
"""

from pathlib import Path

CRM_SCHEMA = [
    {"key": "empresa", "name": "Empresa", "type": "text"},
    {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
    {"key": "valor", "name": "Valor", "type": "number"},
]


def crear_database(client, **cuerpo) -> dict:
    respuesta = client.post("/databases", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id, **cuerpo) -> dict:
    respuesta = client.post(f"/databases/{database_id}/rows", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def frontmatter(path: Path) -> dict:
    """El bloque YAML del fichero, con el parser de la app (sin dependencias)."""
    from app.vault import fm

    raw, _cuerpo = fm.split_document(path.read_text(encoding="utf-8"))
    assert raw is not None, f"{path} no tiene frontmatter"
    return fm.parse(raw).data


def test_un_patch_con_titulo_y_propiedades_renombra_y_guarda(client, vault_path):
    base = crear_database(client, title="Contactos", schema_def=CRM_SCHEMA)
    fila = crear_fila(
        client, base["id"], title="Acme", properties={"empresa": "Acme S.L.", "fase": "lead"}
    )
    carpeta = vault_path / "Bases" / "Contactos"
    assert (carpeta / "Acme.md").exists()

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"title": "Acme SA", "properties": {"fase": "ganado", "valor": 1500}},
    )
    assert respuesta.status_code == 200, respuesta.text
    editada = respuesta.json()
    assert editada["id"] == fila["id"], "renombrar no cambia la identidad"
    assert editada["title"] == "Acme SA"
    assert editada["properties"]["fase"] == "ganado"
    assert editada["properties"]["valor"] == 1500.0

    assert not (carpeta / "Acme.md").exists()
    nuevo = carpeta / "Acme SA.md"
    assert nuevo.exists(), "el título es el nombre del fichero: se renombra"

    datos = frontmatter(nuevo)
    assert datos["id"] == fila["id"]
    assert datos["fase"] == "ganado"
    assert datos["valor"] == "1500"
    assert datos["empresa"] == "Acme S.L.", "lo que no llega en el PATCH se conserva"
    assert "title" not in datos

    # Y lo que sirve la API es lo que dice el fichero.
    leida = client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()
    assert (leida["title"], leida["properties"]["fase"]) == ("Acme SA", "ganado")


def test_renombrar_a_un_titulo_ocupado_no_pisa_la_otra_fila(client, vault_path):
    base = crear_database(client, title="Contactos", schema_def=CRM_SCHEMA)
    acme = crear_fila(client, base["id"], title="Acme", properties={"fase": "lead"})
    globex = crear_fila(client, base["id"], title="Globex", properties={"fase": "ganado"})
    carpeta = vault_path / "Bases" / "Contactos"

    renombrada = client.patch(
        f"/databases/{base['id']}/rows/{acme['id']}", json={"title": "Globex"}
    ).json()

    assert frontmatter(carpeta / "Globex.md")["id"] == globex["id"], "la otra fila sigue intacta"
    assert frontmatter(carpeta / "Globex.md")["fase"] == "ganado"
    assert (carpeta / "Globex (2).md").exists(), f"colisión sin sufijo: {sorted(p.name for p in carpeta.iterdir())}"
    assert frontmatter(carpeta / "Globex (2).md")["id"] == acme["id"]
    assert not (carpeta / "Acme.md").exists()

    # El título de la fila es el nombre que quedó en el disco, y no se pierde.
    assert renombrada["id"] == acme["id"]
    assert renombrada["title"] == "Globex (2)"
    filas = {f["id"]: f for f in client.get(f"/databases/{base['id']}/rows").json()}
    assert filas[acme["id"]]["title"] == "Globex (2)"
    assert filas[globex["id"]]["title"] == "Globex"
