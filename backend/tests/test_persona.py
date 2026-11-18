"""Propiedad `person`: el responsable es un usuario, no texto libre.

Lo que se fija aquí (tarjeta «Propiedad Persona»):

1. `person` es un tipo del schema y su celda es el **id** de un usuario de la
   tabla `users`; el `.md` guarda ese id y la API devuelve además el nombre
   visible y el color (`people`), resueltos en una consulta por respuesta;
2. no acepta texto libre: un id que no existe (o un nombre escrito a mano) da
   **400** nombrando el **nombre visible** de la propiedad, no su clave;
3. una celda sin asignar es el vacío (`None`): `is_empty`/`is_not_empty` del
   filtro sirven sin un valor especial;
4. filtra (`eq`/`neq` por id y por nombre), ordena por nombre y reindexar
   reconstruye lo mismo;
5. renombrar al usuario cambia lo que enseña la celda y **no** a quién apunta
   (el `.md` conserva el id); si el usuario se borra, la celda lo dice
   (`display_name: null`) y no revienta;
6. una propiedad de texto se puede convertir a `person` solo si **todos** sus
   valores casan con usuarios; si uno no casa, se avisa y no se toca nada.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import index as indice
from app.config import settings
from app.vault import fm

PROYECTOS = [
    {"key": "responsable", "name": "Responsable", "type": "person"},
    {"key": "estado", "name": "Estado", "type": "select", "options": ["Abierto", "Cerrado"]},
]


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------

def crear_usuario(client, username: str, display_name: str) -> dict:
    """Un usuario más (el del fixture es admin, así que puede crearlos)."""
    respuesta = client.post(
        "/auth/users",
        json={
            "username": username,
            "display_name": display_name,
            "password": "contrasena-de-prueba",
        },
    )
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def crear_base(client, title: str, schema_def: list[dict]) -> dict:
    respuesta = client.post("/databases", json={"title": title, "schema_def": schema_def})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id: str, **cuerpo) -> dict:
    respuesta = client.post(f"/databases/{database_id}/rows", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def filas(client, database_id: str, **params) -> list[dict]:
    respuesta = client.get(f"/databases/{database_id}/rows", params=params)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def fichero_fila(vault_path: Path, base: str, fila: dict) -> Path:
    return vault_path / "Bases" / base / f"{fila['id'][:8]}.md"


def frontmatter(path: Path) -> dict:
    raw, _cuerpo = fm.split_document(path.read_text(encoding="utf-8"))
    assert raw is not None, f"{path} no tiene frontmatter"
    return fm.parse(raw).data


def huella(index_path: Path) -> tuple[str, dict]:
    return indice.dump_hash(str(index_path))


def crear_vista(client, database_id: str, config: dict) -> dict:
    respuesta = client.post(
        f"/databases/{database_id}/views", json={"name": "Vista", "type": "table", "config": config}
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


@pytest.fixture()
def personas(client):
    """Dos usuarios con nombre visible, y la base «Proyectos»."""
    ana = crear_usuario(client, "ana", "Ana")
    bea = crear_usuario(client, "bea", "Bea")
    base = crear_base(client, "Proyectos", PROYECTOS)
    return {"ana": ana, "bea": bea, "base": base}


# --------------------------------------------------------------------------
# 1. el tipo, la celda y el `.md`
# --------------------------------------------------------------------------

def test_una_propiedad_persona_guarda_el_id_del_usuario(client, personas, vault_path):
    ana = personas["ana"]
    base = personas["base"]

    prop = next(p for p in base["schema_def"] if p["key"] == "responsable")
    assert prop["type"] == "person"
    assert prop["name"] == "Responsable"

    fila = crear_fila(client, base["id"], title="Migrar la web", properties={"responsable": ana["id"]})
    assert fila["properties"]["responsable"] == ana["id"]
    assert fila["people"]["responsable"] == {
        "id": ana["id"],
        "display_name": "Ana",
        "username": "ana",
        "color": ana["color"],
        "avatar": ana["avatar"],
    }

    # El `.md` de la fila guarda el id, no el nombre: renombrar a la persona no
    # rompe la celda.
    datos = frontmatter(fichero_fila(vault_path, "Proyectos", fila))
    assert datos["responsable"] == ana["id"]

    # Y el schema del `.md` dice `person`, como cualquier otro tipo.
    base_md = frontmatter(vault_path / "Bases" / "Proyectos.md")
    assert next(p for p in base_md["schema"] if p["key"] == "responsable")["type"] == "person"


def test_una_celda_sin_asignar_es_el_vacio(client, personas, vault_path):
    base = personas["base"]
    fila = crear_fila(client, base["id"], title="Sin dueño")
    assert "responsable" not in fila["properties"]
    assert fila["people"] == {}

    # La clave no se escribe en el `.md`: una celda vacía no ocupa una línea.
    datos = frontmatter(fichero_fila(vault_path, "Proyectos", fila))
    assert "responsable" not in datos

    # Y vaciar la que estaba asignada la quita (no guarda `null` como texto).
    ana = personas["ana"]
    fila = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"responsable": ana["id"]}},
    ).json()
    assert fila["properties"]["responsable"] == ana["id"]
    fila = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"responsable": None}},
    ).json()
    assert "responsable" not in fila["properties"]
    assert "responsable" not in frontmatter(fichero_fila(vault_path, "Proyectos", fila))


# --------------------------------------------------------------------------
# 2. validación: 400 con el nombre visible y nada de texto libre
# --------------------------------------------------------------------------

def test_un_id_que_no_existe_da_400_con_el_nombre_de_la_propiedad(client, personas):
    base = personas["base"]
    respuesta = client.post(
        f"/databases/{base['id']}/rows",
        json={"title": "Sin dueño", "properties": {"responsable": "00000000000000000000000000000000"}},
    )
    assert respuesta.status_code == 400, respuesta.text
    detalle = respuesta.json()["detail"]
    assert "Responsable" in detalle
    assert "responsable" not in detalle, "el mensaje nombra la propiedad, no la clave"

    # Tampoco al editar una fila que ya existe.
    fila = crear_fila(client, base["id"], title="Alguien")
    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"responsable": "no-es-un-id"}},
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "Responsable" in respuesta.json()["detail"]


def test_el_texto_libre_no_vale_como_persona(client, personas):
    """Escribir «Joaquín» donde va una persona es exactamente el problema."""
    base = personas["base"]
    respuesta = client.post(
        f"/databases/{base['id']}/rows",
        json={"title": "Sin dueño", "properties": {"responsable": "Joaquín"}},
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "Responsable" in respuesta.json()["detail"]


def test_una_persona_no_admite_una_lista(client, personas):
    base = personas["base"]
    respuesta = client.post(
        f"/databases/{base['id']}/rows",
        json={"properties": {"responsable": [personas["ana"]["id"]]}},
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "Responsable" in respuesta.json()["detail"]


# --------------------------------------------------------------------------
# 3. filtros y orden
# --------------------------------------------------------------------------

@pytest.fixture()
def filas_repartidas(client, personas):
    base = personas["base"]
    ana = crear_fila(client, base["id"], title="De Ana", properties={"responsable": personas["ana"]["id"]})
    bea = crear_fila(client, base["id"], title="De Bea", properties={"responsable": personas["bea"]["id"]})
    suelta = crear_fila(client, base["id"], title="Sin dueño")
    return {"ana": ana, "bea": bea, "suelta": suelta, "base": base}


def test_filtro_eq_por_id_y_por_nombre_visible(client, filas_repartidas, personas):
    base = filas_repartidas["base"]
    ana = personas["ana"]

    por_id = crear_vista(client, base["id"], {"filters": [{"key": "responsable", "op": "eq", "value": ana["id"]}]})
    assert [f["title"] for f in filas(client, base["id"], view=por_id["id"])] == ["De Ana"]

    # El filtro se escribe con lo que se lee: el nombre visible también vale.
    por_nombre = crear_vista(client, base["id"], {"filters": [{"key": "responsable", "op": "eq", "value": "Ana"}]})
    assert [f["title"] for f in filas(client, base["id"], view=por_nombre["id"])] == ["De Ana"]


def test_filtro_is_empty_e_is_not_empty(client, filas_repartidas):
    base = filas_repartidas["base"]

    vacio = crear_vista(client, base["id"], {"filters": [{"key": "responsable", "op": "is_empty"}]})
    assert [f["title"] for f in filas(client, base["id"], view=vacio["id"])] == ["Sin dueño"]

    lleno = crear_vista(client, base["id"], {"filters": [{"key": "responsable", "op": "is_not_empty"}]})
    assert sorted(f["title"] for f in filas(client, base["id"], view=lleno["id"])) == ["De Ana", "De Bea"]


def test_filtro_neq_de_una_persona(client, filas_repartidas, personas):
    base = filas_repartidas["base"]
    vista = crear_vista(client, base["id"], {"filters": [{"key": "responsable", "op": "neq", "value": "Ana"}]})
    # `neq` es el complemento de `eq`: entra la de Bea y la que no tiene dueño.
    assert sorted(f["title"] for f in filas(client, base["id"], view=vista["id"])) == [
        "De Bea",
        "Sin dueño",
    ]


def test_ordenar_por_persona_va_por_nombre_no_por_id(client, personas):
    base = personas["base"]
    # «Zoe» se crea antes que «Ana»: por id no cuadraría con el orden alfabético.
    zoe = crear_usuario(client, "zoe", "Zoe")
    ana = personas["ana"]
    crear_fila(client, base["id"], title="De Zoe", properties={"responsable": zoe["id"]})
    crear_fila(client, base["id"], title="De Ana", properties={"responsable": ana["id"]})

    vista = crear_vista(client, base["id"], {"sort": [{"key": "responsable", "dir": "asc"}]})
    titulos = [f["title"] for f in filas(client, base["id"], view=vista["id"])]
    assert titulos == ["De Ana", "De Zoe"]


# --------------------------------------------------------------------------
# 4. reindexar reconstruye lo mismo
# --------------------------------------------------------------------------

def test_reindexar_reconstruye_las_personas_igual(client, filas_repartidas, personas, vault_path, index_path):
    base = filas_repartidas["base"]
    antes_filas = filas(client, base["id"])
    antes_hash, antes_cuentas = huella(index_path)

    respuesta = client.post("/reindex")
    assert respuesta.status_code == 200, respuesta.text

    despues_filas = filas(client, base["id"])
    # El orden de las filas no es parte del contrato de un reindexado (las
    # páginas se reinsertan en orden de ruta): se comparan por id.
    por_id = lambda lista: sorted(lista, key=lambda f: f["id"])
    assert por_id(despues_filas) == por_id(antes_filas)
    despues_hash, despues_cuentas = huella(index_path)
    assert despues_cuentas == antes_cuentas
    assert despues_hash == antes_hash, "el índice reconstruido no es idéntico al escrito por la API"

    # Y el `.md` sigue guardando el id (el reindexado no lo reescribe).
    datos = frontmatter(fichero_fila(vault_path, "Proyectos", filas_repartidas["ana"]))
    assert datos["responsable"] == personas["ana"]["id"]


# --------------------------------------------------------------------------
# 5. renombrar al usuario / borrarlo
# --------------------------------------------------------------------------

def test_renombrar_al_usuario_cambia_la_celda_y_no_a_quien_apunta(
    client, personas, filas_repartidas, vault_path
):
    base = filas_repartidas["base"]
    ana = personas["ana"]

    respuesta = client.patch(f"/auth/users/{ana['id']}", json={"display_name": "Ana María"})
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["display_name"] == "Ana María"

    fila = next(f for f in filas(client, base["id"]) if f["title"] == "De Ana")
    assert fila["properties"]["responsable"] == ana["id"], "la celda apunta al mismo id"
    assert fila["people"]["responsable"]["display_name"] == "Ana María", "y enseña el nombre nuevo"
    assert frontmatter(fichero_fila(vault_path, "Proyectos", fila))["responsable"] == ana["id"]


def test_solo_un_admin_renombra_usuarios(client, personas, acceso):
    """Renombrar es de administración; la celda no depende de eso."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as ajena:
        acceso(ajena)  # ya hay usuarios: entra como «prueba» (admin)
        ajena.post("/auth/logout")
        respuesta = ajena.post(
            "/auth/login", json={"username": "ana", "password": "contrasena-de-prueba"}
        )
        assert respuesta.status_code == 200, respuesta.text
        # `ana` no es admin: ni listar ni renombrar.
        assert ajena.get("/auth/users").status_code == 403
        cambio = ajena.patch(
            f"/auth/users/{personas['bea']['id']}", json={"display_name": "Bea 2"}
        )
        assert cambio.status_code == 403, cambio.text


