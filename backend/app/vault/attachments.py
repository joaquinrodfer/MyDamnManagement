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
