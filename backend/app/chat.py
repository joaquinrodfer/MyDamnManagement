"""Chat del equipo (plan del chat, `docs/PLAN-CHAT.md`).

- **Canal de espacio** (`kind = workspace`): uno por espacio no personal, que
  se crea solo la primera vez que alguien lo pide. Lo leen y escriben los
  miembros con al menos el rol del ajuste del espacio `chat_min_role`
  (`editor` por defecto; `viewer` = todos los miembros, `admin` = solo sus
  administradores). Un lector, por defecto, no lo ve.
- **Chat privado** (`kind = direct`): dos personas y nada más; no se añade a
  nadie ni se sale de él.
- **Grupo** (`kind = group`; en el índice, `direct` con `is_group = 1`): con
  nombre, de 2 a `MAX_GROUP` personas (`chat_members`). Cualquiera de dentro
  añade gente o sale; quien lo creó (`created_by`, su administrador; si se va,
  pasa a quien lleve más tiempo dentro) puede además sacar a alguien; cada entrada y salida queda como un mensaje del sistema
  (`event_json`) y quien entra recibe un aviso (`chat_added`).
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
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app import workspaces as ws_mod

MAX_GROUP = 50
MAX_TITLE = 80
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
    if conv["kind"] in ("direct", "group"):
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
    conv = {k: row[k] for k in row.keys()}
    if conv.get("is_group"):
        conv["kind"] = "group"
    return conv


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
            nombres = ", ".join(p["display_name"] for p in otros)
            if conv["kind"] == "group":
                item["admin_id"] = conv.get("created_by")
                item["title"] = conv["title"] or nombres or "Grupo"
            else:
                item["title"] = nombres or "(ya no está)"
        salida.append(item)
    canales = sorted((c for c in salida if c["kind"] == "workspace"), key=lambda c: c["title"].casefold())
    resto = sorted((c for c in salida if c["kind"] != "workspace"), key=lambda c: c["updated_at"], reverse=True)
    return canales + resto


def unread_total(conn: sqlite3.Connection, user: Any) -> int:
    return sum(c["unread"] for c in listing(conn, user))


# --------------------------------------------------------------------------
# directos (CH2)
# --------------------------------------------------------------------------


def open_direct(conn: sqlite3.Connection, user: Any, user_ids: Iterable[str]) -> dict[str, Any]:
    """El chat privado con otra persona: el que ya había o uno nuevo. Es de
    dos; para más gente, un grupo (`create_group`)."""
    yo = _uid(user)
    otros = {str(u) for u in user_ids if str(u).strip()} - {yo}
    if not otros:
        raise ChatError("Elige a una persona.")
    if len(otros) > 1:
        raise ChatError("Un chat privado es entre dos personas: para hablar con varias, crea un grupo.")
    if len(_people(conn, otros)) != 1:
        raise ChatError("Esa persona no existe.", 404)
    gente = otros | {yo}
    for (cid,) in conn.execute(
        "SELECT c.id FROM chat_conversations c JOIN chat_members m ON m.conversation_id = c.id"
        " WHERE c.kind = 'direct' AND c.is_group = 0 AND m.user_id = ?",
        (yo,),
    ).fetchall():
        if participants(conn, {"id": cid, "kind": "direct"}) == gente:
            return get(conn, cid)  # type: ignore[return-value]
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO chat_conversations (id, kind, is_group, created_at, created_by) VALUES (?, 'direct', 0, ?, ?)",
        (cid, _now(), yo),
    )
    for uid in gente:
        conn.execute("INSERT INTO chat_members (conversation_id, user_id, joined_at) VALUES (?, ?, ?)", (cid, uid, _now()))
    return get(conn, cid)  # type: ignore[return-value]


def _clean_title(title: Optional[str]) -> Optional[str]:
    texto = re.sub(r"\s+", " ", title or "").strip()
    if len(texto) > MAX_TITLE:
        raise ChatError(f"El nombre de un grupo es de {MAX_TITLE} caracteres como mucho.")
    return texto or None


def _new_people(conn: sqlite3.Connection, conv_people: set[str], user_ids: Iterable[str]) -> set[str]:
    nuevos = {str(u) for u in user_ids if str(u).strip()} - conv_people
    if len(conv_people) + len(nuevos) > MAX_GROUP:
        raise ChatError(f"Un grupo es de {MAX_GROUP} personas como mucho.")
    if len(_people(conn, nuevos)) != len(nuevos):
        raise ChatError("Alguna de esas personas no existe.", 404)
    return nuevos


def _event(conn: sqlite3.Connection, user: Any, conv: dict[str, Any], tipo: str,
           people: Iterable[str] = (), **extra: Any) -> dict[str, Any]:
    """Un mensaje del sistema («Ana ha añadido a Luis», «Luis ha salido…»).
    Guarda los nombres de ese momento, por si luego cambian o la persona se va."""
    gente = _people(conn, people)
    datos = {"type": tipo, "users": [{"id": u, "name": p["display_name"]} for u, p in sorted(gente.items())], **extra}
    cur = conn.execute(
        "INSERT INTO chat_messages (conversation_id, user_id, body, created_at, event_json) VALUES (?, ?, '', ?, ?)",
        (conv["id"], _uid(user), _now(), json.dumps(datos, ensure_ascii=False)),
    )
    if tipo != "left":
        mark_read(conn, user, conv, cur.lastrowid)
    return get_message(conn, cur.lastrowid)  # type: ignore[return-value]


def _notify_added(conn: sqlite3.Connection, user: Any, conv: dict[str, Any], people: Iterable[str]) -> None:
    from app import notifications

    autor = getattr(user, "display_name", None) or "Alguien"
    titulo = conv.get("title") or "un grupo"
    for uid in people:
        notifications.notify(
            conn, uid, "chat_added", f"{autor} te ha añadido al grupo «{titulo}»",
            dedupe_key=f"chat-added:{conv['id']}:{_now()}", conversation_id=conv["id"],
        )


def create_group(conn: sqlite3.Connection, user: Any, title: Optional[str],
                 user_ids: Iterable[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Un grupo nuevo (siempre nuevo: puede haber varios con la misma gente).
    Devuelve la conversación y su primer mensaje del sistema."""
    yo = _uid(user)
    nombre = _clean_title(title)
    if not nombre:
        raise ChatError("Ponle un nombre al grupo.")
    otros = _new_people(conn, {yo}, user_ids)
    if not otros:
        raise ChatError("Elige al menos a una persona.")
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO chat_conversations (id, kind, is_group, title, created_at, created_by) VALUES (?, 'direct', 1, ?, ?, ?)",
        (cid, nombre, _now(), yo),
    )
    for uid in otros | {yo}:
        conn.execute("INSERT INTO chat_members (conversation_id, user_id, joined_at) VALUES (?, ?, ?)", (cid, uid, _now()))
    conv = get(conn, cid)
    evento = _event(conn, user, conv, "created", otros, title=nombre)  # type: ignore[arg-type]
    _notify_added(conn, user, conv, otros)  # type: ignore[arg-type]
    return conv, evento  # type: ignore[return-value]


