"""P8: vigilante en vivo del vault y avisos al navegador por SSE.

Cuatro bloques, uno por punto del criterio de hecho:

1. el vigilante recoge lo que cambia por fuera (alta, modificación y borrado) y
   `/pages` lo devuelve sin reindexar a mano;
2. un fichero que no se puede leer no para el bucle ni tumba la app;
3. `/events` manda un evento `page` con el `page_id` del índice, y late con
   `: ping` cuando no hay nada que contar;
4. al desconectar, la cola del cliente se suelta (no queda nada colgado).

El intervalo se baja a 0,2 s (y se comprueba que `WATCH_INTERVAL` lo manda) para
que los tests no esperen los 2 s por defecto.

Nota sobre SSE y `TestClient`: el cliente de test de Starlette (0.38) **no
hace streaming** —`portal.call(self.app, ...)` espera a que la respuesta
termine, y un flujo de eventos no termina nunca—, así que aquí se ejercita el
endpoint de verdad (`stream_events()`, su `StreamingResponse` y su generador,
con el bus y el vigilante reales) sin el transporte HTTP. El camino completo por
HTTP (uvicorn + `curl /events` + escribir un `.md` con `cat >`) está en la
evidencia de la tarjeta.
"""

import asyncio
import logging
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

# Id fijos en el frontmatter: la app los toma tal cual (son UUID válidos). Un
# fichero sin `id` recibe uno derivado de la ruta; aquí se prefieren fijos para
# poder afirmar sobre ellos.
ID_EXTERNA = "11111111-1111-4111-8111-111111111111"
ID_OTRO = "22222222-2222-4222-8222-222222222222"
ID_BASE = "44444444-4444-4444-8444-444444444444"
ID_FILA = "55555555-5555-4555-8555-555555555555"

# El formato del ADR, escrito a mano: una base con su schema y su vista, y una
# fila en la carpeta hermana (sin `type`: lo dice su sitio).
BASE_MD = """---
id: {id}
type: database
schema:
  - key: empresa
    name: Empresa
    type: text
  - key: fase
    name: Fase
    type: select
    options: [Lead, Cerrada]
views:
  - id: v1
    name: Tablero
    type: board
    group_by: fase
---
"""

BASE_MD_AMPLIADA = """---
id: {id}
type: database
schema:
  - key: empresa
    name: Empresa
    type: text
  - key: fase
    name: Fase
    type: select
    options: [Lead, Cerrada]
  - key: nota
    name: Nota
    type: text
views:
  - id: v1
    name: Tablero
    type: board
    group_by: fase
---
"""

FILA_MD = """---
id: {id}
empresa: {empresa}
fase: {fase}
---

Notas libres de la fila.
"""


def escribir(vault: Path, rel: str, *, id_: str, cuerpo: str) -> Path:
    """Escribe un `.md` como lo haría cualquier editor de fuera de la app."""
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {id_}\ntype: note\n---\n\n{cuerpo}\n", encoding="utf-8"
    )
    return path


def esperar(condicion, timeout: float = 5.0, paso: float = 0.05):
    """Espera a que `condicion()` devuelva algo verdadero. Devuelve el valor."""
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        valor = condicion()
        if valor:
            return valor
        time.sleep(paso)
    return None


def titulos(client) -> list[str]:
    return [p["title"] for p in client.get("/pages").json()]


@pytest.fixture()
def vigilante(tmp_path, index_path, vault_path, monkeypatch):
    """App arrancada con el vigilante encendido y sondeando cada 0,2 s.

    Se parchea `settings` antes de entrar en el `TestClient` porque el hilo se
    arranca en el `startup`; el `conftest` deja `WATCH_ENABLED=0` para el resto
    de la suite (121 tests no tienen por qué llevar un hilo sondeando).
    """
    from fastapi.testclient import TestClient

    from app.config import settings

    monkeypatch.setattr(settings, "index_path", str(index_path))
    monkeypatch.setattr(settings, "watch_enabled", True)
    monkeypatch.setattr(settings, "watch_interval", 0.2)

    import app.main as main_mod

    with TestClient(app=main_mod.app) as client:
        assert main_mod.watcher is not None, "el vigilante no arrancó"
        assert main_mod.watcher.running, "el vigilante no está corriendo"
        assert main_mod.watcher.interval == pytest.approx(0.2)
        yield client

    # `shutdown` lo para y lo suelta: si no, el hilo seguiría sondeando un
    # vault temporal que ya no existe.
    assert main_mod.watcher is None


