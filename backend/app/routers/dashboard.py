from datetime import date, datetime

from fastapi import APIRouter, Depends

from app import acl, auth, mytasks, notifications
from app.dependencies import get_current_user, get_repo
from app.repository import Repository
from app.serializers import as_utc

router = APIRouter(tags=["dashboard"])

# Ventana de "cerca de hoy" para las fechas de las filas: se incluyen las ya
# vencidas hasta hace una semana (suele ser justo lo más urgente -- una
# "fecha límite" pasada) y las próximas hasta dentro de un mes.
NEAR_PAST_DAYS = 7
NEAR_FUTURE_DAYS = 30

RECENT_PAGES_LIMIT = 8
NOTIFICATIONS_LIMIT = 5
UPCOMING_DATES_LIMIT = 10


@router.get("/dashboard")
def get_dashboard(repo: Repository = Depends(get_repo), user: auth.User = Depends(get_current_user)):
    """Datos de la landing del panel (se muestra cuando no hay ninguna
    página abierta): páginas con cambios recientes, y filas de cualquier
    database que tenga una propiedad de tipo `date` con un valor cercano a
    hoy. No es un modelo de datos nuevo -- solo dos consultas sobre lo que
    ya existe, pensadas para resolverse aquí y no como N peticiones sueltas
    desde el cliente.
    """
    recent_pages = repo.list_recent_pages(RECENT_PAGES_LIMIT)

    db_defs = repo.list_databases()
    # Igual que en /pages/tree: el id de la `page` de una database y el id de
    # la `database` que la describe son distintos -- el frontend navega con el
    # segundo.
    database_id_by_page_id = {d.page_id: d.id for d in db_defs}

    today = date.today()
    upcoming: list[dict] = []
    for d in db_defs:
        date_props = [p for p in (d.schema_def or []) if p.get("type") == "date"]
        if not date_props:
            continue
        for row in repo.list_rows(d.id):
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
                            "database_title": d.title,
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
                "updated_at": as_utc(p.updated_at),
                "header_image": p.header_image_path,
                "header_position": p.header_position,
            }
            for p in recent_pages
        ],
        "upcoming_dates": upcoming[:UPCOMING_DATES_LIMIT],
        # Inicio 2.0: mis tareas abiertas (lo mismo que `GET /my-tasks`, con la
        # regla de vencidas del servidor) y lo último que me ha llegado.
        "tasks": mytasks.my_tasks(repo.conn, user.id, acl.visible_ids(repo.conn, user, repo.workspace_id)),
        "notifications": notifications.list_for(repo.conn, str(user.id), unread_only=True, limit=NOTIFICATIONS_LIMIT),
        "mentions_unread": repo.conn.execute(
            "SELECT count(*) FROM notifications WHERE user_id = ? AND is_read = 0 AND type = 'mention'",
            (str(user.id),),
        ).fetchone()[0],
    }
