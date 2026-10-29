# ADR-0002 — Índice SQLite y capa de repositorio (la app sin Postgres)

- **Estado:** aceptado (P1.1, 2026-10-29)
- **Sustituye a:** el almacén Postgres de las fases 0-3
- **Relacionado:** [`ADR-0001`](ADR-0001-formato-del-vault.md) (formato del vault, P0), `ROADMAP-v1.0.md` §3 y §5-P1

## Contexto

Hasta P1.1 la app era un servicio FastAPI + Postgres 16 en Docker Compose:
modelo de SQLAlchemy con `JSONB`, `UUID`, enums nativos, `tsvector`/`plainto_tsquery('spanish', …)`
para buscar y Alembic aplicando migraciones en el arranque del contenedor.

Eso tenía tres costes que el roadmap rechaza (D3 de `ROADMAP-v1.0.md`):

1. **Un servicio más**: contenedor, puerto 5432, credenciales, `pg_dump`.
2. **Tests imposibles sin infraestructura**: no se podía ejecutar el contrato de la API en un contenedor limpio (el primer paso de P6 dependía de esto).
3. **Trabajo que P1.2 iba a tirar**: cuando la verdad pasen a ser los ficheros `.md`, el ORM y las migraciones de Postgres describen un almacén que ya no será el que manda.

P1 se partió en dos justamente por esto: P1.1 deja la app corriendo sin servicios externos, y P1.2 cambia de dónde sale la verdad.

## Decisión

### 1. `sqlite3` crudo, no SQLAlchemy sobre SQLite

- La búsqueda va en **FTS5 con SQL explícito** en cualquiera de las dos opciones (no hay tipo de columna para una tabla virtual de FTS5), así que la mitad del ORM ya sería inútil.
- El ORM añadiría un **segundo modelo** que mantener sincronizado con la capa de repositorio y con el esquema real; el esquema cabe en una constante SQL de 80 líneas que se revisa de un vistazo.
- A partir de P1.2 la verdad son ficheros y el índice es **derivado y reconstruible**: modelar el índice con un ORM invita a tratarlo como fuente de verdad.
- Lo que ya validó el spike de P0 (`spike/index.py`) se reutiliza tal cual: esquema, `content='pages'`, tokenizer y consulta FTS5, política de reconstrucción.

Los routers no ven SQL: hablan con `Repository` (`backend/app/repository.py`), que devuelve `dataclass` inmutables (`Page`, `Database`, `View`) con los mismos nombres de campo que tenía el modelo anterior. **Ese es el punto de sustitución de P1.2**: cambiar los métodos del repositorio, no las rutas.

### 2. Esquema y versionado: `PRAGMA user_version`

Tablas: `workspaces`, `pages`, `databases`, `views`, `rows`, `links`, `pages_fts` (las cinco del §3 del roadmap más `workspaces` y `views`, que ya existían en la app).

- `pages` guarda el **cuerpo** (`body`) porque `pages_fts` es *external content* sobre ella (`content='pages'`): no se duplica el texto y los `rowid` son estables (el rebuild es reproducible, como exigió P0).
- `pages_fts` se mantiene con **triggers** de INSERT/UPDATE/DELETE sobre `pages`. En P1.1 el índice se escribe a cada petición, así que un `rebuild` explícito no basta; las triggers hacen que la búsqueda nunca se quede atrás.
- `PRAGMA user_version = 1` es la versión de esquema. `MIGRATIONS` (vacío hoy) es la lista de pasos futuros.
  - versión **mayor** que la del código → `IndexSchemaError` con mensaje claro. No se escribe sobre un esquema que no se entiende.
  - versión **menor** → se aplican las migraciones que existan; si falta alguna, error claro.
  - DDL de `pages_fts` distinta de la vigente → error claro (es un fallo de desarrollo: se cambió el tokenizer sin subir `SCHEMA_VERSION`).
- `synchronous = NORMAL` + WAL + `busy_timeout`. Desde P1.2 el índice es reconstruible; hoy aguanta la caída del proceso sin corromper y no finge durabilidad ante un corte de corriente (no la necesita).
- `check_same_thread=False` en la conexión, con una conexión **por petición** que nunca se comparte: FastAPI resuelve la dependencia en un hilo del pool y ejecuta las rutas `async` (la subida de cabecera) en el bucle de eventos. Sin esto, esa ruta reventaba.
- **Versión mínima del motor comprobada**: `check_sqlite_version()` en cada conexión. Por debajo de 3.27 no se puede ni crear `pages_fts` con `remove_diacritics 2`. La imagen fija la base en `python:3.12-slim-bookworm` (Debian bookworm trae SQLite 3.40.1) y el `Dockerfile` comprueba la versión en el build: el tag `-slim` a secas es flotante y podía cambiar de base —y de SQLite— sin que nada avisara.

### 3. Qué se hace con el fichero que ya está en `INDEX_PATH`

Al arrancar (y en cada petición, con cache por `mtime`/tamaño y comprobación de `user_version`). Todo lo que se puede decidir mirando el fichero se decide con una conexión de **solo lectura** (`mode=ro`): abrir con `journal_mode=WAL` ya reescribe la cabecera, y con un `INDEX_PATH` equivocado eso es tocar un fichero ajeno.

