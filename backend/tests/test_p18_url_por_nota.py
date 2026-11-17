"""P18: cada nota con su dirección -- el backend sirve el panel en `/<id>`.

La navegación de la app pasa a vivir en la barra de direcciones (`/<id>`), así
que recargar ahí no puede dar 404. El comodín es el `StaticFiles` del panel, no
una ruta nueva: lo que no es un fichero **y parece una dirección del panel**
(`/<id>`, `/mis-tareas`) se sirve con el `index.html`. Lo que es una ruta de la
API o un fichero que no existe sigue dando su 404 -- el de la API, en JSON--:
si no, un error de la API se convertiría en una página del panel con un 200, que
es justo lo que rompe a cualquier cliente que no sea el navegador.
"""

import uuid

from app.main import _parece_ruta_de_panel

# --------------------------------------------------------------------------
# la regla, sola
# --------------------------------------------------------------------------


def test_la_regla_reconoce_las_direcciones_del_panel():
    assert _parece_ruta_de_panel(f"/{uuid.uuid4()}")
    assert _parece_ruta_de_panel("/mis-tareas")


def test_la_regla_no_se_lleva_ficheros_ni_rutas_de_la_api():
    # Ficheros del panel: se sirven como ficheros, no como el índice.
    assert not _parece_ruta_de_panel("/app.js")
    assert not _parece_ruta_de_panel("/logo.png")
    assert not _parece_ruta_de_panel("/manifest.webmanifest")
    # Rutas de la API (más de una pieza) y prefijos que existen.
    assert not _parece_ruta_de_panel("/api/no-existe")
    assert not _parece_ruta_de_panel("/databases/loquesea")
    assert not _parece_ruta_de_panel("/files/loquesea")
    assert not _parece_ruta_de_panel("/pages")
    # Ni la raíz (la sirve `html=True`) ni basura corta o con punto.
    assert not _parece_ruta_de_panel("/")
    assert not _parece_ruta_de_panel("/a")
    assert not _parece_ruta_de_panel("/nota.con.puntos")


# --------------------------------------------------------------------------
# con sesión: recargar en `/<id>` devuelve el panel
# --------------------------------------------------------------------------


def test_recargar_en_la_direccion_de_una_nota_sirve_el_panel(client):
    direccion = f"/{uuid.uuid4()}"
    respuesta = client.get(direccion, headers={"Accept": "text/html"})
    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("text/html")
    # El panel de verdad, no un 404 con el mismo tipo.
    assert "MyDamnManagement" in respuesta.text
    assert 'id="main"' in respuesta.text
    # Y sin caché: la app que se carga es la de ahora, no la de hace un rato.
    assert respuesta.headers["cache-control"] == "no-cache"


def test_mis_tareas_tambien_es_una_direccion_del_panel(client):
    respuesta = client.get("/mis-tareas", headers={"Accept": "text/html"})
    assert respuesta.status_code == 200
    assert "MyDamnManagement" in respuesta.text


def test_los_ficheros_del_panel_siguen_siendo_ficheros(client):
    respuesta = client.get("/app.js")
    assert respuesta.status_code == 200
    assert "javascript" in respuesta.headers["content-type"]
    assert respuesta.text.lstrip().startswith("/**") or "function" in respuesta.text


# --------------------------------------------------------------------------
# la API no se rompe
# --------------------------------------------------------------------------


def test_health_sigue_igual(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_auth_me_sigue_igual(client):
    respuesta = client.get("/auth/me")
    assert respuesta.status_code == 200
    assert respuesta.json()["username"]


def test_una_ruta_de_la_api_que_no_existe_sigue_siendo_un_404_en_json(client):
    respuesta = client.get("/api/no-existe")
    assert respuesta.status_code == 404
    assert respuesta.headers["content-type"].startswith("application/json")
    assert respuesta.json() == {"detail": "Not Found"}


def test_un_fichero_que_no_existe_sigue_siendo_un_404(client):
    """`/files/loquesea` es del router de adjuntos: sigue contestando él."""
    assert client.get("/files/loquesea").status_code == 404


def test_login_sigue_siendo_la_pantalla(client):
    # Con sesión abierta, `/login` devuelve al panel; lo que importa aquí es
    # que la ruta es la suya y no cae en el comodín.
    respuesta = client.get("/login", follow_redirects=False)
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/"


# --------------------------------------------------------------------------
# sin sesión: el enlace de una nota no se queda en blanco
# --------------------------------------------------------------------------


def test_una_direccion_de_nota_sin_sesion_lleva_al_login_y_vuelve(client):
    client.cookies.clear()
    direccion = f"/{uuid.uuid4()}"
    respuesta = client.get(direccion, headers={"Accept": "text/html"}, follow_redirects=False)
    assert respuesta.status_code == 303
    # Vuelve a la nota, no al inicio: el enlace sirve para mandarlo.
    assert respuesta.headers["location"] == f"/login?next={direccion}"


def test_una_direccion_de_nota_sin_sesion_y_sin_html_aceptado_es_401_json(client):
    """Un cliente que pide JSON (el `fetch` del panel) no recibe una redirección."""
    client.cookies.clear()
    respuesta = client.get(f"/{uuid.uuid4()}", headers={"Accept": "application/json"})
    assert respuesta.status_code == 401
    assert respuesta.json() == {"detail": "no autenticado"}
