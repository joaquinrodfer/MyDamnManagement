from datetime import datetime, timezone

from fastapi import APIRouter

from app import index
from app.config import settings

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
