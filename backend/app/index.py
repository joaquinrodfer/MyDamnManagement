"""Índice SQLite: el almacén de la app, sin Postgres.

Este módulo sustituye a `app/database.py` (motor + sesiones de SQLAlchemy) y
concentra lo que el spike de P0 validó en `spike/index.py`:

- **Un solo fichero**, sin servicio aparte ni puerto que abrir. Un proceso
  escritor (la API); cada petición abre su propia conexión, porque
  `sqlite3.Connection` no es segura entre hilos y FastAPI sirve las rutas
  síncronas desde un pool de hilos.
- **`PRAGMA user_version` como versión de esquema**, en vez de Alembic. Un
  índice con versión más nueva que este código no se toca: falla con un error
  claro en vez de reventar contra un esquema que no entiende. Si la versión es
  más vieja y hay migración, se aplica (`MIGRATIONS`); si falta alguna, también
  falla en claro.
- **Qué se hace con el fichero que ya está en `INDEX_PATH`**, en este orden:
  1. No empieza por la cabecera de SQLite → no es una base de datos: se aparta
     con sufijo `.corrupto-<fecha>` y se rehace vacío. No se borra: si el daño
     fuera parcial, el original sigue ahí para recuperar lo que se pueda.
  2. Tiene tablas que no son las de este esquema (ninguna `pages`, o una
     `pages` sin las columnas del esquema) → `IndexSchemaError` y **no se toca
     nada**: es un `INDEX_PATH` equivocado, no un índice roto.
  3. Tiene nuestro esquema pero `quick_check` no dice `ok` → `IndexSchemaError`
     y no se toca: en P1.1 el índice es la única copia, así que se prefiere
     parar en claro (el mensaje dice qué comprobación falló) a apartar un
     fichero del que todavía se puede sacar algo.
  4. Vale, pero no tiene ninguna tabla → se le pone el esquema.

  La inspección se hace con una conexión de **solo lectura** (`mode=ro`): abrir
  el fichero con `journal_mode = WAL` ya lo modifica, y con un `INDEX_PATH`
  equivocado eso es tocar un fichero ajeno. La única pérdida de datos posible
  es la del punto 1, cuando SQLite no puede leer el fichero en absoluto.
- **Versión del motor comprobada**: `check_sqlite_version()` en cada conexión.
  Por debajo de 3.27 no se puede ni crear `pages_fts` con `remove_diacritics
  2`; la imagen fija su base para que la versión no dependa de la deriva del
  tag (ver `backend/Dockerfile`).
- **FTS5** con `unicode61 remove_diacritics 2`: `reunion` encuentra `Reunión`.
  La tabla `pages_fts` es *external content* sobre `pages` (`content='pages'`),
  así que no duplica el texto y se mantiene sola con triggers de INSERT,
  UPDATE y DELETE. Esas triggers son la razón de que la búsqueda siga bien sin
  reconstruir el índice a mano en cada guardado.
- **`synchronous = NORMAL` + WAL**: razonable para un índice que, desde P1.2,
  será reconstruible desde los ficheros. Aguanta la caída del proceso sin
  corromper; solo un corte de corriente perdería las últimas escrituras, y
  desde P1.2 eso lo recompone el reindexado.

Columnas que todavía no se llenan (`path`, `mtime`, `size`, `content_hash`) son
metadatos de fichero: existen ya en el esquema (§3 del roadmap) para que P1.2
solo tenga que empezar a escribirlas, y son NULL mientras la verdad sea el
propio índice. `path` es UNIQUE pero admite varios NULL, que es justo lo que
hace falta en esta fase.

**Decisión: `sqlite3` crudo, no SQLAlchemy sobre SQLite.** Argumentos en
`docs/ADR-0002-indice-sqlite.md`; en corto: la FTS5 va en SQL explícito de
todas formas, el ORM solo añadiría un segundo modelo que mantener sincronizado
con la capa de repositorio, y a partir de P1.2 la verdad son ficheros — el ORM
sería el sitio equivocado para modelarla.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("mdm.index")

# Subir esto OBLIGA a añadir la migración correspondiente en MIGRATIONS.
SCHEMA_VERSION = 27

# El workspace por defecto tiene id fijo: la app no tiene multi-workspace y, si
# fuera `uuid4()`, borrar el índice y reconstruirlo cambiaría el
# `pages.workspace_id` de cada fila (el ADR exige que la reconstrucción sea
# idéntica). El nombre y la fecha sí son de la fila singleton.
DEFAULT_WORKSPACE_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "mdm:default-workspace"))

GRANTS_SQL = """CREATE TABLE IF NOT EXISTS grants (
    id           TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'group')),
    subject_id   TEXT NOT NULL,
    capability   TEXT NOT NULL,
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    granted_by   TEXT,
    granted_at   TEXT NOT NULL
)"""
# Un permiso no se repite (NULL = todos los espacios cuenta como un valor).
GRANTS_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS grants_unique ON grants"
    "(capability, subject_type, subject_id, COALESCE(workspace_id, ''))"
)

# El chat del equipo (v24, `docs/PLAN-CHAT.md`): conversaciones (canal de
# espacio o directo), participantes de los directos, mensajes, hasta dónde ha
# leído cada uno y su buscador. No es contenido del vault.
CHAT_SQL = """CREATE TABLE IF NOT EXISTS chat_conversations (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('workspace', 'direct')),
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    title        TEXT,
    created_at   TEXT NOT NULL,
    created_by   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS chat_conversations_workspace ON chat_conversations(workspace_id) WHERE kind = 'workspace';
