"""Archivos dentro del cuerpo de una página (multimedia, 1: imágenes)."""

from __future__ import annotations

import base64

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _nota(client):
    r = client.post("/pages", json={"title": "Con imagen", "type": "note"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_subir_una_imagen_y_servirla(client):
    pid = _nota(client)
    r = client.post(f"/pages/{pid}/files", files={"file": ("Captura de pantalla [1].png", PNG, "image/png")})
    assert r.status_code == 201, r.text
    datos = r.json()
    assert datos["kind"] == "image"
    assert datos["url"].startswith("/files/_attachments/")
    assert datos["url"].endswith("-captura-de-pantalla-1.png")
    assert datos["markdown"] == f"![Captura de pantalla (1)]({datos['url']})"

    servida = client.get(datos["url"])
    assert servida.status_code == 200
    assert servida.headers["content-type"] == "image/png"
    assert servida.headers["content-disposition"].startswith("inline")
    assert servida.content == PNG


def test_el_tipo_lo_deciden_los_bytes_no_el_nombre(client):
    """Un SVG con nombre de PNG no es una imagen: se guarda como archivo que se
    descarga, con una extensión que no engaña al editor."""
    pid = _nota(client)
    r = client.post(f"/pages/{pid}/files", files={"file": ("foto.png", b"<svg onload=alert(1)>", "image/png")})
    assert r.status_code == 201, r.text
    datos = r.json()
    assert datos["kind"] == "file"
    assert datos["url"].endswith(".bin")
    servida = client.get(datos["url"])
    assert servida.headers["content-type"] == "application/octet-stream"
    assert servida.headers["content-disposition"].startswith("attachment")


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + bytes(range(256)) * 8
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 64


def test_video_audio_y_pdf_se_reconocen_y_se_sirven_en_linea(client):
    pid = _nota(client)
    esperado = {"informe.pdf": ("pdf", "application/pdf"), "demo.mp4": ("video", "video/mp4"), "nota de voz.mp3": ("audio", "audio/mpeg")}
    for (nombre, datos) in (("informe.pdf", PDF), ("demo.mp4", MP4), ("nota de voz.mp3", MP3)):
        r = client.post(f"/pages/{pid}/files", files={"file": (nombre, datos, "application/octet-stream")})
        assert r.status_code == 201, r.text
        res = r.json()
        kind, tipo = esperado[nombre]
        assert res["kind"] == kind
        assert res["markdown"] == f"[{nombre}]({res['url']})"
        servida = client.get(res["url"])
        assert servida.headers["content-type"] == tipo
        assert servida.headers["content-disposition"].startswith("inline")
        assert servida.headers["accept-ranges"] == "bytes"


def test_un_video_se_puede_pedir_por_trozos(client):
    pid = _nota(client)
    url = client.post(f"/pages/{pid}/files", files={"file": ("demo.mp4", MP4, "video/mp4")}).json()["url"]

    trozo = client.get(url, headers={"Range": "bytes=10-19"})
    assert trozo.status_code == 206
    assert trozo.content == MP4[10:20]
    assert trozo.headers["content-range"] == f"bytes 10-19/{len(MP4)}"

    final = client.get(url, headers={"Range": "bytes=-5"})
    assert final.status_code == 206 and final.content == MP4[-5:]

    fuera = client.get(url, headers={"Range": f"bytes={len(MP4) + 10}-"})
    assert fuera.status_code == 416


def test_cualquier_otro_archivo_se_guarda_y_se_descarga(client):
    pid = _nota(client)
    r = client.post(f"/pages/{pid}/files", files={"file": ("presupuesto final.xlsx", b"PK\x03\x04 hoja", "application/vnd.ms-excel")})
    assert r.status_code == 201, r.text
    res = r.json()
    assert res["kind"] == "file" and res["url"].endswith("-presupuesto-final.xlsx")
    assert res["markdown"] == f"[presupuesto final.xlsx]({res['url']})"
    servida = client.get(res["url"])
    assert servida.headers["content-disposition"].startswith("attachment")

    # Un HTML nunca conserva una extensión que el navegador ejecute.
    html = client.post(f"/pages/{pid}/files", files={"file": ("pagina.html", b"<script>x</script>", "text/html")}).json()
    assert html["url"].endswith(".txt")


def test_un_archivo_demasiado_grande_se_rechaza(client, monkeypatch):
    from app.routers import pages

    monkeypatch.setattr(pages, "MAX_FILE_BYTES", 10)
    pid = _nota(client)
    r = client.post(f"/pages/{pid}/files", files={"file": ("grande.png", PNG, "image/png")})
    assert r.status_code == 413


def test_no_se_sube_a_una_pagina_que_no_existe(client):
    r = client.post("/pages/00000000-0000-0000-0000-000000000000/files", files={"file": ("a.png", PNG, "image/png")})
    assert r.status_code == 404
