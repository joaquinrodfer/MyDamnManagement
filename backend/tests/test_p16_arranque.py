"""P16: el índice se construye en segundo plano y `/health` lo cuenta.

Lo que se comprueba aquí, y por qué:

- con el índice a medio construir, `/health` dice `starting` (no `ok`) y las
  rutas del índice devuelven `503`: nunca una lista a medias que el panel no
  pueda distinguir de un vault vacío;
- el panel y la pantalla de entrada SÍ se sirven: el navegador tiene que poder
  pintar algo mientras se indexa;
- si el reindexado revienta, `/health` pasa a `error` (503) con el motivo;
- si lleva demasiado tiempo sin avanzar, también: un indexado atascado no es
  «casi listo», es un fallo;
- el arranque sigue leyendo el vault entero en cada arranque (ADR-0001 D2): lo
  que cambia es que no bloquea, no que se deje de reindexar;
- el porcentaje que publica `/health` mientras indexa no adelanta el final: el
  `100 %` es de `ok`, y la serie solo sube (ronda 2 de P16: antes decía 100 %
  desde el primer segundo y volvía a bajar al empezar a enlazar).

Este módulo pone `SIN_ESPERA_DE_ARRANQUE = True`: quiere VER el arranque a
medias, así que el `autouse` de `conftest` no le espera el `/health`.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

from app.vault.store import VaultError
from conftest import esperar_indice_listo

SIN_ESPERA_DE_ARRANQUE = True


def _esperar_estado(client, estado: str, timeout: float = 30.0) -> dict:
    """Espera a que `/health` diga `estado` y devuelve el cuerpo."""
    limite = time.monotonic() + timeout
    ultimo: dict = {}
    while time.monotonic() < limite:
        respuesta = client.get("/health")
        ultimo = respuesta.json()
        if ultimo.get("status") == estado:
            return ultimo
        time.sleep(0.02)
    raise AssertionError(f"/health no llegó a {estado!r}: {ultimo}")


@pytest.fixture()
def abrir_app(index_path, monkeypatch):
    """Abre un `TestClient` nuevo (= un arranque nuevo) sobre el índice del test.

    El `client` de `conftest` ya hace esto, pero aquí hace falta abrir la app
    varias veces dentro del mismo test y **sin** esperar al `/health`.
    """
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "index_path", str(index_path))
    return lambda: TestClient(app)


@pytest.fixture()
def freno_del_reindexado(monkeypatch):
    """Deja el reindexado de arranque parado hasta que el test lo suelte.

    Se frena `VaultStore.rebuild` (el trabajo del hilo) y no el hilo: así el
    arranque es el de verdad, con su fase rápida hecha y su fase lenta a
    medias, que es justo el estado que hay que poder mirar.
    """
    from app.vault.store import VaultStore

    freno = threading.Event()
    original = VaultStore.rebuild

    def rebuild_frenado(self, **kwargs):
        if not freno.wait(60):
            raise AssertionError("el test no soltó el reindexado")
        return original(self, **kwargs)

    monkeypatch.setattr(VaultStore, "rebuild", rebuild_frenado)
    yield freno
    freno.set()


def test_con_el_indice_a_medias_health_dice_starting_y_pages_da_503(
    abrir_app, freno_del_reindexado, acceso
):
    with abrir_app() as client:
        # El servidor ya contesta (eso es lo que P16 arregla) y no finge:
        salud = client.get("/health")
        assert salud.status_code == 200
        cuerpo = salud.json()
        assert cuerpo["status"] == "starting"
        assert cuerpo["status"] != "ok", "un /health ok sin índice es el fallo de P7"
        assert cuerpo["progress"]["phase"], "tiene que decir en qué anda"
        assert cuerpo["progress"]["elapsed_seconds"] >= 0

        # Las rutas del índice no devuelven una lista a medias: dicen que no.
        for ruta in ("/pages", "/pages/tree", "/status"):
            respuesta = client.get(ruta)
            assert respuesta.status_code == 503, f"{ruta}: {respuesta.text}"
            datos = respuesta.json()
            assert datos["status"] == "starting"
            assert "construyendo" in datos["detail"]
            assert respuesta.headers["Retry-After"] == "5"
            assert not isinstance(datos, list)

        # El panel y la pantalla de entrada sí se sirven: son estáticos y la
        # puerta de entrada no necesita el índice. `/` sigue pidiendo sesión
        # (P11a) y manda al login, que es lo que verá el navegador.
        assert client.get("/").status_code == 401
        assert (
            client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
            .status_code
            == 303
        )
        assert client.get("/login").status_code == 200
        assert client.get("/manifest.webmanifest").status_code == 200
        # Los estáticos del panel NO son públicos (P11a: todo pide sesión), así
        # que sin cookie dan 401 --pero 401, no 503: el arranque no los bloquea,
        # los deja llegar a la puerta de la sesión, que con cookie los sirve.
        assert client.get("/app.js").status_code == 401

        # Se suelta el reindexado: ahora sí, y solo ahora, dice ok.
        freno_del_reindexado.set()
        esperar_indice_listo(client)
        assert client.get("/health").json() == {"status": "ok"}
        acceso(client)
        assert client.get("/pages").status_code == 200
        assert client.get("/pages/tree").status_code == 200


def test_si_el_reindexado_revienta_health_lo_cuenta_y_la_api_no_sirve(
    abrir_app, monkeypatch
):
    from app.vault.store import VaultStore

    def rebuild_roto(self, **kwargs):
        raise VaultError("el vault no se puede recorrer: PermissionError(13, 'x')")

    monkeypatch.setattr(VaultStore, "rebuild", rebuild_roto)

    with abrir_app() as client:
        cuerpo = _esperar_estado(client, "error")
        assert "el vault no se puede leer" in cuerpo["detail"]
        salud = client.get("/health")
        assert salud.status_code == 503
        # Y las rutas del índice tampoco: un índice que no existe no se sirve
        # como si estuviera vacío.
        paginas = client.get("/pages")
        assert paginas.status_code == 503
        assert "no está disponible" in paginas.json()["detail"]


def test_un_indexado_atascado_es_un_fallo_no_un_arranque_eterno(
    abrir_app, freno_del_reindexado, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "startup_stall_seconds", 0.05)

    with abrir_app() as client:
        assert client.get("/health").json()["status"] == "starting"
        time.sleep(0.3)
        salud = client.get("/health")
        assert salud.status_code == 503
        datos = salud.json()
        assert datos["status"] == "error"
        assert datos["stalled"] is True
        assert "sin avanzar" in datos["detail"]
        # El resto de la API, igual de rota: no hay índice que servir.
        assert client.get("/pages").status_code == 503
        freno_del_reindexado.set()


def test_un_vault_que_no_se_puede_recorrer_deja_la_app_en_error(
    abrir_app, client, vault_path
):
    """El caso de P7, ahora sin que el proceso se muera.

    Antes el PermissionError tumbaba el arranque y el contenedor reiniciaba en
    bucle; ahora la app queda en pie diciendo qué pasa, y el `healthcheck` del
    compose falla (que es lo que tiene que hacer).
    """
    esperar_indice_listo(client)
    roto = vault_path / "Notas" / "Cerrada"
    roto.mkdir(parents=True, exist_ok=True)
    (roto / "dentro.md").write_text("---\nid: roto-1\n---\nhola", encoding="utf-8")
    os.chmod(roto, 0o000)
    try:
        with abrir_app() as otro:
            cuerpo = _esperar_estado(otro, "error")
            assert "PermissionError" in cuerpo["detail"]
            assert otro.get("/health").status_code == 503
    finally:
        os.chmod(roto, 0o700)


def test_el_arranque_sigue_reindexando_lo_que_cambio_por_fuera(
    abrir_app, client, vault_path, acceso
):
    """No se cambia QUÉ se indexa, solo CUÁNDO: el vault se relee en cada arranque."""
    esperar_indice_listo(client)
    (vault_path / "Notas").mkdir(parents=True, exist_ok=True)
    (vault_path / "Notas" / "Escrita por fuera.md").write_text(
        "---\nid: 11111111-1111-1111-1111-111111111111\n---\nhola desde el editor\n",
        encoding="utf-8",
    )

    with abrir_app() as otro:
        esperar_indice_listo(otro)
        acceso(otro)
        titulos = [p["title"] for p in otro.get("/pages").json()]
        assert "Escrita por fuera" in titulos


def test_un_starting_nunca_trae_percent_100(abrir_app, vault_path, monkeypatch):
    """El `100 %` lo pone `ok`, no la última pasada (ronda 2 de P16).

    Se frena el enlazado --la última de las cuatro pasadas, la que más tarda--
    y se mira `/health` con el índice a medias. Antes de esta ronda, en ese
    mismo instante `/health` decía `{"phase": "escribiendo el índice",
    "percent": 100}` desde el primer segundo, y al pasar a `enlazando` el
    número caía a 0: quien lo mirase (el `healthcheck`, el dueño en `docker
    compose up`) leía «ya está» cuando no había escrito ni una fila.
    """
    from app.vault import store as store_mod

    notas = vault_path / "Notas"
    notas.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        (notas / f"nota-{i}.md").write_text(
            "---\nid: %s\n---\nnota %d\n\n[[nota-0]]\n"
            % (uuid.uuid5(uuid.NAMESPACE_URL, f"p16-percent-{i}"), i),
            encoding="utf-8",
        )

    enlazando = threading.Event()
    suelto = threading.Event()
    original = store_mod.sync_wikilinks
    primera = True

    def sync_frenado(conn, page_id, workspace_id, body):
        nonlocal primera
        if primera:
            primera = False
            enlazando.set()
            assert suelto.wait(60), "el test no soltó el enlazado"
        return original(conn, page_id, workspace_id, body)

    monkeypatch.setattr(store_mod, "sync_wikilinks", sync_frenado)

    with abrir_app() as client:
        assert enlazando.wait(60), "el arranque no llegó a enlazar"
        try:
            salud = client.get("/health")
            assert salud.status_code == 200
            cuerpo = salud.json()
            assert cuerpo["status"] == "starting"
            avance = cuerpo["progress"]
            assert avance["phase"] == "enlazando", avance
            assert avance["percent"] is not None, avance
            assert avance["percent"] < 100, avance
            # El `503` de las rutas del índice dice el mismo número, no otro.
            negada = client.get("/pages")
            assert negada.status_code == 503
            assert negada.json()["progress"]["percent"] == avance["percent"]
        finally:
            suelto.set()

        esperar_indice_listo(client)
        assert client.get("/health").json() == {"status": "ok"}


def test_el_progreso_del_reindexado_solo_sube_y_no_adelanta_el_100(
    vault_path, index_path
):
    """La serie completa del reindexado, sin HTTP.

    Con `2 * PROGRESS_EVERY` notas cada pasada publica muestras intermedias, así
    que se puede comprobar lo que de verdad lee quien mira `/health`: el
    porcentaje solo sube, el 100 % es la última muestra, y mientras queda
    enlazado por hacer no se pasa del 75 %.
    """
    from app.index import ensure_index, open_index
    from app.repository import Repository
    from app.vault.reader import PROGRESS_EVERY

    cuantas = 2 * PROGRESS_EVERY
    notas = vault_path / "Notas"
    notas.mkdir(parents=True, exist_ok=True)
    for i in range(cuantas):
        (notas / f"nota-{i:04d}.md").write_text(
            "---\nid: %s\n---\nnota %d\n\n[[nota-0000]]\n"
            % (uuid.uuid5(uuid.NAMESPACE_URL, f"p16-serie-{i}"), i),
            encoding="utf-8",
        )

    muestras: list[tuple[str, int, int]] = []
    ensure_index(str(index_path))
    conn = open_index(str(index_path))
    try:
        repo = Repository(conn)
        repo.ensure_workspace()
        repo.reindex(
            progress=lambda fase, done, total: muestras.append((fase, done, total))
        )
    finally:
        conn.close()

    fases = [fase for fase, _, _ in muestras]
    for esperada in (
        "leyendo el vault", "preparando páginas", "escribiendo el índice", "enlazando"
    ):
        assert esperada in fases, f"falta la fase {esperada!r}: {fases}"

    porcentajes = [
        int(round(100.0 * done / total)) for _, done, total in muestras if total
    ]
    assert porcentajes == sorted(porcentajes), f"el porcentaje retrocede: {porcentajes}"
    assert porcentajes[-1] == 100, porcentajes
    assert 100 not in porcentajes[:-1], f"dice 100 antes de terminar: {porcentajes}"

    # Cuatro pasadas: la escritura empieza en el 50 % (dos de cuatro) y no
    # puede llegar al 75 % hasta haber escrito todas las filas.
    escritura = [(done, total) for fase, done, total in muestras
                 if fase == "escribiendo el índice"]
    assert escritura[0] == (2 * cuantas, 4 * cuantas), escritura[0]
    assert max(done for done, _ in escritura) <= 3 * cuantas, escritura