CREATE TABLE IF NOT EXISTS chat_members (
    conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    joined_at       TEXT NOT NULL,
    PRIMARY KEY (conversation_id, user_id)
);
CREATE INDEX IF NOT EXISTS chat_members_user ON chat_members(user_id);
CREATE TABLE IF NOT EXISTS chat_messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    user_id          TEXT NOT NULL,
    body             TEXT NOT NULL DEFAULT '',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    edited_at        TEXT,
    deleted          INTEGER NOT NULL DEFAULT 0,
    task_id          TEXT
);
CREATE INDEX IF NOT EXISTS chat_messages_conv ON chat_messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS chat_reads (
    conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    last_read_id    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_id, user_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS chat_fts USING fts5(
    body, content = 'chat_messages', content_rowid = 'id', tokenize = "unicode61 remove_diacritics 2"
);
CREATE TRIGGER IF NOT EXISTS chat_fts_ai AFTER INSERT ON chat_messages BEGIN
    INSERT INTO chat_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER IF NOT EXISTS chat_fts_ad AFTER DELETE ON chat_messages BEGIN
    INSERT INTO chat_fts(chat_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER IF NOT EXISTS chat_fts_au AFTER UPDATE OF body ON chat_messages BEGIN
    INSERT INTO chat_fts(chat_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO chat_fts(rowid, body) VALUES (new.id, new.body);
END;
"""

# v26: el rastro de las ediciones de un mensaje del chat (el texto que había
# antes de cada edición).
CHAT_EDITS_SQL = """
CREATE TABLE IF NOT EXISTS chat_message_edits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    replaced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_message_edits_msg ON chat_message_edits(message_id);
"""

# v27: enlaces de un solo uso para entrar (invitación) o poner una contraseña
# nueva (ver `app/access_links.py`).
ACCESS_LINKS_SQL = """
CREATE TABLE IF NOT EXISTS access_links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL CHECK (kind IN ('invite', 'reset')),
    user_id      TEXT,
    workspace_id TEXT,
    role         TEXT,
    display_name TEXT,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    used_at      TEXT
);
"""

# Migraciones futuras: {versión_destino: [sql, ...]}. Se aplican en orden.
MIGRATIONS: dict[int, list[str]] = {
    # P1.2b: `meta` guarda metadatos del índice que no son contenido del vault
    # (cuándo se reindexó por última vez, que es lo que `/status` necesita para
    # dar la deriva). Por eso queda fuera de la huella: ver `DUMP_EXCLUDED`.
    2: ["CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"],
    # Índices de consulta (medidos; ver el comentario de `pages_created` más
    # abajo). `IF NOT EXISTS` hace la migración inocua en un fichero creado ya
    # con `SCHEMA_SQL`, así que el mismo SQL vale para los dos caminos: la
    # migración es lo que lleva los índices a un fichero v2 existente, que es
    # donde `_inspect()` no vuelve a ejecutar `SCHEMA_SQL`.
    3: [
        "CREATE INDEX IF NOT EXISTS pages_created ON pages(created_at)",
        "CREATE INDEX IF NOT EXISTS pages_updated ON pages(updated_at)",
    ],
    # P11a: usuarios y sesiones. Viven en el índice (no en el vault: el vault
    # es de las notas, y sus ficheros los edita cualquiera con un editor de
    # texto). `IF NOT EXISTS` deja la migración inocua en un fichero recién
    # creado con `SCHEMA_SQL`, igual que la 3.
    #
    # `password_hash` y `salt` van en hexadecimal y en columnas separadas: el
    # algoritmo (scrypt) y sus parámetros pueden cambiar sin reinterpretar una
    # cadena empaquetada. De la sesión se guarda el SHA-256 del token, nunca el
    # token: un volcado del índice no da sesiones abiertas.
    4: [
        """CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    color         TEXT,
    created_at    TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0
)""",
        """CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT
)""",
        "CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS sessions_expires ON sessions(expires_at)",
    ],
    # P13: tokens personales de captura (`POST /capture`). Se guarda el SHA-256
    # del token, nunca el token, igual que en `sessions`: un volcado del índice
    # (un backup, un `sqlite3` a mano) no da llaves de captura. `id` es lo que
    # se enseña para poder revocar sin conocer el secreto, y `last_used_at` la
    # última vez que el token capturó algo (NULL = nunca).
    5: [
        """CREATE TABLE IF NOT EXISTS capture_tokens (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    last_used_at TEXT
)""",
        "CREATE INDEX IF NOT EXISTS capture_tokens_user ON capture_tokens(user_id)",
    ],
    # v6: la columna del título de una base es una columna más (se renombra y
    # se quita), pero su clave es la reservada `title`, así que no puede vivir
    # en el `schema`. Va en su propia tabla y no en dos columnas nuevas de
    # `databases` porque `CREATE TABLE IF NOT EXISTS` sí es idempotente: la
    # migración es inocua en un fichero recién creado (donde `SCHEMA_SQL` ya
    # trae la tabla) y en uno viejo.
    6: [
        """CREATE TABLE IF NOT EXISTS database_titles (
    database_id TEXT PRIMARY KEY REFERENCES databases(id) ON DELETE CASCADE,
    name        TEXT,
    hidden      INTEGER NOT NULL DEFAULT 0
)""",
    ],
    # v7: qué cuenta como «fila cerrada» es una regla de la BASE (`closed:
    # {property, values}` en el frontmatter de `Bases/<slug>.md`), no un
    # marcador `done` suelto dentro del `schema`. Aquí se guarda la regla
    # declarada; la que implicaban los `done` viejos no se copia al índice: se
    # deriva del schema al leer (`app.closed.effective`), así que no hace falta
    # migrar ningún fichero.
    #
    # Va en su propia tabla y no en una columna nueva de `databases`, igual que
    # `database_titles`: `CREATE TABLE IF NOT EXISTS` es idempotente y así la
    # migración es inocua en un fichero creado ya con `SCHEMA_SQL` (donde la
    # tabla existe y el `user_version` del fichero de pruebas puede ser viejo).
    7: [
        """CREATE TABLE IF NOT EXISTS database_closed (
    database_id TEXT PRIMARY KEY REFERENCES databases(id) ON DELETE CASCADE,
    rule_json   TEXT NOT NULL
)""",
    ],
    # v8: las plantillas de fila son parte de la base (`row_templates:` en el
    # frontmatter de `Bases/<slug>.md`). Se copian al índice para que la
    # interfaz las lea con la misma consulta que la base; el `.md` sigue siendo
    # la verdad y un reindexado las reconstruye. Como `database_titles`, va en
    # su propia tabla: `CREATE TABLE IF NOT EXISTS` es idempotente y la
    # migración queda inocua en un fichero creado ya con `SCHEMA_SQL`.
    8: [
        """CREATE TABLE IF NOT EXISTS database_row_templates (
    database_id  TEXT PRIMARY KEY REFERENCES databases(id) ON DELETE CASCADE,
    templates_json TEXT NOT NULL
)""",
    ],
    # v9 (3.0, hito 1): el frontmatter de cada página, TAL CUAL (el texto entre
    # los dos `---`, sin ellos), o NULL si el fichero no tiene. Con él y `body`,
    # el índice guarda el documento entero: claves que la app no gestiona,
    # comentarios, orden y estilo del YAML. Es lo que permite que `POST /export`
    # reconstruya el vault desde el índice sin perder nada (ADR-0005, etapa 1),
    # y lo que en el hito 2 pasa a ser la verdad.
    #
    # SQLite no tiene `ADD COLUMN IF NOT EXISTS`: la migración es una función
    # que mira antes si la columna ya está, para que siga siendo inocua en un
    # fichero creado ya con `SCHEMA_SQL` (como las de arriba).
    9: [lambda conn: _add_column(conn, "pages", "frontmatter", "TEXT")],
    # v10 (3.0, hito 2): la verdad pasa a la base (ADR-0005).
    #
    # - `documents`: cada `.md` del vault (papelera incluida) es una fila con su
    #   texto entero. Es lo que antes era el disco: el almacén lee y escribe
    #   aquí (`app.vault.docfs.DocsFS`) y `pages` se deriva de esto en la misma
    #   transacción. El `.md` en disco pasa a ser una proyección opcional.
    # - `pages.version`: cuántas veces se ha escrito la página (ADR-0009 D3).
    #   Sube en cada escritura; `If-Match` y la colaboración se apoyan en ella.
    #
    # La tabla nace vacía: la llena la migración de datos del primer arranque
    # (`VaultStore.rebuild`, que importa el vault de disco una sola vez y lo
    # apunta en `meta.storage`).
    10: [
        """CREATE TABLE IF NOT EXISTS documents (
    path  TEXT PRIMARY KEY,
    text  TEXT NOT NULL,
    mtime REAL NOT NULL,
    size  INTEGER NOT NULL
)""",
        lambda conn: _add_column(conn, "pages", "version", "INTEGER NOT NULL DEFAULT 1"),
    ],
    # v11 (3.0, hito 3): permisos por nota, heredados (ver `app.acl`).
    #
    # - `acl`: quién (`user` o `workspace`) puede qué (`view` < `edit` <
    #   `owner`) sobre qué página o base. Sin claves foráneas a `pages` a
    #   propósito: un reindexado vacía y rellena `pages`, y una cascada se
    #   llevaría los permisos; una nota en la papelera conserva los suyos para
    #   cuando vuelva.
    # - `acl_roots`: las páginas que DECLARAN su permiso (y con él cortan la
    #   herencia). Hace falta aparte de `acl` para poder declarar «nadie más»
    #   (una hija privada dentro de una página compartida) sin ninguna fila.
    # - `pages.acl_source`: la página (ella misma o un ancestro; para una fila,
    #   su base) cuyo permiso manda. Es lo que evita la recursión por consulta:
    #   saber si alguien ve una página es un JOIN con `acl` por esta columna.
    #
    # Al migrar un índice con páginas, las de la raíz quedan compartidas con
    # el espacio (`edit`): nadie pierde de vista lo que veía el día de antes.
    11: [
        """CREATE TABLE IF NOT EXISTS acl (
    id            TEXT PRIMARY KEY,
    subject_type  TEXT NOT NULL CHECK (subject_type IN ('user', 'workspace')),
    subject_id    TEXT NOT NULL,
    resource_type TEXT NOT NULL CHECK (resource_type IN ('page', 'database')),
    resource_id   TEXT NOT NULL,
    level         TEXT NOT NULL CHECK (level IN ('view', 'edit', 'owner')),
    UNIQUE (resource_id, subject_type, subject_id)
)""",
        "CREATE INDEX IF NOT EXISTS acl_subject ON acl(subject_type, subject_id)",
        """CREATE TABLE IF NOT EXISTS acl_roots (
    resource_id   TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL CHECK (resource_type IN ('page', 'database'))
)""",
        lambda conn: _add_column(conn, "pages", "acl_source", "TEXT"),
        "CREATE INDEX IF NOT EXISTS pages_acl_source ON pages(acl_source)",
        lambda conn: _share_existing_roots(conn),
    ],
    # v12 (3.0, hito 5): `pages.title_key`, el título plegado (`links.fold_title`:
    # NFKC + casefold) con su índice. Resolver los `[[enlaces]]` de un guardado
    # era cargar TODOS los títulos del workspace (`TitleIndex.load`): con 5.000
    # notas, la mayor parte de lo que cuesta guardar. Con la columna se piden
    # solo los títulos que nombra el cuerpo. No es contenido (sale del título):
    # fuera de la huella, como `version`.
    12: [
        lambda conn: _add_column(conn, "pages", "title_key", "TEXT"),
        lambda conn: _fill_title_keys(conn),
        "CREATE INDEX IF NOT EXISTS pages_title_key ON pages(workspace_id, title_key)",
    ],
    # v13 (bloque B): `users.avatar`, la imagen que cada uno elige de entre las
    # de la app (`auth.AVATARS`). `NULL` = no ha elegido: se le da una estable
    # por su username (`auth.default_avatar`), como el color.
    13: [lambda conn: _add_column(conn, "users", "avatar", "TEXT")],
    # v14 (plan H1): espacios de trabajo con miembros, grupos y roles.
    # - `workspaces` gana `slug`, `icon` y `settings` (JSON de los ajustes del
    #   espacio); el único que había (`default`) pasa a llamarse **General**.
    # - `groups` y `group_members`; `workspace_members` da acceso a un espacio a
    #   una persona o a un grupo con un rol (`admin`, `editor`, `viewer`).
    # - `acl.subject_type` admite `group` (hay que rehacer la tabla: SQLite no
    #   cambia un CHECK).
    # - `users.can_view_global_workload`: ver la carga del equipo de todos los
    #   espacios sin ser administrador.
    # - Todos los usuarios existentes entran en General (los administradores
    #   como `admin`, el resto como `editor`): nadie pierde lo que veía.
    14: [
        lambda conn: _add_column(conn, "workspaces", "slug", "TEXT"),
        lambda conn: _add_column(conn, "workspaces", "icon", "TEXT"),
        lambda conn: _add_column(conn, "workspaces", "settings", "TEXT NOT NULL DEFAULT '{}'"),
        lambda conn: _add_column(
            conn, "users", "can_view_global_workload", "INTEGER NOT NULL DEFAULT 0"
        ),
        """CREATE TABLE IF NOT EXISTS groups (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
)""",
        """CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
)""",
        "CREATE INDEX IF NOT EXISTS group_members_user ON group_members(user_id)",
        """CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'group')),
    subject_id   TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
    PRIMARY KEY (workspace_id, subject_type, subject_id)
)""",
        "CREATE INDEX IF NOT EXISTS workspace_members_subject ON workspace_members(subject_type, subject_id)",
        lambda conn: _rebuild_acl_with_groups(conn),
        lambda conn: _name_general_and_enroll(conn),
    ],
    # v15 (plan H4): el tipo de una base (Proyectos o Tareas) y qué columna hace
    # de qué (`kind:` en el frontmatter de la base). Como `database_closed`, es
    # una copia derivada del fichero: se reescribe al indexar la base.
    15: [
        """CREATE TABLE IF NOT EXISTS database_kinds (
    database_id TEXT PRIMARY KEY REFERENCES databases(id) ON DELETE CASCADE,
    kind_json   TEXT NOT NULL
)""",
    ],
    # v16 (plan H6): avisos y suscripciones por página. Sin claves foráneas a
    # `pages` a propósito (como `acl`): un reindexado vacía `pages` y una
    # cascada se llevaría los avisos. `dedupe_key` evita el mismo aviso dos veces.
    16: [
        """CREATE TABLE IF NOT EXISTS notifications (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    workspace_id TEXT,
    page_id      TEXT,
    type         TEXT NOT NULL CHECK (type IN ('mention', 'task_assigned', 'deadline_soon', 'deadline_overdue', 'page_update')),
    message      TEXT NOT NULL,
    is_read      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    dedupe_key   TEXT
)""",
        """CREATE TABLE IF NOT EXISTS page_subscriptions (
    user_id TEXT NOT NULL,
    page_id TEXT NOT NULL,
    mode    TEXT NOT NULL CHECK (mode IN ('all', 'mentions_only', 'muted')),
    PRIMARY KEY (user_id, page_id)
)""",
        "CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id, is_read, created_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS notifications_dedupe ON notifications(user_id, dedupe_key) WHERE dedupe_key IS NOT NULL",
    ],
    # v17 (plan H8): registro de auditoría (`app.audit`). Sin claves foráneas:
    # un registro sobrevive a lo que registra (un espacio o una página borrados).
    17: [
        """CREATE TABLE IF NOT EXISTS audit_logs (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    actor_id    TEXT,
    actor_name  TEXT,
    workspace_id TEXT,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    target_name TEXT,
    metadata    TEXT
)""",
        "CREATE INDEX IF NOT EXISTS audit_logs_ts ON audit_logs(ts)",
        "CREATE INDEX IF NOT EXISTS audit_logs_action ON audit_logs(action, ts)",
    ],
    # v18 (plan H9): webhooks salientes por espacio y su cola de entregas
    # (`app.webhooks`). El secreto firma cada envío y no se vuelve a leer.
    18: [
        """CREATE TABLE IF NOT EXISTS workspace_webhooks (
    workspace_id TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    secret       TEXT NOT NULL,
    events       TEXT NOT NULL DEFAULT '[]',
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
)""",
        """CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    event        TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_attempt TEXT NOT NULL,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
    dedupe_key   TEXT
)""",
        "CREATE INDEX IF NOT EXISTS webhook_deliveries_pending ON webhook_deliveries(status, next_attempt)",
        "CREATE UNIQUE INDEX IF NOT EXISTS webhook_deliveries_dedupe ON webhook_deliveries(workspace_id, dedupe_key) WHERE dedupe_key IS NOT NULL",
    ],
    # v19: el orden que cada quien da a las páginas del panel izquierdo al
    # arrastrarlas. Sin fila, la página va después de las ordenadas, por título.
    19: [
        """CREATE TABLE IF NOT EXISTS page_order (
    page_id  TEXT PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
    position INTEGER NOT NULL
)""",
    ],
    # v20: quién crea y edita cada página, para la «Actividad reciente» del
    # inicio (`app.activity`). Ediciones seguidas de la misma persona en la
    # misma página se juntan en una fila. Sin claves foráneas: se poda sola.
    20: [
        """CREATE TABLE IF NOT EXISTS page_activity (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    action  TEXT NOT NULL CHECK (action IN ('created', 'edited')),
    ts      TEXT NOT NULL,
    edits   INTEGER NOT NULL DEFAULT 1
)""",
        "CREATE INDEX IF NOT EXISTS page_activity_ts ON page_activity(ts)",
        "CREATE INDEX IF NOT EXISTS page_activity_page_user ON page_activity(page_id, user_id, ts)",
    ],
    # v21: las páginas que cada quien fija arriba del panel izquierdo
    # («Fijadas»), en el orden que les dé. Se van solas al borrar la página.
    21: [
        """CREATE TABLE IF NOT EXISTS page_pins (
    user_id  TEXT NOT NULL,
    page_id  TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (user_id, page_id)
)""",
    ],
    # La jornada de cada persona, su dedicación a cada espacio, los festivos
    # (comunes y de cada espacio, a mano o importados) y las ausencias: lo que
    # decide cuántas horas tiene cada persona cada día (plan de la carga, C1).
    22: [
        """CREATE TABLE IF NOT EXISTS work_schedules (
    user_id    TEXT PRIMARY KEY,
    hours_json TEXT NOT NULL
)""",
        """CREATE TABLE IF NOT EXISTS workspace_dedication (
    user_id      TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    percent      REAL NOT NULL,
    PRIMARY KEY (user_id, workspace_id)
)""",
        """CREATE TABLE IF NOT EXISTS holiday_sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    url          TEXT,
    params_json  TEXT NOT NULL DEFAULT '{}',
    synced_at    TEXT,
    error        TEXT
)""",
        """CREATE TABLE IF NOT EXISTS holidays (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    day          TEXT NOT NULL,
    name         TEXT NOT NULL,
    source_id    INTEGER REFERENCES holiday_sources(id) ON DELETE CASCADE
)""",
        "CREATE INDEX IF NOT EXISTS holidays_day ON holidays(day)",
        """CREATE TABLE IF NOT EXISTS absences (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    start      TEXT NOT NULL,
    end        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    hours      REAL,
    note       TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    created_at TEXT NOT NULL
)""",
        "CREATE INDEX IF NOT EXISTS absences_user ON absences(user_id, start)",
    ],
    # v23 (plan de roles, R2): los permisos que se conceden a una persona o a
    # un grupo, en todos los espacios (`workspace_id` NULL) o en uno. Los
    # administradores que había reciben todos de forma explícita, y quien
    # tenía `can_view_global_workload`, «Ver carga» en todos (`app.grants`).
    23: [
        GRANTS_SQL,
        GRANTS_INDEX_SQL,
        "CREATE INDEX IF NOT EXISTS grants_subject ON grants(subject_type, subject_id)",
        lambda conn: _migrate_grants(conn),
    ],
    # v24: el chat del equipo (ver `CHAT_SQL`).
    24: [lambda conn: conn.executescript(CHAT_SQL)],
    # v25 (chat, CH3): un aviso puede ser una mención en el chat
    # (`chat_mention`) y llevar a su conversación (`conversation_id`). SQLite no
    # cambia un CHECK: se rehace la tabla, conservando todo.
    25: [lambda conn: _rebuild_notifications_for_chat(conn)],
    # v26 (chat): editar y borrar dejan rastro; las versiones anteriores van a
    # `chat_message_edits`.
    26: [lambda conn: conn.executescript(CHAT_EDITS_SQL)],
    # v27: invitaciones y enlaces de contraseña nueva.
    27: [lambda conn: conn.executescript(ACCESS_LINKS_SQL)],
}


def _migrate_grants(conn: sqlite3.Connection) -> None:
    from app import grants  # import tardío: `grants` no importa este módulo

    grants.migrate_from_admins(conn)


NOTIFICATIONS_SQL = """CREATE TABLE notifications (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    workspace_id    TEXT,
    page_id         TEXT,
    type            TEXT NOT NULL CHECK (type IN ('mention', 'task_assigned', 'deadline_soon', 'deadline_overdue', 'page_update', 'chat_mention')),
    message         TEXT NOT NULL,
    is_read         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    dedupe_key      TEXT,
    conversation_id TEXT
)"""


def _rebuild_notifications_for_chat(conn: sqlite3.Connection) -> None:
    """Migración 25: `notifications` admite `chat_mention` y `conversation_id`."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'notifications'").fetchone()
    if sql is None or "chat_mention" in sql[0]:
        return
    conn.execute("ALTER TABLE notifications RENAME TO notifications_old")
    conn.execute("DROP INDEX IF EXISTS notifications_user")
    conn.execute("DROP INDEX IF EXISTS notifications_dedupe")
    conn.execute(NOTIFICATIONS_SQL)
    conn.execute(
        "INSERT INTO notifications (id, user_id, workspace_id, page_id, type, message, is_read, created_at, dedupe_key)"
        " SELECT id, user_id, workspace_id, page_id, type, message, is_read, created_at, dedupe_key FROM notifications_old"
    )
    conn.execute("DROP TABLE notifications_old")
    conn.execute("CREATE INDEX notifications_user ON notifications(user_id, is_read, created_at)")
    conn.execute(
        "CREATE UNIQUE INDEX notifications_dedupe ON notifications(user_id, dedupe_key) WHERE dedupe_key IS NOT NULL"
    )


