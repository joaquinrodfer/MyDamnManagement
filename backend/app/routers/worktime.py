"""Jornada, dedicación, ausencias y festivos (plan de la carga, C1).

- `GET /worktime/users/{id}`: la jornada, la dedicación y las ausencias de una
  persona. Cada uno la suya; con «Gestionar personas» (`app.grants`), la de las
  personas de sus espacios.
- `PUT /worktime/users/{id}/schedule` `{hours: [7] | null}` y
  `PUT /worktime/users/{id}/dedication` `{dedication: {espacio: %}}`: con
  «Gestionar personas».
- `GET /absences`, `POST /absences`, `DELETE /absences/{id}`: cada persona las
  suyas; con «Gestionar personas», las de las personas de sus espacios.
- `GET /holidays?workspace_id=&year=`, `POST /holidays`, `DELETE /holidays/{id}`:
  los comunes (sin `workspace_id`), con «Gestionar festivos» en todos los
  espacios; los de un espacio, su administrador o quien tiene el permiso en
  él. Leerlos, cualquiera que pertenezca al ámbito.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app import audit, auth, grants, holiday_import, workspaces, worktime
from app.dependencies import get_current_user, get_repo
from app.due import server_today
from app.repository import Repository

router = APIRouter(tags=["worktime"])


def _user_or_404(repo: Repository, user_id: str) -> dict:
    r = repo.conn.execute(
        "SELECT id, username, display_name FROM users WHERE id = ?", (str(user_id),)
    ).fetchone()
    if r is None:
        raise HTTPException(404, "no existe ese usuario")
    return dict(r)


def _self_or_manager(repo: Repository, user: auth.User, user_id: str) -> None:
    """Lo tuyo, siempre; lo de otra persona, con «Gestionar personas» en un
    espacio suyo (o en todos)."""
    if str(user.id) != str(user_id) and not _manages(repo, user, user_id):
        raise HTTPException(403, "solo puedes ver y cambiar lo tuyo")


def _manages(repo: Repository, user: auth.User, user_id: str) -> bool:
    return grants.can_for_person(repo.conn, user, "manage_people", str(user_id))


def _manager(repo: Repository, user: auth.User, user_id: str, que: str) -> None:
    if not _manages(repo, user, user_id):
        raise HTTPException(403, f"cambiar {que} pide el permiso «Gestionar personas»")


# --------------------------------------------------------------------------
# jornada y dedicación
# --------------------------------------------------------------------------


class ScheduleIn(BaseModel):
    # De lunes a domingo; `null` vuelve a la jornada por defecto (8 h L–V).
    hours: Optional[list[float]] = Field(default=None, min_length=7, max_length=7)


class DedicationIn(BaseModel):
    dedication: dict[str, Optional[float]] = Field(default_factory=dict)


def _ficha(repo: Repository, user_id: str) -> dict:
    horas = worktime.schedule_of(repo.conn, user_id)
    propia = repo.conn.execute(
        "SELECT 1 FROM work_schedules WHERE user_id = ?", (str(user_id),)
    ).fetchone() is not None or str(user_id) in worktime._legacy_weekly(repo.conn)
    return {
        "user_id": str(user_id),
        "schedule": horas,
        "weekly": worktime.weekly(horas),
        "custom": propia,
        "default": list(worktime.DEFAULT_DAY_HOURS),
        "dedication": worktime.dedications(repo.conn).get(str(user_id), {}),
        "absences": worktime.absences(repo.conn, user_id, since=server_today() - timedelta(days=31)),
    }


@router.get("/worktime/people")
def manageable_people(
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    """Las personas cuya jornada, dedicación y ausencias puedo gestionar
    («Gestionar personas»: todas, o las de los espacios del permiso). Sin el
    permiso, lista vacía."""
    alcance = grants.scopes(repo.conn, user, "manage_people")
    filas = repo.conn.execute(
        "SELECT id, username, display_name, color, avatar FROM users ORDER BY display_name COLLATE NOCASE"
    ).fetchall()
    if alcance is not None:
        dentro = grants.people_in(repo.conn, alcance)
        filas = [r for r in filas if r["id"] in dentro]
    return {
        "all": alcance is None,
        "people": [
            {"id": r["id"], "username": r["username"], "display_name": r["display_name"],
             "color": r["color"], "avatar": auth.avatar_of(r)}
            for r in filas
        ],
    }


@router.get("/worktime/users/{user_id}")
def get_worktime(
    user_id: str,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    _self_or_manager(repo, user, user_id)
    _user_or_404(repo, user_id)
    return _ficha(repo, user_id)


@router.put("/worktime/users/{user_id}/schedule")
def put_schedule(
    user_id: str,
    payload: ScheduleIn,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    _manager(repo, user, user_id, "la jornada")
    persona = _user_or_404(repo, user_id)
    try:
        worktime.set_schedule(repo.conn, user_id, payload.hours)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    audit.record(
        repo.conn, user, "worktime.schedule", target_type="user", target_id=user_id,
        target_name=persona["display_name"] or persona["username"],
        metadata={"hours": payload.hours},
    )
    return _ficha(repo, user_id)


@router.put("/worktime/users/{user_id}/dedication")
def put_dedication(
    user_id: str,
    payload: DedicationIn,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    _manager(repo, user, user_id, "la dedicación")
    persona = _user_or_404(repo, user_id)
    try:
        worktime.set_dedication(repo.conn, user_id, payload.dedication)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    audit.record(
        repo.conn, user, "worktime.dedication", target_type="user", target_id=user_id,
        target_name=persona["display_name"] or persona["username"],
        metadata={"dedication": payload.dedication},
    )
    return _ficha(repo, user_id)


# --------------------------------------------------------------------------
# ausencias
# --------------------------------------------------------------------------


class AbsenceIn(BaseModel):
    user_id: Optional[str] = None  # sin él, quien pregunta
    start: date
    end: Optional[date] = None
    kind: str = "vacaciones"
    hours: Optional[float] = Field(default=None, gt=0, le=24)
    note: str = Field(default="", max_length=500)


@router.get("/absences")
def list_absences(
    user_id: Optional[str] = None,
    since: Optional[date] = Query(default=None),
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    quien = str(user_id or user.id)
    _self_or_manager(repo, user, quien)
    return {
        "kinds": [{"key": k, "label": worktime.ABSENCE_LABELS[k]} for k in worktime.ABSENCE_KINDS],
        "absences": worktime.absences(repo.conn, quien, since=since),
    }


@router.post("/absences", status_code=201)
def create_absence(
    payload: AbsenceIn,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    quien = str(payload.user_id or user.id)
    _self_or_manager(repo, user, quien)
    _user_or_404(repo, quien)
    try:
        creada = worktime.add_absence(
            repo.conn, quien, payload.start, payload.end or payload.start, payload.kind,
            hours=payload.hours, note=payload.note, created_by=str(user.id),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return creada


@router.delete("/absences/{absence_id}", status_code=204)
def delete_absence(
    absence_id: int,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    ausencia = worktime.get_absence(repo.conn, absence_id)
    if ausencia is None:
        raise HTTPException(404, "no existe esa ausencia")
    _self_or_manager(repo, user, ausencia["user_id"])
    worktime.delete_absence(repo.conn, absence_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------
# festivos
# --------------------------------------------------------------------------


class HolidayIn(BaseModel):
    workspace_id: Optional[str] = None
    day: date
    name: str = Field(default="Festivo", max_length=200)


def _puede_leer(repo: Repository, user: auth.User, workspace_id: Optional[str]) -> None:
    if workspace_id and workspaces.role_of(repo.conn, user, workspace_id) is None and not workspaces.can_manage(
        repo.conn, user, workspace_id
    ):
        raise HTTPException(404, "no existe ese espacio")


def _puede_gestionar(repo: Repository, user: auth.User, workspace_id: Optional[str]) -> None:
    if workspace_id is None:
        if not grants.can(repo.conn, user, "manage_holidays"):
            raise HTTPException(403, "los festivos comunes piden el permiso «Gestionar festivos»")
        return
    if repo.conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (str(workspace_id),)).fetchone() is None:
        raise HTTPException(404, "no existe ese espacio")
    if not workspaces.can_manage_holidays(repo.conn, user, workspace_id):
        raise HTTPException(403, "los festivos de un espacio son de su administrador o de quien tiene «Gestionar festivos»")


@router.get("/holidays/scopes")
def holiday_scopes(
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    """Dónde puedo cambiar festivos: los comunes y los espacios (por ser su
    administrador o por «Gestionar festivos»). Para el apartado «Calendario
    laboral» de Ajustes."""
    filas = repo.conn.execute("SELECT id, name, settings FROM workspaces ORDER BY name COLLATE NOCASE").fetchall()
    espacios = [
        {"id": r["id"], "name": r["name"]}
        for r in filas
        if not workspaces.is_personal(r) and workspaces.can_manage_holidays(repo.conn, user, r["id"])
    ]
    return {"common": grants.can(repo.conn, user, "manage_holidays"), "workspaces": espacios}


@router.get("/holidays")
def list_holidays(
    workspace_id: Optional[str] = None,
    year: Optional[int] = Query(default=None, ge=1900, le=2200),
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    _puede_leer(repo, user, workspace_id)
    puede = True
    try:
        _puede_gestionar(repo, user, workspace_id)
    except HTTPException:
        puede = False
    return {
        "workspace_id": workspace_id,
        "can_edit": puede,
        "holidays": worktime.holidays(repo.conn, workspace_id, year=year),
    }


@router.post("/holidays", status_code=201)
def create_holiday(
    payload: HolidayIn,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    _puede_gestionar(repo, user, payload.workspace_id)
    return worktime.add_holiday(repo.conn, payload.workspace_id, payload.day, payload.name)


@router.delete("/holidays/{holiday_id}", status_code=204)
def delete_holiday(
    holiday_id: int,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    festivo = worktime.get_holiday(repo.conn, holiday_id)
    if festivo is None:
        raise HTTPException(404, "no existe ese festivo")
    _puede_gestionar(repo, user, festivo["workspace_id"])
    worktime.delete_holiday(repo.conn, holiday_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------
# importar festivos (C1b)
# --------------------------------------------------------------------------


class ImportIcsIn(BaseModel):
    workspace_id: Optional[str] = None
    name: str = Field(default="", max_length=200)
    content: str = Field(max_length=holiday_import.MAX_BYTES)


class SourceIn(BaseModel):
    workspace_id: Optional[str] = None
    kind: str  # "url" | "country"
    name: str = Field(default="", max_length=200)
    url: Optional[str] = Field(default=None, max_length=2000)
    country: Optional[str] = Field(default=None, max_length=2)
    region: Optional[str] = Field(default=None, max_length=10)


def _importar(fn):
    try:
        return fn()
    except holiday_import.ImportErrorMsg as e:
        raise HTTPException(400, str(e)) from None


@router.get("/holidays/sources")
def list_sources(
    workspace_id: Optional[str] = None,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    _puede_leer(repo, user, workspace_id)
    return holiday_import.sources(repo.conn, workspace_id)


@router.post("/holidays/import", status_code=201)
def import_ics(
    payload: ImportIcsIn,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    _puede_gestionar(repo, user, payload.workspace_id)
    fuente = _importar(
        lambda: holiday_import.create_source(
            repo.conn, payload.workspace_id, "file", name=payload.name, content=payload.content
        )
    )
    audit.record(repo.conn, user, "holidays.imported", workspace_id=payload.workspace_id,
                 target_type="holiday_source", target_id=fuente["id"], target_name=fuente["name"])
    return fuente


@router.post("/holidays/sources", status_code=201)
def create_source(
    payload: SourceIn,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    _puede_gestionar(repo, user, payload.workspace_id)
    if payload.kind not in ("url", "country"):
        raise HTTPException(400, "Una suscripción es a una URL o a un país.")
    fuente = _importar(
        lambda: holiday_import.create_source(
            repo.conn, payload.workspace_id, payload.kind, name=payload.name,
            url=payload.url, country=payload.country, region=payload.region,
        )
    )
    audit.record(repo.conn, user, "holidays.imported", workspace_id=payload.workspace_id,
                 target_type="holiday_source", target_id=fuente["id"], target_name=fuente["name"])
    return fuente


def _fuente_gestionable(repo: Repository, user: auth.User, source_id: int) -> dict:
    fuente = holiday_import._source(repo.conn, source_id)
    if fuente is None:
        raise HTTPException(404, "no existe esa fuente de festivos")
    _puede_gestionar(repo, user, fuente["workspace_id"])
    return fuente


@router.post("/holidays/sources/{source_id}/sync")
def sync_source(
    source_id: int,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    _fuente_gestionable(repo, user, source_id)
    return _importar(lambda: holiday_import.sync_source(repo.conn, source_id))


@router.delete("/holidays/sources/{source_id}", status_code=204)
def delete_source(
    source_id: int,
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    fuente = _fuente_gestionable(repo, user, source_id)
    holiday_import.delete_source(repo.conn, source_id)
    audit.record(repo.conn, user, "holidays.source_removed", workspace_id=fuente["workspace_id"],
                 target_type="holiday_source", target_id=source_id, target_name=fuente["name"])
    return Response(status_code=204)


@router.get("/holidays/countries")
def official_countries(user: auth.User = Depends(get_current_user)):
    return _importar(holiday_import.countries)


@router.get("/holidays/regions")
def official_regions(
    country: str = Query(min_length=2, max_length=2),
    user: auth.User = Depends(get_current_user),
):
    return _importar(lambda: holiday_import.regions(country.upper(), server_today().year))
