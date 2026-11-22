"""Chat del equipo (plan del chat, `docs/PLAN-CHAT.md`).

- **Canal de espacio** (`kind = workspace`): uno por espacio no personal, que
  se crea solo la primera vez que alguien lo pide. Lo leen y escriben los
  miembros con al menos el rol del ajuste del espacio `chat_min_role`
  (`editor` por defecto; `viewer` = todos los miembros, `admin` = solo sus
  administradores). Un lector, por defecto, no lo ve.
- **Mensaje directo** (`kind = direct`): sus participantes (`chat_members`),
  hasta `MAX_DIRECT` personas.
- Nadie más, tampoco el administrador de la instalación (plan de roles, R1).

Los mensajes viven en el índice (no en el vault: no son notas) y se guardan
siempre. Cada cambio se publica en el bus de eventos (`type: chat`) solo para
quien puede ver la conversación (`recipients`, que `/events` quita al enviar).
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from app import workspaces as ws_mod

MAX_DIRECT = 8
MAX_BODY = 4000
PAGE_SIZE = 50
CHAT_ROLES = ("viewer", "editor", "admin")
DEFAULT_MIN_ROLE = "editor"
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_.-]{2,40})")


class ChatError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid(user: Any) -> str:
    return str(getattr(user, "id", user))


# --------------------------------------------------------------------------
# quién está en qué
# --------------------------------------------------------------------------


def min_role(settings: Any) -> str:
    if isinstance(settings, str):
        try:
            settings = json.loads(settings or "{}")
        except ValueError:
            settings = {}
    valor = (settings or {}).get("chat_min_role") if isinstance(settings, dict) else None
    return valor if valor in CHAT_ROLES else DEFAULT_MIN_ROLE


def workspace_people(conn: sqlite3.Connection, workspace_id: str) -> dict[str, str]:
    """`{persona: mejor rol}` de los miembros del espacio (directos o por grupo)."""
    roles: dict[str, list[str]] = {}
    for uid, role in conn.execute(
        "SELECT subject_id, role FROM workspace_members WHERE workspace_id = ? AND subject_type = 'user'"
        " UNION ALL SELECT gm.user_id, m.role FROM workspace_members m JOIN group_members gm"
        " ON gm.group_id = m.subject_id WHERE m.workspace_id = ? AND m.subject_type = 'group'",
        (workspace_id, workspace_id),
    ):
        roles.setdefault(str(uid), []).append(role)
    return {uid: ws_mod.best_role(*rs) for uid, rs in roles.items()}


def _rank(role: Optional[str]) -> int:
    return ws_mod.ROLES.get(role or "", 0)


def participants(conn: sqlite3.Connection, conv: dict[str, Any]) -> set[str]:
    """Quién puede ver la conversación (y recibe sus eventos)."""
    if conv["kind"] == "direct":
        return {r[0] for r in conn.execute(
            "SELECT user_id FROM chat_members WHERE conversation_id = ?", (conv["id"],)
        )}
    fila = conn.execute("SELECT settings FROM workspaces WHERE id = ?", (conv["workspace_id"],)).fetchone()
    if fila is None or ws_mod.is_personal(fila):
        return set()
    minimo = _rank(min_role(fila["settings"]))
    return {uid for uid, role in workspace_people(conn, conv["workspace_id"]).items() if _rank(role) >= minimo}


def can_access(conn: sqlite3.Connection, user: Any, conv: dict[str, Any]) -> bool:
    return _uid(user) in participants(conn, conv)


# --------------------------------------------------------------------------
# conversaciones
# --------------------------------------------------------------------------


def _conv(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def get(conn: sqlite3.Connection, conversation_id: str) -> Optional[dict[str, Any]]:
    r = conn.execute("SELECT * FROM chat_conversations WHERE id = ?", (str(conversation_id),)).fetchone()
    return _conv(r) if r else None


def get_for(conn: sqlite3.Connection, user: Any, conversation_id: str) -> dict[str, Any]:
    """La conversación si se puede ver; 404 (no existe) si no."""
    conv = get(conn, conversation_id)
    if conv is None or not can_access(conn, user, conv):
        raise ChatError("no existe esa conversación", 404)
    return conv


def ensure_workspace_channels(conn: sqlite3.Connection, user: Any) -> None:
    """Crea el canal de cada espacio (no personal) del que es miembro y aún no lo tiene."""
    sql, args = ws_mod.member_workspaces_sql(_uid(user))
    for r in conn.execute(
        f"SELECT w.id, w.settings FROM workspaces w WHERE w.id IN ({sql})"
        " AND NOT EXISTS (SELECT 1 FROM chat_conversations c WHERE c.kind = 'workspace' AND c.workspace_id = w.id)",
        args,
    ).fetchall():
        if ws_mod.is_personal(r):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO chat_conversations (id, kind, workspace_id, created_at) VALUES (?, 'workspace', ?, ?)",
            (str(uuid.uuid4()), r["id"], _now()),
        )


def _people(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    from app import auth

    ids = sorted(set(ids))
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {
        r["id"]: {"id": r["id"], "display_name": r["display_name"], "username": r["username"],
                  "color": r["color"], "avatar": auth.avatar_of(r)}
        for r in conn.execute(f"SELECT id, username, display_name, color, avatar FROM users WHERE id IN ({marks})", ids)
    }


def listing(conn: sqlite3.Connection, user: Any) -> list[dict[str, Any]]:
    """Las conversaciones que veo: primero los canales, luego los directos (por
    el último mensaje), con su último mensaje y cuántos no he leído."""
    ensure_workspace_channels(conn, user)
    yo = _uid(user)
    sql, args = ws_mod.member_workspaces_sql(yo)
    filas = conn.execute(
        "SELECT c.*, w.name AS workspace_name, w.icon AS workspace_icon, w.settings AS workspace_settings"
        " FROM chat_conversations c LEFT JOIN workspaces w ON w.id = c.workspace_id"
        f" WHERE (c.kind = 'workspace' AND c.workspace_id IN ({sql}))"
        " OR (c.kind = 'direct' AND c.id IN (SELECT conversation_id FROM chat_members WHERE user_id = ?))",
        [*args, yo],
    ).fetchall()
    salida = []
    for r in filas:
        conv = _conv(r)
        if conv["kind"] == "workspace" and not can_access(conn, user, conv):
            continue
        ultimo = conn.execute(
            "SELECT m.*, u.display_name AS author_name FROM chat_messages m LEFT JOIN users u ON u.id = m.user_id"
            " WHERE m.conversation_id = ? ORDER BY m.id DESC LIMIT 1",
            (conv["id"],),
        ).fetchone()
        leido = conn.execute(
            "SELECT last_read_id FROM chat_reads WHERE conversation_id = ? AND user_id = ?", (conv["id"], yo)
        ).fetchone()
        sin_leer = conn.execute(
            "SELECT count(*) FROM chat_messages WHERE conversation_id = ? AND id > ? AND user_id != ? AND deleted = 0",
            (conv["id"], leido[0] if leido else 0, yo),
        ).fetchone()[0]
        item = {
            "id": conv["id"], "kind": conv["kind"], "workspace_id": conv["workspace_id"],
            "unread": sin_leer,
            "last": _preview(ultimo) if ultimo else None,
            "updated_at": ultimo["created_at"] if ultimo else conv["created_at"],
        }
        if conv["kind"] == "workspace":
            item["title"] = r["workspace_name"]
            item["icon"] = r["workspace_icon"]
            item["min_role"] = min_role(r["workspace_settings"])
        else:
            miembros = _people(conn, participants(conn, conv))
            otros = [p for uid, p in miembros.items() if uid != yo]
            item["members"] = list(miembros.values())
            item["title"] = conv["title"] or ", ".join(p["display_name"] for p in otros) or "Solo tú"
        salida.append(item)
    salida.sort(key=lambda c: (c["kind"] != "workspace", c["title"].casefold() if c["kind"] == "workspace" else ""))
    canales = [c for c in salida if c["kind"] == "workspace"]
    directos = sorted((c for c in salida if c["kind"] == "direct"), key=lambda c: c["updated_at"], reverse=True)
    return canales + directos


def unread_total(conn: sqlite3.Connection, user: Any) -> int:
    return sum(c["unread"] for c in listing(conn, user))


# --------------------------------------------------------------------------
# directos (CH2)
# --------------------------------------------------------------------------


def open_direct(conn: sqlite3.Connection, user: Any, user_ids: Iterable[str]) -> dict[str, Any]:
    """El directo con esas personas (y quien pide): el que ya había con
    exactamente las mismas, o uno nuevo."""
    yo = _uid(user)
    gente = {str(u) for u in user_ids if str(u).strip()} | {yo}
    if len(gente) < 2:
        raise ChatError("Elige al menos a una persona.")
    if len(gente) > MAX_DIRECT:
        raise ChatError(f"Un mensaje directo es para {MAX_DIRECT} personas como mucho.")
    if len(_people(conn, gente)) != len(gente):
        raise ChatError("Alguna de esas personas no existe.", 404)
    for (cid,) in conn.execute(
        "SELECT c.id FROM chat_conversations c JOIN chat_members m ON m.conversation_id = c.id"
        " WHERE c.kind = 'direct' AND m.user_id = ?",
        (yo,),
    ).fetchall():
        if participants(conn, {"id": cid, "kind": "direct"}) == gente:
            return get(conn, cid)  # type: ignore[return-value]
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO chat_conversations (id, kind, created_at, created_by) VALUES (?, 'direct', ?, ?)",
        (cid, _now(), yo),
    )
    for uid in gente:
        conn.execute("INSERT INTO chat_members (conversation_id, user_id, joined_at) VALUES (?, ?, ?)", (cid, uid, _now()))
    return get(conn, cid)  # type: ignore[return-value]


def add_members(conn: sqlite3.Connection, user: Any, conv: dict[str, Any], user_ids: Iterable[str]) -> None:
    if conv["kind"] != "direct":
        raise ChatError("Al canal de un espacio se entra siendo miembro del espacio.")
    actuales = participants(conn, conv)
    nuevos = {str(u) for u in user_ids} - actuales
    if len(actuales) + len(nuevos) > MAX_DIRECT:
        raise ChatError(f"Un mensaje directo es para {MAX_DIRECT} personas como mucho.")
    if len(_people(conn, nuevos)) != len(nuevos):
        raise ChatError("Alguna de esas personas no existe.", 404)
    for uid in nuevos:
        conn.execute("INSERT OR IGNORE INTO chat_members (conversation_id, user_id, joined_at) VALUES (?, ?, ?)",
                     (conv["id"], uid, _now()))


def leave(conn: sqlite3.Connection, user: Any, conv: dict[str, Any]) -> None:
    if conv["kind"] != "direct":
        raise ChatError("Del canal de un espacio se sale dejando el espacio.")
    conn.execute("DELETE FROM chat_members WHERE conversation_id = ? AND user_id = ?", (conv["id"], _uid(user)))


# --------------------------------------------------------------------------
# mensajes
# --------------------------------------------------------------------------


def _preview(row: sqlite3.Row) -> dict[str, Any]:
    texto = "" if row["deleted"] else re.sub(r"\s+", " ", row["body"] or "")[:120]
    return {"id": row["id"], "user_id": row["user_id"], "author_name": row["author_name"],
            "body": texto, "deleted": bool(row["deleted"]), "created_at": row["created_at"]}


def _message(row: sqlite3.Row, people: dict[str, dict[str, Any]]) -> dict[str, Any]:
    borrado = bool(row["deleted"])
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "user_id": row["user_id"],
        "author": people.get(row["user_id"]),
        "body": "" if borrado else row["body"],
        "attachments": [] if borrado else json.loads(row["attachments_json"] or "[]"),
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
        "deleted": borrado,
        "task_id": row["task_id"],
    }


def messages(conn: sqlite3.Connection, conv: dict[str, Any], before: Optional[int] = None,
             limit: int = PAGE_SIZE) -> list[dict[str, Any]]:
    """Los mensajes de la conversación, del más viejo al más nuevo; `before`
    pide la página anterior (los `limit` anteriores a ese id)."""
    limit = max(1, min(int(limit), 200))
    sql = "SELECT * FROM chat_messages WHERE conversation_id = ?"
    args: list[Any] = [conv["id"]]
    if before:
        sql += " AND id < ?"
        args.append(int(before))
    filas = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()[::-1]
    gente = _people(conn, {r["user_id"] for r in filas})
    return [_message(r, gente) for r in filas]


def get_message(conn: sqlite3.Connection, message_id: int) -> Optional[dict[str, Any]]:
    r = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (int(message_id),)).fetchone()
    if r is None:
        return None
    return _message(r, _people(conn, [r["user_id"]]))


def _clean(body: str) -> str:
    texto = (body or "").strip()
    if len(texto) > MAX_BODY:
        raise ChatError(f"Un mensaje es de {MAX_BODY} caracteres como mucho.")
    return texto


def _clean_attachments(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    if not isinstance(raw, list) or len(raw) > 10:
        raise ChatError("Como mucho 10 adjuntos por mensaje.")
    out = []
    for a in raw:
        if not isinstance(a, dict) or not str(a.get("url") or "").startswith("/files/"):
            raise ChatError("Un adjunto es un archivo subido a Notemil.")
        out.append({
            "url": str(a["url"])[:500],
            "name": str(a.get("name") or "archivo")[:200],
            "type": str(a.get("type") or "")[:100],
            "size": int(a.get("size") or 0),
        })
    return out


def post(conn: sqlite3.Connection, user: Any, conv: dict[str, Any], body: str,
         attachments: Any = None) -> dict[str, Any]:
    texto = _clean(body)
    adjuntos = _clean_attachments(attachments)
    if not texto and not adjuntos:
        raise ChatError("El mensaje está vacío.")
    cur = conn.execute(
        "INSERT INTO chat_messages (conversation_id, user_id, body, attachments_json, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (conv["id"], _uid(user), texto, json.dumps(adjuntos, ensure_ascii=False), _now()),
    )
    mark_read(conn, user, conv, cur.lastrowid)
    return get_message(conn, cur.lastrowid)  # type: ignore[return-value]


def _own(conn: sqlite3.Connection, user: Any, message_id: int) -> dict[str, Any]:
    msg = get_message(conn, message_id)
    if msg is None or msg["deleted"]:
        raise ChatError("no existe ese mensaje", 404)
    if msg["user_id"] != _uid(user):
        raise ChatError("solo quien escribió el mensaje puede cambiarlo", 403)
    return msg


def edit(conn: sqlite3.Connection, user: Any, message_id: int, body: str) -> dict[str, Any]:
    msg = _own(conn, user, message_id)
    texto = _clean(body)
    if not texto and not msg["attachments"]:
        raise ChatError("El mensaje está vacío: bórralo.")
    conn.execute("UPDATE chat_messages SET body = ?, edited_at = ? WHERE id = ?", (texto, _now(), int(message_id)))
    return get_message(conn, message_id)  # type: ignore[return-value]


def delete(conn: sqlite3.Connection, user: Any, message_id: int) -> dict[str, Any]:
    _own(conn, user, message_id)
    conn.execute(
        "UPDATE chat_messages SET deleted = 1, body = '', attachments_json = '[]', edited_at = ? WHERE id = ?",
        (_now(), int(message_id)),
    )
    return get_message(conn, message_id)  # type: ignore[return-value]


def mark_read(conn: sqlite3.Connection, user: Any, conv: dict[str, Any], last_id: Optional[int] = None) -> None:
    if last_id is None:
        fila = conn.execute("SELECT max(id) FROM chat_messages WHERE conversation_id = ?", (conv["id"],)).fetchone()
        last_id = fila[0] or 0
    conn.execute(
        "INSERT INTO chat_reads (conversation_id, user_id, last_read_id) VALUES (?, ?, ?)"
        " ON CONFLICT(conversation_id, user_id) DO UPDATE SET last_read_id = max(last_read_id, excluded.last_read_id)",
        (conv["id"], _uid(user), int(last_id)),
    )


def after_post(conn: sqlite3.Connection, user: Any, conv: dict[str, Any], msg: dict[str, Any]) -> int:
    """Después de enviar: un aviso a cada persona que el mensaje menciona con
    `@usuario` y que puede ver la conversación (no a quien lo escribe).
    Devuelve cuántos avisos se crearon."""
    from app import notifications

    nombrados = mentioned(conn, msg.get("body") or "") & participants(conn, conv)
    nombrados.discard(_uid(user))
    if not nombrados:
        return 0
    if conv["kind"] == "workspace":
        fila = conn.execute("SELECT name FROM workspaces WHERE id = ?", (conv["workspace_id"],)).fetchone()
        donde = f"en # {fila['name']}" if fila else "en un canal"
    else:
        donde = "en un mensaje directo"
    autor = getattr(user, "display_name", None) or "Alguien"
    resumen = re.sub(r"\s+", " ", msg.get("body") or "")[:120]
    creados = 0
    for uid in nombrados:
        creados += notifications.notify(
            conn, uid, "chat_mention", f"{autor} te ha mencionado {donde}: {resumen}",
            dedupe_key=f"chat:{msg['id']}", conversation_id=conv["id"],
            workspace_id=conv.get("workspace_id"),
        )
    return creados


def mentioned(conn: sqlite3.Connection, body: str) -> set[str]:
    """Los ids de las personas que nombra un mensaje con `@usuario`."""
    nombres = {m.casefold() for m in MENTION_RE.findall(body or "")}
    if not nombres:
        return set()
    return {r[0] for r in conn.execute("SELECT id, username FROM users") if (r[1] or "").casefold() in nombres}


# --------------------------------------------------------------------------
# eventos en vivo
# --------------------------------------------------------------------------


def publish(conn: sqlite3.Connection, conv: dict[str, Any], action: str, **data: Any) -> None:
    """Avisa por `/events` solo a quien puede ver la conversación."""
    from app.events import bus

    bus.publish({
        "type": "chat", "action": action, "conversation_id": conv["id"],
        "recipients": sorted(participants(conn, conv)), **data,
    })


# --------------------------------------------------------------------------
# buscar (CH4)
# --------------------------------------------------------------------------


def search(conn: sqlite3.Connection, user: Any, q: str, limit: int = 30) -> list[dict[str, Any]]:
    """Los mensajes que casan con `q` en las conversaciones que veo."""
    from app import index as index_mod

    texto = (q or "").strip()
    if len(texto) < 2:
        return []
    mias = {c["id"]: c for c in listing(conn, user)}
    if not mias:
        return []
    ids = sorted(mias)
    marks = ",".join("?" * len(ids))
    filas = conn.execute(
        "SELECT m.* FROM chat_fts f JOIN chat_messages m ON m.id = f.rowid"
        f" WHERE chat_fts MATCH ? AND m.deleted = 0 AND m.conversation_id IN ({marks})"
        " ORDER BY m.id DESC LIMIT ?",
        (index_mod.fts_query(texto), *ids, max(1, min(limit, 100))),
    ).fetchall()
    gente = _people(conn, {r["user_id"] for r in filas})
    salida = []
    for r in filas:
        m = _message(r, gente)
        c = mias[r["conversation_id"]]
        m["conversation_title"] = c["title"]
        m["conversation_kind"] = c["kind"]
        salida.append(m)
    return salida


def set_task(conn: sqlite3.Connection, message_id: int, task_id: str) -> None:
    conn.execute("UPDATE chat_messages SET task_id = ? WHERE id = ?", (str(task_id), int(message_id)))
