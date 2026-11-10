"""Frontmatter: un `#` dentro de un valor entrecomillado no es un comentario.

Regresión de la tarjeta «Filas por id»: el título de una fila es texto libre en
`title` del frontmatter, y `dump_scalar` entrecomilla justo los valores que
llevan ` #` (`Pedido #3`). El escritor tomaba el **primer** ` #` de la línea como
comentario de cola aunque estuviera dentro de las comillas, así que renombrar la
fila dejaba `title: "Pedido #4" #3"` y el trozo perdido se acumulaba en cada
`PATCH`. El lector tenía el mismo defecto, y un comentario de verdad tras un
valor entrecomillado (`title: "Acme" # nota`) se leía con las comillas puestas.
"""

from __future__ import annotations

import pytest

from app.vault import fm

# Valores que un usuario escribe de verdad en un título o una propiedad, más los
# que rompen un escritor de YAML descuidado. La ida y vuelta tiene que ser exacta.
VALORES = [
    "Pedido #3",
    'Acme "S.A." #1',
    "O'Neill #2",
    "Reunión: Acme #4",
    "  márgenes  #5  ",
    "a # b # c",
    "#empieza por almohadilla",
    "100% #7",
    "dos \" y ' mezcladas #8",
    "titulo: con dos puntos",
    "termina en dos puntos:",
    "- empieza por guion",
    "true",
    "3M",
    "sin nada especial",
    "",
]


@pytest.mark.parametrize("valor", VALORES)
def test_lo_que_se_escribe_se_lee_exactamente_igual(valor):
    """`dump_value` + `parse` devuelven el mismo valor: sin recortes ni comillas."""
    linea = fm.dump_value("title", valor)[0]
    assert fm.parse(linea).data["title"] == valor


@pytest.mark.parametrize("valor", VALORES)
def test_actualizar_y_releer_devuelve_lo_escrito(valor):
    """Actualizar una clave y releerla no puede inventarse ni perder nada."""
    raw, avisos = fm.update("title: viejo\n", {"title": valor})
    assert avisos == []
    assert fm.parse(raw).data["title"] == valor


# --------------------------------------------------------------------------
# el `#` según dónde esté
# --------------------------------------------------------------------------

def test_almohadilla_dentro_de_comillas_dobles_es_parte_del_valor():
    raw = 'title: "Pedido #3"\n'
    assert fm.parse(raw).data["title"] == "Pedido #3"

    nuevo, avisos = fm.update(raw, {"title": "Pedido #4"})
    assert avisos == []
    assert nuevo.strip() == 'title: "Pedido #4"'
    assert fm.parse(nuevo).data["title"] == "Pedido #4"


def test_almohadilla_dentro_de_comillas_simples_es_parte_del_valor():
    raw = "title: 'Pedido #3'\n"
    assert fm.parse(raw).data["title"] == "Pedido #3"

    nuevo, _ = fm.update(raw, {"title": "Pedido #4"})
    assert nuevo.strip() == 'title: "Pedido #4"', nuevo
    assert fm.parse(nuevo).data["title"] == "Pedido #4"
    assert "#3" not in nuevo


def test_comillas_escapadas_dentro_del_valor():
    raw = 'empresa: "Acme \\"S.A.\\" #1"\n'
    assert fm.parse(raw).data["empresa"] == 'Acme "S.A." #1'

    nuevo, _ = fm.update(raw, {"empresa": "Otra"})
    assert nuevo.strip() == "empresa: Otra"
    assert fm.parse(nuevo).data["empresa"] == "Otra"


def test_comentario_de_verdad_tras_un_valor_sin_comillas_se_conserva():
    raw, avisos = fm.update("fase: lead # comentario real", {"fase": "ganado"})
    assert avisos == []
    assert raw == "fase: ganado # comentario real"
    assert fm.parse(raw).data["fase"] == "ganado"


def test_comentario_de_verdad_tras_un_valor_entrecomillado_se_conserva():
    raw = 'title: "Acme" # nota\n'
    # Y antes de reescribir, el valor se lee sin las comillas ni el comentario.
    assert fm.parse(raw).data["title"] == "Acme"

    nuevo, _ = fm.update(raw, {"title": "Otra"})
    assert nuevo.strip() == "title: Otra # nota"
    assert fm.parse(nuevo).data["title"] == "Otra"


def test_una_almohadilla_pegada_al_valor_no_es_comentario():
    assert fm.parse("referencia: A#12\n").data["referencia"] == "A#12"


# --------------------------------------------------------------------------
# el fallo que abrió esta tarjeta: reescribir tres veces no acumula basura
# --------------------------------------------------------------------------

def test_reescribir_un_titulo_con_almohadilla_tres_veces_no_acumula():
    raw = 'title: "Pedido #3"\n'
    for titulo in ("Pedido #4", "Pedido #5", "Pedido #6"):
        raw, avisos = fm.update(raw, {"title": titulo})
        assert avisos == []
        assert raw.strip() == f'title: "{titulo}"', raw
        assert raw.count("#") == 1, raw
        assert fm.parse(raw).data["title"] == titulo
