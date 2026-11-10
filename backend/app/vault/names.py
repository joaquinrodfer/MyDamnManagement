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

Las **filas de una base** no pasan por aquí: su fichero no lleva el título sino
los 8 primeros caracteres hex de su `id` (`row_stem`), y el título vive en el
frontmatter. Así el sufijo ` (2)` ya no aparece al duplicar el título de una
fila: dos filas pueden llamarse igual y cada una conserva su fichero.
"""

from __future__ import annotations

import os
import re
import unicodedata
import uuid

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


def _nfc_key(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _busy(directory: str, candidate: str, taken: set[str],
          ignore: set[str] | None = None) -> bool:
    """¿Está ocupado `candidate` en `directory`?

    Cuentan el fichero `<candidate>.md` y una carpeta con ese nombre (la
    carpeta de filas de una base). `ignore` son stems que hay que tratar como
    libres: la migración de una fila a `<id8>.md` no puede chocar con el
    fichero que esa misma fila tiene ahora.
    """
    if ignore and _nfc_key(candidate) in ignore:
        return False
    keys = {"%s.md" % _nfc_key(candidate), _nfc_key(candidate)}
    if keys & taken:
        return True
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return False
    return any(_nfc_key(e) in keys for e in entries)


def unique_stem(directory: str, stem: str, taken: set[str] | None = None) -> tuple[str, bool]:
    """`stem` si está libre en `directory`, si no `stem (2)`, ` (3)`..."""
    taken = taken or set()
    if not _busy(directory, stem, taken):
        return stem, False
    n = 2
    while _busy(directory, f"{stem} ({n})", taken):
        n += 1
    return f"{stem} ({n})", True


# Las filas no llevan su título en el nombre del fichero: se llaman por los 8
# primeros caracteres hex de su `id` (`Bases/Contactos/7f3a91c2.md`) y el
# título vive en el frontmatter, así que renombrar una fila no mueve el
# fichero y no puede romper un enlace. Ocho caracteres son 32 bits: con miles
# de filas por carpeta el choque es improbable, y si ocurre se alarga a 10,
# 12… hasta el id entero, que es único.
ROW_STEM_MIN = 8
ROW_STEM_STEP = 2

# Namespace del stem de una fila cuyo `id` no es un UUID (escrita a mano): el
# stem sale de un uuid5 del id, determinista entre ejecuciones.
_ROW_STEM_NAMESPACE = uuid.UUID("6d1c9a0e-4f2b-5c37-8e51-2a7b9f0d3c48")


def hexed_id(row_id) -> str:
    """El `id` de una fila en hex, sin guiones.

    Un id que no es un UUID (una fila escrita a mano) se convierte con un
    uuid5 del propio id: el nombre del fichero es estable entre ejecuciones.
    """
    try:
        return uuid.UUID(str(row_id).strip()).hex
    except (ValueError, AttributeError):
        return uuid.uuid5(_ROW_STEM_NAMESPACE, str(row_id)).hex


def is_row_stem(stem: str, row_id) -> bool:
    """¿`stem` ya es el nombre por id de esta fila (`<id8>`, `<id10>`…)?

    Lo usa la migración para no renombrar lo que ya está migrado (idempotencia)
    y para no acortar a 8 un fichero que ya se llamaba `id10.md`.
    """
    key = _nfc_key(stem)
    return (
        len(key) >= ROW_STEM_MIN
        and all(ch in "0123456789abcdef" for ch in key)
        and hexed_id(row_id).startswith(key)
    )


def row_stem(directory: str, row_id, *,
             ignore: set[str] | None = None) -> tuple[str, bool]:
    """Stem de una fila: `<id8>`, alargando a 10, 12… si choca en la carpeta.

    Devuelve `(stem, alargado)`, como `unique_stem`: `alargado` dice que los 8
    primeros caracteres no estaban libres y se usaron más. `ignore` son stems
    que cuentan como libres (ver `_busy`).
    """
    hexed = hexed_id(row_id)
    ignore_keys = {_nfc_key(stem) for stem in (ignore or set())}
    length = ROW_STEM_MIN
    while length < len(hexed):
        if not _busy(directory, hexed[:length], set(), ignore_keys):
            return hexed[:length], length != ROW_STEM_MIN
        length += ROW_STEM_STEP
    return hexed, True
