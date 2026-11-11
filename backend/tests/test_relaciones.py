"""Relaciones entre bases (1/2): modelo, validación y lectura con título.

Lo que se fija aquí (tarjeta «Relaciones entre bases (1/2)»):

1. una propiedad `relation` declara `target_database` (y `multiple`) en el
   `schema` del `.md`, y la API lo devuelve;
2. el valor de la celda es el **`id`** de la fila destino; al guardar se valida
   que exista, que sea de la base destino y que el número de ids cuadre, con un
   400 que nombra el **nombre visible** de la propiedad;
3. al leer, la API resuelve el título de cada fila referenciada
   (`related: [{id, title, base_id}]`) **en una sola consulta por respuesta**;
4. borrar la fila destino deja la celda apuntando a un id que ya no resuelve
   (`title: null`), no la vacía en silencio; borrar la base destino marca la
   propiedad (`warning`) sin romper el render ni el reindexado;
5. un `.md` viejo con texto libre en una propiedad `relation` se lee como «sin
   valor» y **no se reescribe**;
6. reindexar desde el vault reconstruye lo mismo y el título sigue resolviendo.
"""

from __future__ import annotations

from pathlib import Path

from app import index as indice
from app.properties import relation_ids
from app.repository import Repository
from app.vault import fm

CONTACTOS = [{"key": "empresa", "name": "Empresa", "type": "text"}]
REUNIONES = [
    {"key": "fecha", "name": "Fecha", "type": "date"},
]


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------

