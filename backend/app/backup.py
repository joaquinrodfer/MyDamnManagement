"""Copias de seguridad (plan H0).

Desde la ADR-0005 la verdad vive en el índice SQLite (notas, bases, usuarios,
permisos) y los adjuntos en el vault: copiar el vault ya no basta. Una copia es
un `.tar.gz` con tres cosas:

- `manifest.json`: de qué versión es, cuándo se hizo y cuánto lleva;
- `index.sqlite`: una foto **consistente** del índice, tomada con la API de
  copia de SQLite (vale con la app en marcha y con escrituras en curso);
- `vault/`: la carpeta del vault tal cual (adjuntos y, si hay proyección, los
  `.md`).

Esto deja tres usos:

1. **Descargar ahora** (administración).
2. **Copias programadas** en `BACKUP_DIR`, con las N últimas.
3. **Copia antes de migrar** (`copy_before_migration`): antes de cambiar el
   esquema del índice se guarda el fichero tal como estaba, porque volver atrás
   un despliegue no deshace una migración.

Restaurar es lo inverso y está en el README: parar la app, desempaquetar,
dejar `index.sqlite` en `INDEX_PATH` y `vault/` en `VAULT_PATH`, arrancar.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import sqlite3
import tarfile
import tempfile
import threading
from datetime import datetime, timedelta, timezone

from app import index as index_mod
from app.config import settings

log = logging.getLogger("app.backup")

# Nombre de las copias programadas y descargadas. Es lo único que se sirve
# desde `BACKUP_DIR`: el nombre se valida entero, nunca se une una ruta de fuera.
FILE_RE = re.compile(r"noteping-copia-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.tar\.gz")

META_FREQUENCY = "backup_frequency"  # off | daily | weekly
META_KEEP = "backup_keep"
META_LAST = "backup_last"
FREQUENCIES = {"off": None, "daily": timedelta(days=1), "weekly": timedelta(days=7)}
DEFAULT_KEEP = 7
MAX_KEEP = 90


class BackupError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def file_name(when: datetime | None = None) -> str:
    return "noteping-copia-" + (when or _now()).strftime("%Y-%m-%dT%H-%M-%SZ") + ".tar.gz"


# --------------------------------------------------------------------------
# la copia
# --------------------------------------------------------------------------


def snapshot_index(src_path: str, dest_path: str) -> None:
    """Foto consistente del índice en `dest_path` (API de copia de SQLite)."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _counts(index_copy: str) -> dict:
    conn = sqlite3.connect(index_copy)
    try:
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "pages": one("SELECT count(*) FROM pages"),
            "users": one("SELECT count(*) FROM users"),
            "schema_version": one("PRAGMA user_version"),
        }
    finally:
        conn.close()


