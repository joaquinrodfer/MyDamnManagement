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

from app import auth, config
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


class PasswordRequest(BaseModel):
    current_password: str
    new_password: str


class UserCreateRequest(BaseModel):
    username: str
    display_name: str
    password: str
    color: Optional[str] = None
    is_admin: bool = False


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
    response = JSONResponse(user.as_dict(), status_code=status_code)
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
        )
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
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


@router.get("/me")
def me(user: auth.User = Depends(get_current_user)):
    """Quién está detrás de la sesión. Lo usan el panel y la pantalla de login."""
    return user.as_dict()


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
        )
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return created.as_dict()
