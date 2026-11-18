"""3.0, hito 2: la verdad de las notas está en la base, con versión por documento.

Estos tests corren en el modo de producción: **sin proyección**
(`VAULT_PROJECTION=0`), así que el disco no tiene `.md` y todo sale de la base.
El resto de la suite corre con la proyección encendida (ver `conftest.py`).

Lo que se fija:

1. guardar no escribe ningún `.md`: los documentos están en `documents`;
2. el primer arranque migra el vault de disco a la base una sola vez, y los
   siguientes no lo vuelven a leer (ni llaman al reindexado);
3. el export del hito 1 es idéntico antes y después de migrar;
4. `version` sube en cada escritura y `If-Match` da 409 con la versión y el
   documento actuales; un reindexado no se la come;
5. guardar es atómico: si algo falla a mitad, ni documento ni índice cambian.
"""

import io
import sqlite3
from pathlib import Path

import pytest

from app import index as indice
from app.config import settings

from conftest import iniciar_sesion
from test_h1_export_vault import _files, montar_vault


@pytest.fixture()
def sin_proyeccion(monkeypatch):
    monkeypatch.setattr(settings, "vault_projection", False)


@pytest.fixture()
def abrir(index_path, monkeypatch):
    """Arranca la app (un `TestClient` nuevo) sobre el índice del test, con sesión."""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "index_path", str(index_path))

    def _abrir():
        return TestClient(app)

    return _abrir


@pytest.fixture()
def cliente(sin_proyeccion, abrir):
    with abrir() as c:
        iniciar_sesion(c)
        yield c