def test_renombrar_a_un_nombre_vacio_es_un_400(client, personas):
    respuesta = client.patch(
        f"/auth/users/{personas['ana']['id']}", json={"display_name": "   "}
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "nombre visible" in respuesta.json()["detail"]


def test_un_usuario_borrado_no_revienta_la_celda(client, personas, filas_repartidas, vault_path):
    """Borrar usuarios no existe en la API; el modelo de lectura lo soporta igual.

    La comprobación es la que importa: si la fila de `users` desaparece, la
    celda conserva su id, la API sigue respondiendo y dice que ya no está.
    """
    base = filas_repartidas["base"]
    bea = personas["bea"]

    conexion = sqlite3.connect(settings.index_path)
    try:
        conexion.execute("PRAGMA foreign_keys = ON")
        conexion.execute("DELETE FROM sessions WHERE user_id = ?", (bea["id"],))
        conexion.execute("DELETE FROM users WHERE id = ?", (bea["id"],))
        conexion.commit()
    finally:
        conexion.close()

    # La base y sus filas siguen leyéndose.
    assert client.get(f"/databases/{base['id']}").status_code == 200
    fila = next(f for f in filas(client, base["id"]) if f["title"] == "De Bea")
    assert fila["properties"]["responsable"] == bea["id"], "el id se queda en la celda"
    assert fila["people"]["responsable"]["id"] == bea["id"]
    assert fila["people"]["responsable"]["display_name"] is None, "y se dice que ya no está"

    # Y reindexar no revienta ni cambia el fichero.
    assert client.post("/reindex").status_code == 200
    datos = frontmatter(fichero_fila(vault_path, "Proyectos", fila))
    assert datos["responsable"] == bea["id"]


# --------------------------------------------------------------------------
# 6. convertir una propiedad de texto existente
# --------------------------------------------------------------------------

def _base_de_texto(client) -> dict:
    return crear_base(
        client,
        "Tareas",
        [{"key": "responsable", "name": "Responsable", "type": "text"}],
    )


def _convertir_a_persona(client, base: dict, nombre: str = "Responsable") -> "object":
    return client.patch(
        f"/databases/{base['id']}",
        json={"schema_def": [{"key": "responsable", "name": nombre, "type": "person"}]},
    )


def test_convertir_texto_a_persona_cuando_los_valores_casan(client, personas, vault_path):
    base = _base_de_texto(client)
    desde_nombre = crear_fila(client, base["id"], title="Por nombre", properties={"responsable": "Ana"})
    desde_usuario = crear_fila(client, base["id"], title="Por @usuario", properties={"responsable": "bea"})
    sin_nada = crear_fila(client, base["id"], title="Sin dueño")

    respuesta = _convertir_a_persona(client, base)
    assert respuesta.status_code == 200, respuesta.text
    convertida = respuesta.json()
    assert next(p for p in convertida["schema_def"] if p["key"] == "responsable")["type"] == "person"

    filas_convertidas = {f["title"]: f for f in filas(client, base["id"])}
    assert filas_convertidas["Por nombre"]["properties"]["responsable"] == personas["ana"]["id"]
    assert filas_convertidas["Por @usuario"]["properties"]["responsable"] == personas["bea"]["id"]
    assert "responsable" not in filas_convertidas["Sin dueño"]["properties"]
    assert filas_convertidas["Por nombre"]["people"]["responsable"]["display_name"] == "Ana"

    # El `.md` de cada fila guarda ya el id, no el nombre.
    assert frontmatter(fichero_fila(vault_path, "Tareas", desde_nombre))["responsable"] == personas["ana"]["id"]
    assert frontmatter(fichero_fila(vault_path, "Tareas", desde_usuario))["responsable"] == personas["bea"]["id"]
    assert "responsable" not in frontmatter(fichero_fila(vault_path, "Tareas", sin_nada))


def test_no_convierte_si_un_valor_no_casa_con_ningun_usuario(client, personas, vault_path):
    base = _base_de_texto(client)
    crear_fila(client, base["id"], title="De Ana", properties={"responsable": "Ana"})
    rara = crear_fila(client, base["id"], title="De nadie", properties={"responsable": "Joaquín"})
    ruta = fichero_fila(vault_path, "Tareas", rara)
    antes = ruta.read_text(encoding="utf-8")

    respuesta = _convertir_a_persona(client, base)
    assert respuesta.status_code == 400, respuesta.text
    detalle = respuesta.json()["detail"]
    assert "Responsable" in detalle
    assert "Joaquín" in detalle

    # Nada se ha tocado: ni el schema, ni la fila que sí casaba, ni el `.md`.
    sigue = client.get(f"/databases/{base['id']}").json()
    assert next(p for p in sigue["schema_def"] if p["key"] == "responsable")["type"] == "text"
    filas_actuales = filas(client, base["id"])
    assert {f["title"]: f["properties"].get("responsable") for f in filas_actuales} == {
        "De Ana": "Ana",
        "De nadie": "Joaquín",
    }
    assert ruta.read_text(encoding="utf-8") == antes


def _base_de_texto_con_estado(client) -> dict:
    return crear_base(
        client,
        "Tareas",
        [
            {"key": "responsable", "name": "Responsable", "type": "text"},
            {"key": "estado", "name": "Estado", "type": "text"},
        ],
    )


def _renombrar_y_convertir(client, base: dict):
    """El guardado del diálogo «Propiedades»: renombrar y cambiar el tipo a la vez.

    `estado` -> «Situación» (sigue texto) y `responsable` -> «Dueño» (persona),
    en la **misma** petición: es el camino normal de la interfaz, no un caso de
    laboratorio.
    """
    return client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {"key": "responsable", "name": "Dueño", "type": "person"},
                {"key": "estado", "name": "Situación", "type": "text"},
            ]
        },
    )


