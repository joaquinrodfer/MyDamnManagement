#!/usr/bin/env python3
"""Demostración ejecutable de P1.2b: el vault es un backup completo.

Corre la app de verdad (FastAPI + `TestClient`, sin abrir puertos) sobre un
vault y un índice temporales, y comprueba lo que promete la tarjeta:

1. subir una imagen de cabecera deja el adjunto **dentro** del vault;
2. copiar la carpeta del vault a otro sitio da una nota entera: `.md` + adjunto,
   y se comprueba **sin la app** (leyendo el `.md` y el fichero);
3. esa copia, sola, arranca la app: la nota sigue con su cabecera y `/files` la
   sirve (es decir, la copia no dependía de nada de fuera del vault);
4. de propina, las otras dos piezas: restaurar de la papelera y la deriva que
   cuenta `/status`.

Sale con código 1 si alguna comprobación falla. Uso: `python3
backend/scripts/verify_p1_2b.py`.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

WORK = Path(tempfile.mkdtemp(prefix="mdm-p12b-"))
VAULT = WORK / "vault"
INDEX = WORK / "index.sqlite"
BACKUP = WORK / "backup"
INDEX_COPIA = WORK / "index-copia.sqlite"

os.environ["FRONTEND_DIR"] = str(REPO_DIR / "frontend")
os.environ["VAULT_PATH"] = str(VAULT)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402

settings.vault_path = str(VAULT)
settings.index_path = str(INDEX)

from app.main import app  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"imagen-de-cabecera" * 8

ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    ok = ok and condition
    print(f"  [{'OK ' if condition else 'FALLO'}] {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print(f"Vault temporal : {VAULT}")
    print(f"Índice         : {INDEX}\n")

    with TestClient(app) as client:
        # ---- 1. adjunto dentro del vault --------------------------------
        nota = client.post(
            "/pages",
            json={"title": "Reunión con Acme", "body_markdown": "Ver [[Presupuesto]] y el acta."},
        ).json()
        subida = client.post(
            f"/pages/{nota['id']}/header-image",
            files={"file": ("cabecera.png", PNG, "image/png")},
        )
        check("subir una cabecera responde 200", subida.status_code == 200, subida.text)
        url = subida.json()["header_image_path"]
        rel = url[len("/files/"):]
        adjunto = VAULT / rel
        check("la URL es la ruta del vault", url.startswith("/files/_attachments/"), url)
        check("el adjunto está en el vault", adjunto.is_file(), str(adjunto))
        check("y son los mismos bytes", adjunto.read_bytes() == PNG)
        check(
            "fuera de _attachments/ no se sirve nada",
            client.get("/files/Notas/Reunión con Acme.md").status_code == 404,
        )

        # ---- 2. copia del vault, comprobada sin la app -------------------
        shutil.copytree(VAULT, BACKUP)
        copia_md = BACKUP / "Notas" / "Reunión con Acme.md"
        texto = copia_md.read_text(encoding="utf-8")
        check("la copia trae el .md", copia_md.is_file(), str(copia_md))
        aguja = "header_image: /files/"
        check("con su header_image en el frontmatter", aguja in texto)
        url_copia = texto.split(aguja, 1)[1].splitlines()[0].strip()
        adjunto_copia = BACKUP / url_copia
        check(
            "y el adjunto al que apunta también está en la copia",
            adjunto_copia.is_file(),
            str(adjunto_copia),
        )
        check("con los mismos bytes que el original", adjunto_copia.read_bytes() == PNG)
        print("    --- Notas/Reunión con Acme.md (copia) ---")
        for line in texto.splitlines()[:9]:
            print(f"    | {line}")

    # ---- 3. la copia sola arranca la app --------------------------------
    settings.vault_path = str(BACKUP)
    settings.index_path = str(INDEX_COPIA)
    with TestClient(app) as client:
        paginas = client.get("/pages").json()
        check("el arranque reindexa la copia sin tocar nada más", len(paginas) == 1,
              f"{len(paginas)} notas")
        recuperada = client.get(f"/pages/{nota['id']}").json()
        check("la nota conserva su id", recuperada["id"] == nota["id"])
        check("y su cabecera", recuperada["header_image_path"] == url, recuperada["header_image_path"])
        servida = client.get(url)
        check("/files sirve la imagen de la copia", servida.status_code == 200)
        check("con su tipo y su nosniff",
              servida.headers.get("content-type") == "image/png"
              and servida.headers.get("x-content-type-options") == "nosniff")

    # ---- 4. papelera y deriva ------------------------------------------
    settings.vault_path = str(VAULT)
    settings.index_path = str(INDEX)
    with TestClient(app) as client:
        client.delete(f"/pages/{nota['id']}")
        basura = client.get("/trash").json()
        entrada = next((e for e in basura if e["original_path"] == "Notas/Reunión con Acme.md"), None)
        check("borrar deja la nota en la papelera", entrada is not None)
        if entrada:
            print(f"    en papelera: {entrada['path']} ({entrada['deleted_at']})")
            restaurada = client.post("/trash/restore", json={"path": entrada["path"]})
            check("POST /trash/restore la devuelve a su sitio",
                  restaurada.status_code == 200
                  and (VAULT / "Notas" / "Reunión con Acme.md").is_file(), restaurada.text)
            check("y vuelve a la app con su cabecera",
                  client.get(f"/pages/{nota['id']}").json()["header_image_path"] == url)

        antes = client.get("/status").json()["drift"]
        check("sin cambios por fuera, la deriva es cero", antes["changed"] == 0,
              f"last_reindex_at={antes['last_reindex_at']}")

        fichero = VAULT / "Notas" / "Reunión con Acme.md"
        fichero.write_text(fichero.read_text(encoding="utf-8") + "\notra línea\n", encoding="utf-8")
        (VAULT / "Notas" / "Escrita a mano.md").write_text(
            "---\nid: 8f14e45f-ceea-467a-9f1b-0f0a1b2c3d4e\n---\n\na mano\n", encoding="utf-8"
        )
        durante = client.get("/status").json()["drift"]
        check("dos cambios por fuera: uno modificado y uno nuevo",
              (durante["modified"], durante["added"], durante["changed"]) == (1, 1, 2),
              str(durante))
        reindex = client.post("/reindex").json()
        despues = client.get("/status").json()["drift"]
        check("tras POST /reindex la deriva vuelve a cero", despues["changed"] == 0,
              f"{reindex['notes']} notas, last_reindex_at={despues['last_reindex_at']}")

    print("\nResultado:", "TODO OK" if ok else "HAY FALLOS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
