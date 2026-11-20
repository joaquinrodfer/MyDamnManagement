"""La tipografía autoalojada se sirve sin sesión (la usa la pantalla de entrada),
pero solo ficheros `.woff2` de `/fonts/`, nada más del panel."""

from fastapi.testclient import TestClient


def test_la_fuente_se_sirve_sin_sesion_y_nada_mas(client):
    from app.main import app

    with TestClient(app) as anonimo:
        r = anonimo.get("/fonts/inter-latin-wght-normal.woff2")
        assert r.status_code == 200 and len(r.content) > 10000
        assert anonimo.get("/fonts/LICENSE-Inter.txt").status_code == 401
        assert anonimo.get("/fonts/%2e%2e/app.js").status_code in (401, 404)


def test_la_entrada_usa_inter(client):
    from app.main import app

    with TestClient(app) as anonimo:
        assert "/fonts/inter-latin-wght-normal.woff2" in anonimo.get("/login").text