def test_convertir_y_renombrar_la_misma_propiedad_en_un_guardado(client, personas, vault_path):
    """Convertir a persona no puede depender de que no renombres la propiedad.

    La conversión se validaba con la clave vieja y el renombrado ya había
    escrito la nueva en las filas: no encontraba ningún valor que casar y el
    schema quedaba en `person` con texto libre en el `.md` (celdas «(ya no
    está)»). Ahora se lee con la clave vieja y se escribe con la final.
    """
    base = _base_de_texto_con_estado(client)
    por_nombre = crear_fila(
        client,
        base["id"],
        title="Por nombre",
        properties={"responsable": "Ana", "estado": "Abierto"},
    )
    por_usuario = crear_fila(
        client,
        base["id"],
        title="Por @usuario",
        properties={"responsable": "bea", "estado": "Abierto"},
    )

    respuesta = _renombrar_y_convertir(client, base)
    assert respuesta.status_code == 200, respuesta.text
    schema = {p["key"]: p for p in respuesta.json()["schema_def"]}
    assert schema["dueno"]["type"] == "person"
    assert schema["dueno"]["name"] == "Dueño"
    assert "situacion" in schema, "la otra propiedad también se renombra"
    assert "estado" not in schema

    convertidas = {f["title"]: f for f in filas(client, base["id"])}
    assert convertidas["Por nombre"]["properties"]["dueno"] == personas["ana"]["id"]
    assert convertidas["Por @usuario"]["properties"]["dueno"] == personas["bea"]["id"]
    assert convertidas["Por nombre"]["people"]["dueno"]["display_name"] == "Ana"
    assert convertidas["Por nombre"]["properties"]["situacion"] == "Abierto"

    # El `.md` guarda el id bajo la clave nueva; la clave vieja no sobrevive.
    datos = frontmatter(fichero_fila(vault_path, "Tareas", por_nombre))
    assert datos["dueno"] == personas["ana"]["id"]
    assert datos["situacion"] == "Abierto"
    assert "responsable" not in datos and "estado" not in datos
    datos = frontmatter(fichero_fila(vault_path, "Tareas", por_usuario))
    assert datos["dueno"] == personas["bea"]["id"]


