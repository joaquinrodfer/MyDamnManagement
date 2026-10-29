"""Operaciones de disco del vault: escritura atómica, renombrado y papelera.

Escritura atómica
-----------------
`os.replace()` es atómico dentro del mismo sistema de ficheros, así que el
temporal se crea SIEMPRE en el mismo directorio que el destino (si se crea en
/tmp, `rename()` cruza dispositivos y lanza EXDEV). Se hace `fsync` del fichero
antes del replace y `fsync` del directorio después: sin el segundo, un corte de
corriente puede dejar el rename sin persistir en el directorio, aunque el
contenido sí esté.

Papelera (`.trash/`)
--------------------
`.trash/<UTC 2026-10-29T18-04-11>/<ruta relativa original>`.
Se preserva la ruta relativa en vez de aplanar porque dos ficheros con el mismo
nombre en carpetas distintas se pisarían en un `_trash` plano, y porque el
`restore` es un `rename` de vuelta sin necesidad de leer metadatos. Si ya existe
ese directorio de fecha (dos borrados en el mismo segundo) se usa `-2`, `-3`.
No se usa sidecar `.origin` porque el propio árbol ya es el origen y un sidecar
puede perderse en un rsync parcial.
"""

from __future__ import annotations

import errno
import os
import secrets
import shutil
from datetime import datetime, timezone

TMP_SUFFIX = ".tmp-"


class PathEscape(ValueError):
    """Una ruta construida desde un título o una carpeta se sale del vault."""


def safe_rel(root: str, *parts: str) -> str:
    """Ruta relativa (con '/') de `parts` dentro de `root`, o `PathEscape`.

    TODA ruta que se construya desde un título, una carpeta, una ruta de la
    papelera o un `path` leído de disco pasa por aquí antes de tocar el disco.
    Sin esto, `create_page(vault, "../../FUERA", "x")` escribía fuera del vault
    (reproducido: `page.path = ../../FUERA/x.md`), y `restore()` aceptaba un
    `trash_rel` con `..`. La comprobación es sobre `realpath`, así que también
    cubre symlinks y rutas absolutas coladas como carpeta.
    """
    root_abs = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_abs, *parts))
    if target == root_abs:
        return ""
    if not target.startswith(root_abs + os.sep):
        raise PathEscape(
            f"ruta fuera del vault: {os.path.join(*parts)!r} resuelve a {target!r} "
            f"(vault: {root_abs!r})")
    return os.path.relpath(target, root_abs).replace(os.sep, "/")


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: str, data: str, *, simulate_crash: bool = False,
                 hard_crash: bool = False) -> str:
    """Escribe `data` en `path` de forma atómica. Devuelve la ruta del temporal."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}{TMP_SUFFIX}"
                                  f"{os.getpid()}-{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            if hard_crash:          # corte seco: ni cleanup ni rename
                os._exit(9)
            if simulate_crash:      # corte simulado a mitad de escritura
                raise KeyboardInterrupt("crash simulado antes del rename")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    _fsync_dir(directory)
    return tmp


def move(src: str, dst: str) -> None:
    """Mueve un fichero (rename) creando el directorio destino si hace falta."""
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    os.replace(src, dst)
    _fsync_dir(os.path.dirname(dst) or ".")
    if os.path.dirname(src) != os.path.dirname(dst):
        _fsync_dir(os.path.dirname(src) or ".")


def trash_dir_for(vault: str, now: datetime | None = None) -> str:
    """Directorio de papelera para este borrado, con sufijo si ya existe."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H-%M-%S")
    base = os.path.join(vault, ".trash", stamp)
    if not os.path.exists(base):
        return base
    n = 2
    while os.path.exists(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


def to_trash(vault: str, rel_path: str, now: datetime | None = None) -> str:
    """Mueve `rel_path` (relativo al vault) a la papelera. Devuelve la ruta nueva."""
    # `rel_path` viene del modelo, pero se valida igual: un `..` aquí sacaría el
    # fichero del vault o escribiría la papelera fuera de `.trash/`.
    src = os.path.join(vault, safe_rel(vault, rel_path))
    target_dir = trash_dir_for(vault, now)
    dst = os.path.join(target_dir, rel_path)
    if os.path.exists(dst):                     # mismo rel_path, mismo segundo
        target_dir = trash_dir_for(vault, now)
        # el directorio recién creado colisiona: fuerza otro sufijo
        n = 2
        while os.path.exists(f"{target_dir}-{n}"):
            n += 1
        target_dir = f"{target_dir}-{n}"
        dst = os.path.join(target_dir, rel_path)
    move(src, dst)
    # deja rastro legible del origen incluso si se aplana a mano
    return dst


def restore(vault: str, trash_rel: str) -> str:
    """Devuelve a su sitio un fichero de `.trash/<ts>/<ruta>`, sin pisar nada."""
    # Ambas rutas se validan: `trash_rel` no puede salir de `.trash/` y la ruta
    # original reconstruida no puede salir del vault (antes, un `..` en el
    # nombre dentro de la papelera hacía que `restore` escribiera fuera).
    src = os.path.join(vault, safe_rel(vault, trash_rel))
    trash_root = os.path.join(vault, ".trash")
    parts = os.path.relpath(src, trash_root).split(os.sep)
    if len(parts) < 2 or parts[0] in (os.curdir, os.pardir):
        raise ValueError("ruta de papelera sin ruta original")
    rel_original = os.sep.join(parts[1:])
    dst = os.path.join(vault, safe_rel(vault, rel_original))
    base, ext = os.path.splitext(dst)
    n = 2
    while os.path.exists(dst):
        dst = f"{base} ({n}){ext}"
        n += 1
    move(src, dst)
    return dst


def rmtree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
