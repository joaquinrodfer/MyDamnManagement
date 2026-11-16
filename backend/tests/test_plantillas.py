"""Plantillas: crear un proyecto o una tarea en un clic (función 29).

Lo que fija esta suite, que es lo que pide la tarjeta:

1. las cuatro plantillas de serie (Proyecto, Tareas sueltas, CRM y Reuniones)
   crean una base con su schema, sus 2-3 vistas, la regla `closed` cuando
   aplica y sus plantillas de fila, y **sin filas de ejemplo**;
2. la plantilla se copia: cambiar la plantilla después no cambia las bases ya
   creadas (todo vive en el `.md`);
3. una plantilla que miente —un tipo o un valor que el código no conoce— da un
   400 con el motivo y **no crea nada**;
4. una plantilla de fila guardada en el `.md` crea una fila con sus valores por
   defecto y su cuerpo inicial, y una plantilla inválida no se guarda a medias.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.vault import fm


def frontmatter(path: Path) -> dict:
    raw, _cuerpo = fm.split_document(path.read_text(encoding="utf-8"))
    assert raw is not None, f"{path} no tiene frontmatter"
    return fm.parse(raw).data


def crear_desde(client, template, **cuerpo):
    return client.post(
        "/databases/from-template", json={"template": template, **cuerpo}
    )


def vistas(client, database_id) -> list[dict]:
    respuesta = client.get(f"/databases/{database_id}/views")
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


# --------------------------------------------------------------------------
# 1. cada plantilla de serie crea lo que promete
# --------------------------------------------------------------------------

def test_el_catalogo_ofrece_las_cuatro_plantillas(client):
    catalogo = client.get("/databases/templates").json()
    assert [p["key"] for p in catalogo] == ["proyecto", "tasks", "crm", "reuniones"]
    assert [p["title"] for p in catalogo] == [
        "Proyecto",
        "Tareas sueltas",
        "CRM",
        "Reuniones",
    ]
    # El catálogo dice lo que trae cada plantilla: la interfaz no adivina.
    proyecto = next(p for p in catalogo if p["key"] == "proyecto")
    assert {"Estado", "Responsable", "Prioridad", "Inicio", "Entrega"} <= set(
        proyecto["properties"]
    )
    assert proyecto["views"] == ["Tabla", "Tablero", "Calendario"]
    assert proyecto["row_templates"] == ["Revisión semanal", "Tarea urgente"]
    assert proyecto["closed"] == {"property": "estado", "values": ["hecha"]}


def test_crear_desde_proyecto_deja_esquema_vistas_y_estado(client, vault_path):
    base = crear_desde(client, "proyecto", title="Lanzamiento").json()
    assert base["title"] == "Lanzamiento"
    assert [p["key"] for p in base["schema_def"]] == [
        "estado",
        "responsable",
        "prioridad",
        "inicio",
        "entrega",
        "parte_de",
    ]
    tipos = {p["key"]: p["type"] for p in base["schema_def"]}
    assert tipos["estado"] == "select"
    assert tipos["responsable"] == "person"
    assert tipos["parte_de"] == "parent_row"
    assert base["closed"] == {
        "property": "estado",
        "values": ["hecha"],
        "source": "base",
    }

    # Tres vistas, cada una con SU configuración, escritas en el `.md`.
    vistas_de_la_base = {v["name"]: v for v in vistas(client, base["id"])}
    assert set(vistas_de_la_base) == {"Tabla", "Tablero", "Calendario"}
    assert vistas_de_la_base["Tablero"]["config"] == {
        "group_by": "estado",
        "filters": [{"key": "estado", "op": "neq", "value": "hecha"}],
    }
    assert vistas_de_la_base["Calendario"]["config"] == {
        "date_property": "entrega",
        "filters": [{"key": "entrega", "op": "is_not_empty"}],
    }
    tabla = vistas_de_la_base["Tabla"]["config"]
    assert tabla["sort"] == [{"key": "entrega", "dir": "asc"}]
    assert tabla["order"] == ["estado", "responsable", "prioridad", "entrega"]
    assert tabla["widths"]["estado"] == 140

    # Cero filas: la plantilla no trae datos de mentira.
    assert client.get(f"/databases/{base['id']}/rows").json() == []

    # Las plantillas de fila son parte de la base.
    assert [t["name"] for t in base["row_templates"]] == [
        "Revisión semanal",
        "Tarea urgente",
    ]

    # Y el `.md` dice lo mismo: schema, closed, views y row_templates.
    archivo = vault_path / "Bases" / "Lanzamiento.md"
    datos = frontmatter(archivo)
    assert datos["type"] == "database"
    assert datos["closed"] == {"property": "estado", "values": ["hecha"]}
    assert {v["name"] for v in datos["views"]} == {"Tabla", "Tablero", "Calendario"}
    en_el_fichero = {v["name"]: v for v in datos["views"]}
    assert en_el_fichero["Tablero"]["filters"] == [
        {"key": "estado", "op": "neq", "value": "hecha"}
    ]
    assert en_el_fichero["Calendario"]["filters"] == [
        {"key": "entrega", "op": "is_not_empty"}
    ]
    assert [t["name"] for t in datos["row_templates"]] == [
        "Revisión semanal",
        "Tarea urgente",
    ]
    # Y el `.md` de la base no es una tabla con filas de ejemplo.
    filas = [p for p in (vault_path / "Bases" / "Lanzamiento").iterdir() if p.suffix == ".md"]
    assert filas == []


def test_crear_desde_tareas_sueltas_es_la_version_simple(client, vault_path):
    base = crear_desde(client, "tasks", title="Recados").json()
    assert [p["key"] for p in base["schema_def"]] == ["estado", "prioridad", "fecha"]
    assert base["closed"]["values"] == ["hecha"]
    assert {v["name"] for v in vistas(client, base["id"])} == {"Tabla", "Tablero"}
    datos = frontmatter(vault_path / "Bases" / "Recados.md")
    assert [p["name"] for p in datos["schema"]] == ["Estado", "Prioridad", "Fecha"]
    assert client.get(f"/databases/{base['id']}/rows").json() == []


def test_crear_desde_crm_conserva_la_plantilla_de_siempre(client, vault_path):
    base = crear_desde(client, "crm").json()
    assert base["title"] == "CRM"
    assert {p["key"] for p in base["schema_def"]} == {
        "empresa",
        "fase",
        "valor",
        "proximo_contacto",
    }
    assert {v["name"] for v in vistas(client, base["id"])} == {
        "Pipeline",
        "Tabla",
        "Agenda",
    }
    assert base["closed"]["values"] == ["ganado", "perdido"]
    assert client.get(f"/databases/{base['id']}/rows").json() == []


def test_crear_desde_reuniones_trae_fecha_persona_y_notas(client):
    base = crear_desde(client, "reuniones", title="Semanales").json()
    tipos = {p["key"]: p["type"] for p in base["schema_def"]}
    assert tipos == {"fecha": "date", "asistente": "person", "notas": "text"}
    # Sin estado no hay `closed`: no se inventa una regla que la base no tiene.
    assert base["closed"] is None
    assert {v["name"] for v in vistas(client, base["id"])} == {"Tabla", "Calendario"}
    assert [t["name"] for t in base["row_templates"]] == ["Reunión de equipo"]


def test_la_base_puede_nacer_en_una_carpeta(client, vault_path):
    nota = client.post("/pages", json={"title": "Clientes"}).json()
    base = crear_desde(
        client, "proyecto", title="Web", parent_id=nota["id"]
    ).json()
    assert base["title"] == "Web"
    # Cuelga de la nota: la carpeta de hijas de la nota, no `Bases/`.
    assert (vault_path / "Notas" / "Clientes" / "Web.md").exists()


# --------------------------------------------------------------------------
# 2. copiar es copiar: la plantilla no se queda enlazada
# --------------------------------------------------------------------------

def test_cambiar_la_plantilla_no_cambia_las_bases_ya_creadas(client, vault_path, monkeypatch):
    from app import templates

    antes = crear_desde(client, "proyecto", title="Copia").json()
    archivo = vault_path / "Bases" / "Copia.md"
    original = archivo.read_text(encoding="utf-8")

    # Alguien cambia la plantilla de serie (en el código, en una versión
    # futura). La base que ya existe no puede cambiar: lo suyo está en su .md.
    monkeypatch.setitem(
        templates.TEMPLATES,
        "proyecto",
        {
            **templates.TEMPLATES["proyecto"],
            "schema_def": [
                {"key": "otra", "name": "Otra", "type": "text"},
            ],
            "views": [{"name": "Solo", "type": "table", "config": {}}],
            "row_templates": [],
        },
    )
    assert archivo.read_text(encoding="utf-8") == original
    despues = client.get(f"/databases/{antes['id']}").json()
    assert [p["key"] for p in despues["schema_def"]] == [
        "estado",
        "responsable",
        "prioridad",
        "inicio",
        "entrega",
        "parte_de",
    ]
    assert {v["name"] for v in vistas(client, antes["id"])} == {
        "Tabla",
        "Tablero",
        "Calendario",
    }


# --------------------------------------------------------------------------
# 3. una plantilla que miente no crea nada
# --------------------------------------------------------------------------

def _plantilla_rota(**cambios) -> dict:
    from app import templates

    base = {
        "title": "Rota",
        "schema_def": [{"key": "texto", "name": "Texto", "type": "text"}],
        "views": [{"name": "Tabla", "type": "table", "config": {}}],
    }
    base.update(cambios)
    return base


def test_un_tipo_que_el_codigo_no_conoce_es_un_400_y_no_crea_nada(
    client, vault_path, monkeypatch
):
    from app import templates

    monkeypatch.setitem(
        templates.TEMPLATES,
        "rota",
        _plantilla_rota(
            schema_def=[{"key": "raro", "name": "Raro", "type": "magia"}]
        ),
    )
    respuesta = crear_desde(client, "rota", title="A medias")
    assert respuesta.status_code == 400, respuesta.text
    assert "magia" in respuesta.json()["detail"]
    assert "no conoce" in respuesta.json()["detail"]
    # Ni base, ni página, ni fichero: no se crea a medias.
    assert client.get("/databases").json() == []
    assert client.get("/pages").json() == []
    assert list((vault_path / "Bases").glob("*.md")) == []


def test_una_vista_que_agrupa_por_una_propiedad_que_no_existe_no_crea_nada(
    client, vault_path, monkeypatch
):
    from app import templates

    monkeypatch.setitem(
        templates.TEMPLATES,
        "rota",
        _plantilla_rota(
            views=[{"name": "Tablero", "type": "board", "config": {"group_by": "fase"}}]
        ),
    )
    respuesta = crear_desde(client, "rota")
    assert respuesta.status_code == 400
    assert "fase" in respuesta.json()["detail"]
    assert client.get("/databases").json() == []
    assert list((vault_path / "Bases").glob("*.md")) == []


def test_una_plantilla_de_fila_con_un_valor_inventado_no_crea_nada(
    client, vault_path, monkeypatch
):
    from app import templates

    monkeypatch.setitem(
        templates.TEMPLATES,
        "rota",
        _plantilla_rota(
            schema_def=[
                {
                    "key": "estado",
                    "name": "Estado",
                    "type": "select",
                    "options": ["abierta", "hecha"],
                }
            ],
            row_templates=[
                {"name": "Mala", "properties": {"estado": "inventado"}, "body": ""}
            ],
        ),
    )
    respuesta = crear_desde(client, "rota")
    assert respuesta.status_code == 400
    assert "inventado" in respuesta.json()["detail"]
    assert client.get("/databases").json() == []
    assert list((vault_path / "Bases").glob("*.md")) == []


def test_el_closed_que_no_casa_con_el_esquema_no_crea_nada(
    client, vault_path, monkeypatch
):
    from app import templates

    monkeypatch.setitem(
        templates.TEMPLATES,
        "rota",
        _plantilla_rota(closed={"property": "estado", "values": ["hecha"]}),
    )
    respuesta = crear_desde(client, "rota")
    assert respuesta.status_code == 400
    assert "estado" in respuesta.json()["detail"]
    assert client.get("/databases").json() == []
    assert list((vault_path / "Bases").glob("*.md")) == []


def test_una_plantilla_que_no_existe_es_un_404(client):
    respuesta = crear_desde(client, "no-existe")
    assert respuesta.status_code == 404


# --------------------------------------------------------------------------
# 4. plantillas de fila: guardar y usar
# --------------------------------------------------------------------------

def test_guardar_plantillas_de_fila_las_escribe_en_el_md(client, vault_path):
    base = client.post(
        "/databases",
        json={
            "title": "Tareas",
            "schema_def": [
                {
                    "key": "estado",
                    "name": "Estado",
                    "type": "select",
                    "options": ["abierta", "hecha"],
                },
                {"key": "prioridad", "name": "Prioridad", "type": "text"},
            ],
        },
    ).json()

    respuesta = client.put(
        f"/databases/{base['id']}/row-templates",
        json={
            "row_templates": [
                {
                    "name": "Revisión semanal",
                    "properties": {"estado": "abierta", "prioridad": "media"},
                    "body": "## Qué revisar\n",
                }
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["row_templates"] == [
        {
            "name": "Revisión semanal",
            "properties": {"estado": "abierta", "prioridad": "media"},
            "body": "## Qué revisar\n",
            "values": {"estado": "abierta", "prioridad": "media"},
            "error": None,
        }
    ]

    datos = frontmatter(vault_path / "Bases" / "Tareas.md")
    assert datos["row_templates"] == [
        {
            "name": "Revisión semanal",
            "properties": {"estado": "abierta", "prioridad": "media"},
            "body": "## Qué revisar\n",
        }
    ]

    # Borrarlas quita la clave del fichero: no queda un `row_templates: []`.
    vacia = client.put(
        f"/databases/{base['id']}/row-templates", json={"row_templates": []}
    ).json()
    assert vacia["row_templates"] == []
    assert "row_templates" not in frontmatter(vault_path / "Bases" / "Tareas.md")


def test_una_plantilla_de_fila_invalida_no_se_guarda(client, vault_path):
    base = client.post(
        "/databases",
        json={
            "title": "Tareas",
            "schema_def": [
                {
                    "key": "estado",
                    "name": "Estado",
                    "type": "select",
                    "options": ["abierta", "hecha"],
                }
            ],
        },
    ).json()

    respuesta = client.put(
        f"/databases/{base['id']}/row-templates",
        json={
            "row_templates": [
                {"name": "Mala", "properties": {"estado": "inventado"}, "body": ""}
            ]
        },
    )
    assert respuesta.status_code == 400
    assert "inventado" in respuesta.json()["detail"]
    # El `.md` no cambia: no se guarda a medias.
    assert "row_templates" not in frontmatter(vault_path / "Bases" / "Tareas.md")
    assert client.get(f"/databases/{base['id']}").json()["row_templates"] == []


def test_crear_una_fila_desde_plantilla_escribe_valores_y_cuerpo(client, vault_path):
    base = crear_desde(client, "proyecto", title="Lanzamiento").json()

    respuesta = client.post(
        f"/databases/{base['id']}/rows/from-template",
        json={"template": "Revisión semanal", "title": "Revisión del lunes"},
    )
    assert respuesta.status_code == 200, respuesta.text
    fila = respuesta.json()
    assert fila["title"] == "Revisión del lunes"
    assert fila["properties"]["estado"] == "abierta"
    assert fila["properties"]["prioridad"] == "media"
    assert fila["closed"] is False

    # La fila nace con el cuerpo de la plantilla en su `.md`.
    archivo = vault_path / "Bases" / "Lanzamiento" / f"{fila['id'][:8]}.md"
    texto = archivo.read_text(encoding="utf-8")
    assert "## Qué revisar" in texto
    detalle = client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()
    assert "## Qué revisar" in detalle["body_markdown"]


def test_la_plantilla_de_fila_rellena_pero_no_cierra_la_fila(client):
    base = crear_desde(client, "proyecto", title="Lanzamiento").json()
    fila = client.post(
        f"/databases/{base['id']}/rows/from-template",
        json={"template": "Revisión semanal", "title": "Lunes"},
    ).json()
    # «hecha» cierra; «abierta» no. La plantilla trae abierta, así que la fila
    # nace abierta y el contador de su madre no la cuenta como cerrada.
    assert fila["closed"] is False


def test_una_plantilla_de_fila_que_no_existe_es_un_404(client):
    base = crear_desde(client, "proyecto", title="Lanzamiento").json()
    respuesta = client.post(
        f"/databases/{base['id']}/rows/from-template",
        json={"template": "No está", "title": "X"},
    )
    assert respuesta.status_code == 404


def test_una_fila_puede_nacer_con_cuerpo_por_la_api(client, vault_path):
    base = client.post("/databases", json={"title": "Notas"}).json()
    fila = client.post(
        f"/databases/{base['id']}/rows",
        json={"title": "Con cuerpo", "body_markdown": "Hola\n"},
    ).json()
    archivo = vault_path / "Bases" / "Notas" / f"{fila['id'][:8]}.md"
    assert "Hola" in archivo.read_text(encoding="utf-8")


def test_las_plantillas_de_fila_sobreviven_a_un_reindexado(client, vault_path):
    base = crear_desde(client, "proyecto", title="Lanzamiento").json()
    client.post("/reindex")
    despues = client.get(f"/databases/{base['id']}").json()
    assert [t["name"] for t in despues["row_templates"]] == [
        "Revisión semanal",
        "Tarea urgente",
    ]


# --------------------------------------------------------------------------
# 5. las vistas de serie traen su configuración entera (agrupación y filtros)
# --------------------------------------------------------------------------

FILTROS_DE_SERIE = [
    (
        "proyecto",
        [
            {"key": "estado", "op": "neq", "value": "hecha"},
            {"key": "entrega", "op": "is_not_empty"},
        ],
    ),
    ("tasks", [{"key": "estado", "op": "neq", "value": "hecha"}]),
    ("crm", [{"key": "proximo_contacto", "op": "is_not_empty"}]),
    ("reuniones", [{"key": "fecha", "op": "is_not_empty"}]),
]


@pytest.mark.parametrize("clave,filtros", FILTROS_DE_SERIE)
def test_cada_plantilla_de_serie_trae_sus_filtros_en_el_md(
    client, vault_path, clave, filtros
):
    base = crear_desde(client, clave, title="Copia").json()
    datos = frontmatter(vault_path / "Bases" / "Copia.md")
    en_el_md = [f for v in datos["views"] for f in (v.get("filters") or [])]
    assert en_el_md == filtros
    # Y la API devuelve lo mismo: el filtro no se queda a medio escribir.
    devueltos = [f for v in vistas(client, base["id"]) for f in (v["config"].get("filters") or [])]
    assert devueltos == filtros


# --------------------------------------------------------------------------
# 6. la plantilla de fila viaja resuelta y no miente al pintarse
# --------------------------------------------------------------------------

def base_de_tipos(client):
    return client.post(
        "/databases",
        json={
            "title": "Tipos",
            "schema_def": [
                {"key": "hecho", "name": "Hecho", "type": "checkbox"},
                {"key": "horas", "name": "Horas", "type": "number"},
                {"key": "cuando", "name": "Cuándo", "type": "date"},
                {
                    "key": "estado",
                    "name": "Estado",
                    "type": "select",
                    "options": ["abierta", "hecha"],
                },
            ],
        },
    ).json()


def test_la_plantilla_de_fila_llega_con_los_valores_coaccionados(client):
    base = base_de_tipos(client)
    client.put(
        f"/databases/{base['id']}/row-templates",
        json={
            "row_templates": [
                {
                    "name": "Completa",
                    "properties": {
                        "hecho": False,
                        "horas": 3,
                        "cuando": "2026-11-16",
                        "estado": "abierta",
                    },
                    "body": "## Contexto\n",
                }
            ]
        },
    )
    leida = client.get(f"/databases/{base['id']}").json()["row_templates"][0]
    # El `.md` no infiere tipos: se lee texto…
    assert leida["properties"] == {
        "hecho": "false",
        "horas": "3",
        "cuando": "2026-11-16",
        "estado": "abierta",
    }
    # …y `values` es lo que se puede pintar en un control. `Boolean("false")` en
    # el cliente marcaba la casilla y la fila nacía con `hecho: true`.
    assert leida["values"] == {
        "hecho": False,
        "horas": 3,
        "cuando": "2026-11-16",
        "estado": "abierta",
    }
    assert leida["error"] is None


def test_guardar_el_editor_sin_tocar_nada_no_reescribe_el_md(client, vault_path):
    base = base_de_tipos(client)
    client.put(
        f"/databases/{base['id']}/row-templates",
        json={
            "row_templates": [
                {"name": "Con casilla", "properties": {"hecho": False}, "body": ""}
            ]
        },
    )
    archivo = vault_path / "Bases" / "Tipos.md"
    antes = frontmatter(archivo)["row_templates"]

    # Es exactamente lo que hace el editor al abrir y guardar: manda de vuelta
    # los valores que pintó (los coaccionados), no el texto del `.md`.
    leida = client.get(f"/databases/{base['id']}").json()["row_templates"][0]
    respuesta = client.put(
        f"/databases/{base['id']}/row-templates",
        json={
            "row_templates": [
                {
                    "name": leida["name"],
                    "properties": leida["values"],
                    "body": leida["body"],
                }
            ]
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    assert frontmatter(archivo)["row_templates"] == antes
    texto = archivo.read_text(encoding="utf-8")
    assert "hecho: false" in texto
    assert "hecho: true" not in texto


def test_una_plantilla_que_ya_no_encaja_dice_por_que_y_no_crea_fila(client, vault_path):
    base = crear_desde(client, "proyecto", title="Miente").json()
    archivo = vault_path / "Bases" / "Miente.md"
    # Alguien edita el `.md` a mano (o el fichero viene de otra versión): la
    # plantilla nombra un valor que la propiedad ya no tiene.
    archivo.write_text(
        archivo.read_text(encoding="utf-8").replace(
            "prioridad: media", "prioridad: inventada"
        ),
        encoding="utf-8",
    )
    client.post("/reindex")

    leida = next(
        t
        for t in client.get(f"/databases/{base['id']}").json()["row_templates"]
        if t["name"] == "Revisión semanal"
    )
    assert leida["error"] is not None
    assert "inventada" in leida["error"]
    # El valor que no encaja no viaja como si fuera bueno: pintarlo en un
    # `<select>` sin esa opción lo perdería en silencio.
    assert "prioridad" not in leida["values"]
    assert leida["values"]["estado"] == "abierta"

    # El alta no crea a medias: 400 con el motivo y ninguna fila.
    respuesta = client.post(
        f"/databases/{base['id']}/rows",
        json={"title": "A medias", "template": "Revisión semanal"},
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "inventada" in respuesta.json()["detail"]
    assert client.get(f"/databases/{base['id']}/rows").json() == []


def test_el_alta_con_plantilla_revalida_y_respeta_lo_editado(client):
    base = crear_desde(client, "proyecto", title="Alta").json()
    respuesta = client.post(
        f"/databases/{base['id']}/rows",
        json={
            "title": "Urgente de verdad",
            "template": "Tarea urgente",
            "properties": {"prioridad": "baja"},
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    fila = respuesta.json()
    assert fila["properties"]["prioridad"] == "baja"  # lo editado manda
    assert fila["properties"]["estado"] == "abierta"  # el resto, de la plantilla
    detalle = client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()
    assert "## Qué hay que hacer" in detalle["body_markdown"]

    # Una plantilla que no existe no crea nada.
    inexistente = client.post(
        f"/databases/{base['id']}/rows",
        json={"title": "X", "template": "No está"},
    )
    assert inexistente.status_code == 404


def test_el_alta_puede_vaciar_un_valor_de_la_plantilla(client, vault_path):
    """Un `null` explícito es «no quiero ese valor», no «pon el de la plantilla».

    El alta manda así las claves de la plantilla que el usuario ha vaciado; si
    el servidor las rellenara, desharía el cambio sin decir nada.
    """
    base = crear_desde(client, "proyecto", title="Vacíos").json()
    respuesta = client.post(
        f"/databases/{base['id']}/rows",
        json={
            "title": "Sin estado",
            "template": "Revisión semanal",
            "properties": {"estado": None},
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    fila = respuesta.json()
    datos = frontmatter(
        vault_path / "Bases" / "Vacíos" / f"{fila['id'][:8]}.md"
    )
    assert "estado" not in datos
    assert datos["prioridad"] == "media"


def test_cambiar_el_schema_ajusta_las_plantillas_de_fila(client, vault_path):
    base = crear_desde(client, "proyecto", title="Ajuste").json()
    schema = []
    for prop in base["schema_def"]:
        if prop["key"] == "prioridad":
            # La opción «alta» desaparece del vocabulario de la base.
            prop = {**prop, "options": ["media", "baja"], "sort_order": ["media", "baja"]}
        schema.append(prop)
    respuesta = client.patch(f"/databases/{base['id']}", json={"schema_def": schema})
    assert respuesta.status_code == 200, respuesta.text

    plantillas = {t["name"]: t for t in respuesta.json()["row_templates"]}
    # «Tarea urgente» pedía prioridad alta: el valor se retira, no se queda
    # mintiendo ni el editor lo pierde en silencio al guardar.
    assert plantillas["Tarea urgente"]["properties"] == {"estado": "abierta"}
    assert plantillas["Tarea urgente"]["error"] is None
    assert plantillas["Revisión semanal"]["properties"] == {
        "estado": "abierta",
        "prioridad": "media",
    }
    en_el_md = frontmatter(vault_path / "Bases" / "Ajuste.md")["row_templates"]
    assert {t["name"]: t.get("properties") for t in en_el_md}["Tarea urgente"] == {
        "estado": "abierta"
    }


def test_renombrar_una_propiedad_traduce_las_plantillas_de_fila(client, vault_path):
    base = crear_desde(client, "proyecto", title="Renombre").json()
    schema = [
        {**prop, "name": "Urgencia"} if prop["key"] == "prioridad" else prop
        for prop in base["schema_def"]
    ]
    respuesta = client.patch(f"/databases/{base['id']}", json={"schema_def": schema})
    assert respuesta.status_code == 200, respuesta.text
    claves = {p["key"] for p in respuesta.json()["schema_def"]}
    plantillas = respuesta.json()["row_templates"]
    for plantilla in plantillas:
        assert set(plantilla["properties"]) <= claves
        assert "prioridad" not in plantilla["properties"]
    # La que tenía prioridad la sigue teniendo, con la clave nueva.
    semanal = next(t for t in plantillas if t["name"] == "Revisión semanal")
    assert semanal["values"] == {"estado": "abierta", "urgencia": "media"}


def test_una_fecha_por_defecto_que_no_es_fecha_no_se_guarda(client, vault_path):
    base = client.post(
        "/databases",
        json={
            "title": "Citas",
            "schema_def": [{"key": "cuando", "name": "Cuándo", "type": "date"}],
        },
    ).json()
    respuesta = client.put(
        f"/databases/{base['id']}/row-templates",
        json={
            "row_templates": [
                {"name": "Mala", "properties": {"cuando": "31/12/2026"}, "body": ""}
            ]
        },
    )
    assert respuesta.status_code == 400, respuesta.text
    assert "AAAA-MM-DD" in respuesta.json()["detail"]
    assert "row_templates" not in frontmatter(vault_path / "Bases" / "Citas.md")