def test_un_400_de_la_conversion_no_deja_renombrada_otra_propiedad(client, personas, vault_path):
    """Si la conversión rechaza, no se ha escrito NADA (ni el renombrado).

    El renombrado de filas iba antes que la validación: un valor que no casaba
    devolvía 400 con «no se ha tocado ninguna fila» mientras el `.md` ya tenía
    la clave nueva y el schema seguía con la vieja (esa columna aparecía vacía
    en la app).
    """
    base = _base_de_texto_con_estado(client)
    fila = crear_fila(
        client,
        base["id"],
        title="De nadie",
        properties={"responsable": "Joaquín", "estado": "Abierto"},
    )
    ruta = fichero_fila(vault_path, "Tareas", fila)
    antes = ruta.read_text(encoding="utf-8")

    respuesta = _renombrar_y_convertir(client, base)
    assert respuesta.status_code == 400, respuesta.text
    detalle = respuesta.json()["detail"]
    assert "Dueño" in detalle, "el mensaje nombra la propiedad, no la clave"
    assert "Joaquín" in detalle

    # El fichero de la fila está byte a byte como estaba: ni `situacion` ni id.
    assert ruta.read_text(encoding="utf-8") == antes

    # Y el schema de la base sigue siendo el viejo.
    sigue = client.get(f"/databases/{base['id']}").json()
    assert [p["key"] for p in sigue["schema_def"]] == ["responsable", "estado"]
    assert all(p["type"] == "text" for p in sigue["schema_def"])

    # La app sigue viendo los dos valores donde estaban.
    actual = next(f for f in filas(client, base["id"]) if f["id"] == fila["id"])
    assert actual["properties"] == {"responsable": "Joaquín", "estado": "Abierto"}