def add_members(conn: sqlite3.Connection, user: Any, conv: dict[str, Any],
                user_ids: Iterable[str]) -> Optional[dict[str, Any]]:
    """Añade gente a un grupo; devuelve el mensaje del sistema (o None si no
    había nadie nuevo)."""
    if conv["kind"] == "workspace":
        raise ChatError("Al canal de un espacio se entra siendo miembro del espacio.")
    if conv["kind"] == "direct":
        raise ChatError("A un chat privado no se añade a nadie: crea un grupo.")
    nuevos = _new_people(conn, participants(conn, conv), user_ids)
    if not nuevos:
        return None
    for uid in nuevos:
        conn.execute("INSERT OR IGNORE INTO chat_members (conversation_id, user_id, joined_at) VALUES (?, ?, ?)",
                     (conv["id"], uid, _now()))
    evento = _event(conn, user, conv, "added", nuevos)
    _notify_added(conn, user, conv, nuevos)
    return evento


def rename_group(conn: sqlite3.Connection, user: Any, conv: dict[str, Any], title: Optional[str]) -> Optional[dict[str, Any]]:
    if conv["kind"] != "group":
        raise ChatError("Solo los grupos tienen nombre.")
    nombre = _clean_title(title)
    if not nombre:
        raise ChatError("Ponle un nombre al grupo.")
    if nombre == conv.get("title"):
        return None
    conn.execute("UPDATE chat_conversations SET title = ? WHERE id = ?", (nombre, conv["id"]))
    conv["title"] = nombre
    return _event(conn, user, conv, "renamed", title=nombre)


