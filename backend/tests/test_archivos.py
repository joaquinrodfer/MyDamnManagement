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
    pid = _nota(client)
    r = client.post(f"/pages/{pid}/files", files={"file": ("foto.png", b"<svg onload=alert(1)>", "image/png")})
    assert r.status_code == 415


def test_un_archivo_demasiado_grande_se_rechaza(client, monkeypatch):
    from app.routers import pages

    monkeypatch.setattr(pages, "MAX_FILE_BYTES", 10)
    pid = _nota(client)
    r = client.post(f"/pages/{pid}/files", files={"file": ("grande.png", PNG, "image/png")})
    assert r.status_code == 413


def test_no_se_sube_a_una_pagina_que_no_existe(client):
    r = client.post("/pages/00000000-0000-0000-0000-000000000000/files", files={"file": ("a.png", PNG, "image/png")})
    assert r.status_code == 404
