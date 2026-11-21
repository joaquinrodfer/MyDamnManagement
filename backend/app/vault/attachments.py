"""Adjuntos dentro del vault: `_attachments/<año>/<mes>/<nombre>`.

ADR-0001: copiar la carpeta del vault es un backup completo, y eso incluye las
imágenes de cabecera. Hasta P1.2b vivían en `/app/attachments`, en un volumen
aparte, así que la copia del vault salía con los `.md` y sin sus imágenes: lo
contrario de lo que la v1.0 promete.

Dos piezas:

- **Guardar** (`save_header`): nombre fijo por página y extensión
  (`_attachments/2026/10/header-<id>.png`), así volver a subir una cabecera
  reemplaza a la anterior en vez de acumular huérfanos. Escritura atómica.
- **Servir** (`/files/<ruta del vault>`, ver `app/routers/files.py`): la ruta
  tiene que estar dentro de `_attachments/` y el tipo se decide por los **magic
  bytes** del fichero, no por la extensión ni por lo que declaró el cliente.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from app.vault import vaultfs

# Carpeta de los adjuntos dentro del vault (ADR-0001). `reader.IGNORED_DIRS` la
# salta, así que un `.md` colado ahí no se indexa como nota.
ATTACHMENTS_DIR = "_attachments"

# Los adjuntos se sirven con su ruta del vault detrás de `/files/`, así que el
# `header_image` del frontmatter se puede resolver contra el vault sin tabla
# de traducción: `/files/_attachments/2026/10/header-<id>.png`.
URL_PREFIX = "/files/"

# Lo único que `/files` sirve como imagen; el resto baja como
# `application/octet-stream` con `Content-Disposition: attachment`.
MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


def detect_image_format(data: bytes) -> Optional[str]:
    """Extensión ("png"/"jpg"/"gif"/"webp") según los magic bytes, o None.

    A propósito **no** se mira el Content-Type declarado ni el nombre del
    archivo del cliente: los dos se pueden mentir, y `/files` sirve lo subido en
    el mismo origen que la app (y sin CSP), así que un SVG o un HTML guardado
    ahí sería XSS almacenado. Solo cuentan las firmas reales de PNG, JPEG, GIF
    y WebP.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def media_type(head: bytes) -> Optional[str]:
    """Content-Type de una imagen conocida, o None si el contenido no lo es."""
    fmt = detect_image_format(head)
    return MEDIA_TYPES.get(fmt) if fmt else None


def url_for(rel: str) -> str:
    """URL con la que se sirve un fichero del vault."""
    return URL_PREFIX + rel.lstrip("/")


def month_folder(now: Optional[datetime] = None) -> str:
    """`<año>/<mes>` en UTC, el trozo de ruta que pide el ADR."""
    now = now or datetime.now(timezone.utc)
    return f"{now:%Y}/{now:%m}"


def header_stem(page_id) -> str:
    return f"header-{page_id}"


def header_rel(page_id, ext: str, now: Optional[datetime] = None) -> str:
    """Ruta (relativa al vault) de la cabecera de una página."""
    return f"{ATTACHMENTS_DIR}/{month_folder(now)}/{header_stem(page_id)}.{ext}"


def save_header(
    root: str, page_id, data: bytes, ext: str, now: Optional[datetime] = None
) -> str:
    """Escribe la cabecera en el vault. Devuelve su ruta relativa."""
    rel = header_rel(page_id, ext, now)
    vaultfs.atomic_write_bytes(os.path.join(root, rel), data)
    return rel


def find_header(root: str, page_id) -> list[str]:
    """Rutas relativas de las cabeceras de esa página (una por extensión).

    Se recorre `_attachments/` entero a propósito: una cabecera subida en
    octubre y reemplazada en noviembre está en dos carpetas de mes distintas.
    """
    base = os.path.join(root, ATTACHMENTS_DIR)
    prefix = header_stem(page_id) + "."
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for name in sorted(filenames):
            if name.startswith(prefix):
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                found.append(rel.replace(os.sep, "/"))
    return found


