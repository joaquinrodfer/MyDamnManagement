"""P15: el agente de hitos — brief, prompt, anclas literales e idempotencia.

Lo que se fija aquí, sin red (el cliente de `/agent` y el modelo se inyectan):

1. **Descubrimiento.** Las notas salen del brief (`Agentes/<agente>.md`, o
   `Notas/Agentes/...`) como rutas `.md` o wikilinks; lo que no es una nota no
   se lee.
2. **Prompt.** Lleva el formato fijo de salida, el brief y el cuerpo de cada
   nota, y dice qué notas dejó fuera por tamaño.
3. **Ancla literal.** Un hito cuyo `old_text` no está en la nota **no** se
   propone: se descarta con el motivo. Mejor cero sugerencias que una huérfana.
4. **Idempotencia.** La misma nota con el mismo ancla y el mismo texto nuevo no
   se propone dos veces, sea cual sea el estado de la que ya existe.
5. **Códigos de salida.** 0 bien, 1 configuración, 2 servidor caído, 3 modelo
   roto, 70 petición rechazada por el servidor. Sin trazas de Python.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

RUTA_AGENTE = Path(__file__).resolve().parents[2] / "tools" / "agent" / "hitos.py"


def _cargar():
    espec = importlib.util.spec_from_file_location("mdm_agente_hitos", RUTA_AGENTE)
    modulo = importlib.util.module_from_spec(espec)
    sys.modules["mdm_agente_hitos"] = modulo
    espec.loader.exec_module(modulo)
    return modulo


hitos = _cargar()

BRIEF = """Eres el agente de hitos. Propón hitos concretos.

## Proyectos

- Notas/Proyectos/Web.md
- `Notas/Proyectos/CRM.md`
"""

CUERPO = """# Proyecto Web

Reescribir el panel en v2.

## Hitos

