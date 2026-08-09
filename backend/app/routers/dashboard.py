from datetime import date, datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import models
from app.database import get_db

router = APIRouter(tags=["dashboard"])

# Ventana de "cerca de hoy" para las fechas de las filas: se incluyen las ya
# vencidas hasta hace una semana (suele ser justo lo más urgente -- una
# "fecha límite" pasada) y las próximas hasta dentro de un mes.
NEAR_PAST_DAYS = 7
NEAR_FUTURE_DAYS = 30

RECENT_PAGES_LIMIT = 8
UPCOMING_DATES_LIMIT = 10


@router.get("/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    """Datos de la landing del panel (se muestra cuando no hay ninguna
    página abierta): páginas con cambios recientes, y filas de cualquier
    database que tenga una propiedad de tipo `date` con un valor cercano a
    hoy. No es un modelo de datos nuevo -- solo dos consultas sobre lo que
    ya existe (`pages.updated_at` y `pages.properties`), pensadas para
    resolverse aquí y no como N peticiones sueltas desde el cliente."""
    recent_pages = (
        db.query(models.Page)
        .filter(models.Page.type != models.PageType.database_row)
        .order_by(models.Page.updated_at.desc())
        .limit(RECENT_PAGES_LIMIT)
        .all()
    )

    db_defs = db.query(models.DatabaseDef).all()
    # Igual que en /pages/tree: el id de la `page` de una database y el id de
    # la fila `databases` que la describe son distintos -- el frontend
    # navega con el segundo.
    database_id_by_page_id = {d.page_id: d.id for d in db_defs}

    today = date.today()
    upcoming: list[dict] = []
    for d in db_defs:
        date_props = [p for p in (d.schema_def or []) if p.get("type") == "date"]
        if not date_props:
            continue
        rows = db.query(models.Page).filter(models.Page.database_id == d.id).all()
        for row in rows:
            props = row.properties or {}
            for prop in date_props:
                raw = props.get(prop["key"])
                if not raw:
                    continue
                try:
                    value = datetime.strptime(str(raw), "%Y-%m-%d").date()
                except ValueError:
                    continue
                delta = (value - today).days
                if -NEAR_PAST_DAYS <= delta <= NEAR_FUTURE_DAYS:
                    upcoming.append(
                        {
                            "row_id": row.id,
                            "row_title": row.title,
                            "database_id": d.id,
                            "database_title": d.page.title,
                            "property_name": prop.get("name", prop["key"]),
                            "date": value.isoformat(),
                            "days_from_today": delta,
                        }
                    )

    upcoming.sort(key=lambda x: x["days_from_today"])

    return {
        "recent_pages": [
            {
                "id": p.id,
                "title": p.title,
                "icon": p.icon,
                "type": p.type,
                "database_id": database_id_by_page_id.get(p.id),
                "updated_at": p.updated_at,
            }
            for p in recent_pages
        ],
        "upcoming_dates": upcoming[:UPCOMING_DATES_LIMIT],
    }