def make_backup(dest_file: str, *, kind: str = "manual") -> dict:
    """Escribe la copia en `dest_file` (se crea entera o no queda nada) y
    devuelve su manifiesto."""
    vault = os.path.abspath(settings.vault_path)
    dest_abs = os.path.abspath(dest_file)
    os.makedirs(os.path.dirname(dest_abs), exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix=".copia-", dir=os.path.dirname(dest_abs))
    partial = os.path.join(tmp_dir, "copia.tar.gz")
    try:
        copy = os.path.join(tmp_dir, "index.sqlite")
        snapshot_index(settings.index_path, copy)
        files = 0
        with tarfile.open(partial, "w:gz") as tar:
            if os.path.isdir(vault):
                for root, dirs, names in os.walk(vault):
                    # La copia no puede llevarse a sí misma si se guarda dentro.
                    dirs[:] = [d for d in dirs if os.path.join(root, d) != os.path.dirname(dest_abs)]
                    for name in names:
                        full = os.path.join(root, name)
                        if os.path.abspath(full) == dest_abs or os.path.islink(full):
                            continue
                        tar.add(full, arcname="vault/" + os.path.relpath(full, vault))
                        files += 1
            manifest = {
                "app": "Noteping",
                "kind": kind,
                "created_at": _now().isoformat(),
                "vault_files": files,
                **_counts(copy),
            }
            raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(raw)
            info.mtime = int(_now().timestamp())
            tar.addfile(info, io.BytesIO(raw))
            tar.add(copy, arcname="index.sqlite")
        os.replace(partial, dest_abs)
        return manifest
    finally:
        for name in os.listdir(tmp_dir):
            try:
                os.unlink(os.path.join(tmp_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


def read_manifest(path: str) -> dict:
    with tarfile.open(path, "r:gz") as tar:
        member = tar.extractfile("manifest.json")
        if member is None:
            raise BackupError("la copia no tiene manifest.json")
        return json.loads(member.read())


# --------------------------------------------------------------------------
# copia antes de migrar
# --------------------------------------------------------------------------


def copy_before_migration(index_path: str, from_version: int, to_version: int) -> str | None:
    """Guarda el índice tal como está antes de cambiarle el esquema.

    Devuelve la ruta de la copia, o `None` si no se pudo (se avisa en el log y
    la migración sigue: un disco lleno no debe impedir arrancar).
    """
    folder = settings.backup_dir or os.path.dirname(os.path.abspath(index_path))
    dest = os.path.join(
        folder, f"{os.path.basename(index_path)}.antes-de-v{to_version}-desde-v{from_version}"
    )
    try:
        os.makedirs(folder, exist_ok=True)
        snapshot_index(index_path, dest)
        log.warning(
            "copia del índice antes de migrar de la v%s a la v%s: %s", from_version, to_version, dest
        )
        return dest
    except Exception:  # noqa: BLE001
        log.exception("no se pudo copiar el índice antes de migrar (se migra igualmente)")
        return None


# --------------------------------------------------------------------------
# copias programadas
# --------------------------------------------------------------------------


def listing() -> list[dict]:
    folder = settings.backup_dir
    if not folder or not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder), reverse=True):
        if FILE_RE.fullmatch(name):
            st = os.stat(os.path.join(folder, name))
            out.append({
                "name": name,
                "size": st.st_size,
                "created_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
            })
    return out


def prune(keep: int) -> int:
    """Borra las copias programadas más viejas; deja las `keep` últimas."""
    borradas = 0
    for item in listing()[max(keep, 1):]:
        try:
            os.unlink(os.path.join(settings.backup_dir, item["name"]))
            borradas += 1
        except OSError:
            log.exception("no se pudo borrar la copia %s", item["name"])
    return borradas


def get_settings(conn) -> dict:
    freq = index_mod.get_meta(conn, META_FREQUENCY) or "off"
    try:
        keep = int(index_mod.get_meta(conn, META_KEEP) or DEFAULT_KEEP)
    except ValueError:
        keep = DEFAULT_KEEP
    return {
        "frequency": freq if freq in FREQUENCIES else "off",
        "keep": min(max(keep, 1), MAX_KEEP),
        "last": index_mod.get_meta(conn, META_LAST),
    }


def is_due(conn, now: datetime | None = None) -> bool:
    cfg = get_settings(conn)
    every = FREQUENCIES[cfg["frequency"]]
    if every is None or not settings.backup_dir:
        return False
    if not cfg["last"]:
        return True
    try:
        last = datetime.fromisoformat(cfg["last"])
    except ValueError:
        return True
    return (now or _now()) - last >= every


def run_scheduled(conn) -> dict:
    """Hace una copia en `BACKUP_DIR`, anota la hora y poda las viejas."""
    if not settings.backup_dir:
        raise BackupError("BACKUP_DIR no está configurado")
    dest = os.path.join(settings.backup_dir, file_name())
    manifest = make_backup(dest, kind="programada")
    index_mod.set_meta(conn, META_LAST, manifest["created_at"])
    conn.commit()
    prune(get_settings(conn)["keep"])
    return {"name": os.path.basename(dest), **manifest}


class BackupScheduler(threading.Thread):
    """Mira cada pocos minutos si toca una copia programada."""

    def __init__(self, interval: float = 600.0) -> None:
        super().__init__(name="backup-scheduler", daemon=True)
        self.interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            self.tick()

    def tick(self) -> bool:
        try:
            conn = index_mod.open_index(settings.index_path)
            try:
                if is_due(conn):
                    run_scheduled(conn)
                    return True
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - un fallo no mata el hilo
            log.exception("falló la copia programada")
        return False
