"""Filtros y orden POR VISTA: lo que la vista enseña, guardado en su `.md`.

Una vista es una forma de mirar la base: `filters` (varias condiciones, unidas
con Y) y `sort` (varias claves en prioridad) viven en la entrada de esa vista
en el frontmatter de `Bases/<slug>.md`, no en la base. Este módulo fija tres
cosas:

1. cada operador filtra lo que dice su nombre, con el tipo del que viene;
2. el orden por varias claves respeta la prioridad y deja las filas sin valor
   al final en las dos direcciones;
3. lo escrito se relee igual tras reindexar, y dos vistas de la misma base
   pueden filtrar distinto (la base sin vista no filtra).
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.views import apply_view


def fila(**properties):
    """Una fila mínima: `apply_view` solo mira sus propiedades."""
    return SimpleNamespace(properties=properties)


def valores(filas):
    return [f.properties.get("nombre") for f in filas]


# --------------------------------------------------------------------------
# 1. los operadores, por tipo de propiedad
# --------------------------------------------------------------------------


def test_texto_es_no_es_contiene_y_vacio():
    filas = [
        fila(nombre="Alfa", nota="cliente grande"),
        fila(nombre="Bravo", nota=""),
        fila(nombre="Charlie"),
    ]
    assert valores(apply_view(filas, {"filters": [{"key": "nota", "op": "eq", "value": "cliente grande"}]})) == ["Alfa"]
    # «es» no distingue mayúsculas: quien filtra escribe, no copia el `.md`.
    assert valores(apply_view(filas, {"filters": [{"key": "nota", "op": "eq", "value": "CLIENTE GRANDE"}]})) == ["Alfa"]
    assert valores(apply_view(filas, {"filters": [{"key": "nota", "op": "neq", "value": "cliente grande"}]})) == ["Bravo", "Charlie"]
    assert valores(apply_view(filas, {"filters": [{"key": "nota", "op": "contains", "value": "cliente"}]})) == ["Alfa"]
    assert valores(apply_view(filas, {"filters": [{"key": "nota", "op": "is_empty"}]})) == ["Bravo", "Charlie"]
    assert valores(apply_view(filas, {"filters": [{"key": "nota", "op": "is_not_empty"}]})) == ["Alfa"]
    # Una propiedad que la fila no trae es «vacía», no un error.
    assert valores(apply_view(filas, {"filters": [{"key": "no_existe", "op": "is_empty"}]})) == [
        "Alfa",
        "Bravo",
        "Charlie",
    ]


def test_numero_los_seis_operadores():
    filas = [fila(nombre=n, valor=v) for n, v in (("Alfa", 100), ("Bravo", 200), ("Charlie", 300))]

    def elegidos(op, value):
        return valores(apply_view(filas, {"filters": [{"key": "valor", "op": op, "value": value}]}))

    assert elegidos("eq", "200") == ["Bravo"]
    assert elegidos("neq", "200") == ["Alfa", "Charlie"]
    assert elegidos("gt", "200") == ["Charlie"]
    assert elegidos("lt", "200") == ["Alfa"]
    assert elegidos("gte", "200") == ["Bravo", "Charlie"]
    assert elegidos("lte", "200") == ["Alfa", "Bravo"]


def test_numero_compara_como_numero_aunque_el_md_lo_de_vuelta_como_texto():
    # El parser no infiere tipos: `valor: 100` puede llegar como "100". Si se
    # comparase como texto, "100" < "9" y "10" > "9" darían un orden absurdo.
    filas = [fila(nombre="nueve", valor="9"), fila(nombre="diez", valor="10")]
    assert valores(apply_view(filas, {"filters": [{"key": "valor", "op": "gt", "value": 9}]})) == ["diez"]
    assert valores(
        apply_view(filas, {"sort": [{"key": "valor", "dir": "asc"}]})
    ) == ["nueve", "diez"]


def test_fecha_es_antes_de_despues_de_y_vacia():
    filas = [
        fila(nombre="enero", limite="2026-01-10"),
        fila(nombre="marzo", limite="2026-03-10"),
        fila(nombre="sin fecha"),
    ]

    def elegidos(filtro):
        return valores(apply_view(filas, {"filters": [filtro]}))

    assert elegidos({"key": "limite", "op": "eq", "value": "2026-01-10"}) == ["enero"]
    assert elegidos({"key": "limite", "op": "before", "value": "2026-02-01"}) == ["enero"]
    assert elegidos({"key": "limite", "op": "after", "value": "2026-02-01"}) == ["marzo"]
    assert elegidos({"key": "limite", "op": "is_empty"}) == ["sin fecha"]
    # Una fila sin fecha no cumple «antes de» ni «después de»: no hay nada que
    # comparar (y no se cuela en las dos).
    assert "sin fecha" not in elegidos({"key": "limite", "op": "before", "value": "2026-12-31"})


def test_seleccion_es_y_no_es():
    filas = [fila(nombre="a", fase="lead"), fila(nombre="b", fase="ganado"), fila(nombre="c")]
    assert valores(apply_view(filas, {"filters": [{"key": "fase", "op": "eq", "value": "lead"}]})) == ["a"]
    assert valores(apply_view(filas, {"filters": [{"key": "fase", "op": "neq", "value": "lead"}]})) == ["b", "c"]


def test_casilla_marcado_y_desmarcado():
    # La casilla llega como booleano del índice y puede llegar como texto del
    # `.md`: las dos formas se entienden.
    filas = [
        fila(nombre="si", activo=True),
        fila(nombre="no", activo=False),
        fila(nombre="texto-si", activo="true"),
        fila(nombre="sin valor"),
    ]
    assert valores(apply_view(filas, {"filters": [{"key": "activo", "op": "checked"}]})) == ["si", "texto-si"]
    assert valores(apply_view(filas, {"filters": [{"key": "activo", "op": "unchecked"}]})) == [
        "no",
        "sin valor",
    ]


def test_relacion_incluye_el_id_simple_y_en_lista():
    filas = [
        fila(nombre="una", contacto="3a7f0000-0000-0000-0000-000000000001"),
        fila(nombre="varias", contacto=["3a7f0000-0000-0000-0000-000000000002"]),
        fila(nombre="otra", contacto=["3a7f0000-0000-0000-0000-000000000003"]),
        fila(nombre="ninguna"),
    ]
    objetivo = "3a7f0000-0000-0000-0000-000000000002"
    assert valores(apply_view(filas, {"filters": [{"key": "contacto", "op": "includes", "value": objetivo}]})) == [
        "varias"
    ]
    # El `id` de una relación simple también responde a «incluye».
    simple = "3a7f0000-0000-0000-0000-000000000001"
    assert valores(apply_view(filas, {"filters": [{"key": "contacto", "op": "includes", "value": simple}]})) == ["una"]


def test_varias_condiciones_se_combinan_con_y():
    filas = [
        fila(nombre="lead-100", fase="lead", valor=100),
        fila(nombre="lead-300", fase="lead", valor=300),
        fila(nombre="ganado-300", fase="ganado", valor=300),
    ]
    config = {
        "filters": [
            {"key": "fase", "op": "eq", "value": "lead"},
            {"key": "valor", "op": "gte", "value": 300},
        ]
    }
    assert valores(apply_view(filas, config)) == ["lead-300"]


def test_un_filtro_que_no_deja_pasar_nada_devuelve_la_lista_vacia():
    filas = [fila(nombre="a", fase="lead")]
    assert apply_view(filas, {"filters": [{"key": "fase", "op": "eq", "value": "perdido"}]}) == []


# --------------------------------------------------------------------------
# 2. el orden por varias claves
# --------------------------------------------------------------------------


def test_orden_por_dos_claves_respeta_la_prioridad():
    filas = [
        fila(nombre="lead-100", fase="lead", valor=100),
        fila(nombre="ganado-900", fase="ganado", valor=900),
        fila(nombre="lead-900", fase="lead", valor=900),
        fila(nombre="ganado-100", fase="ganado", valor=100),
    ]
    config = {"sort": [{"key": "fase", "dir": "asc"}, {"key": "valor", "dir": "desc"}]}
    # Primero fase (ganado < lead), y dentro de cada fase el valor de mayor a
    # menor: la segunda clave desempata, no manda.
    assert valores(apply_view(filas, config)) == ["ganado-900", "ganado-100", "lead-900", "lead-100"]


def test_la_segunda_clave_solo_desempata():
    filas = [
        fila(nombre="lead-uno", fase="lead", valor=1),
        fila(nombre="ganado-uno", fase="ganado", valor=1),
        fila(nombre="lead-dos", fase="lead", valor=2),
    ]
    config = {"sort": [{"key": "valor", "dir": "asc"}, {"key": "fase", "dir": "desc"}]}
    # El valor manda (1 antes que 2); entre los dos que empatan a 1, la fase
    # descendente pone «lead» antes que «ganado».
    assert valores(apply_view(filas, config)) == ["lead-uno", "ganado-uno", "lead-dos"]


def test_las_filas_sin_valor_van_al_final_en_las_dos_direcciones():
    filas = [
        fila(nombre="sin valor"),
        fila(nombre="dos", valor=2),
        fila(nombre="uno", valor=1),
    ]
    asc = apply_view(filas, {"sort": [{"key": "valor", "dir": "asc"}]})
    desc = apply_view(filas, {"sort": [{"key": "valor", "dir": "desc"}]})
    assert valores(asc) == ["uno", "dos", "sin valor"]
    assert valores(desc) == ["dos", "uno", "sin valor"]


def test_la_forma_antigua_de_orden_de_una_sola_clave_se_sigue_aplicando():
    # `sort: {key, dir}` es lo que escribieron las versiones anteriores. No se
    # pierde: una vista antigua sigue ordenando.
    filas = [fila(nombre="b", valor=1), fila(nombre="a", valor=2)]
    assert valores(apply_view(filas, {"sort": {"key": "valor", "dir": "desc"}})) == ["a", "b"]


# --------------------------------------------------------------------------
# 3. ida y vuelta por la API: el `.md` es la verdad
# --------------------------------------------------------------------------

SCHEMA = [
    {"key": "empresa", "name": "Empresa", "type": "text"},
    {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "propuesta", "ganado"]},
    {"key": "valor", "name": "Valor", "type": "number"},
    {"key": "limite", "name": "Límite", "type": "date"},
    {"key": "activo", "name": "Activo", "type": "checkbox"},
]

# Diez filas: dos condiciones dejan pasar tres (fase lead y valor ≥ 300).
FILAS = [
    ("Alfa", "lead", 100, "2026-01-10", True),
    ("Bravo", "lead", 300, "2026-02-10", False),
    ("Charlie", "lead", 500, None, True),
    ("Delta", "propuesta", 900, "2026-03-10", False),
    ("Echo", "ganado", 1200, "2026-04-10", True),
    ("Foxtrot", "lead", 200, "2026-05-10", False),
    ("Golf", "propuesta", 300, None, True),
    ("Hotel", "ganado", 400, "2026-06-10", False),
    ("India", "lead", 800, "2026-07-10", True),
    ("Juliet", "propuesta", 50, "2026-08-10", False),
]


def crear_base(client):
    respuesta = client.post("/databases", json={"title": "Contactos", "schema_def": SCHEMA})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_filas(client, database_id):
    creadas = []
    for empresa, fase, valor, limite, activo in FILAS:
        propiedades = {"empresa": empresa, "fase": fase, "valor": valor, "activo": activo}
        if limite is not None:
            propiedades["limite"] = limite
        respuesta = client.post(
            f"/databases/{database_id}/rows",
            json={"title": empresa, "properties": propiedades},
        )
        assert respuesta.status_code == 200, respuesta.text
        creadas.append(respuesta.json())
    return creadas


def crear_vista(client, database_id, nombre, config=None, tipo="table"):
    respuesta = client.post(
        f"/databases/{database_id}/views",
        json={"name": nombre, "type": tipo, "config": config or {}},
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def md_de_la_base(vault_path: Path, titulo="Contactos") -> tuple[str, list[dict]]:
    from app.vault import fm

    fichero = vault_path / "Bases" / f"{titulo}.md"
    texto = fichero.read_text(encoding="utf-8")
    raw, _cuerpo = fm.split_document(texto)
    assert raw is not None, "la base no tiene frontmatter"
    return texto, fm.parse(raw).data.get("views") or []


FILTRO = [
    {"key": "fase", "op": "eq", "value": "lead"},
    {"key": "valor", "op": "gte", "value": 300},
]
ORDEN = [
    {"key": "fase", "dir": "asc"},
    {"key": "valor", "dir": "desc"},
]


def test_filtros_y_orden_por_vista_sobreviven_al_md_y_al_reindexado(client, vault_path):
    base = crear_base(client)
    crear_filas(client, base["id"])
    vista = crear_vista(client, base["id"], "Leads grandes", {"filters": FILTRO, "sort": ORDEN})

    texto, vistas = md_de_la_base(vault_path)
    guardada = next(v for v in vistas if v["id"] == vista["id"])
    # Las dos claves viven en la entrada de ESA vista, no en la base.
    assert guardada["filters"] == [
        {"key": "fase", "op": "eq", "value": "lead"},
        {"key": "valor", "op": "gte", "value": "300"},
    ]
    assert guardada["sort"] == ORDEN
    # Y son YAML legible a mano, no el `repr` de Python.
    assert "- key: fase" in texto
    assert "op: gte" in texto
    assert "dir: desc" in texto

    # La API devuelve exactamente lo mismo que el fichero.
    leidas = client.get(f"/databases/{base['id']}/views").json()
    assert next(v for v in leidas if v["id"] == vista["id"])["config"]["sort"] == ORDEN

    # La vista enseña las tres filas que cumplen las dos condiciones, en el
    # orden de las dos claves.
    filas = client.get(f"/databases/{base['id']}/rows?view={vista['id']}").json()
    assert [r["title"] for r in filas] == ["India", "Charlie", "Bravo"]

    # «N de M» de la interfaz sale de estas dos cuentas: la vista y la base.
    assert len(client.get(f"/databases/{base['id']}/rows").json()) == len(FILAS)

    # Reindexar no se lleva nada: el `.md` es la verdad.
    assert client.post("/reindex").status_code == 200
    texto, vistas = md_de_la_base(vault_path)
    guardada = next(v for v in vistas if v["id"] == vista["id"])
    assert guardada["filters"] == [
        {"key": "fase", "op": "eq", "value": "lead"},
        {"key": "valor", "op": "gte", "value": "300"},
    ]
    assert guardada["sort"] == ORDEN
    filas = client.get(f"/databases/{base['id']}/rows?view={vista['id']}").json()
    assert [r["title"] for r in filas] == ["India", "Charlie", "Bravo"]


def test_cada_vista_guarda_lo_suyo_y_la_base_sin_vista_no_filtra(client, vault_path):
    base = crear_base(client)
    crear_filas(client, base["id"])
    leads = crear_vista(
        client,
        base["id"],
        "Leads",
        {"filters": [{"key": "fase", "op": "eq", "value": "lead"}]},
    )
    ganadas = crear_vista(
        client,
        base["id"],
        "Ganadas",
        {"filters": [{"key": "fase", "op": "eq", "value": "ganado"}]},
    )

    assert [r["title"] for r in client.get(f"/databases/{base['id']}/rows?view={leads['id']}").json()] == [
        "Alfa",
        "Bravo",
        "Charlie",
        "Foxtrot",
        "India",
    ]
    assert [r["title"] for r in client.get(f"/databases/{base['id']}/rows?view={ganadas['id']}").json()] == [
        "Echo",
        "Hotel",
    ]
    # «Todas (sin vista)» es la base: ni filtro ni contagio de la vista activa.
    assert len(client.get(f"/databases/{base['id']}/rows").json()) == len(FILAS)

    # Cambiar el filtro de una no toca el de la otra, y queda en el `.md`.
    client.patch(f"/databases/{base['id']}/views/{leads['id']}", json={"config": {"filters": []}})
    assert len(client.get(f"/databases/{base['id']}/rows?view={leads['id']}").json()) == len(FILAS)
    assert [r["title"] for r in client.get(f"/databases/{base['id']}/rows?view={ganadas['id']}").json()] == [
        "Echo",
        "Hotel",
    ]
    _texto, vistas = md_de_la_base(vault_path)
    por_nombre = {v["name"]: v for v in vistas}
    assert "filters" not in por_nombre["Leads"]
    assert por_nombre["Ganadas"]["filters"] == [{"key": "fase", "op": "eq", "value": "ganado"}]


def test_los_operadores_sin_valor_no_escriben_value_en_el_md(client, vault_path):
    base = crear_base(client)
    crear_filas(client, base["id"])
    vista = crear_vista(
        client,
        base["id"],
        "Sin fecha",
        {"filters": [{"key": "limite", "op": "is_empty"}]},
    )

    texto, vistas = md_de_la_base(vault_path)
    guardada = next(v for v in vistas if v["id"] == vista["id"])
    assert guardada["filters"] == [{"key": "limite", "op": "is_empty"}]
    assert "value:" not in texto
    assert [r["title"] for r in client.get(f"/databases/{base['id']}/rows?view={vista['id']}").json()] == [
        "Charlie",
        "Golf",
    ]


def test_renombrar_una_propiedad_traduce_el_filtro_y_el_orden_de_la_vista(client, vault_path):
    base = crear_base(client)
    crear_filas(client, base["id"])
    vista = crear_vista(
        client,
        base["id"],
        "Leads grandes",
        {
            "filters": [{"key": "fase", "op": "eq", "value": "lead"}],
            "sort": [{"key": "fase", "dir": "asc"}, {"key": "valor", "dir": "desc"}],
        },
    )

    nuevo_schema = [
        {**p, "name": "Etapa"} if p["key"] == "fase" else p for p in SCHEMA
    ]
    respuesta = client.patch(f"/databases/{base['id']}", json={"schema_def": nuevo_schema})
    assert respuesta.status_code == 200, respuesta.text
    clave = next(p["key"] for p in respuesta.json()["schema_def"] if p["name"] == "Etapa")
    assert clave != "fase"

    _texto, vistas = md_de_la_base(vault_path)
    guardada = next(v for v in vistas if v["id"] == vista["id"])
    assert guardada["filters"] == [{"key": clave, "op": "eq", "value": "lead"}]
    assert guardada["sort"] == [{"key": clave, "dir": "asc"}, {"key": "valor", "dir": "desc"}]
    filas = client.get(f"/databases/{base['id']}/rows?view={vista['id']}").json()
    assert [r["title"] for r in filas] == [
        "India",
        "Charlie",
        "Bravo",
        "Foxtrot",
        "Alfa",
    ]


def test_un_filtro_imposible_devuelve_cero_filas(client):
    base = crear_base(client)
    crear_filas(client, base["id"])
    vista = crear_vista(
        client,
        base["id"],
        "Nada",
        {"filters": [{"key": "fase", "op": "eq", "value": "perdido"}]},
    )
    # Cero filas y 200: el «vacío honesto» lo dice la vista, no un error.
    assert client.get(f"/databases/{base['id']}/rows?view={vista['id']}").json() == []


@pytest.mark.parametrize(
    "op,value",
    [
        ("eq", "2026-01-10"),
        ("before", "2026-06-01"),
        ("is_not_empty", None),
    ],
)
def test_las_fechas_filtran_igual_desde_la_vista(client, op, value):
    base = crear_base(client)
    crear_filas(client, base["id"])
    condicion = {"key": "limite", "op": op}
    if value is not None:
        condicion["value"] = value
    vista = crear_vista(client, base["id"], "Fechas", {"filters": [condicion]})

    filas = client.get(f"/databases/{base['id']}/rows?view={vista['id']}").json()
    if op == "eq":
        assert [r["title"] for r in filas] == ["Alfa"]
    elif op == "before":
        assert [r["title"] for r in filas] == [
            "Alfa",
            "Bravo",
            "Delta",
            "Echo",
            "Foxtrot",
        ]
    else:
        assert len(filas) == 8  # las dos filas sin fecha no pasan
