"""B2: guardar una celda no toca las demás (ni pierde lo que el índice no lee).

El defecto del informe: una propiedad `number` con texto en el `.md`
(`valor: A-12`) se indexa como `null` —el índice no puede guardar texto en un
campo numérico—. El `PATCH` de una fila se construía mezclando lo que había en
el índice con lo que cambiaba el usuario y reescribía **todas** las propiedades
del fichero, así que `valor: A-12` desaparecía del `.md` al guardar cualquier
otra celda, sin avisar.

Lo que se fija aquí:

1. la reproducción exacta del informe: editar `empresa` con `valor: A-12` en el
   fichero deja `valor: A-12` donde estaba;
2. vaciar desde la app la celda que el índice no entiende avisa (400 con el
   motivo) y no borra nada;
3. dos valores ilegibles a la vez sobreviven igual;
4. editar `empresa` en una fila sana no cambia ninguna otra línea;
5. la invariante: editar cualquier celda de una fila no puede eliminar del
   `.md` ninguna clave que no sea la que se está cambiando.
6. una entrada que el parser no interpreta (`empresa: |`, flow map, ancla) no se
   puede vaciar **ni sobrescribir** desde la app: 400 con el motivo y ni un byte
   escrito, en vez del 200 sin cambio que se tiraba la edición (revisión, ronda 2).
"""

import difflib
from pathlib import Path

import pytest

from app.vault import fm

SCHEMA = [
    {"key": "empresa", "name": "Empresa", "type": "text"},
    {"key": "fase", "name": "Fase", "type": "select", "options": ["lead", "ganado"]},
    {"key": "proximo_contacto", "name": "Próximo contacto", "type": "text"},
    {
        "key": "etiquetas",
        "name": "Etiquetas",
        "type": "multiselect",
        "options": ["rojo", "verde", "azul"],
    },
    {"key": "valor", "name": "Valor", "type": "number"},
    {"key": "importe", "name": "Importe", "type": "number"},
]


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------


def crear_base(client) -> dict:
    respuesta = client.post("/databases", json={"title": "Contactos", "schema_def": SCHEMA})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def crear_fila(client, database_id, titulo="Acme", **properties) -> dict:
    respuesta = client.post(
        f"/databases/{database_id}/rows", json={"title": titulo, "properties": properties}
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def texto(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def datos(contenido: str) -> dict:
    """El frontmatter interpretado, con el parser de la app."""
    raw, _cuerpo = fm.split_document(contenido)
    assert raw is not None, "el .md no tiene frontmatter"
    return fm.parse(raw).data


def claves_tocadas(antes: str, despues: str) -> set[str]:
    """Claves del frontmatter cuyas líneas cambian (añadidas, quitadas o no).

    Es la comprobación literal de la invariante: si `valor: A-12` desaparece,
    aquí sale `valor` aunque el resto del fichero quede igual.
    """
    tocadas: set[str] = set()
    for linea in difflib.unified_diff(
        antes.splitlines(), despues.splitlines(), lineterm="", n=0
    ):
        if linea[:1] not in "+-" or linea[:3] in ("---", "+++"):
            continue
        cuerpo = linea[1:].strip()
        tocadas.add(cuerpo.split(":", 1)[0].strip() or cuerpo)
    return tocadas


def escribir_a_mano(contenido: str, *cambios: tuple[str, str]) -> str:
    """Lo que hace el dueño con su vault: editar el `.md` a mano."""
    for viejo, nuevo in cambios:
        assert viejo in contenido, f"{viejo!r} no está en el fichero: {contenido}"
        contenido = contenido.replace(viejo, nuevo)
    return contenido


def fila_del_informe(client, vault_path: Path) -> tuple[dict, dict, Path]:
    """`Bases/Contactos/Acme.md` con `valor: A-12` dentro, indexado."""
    base = crear_base(client)
    fila = crear_fila(
        client,
        base["id"],
        empresa="Acme Corp",
        fase="ganado",
        proximo_contacto="A-12",
        etiquetas=["rojo", "verde"],
        valor=10,
    )
    fichero = vault_path / "Bases" / "Contactos" / "Acme.md"
    fichero.write_text(
        escribir_a_mano(texto(fichero), ("valor: 10", "valor: A-12")), encoding="utf-8"
    )
    assert "valor: A-12" in texto(fichero)

    assert client.post("/reindex").status_code == 200
    # El índice no puede con `A-12` en un campo numérico: lo deja a `null`, que
    # es lo que enseña la tabla (defecto del informe, paso 2).
    en_indice = client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()
    assert en_indice["properties"]["valor"] is None
    return base, fila, fichero


# --------------------------------------------------------------------------
# 1. la reproducción exacta del informe
# --------------------------------------------------------------------------


def test_editar_empresa_no_borra_un_valor_que_el_indice_no_entiende(
    client, vault_path
):
    base, fila, fichero = fila_del_informe(client, vault_path)
    antes = texto(fichero)

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"empresa": "Acme Corp SL"}},
    )
    assert respuesta.status_code == 200, respuesta.text
    despues = texto(fichero)

    # El dato del dueño sigue en el fichero, con la celda editada al día.
    assert 'empresa: "Acme Corp SL"' in despues
    assert "valor: A-12" in despues, "editar otra celda ha borrado `valor: A-12`"
    assert set(datos(antes)) == set(datos(despues)), "ha desaparecido una clave"
    assert claves_tocadas(antes, despues) == {"empresa", "updated"}

    # Y lo que devuelve la API es lo que dice el fichero: `valor` sigue sin
    # representar (la tabla enseña `—`), pero `empresa` ya es la nueva.
    guardada = client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()
    assert guardada["properties"]["empresa"] == "Acme Corp SL"
    assert guardada["properties"]["valor"] is None
    assert guardada["properties"]["proximo_contacto"] == "A-12"