def crear_base(client, title: str, schema_def: list[dict]) -> dict:
    respuesta = client.post("/databases", json={"title": title, "schema_def": schema_def})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id: str, **cuerpo) -> dict:
    respuesta = client.post(f"/databases/{database_id}/rows", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def fichero_base(vault_path: Path, titulo: str) -> Path:
    return vault_path / "Bases" / f"{titulo}.md"


def fichero_fila(vault_path: Path, base: str, fila: dict) -> Path:
    return vault_path / "Bases" / base / f"{fila['id'][:8]}.md"


def frontmatter(path: Path) -> dict:
    raw, _cuerpo = fm.split_document(path.read_text(encoding="utf-8"))
    assert raw is not None, f"{path} no tiene frontmatter"
    return fm.parse(raw).data


def dos_bases(client, *, multiple: bool = False) -> tuple[dict, dict]:
    """Contactos (destino) y Reuniones, con una `relacion` de Reuniones a Contactos."""
    contactos = crear_base(client, "Contactos", CONTACTOS)
    reuniones = crear_base(
        client,
        "Reuniones",
        REUNIONES
        + [
            {
                "key": "contacto",
                "name": "Contacto",
                "type": "relation",
                "target_database": contactos["id"],
                "multiple": multiple or None,
            }
        ],
    )
    return contactos, reuniones


# --------------------------------------------------------------------------
# 1. el modelo: la relación declara su base destino y vive en el `.md`
# --------------------------------------------------------------------------

def test_una_propiedad_de_relacion_declara_su_base_destino(client, vault_path):
    contactos, reuniones = dos_bases(client)

    relacion = next(p for p in reuniones["schema_def"] if p["key"] == "contacto")
    assert relacion["type"] == "relation"
    assert relacion["target_database"] == contactos["id"]
    assert "multiple" not in relacion      # simple: la clave no se escribe

    datos = frontmatter(fichero_base(vault_path, "Reuniones"))
    assert datos["schema"][1] == {
        "key": "contacto",
        "name": "Contacto",
        "type": "relation",
        "target_database": contactos["id"],
    }


def test_renombrar_una_propiedad_de_relacion_no_le_quita_la_base_destino(client):
    """El diálogo de propiedades todavía no pregunta por `target_database`.

    Guardar el resto de la base (un renombrado) no puede dejar la relación sin
    destino: si la petición no lo trae, se conserva el que ya tenía.
    """
    contactos, reuniones = dos_bases(client)

    respuesta = client.patch(
        f"/databases/{reuniones['id']}",
        json={
            "schema_def": [
                {"key": "fecha", "name": "Fecha", "type": "date"},
                {"key": "contacto", "name": "Persona", "type": "relation"},
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    relacion = next(p for p in respuesta.json()["schema_def"] if p["name"] == "Persona")
    assert relacion["key"] == "persona"      # el renombrado deriva la clave
    assert relacion["target_database"] == contactos["id"]


def test_una_relacion_sin_base_destino_no_revienta_y_se_avisa(client):
    """Un `.md` con una `relation` sin destino se lee; la propiedad lo dice."""
    base = crear_base(
        client, "Pedidos", [{"name": "Cliente", "type": "relation"}]
    )
    respuesta = client.get(f"/databases/{base['id']}")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["schema_def"][0]["warning"] == (
        "la propiedad no declara una base destino válida"
    )


# --------------------------------------------------------------------------
# 2. guardar y validar
# --------------------------------------------------------------------------

def test_guardar_un_id_valido_lo_escribe_en_el_md(client, vault_path):
    contactos, reuniones = dos_bases(client)
    ana = crear_fila(client, contactos["id"], title="Ana Pérez", properties={})

    fila = crear_fila(
        client, reuniones["id"], title="Kickoff", properties={"contacto": ana["id"]}
    )
    assert fila["properties"]["contacto"] == ana["id"]

    datos = frontmatter(fichero_fila(vault_path, "Reuniones", fila))
    assert datos["contacto"] == ana["id"]


def test_guardar_un_id_de_otra_base_es_400_con_el_nombre_visible(client):
    contactos, reuniones = dos_bases(client)
    proveedores = crear_base(client, "Proveedores", CONTACTOS)
    ajeno = crear_fila(client, proveedores["id"], title="Acme", properties={})

    respuesta = client.post(
        f"/databases/{reuniones['id']}/rows",
        json={"title": "Kickoff", "properties": {"contacto": ajeno["id"]}},
    )
    assert respuesta.status_code == 400
    assert "«Contacto»" in respuesta.json()["detail"]
    assert "no es de la base" in respuesta.json()["detail"]
    # El mensaje nombra lo que el usuario ve, nunca la clave interna.
    assert "'contacto'" not in respuesta.json()["detail"]


def test_guardar_un_id_que_no_existe_es_400(client):
    _contactos, reuniones = dos_bases(client)
    fantasma = "00000000-0000-4000-8000-000000000000"

    respuesta = client.post(
        f"/databases/{reuniones['id']}/rows",
        json={"title": "Kickoff", "properties": {"contacto": fantasma}},
    )
    assert respuesta.status_code == 400
    assert "«Contacto»" in respuesta.json()["detail"]
    assert "no existe" in respuesta.json()["detail"]


def test_guardar_basura_es_400(client):
    _contactos, reuniones = dos_bases(client)

    respuesta = client.post(
        f"/databases/{reuniones['id']}/rows",
        json={"title": "Kickoff", "properties": {"contacto": "Ana Pérez"}},
    )
    assert respuesta.status_code == 400
    assert "«Contacto»" in respuesta.json()["detail"]
    assert "no es un id de fila válido" in respuesta.json()["detail"]


def test_una_relacion_simple_no_acepta_una_lista_y_una_multiple_no_acepta_un_id(client):
    contactos, reuniones = dos_bases(client)
    ana = crear_fila(client, contactos["id"], title="Ana", properties={})

    simple = client.post(
        f"/databases/{reuniones['id']}/rows",
        json={"title": "X", "properties": {"contacto": [ana["id"]]}},
    )
    assert simple.status_code == 400
    assert "una sola fila" in simple.json()["detail"]

    contactos2, reuniones2 = dos_bases_multiple(client)
    ana2 = crear_fila(client, contactos2["id"], title="Ana", properties={})
    multiple = client.post(
        f"/databases/{reuniones2['id']}/rows",
        json={"title": "X", "properties": {"contacto": ana2["id"]}},
    )
    assert multiple.status_code == 400
    assert "varias filas" in multiple.json()["detail"]


def dos_bases_multiple(client) -> tuple[dict, dict]:
    contactos = crear_base(client, "Contactos", CONTACTOS)
    reuniones = crear_base(
        client,
        "Reuniones",
        REUNIONES
        + [
            {
                "key": "contacto",
                "name": "Contacto",
                "type": "relation",
                "target_database": contactos["id"],
                "multiple": True,
            }
        ],
    )
    return contactos, reuniones


def test_una_relacion_multiple_guarda_una_lista_de_ids(client, vault_path):
    contactos, reuniones = dos_bases_multiple(client)
    ana = crear_fila(client, contactos["id"], title="Ana", properties={})
    luis = crear_fila(client, contactos["id"], title="Luis", properties={})

    fila = crear_fila(
        client,
        reuniones["id"],
        title="Kickoff",
        properties={"contacto": [ana["id"], luis["id"], ana["id"]]},
    )
    # Sin duplicados: una fila se enlaza una vez.
    assert fila["properties"]["contacto"] == [ana["id"], luis["id"]]
    assert [r["title"] for r in fila["related"]["contacto"]] == ["Ana", "Luis"]

    datos = frontmatter(fichero_fila(vault_path, "Reuniones", fila))
    assert datos["contacto"] == [ana["id"], luis["id"]]


def test_una_relacion_multiple_vacia_se_guarda_como_sin_valor(client, vault_path):
    _contactos, reuniones = dos_bases_multiple(client)
    fila = crear_fila(
        client, reuniones["id"], title="Kickoff", properties={"contacto": []}
    )
    assert fila["properties"].get("contacto") is None
    assert fila["related"] == {}
    assert "contacto" not in frontmatter(fichero_fila(vault_path, "Reuniones", fila))


# --------------------------------------------------------------------------
# 3. lectura: el título lo resuelve el servidor, en una consulta
# --------------------------------------------------------------------------

def test_la_fila_devuelve_el_titulo_de_la_fila_referenciada(client):
    contactos, reuniones = dos_bases(client)
    ana = crear_fila(client, contactos["id"], title="Ana Pérez", properties={})
    fila = crear_fila(
        client, reuniones["id"], title="Kickoff", properties={"contacto": ana["id"]}
    )

    leida = client.get(f"/databases/{reuniones['id']}/rows/{fila['id']}").json()
    assert leida["related"]["contacto"] == [
        {"id": ana["id"], "title": "Ana Pérez", "base_id": contactos["id"]}
    ]


def test_el_titulo_se_resuelve_al_renombrar_la_fila_destino(client):
    """La celda guarda el id, no el título: renombrar no rompe la relación."""
    contactos, reuniones = dos_bases(client)
    ana = crear_fila(client, contactos["id"], title="Ana Pérez", properties={})
    fila = crear_fila(
        client, reuniones["id"], title="Kickoff", properties={"contacto": ana["id"]}
    )

    renombrada = client.patch(
        f"/databases/{contactos['id']}/rows/{ana['id']}", json={"title": "Ana P."}
    )
    assert renombrada.status_code == 200, renombrada.text

    leida = client.get(f"/databases/{reuniones['id']}/rows/{fila['id']}").json()
    assert leida["related"]["contacto"][0]["title"] == "Ana P."


def test_veinte_filas_resuelven_sus_titulos_en_una_sola_consulta(
    client, index_path, monkeypatch
):
    """Sin N+1: el coste de resolver las relaciones es el de la respuesta.

    Dos pruebas, la de la API y la del SQL:

    - el endpoint llama a la resolución **una vez** para las 20 filas (y le pasa
      los 20 ids juntos);
    - contra el índice, esa resolución es **una** sentencia `SELECT`; hacerlo
      celda a celda serían 20.
    """
    contactos, reuniones = dos_bases(client)
    for numero in range(20):
        ana = crear_fila(client, contactos["id"], title=f"Persona {numero}", properties={})
        crear_fila(
            client, reuniones["id"], title=f"Reunión {numero}", properties={"contacto": ana["id"]}
        )

    original = Repository.row_refs
    llamadas: list[int] = []

    def contar(self, ids):
        ids = list(ids)
        llamadas.append(len(ids))
        return original(self, ids)

    with monkeypatch.context() as mp:
        mp.setattr(Repository, "row_refs", contar)
        filas = client.get(f"/databases/{reuniones['id']}/rows").json()

    assert len(filas) == 20
    assert all(f["related"]["contacto"][0]["title"].startswith("Persona") for f in filas)
    assert len(llamadas) == 1, f"una consulta por respuesta, no {len(llamadas)}"
    assert llamadas[0] == 20

    # La misma resolución contra el índice, contando sentencias de verdad.
    conn = indice.connect(str(index_path))
    try:
        repo = Repository(conn)
        base = repo.get_database(filas[0]["database_id"])
        sentencias: list[str] = []
        conn.set_trace_callback(sentencias.append)
        leidas = repo.list_rows(base.id)
        ids = relation_ids(base.schema_def, [r.properties for r in leidas])
        repo.row_refs(ids)
        resolucion = [s for s in sentencias if "FROM rows r JOIN pages p" in s]
        assert len(resolucion) == 1, resolucion

        # Y lo que sería el N+1, para que la diferencia se vea: una por fila.
        conn.set_trace_callback(None)
        sentencias.clear()
        conn.set_trace_callback(sentencias.append)
        for row in leidas:
            repo.row_refs([row.properties["contacto"]])
        celda_a_celda = [s for s in sentencias if "FROM rows r JOIN pages p" in s]
        assert len(celda_a_celda) == 20
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 4. borrar la fila destino / la base destino
# --------------------------------------------------------------------------

def test_borrar_la_fila_destino_deja_el_id_y_el_titulo_a_null(client, vault_path):
    contactos, reuniones = dos_bases(client)
    ana = crear_fila(client, contactos["id"], title="Ana", properties={})
    fila = crear_fila(
        client, reuniones["id"], title="Kickoff", properties={"contacto": ana["id"]}
    )

    borrada = client.delete(f"/databases/{contactos['id']}/rows/{ana['id']}")
    assert borrada.status_code == 204

    leida = client.get(f"/databases/{reuniones['id']}/rows/{fila['id']}").json()
    # La celda no se vacía en silencio: el id sigue (el `.md` no se ha tocado) y
    # el título sale `null`, que es el aviso de «esa fila ya no está».
    assert leida["properties"]["contacto"] == ana["id"]
    assert leida["related"]["contacto"] == [
        {"id": ana["id"], "title": None, "base_id": contactos["id"]}
    ]
    # Y el `.md` de la fila sigue apuntando al id: la relación no se rompe sola.
    datos = frontmatter(fichero_fila(vault_path, "Reuniones", fila))
    assert datos["contacto"] == ana["id"]


def test_borrar_una_de_varias_filas_deja_solo_esa_sin_titulo(client):
    """En una relación múltiple, cada id resuelve (o no) por su cuenta."""
    contactos, reuniones = dos_bases_multiple(client)
    ana = crear_fila(client, contactos["id"], title="Ana", properties={})
    luis = crear_fila(client, contactos["id"], title="Luis", properties={})
    fila = crear_fila(
        client,
        reuniones["id"],
        title="Kickoff",
        properties={"contacto": [ana["id"], luis["id"]]},
    )

    assert client.delete(f"/databases/{contactos['id']}/rows/{luis['id']}").status_code == 204

    leida = client.get(f"/databases/{reuniones['id']}/rows/{fila['id']}").json()
    assert leida["properties"]["contacto"] == [ana["id"], luis["id"]]
    assert [(r["id"], r["title"]) for r in leida["related"]["contacto"]] == [
        (ana["id"], "Ana"),
        (luis["id"], None),
    ]


def test_borrar_la_base_destino_marca_la_propiedad_sin_romper_nada(client):
    contactos, reuniones = dos_bases(client)
    ana = crear_fila(client, contactos["id"], title="Ana", properties={})
    fila = crear_fila(
        client, reuniones["id"], title="Kickoff", properties={"contacto": ana["id"]}
    )

    assert client.delete(f"/databases/{contactos['id']}").status_code == 204

    # La base que enlazaba sigue abriendo, con su propiedad marcada.
    base = client.get(f"/databases/{reuniones['id']}")
    assert base.status_code == 200, base.text
    relacion = next(p for p in base.json()["schema_def"] if p["key"] == "contacto")
    assert relacion["warning"] == "la base destino ya no existe"
    assert relacion["target_database"] == contactos["id"]

    # La fila también: el id sigue ahí y no resuelve.
    leida = client.get(f"/databases/{reuniones['id']}/rows/{fila['id']}")
    assert leida.status_code == 200, leida.text
    assert leida.json()["related"]["contacto"][0]["title"] is None

    # Y reindexar no revienta ni se lleva la propiedad por delante.
    assert client.post("/reindex").status_code == 200
    despues = client.get(f"/databases/{reuniones['id']}").json()
    relacion = next(p for p in despues["schema_def"] if p["key"] == "contacto")
    assert relacion["target_database"] == contactos["id"]
    assert relacion["warning"] == "la base destino ya no existe"


# --------------------------------------------------------------------------
# 5. compatibilidad: un `.md` con texto libre viejo no se rompe ni se reescribe
# --------------------------------------------------------------------------

def test_un_texto_libre_viejo_se_lee_sin_valor_y_no_se_reescribe(client, vault_path):
    contactos, reuniones = dos_bases(client)
    ana = crear_fila(client, contactos["id"], title="Ana", properties={})
    fila = crear_fila(
        client, reuniones["id"], title="Kickoff", properties={"contacto": ana["id"]}
    )

    # Alguien escribió esto a mano cuando la propiedad era texto libre.
    ruta = fichero_fila(vault_path, "Reuniones", fila)
    ruta.write_text(
        ruta.read_text(encoding="utf-8").replace(f"contacto: {ana['id']}", "contacto: Ana Pérez"),
        encoding="utf-8",
    )
    antes = ruta.read_bytes()
    assert client.post("/reindex").status_code == 200

    leida = client.get(f"/databases/{reuniones['id']}/rows/{fila['id']}").json()
    assert leida["properties"]["contacto"] is None      # sin valor, no un error
    assert leida["related"] == {}

    # Ni la lectura ni el reindexado tocan el fichero de nadie.
    assert ruta.read_bytes() == antes

    # Y escribir encima del texto viejo se rechaza en claro (B2: el índice no
    # puede representarlo, así que la tabla lo enseña vacío y guardar encima
    # borraría del `.md` un dato que nadie ha visto). El resto de la fila se
    # sigue editando: solo se protege esa celda.
    encima = client.patch(
        f"/databases/{reuniones['id']}/rows/{fila['id']}",
        json={"properties": {"contacto": ana["id"]}},
    )
    assert encima.status_code == 400
    assert "Ana Pérez" in encima.json()["detail"]
    assert ruta.read_bytes() == antes


# --------------------------------------------------------------------------
# 6. el vault es la verdad
# --------------------------------------------------------------------------

def test_reindexar_reconstruye_la_relacion_y_el_titulo(client, vault_path):
    contactos, reuniones = dos_bases(client)
    ana = crear_fila(client, contactos["id"], title="Ana Pérez", properties={})
    fila = crear_fila(
        client, reuniones["id"], title="Kickoff", properties={"contacto": ana["id"]}
    )

    assert client.post("/reindex").status_code == 200

    base = client.get(f"/databases/{reuniones['id']}").json()
    relacion = next(p for p in base["schema_def"] if p["key"] == "contacto")
    assert relacion["target_database"] == contactos["id"]

    leida = client.get(f"/databases/{reuniones['id']}/rows/{fila['id']}").json()
    assert leida["properties"]["contacto"] == ana["id"]
    assert leida["related"]["contacto"][0]["title"] == "Ana Pérez"
