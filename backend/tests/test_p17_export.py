"""P17.4: `GET /pages/{id}/export.md`, el `.md` con las bases dentro materializadas.

Lo que se fija aquí es el criterio de la tarjeta:

1. una página **sin** bases sale **idéntica** a la que exportaba el navegador
   (`exportPageAsMarkdown`: mismo frontmatter, mismo cuerpo);
2. cada bloque `` ```mdm-database `` se sustituye por la tabla de la base: su
   título (el de la base, no el del bloque), las columnas visibles de la vista
   indicada y las filas de esa vista, con el título de la fila delante;
3. si la vista no existe se usa la primera; si no hay ninguna vista, todas las
   propiedades en orden de esquema;
4. `EXPORT_MAX_ROWS` recorta y el documento lo dice (`_Mostrando 200 de 340`);
5. una base inexistente o un `id` inválido **no** producen una tabla vacía en
   silencio: la valla se queda como estaba y debajo va el aviso;
6. los valores con `|` o saltos de línea no rompen la tabla, las fechas van en
   ISO corto y lo vacío es celda vacía.

Todo por HTTP, como el resto de la suite: el endpoint se prueba como lo llama
un `curl` o el navegador, no la función suelta.
"""

import uuid

from app.config import settings

SIMPLE = [
    {"key": "empresa", "name": "Empresa", "type": "text"},
    {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
]

CRM = [
    {"key": "empresa", "name": "Empresa", "type": "text"},
    {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
    {"key": "proximo", "name": "Próximo", "type": "date"},
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def crear_nota(client, title="Nota", body="", **extra):
    respuesta = client.post("/pages", json={"title": title, "body_markdown": body, **extra})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_base(client, title="Contactos", schema_def=None, views=None):
    respuesta = client.post(
        "/databases", json={"title": title, "schema_def": schema_def or SIMPLE}
    )
    assert respuesta.status_code == 200, respuesta.text
    base = respuesta.json()
    for view in views or []:
        crear_vista(client, base["id"], **view)
    return base


def crear_vista(client, database_id, name, type="table", config=None):
    respuesta = client.post(
        f"/databases/{database_id}/views",
        json={"name": name, "type": type, "config": config or {}},
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id, title, **properties):
    respuesta = client.post(
        f"/databases/{database_id}/rows",
        json={"title": title, "properties": properties},
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def bloque(database_id, view=None, title=None):
    """La valla tal como la escribe el editor, con sus pistas opcionales."""
    lineas = ["```mdm-database", f"id: {database_id}"]
    if view is not None:
        lineas.append(f"view: {view}")
    if title is not None:
        lineas.append(f"title: {title}")
    lineas.append("```")
    return "\n".join(lineas)


def exportar(client, nota):
    return client.get(f"/pages/{nota['id']}/export.md")


def filas_de_tabla(texto):
    """Las líneas de datos de la primera tabla del documento (sin cabecera)."""
    lineas = [linea for linea in texto.split("\n") if linea.startswith("|")]
    return lineas[2:]


# --------------------------------------------------------------------------
# 1. sin bases: la descarga de siempre
# --------------------------------------------------------------------------

def test_pagina_sin_bases_es_identica_a_la_de_hoy(client):
    cuerpo = "# Título\n\nUn párrafo con **negrita** y un | que no es tabla."
    nota = crear_nota(client, "Mi nota", cuerpo)
    assert nota["body_markdown"] == cuerpo

    respuesta = exportar(client, nota)

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"] == "text/markdown; charset=utf-8"
    assert respuesta.headers["content-disposition"] == 'attachment; filename="Mi nota.md"'

    # El frontmatter que armaba `exportPageAsMarkdown` en el navegador: mismas
    # claves, mismo orden, comillas dobles y los timestamps tal como los da la
    # API (UTC con `Z`).
    assert nota["created_at"].endswith("Z")
    esperado = (
        "---\n"
        f'title: "{nota["title"]}"\n'
        f'icon: "{nota["icon"]}"\n'
        f'created_by: "{nota["created_by"]}"\n'
        'description: ""\n'
        f'created_at: "{nota["created_at"]}"\n'
        f'updated_at: "{nota["updated_at"]}"\n'
        "---\n\n"
        f"{cuerpo}"
    )
    assert respuesta.text == esperado


def test_un_titulo_con_acentos_viaja_en_filename_estrella(client):
    """Un `filename="..."` no ASCII rompería la cabecera: va el nombre real en
    `filename*` (RFC 5987) y un respaldo ASCII para clientes viejos."""
    nota = crear_nota(client, "Nota de diseño", "cuerpo")

    respuesta = exportar(client, nota)

    assert respuesta.status_code == 200
    disposicion = respuesta.headers["content-disposition"]
    assert disposicion.startswith('attachment; filename="Nota de diseno.md"')
    assert "filename*=UTF-8''Nota%20de%20dise%C3%B1o.md" in disposicion


def test_nota_sin_cuerpo_es_solo_el_frontmatter(client):
    nota = crear_nota(client, "Vacía", "")

    respuesta = exportar(client, nota)

    assert respuesta.status_code == 200
    assert respuesta.text.startswith("---\n")
    assert respuesta.text.endswith("---\n\n")
    # Ni una tabla, ni un aviso: no hay cuerpo que recorrer.
    assert "|" not in respuesta.text
    assert "Base no encontrada" not in respuesta.text


def test_sin_sesion_el_export_es_401(client):
    nota = crear_nota(client, "Privada", "cuerpo")
    client.cookies.clear()

    respuesta = exportar(client, nota)

    assert respuesta.status_code == 401
    assert respuesta.json() == {"detail": "no autenticado"}


def test_pagina_inexistente_es_404(client):
    respuesta = client.get(f"/pages/{uuid.uuid4()}/export.md")
    assert respuesta.status_code == 404


# --------------------------------------------------------------------------
# 2. una base: título de la base, columnas de la vista, filas de la vista
# --------------------------------------------------------------------------

def test_una_base_se_convierte_en_su_tabla(client):
    base = crear_base(client, "Contactos", SIMPLE)
    # Dos vistas a propósito: la primera es la que se usaría por defecto, así
    # que si sale «Tablero» es porque se ha pedido por nombre.
    crear_vista(client, base["id"], "Tabla", config={"visible_properties": ["empresa"]})
    crear_vista(
        client,
        base["id"],
        "Tablero",
        "board",
        {"group_by": "fase", "visible_properties": ["empresa", "fase"]},
    )
    crear_fila(client, base["id"], "Acme", empresa="Acme S.L.", fase="lead")
    crear_fila(client, base["id"], "Beta", empresa="Beta", fase="ganado")

    nota = crear_nota(
        client,
        "Cuentas",
        f"Antes\n\n{bloque(base['id'], 'Tablero', title='Pista para leer a mano')}\n\nDespués",
    )

    texto = exportar(client, nota).text

    # El bloque desaparece y en su sitio queda la tabla, entre el texto previo
    # y el posterior.
    assert "```mdm-database" not in texto
    assert "Pista para leer a mano" not in texto
    esperado = (
        "**Contactos** — vista Tablero (2 filas)\n"
        "\n"
        "| Título | Empresa | Fase |\n"
        "| --- | --- | --- |\n"
        "| Acme | Acme S.L. | lead |\n"
        "| Beta | Beta | ganado |"
    )
    assert esperado in texto
    assert "Antes\n\n**Contactos**" in texto
    assert "Después" in texto

    # Exportar es leer: la nota no cambia.
    assert client.get(f"/pages/{nota['id']}").json()["body_markdown"] == nota["body_markdown"]


def test_dos_bases_en_la_misma_nota(client):
    contactos = crear_base(client, "Contactos", SIMPLE)
    crear_vista(client, contactos["id"], "Principal")
    crear_fila(client, contactos["id"], "Acme", empresa="Acme S.L.", fase="lead")

    tareas = crear_base(
        client,
        "Tareas",
        [{"key": "estado", "name": "Estado", "type": "text"}],
        views=[{"name": "Tablero", "type": "board", "config": {}}],
    )
    crear_fila(client, tareas["id"], "Publicar", estado="hecha")

    nota = crear_nota(
        client,
        "Plan",
        f"{bloque(contactos['id'], 'Principal')}\n\n{bloque(tareas['id'], 'Tablero')}",
    )

    texto = exportar(client, nota).text

    assert "**Contactos** — vista Principal (1 fila)" in texto
    assert "| Título | Empresa | Fase |" in texto
    assert "| Acme | Acme S.L. | lead |" in texto
    assert "**Tareas** — vista Tablero (1 fila)" in texto
    assert "| Título | Estado |" in texto
    assert "| Publicar | hecha |" in texto
    assert texto.count("```mdm-database") == 0


def test_la_base_sin_vistas_usa_todas_las_propiedades_del_esquema(client):
    base = crear_base(client, "Sin vistas", CRM)
    crear_fila(client, base["id"], "Acme", empresa="Acme", fase="lead", proximo="2026-11-12")

    nota = crear_nota(client, "Sin vistas", bloque(base["id"]))

    texto = exportar(client, nota).text

    assert "**Sin vistas** (1 fila)" in texto
    assert "| Título | Empresa | Fase | Próximo |" in texto
    assert "| Acme | Acme | lead | 2026-11-12 |" in texto


def test_vista_inexistente_usa_la_primera(client):
    base = crear_base(client, "Contactos", SIMPLE)
    crear_vista(client, base["id"], "Tabla", config={"visible_properties": ["fase"]})
    crear_vista(client, base["id"], "Tablero", "board", {"visible_properties": ["empresa"]})
    crear_fila(client, base["id"], "Acme", empresa="Acme S.L.", fase="lead")

    nota = crear_nota(client, "Fantasma", bloque(base["id"], "NoExiste"))

    texto = exportar(client, nota).text

    # La primera vista: su nombre y sus columnas.
    assert "**Contactos** — vista Tabla (1 fila)" in texto
    assert "| Título | Fase |" in texto
    assert "| Acme | lead |" in texto


def test_la_vista_aplica_sus_filtros_y_su_orden(client):
    base = crear_base(client, "Contactos", SIMPLE)
    crear_vista(
        client,
        base["id"],
        "Ganados",
        config={"filters": [{"key": "fase", "op": "eq", "value": "ganado"}]},
    )
    crear_fila(client, base["id"], "Beta", empresa="Beta", fase="ganado")
    crear_fila(client, base["id"], "Acme", empresa="Acme", fase="lead")
    crear_fila(client, base["id"], "Gamma", empresa="Gamma", fase="ganado")

    nota = crear_nota(client, "Ganados", bloque(base["id"], "Ganados"))

    texto = exportar(client, nota).text

    assert "**Contactos** — vista Ganados (2 filas)" in texto
    assert "| Beta | Beta | ganado |" in texto
    assert "| Gamma | Gamma | ganado |" in texto
    assert "| Acme |" not in texto


# --------------------------------------------------------------------------
# 3. base inexistente o id inválido: la valla intacta más el aviso
# --------------------------------------------------------------------------

def test_base_inexistente_deja_la_valla_y_avisa(client):
    perdida = uuid.uuid4()
    valla = bloque(perdida, "Tablero", title="Contactos")
    nota = crear_nota(client, "Rota", f"Antes\n\n{valla}\n\nDespués")

    texto = exportar(client, nota).text

    assert valla in texto
    assert f"> ⚠️ Base no encontrada: Contactos" in texto
    assert "| Título |" not in texto


def test_id_invalido_deja_la_valla_y_avisa(client):
    valla = bloque("no-es-un-uuid")
    nota = crear_nota(client, "Rota", valla)

    texto = exportar(client, nota).text

    assert valla in texto
    assert "> ⚠️ Base no encontrada: no-es-un-uuid" in texto


# --------------------------------------------------------------------------
# 4. el límite de filas, con pie honesto
# --------------------------------------------------------------------------

def test_recorta_a_export_max_rows_y_lo_dice(client):
    total = 203
    base = crear_base(client, "Muchas", [{"key": "n", "name": "N", "type": "number"}])
    crear_vista(client, base["id"], "Tabla")
    for numero in range(total):
        crear_fila(client, base["id"], f"Fila {numero:03d}", n=numero)

    nota = crear_nota(client, "Larga", bloque(base["id"], "Tabla"))

    texto = exportar(client, nota).text

    assert settings.export_max_rows == 200
    assert f"**Muchas** — vista Tabla ({total} filas)" in texto
    assert len(filas_de_tabla(texto)) == 200
    assert "_Mostrando 200 de 203 filas._" in texto
    # La última que cabe, y ni una más.
    assert "| Fila 199 | 199 |" in texto
    assert "| Fila 200 |" not in texto


def test_el_limite_es_configurable(client, monkeypatch):
    monkeypatch.setattr(settings, "export_max_rows", 2)
    base = crear_base(client, "Corta", SIMPLE)
    for nombre in ("A", "B", "C"):
        crear_fila(client, base["id"], nombre)

    nota = crear_nota(client, "Corta", bloque(base["id"]))

    texto = exportar(client, nota).text

    assert "**Corta** (3 filas)" in texto
    assert len(filas_de_tabla(texto)) == 2
    assert "_Mostrando 2 de 3 filas._" in texto


def test_sin_recorte_no_hay_pie(client):
    base = crear_base(client, "Corta", SIMPLE)
    crear_fila(client, base["id"], "A")

    texto = exportar(client, crear_nota(client, "Corta", bloque(base["id"]))).text

    assert "_Mostrando" not in texto


# --------------------------------------------------------------------------
# 5. celdas: escapes, fechas, vacíos, títulos y tipos
# --------------------------------------------------------------------------

def test_celdas_con_pipe_y_salto_de_linea_no_rompen_la_tabla(client):
    base = crear_base(client, "Raras", [{"key": "nota", "name": "Nota", "type": "text"}])
    crear_fila(client, base["id"], "Acme", nota="Acme | S.L.\nsegunda línea")
    crear_fila(client, base["id"], "Beta")

    texto = exportar(client, crear_nota(client, "Raras", bloque(base["id"]))).text

    assert "| Acme | Acme \\| S.L.<br>segunda línea |" in texto
    # La fila sin propiedades es solo título: las demás celdas, vacías.
    assert "| Beta |  |" in texto


def test_fila_sin_propiedades_es_solo_el_titulo(client):
    base = crear_base(client, "Vacías", CRM)
    crear_fila(client, base["id"], "Solo título")

    texto = exportar(client, crear_nota(client, "Vacías", bloque(base["id"]))).text

    assert "| Título | Empresa | Fase | Próximo |" in texto
    assert "| Solo título |  |  |  |" in texto


def test_fechas_en_iso_corto_y_tipos_como_el_panel(client):
    schema = [
        {"key": "proximo", "name": "Próximo", "type": "date"},
        {"key": "valor", "name": "Valor", "type": "number"},
        {"key": "hecho", "name": "Hecho", "type": "checkbox"},
        {"key": "etiquetas", "name": "Etiquetas", "type": "multiselect", "options": ["a", "b"]},
    ]
    base = crear_base(client, "Tipos", schema)
    crear_fila(
        client,
        base["id"],
        "Acme",
        proximo="2026-11-12T00:00:00+00:00",
        valor=2500.0,
        hecho=True,
        etiquetas=["a", "b"],
    )
    crear_fila(client, base["id"], "Beta", hecho=False)

    texto = exportar(client, crear_nota(client, "Tipos", bloque(base["id"]))).text

    assert "| Acme | 2026-11-12 | 2500 | ✓ | a, b |" in texto
    # Un checkbox sin marcar no se disfraza de celda vacía; el resto, sí.
    assert "| Beta |  |  | — |  |" in texto


def test_el_titulo_de_la_fila_es_siempre_la_primera_columna(client):
    base = crear_base(client, "Contactos", SIMPLE)
    crear_vista(client, base["id"], "Solo fase", config={"visible_properties": ["fase"]})
    crear_fila(client, base["id"], "Acme", empresa="Acme S.L.", fase="lead")

    texto = exportar(client, crear_nota(client, "Solo fase", bloque(base["id"], "Solo fase"))).text

    assert "| Título | Fase |" in texto
    assert "| Acme | lead |" in texto
    # La columna oculta no aparece, pero el título de la fila sí.
    assert "Empresa" not in texto


# --------------------------------------------------------------------------
# 6. lo que no es un bloque de base no se toca
# --------------------------------------------------------------------------

def test_una_valla_normal_con_un_ejemplo_dentro_no_se_materializa(client):
    cuerpo = "```python\nprint('```mdm-database')\nid: 7a3f\n```"
    nota = crear_nota(client, "Ejemplo", cuerpo)

    texto = exportar(client, nota).text

    assert texto.endswith(cuerpo)
    assert "Base no encontrada" not in texto


def test_un_bloque_de_base_dentro_de_un_ejemplo_de_codigo_es_texto(client):
    """Una nota que documenta el formato (` ```python ` con un bloque dentro)
    exporta su ejemplo tal cual: mirar dentro de una valla normal metería una
    tabla —o un aviso— en mitad de un snippet."""
    cuerpo = (
        "```python\n"
        "```mdm-database\n"
        "id: 7a3f9c21-4b0e-4c8a-9f31-2d5e6b7a8c90\n"
        "```\n"
        "```"
    )
    nota = crear_nota(client, "Formato", cuerpo)

    texto = exportar(client, nota).text

    assert texto.endswith(cuerpo)
    assert "Base no encontrada" not in texto


def test_una_valla_de_base_sin_cerrar_se_deja_como_esta(client):
    cuerpo = "```mdm-database\nid: 7a3f9c21-4b0e-4c8a-9f31-2d5e6b7a8c90"
    nota = crear_nota(client, "Abierta", cuerpo)

    texto = exportar(client, nota).text

    assert texto.endswith(cuerpo)
    assert "Base no encontrada" not in texto
