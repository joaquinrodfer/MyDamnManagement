"""«Que no se pase una fecha sin enterarte»: lo vencido y lo de hoy, contados.

Lo que se fija aquí:

1. **Quién decide qué está vencido**: el servidor. La fecha de cada fila se
   compara contra `app.due.server_today()`, no contra el reloj del navegador, y
   el resultado viaja en `GET /my-tasks` (`today`, `counts`, `due`, `days`) y en
   cada fila de una base (`overdue_keys`). Los tests fijan el día del servidor
   con `monkeypatch`, así que no dependen del día en que se ejecuten.
2. **Los seis casos de fecha** de la tarjeta: vencida, hoy, mañana, sin fecha,
   cerrada con fecha pasada (no cuenta) y fecha imposible (no cuenta como
   vencida).
3. **La marca en la tabla**: una fecha pasada se marca solo si la base declara
   regla de cierre y la fila no está cerrada.
4. **El contador y la vista dicen lo mismo**: `counts` se cuenta sobre las
   tareas que se devuelven.
"""

from __future__ import annotations

from datetime import date

import pytest

HOY = date(2026, 11, 16)


@pytest.fixture()
def hoy(monkeypatch):
    """Fija el día del servidor: los tests no dependen de la fecha de ejecución.

    Se parchea `app.due.server_today`, que es lo que llaman tanto `mytasks`
    como el serializador de filas (por módulo, no por nombre), así que la
    comparación que se prueba es la que corre en la app.
    """
    from app import due

    monkeypatch.setattr(due, "server_today", lambda: HOY)
    return HOY


PROYECTOS = [
    {"key": "responsable", "name": "Responsable", "type": "person"},
    {
        "key": "estado",
        "name": "Estado",
        "type": "select",
        "options": ["Abierto", "En curso", "Hecho"],
    },
    {"key": "vence", "name": "Vence", "type": "date"},
]

# Una base con responsable y fecha pero **sin** regla de cierre: no tiene
# estado, así que nada la puede cerrar (y en la tabla no se marca nada).
SIN_REGLA = [
    {"key": "responsable", "name": "Responsable", "type": "person"},
    {"key": "vence", "name": "Vence", "type": "date"},
]

CERRADA_EN_PROYECTOS = {"property": "estado", "values": ["Hecho"]}


def yo(client) -> str:
    return client.get("/auth/me").json()["id"]


