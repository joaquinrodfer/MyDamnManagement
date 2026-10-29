#!/usr/bin/env python3
"""Migración de una vez: Postgres -> índice SQLite.

Lee los datos actuales de la base de datos de Postgres (solo SELECT, no escribe
nada allí) y los copia al índice SQLite que usa la app desde P1.1.

- **Idempotente**: cada fila se escribe por su `id` con `INSERT ... ON CONFLICT
  DO UPDATE`, así que ejecutarlo dos veces deja el índice igual y la segunda vez
  lo dice con contadores (0 nuevas, N actualizadas).
- **Con contadores**: por tabla (workspaces, pages, rows, databases, views,
  links) y un resumen final, más las filas omitidas y el motivo.
- **No depende del orden de lectura**: `SELECT * FROM pages` puede devolver un
  hijo antes que su padre (basta con que el padre se editara después: su tupla
  se va al final del heap). Las claves foráneas se difieren al `COMMIT`
  (`PRAGMA defer_foreign_keys`) y, antes de confirmar, se comprueban con
  `PRAGMA foreign_key_check`: una FK rota sale como error de datos (código 3)
  nombrando tabla y fila, no como traceback.
- **Filas huérfanas de database**: una fila con `database_id` NULL (el esquema
  viejo lo permitía: `ON DELETE SET NULL`) no se puede colocar en el índice. Se
  omite, se cuenta en `omitidas` y se avisa con su id; su página sí se migra.
- **Sin Postgres, no inventa nada**: si falta `psycopg2`, si no hay URL o si la
  conexión falla, sale con código 2 y un mensaje claro. No escribe en el índice.
- Postgres queda intacto como respaldo hasta que valides la migración.

Uso:

    pip install -r backend/requirements-migration.txt   # psycopg2, opcional
    python backend/scripts/migrate_postgres_to_sqlite.py \\
        --database-url postgresql://usuario:clave@localhost:5432/mydamnmanagement \\
        --index-path /data/index.sqlite [--dry-run]

Variables de entorno aceptadas si no se pasan argumentos:
`MDM_DATABASE_URL` o `DATABASE_URL`, e `INDEX_PATH`.

Qué NO migra (y por qué):
- `tags` / `page_tags`: la API nunca escribió en ellas (no hay endpoint de
  etiquetas). Migrarlas daría la falsa impresión de que están soportadas.
- `attachments`: la API guarda la imagen de cabecera como fichero y su ruta en
  `pages.header_image_path`; la tabla existe en el modelo antiguo pero ningún
  endpoint la usaba.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import index as indice  # noqa: E402

EXIT_OK = 0
EXIT_SIN_POSTGRES = 2
EXIT_ERROR_DATOS = 3

PAGE_COLUMNS = (
    "id, workspace_id, parent_id, type, title, icon, description, created_by,"
    " header_image_path, body, path, mtime, size, content_hash, created_at, updated_at"
)

# Tablas del índice, en el orden en que se escriben: `databases` y `rows`
# dependen de `pages`, `rows` de `databases` y `links` de `pages`. La única
# dependencia que puede ir "hacia atrás" al leer de Postgres es `pages.parent_id`
# (un hijo puede llegar antes que su padre); es lo que resuelven
# `defer_foreign_keys` y `foreign_key_check`.
TABLAS = ("workspaces", "pages", "rows", "databases", "views", "links")


class MigrationError(RuntimeError):
    """Fallo esperable de la migración (sin Postgres, tabla ausente...)."""


@dataclass
class Dump:
    """Lo leído de Postgres, ya normalizado a las columnas del índice."""

    workspaces: list[dict[str, Any]] = field(default_factory=list)
    pages: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    databases: list[dict[str, Any]] = field(default_factory=list)
    views: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Counters:
    nuevas: dict[str, int] = field(default_factory=dict)
    actualizadas: dict[str, int] = field(default_factory=dict)
    omitidas: dict[str, int] = field(default_factory=dict)
    avisos: list[str] = field(default_factory=list)

    def add(self, tabla: str, created: bool) -> None:
        destino = self.nuevas if created else self.actualizadas
        destino[tabla] = destino.get(tabla, 0) + 1

    def omitir(self, tabla: str, aviso: str) -> None:
        """Cuenta una fila que no se puede migrar y guarda el porqué."""
        self.omitidas[tabla] = self.omitidas.get(tabla, 0) + 1
        self.avisos.append(aviso)

    def resumen(self) -> str:
        lineas = []
        for tabla in TABLAS:
            lineas.append(
                f"  {tabla:10s} nuevas: {self.nuevas.get(tabla, 0):5d}   "
                f"actualizadas: {self.actualizadas.get(tabla, 0):5d}"
            )
        total_nuevas = sum(self.nuevas.values())
        total_actualizadas = sum(self.actualizadas.values())
        lineas.append(f"  {'TOTAL':10s} nuevas: {total_nuevas:5d}   actualizadas: {total_actualizadas:5d}")
        if self.omitidas:
            detalle = ", ".join(f"{t}: {n}" for t, n in sorted(self.omitidas.items()))
            lineas.append(f"  {'OMITIDAS':10s} {detalle}")
        return "\n".join(lineas)


def _iso(value: Any) -> Optional[str]:
    """Fecha de Postgres -> ISO 8601 con offset UTC (como guarda el índice)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:  # el esquema viejo era timestamp sin zona, en UTC
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _json(value: Any, default: Any) -> str:
    if value is None:
        return json.dumps(default, ensure_ascii=False, sort_keys=True)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _enum(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


# --------------------------------------------------------------------------
# lectura de Postgres
# --------------------------------------------------------------------------


def _redact(dsn: str) -> str:
    """Quita la contraseña de la URL antes de imprimirla."""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    esquema, resto = dsn.split("://", 1)
    credenciales, host = resto.split("@", 1)
    usuario = credenciales.split(":", 1)[0]
    return f"{esquema}://{usuario}:***@{host}"


def cargar_desde_postgres(dsn: str) -> Dump:
    try:
        import psycopg2  # type: ignore[import-not-found]
        import psycopg2.extras  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise MigrationError(
            "falta psycopg2, que esta imagen ya no trae (Postgres salió de la app). "
            "Instálalo solo para migrar: pip install -r backend/requirements-migration.txt"
        ) from exc

    try:
        # read-only de verdad: la migración no puede tocar el original.
        conexion = psycopg2.connect(dsn, options="-c default_transaction_read_only=on")
    except Exception as exc:  # noqa: BLE001 - cualquier fallo de conexión es "no hay Postgres"
        raise MigrationError(
            f"no se pudo conectar a Postgres en {_redact(dsn)}: {exc}. "
            "No se ha escrito nada en el índice."
        ) from exc

    def leer(conexion, sql: str, tabla: str) -> list[dict[str, Any]]:
        try:
            with conexion.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                return [dict(fila) for fila in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            conexion.rollback()
            raise MigrationError(
                f"no se pudo leer la tabla '{tabla}': {exc}. "
                "No se ha escrito nada en el índice."
            ) from exc

    try:
        workspaces = leer(conexion, "SELECT * FROM workspaces", "workspaces")
        pages = leer(conexion, "SELECT * FROM pages", "pages")
        contenidos = leer(conexion, "SELECT * FROM page_content", "page_content")
        databases = leer(conexion, "SELECT * FROM databases", "databases")
        views = leer(conexion, "SELECT * FROM views", "views")
        links = leer(conexion, "SELECT * FROM links", "links")
    finally:
        conexion.close()

    cuerpos = {str(c["page_id"]): c.get("body_markdown") or "" for c in contenidos}
    titulos = {str(p["id"]): p.get("title") or "" for p in pages}

    dump = Dump()
    for w in workspaces:
        dump.workspaces.append(
            {
                "id": str(w["id"]),
                "name": w.get("name") or "default",
                "created_at": _iso(w.get("created_at")),
            }
        )
    for p in pages:
        page_id = str(p["id"])
        tipo = _enum(p.get("type") or "note")
        dump.pages.append(
            {
                "id": page_id,
                "workspace_id": str(p["workspace_id"]),
                "parent_id": str(p["parent_id"]) if p.get("parent_id") else None,
                "type": tipo,
                "title": p.get("title") or "Sin título",
                "icon": p.get("icon"),
                "description": p.get("description"),
                "created_by": p.get("created_by"),
                "header_image_path": p.get("header_image_path"),
                "body": cuerpos.get(page_id, ""),
                "path": None,  # lo pondrá P1.2 al escribir los .md
                "mtime": None,
                "size": None,
                "content_hash": None,
                "created_at": _iso(p.get("created_at")),
                "updated_at": _iso(p.get("updated_at")),
            }
        )
        if tipo == "database_row":
            dump.rows.append(
                {
                    "id": page_id,
                    "database_id": str(p["database_id"]) if p.get("database_id") else None,
                    "properties_json": _json(p.get("properties"), {}),
                    "path": None,
                }
            )
    for d in databases:
        dump.databases.append(
            {
                "id": str(d["id"]),
                "page_id": str(d["page_id"]),
                "schema_json": _json(d.get("schema_def"), []),
            }
        )
    for v in views:
        dump.views.append(
            {
                "id": str(v["id"]),
                "database_id": str(v["database_id"]),
                "name": v.get("name") or "Vista",
                "type": _enum(v.get("type") or "table"),
                "config_json": _json(v.get("config"), {}),
            }
        )
    for l in links:
        destino = str(l["target_page_id"])
        dump.links.append(
            {
                "id": str(l["id"]),
                "from_id": str(l["source_page_id"]),
                "to_id": destino,
                # El modelo viejo no guardaba el título crudo del enlace, solo
                # el destino ya resuelto: se usa el título del destino.
                "kind": _enum(l.get("kind") or "wikilink"),
                "target_title": titulos.get(destino, ""),
            }
        )
    return dump


# --------------------------------------------------------------------------
# escritura en el índice
# --------------------------------------------------------------------------


def _existe(conn, tabla: str, id_: str) -> bool:
    return conn.execute(f"SELECT 1 FROM {tabla} WHERE id = ?", (id_,)).fetchone() is not None


def _insertar(
    conn: sqlite3.Connection, tabla: str, sql: str, params: Sequence[Any], id_: str
) -> None:
    """Ejecuta un INSERT y convierte un fallo de restricción en `MigrationError`.

    Así el CLI enseña qué tabla y qué fila rompieron la migración en vez del
    traceback de `sqlite3.IntegrityError`.
    """
    try:
        conn.execute(sql, params)
    except sqlite3.IntegrityError as exc:
        raise MigrationError(
            f"la tabla '{tabla}' rechazó la fila id={id_}: {exc}. "
            "No se ha escrito nada: la transacción se deshace entera."
        ) from exc


def _verificar_claves(conn: sqlite3.Connection) -> None:
    """Comprueba las FK antes del `COMMIT`, nombrando tabla y fila.

    Con las FK diferidas, una violación aparece al confirmar y SQLite solo dice
    "FOREIGN KEY constraint failed", sin decir dónde. `PRAGMA foreign_key_check`
    devuelve la tabla y el rowid; aquí el rowid se traduce al `id` para que el
    mensaje sirva de algo.
    """
    rotas = conn.execute("PRAGMA foreign_key_check").fetchall()
    if not rotas:
        return
    tabla, rowid, padre, fkid = (rotas[0][0], rotas[0][1], rotas[0][2], rotas[0][3])
    id_ = columna = valor = None
    if tabla in TABLAS:
        fila = conn.execute(f"SELECT id FROM {tabla} WHERE rowid = ?", (rowid,)).fetchone()
        id_ = fila["id"] if fila is not None else None
        claves = conn.execute(f"PRAGMA foreign_key_list({tabla})").fetchall()
        if fkid is not None and 0 <= fkid < len(claves):
            columna = claves[fkid][3]
            actual = conn.execute(
                f'SELECT "{columna}" AS valor FROM {tabla} WHERE rowid = ?', (rowid,)
            ).fetchone()
            valor = actual["valor"] if actual is not None else None
    detalle = f"fila id={id_}" if id_ else f"rowid={rowid}"
    if columna:
        detalle += f", {columna}={valor}"
    raise MigrationError(
        f"clave foránea rota en la tabla '{tabla}' ({detalle}): apunta a una "
        f"fila de '{padre}' que no está ni en el dump ni en el índice. No se ha "
        "escrito nada: la transacción se deshace entera."
    )


def _omitir_filas_sin_database(filas: dict[str, list[dict[str, Any]]], contadores: Counters) -> None:
    """Quita las filas de `rows` que no tienen `database_id`, contándolas.

    En el esquema viejo `rows.database_id` admitía NULL (`ON DELETE SET
    NULL`): al borrar la database, sus filas se quedaban sin destino. En el
    índice es NOT NULL, así que no hay dónde colocarlas. La página de la fila
    sí se migra: existía en `/pages` y perderla sería peor que dejarla sin
    propiedades.
    """
    migrables = []
    for fila in filas["rows"]:
        if fila.get("database_id"):
            migrables.append(fila)
            continue
        contadores.omitir(
            "rows",
            f"la fila {fila['id']} no tiene database_id (su database se borró: "
            "ON DELETE SET NULL); su página se migra sin propiedades, su "
            "entrada en `rows` no",
        )
    filas["rows"] = migrables


def aplicar(index_path: str, dump: Dump, dry_run: bool = False) -> Counters:
    """Escribe el dump en el índice. Idempotente por `id`."""
    contadores = Counters()

    if dump.pages and not dump.workspaces:
        raise MigrationError(
            "Postgres tiene páginas pero ningún workspace: el índice no puede "
            "cumplir la FK. No se ha escrito nada. Revisa la base de datos original."
        )

    filas: dict[str, list[dict[str, Any]]] = {
        tabla: list(getattr(dump, tabla)) for tabla in TABLAS
    }
    _omitir_filas_sin_database(filas, contadores)

    if dry_run:
        # Un dry run no debe crear ni tocar el índice: si no existe, todo sería
        # nuevo; si existe, se mira qué ids faltan.
        if os.path.exists(index_path):
            conn = indice.connect(index_path)
            try:
                for tabla in TABLAS:
                    for fila in filas[tabla]:
                        contadores.add(tabla, not _existe(conn, tabla, fila["id"]))
            finally:
                conn.close()
        else:
            for tabla in TABLAS:
                for fila in filas[tabla]:
                    contadores.add(tabla, True)
        return contadores

    indice.ensure_index(index_path)
    conn = indice.connect(index_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Las páginas llegan en el orden del heap de Postgres, y un hijo puede
        # venir antes que su padre. Con las FK diferidas al COMMIT el orden
        # deja de importar; las violaciones reales se comprueban justo antes de
        # confirmar, para poder nombrarlas.
        conn.execute("PRAGMA defer_foreign_keys = ON")

        for w in filas["workspaces"]:
            creada = not _existe(conn, "workspaces", w["id"])
            _insertar(
                conn, "workspaces",
                "INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET name = excluded.name,"
                " created_at = excluded.created_at",
                (w["id"], w["name"], w["created_at"]),
                w["id"],
            )
            contadores.add("workspaces", creada)

        for p in filas["pages"]:
            creada = not _existe(conn, "pages", p["id"])
            columnas = ", ".join(p)
            marcas = ", ".join("?" for _ in p)
            updates = ", ".join(f"{c} = excluded.{c}" for c in p if c != "id")
            _insertar(
                conn, "pages",
                f"INSERT INTO pages ({columnas}) VALUES ({marcas})"
                f" ON CONFLICT(id) DO UPDATE SET {updates}",
                tuple(p.values()),
                p["id"],
            )
            contadores.add("pages", creada)

        for d in filas["databases"]:
            creada = not _existe(conn, "databases", d["id"])
            _insertar(
                conn, "databases",
                "INSERT INTO databases (id, page_id, schema_json) VALUES (?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET page_id = excluded.page_id,"
                " schema_json = excluded.schema_json",
                (d["id"], d["page_id"], d["schema_json"]),
                d["id"],
            )
            contadores.add("databases", creada)

        # `rows` después de `databases`: la FK va de rows.database_id a
        # databases.id (y la de rows.id a pages.id, ya insertadas).
        for r in filas["rows"]:
            creada = not _existe(conn, "rows", r["id"])
            _insertar(
                conn, "rows",
                "INSERT INTO rows (id, database_id, properties_json, path)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET database_id = excluded.database_id,"
                " properties_json = excluded.properties_json, path = excluded.path",
                (r["id"], r["database_id"], r["properties_json"], r["path"]),
                r["id"],
            )
            contadores.add("rows", creada)

        for v in filas["views"]:
            creada = not _existe(conn, "views", v["id"])
            _insertar(
                conn, "views",
                "INSERT INTO views (id, database_id, name, type, config_json)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET database_id = excluded.database_id,"
                " name = excluded.name, type = excluded.type,"
                " config_json = excluded.config_json",
                (v["id"], v["database_id"], v["name"], v["type"], v["config_json"]),
                v["id"],
            )
            contadores.add("views", creada)

        for l in filas["links"]:
            creada = not _existe(conn, "links", l["id"])
            _insertar(
                conn, "links",
                "INSERT INTO links (id, from_id, to_id, kind, target_title)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET from_id = excluded.from_id,"
                " to_id = excluded.to_id, kind = excluded.kind,"
                " target_title = excluded.target_title",
                (l["id"], l["from_id"], l["to_id"], l["kind"], l["target_title"]),
                l["id"],
            )
            contadores.add("links", creada)

        _verificar_claves(conn)
        try:
            conn.commit()
        except sqlite3.IntegrityError as exc:
            # Red de seguridad: `_verificar_claves` ya mira las FK, pero si
            # alguna restricción diferida se escapa, el mensaje sigue siendo
            # el de un error de datos.
            raise MigrationError(
                f"el índice rechazó la migración al confirmar: {exc}. "
                "No se ha escrito nada: la transacción se deshace entera."
            ) from exc
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    return contadores


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copia los datos de Postgres al índice SQLite (una vez, idempotente)."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("MDM_DATABASE_URL") or os.environ.get("DATABASE_URL"),
        help="URL de Postgres. Por defecto MDM_DATABASE_URL o DATABASE_URL.",
    )
    parser.add_argument(
        "--index-path",
        default=os.environ.get("INDEX_PATH") or "/data/index.sqlite",
        help="Ruta del índice SQLite de destino (por defecto INDEX_PATH o /data/index.sqlite).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lee Postgres y cuenta filas, sin escribir en el índice.",
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        print(
            "No hay Postgres al que conectarse: falta --database-url "
            "(o MDM_DATABASE_URL / DATABASE_URL).\n"
            "No se ha tocado el índice. Si ya no tienes Postgres, no hace falta "
            "migrar nada: la app arranca con un índice vacío.",
            file=sys.stderr,
        )
        return EXIT_SIN_POSTGRES

    print(f"Postgres: {_redact(args.database_url)}")
    print(f"Índice de destino: {args.index_path}")
    try:
        dump = cargar_desde_postgres(args.database_url)
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_SIN_POSTGRES

    print(
        "Leído de Postgres: "
        + ", ".join(
            f"{tabla}={len(getattr(dump, tabla))}"
            for tabla in ("workspaces", "pages", "rows", "databases", "views", "links")
        )
    )
    try:
        contadores = aplicar(args.index_path, dump, dry_run=args.dry_run)
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR_DATOS
    except indice.IndexSchemaError as exc:
        print(
            f"ERROR: el índice {args.index_path} no se puede usar: {exc}",
            file=sys.stderr,
        )
        return EXIT_ERROR_DATOS
    except sqlite3.DatabaseError as exc:
        print(
            f"ERROR: el índice {args.index_path} rechazó la migración: {exc}",
            file=sys.stderr,
        )
        return EXIT_ERROR_DATOS

    for aviso in contadores.avisos:
        print(f"AVISO: {aviso}", file=sys.stderr)

    if args.dry_run:
        print("Dry run: no se ha escrito nada. Lo que se haría:")
    else:
        print("Migración aplicada. Contadores:")
    print(contadores.resumen())
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
