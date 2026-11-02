"""La sesión que necesitan los `verify_*.py` desde P11a.

Desde que la API exige sesión, un script de verificación que hable con la app
tiene que entrar como lo haría una persona: `/auth/setup` la primera vez (su
índice temporal no tiene usuarios) y `/auth/login` cuando el usuario ya está.
El cliente de `TestClient` guarda la cookie él solo, así que con esto las
llamadas siguientes van autenticadas.

Cada script monta su propio vault e índice temporales, así que este usuario no
se cuela en la base de nadie. Después de borrar el índice —varias
verificaciones lo hacen a propósito— el arranque siguiente no tiene usuarios y
`/auth/setup` vuelve a crear el primero: el mismo camino que sigue la app.
"""

from __future__ import annotations

USUARIO = "verificador"
NOMBRE = "Verificador"
CONTRASENA = "contrasena-de-verificacion"


def abrir_sesion(client) -> None:
    """Deja `client` con una sesión abierta (alta del primero o login)."""
    estado = client.get("/auth/status").json()
    if estado["needs_setup"]:
        respuesta = client.post(
            "/auth/setup",
            json={
                "username": USUARIO,
                "display_name": NOMBRE,
                "password": CONTRASENA,
            },
        )
    else:
        respuesta = client.post(
            "/auth/login", json={"username": USUARIO, "password": CONTRASENA}
        )
    if respuesta.status_code not in (200, 201):
        raise SystemExit(
            "no se pudo abrir sesión para verificar: "
            f"{respuesta.status_code} {respuesta.text}"
        )
