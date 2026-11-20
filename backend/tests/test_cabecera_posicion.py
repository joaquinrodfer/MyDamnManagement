"""Encuadre de la imagen de cabecera: se guarda en el frontmatter y se reinicia
al cambiar o quitar la imagen."""

from test_api_contract import PNG, crear


def test_encuadre_de_la_cabecera(client, vault_path):
    page = crear(client, title="Con portada")
    pid = page["id"]
    # Sin imagen no hay nada que encuadrar.
    assert client.put(f"/pages/{pid}/header-image/position", json={"position": 30}).status_code == 400
    client.post(f"/pages/{pid}/header-image", files={"file": ("c.png", PNG, "image/png")})
    assert client.get(f"/pages/{pid}").json()["header_position"] is None  # centrada
    r = client.put(f"/pages/{pid}/header-image/position", json={"position": 30})
    assert r.status_code == 200 and r.json()["header_position"] == 30
    assert client.get(f"/pages/{pid}").json()["header_position"] == 30
    # Fuera de rango, no.
    assert client.put(f"/pages/{pid}/header-image/position", json={"position": 120}).status_code == 422
    # Vive en el .md (sobrevive a un reindexado).
    md = next(p for p in vault_path.rglob("*.md") if "Con portada" in p.name)
    assert "header_position: 30" in md.read_text(encoding="utf-8")
    # Una imagen nueva vuelve a centrarse; sin imagen, no hay encuadre.
    client.post(f"/pages/{pid}/header-image", files={"file": ("c.png", PNG, "image/png")})
    assert client.get(f"/pages/{pid}").json()["header_position"] is None
    client.put(f"/pages/{pid}/header-image/position", json={"position": 70})
    client.delete(f"/pages/{pid}/header-image")
    assert client.get(f"/pages/{pid}").json()["header_position"] is None
    assert "header_position" not in md.read_text(encoding="utf-8")