@pytest.fixture()
def export_dir(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "exports"
    monkeypatch.setattr(settings, "export_dir", str(path))
    return path


def _ok(respuesta, codigo=200):
    assert respuesta.status_code == codigo, respuesta.text
    return respuesta.json() if respuesta.content and codigo != 204 else None


def _q(index_path, sql, args=()):
    conn = sqlite3.connect(str(index_path))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 1. guardar no escribe `.md`
# --------------------------------------------------------------------------


def test_guardar_no_escribe_ficheros_md(cliente, vault_path, index_path):
    nota = _ok(cliente.post("/pages", json={"title": "Solo en la base", "body_markdown": "uno"}))
    _ok(cliente.patch(f"/pages/{nota['id']}", json={"body_markdown": "dos", "title": "Renombrada"}))
    hija = _ok(cliente.post("/pages", json={"title": "Hija", "parent_id": nota["id"]}))
    base = _ok(cliente.post("/databases", json={"title": "Tareas", "schema_def": [
        {"key": "estado", "name": "Estado", "type": "select", "options": ["a", "b"]},
    ]}))
    fila = _ok(cliente.post(f"/databases/{base['id']}/rows", json={"title": "F", "properties": {"estado": "a"}}))
    _ok(cliente.patch(f"/databases/{base['id']}/rows/{fila['id']}", json={"properties": {"estado": "b"}}))
    _ok(cliente.delete(f"/pages/{hija['id']}"), 204)

    assert list(vault_path.rglob("*.md")) == []
    rutas = {r[0] for r in _q(index_path, "SELECT path FROM documents")}
    assert "Notas/Renombrada.md" in rutas
    assert "Bases/Tareas.md" in rutas
    assert any(r.startswith("Bases/Tareas/") for r in rutas)
    assert any(r.startswith(".trash/") and r.endswith("/Notas/Renombrada/Hija.md") for r in rutas)
    # Lo que se sirve es lo que dice la base.
    assert _ok(cliente.get(f"/pages/{nota['id']}"))["body_markdown"] == "dos"
    # Y el documento de cada página es exactamente su frontmatter + cuerpo.
    from app.vault_export import page_text

    for path, front, body in _q(index_path, "SELECT path, frontmatter, body FROM pages"):
        texto = _q(index_path, "SELECT text FROM documents WHERE path = ?", (path,))[0][0]
        assert texto == page_text(front, body), path


def test_sin_proyeccion_no_hay_vigilante_ni_deriva(cliente):
    from app import main

    assert main.watcher is None
    deriva = _ok(cliente.get("/status"))["drift"]
    assert deriva["changed"] == 0 and deriva["projection"] is False


# --------------------------------------------------------------------------
# 2. migración de un vault en disco: una vez
# --------------------------------------------------------------------------


def _vault_en_disco(vault_path: Path) -> None:
    notas = vault_path / "Notas"
    notas.mkdir(parents=True, exist_ok=True)
    (notas / "Uno.md").write_text(
        "---\nid: 0b8f4b9e-3e0c-4e0b-9a43-6f0a3b6a1c01\ncreated: 2026-01-01T00:00:00+00:00\n"
        "updated: 2026-01-01T00:00:00+00:00\n---\n\nEnlaza a [[Dos]].\n",
        encoding="utf-8",
    )
    (notas / "Dos.md").write_text("Sin frontmatter, con CRLF.\r\n", encoding="utf-8")


def test_el_primer_arranque_migra_y_los_siguientes_no_releen(
    sin_proyeccion, abrir, vault_path, index_path, monkeypatch
):
    _vault_en_disco(vault_path)
    with abrir() as c:
        iniciar_sesion(c)
        assert {p["title"] for p in c.get("/pages").json()} == {"Uno", "Dos"}
    assert _q(index_path, "SELECT value FROM meta WHERE key = 'storage'") == [("db",)]
    assert len(_q(index_path, "SELECT path FROM documents")) == 2
    assert _q(index_path, "SELECT count(*) FROM links WHERE to_id IS NOT NULL")[0][0] == 1

    # Lo que cambie en el disco después ya no es la verdad.
    (vault_path / "Notas" / "Tres.md").write_text("tres\n", encoding="utf-8")
    (vault_path / "Notas" / "Uno.md").unlink()

    # Y el segundo arranque no reindexa: si llamara a `rebuild`, reventaría.
    from app.vault.store import VaultStore

    def prohibido(self, **kwargs):  # pragma: no cover - es lo que NO tiene que pasar
        raise AssertionError("el arranque ha reindexado un índice ya migrado")

    monkeypatch.setattr(VaultStore, "rebuild", prohibido)
    with abrir() as c:
        estado = c.get("/health").json()
        assert estado["status"] == "ok", estado
        iniciar_sesion(c)
        assert {p["title"] for p in c.get("/pages").json()} == {"Uno", "Dos"}


def test_un_reindexado_explicito_reconstruye_desde_la_base(cliente, vault_path, index_path):
    _ok(cliente.post("/pages", json={"title": "Otra"}))
    nota = _ok(cliente.post("/pages", json={"title": "Viva", "body_markdown": "[[Otra]]"}))
    huella = indice.dump_hash(str(index_path))
    (vault_path / "Notas").mkdir(exist_ok=True)
    (vault_path / "Notas" / "Intrusa.md").write_text("no entra\n", encoding="utf-8")

    _ok(cliente.post("/reindex"))
    assert "Intrusa" not in {p["title"] for p in cliente.get("/pages").json()}
    assert _ok(cliente.get(f"/pages/{nota['id']}"))["body_markdown"] == "[[Otra]]"
    assert indice.dump_hash(str(index_path))[1] == huella[1]


# --------------------------------------------------------------------------
# 3. el export del hito 1, idéntico tras migrar
# --------------------------------------------------------------------------


def test_el_export_es_identico_despues_de_migrar(
    client, vault_path, export_dir, tmp_path, monkeypatch
):
    """Vault variado (con proyección, como el de 2.x) -> export A.

    Ese export, tomado como el vault en disco de una instalación 2.x, se migra
    en un índice nuevo **sin proyección** y se vuelve a exportar: B tiene que
    ser A byte a byte.
    """
    montar_vault(client, vault_path)
    a = Path(_ok(client.post("/export", json={"format": "folder"}))["destination"])

    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "vault_projection", False)
    monkeypatch.setattr(settings, "vault_path", str(a))
    monkeypatch.setattr(settings, "index_path", str(tmp_path / "migrado.sqlite"))
    monkeypatch.setattr(settings, "export_dir", str(tmp_path / "exports-b"))
    with TestClient(app) as c:
        iniciar_sesion(c)
        stats = _ok(c.post("/export", json={"format": "folder"}))
    assert stats["unindexed"] == []
    b = Path(stats["destination"])
    assert _files(b) == _files(a)


# --------------------------------------------------------------------------
# 4. versión e If-Match
# --------------------------------------------------------------------------


def test_version_sube_en_cada_escritura_y_va_en_etag(cliente):
    nota = _ok(cliente.post("/pages", json={"title": "V", "body_markdown": "1"}))
    assert nota["version"] == 1
    r = cliente.patch(f"/pages/{nota['id']}", json={"body_markdown": "2"})
    assert r.json()["version"] == 2 and r.headers["etag"] == '"2"'
    r = cliente.get(f"/pages/{nota['id']}")
    assert r.json()["version"] == 2 and r.headers["etag"] == '"2"'
    # Un PATCH que no cambia nada no es una escritura.
    assert _ok(cliente.patch(f"/pages/{nota['id']}", json={"title": "V"}))["version"] == 2


