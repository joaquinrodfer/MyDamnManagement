"""Estado cerrado con semántica: la base declara qué cuenta como fila hecha.

Lo que se fija aquí, que es lo que pide la tarjeta (función 5):

1. la base declara su estado en el frontmatter (`closed: {property, values}`) y
   se escribe desde la interfaz con `PUT /databases/{id}/closed`;
2. la regla se valida contra las **opciones reales** de la propiedad: un valor
   que no existe es un 400 que dice cuáles hay, y no deja nada escrito;
3. una sola propiedad cerrada por base: si se marca otra, se mueve;
4. las bases con el `done` viejo se siguen leyendo (compatibilidad), y al
   guardar la regla se escribe el formato nuevo y el `done` se retira;
5. la semántica se aplica en todas partes: contador de subtareas, el campo
   `closed` de cada fila, los filtros `is_closed`/`is_open` y `/my-tasks`,
   leyendo **la regla de cada base** (sin lista de respaldo escondida);
6. quitar el `closed` no cierra nada y cambiar los valores recalcula sin tocar
   el `.md` de ninguna fila.
"""

from __future__ import annotations

from pathlib import Path

ESTADO = {
    "key": "estado",
    "name": "Estado",
    "type": "select",
    "options": ["abierta", "en curso", "hecha", "cerrada"],
}

PRIORIDAD = {
    "key": "prioridad",
    "name": "Prioridad",
    "type": "select",
    "options": ["alta", "baja"],
}

PASO_DE = {"key": "paso_de", "name": "Subtarea de", "type": "parent_row"}

SCHEMA = [PASO_DE, ESTADO, PRIORIDAD]


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------

