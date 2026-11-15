"""Un `.md` fabricado no puede reventar el parser (ni comerse una vista a medias).

Dos hallazgos de la revisión de los flow maps (`t_ccaecc5e`), los dos del mismo
subconjunto y con el mismo síntoma: el fichero se lee **mal** y el dueño no se
entera.

1. **Anidamiento sin tope.** `_flow_map` → `_flow_item` → `_parse_inline` →
   `_flow_map`… la profundidad del anidamiento era la de la pila. Con 340
   niveles de `{a: {a: …}}` dentro del bloque de una vista salía un
   `RecursionError` que nadie capturaba (`parse` atrapa `FrontmatterError` e
   `IndexError`): `POST /reindex` devolvía 500 y, con ese `.md` en el vault al
   arrancar, **el índice no se construía** (`/health` en `error`, todas las
   bases a cero hasta editar el fichero a mano). Ahora hay un tope explícito
   (`fm.MAX_FLOW_DEPTH`) y, al pasarse, la entrada queda `opaque` con aviso: el
   `.md` no se toca, la base se queda sin vistas —lo mismo que con un
   `widths: |`, que ya se aceptó así— y `/reindex` dice el fichero y la línea.
   El tope de bloque (`fm.MAX_BLOCK_DEPTH`) cubre el mismo agujero por
   indentación, que también existía antes de esta rama.

2. **Un apóstrofo se comía el resto del mapa.** `_flow_split` contaba cualquier
   `'` como apertura de escalar entrecomillado, así que
   `filters: {texto: O'Brien, fase: lead}` se leía como un solo par y `fase`
   desaparecía **sin aviso** (en una lista, `[it's, fine]` era un elemento).
   Una comilla ahora solo abre escalar donde la admitiría YAML, y una que abre
   y no cierra se avisa en vez de devolver el resto del mapa como un valor.

El módulo pone `SIN_ESPERA_DE_ARRANQUE = True` porque el test del arranque
quiere mirar `/health` él mismo, como un operador.
"""

from __future__ import annotations

import time

from conftest import iniciar_sesion

from app.vault import fm

SIN_ESPERA_DE_ARRANQUE = True

