# MyDamnManagement

![Logo MyDamnManagement](public/MDMLogo.png)

Notas en Markdown + bases de datos tipo Notion + CRM + gestor de tareas — autoalojado, sin depender de terceros. Un único motor genérico de páginas/bases de datos/vistas del que salen los tres productos.

## Qué es

No hay tres subsistemas separados. Hay un motor con tres entidades:

- **`page`** — una página (nota, base de datos, o fila de una base de datos).
- **`database`** — define un esquema de propiedades tipadas (texto, número, fecha, relación...).
- **`view`** — una forma de mirar esas filas (tabla, tablero kanban, calendario).

CRM y Tareas/Proyectos son configuraciones de `database` + `view`, no código aparte. Notas y wiki son páginas con `type = note` y cuerpo en Markdown, con wikilinks y backlinks.

El detalle completo, con diagramas, está en [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Estado actual

Hay un **panel visual real** en [http://localhost:8000/](http://localhost:8000/) — árbol de páginas (con selección múltiple y borrado en lote), editor de notas con formato en vivo y **edición por bloques** al estilo Notion (párrafo/H1-H5/listas/código/línea horizontal, seleccionables, convertibles entre sí —también con el comando `/`—, borrables en lote), tablas/tableros para las bases de datos, y botones de un clic para crear un **CRM** o un **gestor de tareas** ya configurados (Fase 3).

El resto del frontend sigue siendo HTML/CSS/JS plano sin build (`frontend/index.html` + `frontend/app.js`, servidos por la propia API); la única pieza compilada es el editor, vendorizado como un único archivo (`frontend/vendor/editor.bundle.js`, generado una vez con esbuild desde `frontend/editor-src/` — ver más abajo).

Las páginas tienen metadatos propios: icono (emoji real, con selector de búsqueda), creador (se fija solo al crear, pensado para cuando haya espacios grupales), descripción y una imagen de cabecera opcionales. Todo se autoguarda 2 segundos después del último cambio — no hay botón "Guardar". El editor entiende `/` como comando de bloque al estilo Notion, convierte `->`/`<-`/`<->` en flechas de verdad y muestra los wikilinks como enlaces normales con vista previa al pasar el ratón.

## Cómo arrancarlo (desarrollo)

Requiere Docker Desktop (Windows, backend WSL2 recomendado).

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:8000/health
```

La app ya no necesita Postgres: guarda todo en un único índice SQLite (`INDEX_PATH`, en el contenedor `/data/index.sqlite`, volumen `mdm_data` fuera del repo). El esquema se crea o se migra solo al arrancar (`PRAGMA user_version`; ver `backend/app/index.py` y [`docs/ADR-0002-indice-sqlite.md`](docs/ADR-0002-indice-sqlite.md)): no hay `alembic upgrade head` ni servicio `db`. Si el fichero del índice no es una base de datos (basura o truncado de tal forma que SQLite no lo abre), se aparta con sufijo `.corrupto-<fecha>` y se rehace vacío — no impide arrancar. Si lo que hay en `INDEX_PATH` es un fichero de otra cosa (un esquema ajeno, o una base que `quick_check` da por dañada), la app **no arranca** y lo dice: el índice es reconstruible desde el vault, pero no se toca ni se renombra nada que no sea nuestro. Detalle en el §3 de [`docs/ADR-0002-indice-sqlite.md`](docs/ADR-0002-indice-sqlite.md).

Desde **P1.2a** las notas viven en ficheros `.md` (`VAULT_PATH`, volumen `mdm_vault`): guardar una nota escribe `Notas/<título>.md` (y su carpeta si tiene subpáginas) y el índice es **derivado**. El fichero manda: si editas un `.md` por fuera, `POST /reindex` lo recoge; si borras `index.sqlite`, el arranque lo reconstruye desde el vault. Renombrar es renombrar el fichero (el `id` del frontmatter es la identidad y sobrevive) y borrar mueve a `.trash/<fecha UTC>/<ruta original>` en vez de destruir: `GET /trash` lo lista con su ruta original y su fecha, y `POST /trash/restore` lo devuelve a su sitio (409 si esa ruta está ocupada, nunca un duplicado silencioso).

Desde **P1.2b** los adjuntos también están dentro del vault: la imagen de cabecera se guarda en `_attachments/<año>/<mes>/header-<id>.<ext>`, su URL es la ruta del vault (`/files/_attachments/…`, servida con el tipo decidido por magic bytes y `nosniff`) y la nota la lleva en el frontmatter, así que **copiar la carpeta del vault es un backup completo** — `.md` + imágenes, y la copia arranca sola. Para los adjuntos que quedaron en el volumen viejo (`/app/attachments`) hay una migración de una vez, idempotente y con contadores. `GET /status` dice además la **deriva** entre índice y disco: cuántos ficheros han cambiado por fuera desde el último reindexado (comparando `mtime` + tamaño, sin leer cuerpos) y cuándo fue ese reindexado.

Desde **P2** las bases de datos y sus filas también son ficheros: `Bases/<slug>.md` con `type: database`, el `schema` y las `views` en el frontmatter, y cada fila en `Bases/<slug>/<fila>.md` con las propiedades en el frontmatter y sin `title` (el título es el nombre del fichero). Crear la base (también desde CRM/Tareas), editar `schema`/`views`, crear, editar y borrar filas escriben el fichero y actualizan el índice en la misma operación; renombrar una base renombra su carpeta de filas y borrar la manda a la papelera con ellas. Una base que cuelga de una nota vive en la carpeta de hijas de esa nota (`Notas/Proyecto/Contactos.md`). Con esto **todo el contenido está en `.md` y el índice se puede tirar**: el reindexado reconstruye notas, bases, filas y vistas, y una fila huérfana (sin su `Bases/<slug>.md`) se avisa sin abortar.

Si el vault está vacío **a propósito** (lo vaciaste a mano, restauraste solo su volumen y no el del índice, o `VAULT_PATH` apunta a un directorio vacío), el arranque se niega a vaciar el índice y no arranca: `POST /reindex?force=true` no sirve de salida porque el servicio es justo el que no está en pie. La salida es arrancar una vez con `VAULT_ALLOW_EMPTY=1`, que deja el índice **vacío** (desde P2 también las bases y las filas son ficheros, así que un vault vacío no tiene nada que indexar). Una ruta que no existe no se cubre con esa variable: un volumen sin montar no es un vault vacío, y esa sigue fallando. Formato exacto en [`docs/ADR-0001-formato-del-vault.md`](docs/ADR-0001-formato-del-vault.md).

Tests (sin Docker y sin servicios externos, sobre un índice y un vault temporales):

```bash
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
pytest
```

### CI (GitHub Actions)

Cada `push` y cada `pull_request` ejecutan [`.github/workflows/tests.yml`](.github/workflows/tests.yml): dos jobs en paralelo, sin secretos y sin servicios.

- **`pytest (Python 3.12)`** — la versión de Python de la imagen (`backend/Dockerfile`), no la del contenedor de desarrollo. Instala `backend/requirements.txt` junto con `backend/requirements-dev.txt`, cachea pip entre ejecuciones y ejecuta `pytest -q` desde la raíz (la suite entera de `backend/tests/`). No levanta Postgres ni Docker: el índice es un fichero SQLite y cada test estrena su propio vault temporal, así que el job no necesita nada más.
- **`docker build`** — construye la imagen de `backend/Dockerfile` con el contexto en la raíz del repo (lo mismo que hace `docker compose up --build`) y no la publica en ningún sitio. El propio Dockerfile aborta el build si el SQLite de la base baja de 3.27.

**Estado conocido:** con la punta actual el job `pytest (Python 3.12)` sale en **rojo**, y no por el workflow. `backend/scripts/migrate_attachments_into_vault.py:251` llama a `Path.read_text(..., newline="")`, parámetro que existe desde Python 3.13; en 3.12 lanza `TypeError: Path.read_text() got an unexpected keyword argument 'newline'` y arrastra consigo `backend/tests/test_migrate_attachments.py` (la suite entera en 3.12: `1 failed, 27 passed, 177 errors`; los 177 errores son cascada del fixture, no fallos distintos). Sin ese fichero, los otros pasan: `109 passed`. El mismo script es el que ejecuta la migración de adjuntos dentro de la imagen 3.12, así que ahí también revienta. El arreglo va aparte, en la tarjeta **t_0cd3ea99**; el workflow se queda en 3.12 porque es la versión de la imagen. Hasta que se arregle, el primer push sale en rojo por esta causa: es el hallazgo, no una regresión de lo que cambies.

Lo que **no** cubre: que la app arranque contra los volúmenes de verdad, el panel en un navegador, ni ningún despliegue. Si un job falla, la ejecución queda en rojo en la pestaña *Actions* del repo, pero GitHub no impide el push por ello: para eso habría que marcar la comprobación como obligatoria en la protección de la rama, y eso se configura en el repo, no en este fichero.

Para reproducir el job de tests tal cual, con la misma versión:

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
pytest -q
```

Comprobado con CPython 3.12.16 (build de [python-build-standalone](https://github.com/astral-sh/python-build-standalone), cualquier `3.12.x` reproduce lo mismo): el `pip install` sale limpio y `pytest -q` da lo descrito en «estado conocido». Con el intérprete del contenedor de desarrollo (3.13/3.14) la suite entera pasa, así que un verde local no dice nada sobre este job.

El job de la imagen es, desde la raíz y con Docker en marcha:

```bash
docker build -f backend/Dockerfile -t mydamnmanagement:ci .
```

Migración de una vez desde el Postgres antiguo (opcional; deja Postgres intacto como respaldo y es idempotente):

```bash
pip install -r backend/requirements-migration.txt
python backend/scripts/migrate_postgres_to_sqlite.py \
    --database-url postgresql://usuario:clave@localhost:5432/mydamnmanagement \
    --index-path /data/index.sqlite
```

Pasar las notas que ya están en el índice al vault (una vez, al adoptar P1.2; idempotente y con contadores):

```bash
python backend/scripts/migrate_index_to_vault.py \
    --index /data/index.sqlite --vault /data/vault --reindex
```

Pasar las bases de datos y sus filas al vault (una vez, al adoptar P2; idempotente y con contadores: `bases_escritas`, `filas_escritas`, `vistas_escritas`, `bases_saltadas`, `filas_saltadas`). Migra antes las notas: una base que cuelga de una nota se escribe en la carpeta de esa nota solo si la nota ya está en el vault:

```bash
python backend/scripts/migrate_databases_to_vault.py \
    --index /data/index.sqlite --vault /data/vault --reindex
```

El `id` de cada base pasa a ser el de su página, así que el `database_id` que devolvía la API cambia una vez; las filas conservan el suyo.

Mover los adjuntos que quedaron fuera del vault (una vez, al adoptar P1.2b; idempotente y con contadores: `movidos`, `ya_estaban`, `notas_actualizadas`, `papelera_actualizadas`, `indice_actualizado`):

```bash
python backend/scripts/migrate_attachments_into_vault.py \
    --source /app/attachments --vault /data/vault --index /data/index.sqlite --reindex
```

Reescribe el `header_image` de las notas **y también el de las que están en `.trash/`** (solo el frontmatter; el fichero no se mueve de la papelera): una nota borrada antes de migrar se queda apuntando al esquema viejo y, al restaurarla, su imagen daría 404 — el adjunto migrado no tendría ninguna referencia viva. Esas cuentan aparte, en `papelera_actualizadas`, y las que apuntan a un fichero que no existe ni en el origen ni en el vault salen como `aviso:` y se dejan intactas.

El `--reindex` final es opcional pero recomendable: reescribir el frontmatter cambia el `mtime`, así que sin él `GET /status` muestra deriva (`drift.modified` = notas reescritas) hasta el siguiente reindexado. Con el flag, el índice queda al día en la misma pasada. Sin `--index` (o si el fichero no existe) el script solo toca los `.md`; con `--reindex` y un `--index` que no existe, **no se crea el índice**: avisa (`no se reindexa: no hay índice en …`), deja los contadores de la migración como están y sale con 0, así que un `--index` con errata no deja un índice nuevo y vacío haciendo compañía al de verdad.

En Docker el volumen viejo (`<proyecto>_attachments`) ya no se monta: P1.2b lo retira de `docker-compose.yml`, pero **no** borra sus datos. Hay que montarlo solo para esta pasada, sin `:ro` (el script retira los originales que ya están en el vault y poda las carpetas vacías):

```bash
docker compose run --rm \
    -v mydamnmanagement_attachments:/app/attachments \
    api python scripts/migrate_attachments_into_vault.py \
    --source /app/attachments --vault /data/vault --index /data/index.sqlite --reindex
```

El nombre del volumen es el del proyecto de compose que lo creó: `docker volume ls | grep attachments` dice cuál es si no es `mydamnmanagement`.

Documentación interactiva de la API en [http://localhost:8000/docs](http://localhost:8000/docs). Panel visual en [http://localhost:8000/](http://localhost:8000/) (estado de `api`/`db` en la cabecera, árbol de páginas y bases de datos a la izquierda).

## Panel visual

- **Páginas**: `+` crea una nota (`Sin título`, lista para renombrar). Icono (emoji real, clic abre un selector con buscador), título, descripción opcional e imagen de cabecera opcional, todo en la propia cabecera de la nota, que ocupa todo el ancho — no hay una caja separada para el cuerpo; "Creado por" se fija solo una vez, al crearla. El editor es un único cuadro — no hay "modo edición" y "modo vista previa" separados: escribes Markdown y el propio formato aparece al momento (encabezados más grandes, negrita/cursiva reales, `->`/`<-`/`<->` como flechas de verdad, wikilinks con aspecto de enlace normal y vista previa al pasar el ratón), con el marcador de sintaxis (`#`, `**`, `[[`/`]]`) visible pero pequeño y discreto en el sitio donde lo escribiste, o el markdown en crudo si el cursor está justo ahí. `Ctrl/Cmd + clic` sobre un wikilink resuelto navega a esa página (a la nota o a la database, según lo que sea); backlinks en un desplegable arriba. Todo se **autoguarda** 2s después del último cambio (el estado "Guardado"/"Guardando…"/"Cambios sin guardar…" con icono de progreso, arriba del todo); `Ctrl/Cmd + S` fuerza el guardado ya.
- **Bloques**: cada párrafo/encabezado/ítem de lista/bloque de código/línea horizontal tiene un asa `⋮⋮` a la izquierda (aparece al pasar el ratón por esa línea). Clic para seleccionar el bloque; `Mayús`/`Ctrl`+clic para seleccionar varios; clic derecho abre un menú que primero indica el tipo del bloque (si hay uno solo seleccionado) y luego ofrece **convertir** a otro tipo o **eliminar**. Escribir `/` al principio de un bloque vacío abre el mismo listado de tipos como comando (`/h2`, `/list`, `/code`... — filtra según lo que escribas después de la barra); `Mayús+Enter` crea un bloque nuevo, `Enter` a solas baja de línea dentro del mismo bloque. Sigue siendo el mismo `body_markdown` de siempre — un bloque es un rango de texto calculado sobre el árbol de sintaxis, no un objeto nuevo (ver `docs/ARCHITECTURE.md`).
- **Selección múltiple de páginas**: `Ctrl`/`Mayús`+clic sobre filas del árbol (barra lateral) para seleccionar varias; clic derecho abre un menú para eliminarlas todas de una vez. Clic en zona vacía de la barra lateral limpia la selección.
- **Bases de datos**: los botones **CRM** / **Tareas** crean de un clic una database ya configurada (propiedades + vistas por defecto, plantillas en `backend/app/templates.py`) — el título es editable después haciendo clic en él. `+` abre el diálogo de creación en blanco: nombre y propiedades (clave, nombre visible, tipo; `select`/`multiselect` piden opciones separadas por coma). El formulario de "+ Nueva fila" genera automáticamente el input correcto según el tipo declarado (texto, número, `select`, checkbox, fecha).
- **Vistas**: `+ Vista` crea una vista `table`/`board`/`list`/`calendar`; para `board` se elige la propiedad de agrupación y las columnas salen de sus `options` (o de los valores encontrados si no las tiene).
- **Buscar**: el cuadro de la barra lateral llama a `/search` con un pequeño debounce.

### El editor de notas (única pieza con build)

Todo el resto del frontend es HTML/JS servido tal cual, sin compilar. El editor es la excepción deliberada: un editor con formato en vivo de verdad necesita gestión de cursor/selección que un `<textarea>` no puede dar (no soporta tamaños de fuente distintos por línea), así que usa [CodeMirror 6](https://codemirror.net/). CM6 se distribue como paquetes ES modules pensados para bundlers — en vez de tirar de un CDN en cada carga (dependencia de terceros en tiempo de ejecución, mala idea detrás de Tailscale) o de meter un bundler en el flujo de trabajo normal, se compila **una vez** a un único archivo:

```bash
cd frontend/editor-src
npm install
npm run build   # genera ../vendor/editor.bundle.js (se commitea)
```

Solo hace falta repetirlo si tocas `frontend/editor-src/entry.js` (la lógica de qué se resalta y cómo). El resto del tiempo, `frontend/vendor/editor.bundle.js` es un archivo estático más, y `frontend/app.js` lo importa como cualquier módulo ES (`import { createNoteEditor } from "/vendor/editor.bundle.js"`).

## Endpoints disponibles

| Endpoint       | Método | Qué hace                                              |
| -------------- | ------ | ------------------------------------------------------ |
| `/`            | GET    | Panel de estado (frontend estático, sin build)           |
| `/health`      | GET    | Comprobación de vida del servicio                       |
| `/status`      | GET    | Estado agregado de `api` y del almacén (`db`, ahora el índice SQLite) y `drift`: ficheros modificados, añadidos o desaparecidos desde el último reindexado (`mtime` + tamaño) y la marca de ese reindexado |
| `/reindex`     | POST   | Reconstruye el índice desde el vault (`Notas/*.md` y `Bases/**`) y devuelve recuento de notas, bases, filas, vistas y enlaces, duración y avisos. **409** si el vault no existe o no tiene ningún `.md` (un volumen sin montar no es un vault vacío); `?force=true` para vaciarlo a propósito (en el arranque, `VAULT_ALLOW_EMPTY=1`) |
| `/pages`       | GET    | Lista todas las páginas del workspace                   |
| `/pages`       | POST   | Crea la página con su cuerpo y sus wikilinks (y escribe su `.md` si es una nota) |
| `/pages/{id}`  | GET    | Lee una página con su cuerpo                             |
| `/pages/{id}`  | PATCH  | Actualiza título, icono, descripción, cuerpo o `parent_id` (mover de carpeta; `null` = a la raíz) |
| `/pages/{id}`  | DELETE | Borra la página (las notas van a `.trash/`; el resto, encadenado a hijos, enlaces y, si es una database, sus filas y vistas). **400** si de una nota cuelga una base: en P1.2a la base solo vive en el índice y no tendría papelera |
| `/pages/tree`  | GET    | Árbol de páginas por `parent_id`                        |
| `/pages/{id}/backlinks` | GET | Páginas que enlazan a esta vía `[[wikilink]]`     |
| `/pages/{id}/header-image` | POST | Sube/reemplaza la imagen de cabecera (`multipart/form-data`, campo `file`) |
| `/pages/{id}/header-image` | DELETE | Quita la imagen de cabecera                       |
| `/files/...`   | GET    | Sirve los adjuntos del vault (`_attachments/<año>/<mes>/…`): el `Content-Type` sale de los magic bytes del fichero y va con `nosniff`; fuera de `_attachments/`, 404 |
| `/trash`       | GET    | Papelera: ruta original, fecha del borrado (UTC) y tamaño de cada fichero, lo más reciente primero |
| `/trash/restore` | POST | Devuelve un fichero de la papelera a su ruta original y reindexa. **409** si la ruta está ocupada o si un ancestro de ella es un fichero (no una carpeta); **404** si la entrada no existe |
| `/search?q=`   | GET    | Búsqueda full-text (título + cuerpo) en español          |

Los wikilinks se escriben como `[[Título de la página]]` (o `[[Título\|Alias]]`) en `body_markdown`; se resuelven por título exacto (sin distinguir mayúsculas) dentro del mismo workspace al guardar la página.

### Motor de `database` + `view`

| Endpoint | Método | Qué hace |
| --- | --- | --- |
| `/databases` | POST | Crea una `database` (página + `schema_def`) |
| `/databases` | GET | Lista todas las databases |
| `/databases/{id}` | GET / PATCH / DELETE | Leer, editar (título/icono/schema) o borrar (arrastra sus filas) |
| `/databases/{id}/rows` | POST | Crea una fila; valida `properties` contra el `schema_def` |
| `/databases/{id}/rows` | GET | Lista filas; con `?view={id}` aplica el filtro/orden de esa vista |
| `/databases/{id}/rows/{id}` | GET / PATCH / DELETE | Leer, editar (merge parcial de `properties`) o borrar una fila |
| `/databases/{id}/views` | POST / GET | Crear o listar vistas (`table`\|`board`\|`calendar`\|`list`) |
| `/databases/{id}/views/{id}` | PATCH / DELETE | Editar o borrar una vista |
| `/databases/templates` | GET | Lista las plantillas disponibles (`crm`, `tasks`) |
| `/databases/from-template` | POST | Crea una `database` desde una plantilla: schema + vistas por defecto en una llamada |

Una `view.config` tiene esta forma: `{"filters": [{"key":"fase","op":"eq","value":"ganado"}], "sort": {"key":"valor","dir":"desc"}, "group_by": "fase", "visible_properties": ["fase","valor"]}`. `filters` y `sort` los aplica el backend; `group_by` y `visible_properties` son metadatos para que el frontend decida cómo dibujar la vista.

## Roadmap

- [x] **Fase 0** — Compose + Postgres + API mínima
- [x] **Fase 1** — Notas/wiki: árbol de páginas, wikilinks + backlinks, búsqueda full-text, Alembic
- [x] **Fase 2** — Motor de `database` + `view` genéricos (tabla, board, calendario)
- [x] **Panel visual** (adelanto de Fase 4) — árbol, editor de notas, tablas/tableros, todo sin build
- [x] **Fase 3** — CRM y Tareas como plantillas preconfiguradas (`backend/app/templates.py`; un clic en el panel)
- [x] **Editor de notas con formato en vivo** (CodeMirror 6) — mismo cuadro para escribir y ver el resultado
- [x] **Metadatos de página + autoguardado** — icono/creador/descripción/imagen de cabecera, autoguardado a los 2s
- [x] **Edición por bloques + selección múltiple** — bloques calculados sobre el árbol de sintaxis (sin nuevo modelo de datos), selección múltiple de bloques y de páginas, menú contextual con convertir/eliminar
- [x] **Editor al estilo Notion** — menú de comando `/`, línea horizontal como bloque, `Mayús+Enter` para bloque nuevo, selector de emoji real, autoformato de flechas y wikilinks, logo propio
- [x] **P1.1 — la app sin Postgres** — índice SQLite + capa de repositorio, búsqueda FTS5 sin acentos, `pytest` sin servicios externos y script de migración de una vez
- [x] **P1.2a — las notas pasan a ficheros `.md`** — vault en disco (`Notas/`, `.trash/`), guardar/renombrar/mover/borrar escriben fichero, reindexado desde el vault (`POST /reindex` y al arrancar) y migración del índice al vault
- [x] **P1.2b — el vault completo** — adjuntos dentro del vault (`_attachments/`, servidos por `/files` con la ruta del vault y validación por magic bytes), restaurar desde la papelera (`GET /trash`, `POST /trash/restore`) y deriva índice/disco en `/status`
- [x] **P2 — bases de datos y filas en ficheros** — `Bases/<slug>.md` + `Bases/<slug>/*.md`, `schema` y `views` en el frontmatter, alta/baja/edición escribiendo ficheros, reindexado de bases y filas, papelera y migración
- [ ] **Fase 4** — Despliegue en Proxmox + Tailscale, backups automatizados

## Despliegue objetivo

 Desarrollo en Windows con Docker Desktop; a producción sobre un LXC/VM de Proxmox personal, accesible solo a través de una red [Tailscale](https://tailscale.com/) (sin exponer puertos públicos, sin gestión de cuentas por ahora — uso personal en solitario). El mismo `docker-compose.yml` sirve para los dos: la imagen de `api` es autocontenida (backend + `frontend/` ya copiados dentro, ver más abajo), así que mover el proyecto a Proxmox es copiar `docker-compose.yml` + `.env` y levantar los contenedores ahí — los datos viajan en dos volúmenes: `mdm_vault` (las notas `.md`, **esto es lo que hay que respaldar**; `rsync` de la carpeta basta) y `mdm_data` (el índice SQLite, reconstruible desde el vault).

La imagen de `api` se construye con el contexto en la raíz del repo (`context: .` en `docker-compose.yml`, no `./backend`) precisamente para poder copiar `frontend/` (incluido `vendor/editor.bundle.js`, ya compilado) dentro de la imagen — antes solo llegaba vía el bind-mount de desarrollo (`./frontend:/app/frontend:ro`), que no existe fuera de esta máquina; sin el bind-mount, `/app/frontend` se creaba vacío y el panel no se servía. En desarrollo el bind-mount sigue ahí y se superpone al contenido ya copiado (para hot-reload sin reconstruir la imagen); en un despliegue sin ese bind-mount, la imagen ya trae todo consigo.

## Licencia

[MIT](LICENSE)