def crear_base(client, schema=None, title="Proyecto", closed=None):
    cuerpo = {"title": title, "schema_def": schema or SCHEMA}
    if closed is not None:
        cuerpo["closed"] = closed
    respuesta = client.post("/databases", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id, title, properties=None):
    respuesta = client.post(
        f"/databases/{database_id}/rows",
        json={"title": title, "properties": properties or {}},
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def cerrar(client, database_id, prop="estado", valores=("hecha", "cerrada")):
    return client.put(
        f"/databases/{database_id}/closed",
        json={"property": prop, "values": list(valores)},
    )


def filas(client, database_id, vista=None):
    url = f"/databases/{database_id}/rows"
    if vista:
        url += f"?view={vista}"
    respuesta = client.get(url)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_vista(client, database_id, config=None):
    respuesta = client.post(
        f"/databases/{database_id}/views",
        json={"name": "Vista", "type": "table", "config": config or {}},
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def md_base(vault_path: Path, base="Proyecto") -> str:
    return (vault_path / "Bases" / f"{base}.md").read_text(encoding="utf-8")


def md_filas(vault_path: Path, base="Proyecto") -> dict[str, str]:
    """`{nombre: texto}` de los `.md` de las filas de la base."""
    carpeta = vault_path / "Bases" / base
    return {f.name: f.read_text(encoding="utf-8") for f in sorted(carpeta.glob("*.md"))}


def madre_con_tres_pasos(client, estados=("hecha", "hecha", "abierta")):
    """Una madre con tres subtareas; dos cerradas si la regla dice «hecha»."""
    base = crear_base(client)
    madre = crear_fila(client, base["id"], "Entregable")
    pasos = [
        crear_fila(
            client, base["id"], f"Paso {i}", {"paso_de": madre["id"], "estado": estado}
        )
        for i, estado in enumerate(estados, start=1)
    ]
    return base, madre, pasos


def contador(client, database_id, madre_id):
    return next(r for r in filas(client, database_id) if r["id"] == madre_id)["children"]


# --------------------------------------------------------------------------
# 1. la base declara su estado
# --------------------------------------------------------------------------

def test_la_base_declara_que_cuenta_como_hecha(client, vault_path):
    base = crear_base(client)

    respuesta = cerrar(client, base["id"])

    assert respuesta.status_code == 200, respuesta.text
    declarado = respuesta.json()["closed"]
    assert declarado == {
        "property": "estado",
        "values": ["hecha", "cerrada"],
        "groups": [],
        "source": "base",
    }

    texto = md_base(vault_path)
    assert "closed:" in texto
    assert "property: estado" in texto
    assert "values: [hecha, cerrada]" in texto
    # Y no se ha inventado un marcador dentro del schema.
    assert "done" not in texto

    # La lectura devuelve lo mismo: el `.md` es la verdad.
    leida = client.get(f"/databases/{base['id']}").json()
    assert leida["closed"]["property"] == "estado"
    assert leida["closed"]["values"] == ["hecha", "cerrada"]


def test_se_puede_declarar_al_crear_la_base(client, vault_path):
    base = crear_base(
        client, closed={"property": "estado", "values": ["hecha"]}
    )

    assert base["closed"] == {"property": "estado", "values": ["hecha"], "groups": [], "source": "base"}
    assert "closed:" in md_base(vault_path)


# --------------------------------------------------------------------------
# 2. validación contra las opciones reales
# --------------------------------------------------------------------------

def test_un_valor_que_no_existe_es_un_400_y_no_escribe_nada(client, vault_path):
    base = crear_base(client)

    respuesta = cerrar(client, base["id"], valores=("hecha", "inventada"))

    assert respuesta.status_code == 400, respuesta.text
    detalle = respuesta.json()["detail"]
    assert "«Estado»" in detalle
    assert "«inventada»" in detalle
    # El mensaje dice qué valores sí existen: no hay que adivinarlos.
    assert "abierta" in detalle and "hecha" in detalle
    assert "closed" not in md_base(vault_path)
    assert client.get(f"/databases/{base['id']}").json()["closed"] is None


def test_una_propiedad_de_texto_no_puede_ser_el_estado(client):
    base = crear_base(
        client,
        [
            {"key": "estado", "name": "Estado", "type": "select", "options": ["hecha"]},
            {"key": "nota", "name": "Nota", "type": "text"},
        ],
    )

    respuesta = cerrar(client, base["id"], prop="nota", valores=("x",))

    assert respuesta.status_code == 400, respuesta.text
    assert "solo una selección o una casilla" in respuesta.json()["detail"]


def test_una_propiedad_que_no_existe_es_un_400(client):
    base = crear_base(client)

    respuesta = cerrar(client, base["id"], prop="no_existe", valores=("hecha",))

    assert respuesta.status_code == 400, respuesta.text
    assert "no es una propiedad de esta base" in respuesta.json()["detail"]


def test_una_seleccion_sin_valores_o_sin_opciones_es_un_400(client):
    base = crear_base(client)

    sin_valores = cerrar(client, base["id"], valores=())
    assert sin_valores.status_code == 400, sin_valores.text
    assert "al menos un valor" in sin_valores.json()["detail"]

    otra = crear_base(
        client,
        [{"key": "fase", "name": "Fase", "type": "select", "options": []}],
        title="Sin opciones",
    )
    sin_opciones = cerrar(client, otra["id"], prop="fase", valores=("hecha",))
    assert sin_opciones.status_code == 400, sin_opciones.text
    assert "no tiene opciones" in sin_opciones.json()["detail"]


def test_una_casilla_cierra_marcada_y_no_lleva_valores(client, vault_path):
    base = crear_base(
        client,
        [
            {"key": "terminada", "name": "Terminada", "type": "checkbox"},
            ESTADO,
        ],
        title="Tareas",
    )

    respuesta = cerrar(client, base["id"], prop="terminada", valores=())

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["closed"] == {
        "property": "terminada",
        "values": [],
        "groups": [],
        "source": "base",
    }
    con_valores = cerrar(client, base["id"], prop="terminada", valores=("hecha",))
    assert con_valores.status_code == 400, con_valores.text
    assert "es una casilla" in con_valores.json()["detail"]


# --------------------------------------------------------------------------
# 3. una sola propiedad cerrada por base
# --------------------------------------------------------------------------

def test_marcar_otra_propiedad_mueve_la_regla(client, vault_path):
    base = crear_base(client)
    assert cerrar(client, base["id"]).status_code == 200

    respuesta = cerrar(client, base["id"], prop="prioridad", valores=("alta",))

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["closed"]["property"] == "prioridad"
    assert respuesta.json()["closed"]["values"] == ["alta"]
    texto = md_base(vault_path)
    assert texto.count("closed:") == 1
    assert "property: prioridad" in texto
    assert "property: estado" not in texto


# --------------------------------------------------------------------------
# 4. compatibilidad con el `done` viejo
# --------------------------------------------------------------------------

def test_el_done_viejo_se_lee_como_si_fuera_closed(client, vault_path):
    base = crear_base(
        client,
        [
            PASO_DE,
            {**ESTADO, "done": ["hecha"]},
        ],
        title="Vieja",
    )
    madre = crear_fila(client, base["id"], "Entregable")
    crear_fila(client, base["id"], "Paso 1", {"paso_de": madre["id"], "estado": "hecha"})
    crear_fila(client, base["id"], "Paso 2", {"paso_de": madre["id"], "estado": "abierta"})

    leida = client.get(f"/databases/{base['id']}").json()
    assert leida["closed"] == {
        "property": "estado",
        "values": ["hecha"],
        "groups": [],
        "source": "done",
    }
    assert "done: [hecha]" in md_base(vault_path, "Vieja")
    assert contador(client, base["id"], madre["id"]) == {"total": 2, "closed": 1}

    # Guardar la regla escribe el formato nuevo y retira el `done` viejo.
    respuesta = cerrar(client, base["id"], valores=("hecha",))
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["closed"]["source"] == "base"
    texto = md_base(vault_path, "Vieja")
    assert "closed:" in texto and "values: [hecha]" in texto
    assert "done" not in texto


# --------------------------------------------------------------------------
# 5. la semántica, en todas partes
# --------------------------------------------------------------------------

def test_el_contador_de_subtareas_cuenta_lo_cerrado_por_la_base(client):
    base, madre, _pasos = madre_con_tres_pasos(client)
    # Sin regla no hay estado: ninguna hija cuenta como cerrada.
    assert contador(client, base["id"], madre["id"]) == {"total": 3, "closed": 0}

    assert cerrar(client, base["id"]).status_code == 200

    assert contador(client, base["id"], madre["id"]) == {"total": 3, "closed": 2}
    leidas = {r["id"]: r for r in filas(client, base["id"])}
    assert leidas[_pasos[0]["id"]]["closed"] is True
    assert leidas[_pasos[1]["id"]]["closed"] is True
    assert leidas[_pasos[2]["id"]]["closed"] is False


def test_el_filtro_is_open_deja_solo_lo_abierto(client):
    base, madre, _pasos = madre_con_tres_pasos(client)
    assert cerrar(client, base["id"]).status_code == 200

    abiertas = crear_vista(
        client, base["id"], {"parent_level": "all", "filters": [{"op": "is_open"}]}
    )
    titulos = {r["title"] for r in filas(client, base["id"], abiertas["id"])}

    assert "Paso 3" in titulos
    assert not {"Paso 1", "Paso 2"} & titulos
    # La madre no está cerrada (su columna de estado está vacía): entra.
    assert "Entregable" in titulos

    cerradas = crear_vista(
        client, base["id"], {"parent_level": "all", "filters": [{"op": "is_closed"}]}
    )
    titulos = {r["title"] for r in filas(client, base["id"], cerradas["id"])}
    assert titulos == {"Paso 1", "Paso 2"}


def test_el_filtro_is_closed_tambien_admite_la_clave(client):
    base, _madre, _pasos = madre_con_tres_pasos(client)
    assert cerrar(client, base["id"]).status_code == 200

    vista = crear_vista(
        client,
        base["id"],
        {"parent_level": "all", "filters": [{"key": "estado", "op": "is_closed"}]},
    )

    titulos = [r["title"] for r in filas(client, base["id"], vista["id"])]
    assert titulos == ["Paso 1", "Paso 2"]


def test_sin_regla_is_open_deja_pasar_todo(client):
    base, _madre, _pasos = madre_con_tres_pasos(client)

    vista = crear_vista(
        client, base["id"], {"parent_level": "all", "filters": [{"op": "is_open"}]}
    )

    assert len(filas(client, base["id"], vista["id"])) == 4


def test_mis_tareas_excluye_lo_cerrado_segun_cada_base(client):
    yo = client.get("/auth/me").json()["id"]
    persona = {"key": "responsable", "name": "Responsable", "type": "person"}

    con_hecha = crear_base(
        client,
        [persona, ESTADO],
        title="Con hecha",
        closed={"property": "estado", "values": ["hecha"]},
    )
    crear_fila(
        client, con_hecha["id"], "Cerrada aquí", {"responsable": yo, "estado": "hecha"}
    )
    crear_fila(
        client, con_hecha["id"], "Abierta aquí", {"responsable": yo, "estado": "abierta"}
    )

    # Otra base, otra regla: aquí «cerrada» es «en curso» y «hecha» no cierra.
    otra_regla = crear_base(
        client,
        [persona, {**ESTADO, "key": "fase", "name": "Fase"}],
        title="Otra regla",
        closed={"property": "fase", "values": ["en curso"]},
    )
    crear_fila(
        client, otra_regla["id"], "Cerrada allí", {"responsable": yo, "fase": "en curso"}
    )
    crear_fila(
        client, otra_regla["id"], "Abierta allí", {"responsable": yo, "fase": "hecha"}
    )

    # Una base sin regla no esconde nada, aunque su estado se llame «hecha».
    sin_regla = crear_base(
        client, [persona, ESTADO], title="Sin regla", closed=None
    )
    crear_fila(
        client, sin_regla["id"], "Entra entera", {"responsable": yo, "estado": "hecha"}
    )

    datos = client.get("/my-tasks").json()

    assert sorted(t["title"] for t in datos["tasks"]) == [
        "Abierta allí",
        "Abierta aquí",
        "Entra entera",
    ]
    # La lista que explica la vista sale de las bases, no de una constante.
    assert sorted(datos["closed_values"]) == ["en curso", "hecha"]


def test_mis_tareas_sin_bases_con_regla_no_explica_valores(client):
    yo = client.get("/auth/me").json()["id"]
    persona = {"key": "responsable", "name": "Responsable", "type": "person"}
    base = crear_base(client, [persona, ESTADO], title="Sin regla")
    crear_fila(
        client, base["id"], "Solo abierta", {"responsable": yo, "estado": "hecha"}
    )

    datos = client.get("/my-tasks").json()

    assert datos["closed_values"] == []
    assert [t["title"] for t in datos["tasks"]] == ["Solo abierta"]


# --------------------------------------------------------------------------
# 6. nada de dos verdades
# --------------------------------------------------------------------------

def test_quitar_el_closed_no_cierra_nada_y_no_toca_las_filas(client, vault_path):
    base, madre, _pasos = madre_con_tres_pasos(client)
    assert cerrar(client, base["id"]).status_code == 200
    antes = md_filas(vault_path)
    assert contador(client, base["id"], madre["id"]) == {"total": 3, "closed": 2}

    respuesta = client.delete(f"/databases/{base['id']}/closed")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["closed"] is None
    texto = md_base(vault_path)
    assert "closed" not in texto and "done" not in texto
    assert contador(client, base["id"], madre["id"]) == {"total": 3, "closed": 0}
    # Los valores de las filas siguen donde estaban: quitar la regla solo deja
    # de interpretarlos.
    assert md_filas(vault_path) == antes


def test_cambiar_los_valores_recalcula_sin_tocar_las_filas(client, vault_path):
    base, madre, _pasos = madre_con_tres_pasos(client)
    assert cerrar(client, base["id"]).status_code == 200
    antes = md_filas(vault_path)
    assert contador(client, base["id"], madre["id"]) == {"total": 3, "closed": 2}

    # Ahora solo cierra «abierta»: los mismos .md dicen otra cosa.
    assert cerrar(client, base["id"], valores=("abierta",)).status_code == 200

    assert contador(client, base["id"], madre["id"]) == {"total": 3, "closed": 1}
    assert md_filas(vault_path) == antes


def test_renombrar_la_propiedad_del_estado_mueve_la_regla(client, vault_path):
    base = crear_base(client)
    assert cerrar(client, base["id"]).status_code == 200

    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                PASO_DE,
                {**ESTADO, "name": "Situación"},
                PRIORIDAD,
            ]
        },
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["closed"]["property"] == "situacion"
    texto = md_base(vault_path)
    assert "property: situacion" in texto
    assert "property: estado" not in texto