def test_no_se_convierte_a_persona_desde_otro_tipo(client):
    base = crear_base(
        client,
        "Tareas",
        [{"key": "estado", "name": "Estado", "type": "select", "options": ["Abierto"]}],
    )
    crear_fila(client, base["id"], title="Una", properties={"estado": "Abierto"})

    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={"schema_def": [{"key": "estado", "name": "Estado", "type": "person"}]},
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "Persona" in respuesta.json()["detail"]


def test_el_md_exportado_lleva_el_nombre_no_el_id(client, personas):
    """La tabla exportada dice lo mismo que el panel: el nombre visible."""
    base = personas["base"]
    crear_fila(client, base["id"], title="De Ana", properties={"responsable": personas["ana"]["id"]})
    nota = client.post(
        "/pages",
        json={
            "title": "Panel",
            "body_markdown": f"```mdm-database\nid: {base['id']}\n```",
        },
    ).json()

    exportado = client.get(f"/pages/{nota['id']}/export.md")
    assert exportado.status_code == 200, exportado.text
    cuerpo = exportado.text
    assert "Ana" in cuerpo
    assert personas["ana"]["id"] not in cuerpo, "el .md exportado no enseña el id"


# --------------------------------------------------------------------------
# 7. el directorio de personas
# --------------------------------------------------------------------------

