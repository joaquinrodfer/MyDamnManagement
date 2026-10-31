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
SCHEMA_VERSION = 3

# El workspace por defecto tiene id fijo: la app no tiene multi-workspace y, si
# fuera `uuid4()`, borrar el índice y reconstruirlo cambiaría el
# `pages.workspace_id` de cada fila (el ADR exige que la reconstrucción sea
# idéntica). El nombre y la fecha sí son de la fila singleton.
DEFAULT_WORKSPACE_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "mdm:default-workspace"))

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
}

SCHEMA_SQL = """
CREATE TABLE workspaces (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
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
    updated_at        TEXT NOT NULL
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

PRAGMA user_version = 3;
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


def _quarantine(path: str) -> str:
    """Aparta un fichero ilegible en vez de borrarlo. Devuelve la ruta nueva."""
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


def _inspect_readonly(path: str) -> int | None:
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
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
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
        version = _inspect_readonly(path)
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
            _migrate(conn, version)
            version = SCHEMA_VERSION

        missing = {"pages", "databases", "views", "rows", "links",
                   "workspaces", "meta"} - _table_names(conn)
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
            if not r[0].startswith("pages_fts_") and r[0] not in DUMP_EXCLUDED
        ]
        for table in tables:
            rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
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



