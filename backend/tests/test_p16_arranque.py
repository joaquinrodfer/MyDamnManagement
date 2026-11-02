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
  que cambia es que no bloquea, no que se deje de reindexar.

Este módulo pone `SIN_ESPERA_DE_ARRANQUE = True`: quiere VER el arranque a
medias, así que el `autouse` de `conftest` no le espera el `/health`.
"""

from __future__ import annotations

import os
import threading
import time

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
