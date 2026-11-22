"""Las rutas de entrada (P11a): `/auth/...`.

Todo lo que cuelga de aquí es pequeño a propósito: el estado vive en
`app/auth.py` (usuarios, hashes, sesiones y el limitador de intentos) y esta
capa solo traduce HTTP a esas funciones —formularios, cookies y códigos—.

Qué es público y qué no:

- `POST /auth/login`, `POST /auth/setup` y `GET /auth/status` son las únicas
  rutas de este router que no exigen sesión (las tres son necesarias para
  poder entrar; ver `PUBLIC_PATHS` en `app.main`). `/auth/status` no filtra
  nada: dice si hay que dar de alta al primero y si la petición trae sesión,
  que es exactamente lo que la pantalla de login ya pinta.
- El resto (`/auth/me`, `/auth/logout`, `/auth/password`, `/auth/users`) pasa
  por el middleware como cualquier ruta del dueño.

La cookie de sesión se pone aquí y solo aquí: `HttpOnly` (el JS del panel no
puede leer el token), `SameSite=Lax` (una página de otro sitio no puede usar la
sesión para escribir, pero la navegación normal sí la lleva) y `Path=/`.
`Secure` se decide por petición: `SESSION_COOKIE_SECURE` si está puesto y, si
no, por `X-Forwarded-Proto`/el esquema — detrás de `tailscale serve` el TLS lo
termina el proxy, y sin mirar la cabecera la cookie saldría sin `Secure` en el
único despliegue que de verdad va por HTTPS.
"""

from __future__ import annotations

import sqlite3
from typing import Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app import audit, auth, capture_tokens, config
from app import workspaces as ws_mod
from app.dependencies import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


# ------------------------------------------------------------------ esquemas


class LoginRequest(BaseModel):
    username: str
    password: str


class SetupRequest(BaseModel):
    username: str
    display_name: str
    password: str
    color: Optional[str] = None
    avatar: Optional[str] = None


class PasswordRequest(BaseModel):
    current_password: str
    new_password: str


class TokenCreateRequest(BaseModel):
    # Una etiqueta para reconocer la llave en la lista («Mac de casa»).
    name: Optional[str] = None


class UserCreateRequest(BaseModel):
    username: str
    display_name: str
    password: str
    color: Optional[str] = None
    is_admin: bool = False
    avatar: Optional[str] = None
    can_view_global_workload: bool = False
    # Espacios en los que entra, `[{"id", "role"}]`; sin él, en General.
    workspaces: Optional[list[dict]] = None


class UserUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    color: Optional[str] = None
    avatar: Optional[str] = None
    can_view_global_workload: Optional[bool] = None


class MeUpdateRequest(BaseModel):
    """Lo que cualquiera puede cambiar de sí mismo: su nombre visible y su
    avatar (cualquiera de los dos puede faltar)."""

    display_name: Optional[str] = None
    avatar: Optional[str] = None


def _workspaces_of_payload(conn, items):
    """`[{"id", "role"}]` -> `[(id, role)]`, validado; `None` si no se pidió."""
    if items is None:
        return None
    out = []
    for item in items:
        wid, role = str(item.get("id", "")), item.get("role", "editor")
        if ws_mod.get(conn, wid) is None:
            raise HTTPException(400, f"no existe el espacio {wid}")
        if role not in ws_mod.ROLES:
            raise HTTPException(400, "el rol es admin, editor o viewer")
        out.append((wid, role))
    return out


# ------------------------------------------------------------------ conexión


def get_conn() -> Iterator[sqlite3.Connection]:
    """Conexión al índice para las rutas de autenticación.

    Se resuelve aquí (y no con `app.dependencies.get_conn`) porque el índice se
    lee de `config.settings` **en el momento de la petición**: los tests de
    `DOCS_ENABLED` recargan `app.config` y `app.main`, y un `settings`
    importado al cargar este módulo se quedaría apuntando al índice viejo.
    """
    from app.index import open_index

    conn = open_index(config.settings.index_path)
    try:
        yield conn
    finally:
        conn.close()


