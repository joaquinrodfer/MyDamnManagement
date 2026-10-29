#!/usr/bin/env python3
"""Demostración ejecutable de P1.2a: los ficheros son la verdad de las notas.

Corre la app de verdad (FastAPI + `TestClient`, sin abrir puertos) sobre un
vault y un índice en directorios temporales, y comprueba los cuatro puntos del
criterio de hecho:

1. crear una nota desde la API -> aparece su `.md` en el vault;
2. editar ese `.md` por fuera -> `POST /reindex` -> la app lo ve;
3. borrar el índice -> arrancar -> índice reconstruido con la misma huella;
4. borrar una nota -> va a `.trash/<fecha UTC>/<ruta original>`.

Sale con código 1 si alguna comprobación falla. Uso: `python3
backend/scripts/verify_p1_2a.py`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

WORK = Path(tempfile.mkdtemp(prefix="mdm-p12a-"))
VAULT = WORK / "vault"
INDEX = WORK / "index.sqlite"

os.environ["FRONTEND_DIR"] = str(REPO_DIR / "frontend")
os.environ["ATTACHMENTS_DIR"] = str(WORK / "attachments")
os.environ["VAULT_PATH"] = str(VAULT)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402

settings.vault_path = str(VAULT)
settings.index_path = str(INDEX)
settings.attachments_dir = str(WORK / "attachments")

from app.index import dump_hash  # noqa: E402
from app.main import app  # noqa: E402

ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    ok = ok and condition
    print(f"  [{'OK ' if condition else 'FALLO'}] {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print(f"Vault temporal : {VAULT}")
    print(f"Índice         : {INDEX}\n")

    with TestClient(app) as client:
        # ---- 1. crear una nota escribe el fichero -------------------------
        padre = client.post(
            "/pages", json={"title": "Proyectos", "body_markdown": "Raíz del proyecto"}
        ).json()
        hija = client.post(
            "/pages",
            json={
                "title": "MyDamnManagement",
                "parent_id": padre["id"],
                "body_markdown": "Ver [[Proyectos]] y el presupuesto.",
            },
        ).json()
        ruta_padre = VAULT / "Notas" / "Proyectos.md"
        ruta_hija = VAULT / "Notas" / "Proyectos" / "MyDamnManagement.md"
        check("crear nota escribe Notas/Proyectos.md", ruta_padre.exists(), str(ruta_padre))
        check("la hija va a la carpeta del padre", ruta_hija.exists(), str(ruta_hija))
        texto = ruta_padre.read_text(encoding="utf-8")
        check("el frontmatter lleva el id de la API", f"id: {padre['id']}" in texto)
        print("    --- Notas/Proyectos.md ---")
        for line in texto.splitlines()[:8]:
            print(f"    | {line}")

        # ---- 2. editar por fuera y reindexar ------------------------------
        ruta_hija.write_text(
            ruta_hija.read_text(encoding="utf-8").replace(
                "y el presupuesto.", "y el presupuesto de 2027."
            ),
            encoding="utf-8",
        )
        antes = client.get(f"/pages/{hija['id']}").json()["body_markdown"]
        reindex = client.post("/reindex").json()
        despues = client.get(f"/pages/{hija['id']}").json()["body_markdown"]
        check("la API no ve el cambio antes de reindexar", "2027" not in antes)
        check(
            "POST /reindex recoge el cambio hecho por fuera",
            "2027" in despues,
            f"{reindex['notes']} notas en {reindex['seconds']} s",
        )

        # ---- 4. borrar manda a la papelera --------------------------------
        client.delete(f"/pages/{hija['id']}")
        papeleras = [p for p in (VAULT / ".trash").iterdir() if p.is_dir()]
        en_papelera = papeleras[0] / "Notas" / "Proyectos" / "MyDamnManagement.md" if papeleras else None
        check("borrar mueve el .md a .trash/<fecha>/<ruta original>",
              bool(en_papelera and en_papelera.exists()),
              str(en_papelera))
        # Se restaura para que el índice reconstruido incluya las dos notas.
        en_papelera.rename(ruta_hija)
        client.post("/reindex")

        hash_antes, cuentas_antes = dump_hash(str(INDEX))
        print(f"\n  índice escrito por la API : {cuentas_antes}  huella {hash_antes[:16]}")

    # ---- 3. borrar el índice y arrancar -----------------------------------
    for suffix in ("", "-wal", "-shm"):
        Path(str(INDEX) + suffix).unlink(missing_ok=True)
    check("índice borrado del disco", not INDEX.exists())

    with TestClient(app) as client:
        paginas = client.get("/pages").json()

    hash_despues, cuentas_despues = dump_hash(str(INDEX))
    print(f"  índice reconstruido       : {cuentas_despues}  huella {hash_despues[:16]}")
    check("el arranque reconstruye las notas del vault", len(paginas) == 2)
    check("mismo contenido tabla a tabla", cuentas_antes == cuentas_despues)
    check("misma huella de volcado", hash_antes == hash_despues)

    with TestClient(app) as client:
        leida = client.get(f"/pages/{hija['id']}").json()
        check("el id sobrevive al borrado del índice", leida["title"] == "MyDamnManagement")
        check("y el cuerpo editado por fuera sigue ahí", "2027" in leida["body_markdown"])

    print("\nResultado:", "TODO OK" if ok else "HAY FALLOS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