# --------------------------------------------------------------------------
# 2. la celda ilegible: avisar y no borrar
# --------------------------------------------------------------------------


def test_vaciar_la_celda_que_el_indice_no_entiende_avisa_y_no_borra(client, vault_path):
    base, fila, fichero = fila_del_informe(client, vault_path)
    antes = texto(fichero)

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"valor": None}},
    )
    assert respuesta.status_code == 400, respuesta.text
    detalle = respuesta.json()["detail"]
    assert "valor" in detalle and "A-12" in detalle, detalle
    assert texto(fichero) == antes, "un 400 no puede haber escrito el fichero"

    # La salida buena: escribir un número representable sí se permite (es cómo
    # se arregla la celda desde la app), y solo cambia esa línea.
    arreglada = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"valor": 42}},
    )
    assert arreglada.status_code == 200, arreglada.text
    assert datos(texto(fichero))["valor"] == "42"
    assert arreglada.json()["properties"]["valor"] == 42.0


def test_vaciar_una_celda_legible_sigue_borrando_su_clave(client, vault_path):
    """El 400 es solo para lo que el índice no entiende: vaciar lo demás funciona."""
    base, fila, fichero = fila_del_informe(client, vault_path)

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"proximo_contacto": None}},
    )
    assert respuesta.status_code == 200, respuesta.text
    en_disco = datos(texto(fichero))
    assert "proximo_contacto" not in en_disco
    assert en_disco["valor"] == "A-12", "vaciar una celda no toca las demás"


# --------------------------------------------------------------------------
# 3. dos valores ilegibles a la vez
# --------------------------------------------------------------------------


def test_dos_valores_ilegibles_a_la_vez_se_conservan(client, vault_path):
    base = crear_base(client)
    fila = crear_fila(
        client,
        base["id"],
        empresa="Acme Corp",
        fase="ganado",
        valor=10,
        importe=20,
    )
    fichero = vault_path / "Bases" / "Contactos" / "Acme.md"
    fichero.write_text(
        escribir_a_mano(
            texto(fichero),
            ("valor: 10", "valor: A-12"),
            ("importe: 20", "importe: n/d"),
        ),
        encoding="utf-8",
    )
    assert client.post("/reindex").status_code == 200
    antes = texto(fichero)

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"empresa": "Acme Corp SL"}},
    )
    assert respuesta.status_code == 200, respuesta.text
    despues = texto(fichero)

    assert "valor: A-12" in despues and "importe: n/d" in despues
    assert claves_tocadas(antes, despues) == {"empresa", "updated"}

    # Y ninguno de los dos se puede vaciar a ciegas: cada uno tiene su 400.
    for clave in ("valor", "importe"):
        respuesta = client.patch(
            f"/databases/{base['id']}/rows/{fila['id']}",
            json={"properties": {clave: None}},
        )
        assert respuesta.status_code == 400, f"{clave}: {respuesta.text}"
        assert texto(fichero) == despues


# --------------------------------------------------------------------------
# 4. una fila sin valores raros: nada más cambia
# --------------------------------------------------------------------------


