"""Un flow map escrito a mano en `views` no puede llevarse las vistas de la base.

Un `widths: { fase: 220, valor: 150 }` —la forma en la que el enunciado de la
tarjeta de columnas escribía el ancho a mano— es YAML correcto, pero el
subconjunto del parser lo marcaba `opaque`: la entrada `views` entera se iba de
`data`, `views_from_frontmatter` recibía `None` y el índice se quedaba **sin
ninguna vista** (la pestaña «Tabla» también) y **sin un solo aviso** en
`/reindex`. El `.md` no se tocaba, pero la base se abría sin pestañas y sin
explicación.

Aquí se fija lo que tiene que pasar:

1. un flow map de escalares dentro de un bloque (los `widths` de una vista, un
   `schema: [{key: …, type: …}]`) se lee como mapa, con los dos sentidos:
   `.md` a mano → índice, y el mapa vacío que baja el escritor → `.md`;
2. en el **primer nivel** un `clave: {…}` sigue siendo `opaque`: es una
   propiedad suelta de una fila y el índice no tiene dónde representarla (el
   guard de B2 se apoya en eso, ver `test_b2_filas_sin_perder.py`);
3. si el bloque `views` sigue sin poder interpretarse (un block scalar dentro),
   `/reindex` lo dice con el fichero y la línea en vez de callarse.
"""

from __future__ import annotations

import re

from app.vault import fm