1. No empieza por `SQLite format 3\0` → no es una base de datos: se renombra a `index.sqlite.corrupto-<fecha>` y se crea uno vacío. Se aparta **antes** de abrirlo con sqlite3 porque `journal_mode=WAL` sobre basura puede inicializar el fichero y perder el original.
2. Tiene tablas que no son las de este esquema (ninguna `pages`, o una `pages` sin las columnas del esquema) → `IndexSchemaError` y **el fichero no se toca**: no se le pone el esquema encima, no se renombra y no se escribe nada. Es un `INDEX_PATH` equivocado, no un índice roto; la app no arranca y el mensaje dice qué tablas hay y qué hacer. (En la primera versión de P1.1, un fichero con una tabla `views` del usuario acababa renombrado como «ilegible» y la app arrancaba vacía encima.)
3. Tiene nuestro esquema pero `quick_check` no dice `ok` → `IndexSchemaError` y no se toca nada. En P1.1 el índice es la única copia: se prefiere parar en claro (el mensaje incluye lo que devolvió el comprobador) a apartar un fichero del que todavía se puede sacar algo. Se aparta solo lo que SQLite no puede leer en absoluto (punto 1, o un `sqlite3.DatabaseError` al abrirlo).
4. Fichero de 0 bytes, o SQLite válido sin ninguna tabla → se le pone el esquema.

La única pérdida de datos posible es la del punto 1, cuando SQLite no puede leer el fichero.

### 4. Adaptaciones del código

| Antes (Postgres) | Ahora |
| --- | --- |
| `JSONB` (`properties`, `schema_def`, `views.config`) | `TEXT` con JSON (`ensure_ascii=False`, `sort_keys=True`) |
| `UUID` nativo | `TEXT` con UUID canónico; los schemas de Pydantic siguen exponiendo `uuid.UUID` |
| Enums nativos | `TEXT` + `PageType`/`ViewType`/`LinkKind` en `backend/app/enums.py` |
| `tsvector` + `plainto_tsquery('spanish')` + `ts_rank` | FTS5 `unicode61 remove_diacritics 2` + `bm25()` |
| `page_content` (tabla aparte) | columna `pages.body` (una fila por página, necesaria para FTS5 external content) |
| `models.py` (SQLAlchemy) + `database.py` (engine/sesiones) | `enums.py`, `index.py`, `repository.py`, `dependencies.py` |
| Alembic en el `command` del contenedor | fuera: `alembic/` y `alembic.ini` eliminados, `alembic` fuera de `requirements.txt` |
| `psycopg2-binary` en la imagen | fuera de la imagen; solo en `requirements-migration.txt` para el script de una vez |

La búsqueda mantiene el comportamiento observable: `reunion` encuentra `Reunión` (sin diacríticos) y cada término se busca además como **prefijo**, que es lo que hacía el lematizador español de Postgres (`presupu` encuentra `presupuesto`). Lo que no se replica: la lista de *stopwords* del diccionario español y el stemming real. No hay comportamiento de la API que dependiera de eso y P5 revisará la búsqueda sobre ficheros.

### 5. Un contenedor, sin servicios

`docker-compose.yml` queda con un único servicio `api`; sin `db`, sin puerto 5432, sin `depends_on`. El índice vive en el volumen `mdm_data` montado en `/data` (fuera del repo).

Directorios: los crea la **imagen** en el build (`RUN mkdir -p /app/attachments /data`), así que un contenedor limpio arranca sin montar nada; y al importarse, `app/main.py` crea `attachments_dir` para el caso de un volumen de adjuntos vacío. Lo que la app **no** hace es crear el directorio de `INDEX_PATH`: si el volumen no está montado donde toca, o `INDEX_PATH` apunta a un directorio que no existe, el arranque falla con `unable to open database file`. Es deliberado: un índice que no está donde la app cree es mejor que uno vacío y silencioso.

### 6. Migración de una vez, idempotente

`backend/scripts/migrate_postgres_to_sqlite.py` lee Postgres en **transacción de solo lectura** (`default_transaction_read_only=on`), normaliza al esquema del índice y escribe con `INSERT … ON CONFLICT(id) DO UPDATE`:

- contadores por tabla (`nuevas` / `actualizadas`) y resumen final, más las filas omitidas y el motivo;
- **no depende del orden de lectura**: `SELECT * FROM pages` puede devolver un hijo antes que su padre (basta con que el padre se editara después: su tupla se va al final del heap). Las claves foráneas se difieren al `COMMIT` (`PRAGMA defer_foreign_keys`) y, justo antes de confirmar, se comprueban con `PRAGMA foreign_key_check`, que dice tabla y fila;
- una fila de `rows` con `database_id` a `NULL` (el esquema viejo lo permitía: `ON DELETE SET NULL`) no se puede colocar en el índice: se omite, se cuenta en `omitidas` y se avisa con su id; su página sí se migra;
- si el índice rechaza una fila (restricción, clave foránea rota) → **código 3 con la tabla y la fila en el mensaje**, transacción deshecha entera y sin traceback;
- `--dry-run` que no escribe nada;
- si falta `psycopg2`, no hay URL o no se puede conectar → **código 2 y mensaje claro**, sin tocar el índice;
- Postgres queda intacto como respaldo;
- no migra `tags`/`page_tags` ni `attachments`: la API nunca escribió en esas tablas (no hay endpoints), y migrarlas sugeriría un soporte que no existe.

