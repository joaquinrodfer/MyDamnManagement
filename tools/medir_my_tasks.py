"""Medición de `GET /my-tasks` con un vault fabricado de 20 bases × 200 filas.

No es parte de la suite: se ejecuta a mano y pega el resultado en la tarjeta.
Mide, sobre el índice real (los `.md` se escriben de verdad en un vault
temporal y se indexan por el camino normal):

- cuánto tarda la vista agregada (una petición HTTP con sesión, por el
  middleware de verdad);
- cuánto tarda el camino que la tarjeta descarta (listar bases + pedir las
  filas de cada base en serie), para que la comparación no sea una opinión;
- cuántas consultas SQL y cuánto tiempo hay por debajo, sin HTTP.
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "backend"))

TMP = Path(tempfile.mkdtemp(prefix="mdm-perf-"))
VAULT = TMP / "vault"
VAULT.mkdir()
INDEX = TMP / "indice.sqlite"

os.environ["FRONTEND_DIR"] = str(RAIZ / "frontend")
os.environ["VAULT_PATH"] = str(VAULT)
os.environ["INDEX_PATH"] = str(INDEX)
os.environ["WATCH_ENABLED"] = "0"
os.environ.pop("ADMIN_USER", None)
os.environ.pop("ADMIN_PASSWORD", None)

from app.config import settings  # noqa: E402

settings.vault_path = str(VAULT)
settings.index_path = str(INDEX)

from app import auth, mytasks  # noqa: E402
from app.index import ensure_index, open_index  # noqa: E402
from app.repository import Repository  # noqa: E402

BASES = 20
ROWS_PER_BASE = 200
NOTES = 8000  # 4000 filas + 8000 notas = 12.000 páginas, el tamaño de la tarjeta
ESTADOS = ["Abierto", "En curso", "Hecho", "Cerrado"]
MESES = ["11", "12", "2027-01"]


def schema(with_date: bool = True) -> list[dict]:
    props: list[dict] = [
        {"key": "responsable", "name": "Responsable", "type": "person"},
        {"key": "estado", "name": "Estado", "type": "select", "options": ESTADOS},
    ]
    if with_date:
        props.append({"key": "vence", "name": "Vence", "type": "date"})
    return props


class Contador:
    """Cuenta `execute` sobre una conexión real sin tocar la librería."""

    def __init__(self, real):
        self.real = real
        self.n = 0

    def execute(self, *a, **k):
        self.n += 1
        return self.real.execute(*a, **k)

    def __getattr__(self, name):
        return getattr(self.real, name)


def main() -> None:
    print(f"índice: {INDEX}")
    print(f"vault:  {VAULT}")
    ensure_index(str(INDEX))
    conn = open_index(str(INDEX))
    repo = Repository(conn)
    repo.ensure_workspace()

    ana = auth.create_user(conn, username="ana", display_name="Ana", password="x" * 12)
    bea = auth.create_user(conn, username="bea", display_name="Bea", password="x" * 12)

    t0 = time.perf_counter()
    bases = [
        repo.create_database(
            title=f"Base {n:02d}",
            schema_def=schema(with_date=(n % 4 != 0)),  # 5 bases sin fecha
            created_by="Ana",
        )
        for n in range(BASES)
    ]
    t_bases = time.perf_counter() - t0

    t0 = time.perf_counter()
    for n, base in enumerate(bases):
        for k in range(ROWS_PER_BASE):
            props: dict = {"responsable": ana.id if k % 3 else bea.id, "estado": ESTADOS[k % 4]}
            if n % 4 != 0 and k % 2 == 0:
                props["vence"] = f"2026-{MESES[k % 3]}-{(k % 28) + 1:02d}"
            repo.vault.create_row(base.id, title=f"Tarea {k:03d} de {n:02d}", properties=props)
    t_rows = time.perf_counter() - t0

    t0 = time.perf_counter()
    for n in range(NOTES):
        repo.create_page(title=f"Nota {n:05d}", body_markdown="texto de relleno")
    t_notes = time.perf_counter() - t0

    total = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
    total_rows = conn.execute("SELECT count(*) FROM rows").fetchone()[0]
    print(
        f"vault fabricado: {BASES} bases en {t_bases:.1f} s, "
        f"{total_rows} filas en {t_rows:.1f} s, {NOTES} notas en {t_notes:.1f} s "
        f"({total} páginas en el índice)"
    )
    conn.close()

    # ---------------------------------------------------- petición de verdad
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        # El arranque reindexa el vault en un hilo (P16): hasta que /health no
        # diga `ok`, las rutas del índice contestan 503 a propósito.
        limite = time.monotonic() + 600
        while time.monotonic() < limite:
            salud = client.get("/health")
            if salud.status_code == 200 and salud.json().get("status") == "ok":
                break
            time.sleep(0.2)
        else:
            raise SystemExit(f"/health no llegó a ok: {salud.text[:200]}")

        r = client.post("/auth/login", json={"username": "ana", "password": "x" * 12})
        assert r.status_code == 200, r.text

        medidas = []
        respuesta = None
        for _ in range(10):
            t0 = time.perf_counter()
            respuesta = client.get("/my-tasks")
            medidas.append((time.perf_counter() - t0) * 1000)
            assert respuesta.status_code == 200, respuesta.text
        datos = respuesta.json()
        mediana = statistics.median(medidas)
        print(
            f"\nGET /my-tasks (20 bases, {total_rows} filas, {total} páginas): "
            f"mediana {mediana:.1f} ms, min {min(medidas):.1f} ms, "
            f"max {max(medidas):.1f} ms -> {datos['count']} tareas mías"
        )

        # El camino descartado: listar bases y pedir las filas de cada una en
        # serie, como tendría que hacer el panel sin endpoint agregado.
        t0 = time.perf_counter()
        listado = client.get("/databases").json()
        filas = 0
        for base in listado:
            r = client.get(f"/databases/{base['id']}/rows")
            assert r.status_code == 200, r.text
            filas += len(r.json())
        t_serie = (time.perf_counter() - t0) * 1000
        print(
            f"N llamadas (1 + {len(listado)} de filas, en serie): {t_serie:.1f} ms "
            f"y {filas} filas por la red"
        )
        print(f"diferencia: {t_serie / mediana:.1f}x\n")

    # ------------------------------------------- consultas y tiempo en crudo
    conn = open_index(str(INDEX))
    contador = Contador(conn)
    t0 = time.perf_counter()
    salida = mytasks.my_tasks(contador, ana.id)
    t_crudo = (time.perf_counter() - t0) * 1000
    print(
        f"app.mytasks.my_tasks sin HTTP: {t_crudo:.1f} ms, {contador.n} consultas SQL, "
        f"{salida['count']} tareas"
    )
    conn.close()


if __name__ == "__main__":
    main()
