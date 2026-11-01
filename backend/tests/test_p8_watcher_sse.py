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
import threading
import time
from pathlib import Path

import pytest

# Id fijos en el frontmatter: la app los toma tal cual (son UUID válidos). Un
# fichero sin `id` recibe uno derivado de la ruta; aquí se prefieren fijos para
# poder afirmar sobre ellos.
ID_EXTERNA = "11111111-1111-4111-8111-111111111111"
ID_OTRO = "22222222-2222-4222-8222-222222222222"


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