def delete_header(root: str, page_id, keep: Optional[str] = None) -> int:
    """Borra las cabeceras de esa página (salvo `keep`). Devuelve cuántas quitó."""
    removed = 0
    for rel in find_header(root, page_id):
        if keep is not None and rel == keep:
            continue
        try:
            os.unlink(os.path.join(root, rel))
            removed += 1
        except FileNotFoundError:
            pass
    return removed


# --------------------------------------------------------------------------
# archivos dentro del cuerpo de una página (multimedia)
# --------------------------------------------------------------------------

_SLUG_RE = __import__("re").compile(r"[^a-z0-9]+")


def file_slug(name: str) -> str:
    """Un trozo legible del nombre original para la ruta (`informe-q4`).

    Solo letras ASCII minúsculas, dígitos y guiones, como mucho 40: el nombre
    del cliente no decide nada más que esto (ni la extensión ni el tipo)."""
    import unicodedata

    base = os.path.splitext(os.path.basename(name or ""))[0]
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode().lower()
    return _SLUG_RE.sub("-", base).strip("-")[:40].strip("-") or "archivo"


def file_rel(name: str, ext: str, now: Optional[datetime] = None) -> str:
    """Ruta (relativa al vault) de un archivo nuevo: nunca pisa a otro."""
    import uuid as _uuid

    return f"{ATTACHMENTS_DIR}/{month_folder(now)}/{_uuid.uuid4().hex[:12]}-{file_slug(name)}.{ext}"


def save_file(root: str, name: str, data: bytes, ext: str, now: Optional[datetime] = None) -> str:
    """Escribe un archivo subido al cuerpo de una página. Devuelve su ruta relativa."""
    rel = file_rel(name, ext, now)
    vaultfs.atomic_write_bytes(os.path.join(root, rel), data)
    return rel


# Lo que se sirve en línea además de las imágenes (vídeo, audio, PDF). Igual
# que con las imágenes, el tipo lo deciden los bytes del fichero, nunca su
# nombre: un HTML o un SVG con otra extensión se sigue descargando.
MEDIA_KINDS = {
    "mp4": ("video", "video/mp4"),
    "webm": ("video", "video/webm"),
    "mov": ("video", "video/quicktime"),
    "mp3": ("audio", "audio/mpeg"),
    "m4a": ("audio", "audio/mp4"),
    "ogg": ("audio", "audio/ogg"),
    "wav": ("audio", "audio/wav"),
    "flac": ("audio", "audio/flac"),
    "pdf": ("pdf", "application/pdf"),
}


def detect_media_format(data: bytes) -> Optional[str]:
    """Extensión de un vídeo, audio o PDF conocido según sus bytes, o None."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        marca = data[8:12]
        if marca in (b"M4A ", b"M4B "):
            return "m4a"
        if marca == b"qt  ":
            return "mov"
        return "mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0 and (data[1] & 0x06) != 0):
        return "mp3"
    if data.startswith(b"OggS"):
        return "ogg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"fLaC"):
        return "flac"
    return None


def served_type(head: bytes) -> Optional[str]:
    """Content-Type con el que `/files` sirve un fichero EN LÍNEA, o None si se
    descarga (`attachment`): imágenes, vídeo, audio y PDF reconocidos."""
    imagen = media_type(head)
    if imagen:
        return imagen
    fmt = detect_media_format(head)
    return MEDIA_KINDS[fmt][1] if fmt else None


_EXT_RE = __import__("re").compile(r"^[a-z0-9]{1,8}$")


def other_ext(name: str) -> str:
    """Extensión para un archivo cualquiera (se descarga, nunca se abre en
    línea): la del nombre si es corta y limpia, o `bin`."""
    ext = os.path.splitext(name or "")[1].lstrip(".").lower()
    if ext in ("html", "htm", "svg", "xhtml", "xml", "js", "mjs"):
        return "txt"  # por si acaso: nunca con una extensión que un navegador ejecute
    # Un nombre que dice ser imagen/vídeo/PDF pero cuyos bytes no lo son: el
    # editor decide cómo pintar por la extensión, así que no puede llevarla.
    if ext in MEDIA_KINDS or ext in MEDIA_TYPES or ext == "jpeg":
        return "bin"
    return ext if _EXT_RE.match(ext) else "bin"
