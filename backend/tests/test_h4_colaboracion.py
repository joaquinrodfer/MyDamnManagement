"""3.0, hito 4: la sala de edición en vivo (`WS /ws/pages/{id}`).

Lo que se fija aquí es el servidor: el protocolo (`join`, `update`, `ack`,
`reject`, `presence`), que la autoridad solo acepta cambios hechos sobre su
versión y pone al día a quien llega tarde, los permisos al entrar, el guardado
y que el documento que queda en la base (y en el export) es exactamente el que
han visto los clientes. La prueba con dos navegadores de verdad (el rebase lo
hace `@codemirror/collab` en el cliente) va aparte, en Playwright.
"""

import http.cookiejar
import json
import socket
import threading
import time
import urllib.request
import uuid

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from app import collab
from app.collab import BadChange, apply_changes
from conftest import CONTRASENA_PRUEBA, NOMBRE_PRUEBA, USUARIO_PRUEBA

# Esta prueba arranca un uvicorn de verdad en un hilo: el cliente de pruebas de
# Starlette abre cada WebSocket en su propio bucle de eventos (y lo cancela al
# cerrarlo), que no es como corre en producción --un solo bucle, salas
# compartidas por todas las conexiones-- y cortaría el guardado al salir.


# --------------------------------------------------------------------------
# aplicar un ChangeSet de CodeMirror (unidades UTF-16)
# --------------------------------------------------------------------------


def test_aplicar_cambios_retener_insertar_borrar():
    assert apply_changes("hola", [4, [0, " mundo"]]) == "hola mundo"
    assert apply_changes("hola mundo", [[5], 5]) == "mundo"
    assert apply_changes("ab", [1, [0, "x", "y"], 1]) == "ax\nyb"  # varias líneas
    assert apply_changes("abc", [[3, "z"]]) == "z"
    assert apply_changes("", [[0, "nuevo"]]) == "nuevo"


def test_las_longitudes_son_utf16_como_en_el_navegador():
    doc = "a😀b"  # el emoji mide 2 unidades UTF-16
    assert collab.utf16_len(doc) == 4
    assert apply_changes(doc, [3, [0, "!"], 1]) == "a😀!b"
    assert apply_changes(doc, [1, [2], 1]) == "ab"
    with pytest.raises(BadChange):
        apply_changes(doc, [2, [0, "x"], 2])  # partiría el emoji por la mitad


@pytest.mark.parametrize("malo", [
    [5],                    # se sale
    [3],                    # se queda corto
    [[9, "x"]],             # borra fuera
    "no es una lista",
    [True],
    [[1, 2]],               # texto que no es texto
    [-1, 5],
])
def test_un_cambio_que_no_encaja_se_rechaza(malo):
    with pytest.raises(BadChange):
        apply_changes("abcd", malo)


# --------------------------------------------------------------------------
# un servidor de verdad
# --------------------------------------------------------------------------


class Sesion:
    """Un usuario contra el servidor: HTTP con su cookie y salas por WebSocket."""

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method, path, body=None, expect=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, method=method, data=data,
                                   headers={"Content-Type": "application/json"})
        try:
            with self.http.open(r) as f:
                code, raw = f.status, f.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        if expect is not None:
            assert code == expect, f"{method} {path}: {code} {raw[:300]!r}"
        return json.loads(raw) if raw and raw[:1] in b"{[" else raw.decode()

    def cookie(self):
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    def sala(self, page_id, origin=None):
        headers = {"Cookie": self.cookie()} if list(self.jar) else {}
        if origin:
            headers["Origin"] = origin
        ws = connect(self.base.replace("http", "ws") + f"/ws/pages/{page_id}",
                     additional_headers=headers, open_timeout=5)
        return Sala(ws)


class Sala:
    def __init__(self, ws):
        self.ws = ws

    def send(self, msg):
        self.ws.send(json.dumps(msg))

    def recv(self, timeout=5):
        return json.loads(self.ws.recv(timeout=timeout))

    def close(self):
        self.ws.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _puerto_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def servidor(index_path, vault_path, monkeypatch):
    import uvicorn

    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "index_path", str(index_path))
    puerto = _puerto_libre()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=puerto, log_level="warning"))
    hilo = threading.Thread(target=server.run, daemon=True)
    hilo.start()
    base = f"http://127.0.0.1:{puerto}"
    limite = time.monotonic() + 30
    while time.monotonic() < limite:
        try:
            with urllib.request.urlopen(base + "/health") as f:
                if json.loads(f.read()).get("status") == "ok":
                    break
        except Exception:  # noqa: BLE001 - aún no escucha
            pass
        time.sleep(0.05)
    admin = Sesion(base)
    admin.req("POST", "/auth/setup", {"username": USUARIO_PRUEBA, "display_name": NOMBRE_PRUEBA,
                                      "password": CONTRASENA_PRUEBA}, expect=201)
    yield admin
    server.should_exit = True
    hilo.join(10)


