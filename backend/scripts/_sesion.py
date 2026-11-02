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


def esperar_indice(client, limite_s: float = 60.0) -> None:
    """Espera a que el índice de arranque esté listo (P16).

    Desde P16 el reindexado va en un hilo y, hasta que termina, las rutas que
    leen el índice contestan `503`. Las de `/auth/*` y `/health` no, así que
    se puede esperar con el cliente ya creado. Con un vault de prueba son
    décimas de segundo; el tope está para no colgarse si algo va mal.
    """
    import time

    fin = time.monotonic() + limite_s
    while time.monotonic() < fin:
        estado = client.get("/health").json()
        if estado.get("status") == "ok":
            return
        if estado.get("status") == "error":
            raise SystemExit(
                "el índice de arranque falló: " + str(estado.get("detail") or estado)
            )
        time.sleep(0.05)
    raise SystemExit(f"el índice no estuvo listo en {limite_s:.0f} s")


def abrir_sesion(client) -> None:
    """Deja `client` con una sesión abierta (alta del primero o login)."""
    esperar_indice(client)
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
