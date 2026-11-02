"""P11a: usuarios, sesiones y login — la API deja de estar abierta.

Lo que se fija aquí, en el orden en que importa:

1. **La puerta.** Sin sesión, la API contesta 401 `{"detail": "no
   autenticado"}` y la navegación (`Accept: text/html`) va al login.
   `/health` sigue abierto y `/agent/*` conserva su token acotado, que no
   abre nada más.
2. **La entrada.** Alta del primer usuario (admin) solo cuando no hay
   ninguno; login con la misma respuesta y el mismo trabajo para usuario
   inexistente y contraseña mala; cookie `HttpOnly`/`SameSite=Lax`/`Path=/`
   (y `Secure` detrás de HTTPS); límite de intentos por usuario+IP.
3. **Lo que se guarda.** En `sessions` está el SHA-256 del token, nunca el
   token. La contraseña no se guarda en claro en ningún sitio.
4. **Quién eres.** `created_by` sale de la sesión (nota y frontmatter), no de
   un literal.
5. **Que no se pierde lo de antes.** Una base v3 migra a la 4 conservando sus
   notas y ganando `users`/`sessions`.

Los tests se autentican por HTTP (`/auth/setup` o `/auth/login`, ver
`iniciar_sesion` en `conftest.py`): nada de saltarse el middleware.
"""

import hashlib
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import auth
from app.config import settings
from conftest import CONTRASENA_PRUEBA, NOMBRE_PRUEBA, USUARIO_PRUEBA

SESION = auth.COOKIE_NAME


def _abrir(index_path: Path) -> sqlite3.Connection:
    from app.index import open_index

    return open_index(str(index_path))


def _cookie(client) -> str:
    return client.cookies[SESION]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture()