# --------------------------------------------------------------------------
# protocolo
# --------------------------------------------------------------------------


def nota(admin, body="hola"):
    return admin.req("POST", "/pages", {"title": f"Sala {uuid.uuid4().hex[:6]}", "body_markdown": body}, expect=200)["id"]


def entrar(ws, **extra):
    ws.send({"type": "join", "lastSeenVersion": None, **extra})
    msg = ws.recv()
    assert msg["type"] == "joined", msg
    return msg


def esperar_cuerpo(admin, pid, esperado, timeout=5.0):
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        cuerpo = admin.req("GET", f"/pages/{pid}")["body_markdown"]
        if cuerpo == esperado:
            return cuerpo
        time.sleep(0.05)
    raise AssertionError(f"la base no llegó a {esperado!r}: {cuerpo!r}")


def test_dos_clientes_la_autoridad_ordena_y_rebasa(servidor):
    admin = servidor
    pid = nota(admin, "hola")
    with admin.sala(pid) as a, admin.sala(pid) as b:
        ja = entrar(a)
        assert ja["doc"] == "hola" and ja["version"] == 0 and ja["readonly"] is False
        jb = entrar(b)
        assert jb["version"] == 0

        # A y B escriben a la vez sobre la versión 0. Llega antes A.
        a.send({"type": "update", "version": 0, "updates": [{"clientID": "A", "changes": [4, [0, " A"]]}]})
        ack = a.recv()
        assert ack == {"type": "ack", "version": 0, "updates": [{"clientID": "A", "changes": [4, [0, " A"]]}]}
        assert b.recv()["type"] == "update"  # B se entera del cambio de A

        b.send({"type": "update", "version": 0, "updates": [{"clientID": "B", "changes": [[0, "B "], 4]}]})
        rechazo = b.recv()
        # Rechazado: B recibe lo que le falta para rebasar (el cambio de A).
        assert rechazo["type"] == "reject" and rechazo["version"] == 0
        assert rechazo["updates"][0]["clientID"] == "A"

        # B rebasa (aquí a mano: su inserción al principio no se mueve) y reenvía.
        b.send({"type": "update", "version": 1, "updates": [{"clientID": "B", "changes": [[0, "B "], 6]}]})
        assert b.recv()["type"] == "ack"
        de_b = a.recv()
        assert de_b["type"] == "update" and de_b["version"] == 1

    # La sala guarda lo que vieron los dos.
    assert esperar_cuerpo(admin, pid, "B hola A") == "B hola A"
    from app.routers.collab import rooms

    limite = time.monotonic() + 3
    while rooms.open_rooms and time.monotonic() < limite:
        time.sleep(0.05)
    assert rooms.open_rooms == 0  # sin clientes, la sala no se queda en memoria


def test_un_cambio_que_no_encaja_hace_recargar_al_cliente(servidor):
    admin = servidor
    pid = nota(admin, "abc")
    with admin.sala(pid) as a:
        entrar(a)
        a.send({"type": "update", "version": 0, "updates": [{"clientID": "A", "changes": [10, [0, "x"]]}]})
        msg = a.recv()
        assert msg["type"] == "reject" and msg["doc"] == "abc"
    assert admin.req("GET", f"/pages/{pid}")["body_markdown"] == "abc"


def test_reconectar_trae_solo_lo_que_falta(servidor):
    admin = servidor
    pid = nota(admin, "x")
    with admin.sala(pid) as a:
        ja = entrar(a)
        with admin.sala(pid) as b:
            entrar(b)
            b.send({"type": "update", "version": 0, "updates": [{"clientID": "B", "changes": [1, [0, "y"]]}]})
            b.recv()
            a.recv()
            # A «se cae» en la versión 0 y vuelve con su epoch: le llega solo el cambio.
            with admin.sala(pid) as a2:
                msg = entrar(a2, lastSeenVersion=0, epoch=ja["epoch"])
                assert "doc" not in msg and msg["version"] == 0 and len(msg["updates"]) == 1
                # Con otro epoch (la sala se reinició), el documento entero.
                with admin.sala(pid) as a3:
                    msg = entrar(a3, lastSeenVersion=0, epoch="otro")
                    assert msg["doc"] == "xy" and msg["version"] == 1


