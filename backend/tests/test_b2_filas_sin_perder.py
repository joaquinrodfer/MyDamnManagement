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
"""

import difflib
from pathlib import Path

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
