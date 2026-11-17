"""Mide un reindexado completo y saca la huella de la tabla `links`.

No es parte de la suite: se ejecuta a mano y su salida es la que se pega en la
tarjeta cuando se toca el enlazado o el volcado del índice. Es el método de
`docs/MEDICIONES-P16.md` hecho script, para que «antes» y «después» se midan
igual:

1. genera un vault como el de P16 —`--notas` notas en 40 carpetas, frontmatter,
   dos `[[wikilinks]]` por nota y un 5 % apuntando a un título que no existe,
   con `id` derivado de la ruta para que dos generaciones den los mismos ids;
2. arranca la app sobre un índice vacío (el arranque reindexa en un hilo, P16) y
   sondea `GET /health` cada 200 ms: de ahí sale la tabla de fases, la misma
   que publica `/health` mientras indexa;
3. con el índice hecho, `POST /reindex?force=true` y el total (es el mismo
   trabajo: un rebuild entero);
4. imprime `count(*)` de `links` y el `sha1` del volcado ordenado. Dos ramas con
   el mismo vault tienen que dar **el mismo sha1**: si cambia, el cambio ha
   tocado el resultado, no solo el coste.

Uso:

    python tools/medir_reindexado.py --notas 12000 --vault /var/tmp/v12k \\
        --indice /var/tmp/i12k.sqlite --etiqueta antes

    # la otra rama, con el MISMO vault y otro índice:
    python tools/medir_reindexado.py --notas 12000 --vault /var/tmp/v12k \\
        --indice /var/tmp/i12k-despues.sqlite --etiqueta despues --reusar

No abre puertos: la app va por `TestClient`, el mismo camino ASGI de la suite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import sys
import time
import uuid
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "backend"))

CARPETAS = 40
NS = uuid.UUID("6f3b7c1e-2d4a-4f8b-9c1d-0a2b3c4d5e6f")
RELLENO = (
    "El proyecto avanza por bloques pequeños y cada bloque deja el índice "
    "diciendo lo mismo que el fichero, que es la única verdad del vault. "
)


def generar_vault(raiz: Path, notas: int) -> None:
    """Escribe el vault de prueba (determinista: mismo `--notas`, mismos ids)."""
    rnd = random.Random(20261117)
    por_carpeta, resto = divmod(notas, CARPETAS)
    titulos: list[tuple[str, str]] = []
    for c in range(CARPETAS):
        for i in range(por_carpeta + (1 if c < resto else 0)):
            titulos.append((f"Proyecto {c:02d}", f"Nota {c:02d}-{i:04d}"))
    # Unos pocos títulos repetidos en carpetas distintas: el mapa plegado tiene
    # que devolver las dos páginas, como la consulta de antes.
    for j, k in enumerate(range(0, len(titulos), 200)):
        titulos[k] = ("Proyecto 39", f"Nota 00-{j:04d}")

    for carpeta, titulo in titulos:
        destino = raiz / "Notas" / carpeta
        destino.mkdir(parents=True, exist_ok=True)
        rel = f"Notas/{carpeta}/{titulo}.md"
        enlaces = []
        for _ in range(2):
            if rnd.random() < 0.05:
                enlaces.append(f"[[Inexistente {rnd.randrange(10**6)}]]")
            else:
                enlaces.append(f"[[{titulos[rnd.randrange(len(titulos))][1]}]]")
        cuerpo = [
            f"# {titulo}",
            "",
            RELLENO * 5,
            "",
            "## Relacionado",
            "",
            f"- {enlaces[0]}",
            f"- {enlaces[1]}",
            "",
            RELLENO * 4,
            "",
        ]
        (destino / f"{titulo}.md").write_text(
            "---\n"
            f"id: {uuid.uuid5(NS, rel)}\n"
            "icon: 📄\n"
            "created: 2026-03-01T10:00:00+00:00\n"
            "updated: 2026-10-01T10:00:00+00:00\n"
            "tags: [prueba, escala]\n"
            "---\n" + "\n".join(cuerpo),
            encoding="utf-8",
        )


def huella_links(indice: Path) -> dict:
    """`count(*)` y sha1 del volcado ordenado: la tabla entera, no un resumen."""
    conn = sqlite3.connect(indice)
    try:
        filas = conn.execute(
            "SELECT from_id, to_id, kind, target_title FROM links"
            " ORDER BY from_id, to_id, kind, target_title"
        ).fetchall()
        paginas = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
    finally:
        conn.close()
    volcado = "\n".join("\t".join(str(c) for c in fila) for fila in filas)
    return {
        "links": len(filas),
        "links_sha1": hashlib.sha1(volcado.encode("utf-8")).hexdigest(),
        "pages": paginas,
    }


def medir(vault: Path, indice: Path, etiqueta: str) -> dict:
    from app.config import settings

    settings.vault_path = str(vault)
    settings.index_path = str(indice)

    from fastapi.testclient import TestClient

    from app.main import app

    if indice.exists():
        indice.unlink()

    medida: dict = {"etiqueta": etiqueta, "notas": sum(1 for _ in vault.rglob("*.md"))}

    cliente = TestClient(app)
    cliente.__enter__()
    arranque = time.perf_counter()
    fases: dict[str, float] = {}
    while True:
        cuerpo = cliente.get("/health").json()
        fase = (cuerpo.get("progress") or {}).get("phase") or cuerpo.get("status")
        if fase is not None and fase not in fases:
            fases[fase] = round(time.perf_counter() - arranque, 1)
        if cuerpo.get("status") == "ok":
            break
        if time.perf_counter() - arranque > 7200:
            raise SystemExit(f"el arranque no terminó: {fases}")
        time.sleep(0.2)
    medida["frio"] = {
        "total_s": round(time.perf_counter() - arranque, 1),
        "fases_empiezan_s": fases,
    }
    print(f"[{etiqueta}] frío: {medida['frio']['total_s']} s  {fases}", flush=True)

    # `/reindex` pasa por el middleware de siempre: hay que entrar.
    estado = cliente.get("/auth/status").json()
    if estado.get("needs_setup"):
        credenciales = {
            "username": "prueba",
            "display_name": "Prueba",
            "password": "contrasena-de-prueba",
        }
        cliente.post("/auth/setup", json=credenciales)
    else:
        cliente.post(
            "/auth/login",
            json={"username": "prueba", "password": "contrasena-de-prueba"},
        )

    t0 = time.perf_counter()
    respuesta = cliente.post("/reindex?force=true")
    medida["caliente_s"] = round(time.perf_counter() - t0, 1)
    assert respuesta.status_code == 200, respuesta.text
    print(f"[{etiqueta}] POST /reindex?force=true: {medida['caliente_s']} s", flush=True)

    cliente.__exit__(None, None, None)
    medida.update(huella_links(indice))
    return medida


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notas", type=int, default=12000)
    parser.add_argument("--vault", type=Path, default=RAIZ.parent / "vault-medida")
    parser.add_argument("--indice", type=Path, default=RAIZ.parent / "indice-medida.sqlite")
    parser.add_argument("--etiqueta", default="medida")
    parser.add_argument(
        "--reusar",
        action="store_true",
        help="no regenera el vault (para medir la otra rama sobre el mismo)",
    )
    args = parser.parse_args()

    # El entorno se apunta ANTES de importar `app`: `app.config` lo lee al
    # importarse y `app.main` monta el panel al importarse. Así la medida no
    # toca el vault de verdad ni abre su índice.
    args.indice = args.indice.resolve()
    args.vault = args.vault.resolve()
    os.environ["FRONTEND_DIR"] = str(RAIZ / "frontend")
    os.environ["WATCH_ENABLED"] = "0"
    os.environ["VAULT_PATH"] = str(args.vault)
    os.environ["INDEX_PATH"] = str(args.indice)
    os.environ.pop("ADMIN_USER", None)
    os.environ.pop("ADMIN_PASSWORD", None)

    if not args.reusar or not any(args.vault.rglob("*.md")):
        args.vault.mkdir(parents=True, exist_ok=True)
        generar_vault(args.vault, args.notas)

    # El índice va a un directorio propio y se borra: el arranque reindexa de
    # cero, que es lo que se quiere medir.
    args.indice.parent.mkdir(parents=True, exist_ok=True)
    medida = medir(args.vault, args.indice, args.etiqueta)
    print("MEDIDA " + json.dumps(medida, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