def crear_base(client, title: str, schema_def: list[dict], closed: dict | None = None) -> dict:
    cuerpo = {"title": title, "schema_def": schema_def}
    if closed is not None:
        cuerpo["closed"] = closed
    respuesta = client.post("/databases", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id: str, title: str, properties: dict) -> dict:
    respuesta = client.post(
        f"/databases/{database_id}/rows", json={"title": title, "properties": properties}
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def mis_tareas(client) -> dict:
    respuesta = client.get("/my-tasks")
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def tipar(client, base: dict, tipo: str = "projects") -> dict:
    """La base pasa a ser de Proyectos: solo ahí una fecha pasada es vencida
    (decisión del dueño, 2026-11-20)."""
    respuesta = client.put(
        f"/databases/{base['id']}/kind",
        json={"type": tipo, "roles": {"assignee": "responsable", "status": "estado", "end": "vence"}},
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


@pytest.fixture()
def proyectos(client, hoy):
    base = crear_base(client, "Proyectos", PROYECTOS, closed=CERRADA_EN_PROYECTOS)
    tipar(client, base)
    return base


def con_fecha(client, base: dict, title: str, **props) -> dict:
    return crear_fila(client, base["id"], title, {"responsable": yo(client), **props})


# --------------------------------------------------------------------------
# 1. los seis casos de fecha
# --------------------------------------------------------------------------

def test_vencida_cuenta_como_vencida(client, proyectos):
    con_fecha(client, proyectos, "Ya pasó", estado="En curso", vence="2026-11-15")

    datos = mis_tareas(client)

    assert datos["counts"] == {"overdue": 1, "today": 0}
    tarea = datos["tasks"][0]
    assert tarea["due"] == "overdue"
    assert tarea["days"] == -1


def test_lo_que_vence_hoy_cuenta_como_hoy_y_no_como_vencido(client, proyectos):
    con_fecha(client, proyectos, "Para hoy", estado="En curso", vence="2026-11-16")

    datos = mis_tareas(client)

    assert datos["counts"] == {"overdue": 0, "today": 1}
    assert datos["tasks"][0]["due"] == "today"
    assert datos["tasks"][0]["days"] == 0


def test_lo_de_manana_no_cuenta_ni_como_vencido_ni_como_hoy(client, proyectos):
    con_fecha(client, proyectos, "Mañana", estado="En curso", vence="2026-11-17")

    datos = mis_tareas(client)

    assert datos["counts"] == {"overdue": 0, "today": 0}
    assert datos["tasks"][0]["due"] == "week"
    assert datos["tasks"][0]["days"] == 1


def test_una_tarea_sin_fecha_no_cuenta(client, proyectos):
    con_fecha(client, proyectos, "Algún día", estado="En curso")

    datos = mis_tareas(client)

    assert datos["counts"] == {"overdue": 0, "today": 0}
    assert datos["tasks"][0]["due"] is None
    assert datos["tasks"][0]["days"] is None


def test_una_fila_cerrada_con_fecha_pasada_no_cuenta_como_vencida(client, proyectos):
    """Una tarea hecha tarde no está vencida (ni sale: está cerrada)."""
    con_fecha(client, proyectos, "Hecha tarde", estado="Hecho", vence="2026-11-01")

    datos = mis_tareas(client)

    assert datos["counts"] == {"overdue": 0, "today": 0}
    assert datos["tasks"] == []
    assert datos["count"] == 0


def test_una_fecha_imposible_no_cuenta_como_vencida(client, proyectos):
    con_fecha(client, proyectos, "Cuando pueda", estado="En curso", vence="2026-02-31")

    datos = mis_tareas(client)

    assert datos["counts"] == {"overdue": 0, "today": 0}
    assert datos["tasks"][0]["due"] is None
    assert datos["tasks"][0]["days"] is None
    # Y la fecha no se inventa: la fila va sin fecha, como antes.
    assert datos["tasks"][0]["date"] is None


# --------------------------------------------------------------------------
# 2. el día es el del servidor
# --------------------------------------------------------------------------

def test_la_respuesta_dice_con_que_dia_ha_comparado(client, proyectos):
    """El cliente no necesita saber qué día es: se lo dan hecho."""
    con_fecha(client, proyectos, "Cualquiera", vence="2026-11-15")

    datos = mis_tareas(client)

    assert datos["today"] == "2026-11-16"


def test_el_dia_del_servidor_manda_y_no_el_del_navegador(client, proyectos, monkeypatch):
    """La misma fila cambia de bloque si cambia el día del servidor.

    Es exactamente lo que no puede pasar con dos husos: quien mira no aporta el
    «hoy», lo aporta el servidor. Se mueve el día del servidor y la fila pasa de
    «vencida» a «hoy» sin tocar la fila.
    """
    from app import due

    con_fecha(client, proyectos, "Frontera", estado="En curso", vence="2026-11-16")
    assert mis_tareas(client)["counts"] == {"overdue": 0, "today": 1}

    monkeypatch.setattr(due, "server_today", lambda: date(2026, 11, 17))

    datos = mis_tareas(client)
    assert datos["today"] == "2026-11-17"
    assert datos["counts"] == {"overdue": 1, "today": 0}
    assert datos["tasks"][0]["due"] == "overdue"


def test_los_recuentos_se_cuentan_sobre_lo_que_se_devuelve(client, proyectos):
    con_fecha(client, proyectos, "Vieja", estado="En curso", vence="2026-11-10")
    con_fecha(client, proyectos, "Otra vieja", estado="En curso", vence="2026-11-12")
    con_fecha(client, proyectos, "Hoy", estado="En curso", vence="2026-11-16")
    con_fecha(client, proyectos, "Mañana", estado="En curso", vence="2026-11-17")

    datos = mis_tareas(client)

    assert datos["count"] == 4
    assert datos["counts"] == {"overdue": 2, "today": 1}
    assert sum(1 for t in datos["tasks"] if t["due"] == "overdue") == 2
    assert sum(1 for t in datos["tasks"] if t["due"] == "today") == 1


def test_una_base_libre_con_fecha_pasada_no_cuenta_como_vencida(client, hoy):
    """Solo una base de Proyectos o de Tareas tiene plazos (decisión del dueño,
    2026-11-20): en una base libre la fecha pasada es `past`, sin rojo, sin
    contar en la barra lateral y sin aviso."""
    base = crear_base(client, "Sin regla", SIN_REGLA)
    con_fecha(client, base, "Vieja de una base sin estado", vence="2026-11-01")

    datos = mis_tareas(client)

    assert datos["closed_values"] == []
    assert datos["counts"] == {"overdue": 0, "today": 0}
    assert [t["due"] for t in datos["tasks"]] == ["past"]


def test_una_base_libre_no_marca_fechas_pasadas_en_la_tabla(client, hoy):
    base = crear_base(client, "Libre", PROYECTOS, closed=CERRADA_EN_PROYECTOS)
    con_fecha(client, base, "Vieja", estado="Abierto", vence="2026-11-01")

    assert filas(client, base["id"])[0]["overdue_keys"] == []


# --------------------------------------------------------------------------
# 3. la marca en la tabla
# --------------------------------------------------------------------------

def filas(client, database_id: str) -> list[dict]:
    respuesta = client.get(f"/databases/{database_id}/rows")
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def test_la_tabla_marca_la_fecha_vencida_de_una_fila_abierta(client, proyectos):
    con_fecha(client, proyectos, "Entrega", estado="En curso", vence="2026-11-01")

    fila = filas(client, proyectos["id"])[0]

    assert fila["overdue_keys"] == ["vence"]
    assert fila["closed"] is False


def test_la_tabla_no_marca_la_fecha_vencida_de_una_fila_cerrada(client, proyectos):
    """Una tarea hecha tarde no está vencida, aunque su fecha haya pasado."""
    con_fecha(client, proyectos, "Hecha tarde", estado="Hecho", vence="2026-11-01")

    fila = filas(client, proyectos["id"])[0]

    assert fila["closed"] is True
    assert fila["overdue_keys"] == []


def test_la_tabla_no_marca_nada_si_la_base_no_declara_regla_de_cierre(client, hoy):
    base = crear_base(client, "Sin regla", SIN_REGLA)
    con_fecha(client, base, "Vieja", vence="2026-11-01")

    fila = filas(client, base["id"])[0]

    assert fila["overdue_keys"] == []


def test_la_tabla_no_marca_la_fecha_de_hoy_ni_la_de_manana(client, proyectos):
    con_fecha(client, proyectos, "Hoy", estado="En curso", vence="2026-11-16")
    con_fecha(client, proyectos, "Mañana", estado="En curso", vence="2026-11-17")

    for fila in filas(client, proyectos["id"]):
        assert fila["overdue_keys"] == []


def test_la_tabla_no_marca_una_fecha_imposible(client, proyectos):
    con_fecha(client, proyectos, "Imposible", estado="En curso", vence="2026-13-01")

    fila = filas(client, proyectos["id"])[0]

    assert fila["overdue_keys"] == []


def test_la_tabla_marca_todas_las_fechas_pasadas_y_no_solo_la_primera(client, hoy):
    """La tabla pinta celdas: si dos fechas pasaron, se marcan las dos."""
    base = crear_base(
        client,
        "Dos fechas",
        [
            {"key": "responsable", "name": "Responsable", "type": "person"},
            {"key": "empieza", "name": "Empieza", "type": "date"},
            {"key": "acaba", "name": "Acaba", "type": "date"},
            {"key": "estado", "name": "Estado", "type": "select", "options": ["Abierto", "Hecho"]},
        ],
        closed={"property": "estado", "values": ["Hecho"]},
    )
    client.put(
        f"/databases/{base['id']}/kind",
        json={"type": "projects", "roles": {"assignee": "responsable", "status": "estado", "start": "empieza", "end": "acaba"}},
    ).raise_for_status()
    crear_fila(
        client,
        base["id"],
        "Sprint",
        {
            "responsable": yo(client),
            "estado": "Abierto",
            "empieza": "2026-11-02",
            "acaba": "2026-11-30",
        },
    )

    fila = filas(client, base["id"])[0]

    assert fila["overdue_keys"] == ["empieza"]


# --------------------------------------------------------------------------
# 4. la aritmética de calendario, en la función (sin HTTP)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, esperado",
    [
        ("2026-11-15", "overdue"),
        ("2026-11-16", "today"),
        ("2026-11-17", "week"),
        ("2026-11-22", "week"),   # domingo de esta semana
        ("2026-11-23", "later"),  # ya es la semana que viene
        (None, None),
        ("", None),
        ("cuando pueda", None),
        ("2026-02-30", None),
        (3, None),
        (True, None),
    ],
)
def test_el_bloque_de_una_fecha(raw, esperado):
    from app import due

    assert due.due_state(raw, HOY) == esperado


def test_el_fin_de_semana_es_el_domingo_con_la_semana_empezando_el_lunes():
    from app import due

    # 16 de noviembre de 2026 es lunes.
    assert HOY.isoweekday() == 1
    assert due.week_end(HOY) == date(2026, 11, 22)
    # El propio domingo sigue siendo «esta semana».
    assert due.week_end(date(2026, 11, 22)) == date(2026, 11, 22)


def test_una_fila_cerrada_no_marca_ni_sus_otras_fechas():
    """La regla de cierre se aplica a la fila entera, no a una columna."""
    from app import due

    schema = [
        {"key": "empieza", "name": "Empieza", "type": "date"},
        {"key": "acaba", "name": "Acaba", "type": "date"},
    ]
    rule = {"property": "estado", "values": ["Hecho"]}
    props = {"empieza": "2026-01-01", "acaba": "2026-02-01", "estado": "Hecho"}

    tipo = {"type": "tasks"}
    assert due.overdue_date_keys(schema, props, rule, HOY, tipo) == []
    assert due.overdue_date_keys(schema, {**props, "estado": "Abierto"}, rule, HOY, tipo) == [
        "empieza",
        "acaba",
    ]
    # Sin tipo (base libre) no se marca nada.
    assert due.overdue_date_keys(schema, {**props, "estado": "Abierto"}, rule, HOY) == []


def test_sin_regla_no_se_marca_nada():
    from app import due

    schema = [{"key": "vence", "name": "Vence", "type": "date"}]
    props = {"vence": "2026-01-01"}

    assert due.overdue_date_keys(schema, props, None, HOY) == []