def test_editar_empresa_en_una_fila_sana_no_cambia_nada_mas(client, vault_path):
    base = crear_base(client)
    fila = crear_fila(
        client,
        base["id"],
        empresa="Acme Corp",
        fase="lead",
        proximo_contacto="A-12",
        etiquetas=["rojo", "verde"],
        valor=1200,
    )
    fichero = vault_path / "Bases" / "Contactos" / "Acme.md"
    antes = texto(fichero)

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"empresa": "Acme Corp SL"}},
    )
    assert respuesta.status_code == 200, respuesta.text
    despues = texto(fichero)

    assert claves_tocadas(antes, despues) == {"empresa", "updated"}
    assert datos(despues) == {
        **datos(antes),
        "empresa": "Acme Corp SL",
        "updated": datos(despues)["updated"],
    }
    # El cuerpo (lo que va tras el `---` de cierre) no se toca ni un byte.
    assert antes.split("---\n", 2)[2] == despues.split("---\n", 2)[2]


# --------------------------------------------------------------------------
# 5. la invariante, celda a celda
# --------------------------------------------------------------------------


def test_invariante_editar_cualquier_celda_no_borra_ninguna_clave(client, vault_path):
    """Editar una celda no puede eliminar del `.md` ninguna clave que no sea esa.

    La fila trae dos valores que el índice no entiende (`valor`, `importe`) y
    una clave que la app no conoce (`nota_del_dueno`, escrita a mano): las tres
    tienen que seguir ahí después de editar cualquier otra celda, y el fichero
    solo puede diferir en la celda editada (más el `updated` de siempre).
    """
    base = crear_base(client)
    fila = crear_fila(
        client,
        base["id"],
        empresa="Acme Corp",
        fase="lead",
        proximo_contacto="A-12",
        etiquetas=["rojo", "verde"],
        valor=10,
        importe=20,
    )
    fichero = vault_path / "Bases" / "Contactos" / "Acme.md"
    fichero.write_text(
        escribir_a_mano(
            texto(fichero),
            ("valor: 10", "valor: A-12"),
            ("importe: 20", "importe: n/d"),
        ),
        encoding="utf-8",
    )
    # Una clave que el schema no declara: el índice no la ve, la app no la
    # edita y el fichero la conserva (es del dueño).
    fichero.write_text(
        texto(fichero).replace("fase: lead", "fase: lead\nnota_del_dueno: revisar"),
        encoding="utf-8",
    )
    assert client.post("/reindex").status_code == 200

    # Celdas representables: todas menos las dos ilegibles (escribir un número
    # válido ahí reemplaza el dato a propósito, y eso tiene su propio test).
    nuevas = {
        "empresa": "Acme Corp SL",
        "fase": "ganado",
        "proximo_contacto": "B-7",
        "etiquetas": ["azul"],
    }
    assert set(nuevas) <= {p["key"] for p in SCHEMA}

    for clave, valor in nuevas.items():
        url = f"/databases/{base['id']}/rows/{fila['id']}"
        antes = texto(fichero)
        respuesta = client.patch(url, json={"properties": {clave: valor}})
        assert respuesta.status_code == 200, f"{clave}: {respuesta.text}"
        despues = texto(fichero)

        eliminadas = set(datos(antes)) - set(datos(despues))
        assert eliminadas == set(), f"editar «{clave}» ha borrado {eliminadas} del .md"
        assert claves_tocadas(antes, despues) <= {clave, "updated"}, (
            f"editar «{clave}» ha tocado "
            f"{claves_tocadas(antes, despues) - {clave, 'updated'}}"
        )
        assert "valor: A-12" in despues and "importe: n/d" in despues
        assert despues.count("nota_del_dueno: revisar") == 1


# --------------------------------------------------------------------------
# 6. entradas `opaque`: también están en el fichero (revisión, ronda 1)
# --------------------------------------------------------------------------
#
# `fm.parse().data` omite a propósito las entradas que el parser no entiende
# (block scalars `|`/`>`, flow maps `{}`, anchors/tags). Son exactamente «lo que
# el índice no supo interpretar»: la tabla enseña `—`, y vaciar esa celda desde
# la app no puede llevarse el bloque del `.md`. El guard tiene que mirar las
# `entries` del frontmatter, no solo `data`.

OPACOS = [
    ("block scalar", "empresa: |\n  Acme Corp SL"),
    ("folded", "empresa: >\n  Acme Corp SL"),
    ("flow map", "empresa: {nombre: Acme Corp SL}"),
    ("ancla", "empresa: &acme Acme Corp SL"),
]