def _rebuild_acl_with_groups(conn: sqlite3.Connection) -> None:
    """Migración 14: `acl.subject_type` admite `group`."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'acl'").fetchone()
    if sql is None or "'group'" in sql[0]:
        return
    conn.execute("ALTER TABLE acl RENAME TO acl_old")
    conn.execute("DROP INDEX IF EXISTS acl_subject")
    conn.execute(
        """CREATE TABLE acl (
    id            TEXT PRIMARY KEY,
    subject_type  TEXT NOT NULL CHECK (subject_type IN ('user', 'workspace', 'group')),
    subject_id    TEXT NOT NULL,
    resource_type TEXT NOT NULL CHECK (resource_type IN ('page', 'database')),
    resource_id   TEXT NOT NULL,
    level         TEXT NOT NULL CHECK (level IN ('view', 'edit', 'owner')),
    UNIQUE (resource_id, subject_type, subject_id)
)"""
    )
    conn.execute(
        "INSERT INTO acl (id, subject_type, subject_id, resource_type, resource_id, level)"
        " SELECT id, subject_type, subject_id, resource_type, resource_id, level FROM acl_old"
    )
    conn.execute("DROP TABLE acl_old")
    conn.execute("CREATE INDEX acl_subject ON acl(subject_type, subject_id)")


def _name_general_and_enroll(conn: sqlite3.Connection) -> None:
    """Migración 14: el espacio único se llama General y todos están en él."""
    conn.execute("UPDATE workspaces SET name = 'General' WHERE name = 'default'")
    conn.execute(
        "UPDATE workspaces SET slug = 'general' WHERE slug IS NULL AND id = ?",
        (DEFAULT_WORKSPACE_ID,),
    )
    row = conn.execute("SELECT id FROM workspaces ORDER BY created_at LIMIT 1").fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO workspace_members (workspace_id, subject_type, subject_id, role)"
        " SELECT ?, 'user', id, CASE WHEN is_admin = 1 THEN 'admin' ELSE 'editor' END FROM users",
        (row[0],),
    )


def _fill_title_keys(conn: sqlite3.Connection) -> None:
    """Migración 12: rellena `title_key` de lo que ya había (en el almacén:
    solo él escribe en `pages`, lo comprueba `scripts/verify_p2.py`)."""
    from app.vault import store  # import tardío: el almacén importa este módulo

    store.fill_title_keys(conn)


def _share_existing_roots(conn: sqlite3.Connection) -> None:
    """Migración 11: lo que ya existía queda compartido con el espacio."""
    from app import acl  # import tardío: `acl` usa este módulo

    if conn.execute("SELECT 1 FROM pages LIMIT 1").fetchone() is None:
        return
    acl.share_roots_with_workspace(conn)
    acl.refresh_sources(conn)


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """`ALTER TABLE ... ADD COLUMN` que no falla si la columna ya existe."""
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    if column not in cols:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {column} {decl}')

SCHEMA_SQL = """
-- `slug`, `icon` y `settings` son de la v14 (plan H1): ver la migración 14.
CREATE TABLE workspaces (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    slug       TEXT,
    icon       TEXT,
    settings   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE pages (
    id                TEXT PRIMARY KEY,
    workspace_id      TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    parent_id         TEXT REFERENCES pages(id) ON DELETE CASCADE,
    type              TEXT NOT NULL,
    title             TEXT NOT NULL,
    icon              TEXT,
    description       TEXT,
    created_by        TEXT,
    header_image_path TEXT,
    body              TEXT NOT NULL DEFAULT '',
    path              TEXT UNIQUE,
    mtime             REAL,
    size              INTEGER,
    content_hash      TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    -- El frontmatter tal cual (v9): ver la migración 9. Al final, para que un
    -- índice migrado (ALTER TABLE añade al final) y uno nuevo tengan las
    -- columnas en el mismo orden.
    frontmatter       TEXT,
    -- Cuántas veces se ha escrito (v10, ADR-0009 D3): `If-Match` y la
    -- colaboración. No es contenido: no está en el `.md` ni en la huella.
    version           INTEGER NOT NULL DEFAULT 1,
    -- De qué página sale su permiso (v11): ver la migración 11 y `app.acl`.
    acl_source        TEXT,
    -- El título plegado para resolver enlaces (v12): ver la migración 12.
    title_key         TEXT
);
CREATE INDEX pages_acl_source ON pages(acl_source);
CREATE INDEX pages_title_key ON pages(workspace_id, title_key);

-- Permisos por nota (v11, 3.0 hito 3). Ver la migración 11.
CREATE TABLE acl (
    id            TEXT PRIMARY KEY,
    subject_type  TEXT NOT NULL CHECK (subject_type IN ('user', 'workspace', 'group')),
    subject_id    TEXT NOT NULL,
    resource_type TEXT NOT NULL CHECK (resource_type IN ('page', 'database')),
    resource_id   TEXT NOT NULL,
    level         TEXT NOT NULL CHECK (level IN ('view', 'edit', 'owner')),
    UNIQUE (resource_id, subject_type, subject_id)
);
CREATE INDEX acl_subject ON acl(subject_type, subject_id);
CREATE TABLE acl_roots (
    resource_id   TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL CHECK (resource_type IN ('page', 'database'))
);

-- El vault, dentro de la base (v10, ADR-0005): cada `.md` con su texto entero.
-- Ver la migración 10.
CREATE TABLE documents (
    path  TEXT PRIMARY KEY,
    text  TEXT NOT NULL,
    mtime REAL NOT NULL,
    size  INTEGER NOT NULL
);
CREATE INDEX pages_parent ON pages(parent_id);
CREATE INDEX pages_title ON pages(title COLLATE NOCASE);
-- `GET /pages` ordena por `created_at DESC, rowid DESC` y el panel por
-- `updated_at DESC ... LIMIT 8`: sin índice las dos hacían SCAN + TEMP
-- B-TREE sobre `pages`. Medido con 12.200 páginas (vault de prueba): la
-- lista baja de 173,6 ms a 17,4 ms (-90 %) y las recientes de 2,4 ms a
-- 0,02 ms (-99 %); el coste son +0,05 ms por INSERT en `pages`
-- (0,27 -> 0,32 ms, disparadores FTS incluidos).
CREATE INDEX pages_created ON pages(created_at);
CREATE INDEX pages_updated ON pages(updated_at);

CREATE TABLE databases (
    id          TEXT PRIMARY KEY,
    page_id     TEXT NOT NULL UNIQUE REFERENCES pages(id) ON DELETE CASCADE,
    schema_json TEXT NOT NULL
);

-- Qué cuenta como fila cerrada (v7): la regla que declara el frontmatter de la
-- base (`closed: {property, values}`), o nada si la base no declara estado. Los
-- marcadores `done` viejos no se copian: se derivan del `schema` al leer.
CREATE TABLE database_closed (
    database_id TEXT PRIMARY KEY REFERENCES databases(id) ON DELETE CASCADE,
    rule_json   TEXT NOT NULL
);

-- Plantillas de fila de la base (v8): nombre, valores por defecto de sus
-- propiedades y cuerpo inicial. Viven en el frontmatter (`row_templates:`) y
-- aquí solo se copian para leerlas con la base; sin fila, la base no tiene.
CREATE TABLE database_row_templates (
    database_id  TEXT PRIMARY KEY REFERENCES databases(id) ON DELETE CASCADE,
    templates_json TEXT NOT NULL
);

-- Nombre visible y visibilidad de la columna del título (v6). El título no
-- es una propiedad de la fila: su clave es `title`, reservada, y su valor vive
-- en el frontmatter de cada fila. Aquí solo se guarda cómo se llama la columna
-- y si la tabla la enseña; sin fila, «Título» y visible.
CREATE TABLE database_titles (
    database_id TEXT PRIMARY KEY REFERENCES databases(id) ON DELETE CASCADE,
    name        TEXT,
    hidden      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE views (
    id          TEXT PRIMARY KEY,
    database_id TEXT NOT NULL REFERENCES databases(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX views_database ON views(database_id);

CREATE TABLE rows (
    id              TEXT PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
    database_id     TEXT NOT NULL REFERENCES databases(id) ON DELETE CASCADE,
    properties_json TEXT NOT NULL DEFAULT '{}',
    path            TEXT
);
CREATE INDEX rows_database ON rows(database_id);

CREATE TABLE links (
    id           TEXT PRIMARY KEY,
    from_id      TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    to_id        TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    target_title TEXT NOT NULL,
    UNIQUE (from_id, to_id, kind, target_title)
);
CREATE INDEX links_from ON links(from_id);
CREATE INDEX links_to ON links(to_id);

CREATE VIRTUAL TABLE pages_fts USING fts5(
    title,
    body,
    content = 'pages',
    content_rowid = 'rowid',
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TRIGGER pages_fts_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;
CREATE TRIGGER pages_fts_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, body)
    VALUES ('delete', old.rowid, old.title, old.body);
END;
CREATE TRIGGER pages_fts_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, body)
    VALUES ('delete', old.rowid, old.title, old.body);
    INSERT INTO pages_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;

-- Metadatos del índice, no del vault: `last_reindex_at` (P1.2b). Fuera de
-- `TABLES` y de la huella de contenido a propósito, que si no dos reindexados
-- del mismo vault darían huellas distintas.
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Usuarios y sesiones (P11a). Ver la migración 4 de `MIGRATIONS`: el mismo
-- SQL, para que un fichero creado de cero y uno migrado acaben idénticos.
CREATE TABLE users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    color         TEXT,
    created_at    TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    avatar        TEXT,
    can_view_global_workload INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT
);
CREATE INDEX sessions_user ON sessions(user_id);
CREATE INDEX sessions_expires ON sessions(expires_at);

-- Tokens personales de captura (P13). Ver la migración 5: el mismo SQL, para
-- que un fichero creado de cero y uno migrado acaben idénticos.
CREATE TABLE capture_tokens (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    last_used_at TEXT
);
CREATE INDEX capture_tokens_user ON capture_tokens(user_id);

-- Grupos y pertenencia a espacios (v14, plan H1). Ver la migración 14.
CREATE TABLE groups (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE TABLE group_members (
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX group_members_user ON group_members(user_id);
CREATE TABLE workspace_members (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'group')),
    subject_id   TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
    PRIMARY KEY (workspace_id, subject_type, subject_id)
);
CREATE INDEX workspace_members_subject ON workspace_members(subject_type, subject_id);

-- El tipo de una base (v15, plan H4). Ver la migración 15.
CREATE TABLE database_kinds (
    database_id TEXT PRIMARY KEY REFERENCES databases(id) ON DELETE CASCADE,
    kind_json   TEXT NOT NULL
);

-- Avisos y suscripciones por página (v16, plan H6). Ver la migración 16.
CREATE TABLE notifications (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    workspace_id    TEXT,
    page_id         TEXT,
    type            TEXT NOT NULL CHECK (type IN ('mention', 'task_assigned', 'deadline_soon', 'deadline_overdue', 'page_update', 'chat_mention')),
    message         TEXT NOT NULL,
    is_read         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    dedupe_key      TEXT,
    conversation_id TEXT
);
CREATE TABLE page_subscriptions (
    user_id TEXT NOT NULL,
    page_id TEXT NOT NULL,
    mode    TEXT NOT NULL CHECK (mode IN ('all', 'mentions_only', 'muted')),
    PRIMARY KEY (user_id, page_id)
);
CREATE INDEX notifications_user ON notifications(user_id, is_read, created_at);
CREATE UNIQUE INDEX notifications_dedupe ON notifications(user_id, dedupe_key) WHERE dedupe_key IS NOT NULL;

-- Registro de auditoría (v17, plan H8). Ver la migración 17.
CREATE TABLE audit_logs (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    actor_id    TEXT,
    actor_name  TEXT,
    workspace_id TEXT,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    target_name TEXT,
    metadata    TEXT
);
CREATE INDEX audit_logs_ts ON audit_logs(ts);
CREATE INDEX audit_logs_action ON audit_logs(action, ts);

-- Webhooks salientes por espacio y su cola (v18, plan H9). Ver la migración 18.
CREATE TABLE workspace_webhooks (
    workspace_id TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    secret       TEXT NOT NULL,
    events       TEXT NOT NULL DEFAULT '[]',
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);
CREATE TABLE webhook_deliveries (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    event        TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_attempt TEXT NOT NULL,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
    dedupe_key   TEXT
);
CREATE INDEX webhook_deliveries_pending ON webhook_deliveries(status, next_attempt);
CREATE UNIQUE INDEX webhook_deliveries_dedupe ON webhook_deliveries(workspace_id, dedupe_key) WHERE dedupe_key IS NOT NULL;

-- El orden de las páginas en el panel izquierdo (v19). Ver la migración 19.
CREATE TABLE page_order (
    page_id  TEXT PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
    position INTEGER NOT NULL
);

-- Quién crea y edita cada página (v20). Ver la migración 20 y `app.activity`.
CREATE TABLE page_activity (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    action  TEXT NOT NULL CHECK (action IN ('created', 'edited')),
    ts      TEXT NOT NULL,
    edits   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX page_activity_ts ON page_activity(ts);
CREATE INDEX page_activity_page_user ON page_activity(page_id, user_id, ts);

-- Las páginas fijadas de cada quien (v21). Ver la migración 21.
CREATE TABLE page_pins (
    user_id  TEXT NOT NULL,
    page_id  TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (user_id, page_id)
);

-- Jornada, dedicación, festivos y ausencias (v22). Ver la migración 22.
CREATE TABLE work_schedules (
    user_id    TEXT PRIMARY KEY,
    hours_json TEXT NOT NULL
);

CREATE TABLE workspace_dedication (
    user_id      TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    percent      REAL NOT NULL,
    PRIMARY KEY (user_id, workspace_id)
);

CREATE TABLE holiday_sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    url          TEXT,
    params_json  TEXT NOT NULL DEFAULT '{}',
    synced_at    TEXT,
    error        TEXT
);

CREATE TABLE holidays (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    day          TEXT NOT NULL,
    name         TEXT NOT NULL,
    source_id    INTEGER REFERENCES holiday_sources(id) ON DELETE CASCADE
);

CREATE INDEX holidays_day ON holidays(day);

CREATE TABLE absences (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    start      TEXT NOT NULL,
    end        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    hours      REAL,
    note       TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX absences_user ON absences(user_id, start);

-- Permisos que se conceden (v23, plan de roles R2). Ver la migración 23.
CREATE TABLE grants (
    id           TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'group')),
    subject_id   TEXT NOT NULL,
    capability   TEXT NOT NULL,
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    granted_by   TEXT,
    granted_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX grants_unique ON grants(capability, subject_type, subject_id, COALESCE(workspace_id, ''));
CREATE INDEX grants_subject ON grants(subject_type, subject_id);

-- Chat del equipo (v24). Ver la migración 24.
CREATE TABLE chat_conversations (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('workspace', 'direct')),
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    title        TEXT,
    created_at   TEXT NOT NULL,
    created_by   TEXT
);
CREATE UNIQUE INDEX chat_conversations_workspace ON chat_conversations(workspace_id) WHERE kind = 'workspace';
CREATE TABLE chat_members (
    conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    joined_at       TEXT NOT NULL,
    PRIMARY KEY (conversation_id, user_id)
);
CREATE INDEX chat_members_user ON chat_members(user_id);
CREATE TABLE chat_messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    user_id          TEXT NOT NULL,
    body             TEXT NOT NULL DEFAULT '',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    edited_at        TEXT,
    deleted          INTEGER NOT NULL DEFAULT 0,
    task_id          TEXT
);
CREATE INDEX chat_messages_conv ON chat_messages(conversation_id, id);
CREATE TABLE chat_reads (
    conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    last_read_id    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_id, user_id)
);
CREATE VIRTUAL TABLE chat_fts USING fts5(
    body, content = 'chat_messages', content_rowid = 'id', tokenize = "unicode61 remove_diacritics 2"
);
CREATE TRIGGER chat_fts_ai AFTER INSERT ON chat_messages BEGIN
    INSERT INTO chat_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER chat_fts_ad AFTER DELETE ON chat_messages BEGIN
    INSERT INTO chat_fts(chat_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER chat_fts_au AFTER UPDATE OF body ON chat_messages BEGIN
    INSERT INTO chat_fts(chat_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO chat_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TABLE chat_message_edits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    replaced_at TEXT NOT NULL
);
CREATE INDEX chat_message_edits_msg ON chat_message_edits(message_id);

CREATE TABLE access_links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL CHECK (kind IN ('invite', 'reset')),
    user_id      TEXT,
    workspace_id TEXT,
    role         TEXT,
    display_name TEXT,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    used_at      TEXT
);

