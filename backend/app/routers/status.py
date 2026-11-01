from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app import index
from app.config import settings
from app.repository import Repository
from app.vault import drift
from app.vault.store import VaultError

router = APIRouter(tags=["status"])


@router.get("/status")
def get_status():
    """Estado agregado de cada servicio, para el panel del frontend.

    La clave `db` se mantiene tal cual (el panel pinta `dot-db` con
    `services.db.ok`): lo que cambia es qué hay detrás — antes una conexión a
    Postgres, ahora el índice SQLite.

    `drift` es la deriva entre el índice y el disco (ADR-0001): desde P8 el
    vigilante (`app/vault/watcher.py`) la resuelve solo cada `WATCH_INTERVAL`,
    así que en condiciones normales esto es 0; con `WATCH_ENABLED=0` sigue
    siendo lo único que la cuenta, y en los dos casos dice cuántos ficheros van
    por detrás y cuándo se reindexó por última vez. Cuesta un `stat` por
    fichero y no lee ningún cuerpo.
    """
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "services": {
            "api": {"ok": True},
            "db": _check_index(),
        },
        "drift": _check_drift(),
    }


def _check_index() -> dict:
    conn = None
    try:
        info = index.ensure_index(settings.index_path)
        conn = index.connect(settings.index_path)
        counts = index.count_rows(conn)
        return {
            "ok": True,
            "engine": "sqlite",
            "schema_version": info.version,
            "counts": counts,
        }
    except Exception as exc:  # noqa: BLE001 - queremos reportar cualquier fallo al frontend
        return {"ok": False, "engine": "sqlite", "error": str(exc)}
    finally:
        if conn is not None:
            conn.close()


def _check_drift() -> dict:
    """Índice contra disco. Nunca lanza: el estado no debe tumbar el panel."""
    conn = None
    try:
        conn = index.open_index(settings.index_path)
        return {"ok": True, **drift.scan(settings.vault_path, conn).as_dict()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    finally:
        if conn is not None:
            conn.close()


@router.post("/reindex")
def post_reindex(force: bool = False):
    """Reconstruye las notas del índice desde el vault (ADR-0001, D1).

    Desde P8 el vigilante del vault recoge solo, cada `WATCH_INTERVAL`, lo que
    cambia en los `.md` por fuera. Esto sigue siendo la salida completa: para
    un vault tocado en masa (un `rsync`, restaurar un backup) o cuando
    `WATCH_ENABLED=0`. Devuelve recuentos, duración, la marca del reindexado y
    los primeros avisos (ficheros raros que se han leído igual).

    `?force=true` es la salida explícita cuando el vault está vacío a propósito:
    sin él, un vault inexistente o sin un solo `.md` devuelve 409 en vez de
    vaciar el índice (un volumen sin montar no es un vault vacío).
    """
    conn = index.open_index(settings.index_path)
    try:
        stats = Repository(conn).reindex(force=force)
    except VaultError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"no se pudo leer el vault: {exc}") from exc
    finally:
        conn.close()
    return {"ok": True, "vault": settings.vault_path, **stats.as_dict()}
