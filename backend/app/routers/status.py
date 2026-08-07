from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import text

from app.database import SessionLocal

router = APIRouter(tags=["status"])


@router.get("/status")
def get_status():
    """Estado agregado de cada servicio, para el panel del frontend."""
    services = {
        "api": {"ok": True},
        "db": _check_db(),
    }
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "services": services,
    }


def _check_db() -> dict:
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 - queremos reportar cualquier fallo al frontend
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()
