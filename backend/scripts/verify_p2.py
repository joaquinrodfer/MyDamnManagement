#!/usr/bin/env python3
"""Demostración ejecutable de P2: bases, filas y vistas en ficheros.

Corre la app de verdad (FastAPI + `TestClient`, sin abrir puertos) sobre un
vault y un índice en directorios temporales, y comprueba el criterio de hecho de
la tarjeta:

1. crear una base desde la plantilla CRM -> aparecen `Bases/Contactos.md` (con
   `type: database`, el `schema` y las `views`) y su carpeta de filas;
2. tres filas -> tres `.md` con las propiedades en el frontmatter y sin `title`;
3. editar una propiedad -> cambia el `.md` de la fila (y solo esa);
4. **borrar el índice**, arrancar de nuevo -> bases, filas y vistas siguen ahí y
   la huella de volcado coincide tabla a tabla;
5. ningún endpoint escribe solo en el índice: tras cada escritura, reindexar
   deja la huella idéntica; y en el código, todas las escrituras del índice
   viven en `app/vault/store.py`, junto a su fichero.

Sale con código 1 si alguna comprobación falla. Uso:
`python3 backend/scripts/verify_p2.py`.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

WORK = Path(tempfile.mkdtemp(prefix="mdm-p2-"))
VAULT = WORK / "vault"
INDEX = WORK / "index.sqlite"

os.environ["FRONTEND_DIR"] = str(REPO_DIR / "frontend")
os.environ["VAULT_PATH"] = str(VAULT)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402

settings.vault_path = str(VAULT)
settings.index_path = str(INDEX)

from app import index as indice  # noqa: E402
from app.main import app  # noqa: E402

ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    ok = ok and bool(condition)
    print(f"  [{'OK ' if condition else 'FALLO'}] {label}" + (f" — {detail}" if detail else ""))


def texto(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def frontmatter(path: Path) -> dict:
    """El bloque YAML del fichero, con el parser de la app."""
    from app.vault import fm

    raw, _cuerpo = fm.split_document(texto(path))
    return fm.parse(raw).data if raw is not None else {}


def huella() -> tuple[str, dict]:
    return indice.dump_hash(str(INDEX))


def sin_solo_indice() -> list[str]:
    """Escrituras del índice fuera del puente del vault (`app/vault/store.py`)."""
    patrones = re.compile(
        r"(INSERT INTO (pages|databases|views|rows)\b"
        r"|UPDATE (pages|databases|views|rows)\b"
        r"|DELETE FROM (pages|databases|views|rows)\b)"
    )
    culpables: list[str] = []
    for path in sorted((BACKEND_DIR / "app").rglob("*.py")):
        if path.name == "store.py":      # el único que escribe y escribe ficheros
            continue
        relativo = path.relative_to(REPO_DIR)
        if path.parent.name == "routers":
            if "execute(" in texto(path):
                culpables.append(f"{relativo} (router con SQL propio)")
            continue
        for numero, linea in enumerate(texto(path).splitlines(), start=1):
            if patrones.search(linea) and "CREATE TRIGGER" not in linea:
                culpables.append(f"{relativo}:{numero}: {linea.strip()[:70]}")
    return culpables


def main() -> int:
    print(f"Vault temporal : {VAULT}")
    print(f"Índice         : {INDEX}\n")

    with TestClient(app) as client:
        # ---- 1. la base es un fichero -------------------------------------
        print("1. crear una base desde la plantilla CRM")
        base = client.post("/databases/from-template", json={"template": "crm"}).json()
        fichero_base = VAULT / "Bases" / "Contactos.md"
        check("existe Bases/Contactos.md", fichero_base.exists())
        check("la carpeta de filas existe", (VAULT / "Bases" / "Contactos").is_dir())
        contenido = texto(fichero_base)
        check("type: database en el frontmatter", "type: database" in contenido)
        check("id de la base = id de su página", base["id"] == base["page_id"]
              and f"id: {base['page_id']}" in contenido)
        check("el schema va en el frontmatter", "schema:" in contenido and "key: empresa" in contenido)
        check("las vistas van en el frontmatter con su group_by",
              "views:" in contenido and "group_by: fase" in contenido)
        check("la API devuelve las dos vistas de la plantilla",
              sorted(v["name"] for v in client.get(f"/databases/{base['id']}/views").json())
              == ["Pipeline", "Tabla"])

        # ---- 2. tres filas, tres ficheros --------------------------------
        print("\n2. tres filas")
        filas = []
        for titulo, props in (
            ("Acme", {"empresa": "Acme S.L.", "fase": "lead", "valor": 12000}),
            ("Globex", {"empresa": "Globex", "fase": "lead", "valor": 500}),
            ("Initech", {"empresa": "Initech", "fase": "contactado"}),
        ):
            fila = client.post(
                f"/databases/{base['id']}/rows", json={"title": titulo, "properties": props}
            ).json()
            filas.append(fila)
        rutas = [VAULT / "Bases" / "Contactos" / f"{f['title']}.md" for f in filas]
        check("tres ficheros de fila", all(r.exists() for r in rutas),
              ", ".join(r.name for r in rutas))
        acme = frontmatter(rutas[0])
        check("las propiedades están en el frontmatter",
              acme.get("empresa") == "Acme S.L." and acme.get("fase") == "lead"
              and acme.get("valor") == "12000", str(acme))
        check("la fila NO lleva title (el título es el nombre)", "title" not in acme)
        check("la API devuelve las tres filas",
              len(client.get(f"/databases/{base['id']}/rows").json()) == 3)

        # ---- 3. editar una propiedad -------------------------------------
        print("\n3. editar una propiedad")
        editada = client.patch(
            f"/databases/{base['id']}/rows/{filas[0]['id']}",
            json={"properties": {"fase": "ganado"}},
        ).json()
        check("la respuesta trae la fase nueva", editada["properties"]["fase"] == "ganado")
        acme = frontmatter(rutas[0])
        check("el .md de la fila cambia", acme.get("fase") == "ganado", str(acme))
        check("lo que no llegó se conserva",
              acme.get("empresa") == "Acme S.L." and acme.get("valor") == "12000")
        check("las demás filas no se tocan", frontmatter(rutas[1]).get("fase") == "lead")

        # ---- 4. nada vive solo en el índice ------------------------------
        print("\n4. ninguna escritura vive solo en el índice")
        antes, antes_cuentas = huella()
        respuesta = client.post("/reindex")
        despues, despues_cuentas = huella()
        check("reindexar deja la misma huella", antes == despues and respuesta.status_code == 200)
        check("y los mismos recuentos tabla a tabla", antes_cuentas == despues_cuentas,
              str(antes_cuentas))
        check("el reindexado ve 1 base, 3 filas y 2 vistas",
              (despues_cuentas["databases"], despues_cuentas["rows"], despues_cuentas["views"])
              == (1, 3, 2))
        culpables = sin_solo_indice()
        check(
            "fuera de vault/store.py no hay escrituras de páginas/bases/filas",
            not [c for c in culpables if "workspaces" not in c and "links" not in c],
            "; ".join(culpables) or "solo el workspace singleton y los enlaces derivados",
        )

        # ---- 5. borrar el índice y arrancar ------------------------------
        print("\n5. borrar el índice y arrancar")

    for sufijo in ("", "-wal", "-shm"):
        Path(str(INDEX) + sufijo).unlink(missing_ok=True)
    check("el índice ya no existe", not Path(str(INDEX)).exists())

    before_hash = antes
    with TestClient(app) as otro:
        base_otra = otro.get(f"/databases/{base['id']}")
        check("la base sigue (404 si no)", base_otra.status_code == 200)
        check("con su título", base_otra.json()["title"] == "Contactos")
        check("con su schema",
              [p["key"] for p in base_otra.json()["schema_def"]]
              == ["empresa", "fase", "valor", "proximo_contacto"])
        vistas = otro.get(f"/databases/{base['id']}/views").json()
        check("con sus vistas y su group_by",
              [(v["name"], v["config"].get("group_by")) for v in vistas]
              == [("Pipeline", "fase"), ("Tabla", None)])
        filas_otra = otro.get(f"/databases/{base['id']}/rows").json()
        check("con sus tres filas", len(filas_otra) == 3)
        acme_otra = next(f for f in filas_otra if f["title"] == "Acme")
        check("y la propiedad editada sigue editada",
              acme_otra["properties"] == {"empresa": "Acme S.L.", "fase": "ganado", "valor": 12000},
              str(acme_otra["properties"]))
        check("los ids de las filas son los mismos",
              sorted(f["id"] for f in filas_otra) == sorted(f["id"] for f in filas))

    despues_hash, despues_cuentas = huella()
    check("la huella del índice reconstruido es idéntica", despues_hash == before_hash,
          f"{despues_hash[:16]}… vs {before_hash[:16]}…")
    check("y los recuentos también", despues_cuentas == antes_cuentas, str(despues_cuentas))

    print("\nResultado:", "TODO OK" if ok else "HAY FALLOS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
