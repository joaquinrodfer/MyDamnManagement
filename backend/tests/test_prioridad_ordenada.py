"""Prioridad ordenable: una selección ordena por su orden declarado.

Hasta ahora, una propiedad de selección con `alta / media / baja` se ordenaba
por alfabeto (alta, baja, media), y el tablero ponía las columnas en el orden en
que se crearon.

La decisión de esta tarjeta es **`sort_order` aparte de `options`**, y es
opt-in:

- `options` es la lista de valores válidos y su orden es el de creación. Usarla
  también como orden habría cambiado, en silencio, el orden de **todas** las
  bases que ya tenían una selección, incluidas las que nunca pidieron un orden
  semántico.
- El orden declarado (`sort_order`) solo lo declara quien lo quiere, vive en el
  `schema` de la base (el `.md` es la verdad, no la vista) y una base sin él se
  ordena como siempre, alfabética.
- `sort_order` puede nombrar solo algunos valores (o ninguno de `options`): los
  que no tienen sitio van al final. Así un valor que apareció después no se
  cuela entre los importantes.

Lo que se fija aquí:

1. `asc` devuelve alta, media, baja; `desc`, al revés; y en las dos direcciones
   el valor sin sitio declarado va al final (no es «el más importante»);
2. la prioridad desempata en un orden de varias claves, y ordenar por prioridad
   deja que la segunda clave ordene dentro de cada valor;
3. una multiselección se ordena por su valor más importante;
4. una base que no declara orden sigue alfabética, como hoy; el orden no
   distingue mayúsculas y el valor vacío sigue al final en las dos direcciones;
5. ida y vuelta por la API: el `sort_order` se escribe en el `.md` de la base,
   se relee igual tras reindexar y ordena lo que se ve en la pantalla;
6. un valor escrito a mano en el `.md` de una fila va al final en los dos
   sentidos;
7. `sort_order` en un tipo que no es una selección es un 400, guardar el resto
   del schema no lo borra (ni lo inventa) y `[]` lo quita.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.properties import order_ranks
from app.views import apply_view


# --------------------------------------------------------------------------
# 1-4. la regla, sin pasar por el servidor
# --------------------------------------------------------------------------

PRIORIDAD = {
    "key": "prioridad",
    "name": "Prioridad",
    "type": "select",
    "options": ["baja", "media", "alta"],
    "sort_order": ["alta", "media", "baja"],
}


def fila(nombre, prioridad):
    return SimpleNamespace(properties={"nombre": nombre, "prioridad": prioridad})


def nombres(filas):
    return [f.properties.get("nombre") for f in filas]


def ordenar(filas, claves, schema=(PRIORIDAD,)):
    return nombres(apply_view(filas, {"sort": claves}, orders=order_ranks(list(schema))))


def test_asc_por_el_orden_declarado_y_no_por_alfabeto():
    # Alfabéticamente sería alta, baja, media: el orden que no sirve.
    filas = [fila("media", "media"), fila("alta", "alta"), fila("baja", "baja")]
    assert ordenar(filas, [{"key": "prioridad", "dir": "asc"}]) == ["alta", "media", "baja"]


def test_desc_da_la_vuelta_al_orden_declarado():
    filas = [fila("media", "media"), fila("alta", "alta"), fila("baja", "baja")]
    assert ordenar(filas, [{"key": "prioridad", "dir": "desc"}]) == ["baja", "media", "alta"]


def test_el_valor_sin_sitio_va_al_final_en_los_dos_sentidos():
    # `urgente` apareció después (o se escribió a mano en un `.md`): no tiene
    # sitio declarado y no puede ser «lo más importante» en ninguno de los dos
    # sentidos. El vacío va detrás de todo, como en cualquier otra columna.
    filas = [
        fila("sin sitio", "urgente"),
        fila("media", "media"),
        fila("alta", "alta"),
        fila("vacia", None),
        fila("baja", "baja"),
    ]
    assert ordenar(filas, [{"key": "prioridad", "dir": "asc"}]) == [
        "alta", "media", "baja", "sin sitio", "vacia",
    ]
    assert ordenar(filas, [{"key": "prioridad", "dir": "desc"}]) == [
        "baja", "media", "alta", "sin sitio", "vacia",
    ]


def test_la_prioridad_desempata_en_un_orden_de_varias_claves():
    # Segunda clave: dentro de cada fecha, manda la prioridad. La fecha sigue
    # siendo la primera (`desc` en ella no la desordena).
    filas = [
        SimpleNamespace(properties={"nombre": "uno", "fecha": "2026-01-01", "prioridad": "baja"}),
        SimpleNamespace(properties={"nombre": "dos", "fecha": "2026-01-01", "prioridad": "alta"}),
        SimpleNamespace(properties={"nombre": "tres", "fecha": "2026-02-01", "prioridad": "media"}),
        SimpleNamespace(properties={"nombre": "cuatro", "fecha": "2026-01-01", "prioridad": "media"}),
    ]
    claves = [{"key": "fecha", "dir": "asc"}, {"key": "prioridad", "dir": "asc"}]
    assert ordenar(filas, claves) == ["dos", "cuatro", "uno", "tres"]
    # Y al revés en la prioridad, sin mover la fecha.
    claves = [{"key": "fecha", "dir": "asc"}, {"key": "prioridad", "dir": "desc"}]
    assert ordenar(filas, claves) == ["uno", "cuatro", "dos", "tres"]


def test_ordenar_por_prioridad_deja_que_la_segunda_clave_ordene_dentro():
    filas = [
        SimpleNamespace(properties={"nombre": "alta-tarde", "fecha": "2026-02-01", "prioridad": "alta"}),
        SimpleNamespace(properties={"nombre": "alta-pronto", "fecha": "2026-01-01", "prioridad": "alta"}),
        SimpleNamespace(properties={"nombre": "media", "fecha": "2026-01-01", "prioridad": "media"}),
    ]
    claves = [{"key": "prioridad", "dir": "asc"}, {"key": "fecha", "dir": "asc"}]
    assert ordenar(filas, claves) == ["alta-pronto", "alta-tarde", "media"]


def test_una_multiseleccion_se_ordena_por_su_valor_mas_importante():
    schema = [
        {
            "key": "etiquetas",
            "name": "Etiquetas",
            "type": "multiselect",
            "options": ["c", "b", "a"],
            "sort_order": ["a", "b", "c"],
        }
    ]
    filas = [
        SimpleNamespace(properties={"nombre": "solo-c", "etiquetas": ["c"]}),
        SimpleNamespace(properties={"nombre": "b-y-c", "etiquetas": ["b", "c"]}),
        SimpleNamespace(properties={"nombre": "a", "etiquetas": ["a"]}),
        SimpleNamespace(properties={"nombre": "rara", "etiquetas": ["zzz"]}),
    ]
    asc = nombres(
        apply_view(
            filas,
            {"sort": [{"key": "etiquetas", "dir": "asc"}]},
            orders=order_ranks(schema),
        )
    )
    # `b-y-c` es una fila de `b`: manda su valor más importante, no el primero
    # que se escribió ni el último.
    assert asc == ["a", "b-y-c", "solo-c", "rara"]


def test_sin_orden_declarado_sigue_alfabetico_como_hoy():
    # Es el requisito de no romper nada: la base que no declara `sort_order` se
    # ordena exactamente como antes (alfabético, sin distinguir mayúsculas).
    sin_orden = {
        "key": "prioridad",
        "name": "Prioridad",
        "type": "select",
        "options": ["baja", "media", "alta"],
    }
    filas = [fila("media", "media"), fila("alta", "alta"), fila("baja", "baja")]
    assert ordenar(filas, [{"key": "prioridad", "dir": "asc"}], schema=(sin_orden,)) == [
        "alta", "baja", "media",
    ]


def test_el_orden_no_distingue_mayusculas_ni_espacios():
    # El `.md` lo escribe una persona: `Alta` es la misma opción que `alta`.
    filas = [fila("media", "MEDIA "), fila("alta", "Alta"), fila("baja", " baja")]
    assert ordenar(filas, [{"key": "prioridad", "dir": "asc"}]) == ["alta", "media", "baja"]


# --------------------------------------------------------------------------
# 5-6. ida y vuelta por la API: el `.md` es la verdad
# --------------------------------------------------------------------------

SCHEMA = [
    {
        "key": "prioridad",
        "name": "Prioridad",
        "type": "select",
        "options": ["baja", "media", "alta"],
        "sort_order": ["alta", "media", "baja"],
    },
    {"key": "fecha", "name": "Fecha", "type": "date"},
]

# Cinco filas, cada una con su fecha: la prioridad sola deja empates y la fecha
# los deshace.
FILAS = [
    ("Uno", "alta", "2026-03-01"),
    ("Dos", "media", "2026-01-01"),
    ("Tres", "baja", "2026-01-02"),
    ("Cuatro", "media", "2026-02-01"),
    ("Cinco", "alta", "2026-01-15"),
]


def crear_base(client, schema=None) -> dict:
    respuesta = client.post(
        "/databases", json={"title": "Tareas", "schema_def": schema or SCHEMA}
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_filas(client, database_id):
    for titulo, prioridad, fecha in FILAS:
        respuesta = client.post(
            f"/databases/{database_id}/rows",
            json={"title": titulo, "properties": {"prioridad": prioridad, "fecha": fecha}},
        )
        assert respuesta.status_code == 200, respuesta.text


def crear_vista(client, database_id, nombre, config, tipo="table") -> dict:
    respuesta = client.post(
        f"/databases/{database_id}/views",
        json={"name": nombre, "type": tipo, "config": config},
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def prioridades(client, database_id, view_id):
    filas = client.get(f"/databases/{database_id}/rows?view={view_id}").json()
    return [r["properties"].get("prioridad") for r in filas]


def titulos(client, database_id, view_id=None):
    url = f"/databases/{database_id}/rows"
    if view_id:
        url += f"?view={view_id}"
    return [r["title"] for r in client.get(url).json()]


def md_de_la_base(vault_path: Path) -> str:
    return (vault_path / "Bases" / "Tareas.md").read_text(encoding="utf-8")


def test_el_orden_se_guarda_en_el_md_de_la_base_y_ordena_lo_que_se_ve(client, vault_path):
    base = crear_base(client)
    crear_filas(client, base["id"])
    vista = crear_vista(client, base["id"], "Por prioridad", {"sort": [{"key": "prioridad", "dir": "asc"}]})

    # El orden es de la PROPIEDAD, no de la fila ni de la vista: está en el
    # `schema` del `.md` de la base.
    texto = md_de_la_base(vault_path)
    # YAML legible a mano, en el `schema` de la base (junto a `options`).
    assert "sort_order: [alta, media, baja]" in texto
    guardada = client.get(f"/databases/{base['id']}").json()["schema_def"]
    assert guardada[0]["sort_order"] == ["alta", "media", "baja"]

    # Lo que pinta la vista: alta, media, baja (y no alta, baja, media).
    assert prioridades(client, base["id"], vista["id"]) == [
        "alta", "alta", "media", "media", "baja",
    ]
    # `desc` da la vuelta al orden declarado, no al alfabeto.
    desc = crear_vista(client, base["id"], "Al revés", {"sort": [{"key": "prioridad", "dir": "desc"}]})
    assert prioridades(client, base["id"], desc["id"]) == [
        "baja", "media", "media", "alta", "alta",
    ]
    # Y como clave secundaria desempata sin desordenar la primera.
    compuesta = crear_vista(
        client,
        base["id"],
        "Fecha y prioridad",
        {"sort": [{"key": "fecha", "dir": "asc"}, {"key": "prioridad", "dir": "asc"}]},
    )
    filas = client.get(f"/databases/{base['id']}/rows?view={compuesta['id']}").json()
    assert [r["properties"]["fecha"] for r in filas] == [
        "2026-01-01", "2026-01-02", "2026-01-15", "2026-02-01", "2026-03-01",
    ]
    assert [r["properties"]["prioridad"] for r in filas] == [
        "media", "baja", "alta", "media", "alta",
    ]

    # Reindexar no se lleva nada: el `.md` es la verdad, también en el orden.
    assert client.post("/reindex").status_code == 200
    guardada = client.get(f"/databases/{base['id']}").json()["schema_def"]
    assert guardada[0]["sort_order"] == ["alta", "media", "baja"]
    assert prioridades(client, base["id"], vista["id"]) == [
        "alta", "alta", "media", "media", "baja",
    ]


def test_un_valor_escrito_a_mano_en_el_md_va_al_final_en_los_dos_sentidos(client, vault_path):
    base = crear_base(client)
    crear_filas(client, base["id"])
    vista = crear_vista(client, base["id"], "Por prioridad", {"sort": [{"key": "prioridad", "dir": "asc"}]})

    # Alguien escribe a mano una fila con un valor que la base no conoce (o
    # añadió una opción después y no la declaró en el orden).
    carpeta = vault_path / "Bases" / "Tareas"
    (carpeta / "3a7f0000.md").write_text(
        "---\n"
        "id: 3a7f0000-0000-4000-8000-0000000000aa\n"
        "title: Sexta\n"
        "prioridad: urgente\n"
        "---\n",
        encoding="utf-8",
    )
    assert client.post("/reindex").status_code == 200

    asc = client.get(f"/databases/{base['id']}/rows?view={vista['id']}").json()
    # Al final, no el primero: no tiene sitio declarado.
    assert [r["title"] for r in asc][-1] == "Sexta"

    desc = crear_vista(client, base["id"], "Al revés", {"sort": [{"key": "prioridad", "dir": "desc"}]})
    abajo = client.get(f"/databases/{base['id']}/rows?view={desc['id']}").json()
    assert [r["title"] for r in abajo][-1] == "Sexta"


def test_guardar_el_resto_del_schema_no_borra_el_orden_y_vacio_lo_quita(client, vault_path):
    base = crear_base(client)
    # El diálogo de propiedades no pregunta por `sort_order`: un PATCH que no lo
    # trae (renombrar otra propiedad) lo conserva.
    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {
                    "key": "prioridad",
                    "name": "Prioridad",
                    "type": "select",
                    "options": ["baja", "media", "alta"],
                },
                {"key": "fecha", "name": "Fecha de entrega", "type": "date"},
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["schema_def"][0]["sort_order"] == ["alta", "media", "baja"]

    # `[]` es «quítalo», y el `.md` deja de tener la clave (no un `sort_order: []`).
    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {
                    "key": "prioridad",
                    "name": "Prioridad",
                    "type": "select",
                    "options": ["baja", "media", "alta"],
                    "sort_order": [],
                },
                {"key": "fecha", "name": "Fecha de entrega", "type": "date"},
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    assert "sort_order" not in respuesta.json()["schema_def"][0]
    assert "sort_order" not in md_de_la_base(vault_path)


def test_el_orden_se_limpia_al_guardarlo(client):
    base = crear_base(
        client,
        schema=[
            {
                "key": "prioridad",
                "name": "Prioridad",
                "type": "select",
                "options": ["alta", "media", "baja"],
                # Con espacios, repetido y con huecos: lo que escribe una persona.
                "sort_order": [" alta ", "media", "Alta", "", "baja"],
            }
        ],
    )
    assert base["schema_def"][0]["sort_order"] == ["alta", "media", "baja"]


def test_sort_order_solo_vale_en_una_seleccion(client):
    respuesta = client.post(
        "/databases",
        json={
            "title": "Notas",
            "schema_def": [
                {"key": "texto", "name": "Texto", "type": "text", "sort_order": ["a", "b"]}
            ],
        },
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "sort_order" in respuesta.text


def test_un_valor_declarado_que_no_esta_en_las_opciones_ordena_igual(client):
    # El `.md` es del usuario: se puede declarar el sitio de un valor que no
    # está en `options` (una opción retirada, o escrita a mano en las filas).
    # La propiedad no rechaza el orden; ordena las filas que lo tengan.
    base = crear_base(
        client,
        schema=[
            {
                "key": "prioridad",
                "name": "Prioridad",
                "type": "select",
                "options": ["alta", "media", "baja"],
                "sort_order": ["urgente", "alta", "media", "baja"],
            },
            {"key": "fecha", "name": "Fecha", "type": "date"},
        ],
    )
    assert base["schema_def"][0]["sort_order"] == ["urgente", "alta", "media", "baja"]
    vista = crear_vista(client, base["id"], "Todo", {"sort": [{"key": "prioridad", "dir": "asc"}]})
    crear_filas(client, base["id"])
    # Ninguna fila es `urgente`: el orden de las que hay no cambia por declararlo.
    assert prioridades(client, base["id"], vista["id"]) == [
        "alta", "alta", "media", "media", "baja",
    ]