# --------------------------------------------------------------------------
# 1. el vigilante recoge lo que cambia por fuera
# --------------------------------------------------------------------------


def test_un_md_creado_por_fuera_acaba_en_el_indice_y_en_pages(vigilante, vault_path):
    escribir(vault_path, "Notas/Externa.md", id_=ID_EXTERNA, cuerpo="Hola desde fuera")

    encontrada = esperar(
        lambda: [p for p in vigilante.get("/pages").json() if p["title"] == "Externa"]
    )
    assert encontrada, "el índice no recogió en 5 s el .md creado por fuera"
    assert encontrada[0]["id"] == ID_EXTERNA

    # `GET /pages/{id}` sirve el cuerpo del fichero, no el que hubiera en el índice.
    leida = vigilante.get(f"/pages/{ID_EXTERNA}")
    assert leida.status_code == 200
    assert leida.json()["body_markdown"] == "Hola desde fuera"


def test_un_md_modificado_por_fuera_actualiza_el_cuerpo(vigilante, vault_path):
    escribir(vault_path, "Notas/Externa.md", id_=ID_EXTERNA, cuerpo="Primera versión")
    assert esperar(lambda: "Externa" in titulos(vigilante))

    escribir(vault_path, "Notas/Externa.md", id_=ID_EXTERNA, cuerpo="Segunda versión")
    actualizada = esperar(
        lambda: vigilante.get(f"/pages/{ID_EXTERNA}").json()["body_markdown"]
        == "Segunda versión"
    )
    assert actualizada, "el cambio de cuerpo no llegó al índice"


def test_un_md_borrado_por_fuera_desaparece_de_pages(vigilante, vault_path):
    fichero = escribir(vault_path, "Notas/Externa.md", id_=ID_EXTERNA, cuerpo="Adiós")
    assert esperar(lambda: "Externa" in titulos(vigilante))

    fichero.unlink()
    assert esperar(lambda: "Externa" not in titulos(vigilante)), (
        "el índice sigue sirviendo una nota cuyo .md ya no está"
    )
    assert vigilante.get(f"/pages/{ID_EXTERNA}").status_code == 404


def test_borrar_una_nota_por_fuera_suelta_a_sus_hijas(vigilante, vault_path):
    """La cascada del índice no puede llevarse ficheros que siguen en disco."""
    padre = vigilante.post("/pages", json={"title": "Padre"}).json()
    hija = vigilante.post(
        "/pages", json={"title": "Hija", "parent_id": padre["id"]}
    ).json()
    assert (vault_path / "Notas" / "Padre.md").is_file()
    assert (vault_path / "Notas" / "Padre" / "Hija.md").is_file()

    (vault_path / "Notas" / "Padre.md").unlink()

    assert esperar(lambda: vigilante.get(f"/pages/{padre['id']}").status_code == 404)
    # La hija sigue viva y sin padre, que es lo que daría un reindexado entero
    # (la carpeta se queda sin página contenedora).
    viva = vigilante.get(f"/pages/{hija['id']}")
    assert viva.status_code == 200, viva.text
    assert viva.json()["parent_id"] is None


def test_el_drift_de_status_vuelve_a_cero_solo_con_el_vigilante(vigilante, vault_path):
    """Nadie llama a `/reindex`: el vigilante es el que deja la deriva a 0."""
    escribir(vault_path, "Notas/Externa.md", id_=ID_EXTERNA, cuerpo="deriva")
    assert esperar(lambda: "Externa" in titulos(vigilante))

    def deriva_limpia():
        deriva = vigilante.get("/status").json()["drift"]
        return deriva if deriva["changed"] == 0 else None

    assert deriva_limpia() or esperar(deriva_limpia), "la deriva no volvió a cero"


# --------------------------------------------------------------------------
# 2. un fichero que no se puede leer no para el bucle
# --------------------------------------------------------------------------


def test_un_md_ilegible_no_para_el_vigilante(vigilante, vault_path):
    roto = escribir(vault_path, "Notas/Roto.md", id_=ID_OTRO, cuerpo="no se puede leer")
    os.chmod(roto, 0o000)
    try:
        escribir(vault_path, "Notas/Bueno.md", id_=ID_EXTERNA, cuerpo="este sí")
        assert esperar(lambda: "Bueno" in titulos(vigilante)), (
            "un .md ilegible paró la pasada y el siguiente fichero no se indexó"
        )
        # Y el bucle sigue vivo: otra pasada más, sin que la app se haya caído.
        escribir(vault_path, "Notas/Otro.md", id_=ID_OTRO, cuerpo="también")
        assert esperar(lambda: "Otro" in titulos(vigilante))
        # La app sigue sirviendo: ningún 500 por el fichero ilegible.
        assert vigilante.get("/health").json() == {"status": "ok"}
    finally:
        os.chmod(roto, 0o600)


