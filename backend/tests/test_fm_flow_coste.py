"""El coste de leer un flow no puede crecer con el cuadrado de sus comillas.

Hallazgo de la revisión de `t_f2e7f6f5`: el helper `_abre_escalar` —el que
decide si una comilla abre un escalar entrecomillado— reconstruía el trozo
entero (`"".join(buf).strip()`) **por cada comilla**. Como `buf` va acumulando
el trozo, el coste era O(comillas × longitud del trozo): la entrada de abajo con
40.000 comillas tardaba ~9 s en `develop 1da687b` (~31 s aquí, que es la misma
máquina más lenta), y el reindex —que recorre todos los `.md`— se iba con él. Un
`.md` fabricado no puede degradar el índice, igual que no puede tumbarlo con la
profundidad del anidamiento (`test_fm_flow_robustez.py`).

El arreglo no cambia el comportamiento: el último carácter no-espacio del trozo
lo lleva `_flow_split` al vuelo y consultarlo es O(1). Los tests de
comportamiento de `test_fm_flow_robustez.py` pasan tal cual; aquí se mide el
coste con umbrales holgados, porque un umbral ajustado convierte cualquier
máquina cargada en un fallo falso.
"""

from __future__ import annotations

import time

from app.vault import fm

# El `widths` del informe de la tarjeta: N parejas `"x'` dentro de un flow. Con
# N par las comillas se cierran entre sí y el mapa se lee entero; con N impar el
# escalar queda abierto y la entrada sería `opaque` (caso ya cubierto en
# `test_fm_flow_robustez.py`).
COMILLAS = 40_000

# El O(n) arreglado tarda ~25 ms aquí y el O(n²) de antes ~31 s: 500 ms separa
# los dos mundos sin depender de la velocidad de la máquina.
TOPE_MS = 500


def flow_con(n: int) -> str:
    """El `.md` mínimo de una vista con `n` comillas dentro de un flow."""
    return "views:\n  - name: A\n    widths: {a: " + "\"x'" * n + ", b: 2}\n"


def medir(raw: str, repes: int = 3) -> float:
    """Segundos del mejor `fm.parse` de `repes` pasadas (el mínimo quita ruido)."""
    fm.parse(raw)                     # calienta el import y las regex
    mejor = float("inf")
    for _ in range(repes):
        t0 = time.perf_counter()
        fm.parse(raw)
        mejor = min(mejor, time.perf_counter() - t0)
    return mejor


def test_el_flow_con_40_mil_comillas_se_lee_entero_y_en_menos_de_medio_segundo():
    """El caso medido: antes ~9 s, ahora milisegundos, y sin cambiar lo que lee."""
    datos = fm.parse(flow_con(COMILLAS))

    entrada = next(e for e in datos.entries if e.key == "views")
    assert entrada.opaque is False, entrada.error
    vista = datos.data["views"][0]
    assert vista["name"] == "A"
    # El escalar abre con `"`, y el `'` de dentro es texto: se conserva tal cual
    # y no se lleva por delante la coma que separa `b`.
    assert vista["widths"] == {"a": "\"x'" * COMILLAS, "b": "2"}

    ms = medir(flow_con(COMILLAS)) * 1000
    assert ms < TOPE_MS, f"40.000 comillas en un flow: {ms:.0f} ms (tope {TOPE_MS} ms)"


def test_el_coste_crece_con_la_longitud_y_no_con_el_cuadrado():
    """4x de entrada ~4x de tiempo: el O(n²) de antes daba ~16x."""
    corto = medir(flow_con(COMILLAS // 4), repes=5)
    largo = medir(flow_con(COMILLAS))
    assert largo < corto * 10, (
        f"cuatro veces más comillas han costado {largo / corto:.1f}x "
        f"({corto * 1000:.1f} ms -> {largo * 1000:.1f} ms): vuelve a haber "
        "un coste cuadrático en el flow"
    )
