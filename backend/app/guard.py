"""La guarda de permisos: el único sitio de la API que pregunta a `acl.authorize`.

Se engancha a todos los routers al registrarlos (`main.py`), así que ninguna
ruta lleva su propio `if` de permisos. Decide, para cada petición:

1. **Sobre qué recurso**: los parámetros de la ruta (`page_id`, `database_id`;
   una fila respeta el permiso de su base, y `/acl/{tipo}/{id}` el de su
   recurso), la nota de una sugerencia (`suggestion_id`) y, en las rutas que
   crean o mueven, la página madre que llega en el cuerpo (`parent_id`).
2. **Con qué nivel**: leer (`GET`) pide `view`; escribir, `edit`; cambiar los
   permisos (`PUT /acl/...`), `owner`; y meter algo dentro de otra página
   (`parent_id`), `edit` sobre esa página.
3. **Lo que es solo de administración**: la papelera y `POST /reindex` (hablan
   del vault entero, no de una página).

Sin sesión (las rutas públicas, la puerta de los agentes con su token) o con un
administrador, no se comprueba nada: lo primero lo decide el middleware y lo
segundo lo ve todo (soporte).

Los listados no pasan por aquí: los filtra el repositorio con
`acl.visible_clause` (no hay un recurso que comprobar, hay que quitar filas).
"""

from __future__ import annotations

import json

from fastapi import HTTPException, Request

from app import acl
from app.config import settings
from app.index import open_index

READ_METHODS = {"GET", "HEAD", "OPTIONS"}

# Rutas del vault entero: solo administradores.
ADMIN_PREFIXES = ("/trash", "/reindex")

# Rutas que crean o mueven y traen la página madre en el cuerpo JSON.
PARENT_IN_BODY = {
    ("POST", "/pages"),
    ("POST", "/databases"),
    ("POST", "/databases/from-template"),
}
PARENT_IN_BODY_PATCH_PREFIXES = ("/pages/", "/databases/")


async def guard(request: Request) -> None:
    user = getattr(request.state, "user", None)
    if user is None or getattr(user, "is_admin", False):
        return
    path = request.url.path
    method = request.method.upper()

    if path.startswith(ADMIN_PREFIXES):
        raise HTTPException(403, "solo un administrador puede hacer esto")

    params = request.path_params
    level = "view" if method in READ_METHODS else "edit"
    targets: list[tuple[str, str]] = []

    if path.startswith("/acl/"):
        rid = params.get("resource_id")
        if rid is not None:
            targets.append((str(rid), "view" if method in READ_METHODS else "owner"))
    else:
        for key in ("page_id", "database_id"):
            if key in params:
                targets.append((str(params[key]), level))
                break

    parent = await _parent_from_body(request, method, path)
    if parent:
        targets.append((parent, "edit"))

    sid = params.get("suggestion_id")
    conn = open_index(settings.index_path)
    try:
        if sid is not None:
            page_id = _suggestion_page(conn, str(sid))
            if page_id is None:
                raise HTTPException(404, "Sugerencia no encontrada")
            targets.append((page_id, level))
        for resource_id, needed in targets:
            try:
                acl.authorize(conn, user, resource_id, needed)
            except acl.NotVisible:
                raise HTTPException(404, "No encontrada") from None
            except acl.Forbidden as exc:
                raise HTTPException(403, f"No tienes permiso: {exc}") from None
    finally:
        conn.close()


async def _parent_from_body(request: Request, method: str, path: str) -> str | None:
    """`parent_id` del cuerpo JSON en las rutas que crean o mueven páginas."""
    wants = (method, path.rstrip("/")) in PARENT_IN_BODY or (
        method == "PATCH" and path.startswith(PARENT_IN_BODY_PATCH_PREFIXES)
        and path.count("/") == 2  # /pages/{id} o /databases/{id}, no subrutas
    )
    if not wants:
        return None
    if "application/json" not in request.headers.get("content-type", ""):
        return None
    try:
        body = json.loads(await request.body() or b"null")
    except ValueError:
        return None  # el cuerpo roto lo rechaza la validación de la ruta (422)
    if not isinstance(body, dict):
        return None
    parent = body.get("parent_id")
    return str(parent) if parent else None


def _suggestion_page(conn, suggestion_id: str) -> str | None:
    from app.repository import Repository

    found = Repository(conn).get_suggestion(suggestion_id)
    if found is None:
        return None
    page_id = getattr(found, "page_id", None)
    if page_id is None and isinstance(found, tuple):
        page_id = getattr(found[0], "page_id", None)
    return str(page_id) if page_id else None