PRAGMA user_version = 27;
"""

TABLES = ("workspaces", "pages", "databases", "views", "rows", "links")


class IndexSchemaError(RuntimeError):
    """El fichero del índice es de una versión o una forma que este código no
    sabe usar. Se para en claro en vez de escribir sobre un esquema ajeno."""


MIN_SQLITE_VERSION = (3, 27, 0)
"""Motor mínimo. `remove_diacritics 2` del tokenizer de FTS5 (lo más nuevo que
usa el esquema) existe desde esta versión; por debajo, `pages_fts` ni siquiera
se puede crear como está definida, y el fallo saldría con un error de SQL sin
contexto."""

# Columnas sin las que una tabla `pages` no puede ser la de este esquema: si
# falta alguna, el fichero es de otra cosa y no se migra a ciegas.
PAGES_REQUIRED_COLUMNS = frozenset(
    {"id", "workspace_id", "parent_id", "type", "title", "body",
     "created_at", "updated_at"}
)


def check_sqlite_version() -> None:
    """Falla en claro si el motor es más viejo que `MIN_SQLITE_VERSION`."""
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        actual = ".".join(str(n) for n in sqlite3.sqlite_version_info)
        minimo = ".".join(str(n) for n in MIN_SQLITE_VERSION)
        raise IndexSchemaError(
            f"este SQLite es la {actual} y el índice necesita al menos la "
            f"{minimo} (`remove_diacritics 2` de FTS5): cambia la imagen o el "
            "intérprete antes de arrancar"
        )


@dataclass(frozen=True)
class IndexInfo:
    path: str
    version: int
    recreated: bool = False
    reason: str = ""


def _norm_sql(sql: str | None) -> str:
    return re.sub(r"\s+", " ", (sql or "").strip()).rstrip(";").strip()


def _expected_fts_sql() -> str:
    """SQL con el que SQLite crea `pages_fts` a partir de SCHEMA_SQL.

    Se genera ejecutando la MISMA sentencia en memoria, para no escribir a mano
    el texto esperado ni depender de cómo reformatee SQLite.
    """
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(SCHEMA_SQL)
        row = mem.execute("SELECT sql FROM sqlite_master WHERE name = 'pages_fts'").fetchone()
        return _norm_sql(row[0])
    finally:
        mem.close()


EXPECTED_FTS_SQL = _expected_fts_sql()


def connect(path: str) -> sqlite3.Connection:
    """Conexión nueva con los PRAGMAs que usa la app.

    `check_same_thread=False` es deliberado: cada petición tiene su propia
    conexión, nunca se comparte ni se usa desde dos hilos a la vez, pero
    FastAPI puede resolver la dependencia en un hilo del pool y ejecutar una
    ruta `async` en el bucle de eventos. Sin esto, subir una cabecera (la única
    ruta `async`) reventaba con "SQLite objects created in a thread...".
    """
    check_sqlite_version()
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Las cascadas encadenadas (borrar una página -> sus hijos -> sus nietos)
    # solo se propagan con esto activado; sin él, SQLite corta en el primer
    # nivel y deja nietos sueltos en el árbol.
    conn.execute("PRAGMA recursive_triggers = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def connect_ro(path: str) -> sqlite3.Connection:
    """Conexión de solo lectura, para inspeccionar un fichero sin tocarlo.

    `connect()` activa WAL, y eso reescribe la cabecera del fichero: con un
    `INDEX_PATH` equivocado, abrirlo "para mirar" ya lo modificaba. Con
    `mode=ro` no se escribe nada; a lo sumo SQLite crea los `-wal`/`-shm` de un
    fichero que ya estuviera en WAL, que es lo que necesita para leerlo.
    """
    check_sqlite_version()
    uri = Path(os.path.abspath(path)).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    """Tablas del fichero, sin las internas de SQLite (`sqlite_*`)."""
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not r[0].startswith("sqlite_")
    }


SQLITE_HEADER = b"SQLite format 3\x00"


def _is_corrupt_file(path: str) -> bool:
    """¿El fichero existe con contenido pero no es una base de datos SQLite?

    Se mira la cabecera real, sin abrirlo con sqlite3: abrirlo con los PRAGMAs
    de la app puede inicializarlo y perder el original. Un fichero de 0 bytes
    no es corrupción, es un índice que aún no se ha creado.
    """
    try:
        size = os.path.getsize(path)
        if size == 0:
            return False
        with open(path, "rb") as fh:
            return fh.read(len(SQLITE_HEADER)) != SQLITE_HEADER
    except OSError:
        return False


def _create_schema(conn: sqlite3.Connection) -> None:
    # `executescript` hace COMMIT implícito: por eso el esquema se crea siempre
    # fuera de cualquier transacción de datos.
    conn.executescript(SCHEMA_SQL)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise IndexSchemaError(
            f"el esquema recién creado declara user_version={version}, "
            f"esperado {SCHEMA_VERSION}"
        )


def _forget_checked(path: str) -> None:
    ident = _identity(path)
    if ident is not None:
        _quick_checked.discard(ident)


def _quarantine(path: str) -> str:
    """Aparta un fichero ilegible en vez de borrarlo. Devuelve la ruta nueva."""
    _forget_checked(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = f"{path}.corrupto-{stamp}"
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = f"{path}.corrupto-{stamp}-{n}"
    os.replace(path, dest)
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass
    return dest


def _create(path: str) -> None:
    """Crea el esquema en un fichero vacío (o en uno recién apartado)."""
    conn = connect(path)
    try:
        _create_schema(conn)
    finally:
        conn.close()


def _inspect_readonly(path: str, quick_check: bool = True) -> int | None:
    """Mira el fichero sin escribirlo: su `user_version`, o None si no hay
    ninguna tabla (fichero SQLite válido pero vacío).

    Lanza `IndexSchemaError` si el fichero tiene tablas que no son de este
    esquema, o si SQLite lo lee pero `quick_check` encuentra daños. En los dos
    casos el fichero se queda exactamente como estaba.
    """
    conn = connect_ro(path)
    try:
        tables = _table_names(conn)
        if not tables:
            return None
        if "pages" not in tables:
            raise IndexSchemaError(
                f"el fichero {path} no es un índice de esta app: tiene tablas "
                f"({', '.join(sorted(tables))}) y ninguna es 'pages'. No se ha "
                "tocado nada; apunta INDEX_PATH al índice o aparta ese fichero "
                "tú mismo."
            )
        columns = {r[1] for r in conn.execute("PRAGMA table_info(pages)")}
        faltan = PAGES_REQUIRED_COLUMNS - columns
        if faltan:
            raise IndexSchemaError(
                f"el fichero {path} tiene una tabla 'pages' que no es la de "
                f"esta app (le faltan columnas: {', '.join(sorted(faltan))}). "
                "No se ha tocado nada; apunta INDEX_PATH al índice o aparta ese "
                "fichero tú mismo."
            )
        check = conn.execute("PRAGMA quick_check").fetchone()[0] if quick_check else "ok"
        if check != "ok":
            raise IndexSchemaError(
                f"el índice {path} no pasa quick_check ({check}). No se toca: "
                "en P1.1 el índice es la única copia, así que restaura una "
                "copia de seguridad o aparta el fichero tú mismo antes de "
                "arrancar."
            )
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


# Ficheros (dispositivo, inodo) que ya pasaron `quick_check` en este proceso.
#
# `ensure_index` vuelve a inspeccionar el índice cada vez que cambia su `mtime`
# o su tamaño, y en WAL eso es casi cada escritura (cada checkpoint toca el
# fichero principal). `quick_check` recorre la base ENTERA: medido con 12.000
# notas, ~40-46 ms por petición tras cada guardado, más de la cuarta parte de
# un PATCH. Desde la 3.0 la base es la verdad y crece (`documents`), así que
# ese coste solo iba a más. Se comprueba una vez por fichero: al arrancar y
# cuando el fichero se sustituye (otro inodo: un restore, un `mv`). El resto de
# la inspección (tablas, columnas, versión) sigue en cada cambio: es barata y
# es la que detecta un esquema que hay que migrar.
_quick_checked: set[tuple[str, int, int]] = set()


def _identity(path: str) -> tuple[str, int, int] | None:
    """(ruta, dispositivo, inodo): con la ruta, un inodo reciclado en otro sitio
    (un temporal borrado) no hereda la comprobación de otro fichero."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (os.path.abspath(path), st.st_dev, st.st_ino)