def leave(conn: sqlite3.Connection, user: Any, conv: dict[str, Any]) -> dict[str, Any]:
    """Sale de un grupo; devuelve el mensaje del sistema que lo cuenta."""
    if conv["kind"] == "workspace":
        raise ChatError("Del canal de un espacio se sale dejando el espacio.")
    if conv["kind"] == "direct":
        raise ChatError("De un chat privado no se sale.")
    evento = _event(conn, user, conv, "left")
    conn.execute("DELETE FROM chat_members WHERE conversation_id = ? AND user_id = ?", (conv["id"], _uid(user)))
    _hand_over(conn, conv)
    return evento


def _hand_over(conn: sqlite3.Connection, conv: dict[str, Any]) -> None:
    """Si el administrador del grupo ya no está, lo es quien lleve más tiempo dentro."""
    if conn.execute("SELECT 1 FROM chat_members WHERE conversation_id = ? AND user_id = ?",
                    (conv["id"], conv.get("created_by") or "")).fetchone():
        return
    fila = conn.execute("SELECT user_id FROM chat_members WHERE conversation_id = ? ORDER BY joined_at, user_id LIMIT 1",
                        (conv["id"],)).fetchone()
    conn.execute("UPDATE chat_conversations SET created_by = ? WHERE id = ?", (fila[0] if fila else None, conv["id"]))


def remove_member(conn: sqlite3.Connection, user: Any, conv: dict[str, Any], target_id: str) -> dict[str, Any]:
    """El administrador del grupo saca a alguien: queda un mensaje en el grupo
    y la persona recibe un aviso. Devuelve el mensaje del sistema."""
    from app import notifications

    if conv["kind"] != "group":
        raise ChatError("Solo se puede sacar a alguien de un grupo.")
    if conv.get("created_by") != _uid(user):
        raise ChatError("Solo el administrador del grupo puede sacar a alguien.", 403)
    target_id = str(target_id)
    if target_id == _uid(user):
        raise ChatError("Para irte tú, sal del grupo.")
    if target_id not in participants(conn, conv):
        raise ChatError("Esa persona no está en el grupo.", 404)
    evento = _event(conn, user, conv, "removed", [target_id])
    conn.execute("DELETE FROM chat_members WHERE conversation_id = ? AND user_id = ?", (conv["id"], target_id))
    autor = getattr(user, "display_name", None) or "Alguien"
    notifications.notify(
        conn, target_id, "chat_removed", f"{autor} te ha sacado del grupo «{conv.get('title') or 'sin nombre'}»",
        dedupe_key=f"chat-removed:{conv['id']}:{_now()}",
    )
    return evento


# --------------------------------------------------------------------------
# mensajes
# --------------------------------------------------------------------------


def _preview(row: sqlite3.Row) -> dict[str, Any]:
    texto = "" if row["deleted"] else re.sub(r"\s+", " ", row["body"] or "")[:120]
    return {"id": row["id"], "user_id": row["user_id"], "author_name": row["author_name"],
            "body": texto, "deleted": bool(row["deleted"]), "created_at": row["created_at"],
            "event": _event_of(row)}


def _event_of(row: sqlite3.Row) -> Optional[dict[str, Any]]:
    try:
        return json.loads(row["event_json"]) if row["event_json"] else None
    except ValueError:
        return None


# Editar y borrar un mensaje solo vale durante este rato desde que se envió;
# luego el mensaje queda como está (y lo que se cambió deja rastro: «editado»
# con las versiones anteriores, o «Mensaje borrado» con la hora).
CHANGE_WINDOW = timedelta(minutes=15)


