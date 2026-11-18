"""La marca Noteping: nombre, favicon, iconos y manifiesto.

El favicon y los iconos se piden sin sesión (la pantalla de entrada y la
instalación de la PWA los necesitan antes de entrar), pero solo esos ficheros:
el permiso sigue puesto en el nombre y no en el prefijo.
"""

from fastapi.testclient import TestClient


def _anonimo():
    from app.main import app

    return TestClient(app)


def test_favicon_e_iconos_sin_sesion(client):
    with _anonimo() as anonimo:
        for ruta, tipo in (
            ("/favicon.ico", "image/"),
            ("/icons/noteping-mark.svg", "image/svg+xml"),
            ("/icons/noteping-180.png", "image/png"),
            ("/icons/noteping-192.png", "image/png"),
            ("/icons/noteping-512.png", "image/png"),
        ):
            r = anonimo.get(ruta)
            assert r.status_code == 200, ruta
            assert r.headers["content-type"].startswith(tipo), (ruta, r.headers["content-type"])
        # Lo de fuera de la carpeta, no: ni con un `.svg` al final.
        for ruta in ("/icons/%2e%2e/app.js", "/icons/../index.html", "/icons/x.svg/../../app.js"):
            assert anonimo.get(ruta).status_code in (401, 404), ruta
        # Y el logo viejo ya no está.
        assert anonimo.get("/logo.png").status_code in (401, 404)


def test_el_manifiesto_es_de_noteping(client):
    with _anonimo() as anonimo:
        manifiesto = anonimo.get("/manifest.webmanifest").json()
    assert manifiesto["name"] == "Noteping"
    assert {i["src"] for i in manifiesto["icons"]} == {"/icons/noteping-192.png", "/icons/noteping-512.png"}
    for icono in manifiesto["icons"]:
        assert client.get(icono["src"]).status_code == 200


def test_el_nombre_en_el_panel_y_en_la_entrada(client):
    panel = client.get("/", headers={"Accept": "text/html"}).text
    assert "<title>Noteping</title>" in panel
    assert "MyDamnManagement" not in panel
    with _anonimo() as anonimo:
        entrada = anonimo.get("/login").text
    assert "· Noteping</title>" in entrada
    assert "MyDamnManagement" not in entrada
    assert 'href="/favicon.ico"' in entrada
