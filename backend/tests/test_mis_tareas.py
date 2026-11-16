"""`GET /my-tasks`: «Mis tareas», la vista que cruza todas las bases.

Lo que se fija aquí (tarjeta «Mis tareas»):

1. salen las filas de **dos bases distintas** cuyo responsable soy yo, cada una
   con el **nombre de su base** (una lista mezclada no sirve);
2. **solo las mías**: la fila de otra persona no sale, y una base sin ninguna
   propiedad `person` no aporta nada;
3. el **estado** sale si la base tiene una propiedad de selección que haga de
   estado («Estado» o clave `estado`) y, si no la tiene, no se enseña;
4. la **fecha** es la primera propiedad `date` de la base, y una fecha que no
   se puede leer no se inventa;
5. las filas **cerradas** no salen, y «cerrada» lo dice cada base con su
   regla (`closed: {property, values}`, la misma del contador de subtareas y de
   los filtros). Una base **sin** regla no tiene estado y entra toda: no hay
   nada que diga que está hecha. Los valores que las bases declaran viajan con
   la respuesta (`closed_values`) para que la interfaz explique el criterio;
6. orden por fecha ascendente y, al final, las que no tienen fecha;
7. sin sesión, 401: esto es «mis» tareas.
"""

from __future__ import annotations

import pytest

PROYECTOS = [
    {"key": "responsable", "name": "Responsable", "type": "person"},
    {
        "key": "estado",
        "name": "Estado",
        "type": "select",
        "options": ["Abierto", "En curso", "Hecho", "Cerrado"],
    },
    {"key": "vence", "name": "Vence", "type": "date"},
]

# Una base sin estado ni fecha: sus filas entran todas y sin adornos.
NOTAS = [{"key": "responsable", "name": "Responsable", "type": "person"}]

# Sin ninguna propiedad `person`: no puede tener nada mío.
LIBROS = [{"key": "autor", "name": "Autor", "type": "text"}]


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------

def yo(client) -> str:
    respuesta = client.get("/auth/me")
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()["id"]


def crear_usuario(client, username: str, display_name: str) -> dict:
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


