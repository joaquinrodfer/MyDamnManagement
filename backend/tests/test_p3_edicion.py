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


def test_un_patch_con_titulo_y_propiedades_cambia_el_title_y_no_mueve_el_fichero(
    client, vault_path
):
    base = crear_database(client, title="Contactos", schema_def=CRM_SCHEMA)
    fila = crear_fila(
        client, base["id"], title="Acme", properties={"empresa": "Acme S.L.", "fase": "lead"}
    )
    carpeta = vault_path / "Bases" / "Contactos"
    ruta = carpeta / f"{fila['id'][:8]}.md"
    assert ruta.exists()

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

    # El fichero no se ha movido: mismo nombre (el id), ni uno nuevo ni uno
    # borrado en la carpeta.
    assert ruta.exists()
    assert sorted(p.name for p in carpeta.iterdir()) == [ruta.name]

    datos = frontmatter(ruta)
    assert datos["id"] == fila["id"]
    assert datos["title"] == "Acme SA", "el título vive en el frontmatter"
    assert datos["fase"] == "ganado"
    assert datos["valor"] == "1500"
    assert datos["empresa"] == "Acme S.L.", "lo que no llega en el PATCH se conserva"

    # Y lo que sirve la API es lo que dice el fichero.
    leida = client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()
    assert (leida["title"], leida["properties"]["fase"]) == ("Acme SA", "ganado")


def test_dos_filas_pueden_llamarse_igual_y_conservan_cada_fichero(client, vault_path):
    """El título ya no es el nombre: no hay ` (2)`, y cada fila mantiene su `.md`."""
    base = crear_database(client, title="Contactos", schema_def=CRM_SCHEMA)
    acme = crear_fila(client, base["id"], title="Acme", properties={"fase": "lead"})
    globex = crear_fila(client, base["id"], title="Globex", properties={"fase": "ganado"})
    carpeta = vault_path / "Bases" / "Contactos"

    renombrada = client.patch(
        f"/databases/{base['id']}/rows/{acme['id']}", json={"title": "Globex"}
    ).json()

    # Cada fila sigue en su fichero (el que dicta su id) y sin sufijos.
    rutas = {p.name: frontmatter(p) for p in carpeta.glob("*.md")}
    assert rutas[f"{acme['id'][:8]}.md"]["id"] == acme["id"]
    assert rutas[f"{globex['id'][:8]}.md"]["id"] == globex["id"]
    assert rutas[f"{acme['id'][:8]}.md"]["fase"] == "lead"
    assert rutas[f"{globex['id'][:8]}.md"]["fase"] == "ganado", "la otra fila sigue intacta"
    assert not any("(" in name for name in rutas), sorted(rutas)

    # Las dos se llaman igual y ninguna ha perdido su identidad.
    assert renombrada["id"] == acme["id"]
    assert renombrada["title"] == "Globex"
    filas = {f["id"]: f for f in client.get(f"/databases/{base['id']}/rows").json()}
    assert filas[acme["id"]]["title"] == "Globex"
    assert filas[globex["id"]]["title"] == "Globex"