def fila_con_empresa_opaca(client, vault_path, reemplazo: str) -> tuple[dict, dict, Path]:
    base = crear_base(client)
    fila = crear_fila(
        client, base["id"], empresa="Acme Corp", fase="lead", valor=10
    )
    fichero = vault_path / "Bases" / "Contactos" / "Acme.md"
    fichero.write_text(
        escribir_a_mano(texto(fichero), ('empresa: "Acme Corp"', reemplazo)),
        encoding="utf-8",
    )
    assert reemplazo in texto(fichero)
    assert client.post("/reindex").status_code == 200
    # El índice no ve la clave (el parser la marca opaca): la tabla enseña `—`.
    en_indice = client.get(f"/databases/{base['id']}/rows/{fila['id']}").json()
    assert en_indice["properties"].get("empresa") is None
    return base, fila, fichero


@pytest.mark.parametrize("etiqueta, reemplazo", OPACOS, ids=[o[0] for o in OPACOS])
def test_vaciar_una_entrada_opaca_avisa_y_no_borra(
    client, vault_path, etiqueta, reemplazo
):
    base, fila, fichero = fila_con_empresa_opaca(client, vault_path, reemplazo)
    antes = texto(fichero)

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"empresa": None}},
    )
    assert respuesta.status_code == 400, f"{etiqueta}: {respuesta.text}"
    detalle = respuesta.json()["detail"]
    # El motivo cita la línea del fichero y no se ha escrito nada.
    assert "empresa" in detalle and "empresa: " in detalle, detalle
    assert texto(fichero) == antes, f"{etiqueta}: un 400 no puede escribir el fichero"


@pytest.mark.parametrize("etiqueta, reemplazo", OPACOS, ids=[o[0] for o in OPACOS])
def test_escribir_un_valor_sobre_una_entrada_opaca_avisa_y_no_borra(
    client, vault_path, etiqueta, reemplazo
):
    """El otro camino de la misma celda: teclear un valor no puede perder el bloque.

    La tabla enseña `—` (el índice no tiene la clave), así que el dueño teclea
    ahí el valor para arreglarla desde la app. Antes eso devolvía **200 sin
    escribir nada**: el valor se tiraba y el bloque se quedaba, los dos a medias
    y sin aviso. Ahora: 400 con el motivo, ni un byte escrito, y el índice igual
    (nunca 200 sin cambio).
    """
    base, fila, fichero = fila_con_empresa_opaca(client, vault_path, reemplazo)
    antes = texto(fichero)

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"empresa": "Nueva SL"}},
    )
    assert respuesta.status_code == 400, f"{etiqueta}: {respuesta.text}"
    detalle = respuesta.json()["detail"]
    assert "empresa: " in detalle and "escribir" in detalle, detalle
    assert texto(fichero) == antes, f"{etiqueta}: el 400 no puede escribir el fichero"
    assert client.get(
        f"/databases/{base['id']}/rows/{fila['id']}"
    ).json()["properties"].get("empresa") is None


def test_el_400_de_una_celda_opaca_no_renombra_la_fila(client, vault_path):
    """El guard va antes de mover: un PATCH mixto no deja el fichero a medias."""
    base, fila, fichero = fila_con_empresa_opaca(client, vault_path, OPACOS[0][1])
    antes = texto(fichero)

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"title": "Otro nombre", "properties": {"empresa": "Nueva SL"}},
    )
    assert respuesta.status_code == 400, respuesta.text
    assert texto(fichero) == antes, "ha escrito el fichero antes de rechazar"
    assert not (vault_path / "Bases" / "Contactos" / "Otro nombre.md").exists()


@pytest.mark.parametrize("etiqueta, reemplazo", OPACOS, ids=[o[0] for o in OPACOS])
def test_editar_otra_celda_conserva_la_entrada_opaca(
    client, vault_path, etiqueta, reemplazo
):
    base, fila, fichero = fila_con_empresa_opaca(client, vault_path, reemplazo)
    antes = texto(fichero)

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"fase": "ganado"}},
    )
    assert respuesta.status_code == 200, f"{etiqueta}: {respuesta.text}"
    despues = texto(fichero)

    assert reemplazo in despues, f"{etiqueta}: editar otra celda ha borrado el bloque"
    assert claves_tocadas(antes, despues) == {"fase", "updated"}, etiqueta


