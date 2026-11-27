"""Genera los 16 avatares de Notemil (`frontend/avatars/personaNN.svg`).

Cada uno es una forma de color (círculo, squircle, cuadrado o gota) sobre un
fondo, con una cara mínima (ojos y boca). Sin dependencias; escribe SVG de
64x64. Los nombres `persona01`…`persona16` no cambian: son los que guarda cada
usuario (`auth.AVATARS`).

    python3 tools/avatares.py frontend/avatars
"""
import sys, os

OUT = sys.argv[1]
TINTA = "#1d2130"
# (fondo, forma, tinta de la cara)
COLORES = [
    ("#6c6ff5", "#ffd166", TINTA),
    ("#2ec4b6", "#16324f", "#ffffff"),
    ("#ff8fab", "#ffe5ec", TINTA),
    ("#f4a261", "#264653", "#ffffff"),
    ("#9b8cff", "#e9e6ff", TINTA),
    ("#06d6a0", "#073b4c", "#ffffff"),
    ("#ffbe0b", "#fb5607", "#ffffff"),
    ("#3a86ff", "#caf0f8", TINTA),
    ("#ef476f", "#ffd6a5", TINTA),
    ("#8ac926", "#1b4332", "#ffffff"),
    ("#c77dff", "#3c096c", "#ffffff"),
    ("#ff9f1c", "#fff3e0", TINTA),
    ("#118ab2", "#ffd166", TINTA),
    ("#e76f51", "#2a1a1f", "#ffffff"),
    ("#4cc9f0", "#3a0ca3", "#ffffff"),
    ("#adb5bd", "#343a40", "#ffffff"),
]
FORMAS = ["circulo", "squircle", "cuadrado", "gota"]
OJOS = ["puntos", "ovalos", "cerrados", "guino"]
BOCAS = ["sonrisa", "abierta", "recta", "o"]

def forma(tipo, color, rot, dx, dy):
    t = f'transform="translate({dx} {dy}) rotate({rot} 32 32)"'
    if tipo == "circulo":
        return f'<circle cx="32" cy="34" r="22" fill="{color}" {t}/>'
    if tipo == "squircle":
        return f'<rect x="10" y="12" width="44" height="44" rx="18" fill="{color}" {t}/>'
    if tipo == "cuadrado":
        return f'<rect x="11" y="13" width="42" height="42" rx="9" fill="{color}" {t}/>'
    # gota: un círculo con una esquina en punta
    return f'<path d="M32 12 H50 a4 4 0 0 1 4 4 V34 A22 22 0 1 1 32 12 Z" fill="{color}" {t}/>'

def ojos(tipo, c, x, y):
    a, b = x - 6, x + 6
    if tipo == "puntos":
        return f'<circle cx="{a}" cy="{y}" r="2.2" fill="{c}"/><circle cx="{b}" cy="{y}" r="2.2" fill="{c}"/>'
    if tipo == "ovalos":
        return f'<rect x="{a-1.6}" y="{y-3.2}" width="3.2" height="6.4" rx="1.6" fill="{c}"/><rect x="{b-1.6}" y="{y-3.2}" width="3.2" height="6.4" rx="1.6" fill="{c}"/>'
    if tipo == "cerrados":
        return (f'<path d="M{a-2.6} {y} q2.6 2.6 5.2 0" fill="none" stroke="{c}" stroke-width="1.9" stroke-linecap="round"/>'
                f'<path d="M{b-2.6} {y} q2.6 2.6 5.2 0" fill="none" stroke="{c}" stroke-width="1.9" stroke-linecap="round"/>')
    return (f'<circle cx="{a}" cy="{y}" r="2.2" fill="{c}"/>'
            f'<path d="M{b-2.6} {y+0.6} h5.2" fill="none" stroke="{c}" stroke-width="1.9" stroke-linecap="round"/>')

def boca(tipo, c, x, y):
    if tipo == "sonrisa":
        return f'<path d="M{x-4.5} {y} q4.5 4 9 0" fill="none" stroke="{c}" stroke-width="1.9" stroke-linecap="round"/>'
    if tipo == "abierta":
        return f'<path d="M{x-4.5} {y-0.6} h9 a4.5 4.5 0 0 1 -9 0 z" fill="{c}"/>'
    if tipo == "recta":
        return f'<path d="M{x-3.5} {y+0.8} h7" fill="none" stroke="{c}" stroke-width="1.9" stroke-linecap="round"/>'
    return f'<path d="M{x-6} {y-1} q6 6.5 12 0" fill="none" stroke="{c}" stroke-width="2.2" stroke-linecap="round"/>'

for i, (fondo, color, tinta) in enumerate(COLORES):
    tipo = FORMAS[i % 4]
    ojo = OJOS[(i // 4 + i) % 4]
    bo = BOCAS[(i * 3 + 1) % 4]
    rot = [-12, 8, 18, -6, 0, 14, -16, 6][i % 8]
    dx = [-3, 4, 2, -5, 3, -2, 5, -4][(i * 5) % 8]
    dy = [3, -2, 4, 1, -3, 2, 0, 5][(i * 3) % 8]
    cx, cy = 32 + dx * 0.6, 31 + dy * 0.6
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<defs><clipPath id="c"><circle cx="32" cy="32" r="32"/></clipPath></defs>'
        f'<g clip-path="url(#c)"><rect width="64" height="64" fill="{fondo}"/>'
        f'{forma(tipo, color, rot, dx, dy)}'
        f'<g transform="translate({cx} {cy+4}) scale(1.3) translate({-cx} {-(cy+4)})">'
        f'{ojos(ojo, tinta, cx, cy)}{boca(bo, tinta, cx, cy + 8)}</g>'
        '</g></svg>\n'
    )
    with open(os.path.join(OUT, f"persona{i+1:02d}.svg"), "w") as f:
        f.write(svg)