- **Diseño cerrado** — las tres pantallas aprobadas.
"""


# --------------------------------------------------------------------------
# ayudas
# --------------------------------------------------------------------------


def contexto(ruta, cuerpo=CUERPO, titulo="Proyecto Web", sugerencias=None, recortado=False):
    """Un `GET /agent/context` de mentira, con la forma real de P10b."""
    return {
        "page": {"id": f"id-{ruta}", "title": titulo, "path": ruta, "type": "note"},
        "body": cuerpo,
        "body_truncated": recortado,
        "backlinks": [],
        "links": [],
        "suggestions": list(sugerencias or []),
        "brief": None,
    }


class ClienteFalso:
    """El `/agent` de mentira: contextos por ruta y sugerencias en memoria."""

    def __init__(self, contextos, sugerencias=None):
        self.contextos = dict(contextos)
        self.existentes = list(sugerencias or [])
        self.propuestas = []
        self.pedidos = []
        self.fallo_al_proponer = None

    def contexto(self, *, path=None, page_id=None):
        self.pedidos.append(path)
        return self.contextos.get(path)

    def sugerencias(self):
        return list(self.existentes)

    def proponer(self, cuerpo):
        if self.fallo_al_proponer is not None:
            raise self.fallo_al_proponer
        ruta = next(
            (r for r, c in self.contextos.items() if c["page"]["id"] == cuerpo["page_id"]),
            "",
        )
        datos = dict(cuerpo, id=f"sid-{len(self.propuestas) + 1}", status="pending", path=ruta)
        self.propuestas.append(datos)
        self.existentes.append(
            {
                "id": datos["id"],
                "path": ruta,
                "status": "pending",
                "old_text": cuerpo["old_text"],
                "new_text": cuerpo["new_text"],
            }
        )
        return datos


def cliente_con_brief(cuerpo=CUERPO, brief=BRIEF, sugerencias=None):
    return ClienteFalso(
        {
            "Agentes/agente-hitos.md": contexto(
                "Agentes/agente-hitos.md", cuerpo=brief, titulo="agente-hitos"
            ),
            "Notas/Proyectos/Web.md": contexto("Notas/Proyectos/Web.md", sugerencias=sugerencias),
        }
    )


def ejecutar(cliente, modelo=None, argv=(), entorno=None, monkeypatch=None):
    """`main` con salida capturada y el entorno de la tarjeta."""
    monkeypatch.setenv("MDM_AGENT_TOKEN", "tok-de-hitos")
    monkeypatch.setenv("MDM_AGENTE", "agente-hitos")
    for clave, valor in (entorno or {}).items():
        monkeypatch.setenv(clave, valor)
    salida, errores = _Texto(), _Texto()
    codigo = hitos.main(
        list(argv),
        modelo=modelo or (lambda prompt: json.dumps(RESPUESTA)),
        cliente=cliente,
        salida=salida,
        errores=errores,
    )
    return codigo, salida.valor, errores.valor


class _Texto:
    """Un `StringIO` con `.valor`, para no pasear el objeto por los asserts."""

    def __init__(self):
        self.partes = []

    def write(self, texto):
        self.partes.append(texto)

    @property
    def valor(self):
        return "".join(self.partes)


RESPUESTA = [
    {
        "titulo": "Primera versión en pruebas",
        "detalle": "El panel v2 pasa a pruebas internas.",
        "origen_nota": "Notas/Proyectos/Web.md",
        "old_text": "## Hitos",
    },
    {
        "titulo": "Presupuesto aprobado",
        "detalle": "El dueño firma el presupuesto.",
        "origen_nota": "Notas/Proyectos/Web.md",
        "old_text": "## Presupuesto",
    },
]


# --------------------------------------------------------------------------
# 1. descubrimiento: qué notas mira
# --------------------------------------------------------------------------


def test_el_brief_da_las_notas_con_rutas_o_wikilinks():
    assert hitos.notas_del_brief(BRIEF) == [
        "Notas/Proyectos/Web.md",
        "Notas/Proyectos/CRM.md",
    ]


def test_un_wikilink_del_brief_apunta_a_notas_y_no_se_repite():
    assert hitos.notas_del_brief("Mira [[Proyecto Web]] y [[Proyecto Web|el otro]].") == [
        "Notas/Proyecto Web.md"
    ]


def test_las_instrucciones_del_brief_no_son_notas():
    texto = "# Instrucciones\n\nSé breve, no inventes fechas.\n\n> una cita.md\n"
    assert hitos.notas_del_brief(texto) == []


def test_sin_brief_ni_notas_es_configuracion(monkeypatch):
    cliente = ClienteFalso({})
    codigo, salida, errores = ejecutar(cliente, argv=["--dry-run"], monkeypatch=monkeypatch)
    assert codigo == 1
    assert "no hay ninguna nota que mirar" in errores
    assert cliente.propuestas == []


# --------------------------------------------------------------------------
# 2. el prompt
# --------------------------------------------------------------------------


def test_el_prompt_lleva_formato_brief_y_cuerpo():
    notas = [hitos.Nota("Notas/Proyectos/Web.md", "id-1", "Proyecto Web", CUERPO)]
    prompt = hitos.construir_prompt("agente-hitos", notas, brief=BRIEF, brief_ruta="Agentes/agente-hitos.md")
    assert '"titulo": "...", "detalle": "...", "origen_nota": "...", "old_text": "..."' in prompt
    assert "## Instrucciones del dueño (Agentes/agente-hitos.md)" in prompt
    assert "Propón hitos concretos." in prompt
    assert "## Nota: Notas/Proyectos/Web.md" in prompt
    assert "## Hitos" in prompt


def test_el_prompt_dice_que_notas_dejo_fuera_por_tamano():
    notas = [
        hitos.Nota("Notas/A.md", "id-a", "A", "x" * 500),
        hitos.Nota("Notas/B.md", "id-b", "B", "y" * 500),
    ]
    prompt = hitos.construir_prompt("agente-hitos", notas, brief=None, max_bytes=700)
    assert "## Notas no incluidas" in prompt
    assert "Notas/B.md" in prompt.split("## Notas no incluidas")[1]


def test_el_prompt_recuerda_que_la_nota_viene_recortada():
    notas = [hitos.Nota("Notas/A.md", "id-a", "A", "cuerpo", recortado=True)]
    prompt = hitos.construir_prompt("agente-hitos", notas)
    assert "cuerpo recortado por el servidor" in prompt


# --------------------------------------------------------------------------
# 3. la respuesta del modelo
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texto",
    [
        json.dumps(RESPUESTA),
        json.dumps({"hitos": RESPUESTA}),
        "```json\n" + json.dumps(RESPUESTA) + "\n```",
        "Claro, aquí van:\n" + json.dumps(RESPUESTA) + "\nEspero que sirvan.",
    ],
)
def test_la_lista_de_hitos_se_lee_aunque_venga_adornada(texto):
    assert len(hitos._hitos_de_json(texto)) == 2


def test_un_texto_sin_json_es_fallo_del_modelo():
    with pytest.raises(hitos.ErrorModelo):
        hitos._hitos_de_json("No puedo ayudarte con eso.")


def test_una_lista_vacia_es_una_respuesta_valida():
    assert hitos._hitos_de_json("[]") == []


# --------------------------------------------------------------------------
# 4. ancla literal, topes e idempotencia
# --------------------------------------------------------------------------


def _preparar(hitos_crudos, existentes=None, notas=None, max_hitos=None):
    notas = notas or [hitos.Nota("Notas/Proyectos/Web.md", "id-1", "Proyecto Web", CUERPO)]
    return hitos.preparar(
        hitos_crudos,
        notas,
        existentes or [],
        "agente-hitos",
        max_hitos=max_hitos or hitos.MAX_HITOS,
    )


def test_la_que_ancla_se_propone_y_la_que_no_se_descarta_con_motivo():
    propuestas, descartadas, repetidas = _preparar(RESPUESTA)
    assert len(propuestas) == 1
    assert propuestas[0].old_text == "## Hitos"
    assert propuestas[0].new_text.startswith("## Hitos\n- **Primera versión en pruebas**")
    assert propuestas[0].offset == CUERPO.index("## Hitos")
    assert [d.motivo for d in descartadas] == [
        "el ancla no aparece literalmente en Notas/Proyectos/Web.md"
    ]
    assert repetidas == []


def test_el_ancla_recortada_lo_dice_en_el_motivo():
    nota = hitos.Nota("Notas/Proyectos/Web.md", "id-1", "Proyecto Web", CUERPO, recortado=True)
    _, descartadas, _ = _preparar(RESPUESTA, notas=[nota])
    assert "se leyó recortada" in descartadas[0].motivo


def test_un_origen_que_no_es_una_nota_leida_se_descarta():
    _, descartadas, _ = _preparar(
        [dict(RESPUESTA[0], origen_nota="Notas/Otra.md")]
    )
    assert "no es ninguna de las notas leídas" in descartadas[0].motivo


def test_sin_old_text_no_hay_sugerencia():
    _, descartadas, _ = _preparar([dict(RESPUESTA[0], old_text="")])
    assert "sin `old_text`" in descartadas[0].motivo


def test_un_ancla_de_mas_de_8_kb_se_descarta_antes_de_mandarla():
    cuerpo = "x" * (hitos.MAX_OLD_TEXT + 10)
    nota = hitos.Nota("Notas/Proyectos/Web.md", "id-1", "Proyecto Web", cuerpo)
    _, descartadas, _ = _preparar(
        [dict(RESPUESTA[0], old_text=cuerpo)], notas=[nota]
    )
    assert "supera el tope del servidor" in descartadas[0].motivo


def test_la_misma_sugerencia_pendiente_no_se_repite():
    propuesta = {
        "id": "sha-1",
        "path": "Notas/Proyectos/Web.md",
        "status": "pending",
        "old_text": "## Hitos",
        "new_text": "## Hitos\n- **Primera versión en pruebas** — El panel v2 pasa a pruebas internas.",
    }
    propuestas, _, repetidas = _preparar(RESPUESTA, existentes=[propuesta])
    assert propuestas == []
    assert repetidas[0].estado == "pending"
    assert repetidas[0].suggestion_id == "sha-1"


@pytest.mark.parametrize("estado", ["accepted", "rejected", "orphaned"])
def test_tampoco_se_repite_una_ya_decidida(estado):
    propuesta = {
        "id": "sha-2",
        "path": "Notas/Proyectos/Web.md",
        "status": estado,
        "old_text": "## Hitos",
        "new_text": "## Hitos\n- **Primera versión en pruebas** — El panel v2 pasa a pruebas internas.",
    }
    propuestas, _, repetidas = _preparar(RESPUESTA, existentes=[propuesta])
    assert propuestas == []
    assert repetidas[0].estado == estado


def test_el_mismo_hito_dos_veces_en_una_respuesta_solo_entra_una():
    propuestas, _, repetidas = _preparar([RESPUESTA[0], RESPUESTA[0]])
    assert len(propuestas) == 1
    assert repetidas[0].estado == "repetida en esta ejecución"


def test_el_tope_de_hitos_por_ejecucion():
    crudo = [
        dict(RESPUESTA[0], titulo=f"Hito {i}") for i in range(4)
    ]
    propuestas, descartadas, _ = _preparar(crudo, max_hitos=2)
    assert len(propuestas) == 2
    assert "tope de 2 hitos" in descartadas[0].motivo


# --------------------------------------------------------------------------
# 5. códigos de salida y camino bueno
# --------------------------------------------------------------------------


def test_sin_token_sale_1_y_no_toca_nada(monkeypatch):
    monkeypatch.delenv("MDM_AGENT_TOKEN", raising=False)
    cliente = cliente_con_brief()
    salida, errores = _Texto(), _Texto()
    codigo = hitos.main(["--modelo", "x"], modelo=lambda p: "[]", cliente=cliente,
                        salida=salida, errores=errores)
    assert codigo == 1
    assert "--dry-run (forzado)" in salida.valor
    assert "falta MDM_AGENT_TOKEN" in errores.valor
    assert cliente.pedidos == []
    assert cliente.propuestas == []


def test_servidor_caido_sale_2(monkeypatch):
    class Caido(ClienteFalso):
        def contexto(self, *, path=None, page_id=None):
            raise hitos.ErrorServidor("no se pudo conectar: Connection refused")

    codigo, salida, errores = ejecutar(Caido({}), monkeypatch=monkeypatch)
    assert codigo == 2
    assert "servidor:" in errores


def test_modelo_roto_sale_3(monkeypatch):
    cliente = cliente_con_brief()
    codigo, _, errores = ejecutar(cliente, modelo=lambda p: "no soy JSON", monkeypatch=monkeypatch)
    assert codigo == 3
    assert "modelo:" in errores
    assert cliente.propuestas == []


def test_un_4xx_que_no_es_el_tope_sale_70(monkeypatch):
    cliente = cliente_con_brief()
    cliente.fallo_al_proponer = hitos.ErrorHTTP(400, "old_text no encaja")
    codigo, _, errores = ejecutar(cliente, monkeypatch=monkeypatch)
    assert codigo == 70
    assert "el servidor rechazó la petición" in errores


def test_el_tope_de_pendientes_429_no_es_un_fallo(monkeypatch):
    cliente = cliente_con_brief()
    cliente.fallo_al_proponer = hitos.ErrorHTTP(429, "la nota ya tiene 20 sugerencias")
    codigo, salida, _ = ejecutar(cliente, monkeypatch=monkeypatch)
    assert codigo == 0
    assert "tope de pendientes" in salida


def test_dry_run_imprime_el_prompt_y_no_crea_nada(monkeypatch):
    cliente = cliente_con_brief()
    codigo, salida, _ = ejecutar(cliente, argv=["--dry-run"], monkeypatch=monkeypatch)
    assert codigo == 0
    assert "## Nota: Notas/Proyectos/Web.md" in salida
    assert "sugerencias que crearía: 1" in salida
    assert "'## Hitos'" in salida
    assert "no se crea nada" in salida
    assert cliente.propuestas == []


def test_el_camino_bueno_crea_una_sugerencia_y_sale_0(monkeypatch):
    cliente = cliente_con_brief()
    codigo, salida, _ = ejecutar(cliente, monkeypatch=monkeypatch)
    assert codigo == 0
    assert len(cliente.propuestas) == 1
    cuerpo = cliente.propuestas[0]
    assert cuerpo["kind"] == "edit"
    assert cuerpo["page_id"] == "id-Notas/Proyectos/Web.md"
    assert cuerpo["title"] == "Primera versión en pruebas"
    assert "sugerencias creadas: 1" in salida


def test_dos_ejecuciones_seguidas_no_duplican(monkeypatch):
    cliente = cliente_con_brief()
    primera, _, _ = ejecutar(cliente, monkeypatch=monkeypatch)
    segunda, salida, _ = ejecutar(cliente, monkeypatch=monkeypatch)
    assert (primera, segunda) == (0, 0)
    assert len(cliente.propuestas) == 1
    assert "sugerencias creadas: 0" in salida
    assert "ya propuestas (no se repiten): 1" in salida


def test_las_rechazadas_vuelven_en_el_contexto_y_el_prompt(monkeypatch):
    """El canal de vuelta del ADR-0004: el agente ve el motivo del dueño."""
    cliente = cliente_con_brief(
        sugerencias=[
            {
                "id": "s-9",
                "status": "rejected",
                "title": "Cambiar el orden",
                "reason": "esa sección no se toca",
                "old_text": "## Hitos",
                "new_text": "## Hitos\n- otra cosa",
            }
        ]
    )
    codigo, salida, _ = ejecutar(cliente, argv=["--dry-run"], monkeypatch=monkeypatch)
    assert codigo == 0
    assert "[rejected] Cambiar el orden — rechazada porque: esa sección no se toca" in salida