def test_el_motivo_del_400_cita_el_yaml_del_fichero_no_el_interpretado(
    client, vault_path
):
    """El dueño lee el motivo para arreglar su `.md`: tiene que ver su línea."""
    base = crear_base(client)
    fila = crear_fila(client, base["id"], empresa="Acme Corp", valor=10)
    fichero = vault_path / "Bases" / "Contactos" / "Acme.md"
    fichero.write_text(
        escribir_a_mano(texto(fichero), ("valor: 10", "valor: [1, 2]")),
        encoding="utf-8",
    )
    assert client.post("/reindex").status_code == 200
    assert client.get(
        f"/databases/{base['id']}/rows/{fila['id']}"
    ).json()["properties"]["valor"] is None

    respuesta = client.patch(
        f"/databases/{base['id']}/rows/{fila['id']}",
        json={"properties": {"valor": None}},
    )
    assert respuesta.status_code == 400, respuesta.text
    detalle = respuesta.json()["detail"]
    assert "valor: [1, 2]" in detalle, detalle
    assert "['1', '2']" not in detalle, f"cita el YAML interpretado: {detalle}"


def test_fm_update_no_borra_una_entrada_opaca_ni_pidiendolo():
    """El escritor también protege solo: `None`/`delete` no se llevan lo opaco."""
    raw = "id: 1\nempresa: |\n  Acme Corp SL\nvalor: 2"

    nuevo, avisos = fm.update(raw, {"empresa": None})
    assert "empresa: |" in nuevo and "  Acme Corp SL" in nuevo
    assert nuevo == raw
    assert avisos and "empresa" in avisos[0], avisos

    borrada, avisos = fm.update(raw, {}, delete={"empresa"})
    assert borrada == raw
    assert avisos and "empresa" in avisos[0], avisos

    # Y editar otra clave sigue funcionando sin tocar el bloque.
    otro, avisos = fm.update(raw, {"valor": 3})
    assert "empresa: |\n  Acme Corp SL" in otro
    assert "valor: 3" in otro
    assert avisos == []


def test_invariante_editar_cualquier_celda_no_borra_entradas_opacas(client, vault_path):
    """La invariante, con una entrada opaca: solo puede cambiar la celda editada."""
    base = crear_base(client)
    fila = crear_fila(
        client,
        base["id"],
        empresa="Acme Corp",
        fase="lead",
        proximo_contacto="A-12",
        valor=10,
    )
    fichero = vault_path / "Bases" / "Contactos" / "Acme.md"
    fichero.write_text(
        escribir_a_mano(
            texto(fichero),
            ('empresa: "Acme Corp"', "empresa: |\n  Acme Corp SL"),
            ("valor: 10", "valor: A-12"),
        ),
        encoding="utf-8",
    )
    assert client.post("/reindex").status_code == 200

    # La clave opaca entra también como objetivo: editarla no puede ser un 200
    # que no escribe nada, así que se espera el 400 y el fichero igual. Las
    # ilegibles por tipo sí admiten un valor válido a propósito (su propio test).
    casos = {
        "fase": ("ganado", 200),
        "proximo_contacto": ("B-7", 200),
        "valor": (42, 200),
        "empresa": ("Nueva SL", 400),
    }
    for clave, (valor, esperado) in casos.items():
        antes = texto(fichero)
        entrada_antes = {e.key for e in fm.parse(
            fm.split_document(antes)[0] or ""
        ).entries if e.key}
        respuesta = client.patch(
            f"/databases/{base['id']}/rows/{fila['id']}",
            json={"properties": {clave: valor}},
        )
        assert respuesta.status_code == esperado, f"{clave}: {respuesta.text}"
        despues = texto(fichero)
        entrada_despues = {e.key for e in fm.parse(
            fm.split_document(despues)[0] or ""
        ).entries if e.key}

        assert entrada_despues == entrada_antes, (
            f"editar «{clave}» ha cambiado las claves del .md: "
            f"{entrada_antes ^ entrada_despues}"
        )
        if esperado == 400:
            assert despues == antes, (
                f"«{clave}»: el 400 tiene que dejar el fichero byte a byte igual"
            )
        else:
            assert claves_tocadas(antes, despues) <= {clave, "updated"}, (
                f"editar «{clave}» ha tocado "
                f"{claves_tocadas(antes, despues) - {clave, 'updated'}}"
            )
        assert "empresa: |\n  Acme Corp SL" in despues