BASE = {
    "title": "Proyectos",
    "schema_def": [
        {"key": "empresa", "name": "Empresa", "type": "text"},
        {"key": "valor", "name": "Valor", "type": "number"},
        {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
    ],
}


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------


def anidado(niveles: int) -> str:
    """`{a: {a: …1…}}` con `niveles` mapas."""
    salida = "1"
    for _ in range(niveles):
        salida = "{a: " + salida + "}"
    return salida


def vista_con(clave: str, valor: str) -> str:
    """El `.md` mínimo de una vista con una clave puesta a mano."""
    return f"views:\n  - name: Otra\n    {clave}: {valor}\n"


def entrada(datos, clave: str = "views"):
    return next(e for e in datos.entries if e.key == clave)


def base_con_vistas(client) -> dict:
    """La base sana del repro: «Tabla» y «Otra», cada una con su ancho."""
    base = client.post("/databases", json=BASE).json()
    for nombre, widths in (
        ("Tabla", {"fase": 144}),
        ("Otra", {"fase": 220, "valor": 150}),
    ):
        respuesta = client.post(
            f"/databases/{base['id']}/views",
            json={"name": nombre, "type": "table", "config": {"widths": widths}},
        )
        assert respuesta.status_code == 200, respuesta.text
    return base


def pasar_bloque_de_widths(fichero, widths: dict[str, int], nuevo: str) -> tuple[str, int]:
    """Sustituye el bloque `widths:` que escribió la app por `nuevo`.

    Devuelve (texto_nuevo, línea 1-based de la primera línea del bloque).
    """
    texto = fichero.read_text(encoding="utf-8")
    bloque = "    widths:\n" + "".join(f"      {k}: {v}\n" for k, v in widths.items())
    assert bloque in texto, texto
    # Línea 1-based del `widths:` que se sustituye (el fichero no tiene otro
    # bloque igual: cada vista baja el suyo).
    linea = texto[: texto.index(bloque)].count("\n") + 1
    resultado = texto.replace(bloque, nuevo, 1)
    fichero.write_text(resultado, encoding="utf-8")
    return resultado, linea


def _esperar_health(client, timeout: float = 60.0) -> dict:
    """Espera a que el arranque deje de estar `starting` y devuelve `/health`."""
    limite = time.monotonic() + timeout
    ultimo: dict = {}
    while time.monotonic() < limite:
        respuesta = client.get("/health")
        ultimo = respuesta.json()
        if ultimo.get("status") != "starting":
            return {"http": respuesta.status_code, **ultimo}
        time.sleep(0.02)
    raise AssertionError(f"/health no salió de 'starting' en {timeout} s: {ultimo}")


# --------------------------------------------------------------------------
# 1. tope de anidamiento: el parser no revienta
# --------------------------------------------------------------------------


def test_un_flow_hasta_el_tope_se_lee_entero():
    datos = fm.parse(vista_con("widths", anidado(fm.MAX_FLOW_DEPTH)))
    assert "views" in datos.data, "el anidado dentro del tope tiene que leerse"
    valor = datos.data["views"][0]["widths"]
    for _ in range(fm.MAX_FLOW_DEPTH):
        assert isinstance(valor, dict), valor
        valor = valor["a"]
    assert valor == "1"


def test_pasarse_del_tope_deja_la_entrada_opaca_con_la_linea_del_fallo():
    datos = fm.parse(vista_con("widths", anidado(fm.MAX_FLOW_DEPTH + 1)))
    assert "views" not in datos.data
    fallo = entrada(datos)
    assert fallo.opaque is True
    assert "niveles" in fallo.error and str(fm.MAX_FLOW_DEPTH) in fallo.error, fallo.error
    # La línea del `widths`, no la de `views`: es la que hay que ir a mirar.
    assert fallo.error_line == 2, fallo.error_line


def test_un_flow_de_400_niveles_no_revienta_y_el_escritor_conserva_el_mapa():
    """Con 400 niveles el parser no lanza `RecursionError` y el `.md` se preserva."""
    raw = "title: Proyectos\n" + vista_con("widths", anidado(400))
    datos = fm.parse(raw)          # no debe lanzar: lo que se comprueba es que vuelve
    assert "views" not in datos.data
    assert entrada(datos).opaque is True

    nuevo, avisos = fm.update(raw, {"icon": "📄"})
    assert avisos == []
    assert "    widths: " + anidado(400) in nuevo, "el escritor ha tocado el flow"


def test_dos_mil_claves_encadenadas_tampoco_revientan():
    """El mismo agujero por indentación (`_parse_node`), sin flow de por medio."""
    lineas = ["views:"] + ["  " * n + f"k{n}:" for n in range(1, 2001)]
    datos = fm.parse("\n".join(lineas) + "\n")
    fallo = entrada(datos)
    assert fallo.opaque is True
    assert str(fm.MAX_BLOCK_DEPTH) in fallo.error, fallo.error


# --------------------------------------------------------------------------
# 2. comillas: un apóstrofo no puede comerse el mapa
# --------------------------------------------------------------------------


def test_un_apostrofo_dentro_de_un_valor_sin_comillas_no_se_come_el_resto():
    vista = fm.parse(vista_con("filters", "{texto: O'Brien, fase: lead}")).data["views"][0]
    assert vista["filters"] == {"texto": "O'Brien", "fase": "lead"}


def test_un_apostrofo_en_una_lista_flow_no_junta_los_elementos():
    vista = fm.parse(vista_con("etiquetas", "[it's, fine, 'a, b']")).data["views"][0]
    assert vista["etiquetas"] == ["it's", "fine", "a, b"]


def test_una_comilla_que_abre_el_valor_sigue_protegiendo_las_comas():
    """La regla nueva no puede romper lo que ya funcionaba: escalar entrecomillado."""
    vista = fm.parse(
        "views:\n  - name: Otra\n"
        "    width: {\"a\": \"x, y\", b: 'it''s, ok'}\n"
    ).data["views"][0]
    assert vista["width"] == {"a": "x, y", "b": "it's, ok"}


def test_una_comilla_que_abre_y_no_cierra_no_se_come_el_mapa_en_silencio():
    """Sin cierre no se sabe dónde acaba el valor: se avisa en vez de adivinar."""
    raw = vista_con("filters", "{texto: 'sin cerrar, fase: lead}")
    datos = fm.parse(raw)
    assert "views" not in datos.data
    fallo = entrada(datos)
    assert fallo.opaque is True
    assert "sin cerrar" in fallo.error, fallo.error
    assert fallo.error_line == 2, fallo.error_line


# --------------------------------------------------------------------------
# 3. de punta a punta: `/reindex`, `/health` y el índice
# --------------------------------------------------------------------------


def test_reindex_con_un_widths_de_400_niveles_no_devuelve_500(client, vault_path):
    """El caso medido: 500 en `/reindex` y el índice a cero. Ahora 200 con aviso."""
    base = base_con_vistas(client)
    fichero = vault_path / "Bases" / "Proyectos.md"
    nuevo, linea = pasar_bloque_de_widths(
        fichero, {"fase": 220, "valor": 150}, "    widths: " + anidado(400) + "\n"
    )

    respuesta = client.post("/reindex")
    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["warnings"] >= 1, cuerpo
    aviso = next(a for a in cuerpo["warning_sample"] if "views" in a)
    assert "Bases/Proyectos.md" in aviso, aviso
    assert f"línea {linea}" in aviso, aviso
    assert "niveles" in aviso, aviso

    # El trato es el mismo que con un `widths: |`: la clave no se interpreta,
    # la base se queda sin vistas en el índice y el fichero no se reescribe.
    assert client.get(f"/databases/{base['id']}/views").json() == []
    assert fichero.read_text(encoding="utf-8") == nuevo
    # Y el resto del índice sigue vivo: el reindexado no se ha caído a medias.
    assert client.get("/databases").status_code == 200


def test_el_arranque_con_un_flow_de_400_niveles_construye_el_indice(
    client, vault_path, tmp_path, monkeypatch
):
    """Con ese `.md` en el vault, el índice de arranque se construye igual."""
    base = base_con_vistas(client)
    fichero = vault_path / "Bases" / "Proyectos.md"
    pasar_bloque_de_widths(
        fichero, {"fase": 220, "valor": 150}, "    widths: " + anidado(400) + "\n"
    )

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "index_path", str(tmp_path / "indice-arranque.sqlite"))
    with TestClient(app) as arranque:
        salud = _esperar_health(arranque)
        assert salud["status"] == "ok", salud
        iniciar_sesion(arranque)
        respuesta = arranque.get("/databases")
        assert respuesta.status_code == 200, respuesta.text
        assert [d["id"] for d in respuesta.json()] == [base["id"]]
        # La vista no se lee (el flow no se entiende) pero la base está en el
        # índice y el arranque ha terminado: es el fallo que se estaba midiendo.
        assert arranque.get(f"/databases/{base['id']}/views").json() == []


def test_un_filters_con_apostrofo_a_mano_llega_al_indice(client, vault_path):
    """`{texto: O'Brien, fase: lead}` en una vista: se lee y sobrevive al reindex."""
    base = base_con_vistas(client)
    fichero = vault_path / "Bases" / "Proyectos.md"
    texto, _linea = pasar_bloque_de_widths(
        fichero,
        {"fase": 220, "valor": 150},
        "    widths: {fase: 220, valor: 150}\n"
        "    filters: {texto: O'Brien, fase: lead}\n",
    )

    cuerpo = client.post("/reindex").json()
    assert cuerpo["warnings"] == 0, cuerpo.get("warning_sample")
    assert cuerpo["views"] == 2, cuerpo

    vistas = {
        v["name"]: v for v in client.get(f"/databases/{base['id']}/views").json()
    }
    assert vistas["Otra"]["config"]["filters"] == {"texto": "O'Brien", "fase": "lead"}
    assert vistas["Otra"]["config"]["widths"] == {"fase": 220, "valor": 150}
    assert vistas["Tabla"]["config"]["widths"] == {"fase": 144}

    # Reindexar no reescribe el fichero: el mapa sigue donde lo escribió el dueño.
    assert fichero.read_text(encoding="utf-8") == texto