def test_people_es_el_directorio_para_asignar(client, personas):
    respuesta = client.get("/people")
    assert respuesta.status_code == 200, respuesta.text
    gente = respuesta.json()
    nombres = {u["display_name"] for u in gente}
    assert {"Ana", "Bea"} <= nombres
    ana = next(u for u in gente if u["display_name"] == "Ana")
    assert ana["id"] == personas["ana"]["id"]
    assert ana["username"] == "ana"
    assert ana["color"]
    assert "password_hash" not in ana and "salt" not in ana


def test_people_pide_sesion(client, personas):
    """El directorio no es público: sin cookie, la API responde 401."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonimo:
        assert anonimo.get("/people").status_code == 401


# --------------------------------------------------------------------------
# 8. lectura laxa de un `.md` escrito a mano
# --------------------------------------------------------------------------

def test_un_md_con_texto_libre_en_una_persona_se_lee_y_se_dice_que_no_esta(
    client, personas, vault_path
):
    """El fichero es del usuario: no se reescribe, y la celda lo dice."""
    base = personas["base"]
    fila = crear_fila(client, base["id"], title="A mano")
    ruta = fichero_fila(vault_path, "Proyectos", fila)
    # La clave va en el frontmatter tal y como la escribiría una persona.
    texto = ruta.read_text(encoding="utf-8").replace(
        "---\n", "---\nresponsable: Joaquín\n", 1
    )
    ruta.write_text(texto, encoding="utf-8")
    assert client.post("/reindex").status_code == 200

    leida = next(f for f in filas(client, base["id"]) if f["title"] == "A mano")
    assert leida["properties"]["responsable"] == "Joaquín"
    assert leida["people"]["responsable"]["display_name"] is None
    assert "responsable: Joaquín" in ruta.read_text(encoding="utf-8"), "el .md no se toca"
