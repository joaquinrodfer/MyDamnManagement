"""H9: webhooks salientes por espacio.

URL segura (anti-SSRF), secreto que solo se ve una vez, firma, eventos de las
bases de Tareas, reintentos y entrega real contra un servidor local.
"""

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import webhooks
from app.config import settings
from app.index import DEFAULT_WORKSPACE_ID, open_index
from test_h1_espacios import _sesion, cierra  # noqa: F401 - fixtures


class _Receptor:
    """Un servidor HTTP local que apunta lo que le llega."""

    def __init__(self, status=200):
        self.recibidos = []
        self.status = status
        receptor = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                largo = int(self.headers.get("Content-Length") or 0)
                receptor.recibidos.append((self.path, dict(self.headers), self.rfile.read(largo)))
                self.send_response(receptor.status)
                if receptor.status in (301, 302):
                    self.send_header("Location", "http://169.254.169.254/latest/meta-data")
                self.end_headers()

            def log_message(self, *a):  # silencio
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def cierra(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receptor(monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow_private", True)
    r = _Receptor()
    yield r
    r.cierra()


def _conn():
    return open_index(settings.index_path)


# ---------------------------------------------------------------- seguridad


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "gopher://example.com", "javascript:alert(1)",
    "http://127.0.0.1/hook", "http://localhost/hook", "http://10.1.2.3/hook", "http://192.168.0.10:8080/",
    "http://172.16.0.1/", "http://[::1]/", "http://169.254.169.254/latest/meta-data", "http://0.0.0.0/",
    "http://[::ffff:127.0.0.1]/", "http://user:secreto@example.com/", "", "   ", "http:///sin-servidor",
])
def test_urls_que_no_valen(url):
    with pytest.raises(webhooks.WebhookError):
        webhooks.validate_url(url, allow_private=False)


