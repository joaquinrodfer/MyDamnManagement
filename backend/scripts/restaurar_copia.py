"""Restaura (o solo comprueba) una copia de Notemil.

Uso:

    python backend/scripts/restaurar_copia.py COPIA.tar.gz --comprobar
    python backend/scripts/restaurar_copia.py COPIA.tar.gz --en CARPETA_VACIA

- `--comprobar` saca la copia a una carpeta temporal, la verifica y la borra:
  sirve para probar de vez en cuando que las copias valen.
- `--en` la deja en esa carpeta (tiene que estar vacía o no existir):
  `CARPETA/vault` y `CARPETA/index.sqlite`. Para usarla, arranca Notemil con
  `VAULT_PATH=CARPETA/vault INDEX_PATH=CARPETA/index.sqlite`, o cópialas a los
  volúmenes del contenedor parado (ver README, «Copias de seguridad»).

Qué se verifica: que el `.tar.gz` se lee entero, que no trae rutas raras
(absolutas, con `..` o enlaces), que el número de ficheros del vault es el del
manifiesto, que el índice pasa `PRAGMA integrity_check` y que sus páginas,
usuarios y versión coinciden con el manifiesto. No necesita la app ni red.

Sale con 0 si todo está bien y con 1 si algo falla (y dice qué).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile

PERMITIDOS = ("vault/", "index.sqlite", "manifest.json")


def _miembro_seguro(m: tarfile.TarInfo) -> bool:
    nombre = m.name
    if nombre.startswith("/") or ".." in nombre.split("/") or "\\" in nombre:
        return False
    if not (m.isfile() or m.isdir()):
        return False
    return nombre == "index.sqlite" or nombre == "manifest.json" or nombre.startswith("vault/")


def restaurar(copia: str, destino: str) -> dict:
    """Saca la copia en `destino` y la verifica. Lanza `ValueError` si algo
    no cuadra. Devuelve el manifiesto y lo que se encontró."""
    if os.path.exists(destino) and os.listdir(destino):
        raise ValueError(f"la carpeta {destino} no está vacía: no se pisa nada")
    os.makedirs(destino, exist_ok=True)
    with tarfile.open(copia, "r:gz") as tar:
        miembros = tar.getmembers()
        raros = [m.name for m in miembros if not _miembro_seguro(m)]
        if raros:
            raise ValueError(f"la copia trae rutas que no se esperan: {raros[:5]}")
        datos = tar.extractfile("manifest.json")
        if datos is None:
            raise ValueError("la copia no tiene manifest.json")
        manifiesto = json.loads(datos.read())
        for m in miembros:
            if m.name == "manifest.json":
                continue
            ruta = os.path.join(destino, m.name)
            if m.isdir():
                os.makedirs(ruta, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(ruta), exist_ok=True)
            origen = tar.extractfile(m)
            assert origen is not None
            with open(ruta, "wb") as f:
                shutil.copyfileobj(origen, f)

    vault = os.path.join(destino, "vault")
    ficheros = sum(len(n) for _r, _d, n in os.walk(vault)) if os.path.isdir(vault) else 0
    if ficheros != manifiesto.get("vault_files"):
        raise ValueError(f"el vault trae {ficheros} ficheros y el manifiesto dice {manifiesto.get('vault_files')}")
    indice = os.path.join(destino, "index.sqlite")
    if not os.path.isfile(indice):
        raise ValueError("la copia no trae index.sqlite")
    conn = sqlite3.connect(f"file:{indice}?mode=ro", uri=True)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise ValueError(f"el índice no pasa integrity_check: {ok}")
        encontrado = {
            "pages": conn.execute("SELECT count(*) FROM pages").fetchone()[0],
            "users": conn.execute("SELECT count(*) FROM users").fetchone()[0],
            "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
        }
    finally:
        conn.close()
    for clave, valor in encontrado.items():
        if manifiesto.get(clave) != valor:
            raise ValueError(f"{clave}: el índice tiene {valor} y el manifiesto dice {manifiesto.get(clave)}")
    return {"manifiesto": manifiesto, "ficheros": ficheros, **encontrado}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Restaura o comprueba una copia de Notemil.")
    p.add_argument("copia")
    grupo = p.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--comprobar", action="store_true", help="verificar en una carpeta temporal y borrarla")
    grupo.add_argument("--en", metavar="CARPETA", help="dejarla restaurada en esta carpeta (vacía)")
    args = p.parse_args(argv)
    destino = tempfile.mkdtemp(prefix="notemil-restaura-") if args.comprobar else args.en
    try:
        r = restaurar(args.copia, destino)
    except (ValueError, tarfile.TarError, OSError, sqlite3.Error) as exc:
        print(f"La copia NO vale: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.comprobar:
            shutil.rmtree(destino, ignore_errors=True)
    m = r["manifiesto"]
    print(f"Copia buena: {m.get('kind', '?')} del {m.get('created_at', '?')}")
    print(f"  {r['ficheros']} ficheros en el vault · {r['pages']} páginas · {r['users']} usuarios · índice v{r['schema_version']}")
    if not args.comprobar:
        print(f"  Restaurada en {destino}: VAULT_PATH={destino}/vault INDEX_PATH={destino}/index.sqlite")
    return 0


if __name__ == "__main__":
    sys.exit(main())
