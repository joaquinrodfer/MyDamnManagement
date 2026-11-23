"""Cabeceras de seguridad y comprobación de origen (pentest de 2026-11-23).

- **CSP:** solo scripts propios (`app.js` y el bundle); las pantallas de entrada
  añaden el hash de su script en línea (`login_page.csp_for`). Los estilos sí
  van en línea (el panel los pone desde JS). Imágenes externas permitidas: una
  nota puede enlazar una imagen de internet. Nadie puede meter Notemil en un
  `<iframe>` (`frame-ancestors 'none'`: clickjacking).
- **Origen:** una escritura (POST/PUT/PATCH/DELETE) que trae un `Origin` de
  otra web se corta con 403. La cookie ya es `SameSite=Lax`, pero en este
  servidor hay otras apps del mismo «sitio» (misma IP/dominio, otro puerto) y
  `Lax` no las separa. Sin `Origin` (curl, atajos, el agente) no se mira: lo
  que protege es el navegador, que siempre lo manda.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; media-src 'self' blob:; font-src 'self' data:; "
    "connect-src 'self'; worker-src 'self' blob:; manifest-src 'self'; frame-src 'none'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def headers_for(request: Request, content_type: str) -> dict[str, str]:
    out = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    }
    if content_type.startswith("text/html"):
        out["Content-Security-Policy"] = CSP
    if request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https" or request.url.scheme == "https":
        out["Strict-Transport-Security"] = "max-age=31536000"
    return out


def foreign_origin(request: Request) -> bool:
    """¿Escritura con un `Origin` que no es esta web?"""
    if request.method.upper() not in UNSAFE:
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return False
    if origin == "null":
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return urlsplit(origin).netloc.lower() != host.split(",")[0].strip().lower()