def crear_base(
    client, title: str, schema_def: list[dict], closed: dict | None = None
) -> dict:
    cuerpo = {"title": title, "schema_def": schema_def}
    if closed is not None:
        cuerpo["closed"] = closed
    respuesta = client.post("/databases", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


# La regla de la base «Proyectos»: los dos estados que cuentan como cerrada. Sin
# ella la vista no esconde nada, porque una base sin regla no tiene estado.
CERRADA_EN_PROYECTOS = {"property": "estado", "values": ["Hecho", "Cerrado"]}


def crear_fila(client, database_id: str, title: str, properties: dict | None = None) -> dict:
    respuesta = client.post(
        f"/databases/{database_id}/rows", json={"title": title, "properties": properties or {}}
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def mis_tareas(client) -> dict:
    respuesta = client.get("/my-tasks")
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def titulos(payload: dict) -> list[str]:
    return [t["title"] for t in payload["tasks"]]


@pytest.fixture()
def escenario(client):
    """Dos bases con responsables («Proyectos» con estado y fecha, «Notas» sin
    nada de eso), una tercera sin propiedad `person`, y otra persona."""
    ana = crear_usuario(client, "ana", "Ana")
    proyectos = crear_base(client, "Proyectos", PROYECTOS, closed=CERRADA_EN_PROYECTOS)
    notas = crear_base(client, "Notas", NOTAS)
    libros = crear_base(client, "Libros", LIBROS)
    return {
        "yo": yo(client),
        "ana": ana,
        "proyectos": proyectos,
        "notas": notas,
        "libros": libros,
    }


# --------------------------------------------------------------------------
# 1 y 2. de dónde sale cada cosa, y solo lo mío
# --------------------------------------------------------------------------

def test_las_mias_de_dos_bases_salen_con_el_nombre_de_su_base(client, escenario):
    crear_fila(
        client,
        escenario["proyectos"]["id"],
        "Migrar la web",
        {"responsable": escenario["yo"], "estado": "En curso", "vence": "2026-11-20"},
    )
    crear_fila(client, escenario["notas"]["id"], "Comprar café", {"responsable": escenario["yo"]})

    datos = mis_tareas(client)

    assert datos["count"] == 2
    por_base = {t["database_title"]: t["title"] for t in datos["tasks"]}
    assert por_base == {"Proyectos": "Migrar la web", "Notas": "Comprar café"}
    assert len({t["database_id"] for t in datos["tasks"]}) == 2


def test_la_fila_de_otra_persona_no_es_mia(client, escenario):
    crear_fila(
        client, escenario["proyectos"]["id"], "Lo de Ana", {"responsable": escenario["ana"]["id"]}
    )
    crear_fila(
        client, escenario["proyectos"]["id"], "Lo mío", {"responsable": escenario["yo"]}
    )

    datos = mis_tareas(client)

    assert titulos(datos) == ["Lo mío"]
    assert datos["count"] == 1


def test_una_fila_sin_responsable_no_sale(client, escenario):
    crear_fila(client, escenario["proyectos"]["id"], "De nadie", {"estado": "Abierto"})

    assert mis_tareas(client)["count"] == 0


def test_una_base_sin_propiedad_persona_no_aporta_nada(client, escenario):
    crear_fila(client, escenario["libros"]["id"], "El Quijote", {"autor": "Cervantes"})

    assert mis_tareas(client)["count"] == 0


# --------------------------------------------------------------------------
# 3. el estado
# --------------------------------------------------------------------------

def test_el_estado_sale_cuando_la_base_lo_tiene(client, escenario):
    crear_fila(
        client,
        escenario["proyectos"]["id"],
        "Pintar la valla",
        {"responsable": escenario["yo"], "estado": "En curso"},
    )

    tarea = mis_tareas(client)["tasks"][0]

    assert tarea["status"] == {"key": "estado", "name": "Estado", "value": "En curso"}


def test_una_base_sin_estado_no_ensena_estado(client, escenario):
    crear_fila(client, escenario["notas"]["id"], "Regar", {"responsable": escenario["yo"]})

    tarea = mis_tareas(client)["tasks"][0]

    assert tarea["status"] is None
    assert tarea["date"] is None


def test_el_estado_se_reconoce_por_el_nombre_aunque_la_clave_sea_otra(client, escenario):
    """Una base vieja puede tener «Estado» con la clave `situacion`."""
    rara = crear_base(
        client,
        "Rara",
        [
            {"key": "responsable", "name": "Responsable", "type": "person"},
            {"key": "situacion", "name": "Estado", "type": "select", "options": ["Hecho", "Pendiente"]},
        ],
    )
    crear_fila(
        client, rara["id"], "Expediente", {"responsable": escenario["yo"], "situacion": "Pendiente"}
    )

    tarea = mis_tareas(client)["tasks"][0]

    assert tarea["status"] == {"key": "situacion", "name": "Estado", "value": "Pendiente"}


def test_una_propiedad_estado_de_texto_no_hace_de_estado(client, escenario):
    """Un `text` llamado «Estado» es texto libre: sus palabras no cierran nada.

    Si contara, escribir «hecho» en una nota cualquiera la haría desaparecer de
    la vista. Solo una **selección** hace de estado.
    """
    libre = crear_base(
        client,
        "Texto libre",
        [
            {"key": "responsable", "name": "Responsable", "type": "person"},
            {"key": "estado", "name": "Estado", "type": "text"},
        ],
    )
    crear_fila(
        client,
        libre["id"],
        "Con texto en el estado",
        {"responsable": escenario["yo"], "estado": "hecho"},
    )

    datos = mis_tareas(client)

    assert datos["count"] == 1
    assert datos["tasks"][0]["status"] is None


def test_el_estado_de_un_multiseleccion_cierra_si_uno_de_sus_valores_esta_hecho(client, escenario):
    """«hecho, archivado» sigue siendo una tarea hecha."""
    etiquetas = crear_base(
        client,
        "Etiquetas",
        [
            {"key": "responsable", "name": "Responsable", "type": "person"},
            {
                "key": "estado",
                "name": "Estado",
                "type": "multiselect",
                "options": ["En curso", "Hecho", "Archivado"],
            },
        ],
        closed={"property": "estado", "values": ["Hecho"]},
    )
    crear_fila(
        client,
        etiquetas["id"],
        "Una hecha",
        {"responsable": escenario["yo"], "estado": ["En curso", "Hecho"]},
    )
    crear_fila(
        client,
        etiquetas["id"],
        "Una en curso",
        {"responsable": escenario["yo"], "estado": ["En curso"]},
    )

    datos = mis_tareas(client)

    assert [t["title"] for t in datos["tasks"]] == ["Una en curso"]
    assert datos["tasks"][0]["status"]["value"] == "En curso"


# --------------------------------------------------------------------------
# 4. la fecha
# --------------------------------------------------------------------------

def test_la_fecha_y_su_propiedad_salen_tal_cual(client, escenario):
    crear_fila(
        client,
        escenario["proyectos"]["id"],
        "Entrega",
        {"responsable": escenario["yo"], "vence": "2026-11-14"},
    )

    tarea = mis_tareas(client)["tasks"][0]

    assert tarea["date"] == {"key": "vence", "name": "Vence", "value": "2026-11-14"}


def test_se_usa_la_primera_propiedad_de_fecha(client, escenario):
    """Dos fechas: la vista no adivina, se queda con la primera del schema."""
    dos = crear_base(
        client,
        "Dos fechas",
        [
            {"key": "responsable", "name": "Responsable", "type": "person"},
            {"key": "empieza", "name": "Empieza", "type": "date"},
            {"key": "acaba", "name": "Acaba", "type": "date"},
        ],
    )
    crear_fila(
        client,
        dos["id"],
        "Sprint",
        {"responsable": escenario["yo"], "empieza": "2026-11-10", "acaba": "2026-11-30"},
    )

    tarea = mis_tareas(client)["tasks"][0]

    assert tarea["date"] == {"key": "empieza", "name": "Empieza", "value": "2026-11-10"}


def test_una_fecha_ilegible_no_se_inventa(client, escenario):
    crear_fila(
        client,
        escenario["proyectos"]["id"],
        "Sin fecha buena",
        {"responsable": escenario["yo"], "vence": "cuando pueda"},
    )

    tarea = mis_tareas(client)["tasks"][0]

    assert tarea["date"] is None


# --------------------------------------------------------------------------
# 5. abiertas: la lista de «hechas» y las bases sin estado
# --------------------------------------------------------------------------

@pytest.mark.parametrize("estado", ["Hecho", "Cerrado"])
def test_una_fila_con_estado_hecho_no_sale(client, escenario, estado):
    crear_fila(
        client,
        escenario["proyectos"]["id"],
        "Ya está",
        {"responsable": escenario["yo"], "estado": estado},
    )

    datos = mis_tareas(client)

    assert datos["count"] == 0
    assert datos["closed_values"], "la interfaz necesita la lista para explicarla"


@pytest.mark.parametrize(
    "valor", ["hecha", "HECHO", "  cerrada  ", "Cerrados", "done", "DONE", "ganada"]
)
def test_el_valor_cerrado_casa_sin_depender_de_mayusculas_ni_espacios(valor):
    """La comparación es contra la regla de la base, no del texto exacto.

    Se prueba en la función y no por la API porque las opciones de un `select`
    son las que son: por HTTP solo se pueden guardar las que la base declara.
    """
    from app.closed import is_closed

    regla = {
        "property": "estado",
        "values": ["hecha", "hecho", "cerrada", "cerrados", "done", "ganada"],
    }
    assert is_closed(regla, {"estado": valor}) is True


@pytest.mark.parametrize("valor", ["Abierto", "En curso", "bloqueada", "", None, 3])
def test_lo_que_la_base_no_declara_cerrado_no_cierra(valor):
    """Sin regla, nada cierra; y un valor fuera de la regla tampoco."""
    from app.closed import is_closed

    assert is_closed(None, {"estado": valor}) is False
    assert is_closed({"property": "estado", "values": ["hecha"]}, {"estado": valor}) is False


def test_un_multiselect_cierra_si_uno_de_sus_valores_esta_en_la_regla():
    """«hecho, archivado» sigue siendo una tarea hecha."""
    from app.closed import is_closed

    regla = {"property": "estado", "values": ["Hecho"]}
    assert is_closed(regla, {"estado": ["En curso", "Hecho"]}) is True
    assert is_closed(regla, {"estado": ["En curso"]}) is False


def test_una_fila_en_curso_si_sale(client, escenario):
    crear_fila(
        client,
        escenario["proyectos"]["id"],
        "En ello",
        {"responsable": escenario["yo"], "estado": "En curso"},
    )
    crear_fila(
        client,
        escenario["proyectos"]["id"],
        "Terminada",
        {"responsable": escenario["yo"], "estado": "Hecho"},
    )

    assert titulos(mis_tareas(client)) == ["En ello"]


def test_una_base_sin_estado_entra_toda(client, escenario):
    """Sin propiedad de estado no hay nada que diga que está hecha."""
    for title in ("Una", "Otra", "Y otra"):
        crear_fila(client, escenario["notas"]["id"], title, {"responsable": escenario["yo"]})

    assert sorted(titulos(mis_tareas(client))) == ["Otra", "Una", "Y otra"]


def test_la_lista_de_cerradas_sale_de_las_bases(client, escenario):
    """Ya no hay lista cableada: sin reglas en las bases, no hay valores.

    La interfaz necesita saber qué se está escondiendo para explicarlo; esa
    lista sale de las bases que declaran estado, no de una constante del
    servidor (que podía contradecir al `.md`).
    """
    sin_reglas = [b for b in ("notas", "libros") if not escenario[b].get("closed")]
    assert len(sin_reglas) == 2

    datos = mis_tareas(client)

    assert datos["closed_values"] == ["Cerrado", "Hecho"]
    assert datos["tasks"] == [] and datos["count"] == 0


# ------------------------------------------- la regla `done` de la base manda

def test_el_marcador_done_manda_sobre_la_lista_de_respaldo(client, escenario):
    """Si la base dice qué es «cerrada», se usa eso y no la lista de siempre.

    «En curso» está marcado como cerrada y «Hecho» no: la vista tiene que
    hacerle caso a la base en los dos sentidos, o el marcador no serviría
    para nada.
    """
    marcada = crear_base(
        client,
        "Con marcador",
        [
            {"key": "responsable", "name": "Responsable", "type": "person"},
            {
                "key": "estado",
                "name": "Estado",
                "type": "select",
                "options": ["Abierto", "En curso", "Hecho"],
                "done": ["En curso"],
            },
        ],
    )
    crear_fila(
        client, marcada["id"], "Cuenta como cerrada", {"responsable": escenario["yo"], "estado": "En curso"}
    )
    crear_fila(
        client, marcada["id"], "Cuenta como abierta", {"responsable": escenario["yo"], "estado": "Hecho"}
    )

    assert [t["title"] for t in mis_tareas(client)["tasks"]] == ["Cuenta como abierta"]


def test_un_marcador_de_casilla_cierra_la_fila_aunque_su_estado_diga_otra_cosa(client, escenario):
    """El marcador puede estar en una casilla, no en la propiedad de estado."""
    casilla = crear_base(
        client,
        "Con casilla",
        [
            {"key": "responsable", "name": "Responsable", "type": "person"},
            {"key": "estado", "name": "Estado", "type": "select", "options": ["Abierto", "Hecho"]},
            {"key": "terminada", "name": "Terminada", "type": "checkbox", "done": True},
        ],
    )
    crear_fila(
        client,
        casilla["id"],
        "Terminada de verdad",
        {"responsable": escenario["yo"], "estado": "Abierto", "terminada": True},
    )
    crear_fila(
        client,
        casilla["id"],
        "Sin terminar",
        {"responsable": escenario["yo"], "estado": "Hecho", "terminada": False},
    )

    assert [t["title"] for t in mis_tareas(client)["tasks"]] == ["Sin terminar"]


# --------------------------------------------------------------------------
# 6. orden
# --------------------------------------------------------------------------

def test_orden_por_fecha_ascendente_y_las_sin_fecha_al_final(client, escenario):
    filas = [
        ("Sin fecha", None),
        ("La de después", "2026-12-01"),
        ("La de hoy", "2026-11-16"),
        ("La de antes", "2026-11-01"),
    ]
    for title, vence in filas:
        props = {"responsable": escenario["yo"]}
        if vence:
            props["vence"] = vence
        crear_fila(client, escenario["proyectos"]["id"], title, props)

    assert titulos(mis_tareas(client)) == [
        "La de antes",
        "La de hoy",
        "La de después",
        "Sin fecha",
    ]


# --------------------------------------------------------------------------
# 7. la puerta
# --------------------------------------------------------------------------

def test_my_tasks_pide_sesion(client):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonimo:
        assert anonimo.get("/my-tasks").status_code == 401
