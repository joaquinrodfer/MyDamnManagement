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

Desde **v1.3** la app pide login: hay usuarios y sesiones (cookie `HttpOnly`), el creador de cada nota es quien la escribe de verdad y todo lo que no sea `/health`, el propio login o `/agent/...` exige sesión — ver «Usuarios y acceso».

Las páginas tienen metadatos propios: icono (emoji real, con selector de búsqueda), creador (se fija solo al crear, con el usuario de la sesión), descripción y una imagen de cabecera opcionales. Todo se autoguarda 2 segundos después del último cambio — no hay botón "Guardar". El editor entiende `/` como comando de bloque al estilo Notion, convierte `->`/`<-`/`<->` en flechas de verdad y muestra los wikilinks como enlaces normales con vista previa al pasar el ratón. Al escribir `[[` sale la lista de páginas del vault mientras sigues escribiendo (filtra sin tildes) y, si el título no existe todavía, la nota se crea desde ahí mismo.

## Cómo arrancarlo (desarrollo)

Requiere Docker Desktop (Windows, backend WSL2 recomendado).

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:8000/health
```

La app ya no necesita Postgres: guarda todo en un único índice SQLite (`INDEX_PATH`, en el contenedor `/data/index.sqlite`, volumen `mdm_data` fuera del repo). El esquema se crea o se migra solo al arrancar (`PRAGMA user_version`; ver `backend/app/index.py` y [`docs/ADR-0002-indice-sqlite.md`](docs/ADR-0002-indice-sqlite.md)): no hay `alembic upgrade head` ni servicio `db`. Si el fichero del índice no es una base de datos (basura o truncado de tal forma que SQLite no lo abre), se aparta con sufijo `.corrupto-<fecha>` y se rehace vacío — no impide arrancar. Si lo que hay en `INDEX_PATH` es un fichero de otra cosa (un esquema ajeno, o una base que `quick_check` da por dañada), la app **no arranca** y lo dice: el índice es reconstruible desde el vault, pero no se toca ni se renombra nada que no sea nuestro. Detalle en el §3 de [`docs/ADR-0002-indice-sqlite.md`](docs/ADR-0002-indice-sqlite.md).

Desde **P1.2a** las notas viven en ficheros `.md` (`VAULT_PATH`, volumen `mdm_vault`): guardar una nota escribe `Notas/<título>.md` (y su carpeta si tiene subpáginas) y el índice es **derivado**. El fichero manda: si editas un `.md` por fuera, el vigilante de P8 lo recoge en un par de segundos (o `POST /reindex`); si borras `index.sqlite`, el arranque lo reconstruye desde el vault. Renombrar es renombrar el fichero (el `id` del frontmatter es la identidad y sobrevive) y borrar mueve a `.trash/<fecha UTC>/<ruta original>` en vez de destruir: `GET /trash` lo lista con su ruta original y su fecha, y `POST /trash/restore` lo devuelve a su sitio (409 si esa ruta está ocupada, nunca un duplicado silencioso).

Desde **P1.2b** los adjuntos también están dentro del vault: la imagen de cabecera se guarda en `_attachments/<año>/<mes>/header-<id>.<ext>`, su URL es la ruta del vault (`/files/_attachments/…`, servida con el tipo decidido por magic bytes y `nosniff`) y la nota la lleva en el frontmatter, así que **copiar la carpeta del vault es un backup completo** — `.md` + imágenes, y la copia arranca sola. Para los adjuntos que quedaron en el volumen viejo (`/app/attachments`) hay una migración de una vez, idempotente y con contadores. `GET /status` dice además la **deriva** entre índice y disco: cuántos ficheros han cambiado por fuera desde el último reindexado (comparando `mtime` + tamaño, sin leer cuerpos) y cuándo fue ese reindexado.

Desde **P8** el vault se vigila solo: un hilo compara cada `WATCH_INTERVAL` segundos (2 por defecto) la huella de cada `.md` (`mtime` + tamaño, sin leer cuerpos) con la que guarda el índice y reindexa **solo** los ficheros añadidos, modificados o borrados por fuera, avisando por `GET /events` (Server-Sent Events) con `{"type":"page","action":…,"page_id":…,"path":…}`. El panel escucha ese flujo: el árbol y los backlinks se refrescan solos, la nota abierta se recarga si no hay cambios sin guardar y, si los hay, **no se pisa nada**: sale un aviso con «Recargar» y «Descartar», y el autoguardado espera a que se decida. Un fichero ilegible no para el bucle (se registra y se reintenta en la pasada siguiente). `WATCH_ENABLED=0` apaga el vigilante y `WATCH_INTERVAL` cambia el ritmo; si el vault está en un volumen donde un `stat` es caro (un montaje de red), el log avisa de las pasadas lentas.

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

Lo que **no** cubre: que la app arranque contra los volúmenes de verdad, el panel en un navegador, ni ningún despliegue. Si un job falla, la ejecución queda en rojo en la pestaña *Actions* del repo, pero GitHub no impide el push por ello: para eso habría que marcar la comprobación como obligatoria en la protección de la rama, y eso se configura en el repo, no en este fichero.

Para reproducir el job de tests tal cual, con la misma versión:

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
pytest -q
```