# --------------------------------------------------------------------------
# 3. `/events`: el aviso llega con el page_id del índice
# --------------------------------------------------------------------------


async def _abrir_stream():
    """El endpoint de verdad: su `StreamingResponse` y su generador."""
    from app.routers.events import stream_events

    respuesta = await stream_events()
    return respuesta, respuesta.body_iterator


async def _siguiente_chunk(generador, timeout: float = 5.0) -> str:
    try:
        return await asyncio.wait_for(anext(generador), timeout=timeout)
    except (StopAsyncIteration, TimeoutError):  # pragma: no cover
        return ""


def test_sse_manda_el_evento_con_el_page_id_correcto(vigilante, vault_path):
    from app.events import bus

    antes = bus.subscribers
    resultado = {}

    async def escenario():
        respuesta, generador = await _abrir_stream()
        assert respuesta.media_type == "text/event-stream"
        assert respuesta.headers["cache-control"] == "no-cache"

        # Primer byte: el comentario de cortesía. La cola ya está suscrita.
        resultado["apertura"] = await _siguiente_chunk(generador)
        resultado["clientes"] = bus.subscribers

        escribir(vault_path, "Notas/Avisada.md", id_=ID_EXTERNA, cuerpo="te aviso")

        # Un evento SSE es UN chunk: la línea `event:` y la `data:` van juntas.
        resultado["chunk"] = await _siguiente_chunk(generador, timeout=5.0)
        await generador.aclose()  # el cliente se va: dispara el `finally`

    asyncio.run(escenario())

    assert resultado["apertura"] == ": conectado\n\n"
    assert resultado["clientes"] == antes + 1
    assert resultado["chunk"].strip().splitlines() == [
        "event: page",
        'data: {"type": "page", "action": "added", '
        f'"page_id": "{ID_EXTERNA}", "path": "Notas/Avisada.md"}}',
    ], resultado["chunk"]
    assert esperar(lambda: bus.subscribers == antes), (
        f"quedaron {bus.subscribers - antes} cola(s) colgadas tras desconectar"
    )


def test_sse_late_con_ping_cuando_no_hay_nada_que_contar(vigilante, monkeypatch):
    import app.routers.events as events_mod

    monkeypatch.setattr(events_mod, "PING_INTERVAL", 0.05)

    async def escenario():
        _respuesta, generador = await _abrir_stream()
        apertura = await _siguiente_chunk(generador)
        latido = await _siguiente_chunk(generador, timeout=2.0)
        await generador.aclose()
        return apertura, latido

    apertura, latido = asyncio.run(escenario())
    assert apertura == ": conectado\n\n"
    assert latido == ": ping\n\n"


# --------------------------------------------------------------------------
# 4. el interruptor
# --------------------------------------------------------------------------


def test_watch_enabled_0_no_arranca_el_vigilante(client):
    """El `client` del conftest arranca con `WATCH_ENABLED=0`: ni un hilo."""
    import app.main as main_mod

    assert main_mod.watcher is None
    vivos = [
        hilo.name for hilo in threading.enumerate() if hilo.name == "mdm-vault-watcher"
    ]
    assert not vivos, f"hay hilos del vigilante sin `WATCH_ENABLED`: {vivos}"

# --------------------------------------------------------------------------
# 5. el coste: una pasada lenta avisa (una vez) y el bucle sigue
# --------------------------------------------------------------------------


def test_una_pasada_lenta_avisa_una_sola_vez(tmp_path, caplog):
    """En un vault caro (un `stat` de red) el log lo dice sin inundar.

    No es un cambio de comportamiento -- una pasada lenta ya no puede parar el
    bucle -- sino la señal para quien despliega: subir `WATCH_INTERVAL` o
    apagar el vigilante.
    """
    from app.vault.watcher import VaultWatcher

    watcher = VaultWatcher(
        vault_path=str(tmp_path / "vault"),
        index_path=str(tmp_path / "index.sqlite"),
        interval=0.2,
        publish=lambda event: None,
    )

    with caplog.at_level(logging.WARNING, logger="mdm.watch"):
        watcher._note_slow_pass(5.0)   # 5 s con intervalo 0,2 s: lenta
        watcher._note_slow_pass(5.0)   # otra igual: no repite (throttle de 60 s)
        watcher._note_slow_pass(0.01)  # rápida: ni una línea

    avisos = [r for r in caplog.records if "tardó" in r.getMessage()]
    assert len(avisos) == 1, [r.getMessage() for r in caplog.records]
    assert "WATCH_INTERVAL" in avisos[0].getMessage()


