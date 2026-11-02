"""P16: el `healthcheck` del compose, sacado del YAML y ejecutado de verdad.

El script no se copia aquí: se **extrae** de `docker-compose.prod.yml`, que es
el que se despliega. Si alguien cambia el YAML, este test ejecuta lo nuevo.

Los tres estados que pide la tarjeta:

- `starting` (indexando): aceptable, código 0. Es el estado nuevo de P16 y el
  que antes no existía: el contenedor no contestaba y el chequeo fallaba.
- `ok` (listo): código 0 con el índice y el vault comprobados.
- vault ilegible: código 1 con «el vault no se puede recorrer: …» (el caso de
  P7), y también 1 si el índice está vacío con notas en el vault, si `/health`
  dice `error` o si no hay nada escuchando.

`/health` se sirve desde un servidor de pega con el JSON que toca (el script
solo mira `status`), salvo el caso «no hay API», que apunta a un puerto
cerrado. El puerto del `urlopen` se sustituye en el script extraído: el YAML
lleva el 8000 del contenedor.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

YAML = Path(__file__).resolve().parents[2] / "docker-compose.prod.yml"


def script_del_healthcheck() -> str:
    """El bloque `python -c` del `test:` del healthcheck, ya sin la sangría YAML."""
    lineas = YAML.read_text(encoding="utf-8").splitlines()
    comienzo = [i for i, linea in enumerate(lineas) if linea.strip() == "test:"]
    assert len(comienzo) == 1, f"hay {len(comienzo)} bloques 'test:' en {YAML}"

    i = comienzo[0]
    while lineas[i].strip() != "- |":
        i += 1
        assert i - comienzo[0] < 10, "no encuentro el script del healthcheck"

    cuerpo: list[str] = []
    for linea in lineas[i + 1 :]:
        if not linea.strip():
            cuerpo.append("")
            continue
        if not linea.startswith("          "):
            break
        cuerpo.append(linea[10:])

    script = "\n".join(cuerpo).rstrip() + "\n"
    assert "urllib.request" in script and "VAULT_PATH" in script
    return script


def _cuerpo_del_healthcheck(script: str, puerto: int) -> str:
    # El 8000 es el puerto DENTRO del contenedor; en el test el servidor de
    # pega escucha en uno libre.
    marcador = "http://127.0.0.1:8000/health"
    assert script.count(marcador) == 1
    return script.replace(marcador, f"http://127.0.0.1:{puerto}/health")


@contextmanager
def _api_de_pega(codigo: int, cuerpo: dict):
    """Un `/health` que devuelve `cuerpo` con `codigo` (nada más importa)."""
    datos = json.dumps(cuerpo).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos)

        def log_message(self, *args):  # silencio: el test no es un log
            pass

    servidor = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
    hilo.start()
    try:
        yield servidor.server_address[1]
    finally:
        servidor.shutdown()
        servidor.server_close()
        hilo.join(timeout=5)


def _ejecutar(script: str, *, puerto: int, vault: Path, index: Path):
    return subprocess.run(
        [sys.executable, "-c", _cuerpo_del_healthcheck(script, puerto)],
        env={
            **os.environ,
            "VAULT_PATH": str(vault),
            "INDEX_PATH": str(index),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


def _indice_con_una_nota(index_path: Path) -> None:
    """Índice de verdad con una nota (y su `.md` en el vault del test)."""
    from app.index import ensure_index, open_index
    from app.repository import Repository

    ensure_index(str(index_path))
    conn = open_index(str(index_path))
    try:
        repo = Repository(conn)
        repo.ensure_workspace()
        repo.create_page(title="Una nota", body_markdown="hola")
    finally:
        conn.close()


def test_indexando_es_aceptable(index_path, vault_path):
    """`starting` sale con 0: el contenedor está vivo y trabajando (P16)."""
    script = script_del_healthcheck()
    # Un índice vacío y notas en el vault: si el chequeo no distinguiera
    # `starting` de `ok`, este caso fallaría con «el índice está vacío».
    (vault_path / "Notas").mkdir(parents=True, exist_ok=True)
    (vault_path / "Notas" / "pendiente.md").write_text("hola", encoding="utf-8")

    with _api_de_pega(
        200,
        {
            "status": "starting",
            "progress": {
                "phase": "leyendo el vault",
                "done": 200,
                "total": 12000,
                "percent": 1,
                "elapsed_seconds": 3.5,
            },
        },
    ) as puerto:
        resultado = _ejecutar(script, puerto=puerto, vault=vault_path, index=index_path)

    assert resultado.returncode == 0, resultado.stderr
    assert "indexando" in resultado.stdout and "leyendo el vault" in resultado.stdout


def test_listo_da_ok_con_el_indice_y_el_vault(index_path, vault_path):
    script = script_del_healthcheck()
    _indice_con_una_nota(index_path)

    with _api_de_pega(200, {"status": "ok"}) as puerto:
        resultado = _ejecutar(script, puerto=puerto, vault=vault_path, index=index_path)

    assert resultado.returncode == 0, resultado.stderr
    assert "ok: índice v" in resultado.stdout
    assert "1 ficheros del vault" in resultado.stdout


def test_health_en_error_falla_y_dice_por_que(index_path, vault_path):
    script = script_del_healthcheck()
    (vault_path / "Notas").mkdir(parents=True, exist_ok=True)
    (vault_path / "Notas" / "una.md").write_text("hola", encoding="utf-8")

    with _api_de_pega(
        503, {"status": "error", "detail": "el vault no se puede leer: VaultError"}
    ) as puerto:
        resultado = _ejecutar(script, puerto=puerto, vault=vault_path, index=index_path)

    assert resultado.returncode == 1
    assert "healthcheck: /health dice error" in resultado.stderr
    assert "el vault no se puede leer" in resultado.stderr


def test_el_indice_vacio_con_notas_en_el_vault_falla(index_path, vault_path):
    """El susto de P11a: índice recreado vacío y el panel en blanco, `healthy`."""
    from app.index import ensure_index

    script = script_del_healthcheck()
    ensure_index(str(index_path))
    (vault_path / "Notas").mkdir(parents=True, exist_ok=True)
    (vault_path / "Notas" / "una.md").write_text("hola", encoding="utf-8")

    with _api_de_pega(200, {"status": "ok"}) as puerto:
        resultado = _ejecutar(script, puerto=puerto, vault=vault_path, index=index_path)

    assert resultado.returncode == 1
    assert "el índice está vacío" in resultado.stderr


def test_el_vault_ilegible_falla_con_su_mensaje(index_path, vault_path):
    """El caso de P7: `os.walk` sin `onerror` contaba 0 ficheros y daba healthy."""
    script = script_del_healthcheck()
    _indice_con_una_nota(index_path)
    cerrada = vault_path / "Notas" / "Cerrada"
    cerrada.mkdir(parents=True, exist_ok=True)
    (cerrada / "dentro.md").write_text("hola", encoding="utf-8")
    os.chmod(cerrada, 0o000)
    try:
        with _api_de_pega(200, {"status": "ok"}) as puerto:
            resultado = _ejecutar(
                script, puerto=puerto, vault=vault_path, index=index_path
            )
    finally:
        os.chmod(cerrada, 0o700)

    assert resultado.returncode == 1
    assert "el vault no se puede recorrer" in resultado.stderr


def test_si_falta_el_fichero_del_indice_lo_dice(tmp_path, vault_path):
    """Sin índice no es «no se puede escribir»: es que no está donde se mira."""
    script = script_del_healthcheck()
    (vault_path / "Notas").mkdir(parents=True, exist_ok=True)
    (vault_path / "Notas" / "una.md").write_text("hola", encoding="utf-8")

    with _api_de_pega(200, {"status": "ok"}) as puerto:
        resultado = _ejecutar(
            script, puerto=puerto, vault=vault_path, index=tmp_path / "no-existe.sqlite"
        )

    assert resultado.returncode == 1
    assert "no existe el índice" in resultado.stderr


def test_sin_api_escuchando_falla(index_path, vault_path):
    script = script_del_healthcheck()
    # Un puerto que estaba libre hace un instante: nadie escucha ahí.
    with socket.socket() as hueco:
        hueco.bind(("127.0.0.1", 0))
        puerto = hueco.getsockname()[1]

    resultado = _ejecutar(script, puerto=puerto, vault=vault_path, index=index_path)

    assert resultado.returncode == 1
    assert "la API no responde en /health" in resultado.stderr
