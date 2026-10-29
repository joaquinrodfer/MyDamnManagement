"""`/files`: los adjuntos del vault (imágenes de cabecera, etc.).

La URL es la ruta del fichero dentro del vault con `/files/` delante
(`/files/_attachments/2026/10/header-<id>.png`). Así lo que se guarda en
`header_image` se resuelve contra el vault sin tabla de traducción y una copia
de la carpeta del vault se lleva todo lo necesario para pintar la nota.

Se sirve con `nosniff` y `Content-Disposition: attachment` salvo para las
imágenes raster conocidas: un `.svg` o un `.html` que hubiera quedado ahí a
mano se descarga en vez de renderizarse en el mismo origen que la app (no hay
CSP: `frontend/index.html` usa estilos y scripts inline y una CSP rompería la
app entera). El tipo sale de los magic bytes del fichero, no de su extensión.
"""

from __future__ import annotations

import os
import stat

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import settings
from app.vault import attachments, vaultfs

router = APIRouter(tags=["files"])

# Suficiente para todas las firmas que se comprueban (PNG, JPEG, GIF, WebP).
SNIFF_BYTES = 16


def _resolve(path: str) -> str:
    """Ruta absoluta de un adjunto del vault, o 404.

    Todo lo que no sea un fichero regular dentro de `_attachments/` es 404 y no
    403: el vault no es un servidor de ficheros y no hace falta decir qué hay
    ni aunque exista.
    """
    try:
        rel = vaultfs.safe_rel(settings.vault_path, path)
    except vaultfs.PathEscape:
        raise HTTPException(404, "No existe") from None
    if not rel.startswith(attachments.ATTACHMENTS_DIR + "/"):
        raise HTTPException(404, "No existe")
    abs_path = os.path.join(settings.vault_path, rel)
    try:
        st = os.stat(abs_path)
    except OSError:
        raise HTTPException(404, "No existe") from None
    if not stat.S_ISREG(st.st_mode):
        raise HTTPException(404, "No existe")
    return abs_path


@router.get("/files/{path:path}")
def get_file(path: str):
    abs_path = _resolve(path)
    try:
        with open(abs_path, "rb") as fh:
            head = fh.read(SNIFF_BYTES)
    except OSError:
        raise HTTPException(404, "No existe") from None

    media_type = attachments.media_type(head)
    filename = os.path.basename(abs_path)
    safe_name = filename.replace('"', "").replace("\\", "_")
    safe_name = safe_name.replace("\r", "").replace("\n", "") or "attachment"

    return FileResponse(
        abs_path,
        media_type=media_type or "application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            # Como el resto de estáticos: el navegador revalida en vez de
            # servir de caché a ciegas (el panel añade `?t=` al <img>).
            "Cache-Control": "no-cache",
            "Content-Disposition": (
                f'{"inline" if media_type else "attachment"}; filename="{safe_name}"'
            ),
        },
    )