def _inspect(path: str) -> IndexInfo:
    """Crea, migra, o rechaza el índice. Ver la política en el docstring del
    módulo.

    Todo lo que se puede decidir mirando el fichero se decide en solo lectura;
    solo se abre en escritura cuando está claro que el fichero es nuestro (o
    que no hay nada dentro que perder).
    """
    if not os.path.exists(path):
        _create(path)
        return IndexInfo(path=path, version=SCHEMA_VERSION, recreated=True,
                         reason="no existía")

    if _is_corrupt_file(path):
        # El fichero no empieza con la cabecera de SQLite: se aparta ANTES de
        # abrir una conexión normal, porque `PRAGMA journal_mode = WAL` sobre
        # basura puede reescribir la cabecera e inicializar el fichero, y
        # entonces ya no queda el original para recuperar nada.
        dest = _quarantine(path)
        _create(path)
        return IndexInfo(path=path, version=SCHEMA_VERSION, recreated=True,
                         reason=f"fichero ilegible: no es una base de datos (apartado en {dest})")

    try:
        ident = _identity(path)
        version = _inspect_readonly(path, quick_check=ident not in _quick_checked)
        if ident is not None:
            _quick_checked.add(ident)
    except IndexSchemaError:
        # Esquema ajeno, versión futura o daño que SQLite todavía puede leer:
        # se para en claro y no se toca nada.
        raise
    except sqlite3.DatabaseError as exc:
        # SQLite no puede ni leerlo: corrupción de verdad. Se aparta (no se
        # borra: si el daño fuera parcial, el original sigue ahí).
        try:
            dest = _quarantine(path)
        except OSError:
            os.unlink(path)
            dest = "(borrado, no se pudo apartar)"
        log.warning("índice ilegible (%s); apartado en %s", exc, dest)
        _create(path)
        return IndexInfo(path=path, version=SCHEMA_VERSION, recreated=True,
                         reason=f"fichero ilegible: {exc} (apartado en {dest})")

    if version is None:
        # Fichero SQLite válido y sin ninguna tabla: no hay nada que perder, se
        # le pone el esquema.
        _create(path)
        return IndexInfo(path=path, version=SCHEMA_VERSION, recreated=True,
                         reason="no tenía tablas")

    conn = connect(path)
    try:
        if version > SCHEMA_VERSION:
            raise IndexSchemaError(
                f"el índice está en la versión {version} y este código "
                f"entiende hasta la {SCHEMA_VERSION}: actualiza la app o "
                f"usa otro INDEX_PATH"
            )
        if version < SCHEMA_VERSION:
            # Antes de tocar el esquema, el fichero tal como está (plan H0).
            from app.backup import copy_before_migration

            copy_before_migration(path, version, SCHEMA_VERSION)
            _migrate(conn, version)
            version = SCHEMA_VERSION

        # Toda tabla que una consulta normal necesita va aquí: sin esta
        # comprobación, un índice al que le falta una (p. ej. `database_closed`,
        # la regla de estado de cada base) arranca y luego revienta con un 500
        # en la primera lectura, en vez de decir que el fichero no es el suyo.
        missing = {"pages", "databases", "views", "rows", "links",
                   "workspaces", "meta", "users", "sessions",
                   "database_titles", "database_closed",
                   "database_row_templates"} - _table_names(conn)
        if missing:
            raise IndexSchemaError(
                "al índice le faltan tablas: " + ", ".join(sorted(missing))
            )
        fts = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'pages_fts'"
        ).fetchone()
        if fts is None:
            raise IndexSchemaError(
                "el índice no tiene pages_fts: la búsqueda no funcionaría. "
                "Rehazlo desde el vault o desde el script de migración"
            )
        if _norm_sql(fts[0]) != EXPECTED_FTS_SQL:
            raise IndexSchemaError(
                "la definición de pages_fts no es la vigente (¿cambió el "
                "tokenizer sin subir SCHEMA_VERSION?)"
            )
        return IndexInfo(path=path, version=version)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection, current: int) -> None:
    pending = sorted(v for v in MIGRATIONS if current < v <= SCHEMA_VERSION)
    applied = {current}
    for version in pending:
        for stmt in MIGRATIONS[version]:
            if callable(stmt):
                stmt(conn)
            else:
                conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {version}")
        applied.add(version)
    if SCHEMA_VERSION not in applied:
        raise IndexSchemaError(
            f"el índice está en la versión {current} y no hay migración a la "
            f"{SCHEMA_VERSION}"
        )


