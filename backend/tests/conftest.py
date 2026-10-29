"""Arnés de tests: la app entera contra SQLite, sin servicios externos.

Antes de importar `app.main` se apuntan el panel y los adjuntos a directorios
que existen (la imagen de Docker los tiene en /app; aquí no hay /app), y cada
test estrena su propio fichero de índice en un directorio temporal. Ni Docker,
ni Postgres, ni red.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

# `app.main` monta los estáticos al importarse: los directorios tienen que
# existir y no pueden ser /app.
_SESSION_TMP = Path(tempfile.mkdtemp(prefix="mdm-tests-"))
os.environ["FRONTEND_DIR"] = str(REPO_DIR / "frontend")
os.environ["ATTACHMENTS_DIR"] = str(_SESSION_TMP / "attachments")


@pytest.fixture()
def index_path(tmp_path: Path) -> Path:
    return tmp_path / "indice.sqlite"


@pytest.fixture()
def client(tmp_path: Path, index_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import settings

    monkeypatch.setattr(settings, "index_path", str(index_path))
    monkeypatch.setattr(settings, "attachments_dir", str(tmp_path / "attachments"))

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