# --------------------------------------------------------------------------
# 6. regresión de la ronda 1: copiar un `.md` a mano no puede oscilar
# --------------------------------------------------------------------------


def test_cp_de_una_nota_no_hace_oscilar_el_indice(tmp_path: Path, index_path: Path):
    """`cp Notas/X.md Notas/X-copia.md` deja dos páginas, no una ruleta.

    El `id` del frontmatter viaja con la copia. Si el reindexado del vigilante
    lo aplicara tal cual, `UPSERT_PAGE` movería la MISMA fila de ruta en ruta en
    cada pasada: título alternando en el árbol, un `added` por pasada para
    siempre, una transacción por pasada y `/status` con deriva permanente. La
    regla es la de `reader._merge_page`: el fichero que llega se indexa con el
    `id` derivado de su ruta.
    """
    from app import index as index_mod
    from app.vault import drift
    from app.vault.store import VaultStore, derived_id
    from app.vault.watcher import VaultWatcher

    vault = tmp_path / "vault"
    original = escribir(vault, "Notas/Revisor.md", id_=ID_EXTERNA, cuerpo="original")
    watcher = VaultWatcher(
        str(vault), str(index_path), interval=0.2, publish=lambda evento: None
    )
    assert [e["action"] for e in watcher.run_once()] == ["added"]

    # La copia, por fuera de la app y con el `id` del original dentro.
    copia = vault / "Notas" / "Revisor-copia.md"
    shutil.copyfile(original, copia)

    primera = watcher.run_once()
    assert [(e["action"], e["path"]) for e in primera] == [
        ("added", "Notas/Revisor-copia.md")
    ], primera
    assert primera[0]["page_id"] == derived_id("Notas/Revisor-copia.md"), (
        "la copia se quedó con el id del original: la fila va a saltar de ruta"
    )

    conn = index_mod.open_index(str(index_path))
    try:
        filas = conn.execute("SELECT id, path, title FROM pages ORDER BY path").fetchall()
        paginas = [(fila["path"], fila["title"]) for fila in filas]
        ids = {fila["path"]: fila["id"] for fila in filas}
        assert paginas == [
            ("Notas/Revisor-copia.md", "Revisor-copia"),
            ("Notas/Revisor.md", "Revisor"),
        ], paginas
        assert len(set(ids.values())) == 2, f"dos ficheros con el mismo id: {ids}"
        assert ids["Notas/Revisor.md"] == ID_EXTERNA, "quien ya estaba perdió su id"
        assert drift.diff(str(vault), conn).changed == 0, "el índice no refleja el disco"
    finally:
        conn.close()

    # Segunda y tercera pasada: nada que indexar, ni un evento, ni deriva.
    assert watcher.run_once() == []
    assert watcher.run_once() == [], "el índice sigue teniendo algo que reindexar"

    # Y lo que dice el índice es lo que diría un reindexado entero: dos páginas,
    # mismas rutas y mismos títulos (el `id` del frontmatter se lo queda uno de
    # los dos; el reindexado entero se lo da al primero por ruta, ver
    # `VaultStore._page_id_for`).
    conn = index_mod.open_index(str(tmp_path / "rebuild.sqlite"))
    try:
        VaultStore(str(vault), conn).rebuild()
        reconstruido = [
            (fila["path"], fila["title"])
            for fila in conn.execute("SELECT path, title FROM pages ORDER BY path")
        ]
    finally:
        conn.close()
    assert reconstruido == paginas


def test_cp_de_una_base_no_tumba_el_vigilante(tmp_path: Path, index_path: Path):
    """Copiar el `.md` de una base trae `id`s de vistas repetidos: igual de estable."""
    from app import index as index_mod
    from app.vault import drift
    from app.vault.watcher import VaultWatcher

    vault = tmp_path / "vault"
    base = vault / "Bases" / "Contactos.md"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(BASE_MD.format(id=ID_BASE), encoding="utf-8")
    watcher = VaultWatcher(
        str(vault), str(index_path), interval=0.2, publish=lambda evento: None
    )
    assert [e["action"] for e in watcher.run_once()] == ["added"]

    shutil.copyfile(base, base.with_name("Contactos copia.md"))
    eventos = watcher.run_once()
    assert [(e["action"], e["path"]) for e in eventos] == [
        ("added", "Bases/Contactos copia.md")
    ], eventos

    conn = index_mod.open_index(str(index_path))
    try:
        assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM databases").fetchone()[0] == 2
        assert drift.diff(str(vault), conn).changed == 0
    finally:
        conn.close()

    assert watcher.run_once() == [], "la base copiada se reindexa en cada pasada"


