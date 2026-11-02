"""Arnés de tests: la app entera contra SQLite y un vault, sin servicios externos.

Antes de importar `app.main` se apunta el panel a un directorio que existe (la
imagen de Docker lo tiene en /app; aquí no hay /app), y cada test estrena su
propio fichero de índice **y su propio vault** en directorios temporales: desde
P1.2a guardar una nota escribe un `.md`, así que un vault compartido entre
tests haría que un reindexado se llevara notas de otro test. Desde P1.2b los
adjuntos también viven en el vault, así que no hay ninguna ruta fuera de él
que apuntar. Ni Docker, ni Postgres, ni red.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

# `app.main` monta los estáticos del panel al importarse: el directorio tiene
# que existir y no puede ser /app. Los adjuntos ya no tienen carpeta propia:
# viven dentro del vault de cada test (`_attachments/`).
_SESSION_TMP = Path(tempfile.mkdtemp(prefix="mdm-tests-"))
os.environ["FRONTEND_DIR"] = str(REPO_DIR / "frontend")
os.environ["VAULT_PATH"] = str(_SESSION_TMP / "vault")
# P8: el vigilante del vault se apaga por defecto en los tests. Encendido,
# cada `TestClient` deja un hilo sondeando el vault temporal y emitiendo
# avisos mientras el test hace sus propias cuentas (deriva, reindexado), que es
# ruido que ningún test de esta suite quiere. Los tests del vigilante lo
# encienden ellos mismos, con `WATCH_INTERVAL` bajo (ver
# `test_p8_watcher_sse.py`).
os.environ["WATCH_ENABLED"] = "0"
# P11a: el primer usuario se crea desde la pantalla de alta (o `ADMIN_USER`).
# En los tests se crea por el camino normal —`POST /auth/setup`— así que las
# dos variables se vacían para que un `.env` del desarrollador no cambie el
# arranque de la suite. `iniciar_sesion()` es lo que usa cada cliente de test.
os.environ.pop("ADMIN_USER", None)
os.environ.pop("ADMIN_PASSWORD", None)

# Credenciales del usuario de los tests. El `display_name` es "Tú" a propósito:
# es el valor que tenía `DEFAULT_USER_NAME` y lo que asertaban los tests de
# autoría anteriores a P11a; ahora sale de la sesión, no de un literal.
USUARIO_PRUEBA = "prueba"
NOMBRE_PRUEBA = "Tú"
CONTRASENA_PRUEBA = "contrasena-de-prueba"


def iniciar_sesion(client) -> None:
    """Deja `client` con sesión: alta del primer usuario o login.

    Es la forma de autenticar los tests **sin saltarse el middleware**: pasa
    por `/auth/setup` (índice sin usuarios) o por `/auth/login` (ya hay), que
    es exactamente lo que hace un navegador. Un test que necesite un cliente a
    mano llama a esto después de crearlo (ver el fixture `acceso`).
    """
    estado = client.get("/auth/status").json()
    if estado["needs_setup"]:
        respuesta = client.post(
            "/auth/setup",
            json={
                "username": USUARIO_PRUEBA,
                "display_name": NOMBRE_PRUEBA,
                "password": CONTRASENA_PRUEBA,
            },
        )
    else:
        respuesta = client.post(
            "/auth/login",
            json={"username": USUARIO_PRUEBA, "password": CONTRASENA_PRUEBA},
        )
    assert respuesta.status_code in (200, 201), respuesta.text


@pytest.fixture(autouse=True)
def _sin_intentos_acumulados():
    """El limitador de login es estado de módulo: se vacía por test.

    Sin esto, los logins de una suite de 200 tests se contarían juntos contra
    la misma clave (usuario+IP) y en mitad de la suite empezarían los 429.
    """
    from app import auth

    auth.reset_attempts()
    yield
    auth.reset_attempts()


@pytest.fixture()
def acceso():
    """El helper de login, para los tests que abren su propio `TestClient`."""
    return iniciar_sesion


@pytest.fixture(autouse=True)
def vault_path(tmp_path: Path, monkeypatch) -> Path:
    """Un vault vacío por test (autouse: nadie puede escribir en `/data`).

    Es autouse a propósito: `Repository(conn)` construye su `VaultStore` con
    `settings.vault_path`, y hay tests que usan el repositorio sin pasar por el
    cliente HTTP.
    """
    from app.config import settings

    path = tmp_path / "vault"
    path.mkdir()
    monkeypatch.setattr(settings, "vault_path", str(path))
    return path


@pytest.fixture()
def index_path(tmp_path: Path) -> Path:
    return tmp_path / "indice.sqlite"


@pytest.fixture()
def client(tmp_path: Path, index_path: Path, vault_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import settings

    monkeypatch.setattr(settings, "index_path", str(index_path))

    from app.main import app

    with TestClient(app) as test_client:
        iniciar_sesion(test_client)
        yield test_client