BASE = {
    "title": "Proyectos",
    "schema_def": [
        {"key": "empresa", "name": "Empresa", "type": "text"},
        {"key": "valor", "name": "Valor", "type": "number"},
        {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
    ],
}


def crear_database(client, **cuerpo) -> dict:
    respuesta = client.post("/databases", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_vista(client, database_id, **cuerpo) -> dict:
    respuesta = client.post(f"/databases/{database_id}/views", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def vistas_de(client, database_id) -> dict:
    respuesta = client.get(f"/databases/{database_id}/views")
    assert respuesta.status_code == 200, respuesta.text
    return {v["name"]: v for v in respuesta.json()}


def base_con_dos_vistas(client) -> tuple[dict, dict]:
    """La base sana del repro: «Tabla» y «Otra», cada una con su ancho."""
    base = crear_database(client, **BASE)
    crear_vista(
        client,
        base["id"],
        name="Tabla",
        type="table",
        config={"widths": {"fase": 144}, "order": ["valor", "fase"]},
    )
    crear_vista(
        client,
        base["id"],
        name="Otra",
        type="table",
        config={"widths": {"fase": 220, "valor": 150}, "order": ["valor", "fase"]},
    )
    vistas = vistas_de(client, base["id"])
    assert set(vistas) == {"Tabla", "Otra"}, vistas
    return base, vistas


def pasar_widths_a_flow_map(fichero, esperado: dict[str, int], flow: str) -> str:
    """Sustituye el bloque `widths:` que escribió la app por un flow map a mano."""
    texto = fichero.read_text(encoding="utf-8")
    for m in re.finditer(r"(?m)^ {4}widths:\n(?:^ {6}[^\n]+\n)+", texto):
        pares = {
            linea.strip().split(": ", 1)[0]: linea.strip().split(": ", 1)[1]
            for linea in m.group(0).splitlines()[1:]
        }
        if pares == {clave: str(v) for clave, v in esperado.items()}:
            break
    else:  # pragma: no cover - el mensaje es el diagnóstico
        raise AssertionError(f"no hay un bloque widths {esperado} en:\n{texto}")
    nuevo = texto[: m.start()] + f"    widths: {flow}\n" + texto[m.end():]
    fichero.write_text(nuevo, encoding="utf-8")
    return nuevo


# --------------------------------------------------------------------------
# 1. el parser: el flow map de escalares se lee donde lo usa el `.md`
# --------------------------------------------------------------------------


def test_un_flow_map_dentro_de_un_bloque_se_lee_como_mapa():
    """`widths: { fase: 220, valor: 150 }` no puede hacer opaca la vista."""
    raw = (
        "id: 11111111-1111-4111-8111-111111111111\n"
        "type: database\n"
        "views:\n"
        "  - id: 3c0d88f8-0e90-4fc3-884c-4d788fb2fdf6\n"
        "    name: Otra\n"
        "    type: table\n"
        "    order: [valor, fase]\n"
        "    widths: { fase: 220, valor: 150 }\n"
    )
    datos = fm.parse(raw)
    assert "views" in datos.data, "la clave ha quedado fuera de `data`"
    vista = datos.data["views"][0]
    assert vista["name"] == "Otra"
    assert vista["order"] == ["valor", "fase"]
    # El parser no infiere tipos: el ancho es el texto `"220"` y el modelo lo
    # convierte (ver `reader._columnas_de_vista`).
    assert vista["widths"] == {"fase": "220", "valor": "150"}

    entrada = next(e for e in datos.entries if e.key == "views")
    assert entrada.opaque is False and entrada.error == ""


def test_un_flow_map_se_lee_aunque_lleve_comillas_comas_y_huecos():
    """Lo que un `.md` a mano tiene: comas dentro de comillas y coma final."""
    raw = (
        "views:\n"
        "  - name: Tabla\n"
        '    widths: { "empresa": "1, 2", valor: 90, }\n'
        "    empty: {}\n"
        "    anidado: {a: {b: 1}}\n"
    )
    vista = fm.parse(raw).data["views"][0]
    assert vista["widths"] == {"empresa": "1, 2", "valor": "90"}
    assert vista["empty"] == {}
    assert vista["anidado"] == {"a": {"b": "1"}}


def test_un_comentario_de_cola_no_rompe_el_flow_map():
    """`{ fase: 220 }  # ancho a mano`: el comentario no es parte del mapa."""
    raw = (
        "views:\n"
        "  - name: Tabla\n"
        "    widths: { fase: 220 }  # el ancho que quiero\n"
        '    otros: { a: "x #y" }\n'
    )
    vista = fm.parse(raw).data["views"][0]
    assert vista["widths"] == {"fase": "220"}
    # El `#` de dentro de las comillas es parte del valor, no un comentario.
    assert vista["otros"] == {"a": "x #y"}


def test_una_lista_de_flow_maps_no_se_parte_por_las_comas_de_dentro():
    """`schema: [{key: …, type: …}]` son dos mapas, no cuatro trozos."""
    raw = "schema: [{key: empresa, type: text}, {key: valor, type: number}]\n"
    assert fm.parse(raw).data["schema"] == [
        {"key": "empresa", "type": "text"},
        {"key": "valor", "type": "number"},
    ]


def test_en_el_primer_nivel_un_flow_map_sigue_siendo_opaco():
    """Una propiedad de fila no puede ser un mapa: se preserva sin interpretar.

    El guard de B2 (`store`) se apoya en `entry.opaque` para no borrar del
    `.md` un valor que el índice no representa; si esto se leyera como mapa, la
    celda se podría sobrescribir desde la app y el mapa se perdería.
    """
    raw = "id: 1\nempresa: {nombre: Acme Corp SL}\nvalor: 2\n"
    datos = fm.parse(raw)
    assert "empresa" not in datos.data
    entrada = next(e for e in datos.entries if e.key == "empresa")
    assert entrada.opaque is True

    # Y el escritor la conserva tal cual (aunque se le pida borrarla).
    nuevo, avisos = fm.update(raw, {"valor": 3})
    assert "empresa: {nombre: Acme Corp SL}" in nuevo
    assert avisos == []


def test_el_mapa_vacio_que_baja_el_escritor_dentro_de_una_vista_se_relee():
    """`{}` dentro de la entrada de una vista ya no es opaco (antes sí)."""
    nuevo, avisos = fm.update(
        "title: Proyectos\n",
        {
            "views": [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "name": "Tabla",
                    "type": "table",
                    "config": {},
                }
            ]
        },
    )
    assert avisos == []
    assert re.search(r"(?m)^    config: \{\}$", nuevo), nuevo
    assert fm.parse(nuevo).data["views"][0]["config"] == {}


# --------------------------------------------------------------------------
# 2. el caso del enunciado, de punta a punta: `.md` a mano → `/reindex` → API
# --------------------------------------------------------------------------


def test_un_widths_a_mano_en_flow_map_no_deja_la_base_sin_vistas(
    client, vault_path
):
    base, antes = base_con_dos_vistas(client)
    fichero = vault_path / "Bases" / "Proyectos.md"

    texto = pasar_widths_a_flow_map(
        fichero, {"fase": 220, "valor": 150}, "{ fase: 220, valor: 150 }"
    )
    assert "widths: { fase: 220, valor: 150 }" in texto

    respuesta = client.post("/reindex")
    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["views"] == 2, cuerpo
    assert cuerpo["warnings"] == 0, cuerpo.get("warning_sample")

    despues = vistas_de(client, base["id"])
    assert set(despues) == {"Tabla", "Otra"}, despues
    # La vista editada a mano conserva lo que dice el fichero, con el ancho ya
    # como número; la otra no se ha movido.
    assert despues["Otra"]["config"]["widths"] == {"fase": 220, "valor": 150}
    assert despues["Otra"]["config"]["order"] == ["valor", "fase"]
    assert despues["Tabla"]["config"]["widths"] == {"fase": 144}
    assert despues["Otra"]["id"] == antes["Otra"]["id"]

    # Y el reindexado no ha reescrito el fichero: el flow map sigue donde el
    # dueño lo escribió.
    assert fichero.read_text(encoding="utf-8") == texto


def test_guardar_una_vista_tras_un_flow_map_vuelve_a_bajar_el_bloque(
    client, vault_path
):
    """La clave sigue siendo gestionada: el flow map no la vuelve intocable.

    Mientras `views` estaba `opaque`, el escritor la preservaba sin tocar y
    guardar un ancho desde la app no podía hacer nada (con aviso). Ahora la
    entrada se interpreta y el `.md` se reescribe con el formato de siempre.
    """
    base, _vistas = base_con_dos_vistas(client)
    fichero = vault_path / "Bases" / "Proyectos.md"
    pasar_widths_a_flow_map(
        fichero, {"fase": 220, "valor": 150}, "{ fase: 220, valor: 150 }"
    )
    assert client.post("/reindex").status_code == 200
    vista = vistas_de(client, base["id"])["Otra"]

    respuesta = client.patch(
        f"/databases/{base['id']}/views/{vista['id']}",
        json={"config": {"widths": {"fase": 240}, "order": ["valor", "fase"]}},
    )
    assert respuesta.status_code == 200, respuesta.text

    texto = fichero.read_text(encoding="utf-8")
    assert "widths: { fase: 220" not in texto, texto
    assert re.search(r"(?m)^ {4}widths:$", texto), texto
    assert re.search(r"(?m)^ {6}fase: 240$", texto), texto

    despues = vistas_de(client, base["id"])
    assert set(despues) == {"Tabla", "Otra"}, despues
    assert despues["Otra"]["config"]["widths"] == {"fase": 240}
    assert despues["Tabla"]["config"]["widths"] == {"fase": 144}


def test_si_el_bloque_de_vistas_no_se_entiende_reindex_avisa_con_la_linea(
    client, vault_path
):
    """Un block scalar dentro de `views` sigue sin poder leerse: pero se avisa.

    El bloque entero sigue siendo opaco (el subconjunto no lo interpreta) y el
    índice se queda sin vistas, pero `/reindex` nombra el fichero, la línea y el
    motivo en vez de devolver `views: 0, warnings: 0` sin explicación.
    """
    base, _vistas = base_con_dos_vistas(client)
    fichero = vault_path / "Bases" / "Proyectos.md"
    texto = fichero.read_text(encoding="utf-8")
    reemplazo = "    widths: |\n      fase: 220\n"
    nuevo = re.sub(r"(?m)^ {4}widths:\n(?:^ {6}[^\n]+\n)+", reemplazo, texto, count=1)
    assert nuevo != texto and "widths: |" in nuevo
    fichero.write_text(nuevo, encoding="utf-8")
    linea = nuevo.splitlines().index("    widths: |") + 1

    respuesta = client.post("/reindex")
    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["warnings"] >= 1, cuerpo
    muestra = cuerpo["warning_sample"]
    aviso = next(a for a in muestra if "views" in a)
    assert "Bases/Proyectos.md" in aviso, aviso
    assert f"línea {linea}" in aviso, aviso
    assert "sin vistas" in aviso, aviso

    # Lo que no puede pasar es que el fichero se reescriba solo.
    assert fichero.read_text(encoding="utf-8") == nuevo
    assert vistas_de(client, base["id"]) == {}
