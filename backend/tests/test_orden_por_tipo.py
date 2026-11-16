"""El orden de una tabla, por **tipo de propiedad** y con modo manual o automático.

La tarjeta anterior dejó la mitad de abajo: una selección podía declarar el
orden de sus valores (`sort_order`). Lo que se fija aquí es el resto:

1. cada tipo ordena según lo suyo (texto natural, número, fecha, casilla,
   persona por nombre visible, relación por título, título);
2. una selección o multiselección elige modo por propiedad (`sort_mode:
   manual | auto`), y en automático se ordena por el texto natural;
3. los vacíos —y lo que no se puede ordenar en su tipo: una fecha imposible,
   una relación rota— van al final en las dos direcciones;
4. varias claves siguen funcionando y cada una usa su regla;
5. las bases que no declaran nada se ordenan como hoy (texto natural), y el
   `.md` es la verdad del modo y del orden.

El `sort_order` sin `sort_mode` se lee en `manual` (es lo que escribía la
primera versión, y un `.md` que declara un orden no puede pasar a ignorarlo en
silencio); sin ninguno de los dos, la propiedad es `auto`.
"""

from pathlib import Path
from types import SimpleNamespace

from app.properties import order_ranks, schema_types, sort_mode
from app.views import apply_view


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------

def fila(nombre, **propiedades):
    return SimpleNamespace(title=nombre, properties={"nombre": nombre, **propiedades})


def nombres(filas):
    return [f.properties.get("nombre") for f in filas]


def ordenar(filas, claves, schema, *, refs=None, people=None):
    return nombres(
        apply_view(
            filas,
            {"sort": claves},
            orders=order_ranks(schema),
            types=schema_types(schema),
            refs=refs,
            people=people,
        )
    )


def una_clave(key, dir="asc"):
    return [{"key": key, "dir": dir}]


TEXTO = [{"key": "nombre", "name": "Nombre", "type": "text"}]


# --------------------------------------------------------------------------
# 1. un caso por tipo
# --------------------------------------------------------------------------

def test_texto_es_natural_y_no_distingue_mayusculas_ni_tildes():
    filas = [
        fila("Tarea 10"),
        fila("tarea 2"),
        fila("Tarea 1"),
        fila("Ávila"),
        fila("avion"),
        fila("Banco"),
    ]
    assert ordenar(filas, una_clave("nombre"), TEXTO) == [
        # «avila» < «avion» < «banco»: la tilde y la mayúscula no cuentan.
        "Ávila", "avion", "Banco",
        # Y los números como números: 1, 2, 10 (no 1, 10, 2).
        "Tarea 1", "tarea 2", "Tarea 10",
    ]


def test_numero_ordena_numericamente_aunque_llegue_como_texto():
    filas = [fila("diez", n="10"), fila("dos", n=2), fila("tres", n="3")]
    schema = [{"key": "n", "name": "N", "type": "number"}]
    assert ordenar(filas, una_clave("n"), schema) == ["dos", "tres", "diez"]
    assert ordenar(filas, una_clave("n", "desc"), schema) == ["diez", "tres", "dos"]


def test_fecha_es_cronologica_y_la_imposible_va_al_final_en_los_dos_sentidos():
    filas = [
        fila("marzo", f="2026-03-01"),
        fila("imposible", f="2026-02-31"),
        fila("enero-tarde", f="2026-01-15"),
        fila("enero", f="2026-01-01"),
        fila("vacia", f=None),
    ]
    schema = [{"key": "f", "name": "F", "type": "date"}]
    assert ordenar(filas, una_clave("f"), schema) == [
        "enero", "enero-tarde", "marzo", "imposible", "vacia",
    ]
    assert ordenar(filas, una_clave("f", "desc"), schema) == [
        "marzo", "enero-tarde", "enero", "imposible", "vacia",
    ]


def test_casilla_marcadas_al_final_en_asc_y_primero_en_desc():
    filas = [fila("marcada", c=True), fila("sin", c=False), fila("vacia", c=None)]
    schema = [{"key": "c", "name": "C", "type": "checkbox"}]
    assert ordenar(filas, una_clave("c"), schema) == ["sin", "marcada", "vacia"]
    assert ordenar(filas, una_clave("c", "desc"), schema) == ["marcada", "sin", "vacia"]