def test_presencia_llega_a_los_demas(servidor):
    admin = servidor
    pid = nota(admin, "hola")
    with admin.sala(pid) as a, admin.sala(pid) as b:
        ja = entrar(a)
        entrar(b)
        b.send({"type": "presence", "anchor": 2, "head": 2, "version": 0})
        msg = a.recv()
        assert msg["type"] == "presence" and msg["head"] == 2 and msg["user"]["name"]
        # Quien entra después ve dónde están los demás.
        with admin.sala(pid) as c:
            jc = entrar(c)
            assert any(p.get("head") == 2 for p in jc["peers"])
        # Y cuando alguien se va, los demás quitan su cursor.
        assert a.recv() == {"type": "presence", "peer": jc["peer"], "gone": True}


def test_un_cambio_por_fuera_reinicia_la_sala(servidor):
    """Un PATCH sin sala (otro cliente viejo) mientras la sala tiene cambios."""
    admin = servidor
    pid = nota(admin, "base")
    with admin.sala(pid) as a:
        entrar(a)
        admin.req("PATCH", f"/pages/{pid}", {"body_markdown": "de fuera"}, expect=200)
        a.send({"type": "update", "version": 0, "updates": [{"clientID": "A", "changes": [4, [0, "!"]]}]})
        assert a.recv()["type"] == "ack"
        # Al guardar, la sala ve que la base no tiene lo que ella guardó: se
        # reinicia con lo de la base y el cliente recarga. No se pisa lo de fuera.
        msg = a.recv()
        assert msg["type"] == "reject" and msg["doc"] == "de fuera" and msg["version"] == 0
    assert admin.req("GET", f"/pages/{pid}")["body_markdown"] == "de fuera"


# --------------------------------------------------------------------------
# permisos y seguridad
# --------------------------------------------------------------------------


@pytest.fixture()
def beto(servidor):
    alta = servidor.req("POST", "/auth/users", {
        "username": "beto", "display_name": "Beto", "password": "contrasena-beto",
    }, expect=201)
    sesion = Sesion(servidor.base)
    sesion.req("POST", "/auth/login", {"username": "beto", "password": "contrasena-beto"}, expect=200)
    return sesion, alta["id"]


def _cierre(sesion, pid, **kw):
    """El código con el que la sala cierra la conexión (tras aceptarla)."""
    with pytest.raises(ConnectionClosed) as e:
        with sesion.sala(pid, **kw) as ws:
            ws.recv()
    return e.value.rcvd.code


def test_sin_sesion_o_sin_permiso_no_se_entra(servidor, beto):
    admin = servidor
    pid = nota(admin)
    assert _cierre(Sesion(admin.base), pid) == 4401
    assert _cierre(beto[0], pid) == 4404  # privada del administrador: Beto no la ve
    assert _cierre(admin, str(uuid.uuid4())) == 4404


def test_otro_origen_no_entra(servidor):
    admin = servidor
    pid = nota(admin)
    assert _cierre(admin, pid, origin="https://malo.example") == 4403


def test_quien_solo_ve_entra_en_modo_lectura(servidor, beto):
    admin = servidor
    cliente_beto, beto_id = beto
    me = admin.req("GET", "/auth/me")
    pid = nota(admin, "solo lectura")
    admin.req("PUT", f"/acl/page/{pid}", {"inherit": False, "entries": [
        {"subject_type": "user", "subject_id": me["id"], "level": "owner"},
        {"subject_type": "user", "subject_id": beto_id, "level": "view"},
    ]}, expect=200)
    with cliente_beto.sala(pid) as ws:
        assert entrar(ws)["readonly"] is True
        ws.send({"type": "update", "version": 0, "updates": [{"clientID": "B", "changes": [12, [0, "!"]]}]})
        assert ws.recv()["reason"] == "readonly"
    assert admin.req("GET", f"/pages/{pid}")["body_markdown"] == "solo lectura"


def test_el_export_tras_editar_en_vivo_es_markdown_valido(servidor):
    admin = servidor
    pid = nota(admin, "# Título\n\ntexto")
    with admin.sala(pid) as a:
        entrar(a)
        a.send({"type": "update", "version": 0, "updates": [
            {"clientID": "A", "changes": [15, [0, "", "", "- uno 😀", "- dos"]]},
        ]})
        assert a.recv()["type"] == "ack"
    esperado = "# Título\n\ntexto\n\n- uno 😀\n- dos"
    esperar_cuerpo(admin, pid, esperado)
    md = admin.req("GET", f"/pages/{pid}/export.md")
    assert md.endswith(esperado + "\n") and md.startswith("---\n")
