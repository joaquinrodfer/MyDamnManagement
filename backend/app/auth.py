"""Usuarios, sesiones y contraseñas (P11a): quién eres cuando llamas a la API.

Hasta P11a la API no tenía puerta: quien alcanzaba el puerto leía, escribía y
borraba, y el «creador» de cada nota era un literal (`DEFAULT_USER_NAME`). Este
módulo pone la identidad y el resto de la app la exige (`app.main`), sin tocar
el modelo de notas: `created_by` sigue siendo un texto, solo que ahora sale de
la sesión.

Decisiones que importan:

- **Contraseñas con la biblioteca estándar**, sin dependencias nuevas: `scrypt`
  (`hashlib.scrypt`, n=2**14, r=8, p=1 — 16 MiB por verificación) con una sal
  aleatoria por usuario. `salt` y `password_hash` se guardan en hexadecimal, en
  columnas separadas, para poder mover el algoritmo más adelante sin adivinar
  el formato de una cadena empaquetada.
- **Mismo tiempo para usuario inexistente y contraseña mala.** Un usuario que
  no existe se compara igualmente contra un hash falso (calculado al importar):
  si se respondiera antes, el tiempo de respuesta diría qué usuarios existen
  sin necesidad de acertar la contraseña.
- **En la base se guarda el hash del token de sesión**, nunca el token. Un
  volcado del índice (un backup, un `sqlite3` a mano) no da sesiones abiertas.
  El token es `secrets.token_urlsafe(32)` y su hash es SHA-256: no hace falta
  sal porque el token ya es aleatorio de 256 bits.
- **Caducidad deslizante de 30 días con tope absoluto de 90.** Cada petición
  autenticada empuja `expires_at` a `ahora + SESSION_DAYS`, pero nunca más allá
  de `created_at + 90 días`: una sesión que se usa todas las semanas no dura
  para siempre. Quien impone ese plazo es **la base**: la cookie del navegador
  lleva el tope absoluto como `Max-Age` (ver `app.routers.auth._cookie_kwargs`),
  así que no tira por su cuenta una sesión que la base todavía acepta, y una
  sesión inactiva 30 días deja de valer aunque el navegador siga mandándola.
- **El límite de intentos es en memoria** (usuario+IP, 10 por minuto). No
  sobrevive a un reinicio y no se comparte entre workers; para una app personal
  en un solo proceso es el coste correcto. Lo que evita es el relleno de
  contraseñas por fuerza bruta contra este proceso, no un ataque distribuido.

La tabla `users` vive en el índice (esquema 4). El índice se puede reconstruir
desde el vault, pero los usuarios **no** están en el vault: si se borra el
índice, la app vuelve a pedir el alta del primer usuario (o lo crea desde
`ADMIN_USER`/`ADMIN_PASSWORD`). El README lo dice en «Usuarios y acceso».
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import config

# Nombre de la cookie de sesión. Sin espacio ni acentos: acaba en cabeceras.
COOKIE_NAME = "mdm_session"

# Tope absoluto de una sesión, cuente lo que cuente `SESSION_DAYS`.
ABSOLUTE_DAYS = 90

# Parámetros de scrypt. Subirlos es un cambio de formato, no de esquema: cada
# fila guarda su sal y su hash, así que se pueden rehashear al siguiente login
# (no implementado aquí; hoy no hace falta).
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

# `snake_case`: minúsculas, dígitos y `_`, empezando por letra. 3-32 caracteres
# porque el nombre acaba en rutas de log y en la cabecera de la app.
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")

# Color del usuario: un `#rrggbb`. Si no se elige, sale de la paleta.
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
PALETTE = (
    "#5e6ad2", "#3dd68c", "#f5a623", "#f26464",
    "#9ba4ff", "#2ec4b6", "#c77dff", "#ff9f1c",
)

MIN_PASSWORD = 8
MAX_PASSWORD = 200
MAX_DISPLAY_NAME = 80
MAX_USER_AGENT = 400


class AuthError(ValueError):
    """Un alta, un cambio de contraseña o una sesión con datos no válidos.

    Es `ValueError` porque casi siempre lo provoca lo que llegó por HTTP: el
    router lo traduce a 400 con el motivo, en vez de dejar un 500.
    """


@dataclass(frozen=True)
class User:
    """El usuario de la sesión, sin nada que no deba salir de aquí.

    No lleva `password_hash` ni `salt` a propósito: este objeto viaja a los
    routers, a `request.state` y a la respuesta de `/auth/me`.
    """

    id: str
    username: str
    display_name: str
    color: Optional[str]
    is_admin: bool

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "color": self.color,
            "is_admin": self.is_admin,
        }


# --------------------------------------------------------------- contraseñas


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def hash_password(password: str, *, salt: Optional[bytes] = None) -> tuple[str, str]:
    """`(hash_hex, salt_hex)` para guardar. La sal se genera si no se pasa."""
    raw = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    return _scrypt(password, raw).hex(), raw.hex()


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    """¿Coincide `password` con el hash guardado? Nunca lanza por datos rotos."""
    try:
        salt = bytes.fromhex(salt_hex)
        esperado = bytes.fromhex(password_hash)
    except (TypeError, ValueError):
        return False
    if len(esperado) != SCRYPT_DKLEN:
        return False
    return hmac.compare_digest(_scrypt(password, salt), esperado)


# Hash falso contra el que se compara cuando el usuario no existe. Se calcula
# una vez al importar, con una contraseña que nadie conoce, para que el camino
# «usuario inexistente» cueste lo mismo que el de una contraseña mala.
_DUMMY_HASH, _DUMMY_SALT = hash_password(secrets.token_urlsafe(24))


def burn_password_check(password: str) -> None:
    """Gasta el mismo tiempo que una verificación real y descarta el resultado."""
    verify_password(password, _DUMMY_HASH, _DUMMY_SALT)


# ------------------------------------------------------------------ usuarios


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        color=row["color"],
        is_admin=bool(row["is_admin"]),
    )


def validate_username(username: str) -> str:
    value = (username or "").strip()
    if not USERNAME_RE.match(value):
        raise AuthError(
            "username no válido: minúsculas, dígitos y '_' (3-32 caracteres, "
            "empezando por una letra)"
        )
    return value


def validate_password(password: str) -> str:
    value = password or ""
    if len(value) < MIN_PASSWORD:
        raise AuthError(f"la contraseña tiene que tener al menos {MIN_PASSWORD} caracteres")
    if len(value) > MAX_PASSWORD:
        raise AuthError(f"la contraseña no puede pasar de {MAX_PASSWORD} caracteres")
    return value


def validate_display_name(display_name: str) -> str:
    value = (display_name or "").strip()
    if not value:
        raise AuthError("el nombre visible no puede estar vacío")
    if len(value) > MAX_DISPLAY_NAME:
        raise AuthError(f"el nombre visible no puede pasar de {MAX_DISPLAY_NAME} caracteres")
    return value


def validate_color(color: Optional[str]) -> Optional[str]:
    value = (color or "").strip()
    if not value:
        return None
    if not COLOR_RE.match(value):
        raise AuthError("color no válido: se espera un #rrggbb")
    return value.lower()


def default_color(username: str) -> str:
    """Color estable por usuario: el mismo nombre cae siempre en el mismo tono."""
    return PALETTE[int(hashlib.sha256(username.encode("utf-8")).hexdigest(), 16) % len(PALETTE)]


def count_users(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM users").fetchone()[0]


def get_user(conn: sqlite3.Connection, user_id: str) -> Optional[User]:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user(row) if row is not None else None


def get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[User]:
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
    ).fetchone()
    return _user(row) if row is not None else None


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    display_name: str,
    password: str,
    color: Optional[str] = None,
    is_admin: bool = False,
) -> User:
    """Crea un usuario. `AuthError` si el username ya existe o algo no vale."""
    username = validate_username(username)
    display_name = validate_display_name(display_name)
    validate_password(password)
    color = validate_color(color) or default_color(username)

    password_hash, salt = hash_password(password)
    user_id = secrets.token_hex(16)
    try:
        conn.execute(
            "INSERT INTO users (id, username, display_name, password_hash, salt,"
            " color, created_at, is_admin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                username,
                display_name,
                password_hash,
                salt,
                color,
                _iso(_now()),
                1 if is_admin else 0,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise AuthError(f"el username {username!r} ya existe") from exc
    created = get_user(conn, user_id)
    assert created is not None
    return created


def set_password(conn: sqlite3.Connection, user_id: str, password: str) -> None:
    validate_password(password)
    password_hash, salt = hash_password(password)
    conn.execute(
        "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
        (password_hash, salt, user_id),
    )


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> Optional[User]:
    """Usuario si la contraseña es suya, `None` en cualquier otro caso.

    El camino «no existe» también verifica contra un hash falso: el tiempo de
    respuesta no distingue los dos casos (ver el docstring del módulo).
    """
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
    ).fetchone()
    if row is None:
        burn_password_check(password or "")
        return None
    if not verify_password(password or "", row["password_hash"], row["salt"]):
        return None
    return _user(row)


# ------------------------------------------------------------------ sesiones


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def open_session(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    user_agent: Optional[str] = None,
    days: Optional[int] = None,
) -> str:
    """Abre una sesión y devuelve el token (que solo ve el cliente)."""
    days = config.settings.session_days if days is None else days
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = min(now + timedelta(days=days), now + timedelta(days=ABSOLUTE_DAYS))
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, last_seen,"
        " expires_at, user_agent) VALUES (?, ?, ?, ?, ?, ?)",
        (
            _token_hash(token),
            user_id,
            _iso(now),
            _iso(now),
            _iso(expires),
            (user_agent or "")[:MAX_USER_AGENT] or None,
        ),
    )
    return token


def user_for_token(
    conn: sqlite3.Connection, token: Optional[str], *, days: Optional[int] = None
) -> Optional[User]:
    """Usuario de la sesión, empujando la caducidad deslizante.

    Devuelve `None` si el token no existe, si la sesión caducó o si su usuario
    ya no está. En el caso de caducidad borra la fila: una sesión vencida no se
    queda en la tabla esperando a que alguien la limpie.

    El empujón escribe una fila, así que se hace **como mucho una vez por
    hora**: cargar el panel son ~8 peticiones (el HTML, `app.js`, el bundle y
    las imágenes) y no tiene sentido escribir la misma sesión ocho veces para
    mover una fecha 30 días. La ventana sigue siendo deslizante; solo no se
    afina al segundo.
    """
    if not token:
        return None
    token_hash = _token_hash(token)
    row = conn.execute(
        "SELECT s.created_at AS created_at, s.last_seen AS last_seen,"
        " s.expires_at AS expires_at, u.*"
        " FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None

    now = _now()
    created = _parse(row["created_at"])
    tope = created + timedelta(days=ABSOLUTE_DAYS)
    if now >= tope or now >= _parse(row["expires_at"]):
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        return None

    if now - _parse(row["last_seen"]) >= timedelta(hours=1):
        days = config.settings.session_days if days is None else days
        expires = min(now + timedelta(days=days), tope)
        conn.execute(
            "UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
            (_iso(now), _iso(expires), token_hash),
        )
    return _user(row)


def revoke_session(conn: sqlite3.Connection, token: Optional[str]) -> bool:
    if not token:
        return False
    cursor = conn.execute(
        "DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),)
    )
    return cursor.rowcount > 0


def revoke_user_sessions(
    conn: sqlite3.Connection, user_id: str, *, keep: Optional[str] = None
) -> None:
    """Cierra todas las sesiones de un usuario (menos la que se indique)."""
    if keep:
        conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (user_id, _token_hash(keep)),
        )
    else:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# ------------------------------------------------- límite de intentos (login)
# En memoria y por proceso: ver el docstring del módulo. La clave es
# (username, IP) para que un ataque contra una cuenta concreta no bloquee al
# resto de usuarios (ni al dueño) desde otra IP.

RATE_WINDOW = 60.0
RATE_MAX_ATTEMPTS = 10

_attempts: dict[tuple[str, str], list[float]] = {}
_attempts_lock = threading.Lock()


def _prune(stamps: list[float], now: float) -> list[float]:
    return [t for t in stamps if now - t < RATE_WINDOW]


def blocked(username: str, ip: str) -> bool:
    """¿Se ha pasado de intentos en la última ventana?"""
    key = ((username or "").strip(), ip or "")
    now = time.monotonic()
    with _attempts_lock:
        stamps = _prune(_attempts.get(key, []), now)
        if stamps:
            _attempts[key] = stamps
        else:
            _attempts.pop(key, None)
        return len(stamps) >= RATE_MAX_ATTEMPTS


def note_attempt(username: str, ip: str) -> None:
    key = ((username or "").strip(), ip or "")
    now = time.monotonic()
    with _attempts_lock:
        stamps = _prune(_attempts.get(key, []), now)
        stamps.append(now)
        _attempts[key] = stamps


def forget_attempts(username: str, ip: str) -> None:
    """Un login bueno limpia la cuenta: no se castiga al que se equivocó."""
    with _attempts_lock:
        _attempts.pop(((username or "").strip(), ip or ""), None)


def reset_attempts() -> None:
    """Vacía el limitador entero (lo usan los tests entre casos)."""
    with _attempts_lock:
        _attempts.clear()