# -------------------------------------------------------------------- cookie


def is_https(request: Request) -> bool:
    """¿La petición llegó por HTTPS? Detrás de un proxy, `X-Forwarded-Proto`."""
    forwarded = request.headers.get("x-forwarded-proto", "")
    first = forwarded.split(",")[0].strip().lower()
    if first:
        return first == "https"
    return request.url.scheme == "https"


def cookie_secure(request: Request) -> bool:
    """`SESSION_COOKIE_SECURE` manda; si no está, se deduce de la petición."""
    configured = config.settings.session_cookie_secure
    return is_https(request) if configured is None else bool(configured)


def _cookie_kwargs(request: Request) -> dict:
    """Atributos de la cookie. El `Max-Age` es el tope **absoluto**, no `SESSION_DAYS`.

    La vida de una sesión la decide `sessions.expires_at` en la base (ver
    `app.auth.user_for_token`): 30 días deslizantes desde el último uso, sin
    pasar nunca de 90 desde que se abrió. La cookie no puede caducar antes que
    eso —con 30 días fijos, el navegador la tiraría el día 31 aunque la sesión
    siguiera viva y se usara a diario— ni debe durar más que el tope absoluto.
    Por eso lleva el tope (90 días) y quien corta de verdad es la fila de
    `sessions`: pasados 30 días sin usar la sesión, el servidor responde 401
    aunque el navegador todavía presente la cookie.
    """
    days = auth.ABSOLUTE_DAYS
    return {
        "key": auth.COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "path": "/",
        "secure": cookie_secure(request),
        "max_age": int(days * 24 * 3600),
    }


def signed_in(
    conn: sqlite3.Connection, request: Request, user: auth.User, status_code: int = 200
) -> JSONResponse:
    """Respuesta con el usuario y la cookie de sesión recién abierta.

    Se construye el `JSONResponse` a mano (en vez de devolver un dict y tocar
    la `Response` inyectada) porque la cookie tiene que viajar en ESTA
    respuesta: un objeto que se devuelve no arrastra cabeceras de otro.
    """
    token = auth.open_session(
        conn, user.id, user_agent=request.headers.get("user-agent")
    )
    response = JSONResponse(me_dict(conn, user), status_code=status_code)
    response.set_cookie(value=token, **_cookie_kwargs(request))
    return response


def _client_ip(request: Request) -> str:
    # `request.client` puede faltar. Si falta, se usa un comodín: el límite
    # entonces es por usuario, no por usuario+IP.
    return request.client.host if request.client else "?"


# --------------------------------------------------------------------- rutas


