"""`GET /about`: lo que enseña Ajustes > Información.

Para cualquiera con sesión: qué versión corre (la pone el CI en la imagen:
`MDM_VERSION`, `MDM_REVISION`, `MDM_BUILT`; sin ellas, en desarrollo, se intenta
con `git`), desde cuándo, y si la API y la base están bien.

Para los administradores, además, lo que sirve para saber cómo va la
instalación: cuánto hay (notas, bases, filas, usuarios, sesiones), cuánto se ha
creado mes a mes, quién crea más, cuánto ocupa la base, cuánto está compartido y
las salas de edición en vivo abiertas. Son consultas agregadas (un `GROUP BY`
por tabla), nada que recorra el vault ni cargue cuerpos.
"""

from __future__ import annotations

import os
import platform
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends

from app import auth
from app import index as index_mod
from app.config import settings
from app.dependencies import get_current_user, get_repo
from app.repository import Repository

router = APIRouter(tags=["about"])

_STARTED = time.time()
_REPO_DIR = Path(__file__).resolve().parents[3]


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=_REPO_DIR, capture_output=True, text=True, timeout=1
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def version_info() -> dict:
    """Versión de lo que corre. Se calcula una vez por proceso (no cambia)."""
    global _VERSION
    if _VERSION is not None:
        return _VERSION
    version = os.environ.get("MDM_VERSION") or ""
    revision = os.environ.get("MDM_REVISION") or ""
    built = os.environ.get("MDM_BUILT") or ""
    if not revision:
        revision = _git("rev-parse", "HEAD") or ""
        if revision and not built:
            built = _git("log", "-1", "--format=%cI") or ""
    _VERSION = {
        "version": version or "desarrollo",
        "revision": revision or None,
        "revision_short": revision[:7] if revision else None,
        "built_at": built or None,
        "schema_version": index_mod.SCHEMA_VERSION,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
    }
    return _VERSION


_VERSION: dict | None = None


def _db_status(conn: sqlite3.Connection) -> dict:
    t0 = time.perf_counter()
    try:
        conn.execute("SELECT 1").fetchone()
        ok = True
        error = None
    except sqlite3.DatabaseError as exc:  # pragma: no cover - base rota
        ok, error = False, str(exc)
    return {
        "ok": ok,
        "engine": "sqlite",
        "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        **({"error": error} if error else {}),
    }


def _size(path: str) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(path + suffix)
        except OSError:
            pass
    return total


def _admin_stats(conn: sqlite3.Connection) -> dict:
    count = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    por_tipo = dict(conn.execute("SELECT type, count(*) FROM pages GROUP BY type").fetchall())
    ahora = datetime.now(timezone.utc)
    hace = lambda dias: (ahora - timedelta(days=dias)).isoformat()  # noqa: E731

    # Altas por mes, los últimos 12 meses (de las notas, bases y filas). Todos
    # los meses salen, también los que no tuvieron ninguna (a cero).
    claves = []
    y, m = ahora.year, ahora.month
    for _ in range(12):
        claves.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    claves.reverse()
    desde = claves[0]
    meses: dict[str, dict] = {
        k: {"month": k, "note": 0, "database": 0, "database_row": 0} for k in claves
    }
    for mes, tipo, n in conn.execute(
        "SELECT substr(created_at, 1, 7) AS mes, type, count(*) FROM pages"
        " WHERE substr(created_at, 1, 7) >= ? GROUP BY mes, type ORDER BY mes",
        (desde,),
    ):
        if mes in meses and tipo in meses[mes]:
            meses[mes][tipo] = n

    creadores = [
        {"name": nombre or "Sin autor", "count": n}
        for nombre, n in conn.execute(
            "SELECT created_by, count(*) AS n FROM pages WHERE type != 'database_row'"
            " GROUP BY created_by ORDER BY n DESC LIMIT 5"
        )
    ]
    usuarios = conn.execute(
        "SELECT count(*), sum(is_admin), min(created_at), max(created_at) FROM users"
    ).fetchone()
    raices = count(
        "SELECT count(*) FROM pages WHERE parent_id IS NULL AND type != 'database_row'"
    )
    compartidas = count(
        "SELECT count(*) FROM pages p WHERE p.parent_id IS NULL AND p.type != 'database_row'"
        " AND EXISTS (SELECT 1 FROM acl s WHERE s.resource_id = p.acl_source"
        " AND NOT (s.subject_type = 'user' AND s.level = 'owner'))"
    )
    from app.routers.collab import rooms

    return {
        "counts": {
            "notes": por_tipo.get("note", 0),
            "databases": por_tipo.get("database", 0),
            "rows": por_tipo.get("database_row", 0),
            "views": count("SELECT count(*) FROM views"),
            "links": count("SELECT count(*) FROM links"),
            "users": usuarios[0] or 0,
            "admins": usuarios[1] or 0,
            "sessions_active": count(
                "SELECT count(*) FROM sessions WHERE expires_at > ?", ahora.isoformat()
            ),
            "trash": count(
                "SELECT count(*) FROM documents WHERE path >= '.trash/' AND path < '.trash0'"
            ),
        },
        "activity": {
            "created_7d": count("SELECT count(*) FROM pages WHERE created_at >= ?", hace(7)),
            "created_30d": count("SELECT count(*) FROM pages WHERE created_at >= ?", hace(30)),
            "edited_24h": count("SELECT count(*) FROM pages WHERE updated_at >= ?", hace(1)),
            "edited_7d": count("SELECT count(*) FROM pages WHERE updated_at >= ?", hace(7)),
        },
        "by_month": list(meses.values()),
        "top_creators": creadores,
        "users_since": usuarios[2],
        "last_user_at": usuarios[3],
        "sharing": {"roots": raices, "shared": compartidas, "private": raices - compartidas},
        "storage": {
            "index_bytes": _size(settings.index_path),
            "documents_bytes": count("SELECT coalesce(sum(size), 0) FROM documents"),
            "projection": settings.vault_projection,
            "watcher": bool(settings.vault_projection and settings.watch_enabled),
        },
        "collab": rooms.stats(),
    }


@router.get("/about")
def get_about(
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    from app.startup import startup_index

    arranque = startup_index.snapshot()
    salida = {
        "version": version_info(),
        "server": {
            "started_at": datetime.fromtimestamp(_STARTED, timezone.utc).isoformat(),
            "uptime_s": int(time.time() - _STARTED),
            "startup": arranque.get("status"),
        },
        "api": {"ok": True},
        "db": _db_status(repo.conn),
        "you": {"name": user.display_name, "is_admin": bool(user.is_admin)},
    }
    if user.is_admin:
        salida["admin"] = _admin_stats(repo.conn)
    return salida
