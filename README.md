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

El propio contenedor `api` aplica las migraciones de Alembic (`alembic upgrade head`) antes de arrancar Uvicorn — no hay que hacer nada aparte. Si generas una migración nueva tras cambiar `backend/app/models.py`:

```bash
docker compose run --rm api alembic revision --autogenerate -m "descripción del cambio"
docker compose run --rm api alembic upgrade head
```

**Revisa siempre el archivo generado antes de aplicarlo.** `pages` y `databases` se referencian mutuamente, y autogenerate no resuelve bien ese ciclo (avisa con un `SAWarning` y puede ordenar las tablas de forma que la migración falle al aplicarse). Es exactamente lo que pasó en la migración inicial — se corrigió a mano separando la FK circular con `create_foreign_key`; usa esa migración como referencia si vuelve a pasar.

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
| `/status`      | GET    | Estado agregado de `api` y `db` (usado por el panel)     |
| `/pages`       | GET    | Lista todas las páginas del workspace                   |
| `/pages`       | POST   | Crea página + `page_content` en una transacción         |
| `/pages/{id}`  | GET    | Lee una página con su cuerpo                             |
| `/pages/{id}`  | PATCH  | Actualiza título, icono, descripción o cuerpo             |
| `/pages/{id}`  | DELETE | Borra la página (cascada a contenido/adjuntos/etiquetas) |
| `/pages/tree`  | GET    | Árbol de páginas por `parent_id`                        |
| `/pages/{id}/backlinks` | GET | Páginas que enlazan a esta vía `[[wikilink]]`     |
| `/pages/{id}/header-image` | POST | Sube/reemplaza la imagen de cabecera (`multipart/form-data`, campo `file`) |
| `/pages/{id}/header-image` | DELETE | Quita la imagen de cabecera                       |
| `/files/...`   | GET    | Sirve los archivos subidos (imágenes de cabecera, etc.)  |
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
- [ ] **Fase 4** — Despliegue en Proxmox + Tailscale, backups automatizados

## Despliegue objetivo

Desarrollo en Windows con Docker Desktop; a producción sobre un LXC/VM de Proxmox personal, accesible solo a través de una red [Tailscale](https://tailscale.com/) (sin exponer puertos públicos, sin gestión de cuentas por ahora — uso personal en solitario). El stack de contenedores no cambia entre ambos entornos.

**Pendiente antes de ese despliegue:** `backend/Dockerfile` solo copia `backend/`; hoy `frontend/` llega al contenedor por el bind-mount de desarrollo (`./frontend:/app/frontend:ro` en `docker-compose.yml`), que no existirá en Proxmox si se despliega sin bind-mounts. Hay que decidir cómo empaquetar `frontend/` (incluido `vendor/editor.bundle.js`) dentro de la imagen antes de la Fase 4 — anotado aquí para no perderlo de vista, se resuelve cuando toque el despliegue.

## Licencia

[MIT](LICENSE)