def test_seleccion_en_auto_ordena_por_el_texto_natural():
    # `options` está en orden de creación y `sort_order` declara otro: en auto
    # mandan las dos a la vez, ordena el alfabeto natural.
    schema = [
        {
            "key": "prioridad",
            "name": "Prioridad",
            "type": "select",
            "options": ["baja", "media", "alta"],
            "sort_order": ["alta", "media", "baja"],
            "sort_mode": "auto",
        }
    ]
    filas = [fila("media", prioridad="media"), fila("alta", prioridad="alta"), fila("baja", prioridad="baja")]
    assert ordenar(filas, una_clave("prioridad"), schema) == ["alta", "baja", "media"]
    assert sort_mode(schema[0]) == "auto"
    assert order_ranks(schema) == {}


def test_multiseleccion_en_auto_ordena_por_su_valor_alfabeticamente_menor():
    schema = [
        {
            "key": "etiquetas",
            "name": "Etiquetas",
            "type": "multiselect",
            "options": ["c", "b", "a"],
            "sort_mode": "auto",
        }
    ]
    filas = [
        fila("solo-c", etiquetas=["c"]),
        fila("b-y-c", etiquetas=["c", "b"]),
        fila("a", etiquetas=["a"]),
        fila("sin", etiquetas=[]),
    ]
    # `b-y-c` es una fila de `b`: manda su valor alfabéticamente menor.
    assert ordenar(filas, una_clave("etiquetas"), schema) == ["a", "b-y-c", "solo-c", "sin"]


def test_persona_ordena_por_su_nombre_visible_y_no_por_el_id():
    filas = [
        fila("de-zoe", p="u1"),
        fila("de-ana", p="u2"),
        fila("sin", p=None),
        fila("borrada", p="u9"),
    ]
    schema = [{"key": "p", "name": "P", "type": "person"}]
    people = {"u1": "Zoe", "u2": "Ana", "u9": "Ana"}
    # Los ids dicen lo contrario que los nombres (`u1` < `u2`, pero «Zoe» va
    # después de «Ana»): manda el nombre visible.
    assert ordenar(filas, una_clave("p"), schema, people=people) == [
        "de-ana", "borrada", "de-zoe", "sin",
    ]


def test_relacion_ordena_por_el_titulo_y_la_rota_va_al_final_en_los_dos_sentidos():
    filas = [
        fila("rota", r="r-borrada"),
        fila("beta", r="r2"),
        fila("alfa", r="r1"),
        fila("vacia", r=None),
    ]
    schema = [{"key": "r", "name": "R", "type": "relation"}]
    refs = {"r1": ("Alfa", "otra"), "r2": ("Beta", "otra")}
    assert ordenar(filas, una_clave("r"), schema, refs=refs) == [
        "alfa", "beta", "rota", "vacia",
    ]
    assert ordenar(filas, una_clave("r", "desc"), schema, refs=refs) == [
        "beta", "alfa", "rota", "vacia",
    ]


def test_el_titulo_ordena_con_la_regla_del_texto():
    filas = [
        SimpleNamespace(title="Tarea 10", properties={}),
        SimpleNamespace(title="Tarea 2", properties={}),
        SimpleNamespace(title="Tarea 1", properties={}),
    ]
    claves = una_clave("title")
    assert [f.title for f in apply_view(filas, {"sort": claves}, types={"title": "text"})] == [
        "Tarea 1", "Tarea 2", "Tarea 10",
    ]


# --------------------------------------------------------------------------
# 3-4. varias claves, cada una con su regla; vacíos al final siempre
# --------------------------------------------------------------------------