## Consecuencias

- La app arranca y se testea sin Docker, sin Postgres y sin red: `pytest` en verde sobre un índice en un directorio temporal (contrato de la API, esquema/FTS5 y migración).
- Un único fichero que respaldar (y, desde P1.2, un índice que se puede borrar y reconstruir).
- `GET /status` mantiene `services.db` (el panel pinta `dot-db`): ahora informa del índice, con `engine`, `schema_version` y recuentos por tabla.
- **Diferencias observables en las respuestas** (las rutas, los campos y los tipos de `PageRead`/`DatabaseRead`/`RowRead`/`ViewRead` no cambian):
  1. `body_markdown` de una página `database` o de una fila pasa de `null` a `""`: en el modelo nuevo el cuerpo es una columna de `pages`, con `''` por defecto, y esas páginas nunca tuvieron fila en `page_content`. El frontend ya trataba ambos igual (`body_markdown || ""`).
  2. Búsqueda: se pierden las *stopwords* del diccionario español y el *stemming* real; cada término se busca también como prefijo (§4).
  3. El **orden alfabético** de `/pages/tree` y de los backlinks lo decide SQLite (`COLLATE NOCASE`, que solo pliega ASCII), no la collation española de Postgres: con títulos que empiezan por `Á`/`Ñ` el orden puede cambiar. El conjunto de resultados es el mismo.
- **Igual que antes de P1.1, y ahora con test**: los wikilinks resuelven el título sin distinguir mayúsculas en todo Unicode (`casefold` de NFKC; `casefold` pliega además `ß`→`ss`, que ningún título usa) y enlazan a **todas** las páginas que compartan ese título, como el `lower()` de Postgres. Y un `PATCH` con el mismo título/icono/descripción no mueve `updated_at`, mientras que guardar el cuerpo sí lo mueve (incluso con el mismo texto), como el `update_page` viejo. La primera versión de P1.1 había perdido las dos cosas; `test_api_contract.py` las cubre. Resolución en Python a propósito: una consulta por guardado (`SELECT id, title FROM pages WHERE workspace_id = ?`) y `fold_title` en memoria, sin columna derivada que se pueda quedar obsoleta y sin cambiar el esquema. Coste medido aquí: ~6 ms con 5.001 páginas por cada guardado con enlaces; aceptable para autoguardar cada 2 s, y P1.2 rehace igual esta parte al leer del vault.
- **Deuda conocida y deliberada**: en P1.1 el índice *es* la verdad, no un índice. Por eso la política de esquema es conservadora (nunca se borra por versión, solo por corrupción) y `path`/`mtime`/`size`/`content_hash` existen pero están a `NULL`.
- Los ficheros `.md`, la papelera, el reindexado y el renombrado en disco siguen fuera: son P1.2.

## Lo que no se ha podido verificar aquí

- **Migración con datos reales de Postgres**: no hay Postgres accesible en el entorno de desarrollo. Se ejecuta el camino "no hay Postgres → lo dice y no escribe" y la escritura en el índice con un dump con la forma de Postgres; la lectura real (`cargar_desde_postgres`) queda sin ejecutar.
- **Construcción de la imagen Docker**: no hay Docker en el contenedor. El `Dockerfile` y el `compose` se revisan a mano; el arranque se valida con `uvicorn` + SQLite real.
- **La versión de SQLite de la imagen**: sin daemon no se ha construido. La comprobación del `Dockerfile` (`>= 3.27`) y la de la app (`MIN_SQLITE_VERSION`) se han ejercitado aquí con SQLite 3.53.1, no con la 3.40.1 de bookworm.

## Alternativas descartadas

| Alternativa | Por qué no |
| --- | --- |
| SQLAlchemy 2.0 sobre SQLite | FTS5 seguiría en SQL crudo; un modelo más que sincronizar; invita a tratar el índice como fuente de verdad antes de P1.2 |
| Mantener Postgres y añadir SQLite "en paralelo" | Dos verdades, dos migraciones, y el objetivo de P1.1 (arrancar sin servicios) no se cumple |
| Migrar con `pg_dump` a un `.sql` y reimportar | El esquema destino no es Postgres: ni `JSONB` ni `tsvector` ni enums existen igual; el mapeo habría que escribirlo de todas formas |
| Reconstruir el índice entero en cada escritura | O(n) por guardado a 2 s; las triggers de FTS5 dan lo mismo por fila |
| Borrar el fichero ante cualquier inconsistencia de esquema | En P1.1 el índice no es derivable: sería pérdida de datos silenciosa. Se para en claro y se nombra el problema |