_ready: dict[str, tuple[tuple[int, int] | None, IndexInfo]] = {}
_ready_lock = threading.Lock()


def ensure_index(path: str) -> IndexInfo:
    """Deja el índice listo para usarse y cachea el resultado por fichero.

    El cache se invalida si cambian `mtime`/tamaño, así que un índice
    reemplazado o corrompido después del arranque se vuelve a mirar en la
    siguiente petición.
    """
    key = os.path.abspath(path)
    try:
        st = os.stat(key)
        stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        stamp = None

    with _ready_lock:
        cached = _ready.get(key)
        if cached is not None and cached[0] == stamp:
            # El cache dice qué se comprobó, no que este arranque lo haya
            # rehecho: `recreated` describe la llamada, no el fichero.
            return IndexInfo(path=cached[1].path, version=cached[1].version)
        info = _inspect(key)
        st = os.stat(key)
        _ready[key] = ((st.st_mtime_ns, st.st_size), info)
        return info


def open_index(path: str) -> sqlite3.Connection:
    """Conexión lista para una petición (crea/migra el índice si hace falta)."""
    ensure_index(path)
    conn = connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        # El cache por mtime/tamaño puede quedarse corto (en WAL, cambiar
        # `user_version` no toca el fichero principal): la versión se comprueba
        # siempre, y si no cuadra se vuelve a inspeccionar antes de usar nada.
        conn.close()
        with _ready_lock:
            _ready.pop(os.path.abspath(path), None)
        ensure_index(path)
        conn = connect(path)
    return conn