def test_varias_claves_usan_cada_una_la_regla_de_su_tipo():
    schema = [
        {"key": "f", "name": "F", "type": "date"},
        {"key": "n", "name": "N", "type": "number"},
    ]
    filas = [
        fila("b", f="2026-01-01", n="10"),
        fila("a", f="2026-01-01", n=2),
        fila("c", f="2026-02-01", n="9"),
        fila("sin-fecha", f=None, n=1),
    ]
    claves = [{"key": "f", "dir": "asc"}, {"key": "n", "dir": "asc"}]
    # Dentro de la misma fecha manda el número (2 antes que 10), y la fila sin
    # fecha se queda al final aunque su número sea el más pequeño.
    assert ordenar(filas, claves, schema) == ["a", "b", "c", "sin-fecha"]
    claves = [{"key": "f", "dir": "desc"}, {"key": "n", "dir": "desc"}]
    assert ordenar(filas, claves, schema) == ["c", "b", "a", "sin-fecha"]


def test_los_vacios_van_al_final_en_asc_y_en_desc_para_cada_tipo():
    filas = [fila("llena", v="b"), fila("vacia", v=""), fila("otra", v="a")]
    for tipo in ("text", "date", "number", "select", "multiselect", "checkbox", "url"):
        schema = [{"key": "v", "name": "V", "type": tipo}]
        assert ordenar(filas, una_clave("v"), schema)[-1] == "vacia", tipo
        assert ordenar(filas, una_clave("v", "desc"), schema)[-1] == "vacia", tipo


# --------------------------------------------------------------------------
# 5. por la API: el `.md` es la verdad del modo y del orden
# --------------------------------------------------------------------------