def test_cambiar_las_opciones_ajusta_los_valores_a_su_grafia(client, vault_path):
    base = crear_base(client)
    assert cerrar(client, base["id"]).status_code == 200

    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={
            "schema_def": [
                PASO_DE,
                {**ESTADO, "options": ["abierta", "en curso", "Hecha", "Cerrada"]},
                PRIORIDAD,
            ]
        },
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["closed"]["values"] == ["Hecha", "Cerrada"]
    assert "values: [Hecha, Cerrada]" in md_base(vault_path)


def test_borrar_la_propiedad_del_estado_quita_la_regla(client, vault_path):
    base = crear_base(client)
    assert cerrar(client, base["id"]).status_code == 200

    respuesta = client.patch(
        f"/databases/{base['id']}", json={"schema_def": [PASO_DE, PRIORIDAD]}
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["closed"] is None
    assert "closed" not in md_base(vault_path)


# --------------------------------------------------------------------------
# 7. el `.md` pegado a mano manda
# --------------------------------------------------------------------------

def test_el_frontmatter_escrito_a_mano_se_lee_al_reindexar(client, vault_path):
    base, madre, _pasos = madre_con_tres_pasos(client)
    ruta = vault_path / "Bases" / "Proyecto.md"
    texto = ruta.read_text(encoding="utf-8")
    # El bloque, como lo pegaría el usuario: delante de `schema:` para no
    # tocar nada más del fichero.
    assert "schema:" in texto
    ruta.write_text(
        texto.replace(
            "schema:", "closed:\n  property: estado\n  values: [hecha, cerrada]\nschema:", 1
        ),
        encoding="utf-8",
    )

    assert client.post("/reindex").status_code == 200

    leida = client.get(f"/databases/{base['id']}").json()
    assert leida["closed"] == {
        "property": "estado",
        "values": ["hecha", "cerrada"],
        "groups": [],
        "source": "base",
    }
    assert contador(client, base["id"], madre["id"]) == {"total": 3, "closed": 2}


# --------------------------------------------------------------------------
# 8. la regla NO se cambia por `PATCH /databases/{id}`
# --------------------------------------------------------------------------

def test_patch_de_la_base_con_closed_es_un_400_y_no_escribe_nada(client, vault_path):
    """Sin el campo declarado, Pydantic lo descartaba y el 200 mentía.

    Un cliente que se equivoque de ruta (mandar la regla en el `PATCH` de la
    base en vez de en `PUT /databases/{id}/closed`) tiene que enterarse: 400
    con la ruta buena en el mensaje, y el `.md` byte a byte como estaba.
    """
    base = crear_base(client)
    assert cerrar(client, base["id"], valores=("hecha",)).status_code == 200
    antes = md_base(vault_path)

    respuesta = client.patch(
        f"/databases/{base['id']}",
        json={"closed": {"property": "estado", "values": ["terminada"]}},
    )

    assert respuesta.status_code == 400, respuesta.text
    detalle = respuesta.json()["detail"]
    assert f"PUT /databases/{base['id']}/closed" in detalle
    # El `.md` no cambia a medias y la regla sigue siendo la de antes.
    assert md_base(vault_path) == antes
    leida = client.get(f"/databases/{base['id']}").json()
    assert leida["closed"]["values"] == ["hecha"]


def test_patch_de_la_base_con_closed_null_tampoco_lo_borra(client, vault_path):
    """`null` es «quita la regla» en un cliente que se equivoque de ruta.

    Se rechaza igual: quitar el estado es `DELETE /databases/{id}/closed`.
    """
    base = crear_base(client)
    assert cerrar(client, base["id"]).status_code == 200
    antes = md_base(vault_path)

    respuesta = client.patch(f"/databases/{base['id']}", json={"closed": None})

    assert respuesta.status_code == 400, respuesta.text
    assert md_base(vault_path) == antes
    assert client.get(f"/databases/{base['id']}").json()["closed"] is not None