def _until(created_at: str) -> str:
    try:
        return (datetime.fromisoformat(created_at) + CHANGE_WINDOW).isoformat(timespec="seconds")
    except ValueError:
        return created_at


def changeable(msg: dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Si aún se puede editar o borrar (dentro de `CHANGE_WINDOW`)."""
    try:
        creado = datetime.fromisoformat(msg["created_at"])
    except (TypeError, ValueError):
        return False
    return (now or datetime.now(timezone.utc)) <= creado + CHANGE_WINDOW


def _message(row: sqlite3.Row, people: dict[str, dict[str, Any]], edits: int = 0) -> dict[str, Any]:
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
        "deleted_at": row["edited_at"] if borrado else None,
        "edits": 0 if borrado else edits,
        "changeable_until": _until(row["created_at"]),
        "task_id": row["task_id"],
        "event": _event_of(row),
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
    ediciones = _edit_counts(conn, [r["id"] for r in filas])
    return [_message(r, gente, ediciones.get(r["id"], 0)) for r in filas]


def _edit_counts(conn: sqlite3.Connection, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT message_id, count(*) FROM chat_message_edits WHERE message_id IN ({marks}) GROUP BY message_id", ids
        )
    }


def get_message(conn: sqlite3.Connection, message_id: int) -> Optional[dict[str, Any]]:
    r = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (int(message_id),)).fetchone()
    if r is None:
        return None
    return _message(r, _people(conn, [r["user_id"]]), _edit_counts(conn, [r["id"]]).get(r["id"], 0))


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
    if msg["user_id"] != _uid(user) or msg.get("event"):
        raise ChatError("solo quien escribió el mensaje puede cambiarlo", 403)
    if not changeable(msg):
        minutos = int(CHANGE_WINDOW.total_seconds() // 60)
        raise ChatError(f"Ya no se puede cambiar: solo se puede en los {minutos} minutos después de enviarlo.", 409)
    return msg


def edit(conn: sqlite3.Connection, user: Any, message_id: int, body: str) -> dict[str, Any]:
    msg = _own(conn, user, message_id)
    texto = _clean(body)
    if not texto and not msg["attachments"]:
        raise ChatError("El mensaje está vacío: bórralo.")
    if texto == msg["body"]:
        return msg
    ahora = _now()
    # El rastro: el texto de antes, con la hora en que se sustituyó.
    conn.execute(
        "INSERT INTO chat_message_edits (message_id, body, replaced_at) VALUES (?, ?, ?)",
        (int(message_id), msg["body"], ahora),
    )
    conn.execute("UPDATE chat_messages SET body = ?, edited_at = ? WHERE id = ?", (texto, ahora, int(message_id)))
    return get_message(conn, message_id)  # type: ignore[return-value]


def delete(conn: sqlite3.Connection, user: Any, message_id: int) -> dict[str, Any]:
    _own(conn, user, message_id)
    # Borrar quita el contenido (también sus versiones anteriores); queda el
    # hueco «Mensaje borrado» con quién y cuándo.
    conn.execute("DELETE FROM chat_message_edits WHERE message_id = ?", (int(message_id),))
    conn.execute(
        "UPDATE chat_messages SET deleted = 1, body = '', attachments_json = '[]', edited_at = ? WHERE id = ?",
        (_now(), int(message_id)),
    )
    return get_message(conn, message_id)  # type: ignore[return-value]


def edit_history(conn: sqlite3.Connection, message_id: int) -> list[dict[str, Any]]:
    """Las versiones anteriores de un mensaje, de la más vieja a la más nueva."""
    return [
        {"body": r["body"], "replaced_at": r["replaced_at"]}
        for r in conn.execute(
            "SELECT body, replaced_at FROM chat_message_edits WHERE message_id = ? ORDER BY id", (int(message_id),)
        )
    ]


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
    elif conv["kind"] == "group":
        donde = f"en el grupo «{conv.get('title') or 'sin nombre'}»"
    else:
        donde = "en un chat privado"
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
        f" WHERE chat_fts MATCH ? AND m.deleted = 0 AND m.event_json IS NULL AND m.conversation_id IN ({marks})"
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
