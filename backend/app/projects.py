"""Proyectos y sus tareas (plan H4, parte 3).

Una base de **Proyectos** y una de **Tareas** se enlazan con la columna
«Proyecto» de las tareas (una relación hacia la base de Proyectos). De ahí sale,
siempre calculado y nunca escrito a mano:

- las tareas de un proyecto (de todas las bases de Tareas que apuntan a él);
- cuántas están hechas (`closed`, según la regla de cada base) y las horas: el
  total y las pendientes;
- la estimación del proyecto, si su base tiene esa columna, para compararla con
  lo que suman las tareas.

**Entre espacios.** Un proyecto acepta tareas de otro espacio solo si su espacio
lo permite (ajuste `accept_foreign_tasks`). Lo que la persona que pregunta no
puede ver cuenta en los totales pero sale sin título.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from fastapi import HTTPException

from app import closed, kinds
from app.workload import _ids, _number


def accepts_foreign(conn: sqlite3.Connection, workspace_id: str) -> bool:
    row = conn.execute("SELECT settings FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    try:
        return bool(json.loads(row["settings"] or "{}").get("accept_foreign_tasks")) if row else False
    except ValueError:
        return False


def check_project_link(conn: sqlite3.Connection, db_def: Any, cleaned: Optional[dict]) -> None:
    """Una tarea no puede colgar de un proyecto de otro espacio que no lo acepta."""
    kind = getattr(db_def, "kind", None) or {}
    key = (kind.get("roles") or {}).get("project")
    if kind.get("type") != "tasks" or not key or not cleaned or not cleaned.get(key):
        return
    mine = conn.execute("SELECT workspace_id FROM pages WHERE id = ?", (str(db_def.page_id),)).fetchone()
    for pid in _ids(cleaned.get(key)):
        proj = conn.execute("SELECT title, workspace_id FROM pages WHERE id = ?", (pid,)).fetchone()
        if proj is None or mine is None or proj["workspace_id"] == mine["workspace_id"]:
            continue
        if not accepts_foreign(conn, proj["workspace_id"]):
            raise HTTPException(
                400,
                f"El proyecto «{proj['title']}» es de otro espacio que no acepta tareas de fuera "
                "(su administrador puede permitirlo en los ajustes del espacio).",
            )


def tasks_of(conn: sqlite3.Connection, project_row_id: str, visible: Optional[set[str]]) -> dict[str, Any]:
    """Las tareas que cuelgan de un proyecto, con los totales."""
    proj = conn.execute(
        "SELECT r.database_id AS db, r.properties_json AS props, p.title AS title, p.workspace_id AS ws"
        " FROM rows r JOIN pages p ON p.id = r.id WHERE r.id = ?", (project_row_id,)).fetchone()
    if proj is None:
        raise HTTPException(404, "Fila no encontrada")
    pkind = conn.execute("SELECT kind_json FROM database_kinds WHERE database_id = ?", (proj["db"],)).fetchone()
    pkind = json.loads(pkind["kind_json"]) if pkind else {}
    est_key = (pkind.get("roles") or {}).get("estimate")
    estimate = _number(json.loads(proj["props"] or "{}").get(est_key)) if est_key else None

    tasks: list[dict[str, Any]] = []
    for base, schema, roles, clave, rule in _task_bases(conn, proj["db"], proj["ws"]):
        for row in conn.execute(
            "SELECT r.id AS id, r.properties_json AS props, p.title AS title"
            " FROM rows r JOIN pages p ON p.id = r.id WHERE r.database_id = ?", (base["id"],)):
            props = json.loads(row["props"] or "{}")
            if project_row_id not in _ids(props.get(clave)):
                continue
            people = _ids(props.get(roles.get("assignee"))) if roles.get("assignee") else []
            names = [r["display_name"] for r in conn.execute(
                f"SELECT display_name FROM users WHERE id IN ({','.join('?' * len(people))})", people)] if people else []
            visible_row = visible is None or row["id"] in visible
            tasks.append({
                "id": row["id"] if visible_row else None,
                "title": row["title"] or "Sin título" if visible_row else "Tarea privada",
                "base_id": base["id"] if visible_row else None,
                "base_title": base["base_title"] if visible_row else None,
                "status": props.get(roles.get("status")) if roles.get("status") else None,
                "closed": closed.is_closed(rule, props),
                "assignees": names if visible_row else [],
                "hours": _number(props.get(roles.get("hours"))) if roles.get("hours") else 0.0,
                "end": props.get(roles.get("end")) if roles.get("end") else None,
                "masked": not visible_row,
            })
    tasks.sort(key=lambda t: (t["closed"], str(t["end"] or "9999"), t["title"].casefold()))
    total = sum(t["hours"] for t in tasks)
    pending = sum(t["hours"] for t in tasks if not t["closed"])
    return {
        "project": {"id": project_row_id, "title": proj["title"]},
        "tasks": tasks,
        "totals": {
            "count": len(tasks), "closed": sum(1 for t in tasks if t["closed"]),
            "hours": round(total, 2), "hours_open": round(pending, 2),
            "estimate": estimate,
        },
    }


def _task_bases(conn: sqlite3.Connection, projects_db: str, projects_ws: str):
    """Las bases de Tareas cuya columna «Proyecto» apunta a `projects_db`.

    Da `(base, schema, roles, clave del proyecto, regla de cerrado)`. Una base
    de otro espacio solo cuenta si el espacio del proyecto acepta tareas de fuera.
    """
    tipos = kinds.kind_types(conn)
    for base in conn.execute(
        "SELECT d.id AS id, d.schema_json AS schema_json, k.kind_json AS kind_json, c.rule_json AS rule_json,"
        " bp.title AS base_title, bp.workspace_id AS ws"
        " FROM databases d JOIN database_kinds k ON k.database_id = d.id JOIN pages bp ON bp.id = d.page_id"
        " LEFT JOIN database_closed c ON c.database_id = d.id").fetchall():
        kind = json.loads(base["kind_json"] or "null") or {}
        roles = kind.get("roles") or {}
        schema = json.loads(base["schema_json"] or "[]")
        clave = kinds.project_key(kind, schema, tipos) if kind.get("type") == "tasks" else None
        if not clave:
            continue
        prop = next((p for p in schema if p.get("key") == clave), None)
        if not prop or str(prop.get("target_database") or "") != str(projects_db):
            continue
        if base["ws"] != projects_ws and not accepts_foreign(conn, projects_ws):
            continue
        rule, _src = closed.effective(json.loads(base["rule_json"] or "null"), schema)
        yield base, schema, roles, clave, rule


def progress_of(conn: sqlite3.Connection, projects_db: str) -> dict[str, dict[str, Any]]:
    """El avance de cada proyecto de una base, para la columna «Tareas».

    `{fila del proyecto: {count, closed, hours, hours_open}}`, contando todas
    las tareas (también las que quien pregunta no ve, como en `tasks_of`). Los
    proyectos sin tareas no salen.
    """
    page = conn.execute("SELECT workspace_id FROM pages p JOIN databases d ON d.page_id = p.id WHERE d.id = ?",
                        (str(projects_db),)).fetchone()
    if page is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for base, _schema, roles, clave, rule in _task_bases(conn, str(projects_db), page["workspace_id"]):
        for row in conn.execute("SELECT properties_json AS props FROM rows WHERE database_id = ?", (base["id"],)):
            props = json.loads(row["props"] or "{}")
            ids = _ids(props.get(clave))
            if not ids:
                continue
            hecho = closed.is_closed(rule, props)
            horas = _number(props.get(roles.get("hours"))) if roles.get("hours") else 0.0
            for pid in ids:
                t = out.setdefault(pid, {"count": 0, "closed": 0, "hours": 0.0, "hours_open": 0.0})
                t["count"] += 1
                t["closed"] += 1 if hecho else 0
                t["hours"] += horas
                t["hours_open"] += 0.0 if hecho else horas
    for t in out.values():
        t["hours"] = round(t["hours"], 2)
        t["hours_open"] = round(t["hours_open"], 2)
    return out
