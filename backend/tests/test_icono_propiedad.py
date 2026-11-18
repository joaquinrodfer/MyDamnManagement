"""El icono de una propiedad (`icon` en el `schema`), como en Notion.

Sale delante del nombre en la cabecera de la tabla. Se guarda en el `.md` de
la base, sobrevive a un guardado del schema que no lo trae (el diálogo de
propiedades no lo pide) y `""` lo quita.
"""

from pathlib import Path


def _crear(client, schema):
    r = client.post("/databases", json={"title": "Con iconos", "schema_def": schema})
    assert r.status_code in (200, 201), r.text
    return r.json()


def _md(vault_path: Path, titulo: str) -> str:
    return next(vault_path.rglob(f"{titulo}.md")).read_text(encoding="utf-8")


def test_el_icono_se_guarda_y_se_lee(client, vault_path):
    base = _crear(client, [{"name": "Estado", "type": "select", "options": ["A"], "icon": "🚦"}])
    prop = base["schema_def"][0]
    assert prop["icon"] == "🚦"
    assert "🚦" in _md(vault_path, "Con iconos")
    leida = client.get(f"/databases/{base['id']}").json()
    assert leida["schema_def"][0]["icon"] == "🚦"


def test_guardar_sin_icono_lo_conserva_y_vacio_lo_quita(client, vault_path):
    base = _crear(client, [{"name": "Estado", "type": "text", "icon": "🚦"}])
    key = base["schema_def"][0]["key"]
    r = client.patch(
        f"/databases/{base['id']}",
        json={"schema_def": [{"key": key, "name": "Estado", "type": "text"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["schema_def"][0]["icon"] == "🚦"
    r = client.patch(
        f"/databases/{base['id']}",
        json={"schema_def": [{"key": key, "name": "Estado", "type": "text", "icon": ""}]},
    )
    assert r.status_code == 200, r.text
    assert "icon" not in r.json()["schema_def"][0]
    assert "🚦" not in _md(vault_path, "Con iconos")


def test_un_icono_no_es_un_texto_largo(client):
    r = client.post(
        "/databases",
        json={"title": "Mal", "schema_def": [{"name": "X", "type": "text", "icon": "x" * 40}]},
    )
    assert r.status_code == 422