def test_una_url_publica_vale(monkeypatch):
    monkeypatch.setattr(webhooks.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert webhooks.validate_url("https://hooks.example.com/abc", allow_private=False) == "https://hooks.example.com/abc"
    # Si el nombre tiene ALGUNA dirección interna, se rechaza entero (DNS con trampa).
    monkeypatch.setattr(webhooks.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.0.0.1", 443))])
    with pytest.raises(webhooks.WebhookError):
        webhooks.validate_url("https://doble.example.com/", allow_private=False)


def test_las_direcciones_internas():
    for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "172.20.0.1", "169.254.169.254", "::1", "fe80::1",
               "::ffff:10.0.0.1", "0.0.0.0", "224.0.0.1", "127.0.0.2"):
        assert webhooks._is_internal(ip), ip
    assert not webhooks._is_internal("93.184.216.34")
    assert not webhooks._is_internal("2606:2800:220:1:248:1893:25c8:1946")


def test_no_se_siguen_las_redirecciones(client, monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow_private", True)
    r = _Receptor(status=302)
    try:
        assert webhooks.validate_url(r.url, allow_private=True)
        assert webhooks.post(r.url, b"{}", {"Content-Type": "application/json"}) == 302
        assert len(r.recibidos) == 1  # contestó 302 y no se fue a la dirección de metadatos
    finally:
        r.cierra()


# ------------------------------------------------------------- configuración


def test_configurar_el_webhook_y_el_secreto_solo_se_ve_una_vez(client, receptor, cierra):
    r = client.put(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook", json={"url": receptor.url, "events": ["task.created"]})
    assert r.status_code == 200, r.text
    secreto = r.json()["secret"]
    assert len(secreto) >= 32 and r.json()["config"]["events"] == ["task.created"] and r.json()["config"]["secret_set"]
    # Después ya no sale: ni al leer ni al guardar de nuevo.
    assert "secret" not in client.get(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook").json()
    otra = client.put(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook", json={"url": receptor.url, "events": ["task.created", "task.completed"]})
    assert "secret" not in otra.json() and otra.json()["config"]["events"] == ["task.completed", "task.created"]
    nuevo = client.post(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook/secret").json()["secret"]
    assert nuevo != secreto
    # El secreto no se cuela en la lista de espacios.
    assert secreto not in json.dumps(client.get("/workspaces").json()) and nuevo not in json.dumps(client.get("/workspaces").json())
    # Solo el administrador del espacio lo gestiona.
    editor, _ = _sesion(client, "edi")
    cierra.append(editor)
    assert editor.get(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook").status_code == 403
    assert editor.put(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook", json={"url": receptor.url}).status_code == 403
    assert client.delete(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook").status_code == 204
    assert client.get(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook").json()["config"] is None


def test_rechaza_urls_internas_y_eventos_raros(client):
    for url in ("http://127.0.0.1:9/x", "http://169.254.169.254/", "file:///etc/passwd"):
        r = client.put(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook", json={"url": url})
        assert r.status_code == 400, url
    assert client.put(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook", json={"url": "http://127.0.0.1/", "events": ["x"]}).status_code == 400


# ------------------------------------------------------------------ entregas


def _tareas(client, **extra):
    return client.post("/databases/from-template", json={"template": "tareas", "title": "Tareas", "section": "shared", **extra}).json()


def test_una_tarea_nueva_se_entrega_firmada(client, receptor):
    secreto = client.put(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook", json={"url": receptor.url}).json()["secret"]
    base = _tareas(client)
    yo = client.get("/auth/me").json()
    client.post(f"/databases/{base['id']}/rows", json={"title": "Maquetar", "properties": {"responsable": yo["id"], "horas": 8}})
    conn = _conn()
    try:
        assert conn.execute("SELECT count(*) FROM webhook_deliveries WHERE status='pending'").fetchone()[0] == 1
        assert webhooks.send_pending(conn) == 1
    finally:
        conn.close()
    ruta, cabeceras, cuerpo = receptor.recibidos[0]
    assert ruta == "/hook" and cabeceras["X-Noteping-Event"] == "task.created"
    # La firma es la del secreto: HMAC(marca de tiempo + "." + cuerpo).
    esperado = hmac.new(secreto.encode(), cabeceras["X-Noteping-Timestamp"].encode() + b"." + cuerpo, hashlib.sha256).hexdigest()
    assert cabeceras["X-Noteping-Signature"] == f"sha256={esperado}"
    datos = json.loads(cuerpo)
    # Compatible con Slack/Mattermost (text), Discord (content) y n8n (estructurado).
    assert datos["text"] == datos["content"] and "Maquetar" in datos["text"] and yo["display_name"] in datos["text"]
    assert datos["event"] == "task.created" and datos["workspace"]["name"] == "General"
    assert datos["task"]["title"] == "Maquetar" and datos["task"]["hours"] == 8 and datos["task"]["assignees"] == [yo["display_name"]]
    assert client.get(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook").json()["deliveries"][0]["status"] == "delivered"


def test_completar_una_tarea_y_solo_las_de_bases_de_tareas(client, receptor):
    client.put(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook", json={"url": receptor.url, "events": ["task.completed"]})
    base = _tareas(client)
    fila = client.post(f"/databases/{base['id']}/rows", json={"title": "A", "properties": {"estado": "En curso"}}).json()
    conn = _conn()
    try:
        assert conn.execute("SELECT count(*) FROM webhook_deliveries").fetchone()[0] == 0  # created no está suscrito
    finally:
        conn.close()
    client.patch(f"/databases/{base['id']}/rows/{fila['id']}", json={"properties": {"estado": "Hecha"}})
    client.patch(f"/databases/{base['id']}/rows/{fila['id']}", json={"properties": {"estado": "Hecha", "horas": 3}})  # ya cerrada: no repite
    # Una base normal no dispara nada.
    normal = client.post("/databases", json={"title": "Normal", "schema_def": [{"name": "Estado", "type": "select", "options": ["a", "Hecha"]}]}).json()
    client.post(f"/databases/{normal['id']}/rows", json={"title": "N", "properties": {"estado": "Hecha"}})
    conn = _conn()
    try:
        filas = conn.execute("SELECT event FROM webhook_deliveries").fetchall()
        assert [f[0] for f in filas] == ["task.completed"]
    finally:
        conn.close()


def test_tarea_vencida_una_vez_al_dia(client, receptor):
    from datetime import date, timedelta
    from app import notifications

    client.put(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook", json={"url": receptor.url, "events": ["task.overdue"]})
    base = _tareas(client)
    yo = client.get("/auth/me").json()["id"]
    client.post(f"/databases/{base['id']}/rows", json={"title": "Tarde", "properties": {
        "responsable": yo, "estado": "En curso", "fin": (date.today() - timedelta(days=3)).isoformat()}})
    conn = _conn()
    try:
        notifications.scan_deadlines(conn)
        notifications.scan_deadlines(conn)
        assert conn.execute("SELECT count(*) FROM webhook_deliveries WHERE event='task.overdue'").fetchone()[0] == 1
    finally:
        conn.close()


def test_reintentos_y_fallo_definitivo(client, monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow_private", True)
    r = _Receptor(status=500)
    try:
        client.put(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook", json={"url": r.url, "events": ["task.created"]})
        base = _tareas(client)
        client.post(f"/databases/{base['id']}/rows", json={"title": "X"})
        conn = _conn()
        try:
            for intento in range(1, webhooks.MAX_ATTEMPTS + 1):
                conn.execute("UPDATE webhook_deliveries SET next_attempt = '2000-01-01T00:00:00+00:00'")
                webhooks.send_pending(conn)
                fila = conn.execute("SELECT status, attempts, last_error FROM webhook_deliveries").fetchone()
                assert fila["attempts"] == intento and "500" in fila["last_error"]
                assert fila["status"] == ("failed" if intento == webhooks.MAX_ATTEMPTS else "pending")
            assert len(r.recibidos) == webhooks.MAX_ATTEMPTS
        finally:
            conn.close()
    finally:
        r.cierra()


def test_el_envio_de_prueba(client, receptor):
    client.put(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook", json={"url": receptor.url})
    assert client.post(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook/test").json() == {"ok": True, "status": 200}
    assert json.loads(receptor.recibidos[-1][2])["event"] == "ping"
    receptor.status = 404
    assert client.post(f"/workspaces/{DEFAULT_WORKSPACE_ID}/webhook/test").json() == {"ok": False, "status": 404}


def test_las_entregas_viejas_se_limpian(client, receptor):
    conn = _conn()
    try:
        conn.execute("INSERT INTO webhook_deliveries (id,workspace_id,event,payload,status,attempts,next_attempt,created_at) VALUES ('a','w','x','{}','delivered',1,'2000','2000-01-01T00:00:00+00:00')")
        conn.execute("INSERT INTO webhook_deliveries (id,workspace_id,event,payload,status,attempts,next_attempt,created_at) VALUES ('b','w','x','{}','pending',0,'2000','2000-01-01T00:00:00+00:00')")
        assert webhooks.purge_old(conn, 30) == 1
        assert conn.execute("SELECT id FROM webhook_deliveries").fetchall()[0][0] == "b"
    finally:
        conn.close()


def test_ni_con_el_permiso_de_red_interna_se_llama_a_los_metadatos_de_la_nube():
    for url in ("http://169.254.169.254/latest/meta-data", "http://[fe80::1]/", "http://[::ffff:169.254.169.254]/"):
        with pytest.raises(webhooks.WebhookError):
            webhooks.validate_url(url, allow_private=True)
    # Lo interno normal sí se permite si el despliegue lo pide.
    assert webhooks.validate_url("http://127.0.0.1:5678/webhook", allow_private=True)
