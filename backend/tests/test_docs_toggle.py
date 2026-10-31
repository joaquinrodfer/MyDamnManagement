"""`DOCS_ENABLED` apaga la documentación interactiva (deuda anotada en P7).

`Settings.docs_enabled` se traduce en `docs_url=None`, `redoc_url=None` y
`openapi_url=None` al construir la app (`app.main`). Como `settings = Settings()`
y `app = FastAPI(...)` son singletons de módulo, probar los dos estados obliga
a recargar `app.config` y `app.main` con la variable de entorno puesta. El
recarga se deshace al final de cada test guardando y restaurando el contenido
de los módulos: si se quedara recargado, los demás tests (que hacen
`from app.main import app` al fijarse fixtures) verían otro `app` y otro
`settings` a mitad de sesión.
"""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def build_app(tmp_path: Path, monkeypatch):
    """Factory: `build_app("0")` devuelve una app con `DOCS_ENABLED=0`.

    `build_app(None)` no fija la variable (prueba el valor por defecto).
    El teardown restaura los módulos originales en su estado original.
    """
    import app.config
    import app.main

    # Snapshot del estado actual: tras el recarga se restaura palabra por
    # palabra (importlib.reload reejecuta el código en el MISMO objeto módulo,
    # así que "deshacer" es devolver su __dict__ al de antes).
    saved = {
        name: dict(sys.modules[name].__dict__) for name in ("app.config", "app.main")
    }

    def build(docs_enabled):
        if docs_enabled is None:
            monkeypatch.delenv("DOCS_ENABLED", raising=False)
        else:
            monkeypatch.setenv("DOCS_ENABLED", docs_enabled)
        # config: `settings = Settings()` vuelve a leer el entorno.
        importlib.reload(sys.modules["app.config"])
        # main: construye un `app` nuevo con ese settings (routers, arranque
        # y estáticos incluidos).
        importlib.reload(sys.modules["app.main"])
        # El arranque del TestClient usa estas rutas: al tmp del test, nunca
        # a /data (el fixture autouse del conftest solo parchea el settings
        # original, no este recién creado).
        reloaded = sys.modules["app.config"].settings
        reloaded.index_path = str(tmp_path / "indice.sqlite")
        reloaded.vault_path = str(tmp_path / "vault")
        (tmp_path / "vault").mkdir(exist_ok=True)
        return sys.modules["app.main"].app

    yield build

    for name, snapshot in saved.items():
        sys.modules[name].__dict__.clear()
        sys.modules[name].__dict__.update(snapshot)


def test_docs_apagado_responde_404_y_health_sigue_ok(build_app):
    """`DOCS_ENABLED=0`: las tres rutas de docs dan 404; /health no se entera."""
    app = build_app("0")
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/health").status_code == 200


def test_docs_encendido_por_defecto(build_app):
    """Sin `DOCS_ENABLED` en el entorno (valor por defecto): /docs responde 200."""
    app = build_app(None)
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 200
