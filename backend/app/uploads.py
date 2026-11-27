"""Límites y cuidados de los archivos subidos (páginas y chat).

- **Tamaño**: cada espacio decide su máximo por archivo (`files_max_mb`,
  ajuste del espacio que cambian sus administradores), con un tope de la
  instalación (`HARD_MAX_MB`) que nadie supera. Sin espacio (chats privados y
  grupos), `DEFAULT_MAX_MB`.
- **Caducidad**: los adjuntos del chat son temporales. Pasados
  `chat_files_days` días (ajuste del espacio; `DEFAULT_CHAT_DAYS` en privados
  y grupos) se borra el fichero y el mensaje se queda con «archivo caducado».
  Los de las páginas no caducan: son parte del documento.
- **Lo que no se acepta**: ejecutables y scripts (`BLOCKED_EXT`), y nada si el
  disco se queda con menos de `MIN_FREE_BYTES` libres.
- **Cuerpos enormes**: `BodyLimit` corta la petición antes de que el
  servidor la guarde entera en un temporal (un envío de gigas llenaría el
  disco aunque luego la ruta lo rechazara).

Lo que ya hacía `/files` sigue igual: el tipo se decide por los bytes, lo que
no es imagen/vídeo/audio/PDF se descarga, y ahora además se sirve con una CSP
`sandbox` para que nada pueda ejecutar scripts en el origen de la app.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("app.uploads")

HARD_MAX_MB = 200
DEFAULT_MAX_MB = 25
DEFAULT_CHAT_DAYS = 30
MAX_CHAT_DAYS = 365
MIN_FREE_BYTES = 1024 * 1024 * 1024
MB = 1024 * 1024

BLOCKED_EXT = frozenset({
    "exe", "dll", "com", "bat", "cmd", "msi", "msp", "scr", "pif", "cpl", "hta", "lnk", "reg",
    "vbs", "vbe", "js", "jse", "mjs", "wsf", "wsh", "ps1", "psm1", "sh", "bash", "zsh", "csh",
    "jar", "apk", "app", "dmg", "pkg", "deb", "rpm", "run", "bin", "elf", "so", "dylib",
    "php", "phtml", "py", "pl", "rb", "cgi", "asp", "aspx", "jsp",
})


class UploadRejected(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _settings_of(conn: sqlite3.Connection, workspace_id: Optional[str]) -> dict[str, Any]:
    if not workspace_id:
        return {}
    fila = conn.execute("SELECT settings FROM workspaces WHERE id = ?", (str(workspace_id),)).fetchone()
    try:
        datos = json.loads((fila[0] if fila else None) or "{}")
    except ValueError:
        return {}
    return datos if isinstance(datos, dict) else {}


def max_mb(conn: sqlite3.Connection, workspace_id: Optional[str]) -> int:
    valor = _settings_of(conn, workspace_id).get("files_max_mb")
    return valor if isinstance(valor, int) and 1 <= valor <= HARD_MAX_MB else DEFAULT_MAX_MB


def chat_days(conn: sqlite3.Connection, workspace_id: Optional[str]) -> int:
    valor = _settings_of(conn, workspace_id).get("chat_files_days")
    return valor if isinstance(valor, int) and 1 <= valor <= MAX_CHAT_DAYS else DEFAULT_CHAT_DAYS


def validate_settings(ajustes: dict[str, Any]) -> dict[str, Any]:
    """Comprueba `files_max_mb` y `chat_files_days` si vienen (para el PATCH del espacio)."""
    salida = dict(ajustes)
    for clave, tope, texto in (
        ("files_max_mb", HARD_MAX_MB, f"El tamaño máximo por archivo va de 1 a {HARD_MAX_MB} MB."),
        ("chat_files_days", MAX_CHAT_DAYS, f"Los archivos del chat duran de 1 a {MAX_CHAT_DAYS} días."),
    ):
        if clave not in salida:
            continue
        valor = salida[clave]
        if isinstance(valor, str) and valor.strip().isdigit():
            valor = int(valor.strip())
        if isinstance(valor, bool) or not isinstance(valor, int) or not 1 <= valor <= tope:
            raise ValueError(texto)
        salida[clave] = valor
    return salida


def extension(name: str) -> str:
    return os.path.splitext(name or "")[1].lstrip(".").lower()


async def read_checked(file: Any, name: str, limit_mb: int, root: str) -> bytes:
    """Lee el archivo subido como mucho hasta el límite y lo rechaza si no vale."""
    if extension(name) in BLOCKED_EXT:
        raise UploadRejected("Por seguridad no se aceptan programas ni scripts (.exe, .bat, .js, .sh…).", 415)
    data = await file.read(limit_mb * MB + 1)
    if len(data) > limit_mb * MB:
        raise UploadRejected(f"Máximo {limit_mb} MB por archivo.", 413)
    if not data:
        raise UploadRejected("El archivo está vacío.", 400)
    try:
        libre = shutil.disk_usage(root).free
    except OSError:
        libre = None
    if libre is not None and libre - len(data) < MIN_FREE_BYTES:
        log.warning("subida rechazada: quedan %d MB libres", libre // MB)
        raise UploadRejected("El servidor se está quedando sin espacio: ahora no se pueden subir archivos.", 507)
    return data


# --------------------------------------------------------------------------
# caducidad de los adjuntos del chat
# --------------------------------------------------------------------------


def _rel_of(url: str) -> Optional[str]:
    return url[len("/files/"):] if isinstance(url, str) and url.startswith("/files/") else None


def expire_chat_files(conn: sqlite3.Connection, root: str, now: Optional[datetime] = None) -> int:
    """Borra los adjuntos del chat que han pasado su plazo. Devuelve cuántos.

    El fichero no se borra si alguna página lo enlaza (alguien lo pegó en un
    documento) o si otro mensaje aún vigente lo usa; el mensaje igualmente
    se queda con «archivo caducado»."""
    from app.vault import vaultfs

    ahora = now or datetime.now(timezone.utc)
    plazos: dict[Optional[str], int] = {}
    caducados = 0
    filas = conn.execute(
        "SELECT m.id, m.created_at, m.attachments_json, c.workspace_id FROM chat_messages m"
        " JOIN chat_conversations c ON c.id = m.conversation_id"
        " WHERE m.deleted = 0 AND m.attachments_json NOT IN ('', '[]') AND m.attachments_json LIKE '%\"url\"%'"
    ).fetchall()
    for fila in filas:
        ws = fila["workspace_id"]
        if ws not in plazos:
            plazos[ws] = chat_days(conn, ws)
        try:
            creado = datetime.fromisoformat(fila["created_at"])
        except ValueError:
            continue
        if creado + timedelta(days=plazos[ws]) > ahora:
            continue
        try:
            adjuntos = json.loads(fila["attachments_json"] or "[]")
        except ValueError:
            continue
        nuevos = []
        for a in adjuntos:
            url = a.get("url") if isinstance(a, dict) else None
            if not url:
                nuevos.append(a)
                continue
            nuevos.append({"name": a.get("name"), "type": a.get("type"), "size": a.get("size"),
                           "expired": True, "expired_at": ahora.isoformat(timespec="seconds")})
            caducados += 1
            if _still_used(conn, url, fila["id"]):
                continue
            rel = _rel_of(url)
            try:
                rel = vaultfs.safe_rel(root, rel) if rel else None
            except vaultfs.PathEscape:
                rel = None
            if rel and rel.startswith("_attachments/"):
                try:
                    os.remove(os.path.join(root, rel))
                except FileNotFoundError:
                    pass
                except OSError:
                    log.exception("no se pudo borrar %s", rel)
        conn.execute("UPDATE chat_messages SET attachments_json = ? WHERE id = ?",
                     (json.dumps(nuevos, ensure_ascii=False), fila["id"]))
    return caducados


def _still_used(conn: sqlite3.Connection, url: str, message_id: int) -> bool:
    patron = f"%{url}%"
    if conn.execute("SELECT 1 FROM pages WHERE body LIKE ? LIMIT 1", (patron,)).fetchone():
        return True
    return conn.execute(
        "SELECT 1 FROM chat_messages WHERE id != ? AND deleted = 0 AND attachments_json LIKE ? LIMIT 1",
        (message_id, f'%"url": "{url}"%'),
    ).fetchone() is not None


# --------------------------------------------------------------------------
# tope del cuerpo de las peticiones
# --------------------------------------------------------------------------


class _TooLarge(Exception):
    pass


class BodyLimit:
    """Middleware ASGI: corta las peticiones con un cuerpo mayor que el tope
    (por `Content-Length` o contando los trozos si no lo dicen)."""

    def __init__(self, app: Any, multipart_bytes: int = (HARD_MAX_MB + 2) * MB,
                 other_bytes: int = 50 * MB) -> None:
        self.app = app
        self.multipart_bytes = multipart_bytes
        self.other_bytes = other_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        cabeceras = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        tope = self.multipart_bytes if cabeceras.get("content-type", "").startswith("multipart/") else self.other_bytes
        largo = cabeceras.get("content-length", "")
        if largo.isdigit() and int(largo) > tope:
            await self._reject(send, tope)
            return
        recibido = 0
        empezada = False

        async def recibir() -> dict:
            nonlocal recibido
            mensaje = await receive()
            if mensaje.get("type") == "http.request":
                recibido += len(mensaje.get("body") or b"")
                if recibido > tope:
                    raise _TooLarge()
            return mensaje

        async def enviar(mensaje: dict) -> None:
            nonlocal empezada
            if mensaje.get("type") == "http.response.start":
                empezada = True
            await send(mensaje)

        try:
            await self.app(scope, recibir, enviar)
        except _TooLarge:
            if not empezada:
                await self._reject(send, tope)

    @staticmethod
    async def _reject(send: Any, tope: int) -> None:
        cuerpo = json.dumps({"detail": f"El envío pasa del máximo ({tope // MB} MB)."}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(cuerpo)).encode())]})
        await send({"type": "http.response.body", "body": cuerpo})