def fts_query(text: str) -> str:
    """Texto libre -> consulta FTS5 segura (el usuario no escribe sintaxis).

    Solo sobreviven caracteres de palabra, así que nada de lo que escriba el
    usuario puede interpretarse como operador de FTS5 (`OR`, `NEAR`, `^`, ...).
    Cada token va entre comillas y con `*` final: `reunion` encuentra `Reunión`
    (el tokenizer quita diacríticos) y también `Reuniones` (prefijo).
    """
    tokens = re.findall(r"[\wáéíóúüñÁÉÍÓÚÜÑ]+", text or "", flags=re.UNICODE)
    return " ".join(f'"{tok}"*' for tok in tokens)


def count_rows(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    counts["pages_fts"] = conn.execute("SELECT count(*) FROM pages_fts").fetchone()[0]
    return counts


# Clave de `meta` con el instante del último reindexado (ISO 8601 UTC). Vive en
# el índice y no en el vault: dice cuándo se leyó el vault, no qué contiene, y
# viaja con el índice al que describe.
META_LAST_REINDEX = "last_reindex_at"
# 3.0, hito 2: `db` cuando el vault de disco ya se volcó a `documents` y la
# verdad es la base. Sin la marca, el primer arranque hace esa migración.
META_STORAGE = "storage"


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _canon_cell(value):
    """Valor estable para la huella: los BLOB (tablas internas de FTS5) en base64."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"blob": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _canon_row(row) -> str:
    return json.dumps([_canon_cell(v) for v in row], ensure_ascii=False)


# Tablas que no entran en la huella de contenido, con su motivo.
DUMP_EXCLUDED = {
    # Fila singleton que existe porque `pages.workspace_id` es NOT NULL: su
    # `created_at` dice cuándo se creó el ÍNDICE, no cuándo se creó el
    # contenido. Al borrar el índice y reconstruirlo cambia por definición, y no
    # hay ningún dato del vault detrás. `pages.workspace_id` sí es estable (uuid5).
    "workspaces",
    # Cuándo se reindexó por última vez: es historia del índice, no contenido
    # del vault. Cambia en cada reindexado por definición, así que incluirlo
    # rompería la idempotencia (`misma huella` al borrar y reconstruir).
    "meta",
    # Usuarios y sesiones (P11a): son credenciales de acceso, no contenido del
    # vault. El vault no los tiene (no se pueden reconstruir desde los `.md`),
    # así que si entraran en la huella, reconstruir el índice daría otra
    # huella y la idempotencia de P1.2 se rompería por algo que no es una nota.
    "users",
    "sessions",
    # Tokens de captura (P13): mismo motivo que `users`/`sessions`, son
    # credenciales de acceso y no contenido del vault.
    "capture_tokens",
    # Permisos (v11): quién ve qué no es contenido del vault ni está en el
    # `.md` (como `users`), así que reconstruir el índice desde un export da
    # la misma huella aunque los permisos no viajen en él.
    "acl",
    "acl_roots",
    # Grupos y miembros de los espacios (v14): como los usuarios, no son
    # contenido del vault y no viajan en el export.
    "groups",
    "group_members",
    "workspace_members",
    # Avisos, suscripciones y auditoría (v16 y v17): estado de la app, no
    # contenido del vault.
    "notifications",
    "page_subscriptions",
    "audit_logs",
    "workspace_webhooks",
    "webhook_deliveries",
    # El orden del panel (v19): preferencia de la app, no está en el `.md`.
    "page_order",
    # La actividad del inicio (v20): historia de la app, no está en el `.md`.
    "page_activity",
    # Las fijadas (v21): preferencia de cada quien, no está en el `.md`.
    "page_pins",
    # Jornada, festivos y ausencias (v22): estado de la app, no está en el `.md`.
    "work_schedules",
    "workspace_dedication",
    "holiday_sources",
    "holidays",
    "absences",
    # Permisos que se conceden (v23): como `acl`, no están en el `.md`.
    "grants",
    # El chat (v24): conversaciones de la app, no están en el `.md`.
    "chat_conversations",
    "chat_members",
    "chat_messages",
    "chat_reads",
    "chat_message_edits",
    # Enlaces de acceso (v27): credenciales de un solo uso, no contenido.
    "access_links",
}


# Columnas que no entran en la huella, con su motivo.
DUMP_EXCLUDED_COLUMNS = {
    # Cuántas veces se ha escrito la página (v10): es historia de la base, no
    # contenido. No está en el `.md`, así que un índice reconstruido desde un
    # export empieza en 1 y la huella tiene que seguir siendo la misma.
    "pages": {"version", "acl_source", "title_key"},
}


def dump_hash(path: str) -> tuple[str, dict[str, int]]:
    """Huella del contenido del índice, tabla a tabla. Devuelve (hash, conteos).

    Entran las tablas con contenido del vault (`pages`, `databases`, `views`,
    `rows`, `links`) y el **contenido** de `pages_fts` (título y cuerpo
    indexados, sin el rowid: es un número interno que depende del orden en que
    se insertaron las filas, no de lo que hay guardado). No entran:

    - `workspaces`: ver `DUMP_EXCLUDED`.
    - las tablas internas de FTS5 (`pages_fts_data`, `pages_fts_idx`, ...): son
      la representación interna del índice de texto, su reparto en segmentos
      depende del historial de escrituras y SQLite no promete que sea igual
      entre dos caminos que guardan lo mismo.

    Así, borrar el índice y reconstruirlo desde el vault da la misma huella que
    el índice que escribió la API. Las filas se ordenan para que la huella no
    dependa del orden físico.
    """
    conn = sqlite3.connect(path)
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            if not r[0].startswith(("pages_fts_", "chat_fts")) and r[0] not in DUMP_EXCLUDED
        ]
        for table in tables:
            skip = DUMP_EXCLUDED_COLUMNS.get(table, set())
            cols = [
                r[1] for r in conn.execute(f'PRAGMA table_info("{table}")') if r[1] not in skip
            ]
            col_sql = ", ".join(f'"{c}"' for c in cols)
            rows = conn.execute(f'SELECT {col_sql} FROM "{table}"').fetchall()
            counts[table] = len(rows)
            for line in sorted(_canon_row(r) for r in rows):
                digest.update(line.encode())
                digest.update(b"\n")
        fts_rows = conn.execute(
            "SELECT title, body FROM pages_fts ORDER BY title, body"
        ).fetchall()
        counts["pages_fts"] = len(fts_rows)
        for line in (_canon_row(r) for r in fts_rows):
            digest.update(line.encode())
            digest.update(b"\n")
    finally:
        conn.close()
    return digest.hexdigest(), counts



