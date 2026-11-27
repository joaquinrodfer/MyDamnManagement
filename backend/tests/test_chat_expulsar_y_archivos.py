"""Sacar a alguien de un grupo, y los límites y la caducidad de los archivos."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from tests.test_ch1_chat import _canal, _ok, gente  # noqa: F401 - fixture

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


def test_el_administrador_del_grupo_saca_a_alguien(client, gente):
    ana, beto, carla, ids = gente["ana"], gente["beto"], gente["carla"], gente["ids"]
    g = _ok(ana.post("/chat/groups", json={"title": "Viaje", "user_ids": [ids["beto"], ids["carla"]]}))
    assert g["admin_id"] == ids["ana"]
    # Solo el administrador saca, y no a sí mismo.
    assert beto.delete(f"/chat/conversations/{g['id']}/members/{ids['carla']}").status_code == 403
    assert ana.delete(f"/chat/conversations/{g['id']}/members/{ids['ana']}").status_code == 400
    _ok(ana.delete(f"/chat/conversations/{g['id']}/members/{ids['beto']}"))
    assert beto.get(f"/chat/conversations/{g['id']}/messages").status_code == 404
    ultimo = _ok(carla.get(f"/chat/conversations/{g['id']}/messages"))["messages"][-1]
    assert ultimo["event"]["type"] == "removed" and ultimo["event"]["users"][0]["id"] == ids["beto"]
    aviso = next(a for a in _ok(beto.get("/notifications"))["items"] if a["type"] == "chat_removed")
    assert "Ana te ha sacado del grupo «Viaje»" in aviso["message"]
    assert ana.delete(f"/chat/conversations/{g['id']}/members/{ids['beto']}").status_code == 404
    # Si el administrador se va, lo es quien lleve más tiempo dentro.
    _ok(ana.delete(f"/chat/conversations/{g['id']}/members/me"))
    assert next(c for c in _ok(carla.get("/chat/conversations")) if c["id"] == g["id"])["admin_id"] == ids["carla"]
    # En un privado no se saca a nadie.
    d = _ok(ana.post("/chat/direct", json={"user_ids": [ids["beto"]]}))
    assert ana.delete(f"/chat/conversations/{d['id']}/members/{ids['beto']}").status_code == 400


def test_limite_por_espacio_y_programas_bloqueados(client, gente):
    ana, dev = gente["ana"], gente["dev"]
    canal = _canal(ana, "Desarrollo")
    url = f"/chat/conversations/{canal['id']}/files"
    assert ana.post(url, files={"file": ("virus.exe", b"MZ" + b"0" * 100, "application/octet-stream")}).status_code == 415
    assert ana.post(url, files={"file": ("x.sh", b"rm -rf /", "text/plain")}).status_code == 415
    # El administrador del espacio baja el máximo a 1 MB; los editores no pueden.
    assert ana.patch(f"/workspaces/{dev['id']}", json={"settings": {"files_max_mb": 1}}).status_code == 403
    assert client.patch(f"/workspaces/{dev['id']}", json={"settings": {"files_max_mb": 9999}}).status_code == 400
    assert client.patch(f"/workspaces/{dev['id']}", json={"settings": {"chat_files_days": 0}}).status_code == 400
    ws = _ok(client.patch(f"/workspaces/{dev['id']}", json={"settings": {"files_max_mb": 1, "chat_files_days": 7}}))
    assert ws["settings"]["files_max_mb"] == 1 and ws["settings"]["chat_files_days"] == 7
    grande = b"a" * (1024 * 1024 + 10)
    r = ana.post(url, files={"file": ("datos.csv", grande, "text/csv")})
    assert r.status_code == 413 and "1 MB" in r.json()["detail"]
    assert _ok(ana.post(url, files={"file": ("datos.csv", b"a,b\n1,2\n", "text/csv")}))["type"] == "file"


def test_el_cuerpo_enorme_se_corta_antes(client):
    from app.uploads import MB, BodyLimit

    assert BodyLimit(None).multipart_bytes > 200 * MB
    r = client.post("/chat/conversations/x/files", headers={"Content-Type": "multipart/form-data; boundary=x",
                                                            "Content-Length": str(300 * MB)}, content=b"--x--")
    assert r.status_code == 413


def test_files_sirve_con_sandbox(client, gente):
    ana = gente["ana"]
    canal = _canal(ana, "Desarrollo")
    subido = _ok(ana.post(f"/chat/conversations/{canal['id']}/files",
                          files={"file": ("pagina.html", b"<script>alert(1)</script>", "text/html")}))
    assert subido["url"].endswith(".txt")
    r = ana.get(subido["url"])
    assert "sandbox" in r.headers["content-security-policy"] and r.headers["content-disposition"].startswith("attachment")


def test_los_archivos_del_chat_caducan(client, gente):
    from app import uploads
    from app.config import settings
    from app.index import open_index

    ana, dev = gente["ana"], gente["dev"]
    _ok(client.patch(f"/workspaces/{dev['id']}", json={"settings": {"chat_files_days": 3}}))
    canal = _canal(ana, "Desarrollo")
    viejo = _ok(ana.post(f"/chat/conversations/{canal['id']}/files", files={"file": ("a.png", PNG, "image/png")}))
    nuevo = _ok(ana.post(f"/chat/conversations/{canal['id']}/files", files={"file": ("b.csv", b"x,y", "text/csv")}))
    m1 = _ok(ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "foto", "attachments": [viejo]}))
    _ok(ana.post(f"/chat/conversations/{canal['id']}/messages", json={"body": "csv", "attachments": [nuevo]}))
    ruta = os.path.join(settings.vault_path, viejo["url"][len("/files/"):])
    assert os.path.exists(ruta)
    conn = open_index(settings.index_path)
    try:
        hace = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        conn.execute("UPDATE chat_messages SET created_at = ? WHERE id = ?", (hace, m1["id"]))
        conn.commit()
        assert uploads.expire_chat_files(conn, settings.vault_path) == 1
        conn.commit()
    finally:
        conn.close()
    assert not os.path.exists(ruta)
    msgs = {m["id"]: m for m in _ok(ana.get(f"/chat/conversations/{canal['id']}/messages"))["messages"]}
    adj = msgs[m1["id"]]["attachments"][0]
    assert adj["expired"] and adj["name"] == "a.png" and "url" not in adj
    assert ana.get(viejo["url"]).status_code == 404
    assert ana.get(nuevo["url"]).status_code == 200