def test_if_match_viejo_da_409_con_la_version_y_el_cuerpo_actuales(cliente):
    """Dos pestañas abren la nota en la versión 1; la segunda en guardar pierde."""
    nota = _ok(cliente.post("/pages", json={"title": "Compartida", "body_markdown": "base"}))
    v = nota["version"]
    a = cliente.patch(f"/pages/{nota['id']}", json={"body_markdown": "de A"}, headers={"If-Match": f'"{v}"'})
    assert a.status_code == 200 and a.json()["version"] == v + 1

    b = cliente.patch(f"/pages/{nota['id']}", json={"body_markdown": "de B"}, headers={"If-Match": str(v)})
    assert b.status_code == 409, b.text
    cuerpo = b.json()
    assert cuerpo["version"] == v + 1 and cuerpo["expected_version"] == v
    assert cuerpo["current"]["body_markdown"] == "de A"
    assert b.headers["etag"] == f'"{v + 1}"'
    # No se escribió nada.
    assert _ok(cliente.get(f"/pages/{nota['id']}"))["body_markdown"] == "de A"

    # Con la versión buena, sí; con `*` o sin cabecera, también (último gana).
    ok = cliente.patch(f"/pages/{nota['id']}", json={"body_markdown": "de B, rebasado"},
                       headers={"If-Match": f'W/"{v + 1}"'})
    assert ok.status_code == 200 and ok.json()["version"] == v + 2
    assert cliente.patch(f"/pages/{nota['id']}", json={"body_markdown": "x"}, headers={"If-Match": "*"}).status_code == 200
    assert cliente.patch(f"/pages/{nota['id']}", json={"body_markdown": "y"}).status_code == 200
    assert cliente.patch(f"/pages/{nota['id']}", json={"body_markdown": "z"}, headers={"If-Match": "abc"}).status_code == 400


def test_if_match_en_filas(cliente):
    base = _ok(cliente.post("/databases", json={"title": "B", "schema_def": [
        {"key": "n", "name": "N", "type": "number"},
    ]}))
    fila = _ok(cliente.post(f"/databases/{base['id']}/rows", json={"title": "F", "properties": {"n": 1}}))
    assert fila["version"] == 1
    url = f"/databases/{base['id']}/rows/{fila['id']}"
    assert cliente.patch(url, json={"properties": {"n": 2}}, headers={"If-Match": "1"}).json()["version"] == 2
    r = cliente.patch(url, json={"properties": {"n": 3}}, headers={"If-Match": "1"})
    assert r.status_code == 409, r.text
    assert r.json()["current"]["properties"]["n"] == 2
    assert r.json()["version"] == 2


def test_reindexar_no_se_come_las_versiones(cliente):
    nota = _ok(cliente.post("/pages", json={"title": "Persistente"}))
    for texto in ("a", "b", "c"):
        _ok(cliente.patch(f"/pages/{nota['id']}", json={"body_markdown": texto}))
    assert _ok(cliente.get(f"/pages/{nota['id']}"))["version"] == 4
    _ok(cliente.post("/reindex"))
    assert _ok(cliente.get(f"/pages/{nota['id']}"))["version"] == 4


# --------------------------------------------------------------------------
# 5. atomicidad
# --------------------------------------------------------------------------


def test_un_guardado_que_falla_a_mitad_no_deja_nada_a_medias(cliente, index_path, monkeypatch):
    nota = _ok(cliente.post("/pages", json={"title": "Atómica", "body_markdown": "antes"}))
    antes = _q(index_path, "SELECT text FROM documents WHERE path = 'Notas/Atómica.md'")

    import app.vault.store as store

    def revienta(*args, **kwargs):
        raise RuntimeError("fallo a mitad del guardado")

    # Falla DESPUÉS de escribir el documento y el índice de la página, al
    # recalcular los enlaces: la transacción entera tiene que deshacerse.
    with monkeypatch.context() as m:
        m.setattr(store, "sync_wikilinks", revienta)
        with pytest.raises(RuntimeError):
            cliente.patch(f"/pages/{nota['id']}", json={"body_markdown": "después", "title": "Movida"})

    assert _q(index_path, "SELECT text FROM documents WHERE path = 'Notas/Atómica.md'") == antes
    assert _q(index_path, "SELECT count(*) FROM documents WHERE path = 'Notas/Movida.md'")[0][0] == 0
    leida = _ok(cliente.get(f"/pages/{nota['id']}"))
    assert leida["body_markdown"] == "antes" and leida["title"] == "Atómica" and leida["version"] == 1


def test_la_cabecera_tambien_va_a_la_base(cliente, index_path):
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )
    nota = _ok(cliente.post("/pages", json={"title": "Con imagen"}))
    r = cliente.post(f"/pages/{nota['id']}/header-image", files={"file": ("x.png", io.BytesIO(png), "image/png")})
    assert r.status_code == 200, r.text
    texto = _q(index_path, "SELECT text FROM documents WHERE path = 'Notas/Con imagen.md'")[0][0]
    assert "header_image: /files/_attachments/" in texto
