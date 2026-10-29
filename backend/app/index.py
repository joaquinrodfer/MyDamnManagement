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
- **Fichero corrupto detectado al arrancar**: `PRAGMA quick_check` sobre un
  fichero que no es una base de datos (o que está partido) lanza
  `sqlite3.DatabaseError`; entonces el fichero se aparta con sufijo
  `.corrupto-<fecha>` y se rehace vacío. No se borra: si el daño fuera parcial,
  el original sigue ahí para recuperar lo que se pueda. Es la única pérdida de
  datos posible y solo ocurre cuando SQLite ya no puede leer el fichero.
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

import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("mdm.index")

# Subir esto OBLIGA a añadir la migración correspondiente en MIGRATIONS.
SCHEMA_VERSION = 1

# Migraciones futuras: {versión_destino: [sql, ...]}. Se aplican en orden.
MIGRATIONS: dict[int, list[str]] = {}

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

PRAGMA user_version = 1;
"""

TABLES = ("workspaces", "pages", "databases", "views", "rows", "links")


class IndexSchemaError(RuntimeError):
    """El fichero del índice es de una versión o una forma que este código no
    sabe usar. Se para en claro en vez de escribir sobre un esquema ajeno."""


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


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
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


def _inspect(path: str) -> IndexInfo:
    """Crea, migra, o aparta el índice. Ver la política en el docstring del
    módulo."""
    if not os.path.exists(path):
        conn = connect(path)
        try:
            _create_schema(conn)
            return IndexInfo(path=path, version=SCHEMA_VERSION, recreated=True,
                             reason="no existía")
        finally:
            conn.close()

    if _is_corrupt_file(path):
        # El fichero no empieza con la cabecera de SQLite: se aparta ANTES de
        # abrir una conexión normal, porque `PRAGMA journal_mode = WAL` sobre
        # basura puede reescribir la cabecera e inicializar el fichero, y
        # entonces ya no queda el original para recuperar nada.
        dest = _quarantine(path)
        conn = connect(path)
        try:
            _create_schema(conn)
        finally:
            conn.close()
        return IndexInfo(path=path, version=SCHEMA_VERSION, recreated=True,
                         reason=f"fichero ilegible: no es una base de datos (apartado en {dest})")

    try:
        conn = connect(path)
        try:
            tables = _table_names(conn)
            if "pages" in tables:
                check = conn.execute("PRAGMA quick_check").fetchone()[0]
                if check != "ok":
                    raise IndexSchemaError(f"quick_check del índice: {check}")
            else:
                # Fichero vacío o ajeno pero legible: se le pone el esquema.
                _create_schema(conn)
                return IndexInfo(path=path, version=SCHEMA_VERSION, recreated=True,
                                 reason="no tenía tablas")

            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise IndexSchemaError(
                    f"el índice está en la versión {version} y este código "
                    f"entiende hasta la {SCHEMA_VERSION}: actualiza la app o "
                    f"usa otro INDEX_PATH"
                )
            if version < SCHEMA_VERSION:
                _migrate(conn, version)

            missing = {"pages", "databases", "views", "rows", "links",
                       "workspaces"} - _table_names(conn)
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
    except IndexSchemaError:
        raise
    except sqlite3.DatabaseError as exc:
        # No es una base de datos, o está partida: apartar y rehacer.
        try:
            dest = _quarantine(path)
        except OSError:
            os.unlink(path)
            dest = "(borrado, no se pudo apartar)"
        log.warning("índice ilegible (%s); apartado en %s", exc, dest)
        conn = connect(path)
        try:
            _create_schema(conn)
        finally:
            conn.close()
        return IndexInfo(path=path, version=SCHEMA_VERSION, recreated=True,
                         reason=f"fichero ilegible: {exc} (apartado en {dest})")


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
