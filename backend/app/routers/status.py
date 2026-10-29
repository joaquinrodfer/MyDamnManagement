from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app import index
from app.config import settings
from app.repository import Repository

router = APIRouter(tags=["status"])


@router.get("/status")
def get_status():
    """Estado agregado de cada servicio, para el panel del frontend.

    La clave `db` se mantiene tal cual (el panel pinta `dot-db` con
    `services.db.ok`): lo que cambia es qué hay detrás — antes una conexión a
    Postgres, ahora el índice SQLite.
    """
    services = {
        "api": {"ok": True},
        "db": _check_index(),
    }
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "services": services,
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


@router.post("/reindex")
def post_reindex():
    """Reconstruye las notas del índice desde el vault (ADR-0001, D1).

    Es la única forma de recoger cambios hechos en los `.md` por fuera: en v1.0
    no hay vigilante de ficheros (D2). Devuelve recuentos, duración y los
    primeros avisos del reindexado (ficheros raros que se han leído igual).
    """
    conn = index.open_index(settings.index_path)
    try:
        stats = Repository(conn).reindex()
    except OSError as exc:
        raise HTTPException(500, f"no se pudo leer el vault: {exc}") from exc
    finally:
        conn.close()
    return {"ok": True, "vault": settings.vault_path, **stats.as_dict()}