Comprobado con CPython 3.12.16 (build de [python-build-standalone](https://github.com/astral-sh/python-build-standalone); cualquier `3.12.x` da lo mismo): el `pip install` sale limpio y `pytest -q` da `116 passed, 3 warnings`. Es la misma versión que fija `setup-python`, así que este verde y el del job significan lo mismo. Con el intérprete del contenedor de desarrollo (3.13/3.14) la suite también pasa, pero eso no dice nada del job.

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

Documentación interactiva de la API en [http://localhost:8000/docs](http://localhost:8000/docs) (con `DOCS_ENABLED` encendido, el valor por defecto). Panel visual en [http://localhost:8000/](http://localhost:8000/) (estado de `api`/`db` en la cabecera, árbol de páginas y bases de datos a la izquierda).

## Panel visual

- **Páginas**: `+` crea una nota (`Sin título`, lista para renombrar). Icono (emoji real, clic abre un selector con buscador), título, descripción opcional e imagen de cabecera opcional, todo en la propia cabecera de la nota, que ocupa todo el ancho — no hay una caja separada para el cuerpo; "Creado por" se fija solo una vez, al crearla. El editor es un único cuadro — no hay "modo edición" y "modo vista previa" separados: escribes Markdown y el propio formato aparece al momento (encabezados más grandes, negrita/cursiva reales, `->`/`<-`/`<->` como flechas de verdad, wikilinks con aspecto de enlace normal y vista previa al pasar el ratón), con el marcador de sintaxis (`#`, `**`, `[[`/`]]`) visible pero pequeño y discreto en el sitio donde lo escribiste, o el markdown en crudo si el cursor está justo ahí. `Ctrl/Cmd + clic` sobre un wikilink resuelto navega a esa página (a la nota o a la database, según lo que sea); backlinks en un desplegable arriba. Enlazar no obliga a recordar títulos: al escribir `[[` sale la lista de páginas filtrada por lo que sigas escribiendo (sin distinguir tildes), se elige con `↑`/`↓` y `Enter`, y la primera opción, «Crear «…»», crea la nota en el momento si ese título no existe y deja el enlace escrito. Todo se **autoguarda** 2s después del último cambio (el estado "Guardado"/"Guardando…"/"Cambios sin guardar…" con icono de progreso, arriba del todo); `Ctrl/Cmd + S` fuerza el guardado ya.
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
| `/login`       | GET    | Pantalla de entrada (HTML propio, sin dependencias): login o alta del primer usuario |
| `/auth/status` | GET    | `{needs_setup, authenticated}`: lo que la pantalla necesita para pintar el formulario |
| `/auth/setup`  | POST   | Alta del primer usuario (admin). **403** si ya hay usuarios |
| `/auth/login`  | POST   | `{username, password}` → cookie de sesión `HttpOnly`      |
| `/auth/me`     | GET    | Usuario de la sesión (`id`, `username`, `display_name`, `color`, `is_admin`) |
| `/auth/password` | POST | Cambia la contraseña propia (pide la actual) y cierra las demás sesiones |
| `/auth/users`  | GET    | Lista los usuarios (`as_dict()`, sin hashes). Solo un administrador |
| `/auth/users`  | POST   | Crea otro usuario. Solo un administrador                 |
| `/auth/logout` | POST   | Borra la sesión en la base y borra la cookie             |
| `/auth/tokens` | GET    | Tokens personales de captura del usuario (`id`, `name`, `created_at`, `last_used_at`), **sin el secreto** |
| `/auth/tokens` | POST   | Crea un token de captura y devuelve el secreto **una sola vez** |
| `/auth/tokens/{id}` | DELETE | Revoca un token de captura (404 si no es del usuario) |
| `/capture`     | POST   | Crea una nota en la bandeja (`Bandeja/`) desde un atajo o un script: `{text, title?, source}` → `{id, title, path}`. Sesión **o** token de captura |
| `/inbox`       | GET    | La bandeja: `{dir, count, pages}` para la sección «Bandeja (N)». Solo sesión |
| `/manifest.webmanifest` | GET | Manifiesto de la PWA (`application/manifest+json`) |

Salvo `/health`, `/login`, `/auth/login`, `/auth/setup`, `/auth/status`, `/manifest.webmanifest` y `/agent/...`, **todo lo de arriba pide sesión** (ver «Usuarios y acceso»): sin ella, `401 {"detail": "no autenticado"}` y, si la petición acepta HTML, un `303` al login.

Los wikilinks se escriben como `[[Título de la página]]` (o `[[Título\|Alias]]`) en `body_markdown`; se resuelven por título exacto (sin distinguir mayúsculas) dentro del mismo workspace al guardar la página. En el editor no hace falta teclearlos enteros: `[[` abre la lista de páginas y `Enter` completa.

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

### Agentes (`/agent/...`)

Un agente externo —un proceso que lee el vault y propone cambios— puede **leer contexto y proponer parches**, y nada más. Se identifica con un token propio, independiente de las sesiones de usuario de v1.3. La puerta está pensada para que un LLM trabaje sobre tus notas sin poder tocarlas: lo único que puede crear es una sugerencia pendiente, que aceptas o rechazas tú.

```bash
# En el `.env` del servidor (o en el entorno del contenedor). Pares
# token:nombre separados por comas. El nombre es el del agente y va DENTRO del
# token: el cuerpo de una petición no puede elegirlo.
AGENT_TOKENS="token1:agente-hitos,token2:agente-revision"
```

- **Sin `AGENT_TOKENS`** (o vacía) las tres rutas de `/agent` **no existen: 404**. No es un 403: un despliegue sin agentes no anuncia una superficie activa. (Con `DOCS_ENABLED` encendido, que es el valor por defecto, `/openapi.json` sí lista las rutas aunque respondan 404; `DOCS_ENABLED=0` apaga también eso.)
- El token viaja en la cabecera **`X-Agent-Token`**, nunca en la URL (una URL acaba en el log de acceso; una cabecera no). Sin token o con uno que no está configurado: **401**, con el mismo mensaje en los dos casos.
- La comparación es en tiempo constante (`hmac.compare_digest`) y se hace contra todos los tokens, sin cortar al primero que acierta.
- **Un token válido solo abre `/agent/...`.** Cualquier otra ruta (`/pages`, `/suggestions/{id}/accept`, `/trash`, `/auth/*`, …) con `X-Agent-Token` válido responde **403**: el token no es una llave para escribir, borrar ni aceptar. Lo que **no** hace es autenticar al dueño: desde v1.3 eso lo hace la sesión (`/auth/login`), y quien no la tenga no lee, no escribe y no borra.
- Los tokens **no se registran**: ni al acertar, ni al fallar, ni en los avisos de configuración. El log de una sugerencia creada dice agente, nota y tamaño del parche, jamás su texto: `INFO mdm.agents: sugerencia <id>: agente=agente-hitos nota=Notas/Hitos.md kind=edit parche=98 B`. Para que esa línea llegue al log del contenedor, el arranque pasa `--log-config logging.json` (ver [Logs](#logs)): sin él, uvicorn solo configura sus propios loggers y la app se queda sin `INFO`.
- Un nombre de agente es un nombre de fichero portable (letras, dígitos, `.`, `_` y `-`): se usa para buscar su nota de instrucciones. Una entrada de `AGENT_TOKENS` con un nombre raro se descarta con un aviso (sin el token) y las demás siguen valiendo.

| Endpoint | Método | Qué hace |
| --- | --- | --- |
| `/agent/context?page_id=<uuid>` o `?path=Notas/Algo.md` | GET | La nota, sus backlinks, sus wikilinks (y si resuelven), todas sus sugerencias con el hilo del dueño y el `brief` del agente, en una sola respuesta |
| `/agent/suggestions` | POST | Propone un parche (`kind=edit`) o una nota nueva (`kind=new_page`) sobre `page_id`. **413** si el parche pasa de 8 KB (32 KB en `new_page`), **429** si hay demasiadas sin resolver |
| `/agent/suggestions?status=…&since=<iso>` | GET | Las sugerencias **de ese agente**, con lo que el dueño decidió: `pending`/`accepted`/`rejected`/`orphaned`, y `since` para preguntar «¿qué ha pasado desde mi última visita?» |

Leer el contexto (lo que un agente pide antes de proponer nada):

```bash
curl -s -H "X-Agent-Token: token1" \
  "http://localhost:8000/agent/context?path=Notas/Hitos.md"
```

```json
{"page": {"id": "9f1c…", "title": "Hitos", "path": "Notas/Hitos.md",
          "type": "note", "updated_at": "2026-11-02T09:12:44.120931Z"},
 "body": "# Hitos\n\n- Cerrar P10a\n- Cerrar P10b\n",
 "body_truncated": false,
 "backlinks": [{"id": "3a77…", "title": "Proyecto"}],
 "links": [{"title": "Proyecto", "exists": true}, {"title": "Roadmap", "exists": false}],
 "suggestions": [{"id": "b41e…", "status": "rejected", "agent": "agente-hitos",
                  "kind": "edit", "old_text": "- Cerrar P10a",
                  "new_text": "- Cerrar P10a (hecho)", "rationale": "ya está",
                  "reason": "todavía no", "replies": [{"author": "Tú", "text": "cuando cierre el CI", "at": "2026-11-01T18:04:02.881400+00:00"}],
                  "created_at": "2026-11-01T17:50:00.100000Z", "decided_at": "2026-11-01T18:00:00.245000Z",
                  "page_id": "9f1c…", "path": "Notas/Hitos.md", "title": null}],
 "brief": "Propón hitos cortos y verificables. No cambies las fechas."}
```

- `body` se corta a **20 KB** (`AGENT_CONTEXT_MAX_BODY`) y, si se cortó, `body_truncated` lo dice: quien lo lee tiene que saber que no lo ha visto entero.
- `suggestions` incluye las **ya resueltas**: el motivo del rechazo (`reason`) y las respuestas del dueño (`replies`) son el canal de vuelta. Un agente que vuelve a mirar ve qué se aceptó, qué se rechazó y por qué.
- `brief` es el cuerpo de la nota **`Agentes/<nombre-del-agente>.md`** — las instrucciones que le hayas escrito a ese agente. Se busca en la raíz del vault y en `Notas/Agentes/` (donde nace si la creas desde la app); si no existe, `null`.

Proponer un parche:

```bash
curl -s -X POST http://localhost:8000/agent/suggestions \
  -H "X-Agent-Token: token1" -H "Content-Type: application/json" \
  -d '{"page_id": "9f1c…", "kind": "edit",
       "old_text": "- Cerrar P10b", "new_text": "- Cerrar P10b (hecho)",
       "rationale": "la tarjeta está cerrada", "offset": 40}'
```

El `agent` de la sugerencia **es el del token**: si el cuerpo manda `agent`, se ignora. `offset` es opcional y solo desempata cuando `old_text` aparece varias veces (se aplica en la más cercana). Un `old_text` que ya no está no es un error: la sugerencia nace `orphaned` y se puede responder. Para una nota nueva, `{"kind": "new_page", "title": "…", "new_text": "cuerpo markdown"}`.

Ver lo que el dueño decidió:

```bash
curl -s -H "X-Agent-Token: token1" \
  "http://localhost:8000/agent/suggestions?status=accepted&since=2026-11-01T00:00:00Z"
```

`since` mira la **última actividad** (crear, decidir o la última respuesta del dueño), no la fecha de creación: una sugerencia de anteayer con una respuesta de ayer es novedad de ayer.

**Topes.** `AGENT_MAX_PENDING_NOTE` (20) sin resolver por nota, de cualquier agente, y `AGENT_MAX_PENDING_AGENT` (20) sin resolver por agente en todo el vault. «Sin resolver» es `pending` **y** `orphaned`: una sugerencia cuyo ancla ya no está sigue esperando tu decisión, así que también ocupa sitio — si no, el agente que propone sobre un contexto viejo sería el que nunca toca el límite. Por encima, **429** con el número y el tope en el mensaje: un agente en bucle no puede llenarte una nota de ruido. Aceptar o rechazar libera sitio.

**Lo que un agente NO puede hacer**, y es deliberado: con el token puesto, no edita (`PATCH /pages/{id}`), no borra (`DELETE /pages/{id}`, `/trash`), no acepta ni rechaza sus propias sugerencias y no puede firmar como otro agente; el guardia responde **403** en cualquier ruta que no sea `/agent/...`. Aceptar y rechazar siguen siendo rutas del dueño. El token **no autentica al dueño ni sustituye a una sesión**: desde v1.3 las rutas del dueño piden sesión (`/auth/login`) y un `X-Agent-Token` válido fuera de `/agent/...` sigue dando **403**. Las decisiones de diseño están en [`docs/ADR-0004-puerta-para-agentes.md`](docs/ADR-0004-puerta-para-agentes.md).

## Usuarios y acceso

Desde v1.3 la API **no está abierta**: quien no tiene sesión no lee, no escribe y no borra. Usuarios y sesiones viven en el **índice** (`users`, `sessions`, esquema 4), no en el vault — el vault sigue siendo solo de las notas. El «creador» de cada nota deja de ser un literal: `created_by` es el `display_name` del usuario de la sesión, en el índice y en el frontmatter del `.md`. Las páginas que devuelve la API llevan además `created_by_color`: el color del usuario resuelto **en el servidor** contra `users` (una consulta por respuesta), o `null` si ese nombre no es de nadie (notas antiguas con «Tú»). Así el panel pinta el punto del autor sin que el navegador tenga que conocer la tabla de usuarios.

### Primer arranque

Con el índice sin usuarios, `GET /` lleva a `/login`, que pinta el **alta del primer usuario**. Ese usuario es administrador. Dos caminos:

1. **Desde la pantalla** (lo normal): abre `http://localhost:8000/` y rellena usuario, nombre visible y contraseña. El username es `snake_case` (minúsculas, dígitos y `_`, 3-32 caracteres, empezando por letra); la contraseña, 8+ caracteres.
2. **Desatendido**, con dos variables en el `.env` del servidor:

   ```bash
   ADMIN_USER=joaquin
   ADMIN_PASSWORD=algo-largo-y-unico
   ```

   Si al arrancar no hay ningún usuario, se crea ese administrador (su nombre visible es el propio `ADMIN_USER`). Con usuarios ya en el índice las dos variables **no hacen nada**: no se resetea la contraseña de nadie.

### Entrar, salir y cambiar la contraseña

```bash
curl -s -c cookies.txt -H 'Content-Type: application/json' \
  -d '{"username":"joaquin","password":"algo-largo-y-unico"}' \
  http://localhost:8000/auth/login

curl -s -b cookies.txt http://localhost:8000/auth/me
# {"id":"…","username":"joaquin","display_name":"Joaquín","color":"#5e6ad2","is_admin":true}

curl -s -b cookies.txt -X POST http://localhost:8000/auth/logout
```

- La cookie se llama `mdm_session` y es `HttpOnly` (el JS del panel no puede leer el token), `SameSite=Lax` y `Path=/`. `Secure` se añade cuando la petición llega por HTTPS, mirando `X-Forwarded-Proto` (lo que manda `tailscale serve`) o el esquema; `SESSION_COOKIE_SECURE=1`/`0` lo fuerza.
- En la base se guarda el **SHA-256 del token**, nunca el token: un volcado del índice no da sesiones abiertas.
- Duración: `SESSION_DAYS` (30) **deslizantes**, con tope absoluto de 90 días desde que se abrió (el empujón se escribe como mucho una vez por hora). El `Max-Age` de la cookie es el tope absoluto, no los 30 días: quien corta la sesión es `sessions.expires_at` en la base, así que un navegador no la tira antes de tiempo ni sobrevive a una sesión inactiva. `POST /auth/logout` borra la sesión en la base.
- `POST /auth/password` con `{"current_password": "…", "new_password": "…"}`: pide la actual, cierra las demás sesiones del usuario y deja abierta la que hace el cambio.
- Un usuario inexistente y una contraseña mala responden lo mismo (`401 {"detail": "usuario o contraseña incorrectos"}`) y cuestan lo mismo (se compara contra un hash falso para no filtrar por tiempo). Hay un límite de 10 intentos por minuto por usuario+IP, en memoria: por encima, `429`.

### Crear otro usuario

De momento solo un administrador: `GET /auth/users` lista (sin hash ni salt) y `POST /auth/users` crea:

```bash
curl -s -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"username":"marta","display_name":"Marta","password":"otra-larga","color":"#3dd68c"}' \
  http://localhost:8000/auth/users
```

`color` es opcional (`#rrggbb`; si falta se asigna uno estable por username) e `is_admin` es `false` salvo que se mande `true`. Las notas antiguas con `"Tú"` se quedan como están: la migración no reescribe el vault.

### Qué protege y qué no

Protege:

- **Todo salvo** `/health`, `/login`, `/auth/login`, `/auth/setup`, `/auth/status`, `/manifest.webmanifest` y `/agent/...`. Sin sesión: `401 {"detail": "no autenticado"}` para la API y `303` a `/login` cuando la petición acepta HTML. Navegar a `/` sin sesión no rompe nada: lleva a entrar y vuelve a donde ibas. El panel, si una llamada devuelve 401, salta a `/login`.
- **Las contraseñas** con `hashlib.scrypt` (n=2**14, r=8, p=1) y una sal por usuario, en hexadecimal. Sin dependencias nuevas.
- **Las sesiones** por token aleatorio de 256 bits cuyo hash es lo único que se guarda.

No protege, o no lo hace todavía:

- **No sustituye a la red.** Sigue haciendo falta Tailscale (o lo que pongas delante) y HTTPS: una contraseña que viaja por HTTP se lee. Con `tailscale serve` el TLS lo termina el proxy y la cookie sale `Secure`.
- **No hay permisos por nota**: cualquier usuario con sesión lee y escribe todo el vault; lo único «solo admin» es crear usuarios.
- **No hay 2FA ni recuperación de contraseña**. Si te quedas fuera y no hay otro admin, la salida es editar la base a mano o borrar el índice y volver al alta (las notas del vault no se tocan; las cuentas se pierden).
- **El índice contiene las credenciales.** El backup del vault no se lleva usuarios ni sesiones; si no usas `ADMIN_USER`, guarda también el índice o apunta las contraseñas.
- El límite de intentos es **por proceso y en memoria**: no sobrevive a un reinicio y no se comparte si algún día hay más de un worker.
- **Los agentes no son usuarios.** `/agent/...` sigue con su `X-Agent-Token`, que no es una sesión: no abre ninguna ruta del dueño (`403`) ni las de `/auth/*`.
- **Los tokens de captura tampoco.** Un token personal (P13) solo abre `POST /capture`: el resto de la API responde `403`, y no caducan solos — se revocan desde el panel. Viven en el índice, así que un backup del vault no se los lleva (y hay que apuntarlos si se quiere conservar el atajo).

## Capturar desde fuera (atajo global)

Apuntar algo tiene que costar dos segundos, desde cualquier sitio y en
cualquier sistema. `POST /capture` crea una nota en la **bandeja de entrada**
(`Bandeja/`, configurable con `CAPTURE_DIR`) y el panel la enseña arriba del
todo, en **«Bandeja (N)»**, hasta que se clasifique. El atajo de teclado global
lo pone el **sistema operativo**: la app solo da el endpoint y los scripts.

```bash
curl -s -X POST http://127.0.0.1:8000/capture \
  -H "Authorization: Bearer $MDM_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Comprar pan","title":"Recados","source":"api"}'
# {"id":"…","title":"Recados","path":"Bandeja/Recados.md"}
```

- `text` es obligatorio y tiene tope (`CAPTURE_MAX_BYTES`, 64 KB): una captura
  es un apunte, no un documento.
- `title` es opcional; si falta, `Captura 2026-11-02 14-33`. El `:` de la hora
  se escribe `-` porque **el título es el nombre del fichero** (ADR-0001) y
  Windows no admite `:` en un nombre.
- `source` es `shortcut`, `raycast`, `cli` o `api`.
- Si el título ya existe en la bandeja no falla: el vault resuelve la colisión
  con ` (2)`, ` (3)`…, como en cualquier otra nota.
- El `.md` lleva `captured_at`, `capture_source` y `captured_by` en el
  frontmatter, y se escribe por el **mismo camino** que todo lo demás:
  escritura atómica, índice en la misma transacción y aviso por `/events` (la
  pestaña abierta se entera sin recargar).

### El token de captura

El panel entra con la cookie de sesión, pero un atajo del sistema operativo no
tiene cookie. Para eso está el **token de captura** — y es del usuario, no del
agente: panel → menú del usuario → **«Tokens de captura»** → crear (el secreto
se enseña **una sola vez**), listar (con su `last_used_at`) y revocar. Por API,
con la sesión puesta (`cookies.txt` es el fichero de cookie de `curl`):

```bash
# crear (la respuesta trae el token, la única vez)
curl -s -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"name":"Atajo del Mac"}' http://localhost:8000/auth/tokens
# {"id":"…","name":"Atajo del Mac","created_at":"…","last_used_at":null,"token":"…"}

# listar (sin el secreto) y revocar
curl -s -b cookies.txt http://localhost:8000/auth/tokens
curl -s -b cookies.txt -X DELETE http://localhost:8000/auth/tokens/<id>
```

En la base queda el **SHA-256** del token, nunca el token, igual que en las
sesiones. Un token de captura **solo abre `POST /capture`**: cualquier otra
ruta contesta **403**, por el mismo motivo por el que un token de agente se
queda en `/agent/*` (un secreto guardado en un script no puede ser una llave
maestra del vault). No caduca solo: se revoca.

### Los tres atajos, en `tools/capture/`

Los tres hablan con el mismo endpoint, leen `MDM_TOKEN` (obligatorio) y
`MDM_URL` (por defecto `http://127.0.0.1:8000`), y **avisan** si el servidor no
responde o el token no vale — nunca fallan en silencio. Ninguno necesita nada
que no traiga su sistema.

| Sistema | Qué hay | Cómo se dispara |
| ------- | ------- | --------------- |
| **macOS** | [`macos/captura.md`](tools/capture/macos/captura.md), paso a paso | Un **Atajo de Shortcuts** con atajo de teclado **`⌘⇧N`** |
| **Windows** | [`windows/captura.ps1`](tools/capture/windows/captura.ps1) + [`windows/LEEME.md`](tools/capture/windows/LEEME.md) | Acceso directo al script + **PowerToys → Teclado** |
| **Linux** | [`linux/captura.sh`](tools/capture/linux/captura.sh) + [`linux/mdm-captura.desktop`](tools/capture/linux/mdm-captura.desktop) | Atajo de **GNOME** con `gsettings` |

**macOS.** Se crea un atajo en la app Atajos con **Recibir → Texto → Entrada
rápida**, una acción **Obtener contenido de URL** (POST a `/capture`, cabecera
`Authorization: Bearer …`, cuerpo JSON con `text` = la entrada y `source` =
`shortcut`) y una **notificación** con el título que devuelve el servidor. Los
nombres exactos de cada acción (en español y en inglés) están en
`tools/capture/macos/captura.md`; aquí no hay capturas de pantalla porque no
hay un Mac donde hacerlas.

**Windows.**

```powershell
setx MDM_TOKEN "el-token"          # variable de usuario; MDM_URL si no es localhost
# acceso directo: powershell.exe -NoProfile -Sta -ExecutionPolicy Bypass -WindowStyle Hidden -File "...\captura.ps1"
```

El script abre un cuadro de texto (WinForms) con **el portapapeles como valor
inicial**, hace el `POST` con `Invoke-RestMethod` en UTF-8 y avisa con un globo
de la bandeja (o un cuadro de error). El atajo se asigna de dos formas: la
«Tecla de método abreviado» de las propiedades del acceso directo, o
**PowerToys → Teclado → Remapear un atajo → Abrir una aplicación**. Detalle en
`tools/capture/windows/LEEME.md`.

**Linux.** `captura.sh` usa `zenity`, `kdialog` o `rofi` para pedir el texto y,
si no hay ninguno, lee de la entrada estándar (`printf 'idea' | captura.sh`),
hace el `POST` con `curl` (el JSON se escapa con `awk`, sin `jq`) y avisa con
`notify-send`/`zenity`/`kdialog`. Para el atajo, en GNOME:

```bash
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/captura/']"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/captura/ name 'Capturar en MyDamnManagement'
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/captura/ command "$HOME/.local/bin/captura.sh"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/captura/ binding '<Control><Alt>n'
```

### La bandeja, en el panel

La sección **«Bandeja (N)»** es la primera de la barra lateral y **solo sale si
hay algo** (nada de «Bandeja (0)»). Se despliega al pulsarla y lista las notas
de `Bandeja/`; el árbol de páginas no las repite. Cada una tiene **«Procesar»**:
un selector con las páginas existentes (las notas hacen de carpeta) o una
página nueva; al confirmar se hace el `PATCH` de `parent_id` de siempre, se
refresca el árbol y se abre la nota. Procesar una nota la saca de la bandeja y
la deja donde toque.

### Instalarlo en el Dock (PWA)

El panel es instalable: `frontend/manifest.webmanifest` (nombre, iconos
generados desde `logo.png` y `display: standalone`) y `/manifest.webmanifest`
se sirve con su tipo MIME. En Chrome (macOS o cualquier sistema) → menú → **«Añadir al Dock»** / «Instalar aplicación». **No hay service worker**: la app
necesita el servidor, así que no se promete funcionar sin conexión — lo que
hace el manifiesto es quitar la barra del navegador y darle un icono propio.

### Por qué no es una aplicación de escritorio

La app **no se distribuye como aplicación de escritorio** y no es un olvido: es
un servidor autoalojado (Docker, un vault de `.md` que se puede copiar, una
API) y su interfaz es una página. Un cliente nativo traería instalador por
sistema, firma y notarización en macOS, actualizaciones por cada plataforma y
un segundo sitio donde guardar estado — para acabar pidiendo los mismos datos a
la misma API. Lo único que de verdad hacía falta de un cliente (el atajo global
y un cuadro de texto) lo pone el sistema operativo con los scripts de arriba, y
el panel se instala como PWA si se quiere en el Dock. Menos piezas y ninguna
que pueda quedarse desincronizada del vault.

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
- [x] **v1.3 — usuarios, sesiones y login (P11a)** — esquema 4 con `users`/`sessions`, contraseñas con scrypt, cookie de sesión `HttpOnly` con caducidad deslizante, `/login` propio con alta del primer usuario, y la API cerrada a los anónimos (`401`/redirección) salvo `/health` y `/agent/*`
- [ ] **Fase 4** — Despliegue en Proxmox + Tailscale, backups automatizados

## Despliegue (producción, solo por Tailscale)

`docker-compose.yml` es el de **desarrollo**: monta `backend/app` y `frontend` encima del código que ya trae la imagen y arranca uvicorn con `--reload`. Para dejar esto corriendo en un servidor hay un fichero aparte, [`docker-compose.prod.yml`](docker-compose.prod.yml). No es un *override* del de desarrollo: es un compose completo y autónomo, se usa solo y nunca se combina con el otro.

| | desarrollo (`docker-compose.yml`) | producción (`docker-compose.prod.yml`) |
| --- | --- | --- |
| Código | bind-mount de `backend/app` y `frontend` | dentro de la imagen, sin montajes |
| Recarga | `--reload` | `--workers 1`, sin recarga |
| Puerto | `8000:8000` en todas las interfaces | `127.0.0.1:8000`, solo loopback |
| Volúmenes | código y datos | solo `mdm_data` y `mdm_vault` |
| Reinicio | `unless-stopped` | `unless-stopped` |
| Healthcheck | — | `GET /status`: falla si no se leen el índice o el vault, o si el índice está vacío con notas en el vault |
| Logs | `docker logs` | `docker logs`; rotación del driver de Docker (10 MB × 3), no de la app |

Un solo contenedor (`api`), sin Postgres y sin puerto 5432: el índice es un fichero SQLite y el vault son ficheros `.md`.

### Puesta en marcha

En el servidor (Docker con el plugin `compose` v2 y Tailscale instalado):

```bash
git clone <url-del-repo> MyDamnManagement
cd MyDamnManagement
git switch release/v1.0        # la rama/etiqueta que quieras desplegar
cp .env.example .env           # opcional: ADMIN_USER/ADMIN_PASSWORD; el resto lo fija el compose
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps     # api ... (healthy)
curl -s http://127.0.0.1:8000/health
```

- **Estado del healthcheck:** `docker inspect --format '{{.State.Health.Status}}' mdm_api_prod` (`starting` hasta que pasa el periodo de gracia del arranque, luego `healthy`). El motivo del último fallo, con texto: `docker inspect --format '{{json .State.Health}}' mdm_api_prod | python3 -m json.tool`. Está definido en el compose y, desde v1.3, no pregunta a `/status` (que como el resto pide sesión y el healthcheck no tiene credenciales): comprueba desde dentro del contenedor lo mismo — que la API contesta en `/health` y que el índice y el vault se leen de verdad — y sale con código 1 diciendo qué falla: `la API no responde en /health: …`, `el índice no se puede leer: …`, `el índice está vacío (0 páginas) pero hay N ficheros en el vault …: POST /reindex o reinicia el contenedor`, `no existe el vault … (¿volumen sin montar?)`, `el vault no se puede recorrer: …`.
  El del índice vacío es el caso traicionero: si el fichero del índice se pierde con la app en marcha, el índice se recrea vacío y la API sirve el panel en blanco (y las cuentas se pierden con él, ver «Usuarios y acceso»); por eso el chequeo exige que haya páginas cuando el vault tiene `.md`. Y como `restart: unless-stopped` **no** reinicia por `unhealthy`, el contenedor se quedaría así: la salida es `POST /reindex` o reiniciar. Un vault vacío de verdad (0 `.md`, 0 páginas) sigue dando `healthy`, y la deriva normal —un `.md` editado por fuera, `drift.modified`— **no** cuenta como caída: es lo esperado hasta el siguiente reindexado.
- **Actualizar:** `git pull && docker compose -f docker-compose.prod.yml up -d --build`. Cada arranque reindexa el vault entero, así que un cambio de código no necesita ningún paso extra sobre los datos.
- **No levantes los dos composes a la vez** en la misma máquina: los dos publican el puerto 8000 del host (el de desarrollo en todas las interfaces, el de producción en loopback) y el segundo se queda sin puerto.
- **Los volúmenes se llaman `mdm-prod_mdm_data` y `mdm-prod_mdm_vault`** porque el compose fija `name: mdm-prod`. No dependen del directorio desde el que clones, y así el backup y el restore de más abajo son comandos exactos y no "mira a ver cómo se llama".

### Exponerlo por Tailscale

La app **no** se publica en la red: el compose la deja en `127.0.0.1:8000`. Delante va `tailscale serve`, que termina TLS con el certificado del tailnet y le pasa las peticiones por loopback:

```bash
tailscale status                        # el servidor está en tu tailnet
sudo tailscale serve --bg http://127.0.0.1:8000
tailscale serve status                  # qué está publicado y en qué nombre
```

Se entra desde cualquier dispositivo del tailnet en `https://<equipo>.<tu-tailnet>.ts.net/`. Para quitarlo, `sudo tailscale serve reset`. Hace falta que el tailnet tenga MagicDNS y HTTPS activados (es lo que emite el certificado). Si tu versión de Tailscale es más antigua y no acepta esa forma, la corta es `sudo tailscale serve --bg 8000`; `tailscale serve status` dice cuál ha quedado.

Lo que esto protege y lo que no, sin adornos:

- **La frontera son dos capas: la red y la sesión.** Desde v1.3 la app pide login (ver «Usuarios y acceso»), pero el TLS y el alcance los sigue poniendo Tailscale: sin él, la contraseña del login viajaría en claro por la LAN, y con él la cookie sale `Secure` (`X-Forwarded-Proto: https`). Y el puerto ya no está abierto a la LAN ni a internet: solo llega quien esté en tu tailnet (y a quien tus ACLs dejen llegar a ese equipo).
- **Nunca `tailscale funnel`**: eso publica el servicio en internet. `serve` es para el tailnet; `funnel` no.
- Si compartes tailnet con más gente o con dispositivos que no son tuyos, restringe el acceso con ACLs de Tailscale (`tailscale.com/kb/1018/acls`) antes de meter notas de verdad.
- La alternativa que planteaba el roadmap —un token de API por cabecera— **no está implementada**. Añadirla es código de la app (una dependencia que revise la cabecera en cada router), no empaquetado, y este cambio no toca la app. Queda como deuda para cuando haya más de un usuario.

### `/docs` y `/openapi.json`

**En desarrollo siguen encendidas, y ese es también el valor por defecto** (`docs_enabled: bool = True` en `backend/app/config.py`): `backend/app/main.py` construye el `FastAPI` con `docs_url="/docs" if settings.docs_enabled else None` (y lo mismo con `redoc_url` y `openapi_url`), así que con el ajuste por defecto las tres rutas responden.

`DOCS_ENABLED=0` en el entorno (o en el `.env`) las apaga: `/docs`, `/redoc` y `/openapi.json` dan 404 y la app no publica el esquema OpenAPI. `docker-compose.prod.yml` ya lo trae a `"0"`, así que en producción no se exponen y en el `.env` del servidor no hace falta tocar nada.

Desde v1.3 `/docs`, `/redoc` y `/openapi.json` están detrás de la sesión como el resto de la API, así que apagarlas no cambia quién entra: sigue siendo buena idea en producción (menos superficie y menos esquema publicado), pero lo que protege es el login, no `DOCS_ENABLED`.

### Logs

uvicorn escribe a **stdout/stderr** (log de acceso y errores); la app no abre ningún fichero de log ni rota nada. Se leen con Docker:

```bash
docker compose -f docker-compose.prod.yml logs -f api
docker logs --since 20m mdm_api_prod
```

El compose fija el driver `json-file` con `max-size: 10m` y `max-file: 3` para que el registro no llene el disco del servidor: eso es rotación del driver de Docker, no de la aplicación.

Además del log de uvicorn está el de la app, con sus propios loggers `mdm.*` (`INFO mdm.main:` el resumen del reindexado, `WARNING mdm.main:` un índice ilegible, `INFO mdm.agents:` una sugerencia creada). El handler lo pone el **arranque**, no la app: los tres comandos de `docker-compose.yml`, `docker-compose.prod.yml` y el `CMD` de la imagen pasan `--log-config logging.json` (el fichero es `backend/logging.json`, dentro del contenedor en `/app/logging.json`). Sin ese fichero uvicorn no configura loggers ajenos y de la app solo saldría lo que pasa por el `lastResort` de `logging`, que es WARNING y superior — el `INFO` se tiraba. Si arrancas uvicorn a mano, pásale también `--log-config` para ver lo mismo que en producción.

### Backup

Lo que hay que respaldar es **el vault**: `mdm-prod_mdm_vault` tiene las notas (`Notas/`), las bases y sus filas (`Bases/`), los adjuntos (`_attachments/`) y la papelera (`.trash/`). El índice (`mdm-prod_mdm_data`, un `index.sqlite`) es **derivado**: se reconstruye desde el vault en cada arranque, así que no se respalda.

Copia con `tar` dentro del propio contenedor (misma imagen, sin instalar nada en el servidor):

```bash
cd /ruta/al/MyDamnManagement
mkdir -p backups
docker compose -f docker-compose.prod.yml run --rm -T --entrypoint tar \
    api -czf - -C /data/vault . > "backups/vault-$(date +%F).tar.gz"
tar tzf "backups/vault-$(date +%F).tar.gz" | head   # comprobar que no está vacío
```

`run` levanta un contenedor de un solo uso con los mismos volúmenes, así que funciona igual con el servicio levantado o parado. Se puede copiar en caliente: los `.md` y los adjuntos se escriben de forma atómica, de modo que lo peor es perder el último guardado, nunca un fichero a medias. Para programarlo, una línea de `cron` (aquí un ejemplo diario a las 03:30, en el `crontab` del usuario que puede usar Docker):

```cron
30 3 * * * cd /ruta/al/MyDamnManagement && docker compose -f docker-compose.prod.yml run --rm -T --entrypoint tar api -czf - -C /data/vault . > "backups/vault-$(date +\%F).tar.gz"
```

Si prefieres `rsync`, los volúmenes con nombre viven en `/var/lib/docker/volumes/mdm-prod_mdm_vault/_data` del host y `sudo rsync -a` de esa carpeta también vale; el `tar` de arriba evita depender de esa ruta y de los permisos de root.

### Restore

**Restaurar solo el vault** (el caso normal: el índice no hace falta):

```bash
cd /ruta/al/MyDamnManagement
docker compose -f docker-compose.prod.yml down        # sin -v: no borra volúmenes
docker volume rm mdm-prod_mdm_vault
cat backups/vault-2026-10-29.tar.gz | \
    docker compose -f docker-compose.prod.yml run --rm -T --entrypoint tar api -xzf - -C /data/vault
docker compose -f docker-compose.prod.yml up -d
```

`run` vuelve a crear el volumen vacío que se acaba de borrar. Al arrancar, la app reindexa el vault: **no hace falta `VAULT_ALLOW_EMPTY`**, que es el caso contrario (índice con notas y vault vacío: entonces el arranque se niega a vaciarlo). Con el vault restaurado y el índice vacío, nuevo o atrasado, el arranque lo reconstruye; si el índice se quedó con notas viejas, el vault manda y se reescribe.

Comprobación después del restore:

```bash
docker compose -f docker-compose.prod.yml ps                          # api ... (healthy)
curl -s http://127.0.0.1:8000/status
```

En el JSON de `/status`, `services.db.counts.pages` tiene que ser mayor que 0 y `drift.changed` tiene que ser 0 (si no lo es, `curl -X POST http://127.0.0.1:8000/reindex`). Después, abre el panel y mira que están tus notas.

**Restaurar también el índice** solo tiene sentido si guardaste `mdm-prod_mdm_data`, y entonces hay que parar el servicio antes de copiar (es un SQLite en WAL: copiarlo en caliente no es seguro). Para las notas no hace falta: el vault es la verdad y el índice se rehace. **Pero desde v1.3 el índice guarda los usuarios y las sesiones** (`users`, `sessions`), que no están en el vault: restaurar solo el vault deja la app sin cuentas (volverías al alta del primer usuario, o a `ADMIN_USER` si lo tienes en el `.env`). Si no quieres depender de eso, guarda también el índice o apunta las contraseñas.

### Lo que no está verificado aquí

Este cambio se ha hecho sin poder arrancar el contenedor ni tocar un tailnet: en la máquina donde se escribió no hay plugin `compose` (`docker compose` no existe como subcomando), el daemon de Docker está caído y no hay `tailscale`. Por tanto:

- `docker compose -f docker-compose.prod.yml config` se validó con el plugin `compose` **v2.29.7** durante la revisión de este cambio (`exit 0`: `host_ip: 127.0.0.1`, solo los volúmenes `mdm_data`/`mdm_vault`, ningún bind-mount de código). La máquina donde se escribió el compose no tiene el subcomando `compose`, así que conviene repetirlo en el servidor antes de `up`: es el primer comando que hay que correr allí.
- El comando del `healthcheck` **sí** está probado contra la app de verdad, pero no en Docker: el script se extrae del propio YAML y se ejecuta con el `python` de la imagen contra un `uvicorn` local. Con el healthcheck de v1.3 (que ya no usa `/status`, ver arriba) devuelve 0 con todo sano (`ok: índice v4, 1 páginas, 1 ficheros del vault`); 1 `el índice está vacío (0 páginas) pero hay 1 ficheros en el vault …` con un índice recién creado y una nota en el vault; 1 `no existe el vault …` cuando el directorio no está; 1 `el vault no se puede recorrer: …` cuando el vault (o una carpeta suya) no se deja leer; y 1 `la API no responde en /health: …` cuando no hay nada escuchando. Lo que **no** se ha podido comprobar aquí es que el `python` de la imagen tenga el módulo `sqlite3` (es de la biblioteca estándar, y la imagen ya lo usa para la app) ni el ciclo `healthy`/`unhealthy` de Docker.
- El `tar` que usan el backup y el restore es el de la propia imagen (`python:3.12-slim-bookworm`, base Debian): aquí no se ha podido comprobar que esté, por lo mismo. Si en tu imagen no estuviera, el mismo comando vale con un contenedor de usar y tirar: `docker run --rm -v mdm-prod_mdm_vault:/data/vault:ro -v "$PWD/backups":/backup busybox tar -czf /backup/vault.tar.gz -C /data/vault .`.
- `docker build` de la imagen, `docker compose up` y `tailscale serve` **no** se han ejecutado aquí.

## Licencia

[MIT](LICENSE)
