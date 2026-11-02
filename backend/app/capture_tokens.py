"""Tokens personales de captura (P13): la llave que usa un atajo del sistema.

El panel entra con la cookie de sesión, pero un atajo del sistema operativo
(Shortcuts en macOS, PowerToys en Windows, un script de Linux) no tiene cookie:
necesita un secreto que se pueda guardar en una variable de entorno
(`MDM_TOKEN`) y mandar en una cabecera. Ese es este token — y es del **usuario**,
no del agente.

Contrato:

- Se crea desde la sesión (`POST /auth/tokens`), se lista **sin el secreto**
  (`GET /auth/tokens`, con `last_used_at`) y se revoca (`DELETE
  /auth/tokens/{id}`). El secreto se enseña una sola vez, al crearlo: en la base
  no queda nada con lo que reconstruirlo.
- En la base se guarda el **SHA-256** del token, nunca el token, igual que las
  sesiones. No hace falta sal: el token ya es aleatorio de 256 bits, así que un
  volcado del índice (un backup, un `sqlite3` a mano) no da llaves de captura.
- El token **solo abre `POST /capture`**. `app.main` responde **403** en
  cualquier otra ruta (`user_token_is_scoped`), por el mismo motivo que un token
  de agente se queda en `/agent/*`: un secreto que vive en un script de
  escritorio no puede ser una llave maestra del vault.
- No caduca: se revoca a mano, que es lo que se espera de una llave guardada en
  el llavero. Vale mientras valga su usuario; si el usuario desaparece, el
  token también (`ON DELETE CASCADE`).
- `last_used_at` se mueve en cada captura, para que la lista diga cuál se usa y
  cuál es una llave olvidada.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app import auth

# Nombre con el que se reconoce la llave en la lista («Mac de casa»). Es una
# etiqueta para el dueño, no una credencial: cabe en una línea de interfaz.
MAX_NAME = 80

# Lo que se enseña del token cuando se crea. `secrets.token_urlsafe(32)` son 43
# caracteres sin espacios ni comas: cabe en una cabecera, en una variable de
# entorno y en un cuadro de texto sin escapes.
TOKEN_BYTES = 32


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CaptureToken:
    """Una fila de `capture_tokens`, sin el hash: lo que se puede enseñar."""

    id: str
    user_id: str
    name: Optional[str]
    created_at: str
    last_used_at: Optional[str]

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


@dataclass(frozen=True)
class ResolvedToken:
    """Un token válido y el usuario al que pertenece."""

    token: CaptureToken
    user: auth.User


def _row(row: sqlite3.Row) -> CaptureToken:
    return CaptureToken(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


def create(
    conn: sqlite3.Connection, user_id: str, *, name: Optional[str] = None
) -> tuple[CaptureToken, str]:
    """Crea un token y devuelve `(fila, secreto)`. El secreto no se guarda."""
    clean = (name or "").strip()[:MAX_NAME] or None
    token = secrets.token_urlsafe(TOKEN_BYTES)
    token_id = secrets.token_hex(16)
    conn.execute(
        "INSERT INTO capture_tokens (id, user_id, name, token_hash, created_at,"
        " last_used_at) VALUES (?, ?, ?, ?, ?, NULL)",
        (token_id, user_id, clean, _token_hash(token), _iso(_now())),
    )
    row = conn.execute(
        "SELECT * FROM capture_tokens WHERE id = ?", (token_id,)
    ).fetchone()
    assert row is not None
    return _row(row), token


def list_for_user(conn: sqlite3.Connection, user_id: str) -> list[CaptureToken]:
    """Los tokens de un usuario, el más reciente primero. Sin el secreto."""
    rows = conn.execute(
        "SELECT * FROM capture_tokens WHERE user_id = ?"
        " ORDER BY created_at DESC, rowid DESC",
        (user_id,),
    ).fetchall()
    return [_row(row) for row in rows]


def revoke(conn: sqlite3.Connection, user_id: str, token_id: str) -> bool:
    """Borra un token del usuario. `False` si no era suyo o ya no estaba.

    El `user_id` va en el `WHERE` a propósito: un id de token ajeno no se puede
    revocar aunque se adivine, y el router responde 404 (no 403) para no decir
    siquiera si existía.
    """
    cursor = conn.execute(
        "DELETE FROM capture_tokens WHERE id = ? AND user_id = ?",
        (token_id, user_id),
    )
    return cursor.rowcount > 0


def resolve(conn: sqlite3.Connection, token: Optional[str]) -> Optional[ResolvedToken]:
    """Token presentado -> `(fila, usuario)`, o `None` si no vale.

    No lanza nunca: lo usa el middleware, donde un token desconocido no es un
    error sino «esto no es una llave de captura» (y entonces decide la puerta de
    la sesión). El `JOIN` con `users` hace que un token cuyo usuario ya no está
    no valga aunque la fila siguiera viva.
    """
    if not token:
        return None
    row = conn.execute(
        "SELECT t.*, u.username AS u_username, u.display_name AS u_display_name,"
        " u.color AS u_color, u.is_admin AS u_is_admin"
        " FROM capture_tokens t JOIN users u ON u.id = t.user_id"
        " WHERE t.token_hash = ?",
        (_token_hash(token),),
    ).fetchone()
    if row is None:
        return None
    user = auth.User(
        id=row["user_id"],
        username=row["u_username"],
        display_name=row["u_display_name"],
        color=row["u_color"],
        is_admin=bool(row["u_is_admin"]),
    )
    return ResolvedToken(token=_row(row), user=user)


def mark_used(conn: sqlite3.Connection, token_id: str) -> None:
    """Apunta que el token acaba de capturar algo (`last_used_at`)."""
    conn.execute(
        "UPDATE capture_tokens SET last_used_at = ? WHERE id = ?",
        (_iso(_now()), token_id),
    )
