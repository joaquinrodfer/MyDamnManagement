r"""Saneado de títulos -> nombres de fichero, y resolución de colisiones.

Regla única, aplicada SIEMPRE antes de tocar el disco:

1. Normalizar a NFC (macOS entrega NFD al copiar de un zip/USB; sin esto el
   mismo título produce dos ficheros distintos).
2. Quitar caracteres de control (0x00-0x1F, 0x7F), incluido NUL: en Linux el
   NUL trunca el nombre en la capa POSIX y `open()` lanza ValueError.
3. Sustituir los caracteres que Windows/macOS no aceptan o que rompen rutas:
   `< > : " / \\ | ? *` -> espacio. También `/` y `\` porque son separadores
   de ruta: sin esto un título `A/B` crea una carpeta en vez de un fichero.
   Y los corchetes `[` `]` -> `(` `)`: el nombre del fichero los admite, pero
   el título ES el nombre del fichero y `[[Notas [2026]]]` se lee truncado en
   el primer `]` (`app/wikilinks.py`), así que esa nota no se puede enlazar.
4. Colapsar espacios repetidos, recortar espacios y puntos al principio/final
   (Windows no permite terminar en punto ni en espacio).
5. Evitar `.` y `..` (rutas, no nombres).
6. Prefijar `_` a los nombres de dispositivo reservados de Windows
   (CON, PRN, AUX, NUL, COM1-9, LPT1-9): en Windows `CON.md` no se puede crear.
7. Truncar a MAX_BYTES bytes UTF-8 sin partir un carácter (ext4/APFS limitan
   el componente a 255 bytes, no a 255 caracteres).
8. Vacío -> "Sin título".
9. Colisión dentro de la MISMA carpeta -> ` (2)`, ` (3)`... La comparación es
   `casefold()` + NFC, para que el vault siga siendo portable a macOS/Windows
   (donde `Informe` e `informe` son el mismo fichero). Dos notas con el mismo
   título en carpetas distintas NO colisionan: son páginas distintas.
"""

from __future__ import annotations

import os
import re
import unicodedata

MAX_BYTES = 180  # deja sitio para " (10)" y para el sufijo .md

FORBIDDEN = {"/": " ", "\\": " ", ":": " ", "*": " ", "?": " ", '"': " ",
             "<": " ", ">": " ", "|": " "}
# Los corchetes no rompen el nombre del fichero: rompen el wikilink. El título
# ES el nombre del fichero, así que `[[Notas [2026]]]` se lee como
# `[[Notas [2026` + `]]` (WIKILINK_RE corta en el primer `]`) y la arista a esa
# nota no existe ni se puede escribir a mano. Se sustituyen por paréntesis y no
# por un espacio para que el título saneado se siga leyendo.
BRACKETS = {"[": "(", "]": ")"}
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
RESERVED = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
            *[f"LPT{i}" for i in range(1, 10)]}


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def sanitize(title: str, fallback: str = "Sin título") -> tuple[str, list[str]]:
    """Devuelve (nombre_saneado, avisos de lo que se cambió)."""
    notes: list[str] = []
    original = title
    if unicodedata.normalize("NFC", title) != title:
        notes.append("normalizado NFD->NFC")
    name = nfc(title)

    if CONTROL_RE.search(name):
        notes.append("caracteres de control eliminados")
        name = CONTROL_RE.sub("", name)

    mapped = "".join(FORBIDDEN.get(ch, ch) for ch in name)
    if mapped != name:
        bad = sorted({ch for ch in name if ch in FORBIDDEN})
        notes.append("sustituidos " + " ".join(repr(c) for c in bad) + " por espacio")
        name = mapped

    bracketed = "".join(BRACKETS.get(ch, ch) for ch in name)
    if bracketed != name:
        notes.append("sustituidos '[' ']' por paréntesis: rompen el enlace [[...]]")
        name = bracketed

    name = re.sub(r"\s+", " ", name).strip(" .")

    if name in ("", ".", ".."):
        notes.append(f"nombre vacío o relativo -> {fallback!r}")
        name = fallback

    stem = name.split(".")[0].upper()
    if stem in RESERVED:
        notes.append("nombre reservado de Windows -> prefijo '_'")
        name = "_" + name

    encoded = name.encode("utf-8")
    if len(encoded) > MAX_BYTES:
        cut = encoded[:MAX_BYTES].decode("utf-8", "ignore").rstrip(" .")
        notes.append(f"truncado a {MAX_BYTES} bytes UTF-8 ({len(original)} caracteres)")
        name = cut or fallback

    if name != original:
        notes.append(f"'{original}' -> '{name}'")
    return name, notes


def unique_stem(directory: str, stem: str, taken: set[str] | None = None) -> tuple[str, bool]:
    """`stem` si está libre en `directory`, si no `stem (2)`, ` (3)`..."""
    taken = taken or set()

    def busy(candidate: str) -> bool:
        # el nombre final es '<candidate>.md'; también cuenta una carpeta con
        # ese nombre (la carpeta de filas de una base) y el sufijo .md pegado
        keys = {"%s.md" % unicodedata.normalize("NFC", candidate).casefold(),
                unicodedata.normalize("NFC", candidate).casefold()}
        if keys & taken:
            return True
        try:
            entries = os.listdir(directory)
        except FileNotFoundError:
            return False
        return any(unicodedata.normalize("NFC", e).casefold() in keys for e in entries)

    if not busy(stem):
        return stem, False
    n = 2
    while busy(f"{stem} ({n})"):
        n += 1
    return f"{stem} ({n})", True