# --------------------------------------------------------------------------
# 7. bases y filas tocadas por fuera (sin `POST /reindex`)
# --------------------------------------------------------------------------


def _escribir_base_y_fila(vault_path: Path) -> tuple[Path, Path]:
    base = vault_path / "Bases" / "Contactos.md"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(BASE_MD.format(id=ID_BASE), encoding="utf-8")
    carpeta = vault_path / "Bases" / "Contactos"
    carpeta.mkdir()
    fila = carpeta / "Acme.md"
    fila.write_text(
        FILA_MD.format(id=ID_FILA, empresa="Acme", fase="Lead"), encoding="utf-8"
    )
    return base, fila


def test_una_base_tocada_por_fuera_se_indexa_se_edita_y_se_borra(vigilante, vault_path):
    base, _fila = _escribir_base_y_fila(vault_path)

    def base_indexada():
        return next(
            (b for b in vigilante.get("/databases").json() if b["id"] == ID_BASE), None
        )

    assert esperar(base_indexada), "la base escrita por fuera no llegó al índice"

    # Modificada por fuera: el schema es el del fichero, sin reindexar a mano.
    base.write_text(BASE_MD_AMPLIADA.format(id=ID_BASE), encoding="utf-8")
    assert esperar(
        lambda: [p["key"] for p in base_indexada()["schema_def"]]
        == ["empresa", "fase", "nota"]
    ), "el schema nuevo de la base no llegó al índice"

    # Borrada por fuera: sale del índice y su fila sigue viva (el fichero manda).
    base.unlink()
    assert esperar(lambda: base_indexada() is None), "la base borrada sigue en /databases"
    assert vigilante.get(f"/pages/{ID_BASE}").status_code == 404
    assert esperar(lambda: vigilante.get(f"/pages/{ID_FILA}").status_code == 200), (
        "borrar la base por fuera se llevó por delante el fichero de su fila"
    )


def test_una_fila_tocada_por_fuera_se_indexa_se_edita_y_se_borra(vigilante, vault_path):
    _base, fila = _escribir_base_y_fila(vault_path)

    def filas():
        respuesta = vigilante.get(f"/databases/{ID_BASE}/rows")
        return respuesta.json() if respuesta.status_code == 200 else []

    assert esperar(lambda: [f["title"] for f in filas()] == ["Acme"]), (
        "la fila escrita por fuera no llegó a la tabla de su base"
    )

    # Modificada por fuera: las propiedades se releen del fichero.
    fila.write_text(
        FILA_MD.format(id=ID_FILA, empresa="Acme Renombrada", fase="Cerrada"),
        encoding="utf-8",
    )
    assert esperar(
        lambda: [f["properties"] for f in filas()]
        == [{"empresa": "Acme Renombrada", "fase": "Cerrada"}]
    ), "las propiedades de la fila no se releeron del fichero"

    # Borrada por fuera: fuera de la tabla y fuera del índice.
    fila.unlink()
    assert esperar(lambda: filas() == []), "la fila borrada sigue en la tabla"
    assert vigilante.get(f"/pages/{ID_FILA}").status_code == 404


# --------------------------------------------------------------------------
# 8. el bus: `publish` en el hilo del vigilante con altas y bajas en el bucle
# --------------------------------------------------------------------------


def test_el_bus_reparte_con_altas_y_bajas_concurrentes():
    """Sin candado, `list(self._queues)` puede caer a mitad de una mutación."""
    from app.events import EventBus

    bus = EventBus()
    errores: list[BaseException] = []

    async def escenario():
        detener = threading.Event()

        def publicar():
            try:
                while not detener.is_set():
                    bus.publish({"type": "page"})
                    time.sleep(0.0005)
            except BaseException as exc:  # noqa: BLE001 - se afirma abajo
                errores.append(exc)

        hilo = threading.Thread(target=publicar, name="p8-publica")
        cola_fija = bus.subscribe()
        hilo.start()
        try:
            for _ in range(500):
                bus.unsubscribe(bus.subscribe())
                await asyncio.sleep(0)
        finally:
            detener.set()
            hilo.join(5)
            bus.unsubscribe(cola_fija)

    asyncio.run(escenario())

    assert errores == [], f"publish reventó con altas y bajas concurrentes: {errores}"
    assert bus.subscribers == 0
