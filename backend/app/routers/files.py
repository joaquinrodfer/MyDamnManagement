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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.config import settings
from app.vault import attachments, vaultfs

router = APIRouter(tags=["files"])

# Suficiente para todas las firmas que se comprueban (imágenes, vídeo, audio, PDF).
SNIFF_BYTES = 16
CHUNK = 256 * 1024


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


def _parse_range(header: str, size: int):
    """`bytes=a-b` → (a, b) inclusive, o None si no se entiende / no cabe."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    a, _, b = header[6:].strip().partition("-")
    try:
        if a == "":
            n = int(b)
            if n <= 0:
                return None
            return max(0, size - n), size - 1
        start = int(a)
        end = int(b) if b else size - 1
    except ValueError:
        return None
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)


def _slice(path: str, start: int, end: int):
    with open(path, "rb") as fh:
        fh.seek(start)
        left = end - start + 1
        while left > 0:
            chunk = fh.read(min(CHUNK, left))
            if not chunk:
                break
            left -= len(chunk)
            yield chunk


@router.get("/files/{path:path}")
def get_file(path: str, request: Request):
    abs_path = _resolve(path)
    try:
        with open(abs_path, "rb") as fh:
            head = fh.read(SNIFF_BYTES)
    except OSError:
        raise HTTPException(404, "No existe") from None

    media_type = attachments.served_type(head)
    filename = os.path.basename(abs_path)
    safe_name = filename.replace('"', "").replace("\\", "_")
    safe_name = safe_name.replace("\r", "").replace("\n", "") or "attachment"
    headers = {
        "X-Content-Type-Options": "nosniff",
        # Como el resto de estáticos: el navegador revalida en vez de
        # servir de caché a ciegas (el panel añade `?t=` al <img>).
        "Cache-Control": "no-cache",
        "Content-Disposition": f'{"inline" if media_type else "attachment"}; filename="{safe_name}"',
        "Accept-Ranges": "bytes",
    }
    tipo = media_type or "application/octet-stream"

    # Rangos (`Range: bytes=…`): sin ellos un vídeo no se puede adelantar. Esta
    # versión de Starlette no los atiende en `FileResponse`, así que se sirven
    # aquí: 206 con el trozo pedido, o 416 si no cabe en el fichero.
    rango = request.headers.get("range")
    if rango:
        size = os.path.getsize(abs_path)
        tramo = _parse_range(rango, size)
        if tramo is None:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        start, end = tramo
        return StreamingResponse(
            _slice(abs_path, start, end),
            status_code=206,
            media_type=tipo,
            headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end - start + 1)},
        )
    return FileResponse(abs_path, media_type=tipo, headers=headers)