def crear_base(client, schema_def, title="Tareas"):
    respuesta = client.post("/databases", json={"title": title, "schema_def": schema_def})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id, title, properties):
    respuesta = client.post(
        f"/databases/{database_id}/rows", json={"title": title, "properties": properties}
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_vista(client, database_id, nombre, config):
    respuesta = client.post(
        f"/databases/{database_id}/views",
        json={"name": nombre, "type": "table", "config": config},
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def filas_de(client, database_id, view_id):
    return client.get(f"/databases/{database_id}/rows?view={view_id}").json()


def titulos(filas):
    return [f["title"] for f in filas]


def test_por_la_api_cada_columna_ordena_con_su_regla(client):
    base = crear_base(
        client,
        [
            {"key": "texto", "name": "Texto", "type": "text"},
            {"key": "numero", "name": "Número", "type": "number"},
            {"key": "fecha", "name": "Fecha", "type": "date"},
            {"key": "hecho", "name": "Hecho", "type": "checkbox"},
        ],
    )
    crear_fila(client, base["id"], "Fila 10", {"texto": "Tarea 10", "numero": 10, "fecha": "2026-01-10", "hecho": True})
    crear_fila(client, base["id"], "Fila 2", {"texto": "tarea 2", "numero": 2, "fecha": "2026-01-02", "hecho": False})
    crear_fila(client, base["id"], "Fila 1", {"texto": "Tarea 1", "numero": 1, "fecha": "2026-01-01", "hecho": True})

    for clave, esperado in [
        ("texto", ["Fila 1", "Fila 2", "Fila 10"]),
        ("numero", ["Fila 1", "Fila 2", "Fila 10"]),
        ("fecha", ["Fila 1", "Fila 2", "Fila 10"]),
        ("hecho", ["Fila 2", "Fila 10", "Fila 1"]),
    ]:
        vista = crear_vista(client, base["id"], f"Por {clave}", {"sort": una_clave(clave)})
        assert titulos(filas_de(client, base["id"], vista["id"])) == esperado, clave


def test_por_la_api_una_relacion_ordena_por_el_titulo_de_la_fila_enlazada(client, vault_path):
    contactos = crear_base(client, [{"key": "nombre", "name": "Nombre", "type": "text"}], "Contactos")
    for titulo in ("Zoe", "Ana"):
        crear_fila(client, contactos["id"], titulo, {})
    filas_contactos = {
        f["title"]: f["id"] for f in client.get(f"/databases/{contactos['id']}/rows").json()
    }

    reuniones = crear_base(
        client,
        [
            {
                "key": "contacto",
                "name": "Contacto",
                "type": "relation",
                "target_database": contactos["id"],
            }
        ],
        "Reuniones",
    )
    crear_fila(client, reuniones["id"], "Con Zoe", {"contacto": filas_contactos["Zoe"]})
    crear_fila(client, reuniones["id"], "Con Ana", {"contacto": filas_contactos["Ana"]})
    crear_fila(client, reuniones["id"], "Sin nadie", {})

    # Una relación escrita a mano que apunta a una fila que ya no existe.
    carpeta = vault_path / "Bases" / "Reuniones"
    (carpeta / "rota0000.md").write_text(
        "---\n"
        "id: rota0000-0000-4000-8000-0000000000aa\n"
        "title: Rota\n"
        "contacto: 00000000-0000-4000-8000-000000000000\n"
        "---\n",
        encoding="utf-8",
    )
    assert client.post("/reindex").status_code == 200

    asc = crear_vista(client, reuniones["id"], "Por contacto", {"sort": una_clave("contacto")})
    assert titulos(filas_de(client, reuniones["id"], asc["id"])) == [
        "Con Ana", "Con Zoe", "Rota", "Sin nadie",
    ]
    desc = crear_vista(client, reuniones["id"], "Al revés", {"sort": una_clave("contacto", "desc")})
    assert titulos(filas_de(client, reuniones["id"], desc["id"])) == [
        "Con Zoe", "Con Ana", "Rota", "Sin nadie",
    ]


def test_el_modo_y_el_orden_se_guardan_en_el_md_y_el_auto_ignora_el_orden(client, vault_path):
    base = crear_base(
        client,
        [
            {
                "key": "prioridad",
                "name": "Prioridad",
                "type": "select",
                "options": ["baja", "media", "alta"],
            }
        ],
    )
    for titulo, valor in (("Uno", "alta"), ("Dos", "media"), ("Tres", "baja")):
        crear_fila(client, base["id"], titulo, {"prioridad": valor})

    # Lo que manda el arrastre del tablero: modo manual con el orden nuevo.
    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {
                    "key": "prioridad",
                    "name": "Prioridad",
                    "type": "select",
                    "options": ["baja", "media", "alta"],
                    "sort_mode": "manual",
                    "sort_order": ["baja", "alta", "media"],
                }
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    texto = (vault_path / "Bases" / "Tareas.md").read_text(encoding="utf-8")
    assert "sort_mode: manual" in texto
    assert "sort_order: [baja, alta, media]" in texto

    vista = crear_vista(client, base["id"], "Por prioridad", {"sort": una_clave("prioridad")})
    assert titulos(filas_de(client, base["id"], vista["id"])) == ["Tres", "Uno", "Dos"]

    # El diálogo pasa la propiedad a automático: el orden guardado se suelta y
    # la tabla vuelve al alfabético natural.
    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {
                    "key": "prioridad",
                    "name": "Prioridad",
                    "type": "select",
                    "options": ["baja", "media", "alta"],
                    "sort_mode": "auto",
                }
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    guardada = respuesta.json()["schema_def"][0]
    assert guardada["sort_mode"] == "auto"
    assert "sort_order" not in guardada
    assert titulos(filas_de(client, base["id"], vista["id"])) == ["Uno", "Tres", "Dos"]


def test_un_modo_desconocido_es_400_y_no_se_escribe(client, vault_path):
    base = crear_base(
        client,
        [{"key": "prioridad", "name": "Prioridad", "type": "select", "options": ["a", "b"]}],
    )
    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                {
                    "key": "prioridad",
                    "name": "Prioridad",
                    "type": "select",
                    "options": ["a", "b"],
                    "sort_mode": "aleatorio",
                }
            ]
        },
    )
    assert respuesta.status_code == 422, respuesta.text


def test_una_base_que_no_declara_nada_ordena_por_el_texto_natural(client):
    # Lo de hoy, sin cambiar el orden de nadie: el texto se ordena natural y sin
    # distinguir mayúsculas. Antes «Tarea 10» iba entre «Tarea 1» y «Tarea 2».
    base = crear_base(client, [{"key": "nota", "name": "Nota", "type": "text"}])
    for titulo, valor in (("diez", "Tarea 10"), ("dos", "Tarea 2"), ("uno", "Tarea 1")):
        crear_fila(client, base["id"], titulo, {"nota": valor})
    vista = crear_vista(client, base["id"], "Por nota", {"sort": una_clave("nota")})
    assert titulos(filas_de(client, base["id"], vista["id"])) == ["uno", "dos", "diez"]