def cliente_virgen(tmp_path, vault_path, monkeypatch):
    """App arrancada sobre un índice sin usuarios (primer arranque de verdad)."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "index_path", str(tmp_path / "virgen.sqlite"))

    from app.main import app

    with TestClient(app) as client:
        yield client


# --------------------------------------------------------------------------
# 1. la puerta
# --------------------------------------------------------------------------


def test_sin_sesion_la_api_contesta_401(client):
    client.cookies.clear()
    respuesta = client.get("/pages")
    assert respuesta.status_code == 401
    assert respuesta.json() == {"detail": "no autenticado"}
    # No es un 401 vacío: el `fetch` del panel necesita saber que es la puerta.
    assert respuesta.headers["content-type"].startswith("application/json")


def test_health_no_pide_sesion(client):
    client.cookies.clear()
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health").status_code == 200


def test_navegar_a_la_raiz_sin_sesion_lleva_al_login(client):
    """La SPA no se rompe: `/` con sesión sirve el panel, sin ella va al login."""
    client.cookies.clear()
    respuesta = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/login"


def test_la_navegacion_directa_a_una_ruta_cualquiera_lleva_al_login_con_vuelta(client):
    client.cookies.clear()
    respuesta = client.get(
        "/api-que-no-existe?x=1", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/login?next=/api-que-no-existe%3Fx%3D1"


def test_los_estaticos_del_panel_tambien_piden_sesion(client):
    """`app.js` no es público: sin sesión no hay panel que cargar.

    El logo sí: es el favicon de la propia pantalla de entrada, que se pinta
    antes de que exista sesión.
    """
    client.cookies.clear()
    assert client.get("/app.js").status_code == 401
    logo = client.get("/logo.png")
    assert logo.status_code == 200
    assert logo.headers["content-type"].startswith("image/")


def test_con_sesion_la_raiz_sirve_el_panel(client):
    respuesta = client.get("/", headers={"Accept": "text/html"})
    assert respuesta.status_code == 200
    assert "MyDamnManagement" in respuesta.text


def test_el_login_con_sesion_abierta_devuelve_al_panel(client):
    respuesta = client.get("/login", follow_redirects=False)
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/"


# --------------------------------------------------------------------------
# 2. el primer arranque y la pantalla de entrada
# --------------------------------------------------------------------------


def test_el_primer_arranque_pide_el_alta_del_administrador(cliente_virgen):
    estado = cliente_virgen.get("/auth/status").json()
    assert estado == {"needs_setup": True, "authenticated": False}

    pantalla = cliente_virgen.get("/login")
    assert pantalla.status_code == 200
    assert pantalla.headers["content-type"].startswith("text/html")
    assert "Crea el primer usuario" in pantalla.text
    # La misma página se dibuja con el tema del panel y sin dependencias.
    assert "--accent" in pantalla.text
    assert "cdn" not in pantalla.text.lower()

    alta = cliente_virgen.post(
        "/auth/setup",
        json={
            "username": "primero",
            "display_name": "Primero",
            "password": "contrasena-del-primero",
        },
    )
    assert alta.status_code == 201, alta.text
    assert alta.json()["is_admin"] is True
    assert alta.json()["username"] == "primero"

    # El alta deja sesión abierta: se puede usar la app sin volver a entrar.
    assert cliente_virgen.get("/auth/me").json()["display_name"] == "Primero"
    assert cliente_virgen.get("/pages").json() == []

    # Y no se repite: ya hay usuarios.
    repetida = cliente_virgen.post(
        "/auth/setup",
        json={"username": "otro", "display_name": "Otro", "password": "otra-contrasena"},
    )
    assert repetida.status_code == 403


def test_el_administrador_se_crea_desde_el_entorno_y_no_se_resetea(
    tmp_path, vault_path, monkeypatch
):
    """`ADMIN_USER`/`ADMIN_PASSWORD` solo valen para el primer arranque.

    El segundo arranque, ya con usuarios, no toca nada: cambiar la contraseña
    en el `.env` no resetea la de nadie (ni reescribe el usuario), que es el
    fallo que un operador no perdona.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "index_path", str(tmp_path / "entorno.sqlite"))
    monkeypatch.setattr(settings, "admin_user", "jefa")
    monkeypatch.setattr(settings, "admin_password", "contrasena-de-entorno")

    with TestClient(app) as client:
        assert client.get("/auth/status").json()["needs_setup"] is False
        assert "Crea el primer usuario" not in client.get("/login").text
        entrada = client.post(
            "/auth/login",
            json={"username": "jefa", "password": "contrasena-de-entorno"},
        )
        assert entrada.status_code == 200, entrada.text
        assert entrada.json()["is_admin"] is True
        assert entrada.json()["display_name"] == "jefa"

    # Otro valor en la variable, mismo índice: la contraseña de verdad no cambia.
    monkeypatch.setattr(settings, "admin_password", "otra-que-nadie-usó")
    with TestClient(app) as client:
        assert (
            client.post(
                "/auth/login",
                json={"username": "jefa", "password": "contrasena-de-entorno"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/auth/login",
                json={"username": "jefa", "password": "otra-que-nadie-usó"},
            ).status_code
            == 401
        )


def test_un_admin_user_invalido_no_deja_arrancar(tmp_path, vault_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "index_path", str(tmp_path / "malo.sqlite"))
    monkeypatch.setattr(settings, "admin_user", "Jefa Mala")
    monkeypatch.setattr(settings, "admin_password", "contrasena-de-entorno")

    with pytest.raises(RuntimeError) as excinfo:
        with TestClient(app):
            pass
    assert "ADMIN_USER" in str(excinfo.value)


def test_la_pantalla_con_usuarios_pide_login(cliente_virgen):
    cliente_virgen.post(
        "/auth/setup",
        json={"username": "primero", "display_name": "Primero", "password": "contrasena"},
    )
    cliente_virgen.cookies.clear()
    pantalla = cliente_virgen.get("/login")
    assert "Crea el primer usuario" not in pantalla.text
    assert 'data-endpoint="/auth/login"' in pantalla.text


def test_el_alta_valida_el_username_snake_case(cliente_virgen):
    respuesta = cliente_virgen.post(
        "/auth/setup",
        json={"username": "No Vale", "display_name": "X", "password": "contrasena"},
    )
    assert respuesta.status_code == 400
    assert "username" in respuesta.json()["detail"]



# --------------------------------------------------------------------------
# 3. login: misma respuesta para todos los fallos y límite de intentos
# --------------------------------------------------------------------------


def test_login_correcto_abre_sesion_con_cookie_httponly(client, index_path):
    client.cookies.clear()
    respuesta = client.post(
        "/auth/login", json={"username": USUARIO_PRUEBA, "password": CONTRASENA_PRUEBA}
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["display_name"] == NOMBRE_PRUEBA
    assert respuesta.json()["is_admin"] is True

    cookie = respuesta.headers["set-cookie"]
    assert SESION in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    # http en el cliente de test: `Secure` se reserva para HTTPS (ver abajo).
    assert "Secure" not in cookie

    # En la base está el hash, no el token.
    token = _cookie(client)
    assert token
    conn = sqlite3.connect(index_path)
    try:
        guardados = {r[0] for r in conn.execute("SELECT token_hash FROM sessions")}
    finally:
        conn.close()
    assert token not in guardados
    assert _token_hash(token) in guardados

    assert client.get("/auth/me").status_code == 200


def test_la_cookie_sale_secure_detras_de_https(client):
    client.cookies.clear()
    respuesta = client.post(
        "/auth/login",
        json={"username": USUARIO_PRUEBA, "password": CONTRASENA_PRUEBA},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert respuesta.status_code == 200
    assert "Secure" in respuesta.headers["set-cookie"]


def test_login_malo_y_usuario_inexistente_responden_igual(client):
    client.cookies.clear()
    mala = client.post(
        "/auth/login", json={"username": USUARIO_PRUEBA, "password": "no-es-la-buena"}
    )
    fantasma = client.post(
        "/auth/login", json={"username": "no_existe", "password": "no-es-la-buena"}
    )
    assert mala.status_code == fantasma.status_code == 401
    assert mala.json() == fantasma.json() == {"detail": "usuario o contraseña incorrectos"}
    # Y ninguno de los dos deja sesión abierta.
    assert client.get("/auth/me").status_code == 401


def test_el_login_no_distingue_por_tiempo(client):
    """El usuario inexistente también gasta un scrypt: no hay oráculo por tiempo.

    El margen es ancho (3x) a propósito: lo que se comprueba es que los dos
    caminos hacen el mismo trabajo, no la precisión del reloj de la máquina.
    """
    client.cookies.clear()

    def mediana(payload, veces=3):
        tiempos = []
        for _ in range(veces):
            inicio = time.perf_counter()
            client.post("/auth/login", json=payload)
            tiempos.append(time.perf_counter() - inicio)
        tiempos.sort()
        return tiempos[len(tiempos) // 2]

    existente = mediana({"username": USUARIO_PRUEBA, "password": "no-es-la-buena"})
    fantasma = mediana({"username": "no_existe", "password": "no-es-la-buena"})
    assert fantasma <= existente * 3, (existente, fantasma)
    assert existente <= fantasma * 3, (existente, fantasma)


def test_el_limite_de_intentos_por_usuario_e_ip(client):
    client.cookies.clear()
    for _ in range(auth.RATE_MAX_ATTEMPTS):
        assert (
            client.post(
                "/auth/login", json={"username": USUARIO_PRUEBA, "password": "mala"}
            ).status_code
            == 401
        )

    # El siguiente intento, aunque la contraseña sea la buena, es 429.
    respuesta = client.post(
        "/auth/login", json={"username": USUARIO_PRUEBA, "password": CONTRASENA_PRUEBA}
    )
    assert respuesta.status_code == 429
    assert "intentos" in respuesta.json()["detail"]

    # Otro usuario desde la misma IP no paga el bloqueo del primero.
    otro = client.post("/auth/login", json={"username": "no_existe", "password": "mala"})
    assert otro.status_code == 401

    # Y el limitador se olvida tras un login bueno (aquí, vaciándolo, que es
    # lo que hace el fixture autouse entre tests).
    auth.reset_attempts()
    assert (
        client.post(
            "/auth/login", json={"username": USUARIO_PRUEBA, "password": CONTRASENA_PRUEBA}
        ).status_code
        == 200
    )


# --------------------------------------------------------------------------
# 4. sesiones: /me, logout, cambio de contraseña, caducidad
# --------------------------------------------------------------------------


def test_me_devuelve_el_usuario_de_la_sesion(client):
    datos = client.get("/auth/me").json()
    assert set(datos) == {"id", "username", "display_name", "color", "is_admin"}
    assert datos["username"] == USUARIO_PRUEBA
    assert datos["display_name"] == NOMBRE_PRUEBA
    assert datos["color"].startswith("#")
    assert datos["is_admin"] is True
    # Nada de credenciales en la respuesta.
    assert "password_hash" not in datos and "salt" not in datos


def test_logout_borra_la_sesion_en_la_base(client, index_path):
    token = _cookie(client)
    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/me").status_code == 401
    conn = sqlite3.connect(index_path)
    try:
        assert (
            conn.execute(
                "SELECT count(*) FROM sessions WHERE token_hash = ?", (_token_hash(token),)
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_la_sesion_caducada_no_se_reutiliza(client, index_path):
    token = _cookie(client)
    ayer = datetime.now(timezone.utc) - timedelta(days=1)
    conn = _abrir(index_path)
    try:
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (ayer.isoformat(), _token_hash(token)),
        )
    finally:
        conn.close()

    assert client.get("/auth/me").status_code == 401
    conn = sqlite3.connect(index_path)
    try:
        # La fila vencida se borra: no se queda esperando a que alguien limpie.
        assert (
            conn.execute(
                "SELECT count(*) FROM sessions WHERE token_hash = ?", (_token_hash(token),)
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_la_caducidad_se_desliza_con_el_uso(client, index_path):
    token = _cookie(client)
    ahora = datetime.now(timezone.utc)
    conn = _abrir(index_path)
    try:
        conn.execute(
            "UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
            (
                (ahora - timedelta(days=29)).isoformat(),
                (ahora + timedelta(hours=1)).isoformat(),
                _token_hash(token),
            ),
        )
    finally:
        conn.close()

    assert client.get("/auth/me").status_code == 200

    conn = sqlite3.connect(index_path)
    try:
        expires_at = datetime.fromisoformat(
            conn.execute(
                "SELECT expires_at FROM sessions WHERE token_hash = ?",
                (_token_hash(token),),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    # Empujada a ~30 días vista, no a la hora que quedaba.
    assert expires_at > ahora + timedelta(days=29)


def test_el_tope_absoluto_corta_aunque_la_sesion_se_use(client, index_path):
    """Una sesión de hace 91 días no revive porque se empuje `expires_at`."""
    token = _cookie(client)
    ahora = datetime.now(timezone.utc)
    conn = _abrir(index_path)
    try:
        conn.execute(
            "UPDATE sessions SET created_at = ?, expires_at = ? WHERE token_hash = ?",
            (
                (ahora - timedelta(days=91)).isoformat(),
                (ahora + timedelta(days=30)).isoformat(),
                _token_hash(token),
            ),
        )
    finally:
        conn.close()

    assert client.get("/auth/me").status_code == 401


def test_cambiar_la_contrasena_pide_la_actual_y_cierra_las_otras_sesiones(client):
    assert (
        client.post(
            "/auth/password",
            json={"current_password": "no-es-esta", "new_password": "otra-contrasena"},
        ).status_code
        == 400
    )

    # Una segunda sesión del mismo usuario, para comprobar que se cierra.
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as otro:
        assert (
            otro.post(
                "/auth/login",
                json={"username": USUARIO_PRUEBA, "password": CONTRASENA_PRUEBA},
            ).status_code
            == 200
        )
        cambio = client.post(
            "/auth/password",
            json={
                "current_password": CONTRASENA_PRUEBA,
                "new_password": "la-contrasena-nueva",
            },
        )
        assert cambio.status_code == 200, cambio.text
        # La sesión que cambió sigue viva; la otra no.
        assert client.get("/auth/me").status_code == 200
        assert otro.get("/auth/me").status_code == 401

    # La vieja contraseña ya no entra; la nueva sí.
    client.cookies.clear()
    assert (
        client.post(
            "/auth/login", json={"username": USUARIO_PRUEBA, "password": CONTRASENA_PRUEBA}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/login",
            json={"username": USUARIO_PRUEBA, "password": "la-contrasena-nueva"},
        ).status_code
        == 200
    )


# --------------------------------------------------------------------------
# 5. autoría real y usuarios nuevos
# --------------------------------------------------------------------------


def test_created_by_es_el_usuario_de_la_sesion(client, vault_path):
    alta = client.post(
        "/auth/users",
        json={
            "username": "marta",
            "display_name": "Marta",
            "password": "contrasena-de-marta",
        },
    )
    assert alta.status_code == 201, alta.text
    assert alta.json()["is_admin"] is False

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as marta:
        assert (
            marta.post(
                "/auth/login", json={"username": "marta", "password": "contrasena-de-marta"}
            ).status_code
            == 200
        )
        nota = marta.post(
            "/pages", json={"title": "De Marta", "body_markdown": "hola"}
        ).json()
        assert nota["created_by"] == "Marta"

        # Un usuario normal no crea usuarios.
        assert (
            marta.post(
                "/auth/users",
                json={"username": "otro", "display_name": "Otro", "password": "contrasena"},
            ).status_code
            == 403
        )

    # El fichero también lo dice: la autoría no vive solo en el índice.
    texto = (vault_path / "Notas" / "De Marta.md").read_text(encoding="utf-8")
    assert "created_by: Marta" in texto


def test_crear_un_usuario_repetido_es_400(client):
    respuesta = client.post(
        "/auth/users",
        json={
            "username": USUARIO_PRUEBA,
            "display_name": "Repetida",
            "password": "otra-contrasena",
        },
    )
    assert respuesta.status_code == 400
    assert "ya existe" in respuesta.json()["detail"]


# --------------------------------------------------------------------------
# 6. agentes: su token sigue valiendo, pero solo para lo suyo
# --------------------------------------------------------------------------


@pytest.fixture()
def agentes(monkeypatch):
    monkeypatch.setattr(settings, "agent_tokens", "tok-prueba:agente-prueba")


def test_un_agente_sigue_sin_sesion_en_agent(client, agentes):
    nota = client.post("/pages", json={"title": "Contexto", "body_markdown": "cuerpo"}).json()
    client.cookies.clear()

    respuesta = client.get(
        "/agent/context",
        params={"page_id": nota["id"]},
        headers={"X-Agent-Token": "tok-prueba"},
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["page"]["title"] == "Contexto"


def test_el_token_de_agente_no_abre_la_api_del_dueno(client, agentes):
    client.cookies.clear()
    assert client.get("/pages", headers={"X-Agent-Token": "tok-prueba"}).status_code == 403
    # Ni siquiera las rutas de autenticación.
    assert client.get("/auth/me", headers={"X-Agent-Token": "tok-prueba"}).status_code == 403


# --------------------------------------------------------------------------
# 7. migración v3 -> v4 sobre una base existente
# --------------------------------------------------------------------------


def test_una_base_v3_migra_a_la_4_y_conserva_sus_notas(client, index_path):
    from app.index import SCHEMA_VERSION, ensure_index

    nota = client.post("/pages", json={"title": "Antes de la 4"}).json()

    # Un índice tal y como lo dejó la v1.2 (esquema 3): sin users ni sessions.
    conn = sqlite3.connect(index_path)
    try:
        conn.execute("DROP TABLE sessions")
        conn.execute("DROP TABLE users")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    info = ensure_index(str(index_path))
    assert info.version == SCHEMA_VERSION == 4

    conn = sqlite3.connect(index_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        tablas = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"users", "sessions"} <= tablas
        # Las notas que había siguen ahí, y sus columnas son las de antes.
        assert (
            conn.execute("SELECT title FROM pages WHERE id = ?", (nota["id"],)).fetchone()[0]
            == "Antes de la 4"
        )
        # Las tablas nuevas nacen vacías: nadie inventa usuarios al migrar.
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    finally:
        conn.close()
