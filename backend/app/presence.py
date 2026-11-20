"""Presencia (plan H7): quién está conectado y en qué página.

Dos cosas distintas:

- **Pasiva:** cada pestaña abierta manda un latido (`POST /presence/heartbeat`)
  al abrir una página y cada pocos segundos; el servidor guarda en memoria quién
  ha latido hace menos de `TTL` segundos y qué página mira. No se escribe nada en
  el índice: es estado efímero, no un dato (se pierde al reiniciar, y no pasa
  nada porque las pestañas vuelven a latir).
- **Manual:** el estado que cada uno fija —disponible, en foco, en reunión o un
  texto— sí se guarda (en `meta`), porque lo pone la persona y sobrevive a un
  reinicio.

**Privacidad.** `GET /presence` dice en qué página está alguien solo a quien
puede **leer esa página** (`acl`); a los demás les sale «en línea». Y solo se ve
a quien comparte algún espacio con quien pregunta (el superadministrador, a todos).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Optional

from app import acl
from app import index as index_mod
from app import workspaces as ws_mod

TTL = 75.0  # segundos sin latido para dejar de estar «en línea»
STATUSES = ("available", "focus", "meeting", "custom")
META_STATUS = "presence_status"  # JSON {user_id: {"status", "text"}}
MAX_TEXT = 80

_lock = threading.Lock()
_beats: dict[str, dict[str, Any]] = {}


def beat(user_id: str, page_id: Optional[str], now: Optional[float] = None) -> bool:
    """Registra un latido. Devuelve si cambió algo que merece avisar a los demás
    (acaba de conectarse o cambió de página)."""
    now = time.monotonic() if now is None else now
    with _lock:
        previo = _beats.get(user_id)
        vivo = previo is not None and now - previo["ts"] < TTL
        cambio = not vivo or previo["page_id"] != page_id
        _beats[user_id] = {"page_id": page_id, "ts": now}
        return cambio


def leave(user_id: str) -> None:
    with _lock:
        _beats.pop(user_id, None)


def online(now: Optional[float] = None) -> dict[str, Optional[str]]:
    """`{user_id: page_id}` de quien ha latido hace menos de `TTL` segundos."""
    now = time.monotonic() if now is None else now
    with _lock:
        for uid in [u for u, b in _beats.items() if now - b["ts"] >= TTL]:
            del _beats[uid]
        return {u: b["page_id"] for u, b in _beats.items()}


def reset() -> None:
    with _lock:
        _beats.clear()


# ------------------------------------------------------------ estado manual


def _statuses(conn: sqlite3.Connection) -> dict[str, dict]:
    raw = index_mod.get_meta(conn, META_STATUS)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def get_status(conn: sqlite3.Connection, user_id: str) -> dict[str, str]:
    s = _statuses(conn).get(user_id) or {}
    return {"status": s.get("status", "available"), "text": s.get("text", "")}


def set_status(conn: sqlite3.Connection, user_id: str, status: str, text: str = "") -> dict[str, str]:
    if status not in STATUSES:
        raise ValueError(f"estado desconocido: {status!r}")
    text = (text or "").strip()[:MAX_TEXT]
    if status == "custom" and not text:
        raise ValueError("escribe de qué se trata el estado personalizado")
    data = _statuses(conn)
    if status == "available":
        data.pop(user_id, None)
    else:
        data[user_id] = {"status": status, "text": text if status == "custom" else ""}
    index_mod.set_meta(conn, META_STATUS, json.dumps(data, sort_keys=True, ensure_ascii=False))
    return get_status(conn, user_id)


# ------------------------------------------------------------------ listado


def listing(conn: sqlite3.Connection, user: Any) -> list[dict[str, Any]]:
    """Quién está en línea para `user`, con la página solo si la puede leer."""
    vivos = online()
    if not vivos:
        return []
    statuses = _statuses(conn)
    mine = None
    if not getattr(user, "is_admin", False):
        sql, args = ws_mod.member_workspaces_sql(user.id)
        mine = {r[0] for r in conn.execute(sql, args)}
    from app import auth  # import tardío

    out = []
    for uid, page_id in vivos.items():
        row = conn.execute(
            "SELECT id, username, display_name, color, avatar FROM users WHERE id = ?", (uid,)
        ).fetchone()
        if row is None:
            continue
        if uid != str(user.id) and mine is not None:
            # Solo se ve a quien comparte algún espacio.
            sql, args = ws_mod.member_workspaces_sql(uid)
            if not ({r[0] for r in conn.execute(sql, args)} & mine):
                continue
        viewing = None
        if page_id:
            page = conn.execute("SELECT title FROM pages WHERE id = ?", (page_id,)).fetchone()
            if page is not None and acl.level_for(conn, user, page_id) is not None:
                viewing = {"page_id": page_id, "title": page["title"] or "Sin título"}
        st = statuses.get(uid) or {}
        out.append({
            "id": uid,
            "display_name": row["display_name"],
            "username": row["username"],
            "color": row["color"],
            "avatar": auth.avatar_of(row),
            "status": st.get("status", "available"),
            "status_text": st.get("text", ""),
            "viewing": viewing,
            "you": uid == str(user.id),
        })
    out.sort(key=lambda p: (not p["you"], p["display_name"].casefold()))
    return out


def by_workspace(conn: sqlite3.Connection, user: Any, online_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """El panel de miembros (estilo Discord): los espacios que ve `user`, cada
    uno con sus miembros —directos y por grupo— y si están en línea.

    Los «Espacio de …» personales no salen: solo tienen a su dueño. Lo que se
    sabe de cada conectado (estado, página que mira) sale de `online_list`, que
    ya está filtrado por lo que `user` puede leer.
    """
    from app import auth  # import tardío

    vivos = {p["id"]: p for p in online_list}
    statuses = _statuses(conn)
    out = []
    for w in ws_mod.list_for(conn, user):
        if w.get("personal_of"):
            continue
        ids = {
            r[0]
            for r in conn.execute(
                "SELECT m.subject_id FROM workspace_members m WHERE m.workspace_id = ? AND m.subject_type = 'user'"
                " UNION SELECT gm.user_id FROM workspace_members m JOIN group_members gm ON gm.group_id = m.subject_id"
                " WHERE m.workspace_id = ? AND m.subject_type = 'group'",
                (w["id"], w["id"]),
            )
        }
        miembros = []
        for uid in ids:
            if uid in vivos:
                p = dict(vivos[uid])
                p["online"] = True
                p["role"] = ws_mod.member_role(conn, uid, w["id"])
                miembros.append(p)
                continue
            row = conn.execute(
                "SELECT id, username, display_name, color, avatar FROM users WHERE id = ?", (uid,)
            ).fetchone()
            if row is None:
                continue
            st = statuses.get(uid) or {}
            miembros.append({
                "id": uid,
                "display_name": row["display_name"],
                "username": row["username"],
                "color": row["color"],
                "avatar": auth.avatar_of(row),
                "status": st.get("status", "available"),
                "status_text": st.get("text", ""),
                "viewing": None,
                "you": uid == str(user.id),
                "online": False,
                "role": ws_mod.member_role(conn, uid, w["id"]),
            })
        miembros.sort(key=lambda p: (not p["online"], p["display_name"].casefold()))
        out.append({"id": w["id"], "name": w["name"], "icon": w["icon"], "members": miembros})
    return out