@router.get("/status")
def status(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Lo justo para que la pantalla sepa qué formulario pintar. Público."""
    user = auth.user_for_token(conn, request.cookies.get(auth.COOKIE_NAME))
    return {
        "needs_setup": auth.count_users(conn) == 0,
        "authenticated": user is not None,
    }


@router.post("/setup")
def setup(
    payload: SetupRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Alta del primer usuario, que es admin. Solo cuando no hay ninguno."""
    if auth.count_users(conn) > 0:
        raise HTTPException(
            403,
            "ya hay usuarios: entra por /auth/login (o crea más con POST /auth/users)",
        )
    try:
        user = auth.create_user(
            conn,
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
            color=payload.color,
            is_admin=True,
            avatar=payload.avatar,
        )
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit.record(conn, user, "user.created", target_type="user", target_id=user.id,
                 target_name=user.display_name, metadata={"admin": True, "first_user": True})
    return signed_in(conn, request, user, status_code=201)


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Usuario y contraseña -> cookie de sesión.

    Mismo 401 y mismo texto para «no existe» y «contraseña mala», y el mismo
    trabajo de hash en los dos caminos (`auth.authenticate` compara contra un
    hash falso cuando el usuario no está): el tiempo de respuesta no es un
    oráculo de qué usuarios existen.
    """
    # Los usernames se crean en minúsculas (`snake_case`), así que se acepta
    # que alguien escriba "Joaquin": el fallo de login no puede depender de una
    # mayúscula.
    username = (payload.username or "").strip().lower()
    ip = _client_ip(request)
    if auth.blocked(username, ip):
        raise HTTPException(429, "demasiados intentos: espera un minuto")
    auth.note_attempt(username, ip)

    user = auth.authenticate(conn, username, payload.password or "")
    if user is None:
        raise HTTPException(401, "usuario o contraseña incorrectos")

    auth.forget_attempts(username, ip)
    return signed_in(conn, request, user)


def me_dict(conn: sqlite3.Connection, user: auth.User) -> dict:
    """El usuario y sus permisos (`{permiso: "all" | [espacios]}`, plan de roles R2)."""
    from app import grants

    return {**user.as_dict(), "capabilities": grants.summary(conn, user)}


@router.get("/me")
def me(user: auth.User = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Quién está detrás de la sesión y qué permisos tiene. Lo usan el panel y
    la pantalla de login."""
    return me_dict(conn, user)


@router.get("/me/permissions")
def my_permissions(user: auth.User = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """«Qué puedo hacer» (plan de roles, R5): mi rol, mis espacios con su rol
    (directo o por un grupo) y mis permisos con su origen."""
    from app import grants

    espacios = []
    for r in conn.execute(
        "SELECT w.id, w.name, w.settings, m.role, m.subject_type, g.name AS group_name"
        " FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id"
        " LEFT JOIN groups g ON m.subject_type = 'group' AND g.id = m.subject_id"
        " WHERE (m.subject_type = 'user' AND m.subject_id = ?)"
        " OR (m.subject_type = 'group' AND m.subject_id IN (SELECT group_id FROM group_members WHERE user_id = ?))"
        " ORDER BY w.name COLLATE NOCASE",
        (user.id, user.id),
    ):
        if ws_mod.is_personal(r):
            continue
        espacios.append({
            "id": r["id"], "name": r["name"], "role": r["role"],
            "via": "group" if r["subject_type"] == "group" else "user", "group_name": r["group_name"],
        })
    return {
        "is_admin": user.is_admin,
        "workspaces": espacios,
        "capabilities": grants.explain(conn, user),
    }


@router.patch("/me")
def update_me(
    payload: MeUpdateRequest,
    user: auth.User = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Cambia el nombre visible propio (los ajustes de usuario).

    Cualquier usuario, sobre sí mismo: es lo mismo que hace un admin con
    `PATCH /auth/users/{id}`, pero sin poder tocar a nadie más ni el color. El
    `id` no cambia, así que las celdas `person` siguen apuntando a la misma
    persona y enseñan el nombre nuevo.
    """
    try:
        actualizado = auth.update_user(
            conn, user.id, display_name=payload.display_name, avatar=payload.avatar
        )
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    if actualizado is None:
        raise HTTPException(404, "el usuario de la sesión ya no existe")
    return me_dict(conn, actualizado)


@router.post("/logout", status_code=204)
def logout(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Cierra la sesión de esta cookie. Idempotente: dos veces no es un error."""
    auth.revoke_session(conn, request.cookies.get(auth.COOKIE_NAME))
    response = Response(status_code=204)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@router.post("/password")
def change_password(
    payload: PasswordRequest,
    request: Request,
    user: auth.User = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Cambia la contraseña propia, pidiendo la actual.

    Cierra el resto de sesiones del usuario y deja abierta esta: cambiar la
    contraseña es la forma de echar a quien estuviera dentro con la vieja.
    """
    row = conn.execute(
        "SELECT password_hash, salt FROM users WHERE id = ?", (user.id,)
    ).fetchone()
    if row is None or not auth.verify_password(
        payload.current_password or "", row["password_hash"], row["salt"]
    ):
        raise HTTPException(400, "la contraseña actual no es correcta")
    try:
        auth.set_password(conn, user.id, payload.new_password)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc

    auth.revoke_user_sessions(conn, user.id)
    return signed_in(conn, request, user)


@router.get("/tokens")
def list_capture_tokens(
    user: auth.User = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Los tokens de captura del usuario, **sin el secreto** (P13).

    `last_used_at` es la última vez que ese token capturó algo: la lista sirve
    para decidir qué llave se puede revocar sin miedo a romper un atajo.
    """
    return [token.as_dict() for token in capture_tokens.list_for_user(conn, user.id)]


@router.post("/tokens", status_code=201)
def create_capture_token(
    payload: TokenCreateRequest,
    user: auth.User = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Crea un token de captura. El secreto se enseña **una sola vez**.

    En la base queda su SHA-256, así que esta respuesta es la única ocasión en
    la que el token existe en claro: el panel lo enseña con un aviso y no lo
    vuelve a pedir (si se pierde, se revoca y se crea otro).
    """
    token, secret = capture_tokens.create(conn, user.id, name=payload.name)
    return {**token.as_dict(), "token": secret}


@router.delete("/tokens/{token_id}", status_code=204)
def revoke_capture_token(
    token_id: str,
    user: auth.User = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Revoca un token de captura del usuario. Idempotente a medias: 404 si no está.

    El `user_id` entra en el `WHERE` (ver `capture_tokens.revoke`): el id de un
    token ajeno no se puede revocar. Se responde 404 y no 403 para no decir
    siquiera si existía.
    """
    if not capture_tokens.revoke(conn, user.id, token_id):
        raise HTTPException(404, "no hay ningún token de captura con ese id")
    return Response(status_code=204)


@router.get("/users")
def list_users(
    user: auth.User = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Los usuarios, para el panel de administración (nombre, @usuario, color).

    Solo un admin: la lista no es un secreto, pero tampoco tiene por qué verla
    cualquiera. `as_dict()` no lleva hash ni salt.
    """
    if not user.is_admin:
        raise HTTPException(403, "solo un administrador puede listar los usuarios")
    return [u.as_dict() for u in auth.list_users(conn)]


@router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    payload: UserUpdateRequest,
    user: auth.User = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Renombra a un usuario o le cambia el color. Solo un admin.

    El `id` no se toca (es a lo que apuntan las celdas `person`): cambia el
    nombre visible, que es lo que se lee en las tablas, y no a quién apuntan.
    """
    if not user.is_admin:
        raise HTTPException(403, "solo un administrador puede editar usuarios")
    try:
        actualizado = auth.update_user(
            conn, user_id, display_name=payload.display_name, color=payload.color,
            avatar=payload.avatar, can_view_global_workload=payload.can_view_global_workload,
        )
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    if actualizado is None:
        raise HTTPException(404, "no hay ningún usuario con ese id")
    audit.record(conn, user, "user.updated", target_type="user", target_id=user_id,
                 target_name=actualizado.display_name,
                 metadata=payload.model_dump(exclude_none=True))
    return actualizado.as_dict()


@router.post("/users", status_code=201)
def create_user(
    payload: UserCreateRequest,
    user: auth.User = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Crea otro usuario. De momento solo un admin puede."""
    if not user.is_admin:
        raise HTTPException(403, "solo un administrador puede crear usuarios")
    try:
        created = auth.create_user(
            conn,
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
            color=payload.color,
            is_admin=payload.is_admin,
            avatar=payload.avatar,
            can_view_global_workload=payload.can_view_global_workload,
            workspaces=_workspaces_of_payload(conn, payload.workspaces),
        )
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit.record(conn, user, "user.created", target_type="user", target_id=created.id,
                 target_name=created.display_name, metadata={"admin": payload.is_admin})
    return created.as_dict()
