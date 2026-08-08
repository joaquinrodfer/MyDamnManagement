# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). Versionado según [SemVer](https://semver.org/lang/es/): cada fase del roadmap avanza la versión menor (`0.x.0`) hasta que el proyecto se considere estable.

## [Sin publicar]

## [0.8.0] — Edición por bloques + selección múltiple

### Añadido

- **Bloques en el editor**: cada párrafo/`H1`-`H4`/ítem de lista/bloque de código se calcula a partir del árbol de sintaxis de `@lezer/markdown` (`computeBlocks()` en `entry.js`) — no es un modelo de datos nuevo, `body_markdown` sigue siendo el mismo string de siempre. Cada bloque tiene un asa `⋮⋮` (widget posicionado en el margen izquierdo de su línea, no un gutter — ver la nota técnica más abajo).
  - Clic: selecciona el bloque. `Mayús`+clic: selecciona un rango. `Ctrl`/`Cmd`+clic: añade/quita uno suelto.
  - Clic derecho: menú con **Convertir a** (párrafo, H1-H4, lista, lista numerada, código) y **Eliminar**, aplicado a todos los bloques seleccionados a la vez.
- **Selección múltiple de páginas** en el árbol de la barra lateral: mismos atajos (`Ctrl`/`Mayús`+clic), clic derecho para eliminar varias de una vez. Distingue página `note` (`DELETE /pages/{id}`) de página `database` (`DELETE /databases/{database_id}`) automáticamente.
- Menú contextual genérico (`showContextMenu()` en `app.js`) compartido entre bloques y páginas.

### Corregido / aprendido

- La primera implementación de las asas de bloque usaba un `gutter()` de CodeMirror, igual que `lineNumbers()`. No se alineaba con el contenido: se comprobó que ni siquiera el propio `lineNumbers()` nativo alineaba bien contra líneas de altura variable (los encabezados son más altos que un párrafo normal) — los gutters de CM6 asumen altura de línea uniforme. Solución: el asa es un `Decoration.widget` colocado dentro de la propia línea y sacado al margen con `position: absolute` — al ser hijo real de esa línea, hereda su altura exacta sin cálculo aparte.
- Verificado con medidas reales (`getBoundingClientRect`), no solo inspección visual: con el gutter, el desfase entre el asa y su línea llegaba a acumular >60px en documentos con encabezados; con el widget, alineación exacta (diferencia 0px) en los 6 bloques de un documento de prueba con encabezado, párrafo, lista y código.

### Documentado

- `docs/ARCHITECTURE.md`: comparación completa entre "bloques de verdad" (modelo de datos nuevo) y "bloques calculados" (el enfoque elegido), y por qué.

## [0.7.0] — Metadatos de página, autoguardado, caret visible

### Añadido

- `page` gana tres columnas: `created_by` (se fija solo al crear, pensado para cuando haya espacios grupales/multiusuario), `description` y `header_image_path` — todas opcionales a nivel de base de datos; la API siempre rellena `icon`/`created_by` al crear una página, así que a nivel de producto son "obligatorias" sin forzar una migración de backfill sobre datos existentes.
- `POST/DELETE /pages/{id}/header-image`: sube (`multipart/form-data`) o quita la imagen de cabecera de una página; valida tipo de contenido y tamaño (8 MB máx.), sustituye cualquier cabecera anterior en vez de acumular archivos.
- `/files/...`: monta `/app/attachments` como estáticos para servir lo subido.
- Panel: icono editable (clic sobre el emoji), campo de descripción, imagen de cabecera con subida/borrado, línea "Creado por X" en la nota.
- **Autoguardado**: sin botón "Guardar" — cualquier cambio (título, icono, descripción, cuerpo) programa un guardado 2s después de la última pulsación; si llegan más cambios antes, se reinicia el plazo. Al navegar a otra página o cerrar la pestaña, cualquier guardado pendiente se fuerza antes (no se pierden los últimos <2s de cambios). `Ctrl/Cmd+S` sigue disponible para forzarlo al momento.
- El árbol de páginas ahora muestra el icono real de cada página (antes siempre el genérico 📄/🗄, ignorando `icon`).

### Corregido

- El caret del editor de notas se veía negro en tema oscuro: CodeMirror no dibuja un cursor propio a menos que se incluya la extensión `drawSelection()` (no la teníamos), así que el cursor real era el nativo del navegador — y ese sigue `caret-color`, no las reglas `.cm-cursor` que habíamos escrito. Añadido `caret-color: var(--ink)` en `#note-body .cm-content`.

### Config

- `DEFAULT_USER_NAME` (`.env`): nombre que se asigna como creador de cada página nueva mientras no haya login. Hay que recrear el contenedor `api` (`docker compose up -d api`) tras cambiarlo — las variables de entorno no se recogen con el hot-reload de `--reload`, solo al crear el contenedor.

## [0.6.0] — Editor con formato en vivo + correcciones de navegación

### Corregido

- **Bug real:** el árbol (`GET /pages/tree`) devolvía el id de la `page` para los nodos de tipo `database`, pero el frontend necesitaba el id de la fila `databases` (son dos UUID distintos) para pedir `/databases/{id}`. Cada clic en una base de datos desde la barra lateral o desde resultados de búsqueda fallaba con un 404 silencioso — causa probable de "no consigo ver las bases de datos". `GET /pages/tree` ahora incluye `database_id` en cada nodo de tipo `database`; el frontend lo usa para navegar.
- Los archivos estáticos (`app.js`, `index.html`, ahora también el editor) no llevaban cabecera de caché, así que el navegador podía servir una versión vieja sin avisar. `NoCacheStaticFiles` (en `main.py`) añade `Cache-Control: no-cache` — sigue siendo barato (revalida por ETag) pero nunca sirve una copia obsoleta sin preguntar.

### Añadido

- **Editor de notas con formato en vivo** (CodeMirror 6): un único cuadro para escribir y ver el resultado, no cuadro de texto + panel de vista previa separados. `#`/`##`/`###`, `**negrita**`, `*cursiva*` y `[[wikilinks]]` se renderizan al momento; los marcadores de sintaxis quedan visibles pero pequeños y discretos justo donde se escribieron. `Ctrl/Cmd+clic` en un wikilink resuelto navega; `Ctrl/Cmd+S` guarda.
- `frontend/editor-src/`: fuente del editor (paquetes `@codemirror/*` + `@lezer/highlight`), compilado una vez con esbuild a `frontend/vendor/editor.bundle.js` (vendorizado, commiteado, no depende de ningún CDN en tiempo de ejecución). Única pieza del frontend con build — el resto sigue siendo HTML/JS plano.
- `frontend/app.js` pasa a cargarse como módulo ES (`<script type="module">`) para poder importar el bundle del editor.

### Decisión

- Se planteó migrar todo el frontend a React + Vite. Se descartó por ahora: el problema real (que el editor fuera bueno) lo resuelve una librería de edición dedicada, no el framework — React no habría simplificado la gestión de cursor/selección del editor. Se optó por el punto intermedio: vanilla JS sin build para el resto de la app, CodeMirror vendorizado solo para el editor. Revisable si el resto de la interfaz (diálogos, listas, formularios) empieza a pesar demasiado para mantenerse a mano.

## [0.5.0] — Fase 3: plantillas de CRM y Tareas

### Añadido

- `backend/app/templates.py`: plantillas `crm` ("Contactos": empresa/fase/valor/próximo contacto) y `tasks` ("Tareas": estado/prioridad/fecha límite/proyecto), cada una con dos vistas por defecto (`board` agrupado + `table`). Son datos, no motor nuevo — pasan por las mismas operaciones de creación de la Fase 2.
- `GET /databases/templates`: lista las plantillas disponibles.
- `POST /databases/from-template`: crea database + schema + vistas en una sola llamada (`title` opcional, si no se pasa usa el de la plantilla).
- Panel visual: botones **CRM** / **Tareas** de un clic junto a "Bases de datos". Título de la database ahora editable (antes solo se fijaba al crearla) — hacía falta para poder renombrar lo que sale de una plantilla.

### Verificado

- De extremo a extremo en el navegador: clic en **CRM** → database con 4 propiedades y 2 vistas ya creadas, tablero con las 5 columnas de `fase` a 0; clic en **Tareas** → mismo patrón con su propio schema. Renombrado de título probado (persiste y se refleja en el árbol). `POST /databases/from-template` con una plantilla inexistente devuelve 404.

## [0.4.0] — Panel visual (adelanto de Fase 4)

### Añadido

- `frontend/index.html` + `frontend/app.js`: interfaz completa sobre las Fases 0–2, sin build ni dependencias (fetch directo a la API del propio origen).
- Árbol de páginas y bases de datos en la barra lateral, con búsqueda (`/search`) con debounce.
- Editor de notas: título, cuerpo Markdown, vista previa con `[[wikilinks]]` resueltos en vivo contra el árbol cargado en cliente, y lista de backlinks navegable.
- Diálogo de creación de `database`: propiedades dinámicas (clave/nombre/tipo, opciones para `select`/`multiselect`).
- Vista de `database`: pestañas por vista (`table`/`board`/`calendar`/`list`), tabla o tablero según el tipo activo, formulario de nueva fila que genera el input correcto por tipo de propiedad.
- `GET /pages/tree` ahora excluye `type=database_row`: las filas se ven dentro de su database, no como nodos sueltos del árbol de navegación.

### Verificado

- De extremo a extremo en el navegador (no solo `curl`): nota con wikilink sin resolver → creación de la página destino → backlink real tras volver a guardar; CRM completo (3 filas, vista board agrupada por Fase con conteos correctos por columna).

## [0.3.0] — Fase 2: motor de `database` + `view`

### Añadido

- `POST/GET/PATCH/DELETE /databases`: una `database` es una página + `schema_def` (lista de propiedades tipadas: `text`, `number`, `select`, `multiselect`, `date`, `checkbox`, `relation`, `url`).
- `properties.py`: valida `properties` de una fila contra el `schema_def` de su database (claves conocidas, tipo básico, opciones de `select`/`multiselect`) — 400 con mensaje claro si no cumple.
- `POST/GET/PATCH/DELETE /databases/{id}/rows`: filas (`Page` con `type=database_row`). `PATCH` hace merge parcial de `properties`, no reemplazo completo.
- `POST/GET/PATCH/DELETE /databases/{id}/views`: vistas (`table`/`board`/`calendar`/`list`) con `config` de filtros + orden, aplicados por el backend en `GET /rows?view={id}`; `group_by` y `visible_properties` quedan como metadato para el frontend.
- Verificado creando dos dominios reales con el mismo motor y cero código específico: un CRM ("Contactos": empresa/fase/valor) y un gestor de tareas ("Tareas": estado/prioridad/hecha), incluida una vista board filtrada y ordenada.

### Corregido

- `DatabaseDef` no tenía la relación ORM hacia `Page` (`db_def.page` no existía) — provocaba un 500 en `POST /databases` después de haber confirmado la escritura en base de datos (el commit ya había ocurrido; solo fallaba el serializado de la respuesta), dejando páginas de database huérfanas sin que el cliente lo supiera. Al añadir la relación hubo que desambiguar con `foreign_keys=[page_id]` porque `pages` y `databases` se referencian mutuamente.

## [0.2.0] — Fase 1: notas/wiki

### Añadido

- Alembic: migraciones versionadas, con `alembic upgrade head` en el arranque del contenedor `api`. Sustituye al `create_all()` de la Fase 0.
- `GET /pages/tree`: árbol de páginas por `parent_id`.
- Wikilinks `[[Título]]` / `[[Título|Alias]]`: se resuelven al guardar (`app/wikilinks.py`) y generan `Link` reales entre páginas.
- `GET /pages/{id}/backlinks`: páginas que enlazan a una página dada.
- `GET /search?q=`: búsqueda full-text (Postgres `tsvector`, configuración `spanish`) sobre título + cuerpo.

### Corregido

- La migración inicial autogenerada por Alembic fallaba al aplicarse: `pages` y `databases` se referencian mutuamente y autogenerate no resuelve ese ciclo por sí solo. Se separó la FK circular (`databases.page_id -> pages.id`) en un `create_foreign_key` aparte, después de crear ambas tablas.

## [0.1.1] — Panel de estado

### Añadido

- Panel de estado (`frontend/index.html`), servido por la propia API en `/`, con sondeo cada 4s del nuevo endpoint `GET /status` (comprueba `api` y conectividad real a `db`).

## [0.1.0] — Fase 0: esqueleto

### Añadido

- Docker Compose con `api` (FastAPI + Uvicorn) y `db` (Postgres 16).
- Modelo de datos completo: `workspace`, `page`, `page_content`, `databases`, `view`, `link`, `tag`, `attachment`.
- CRUD de `/pages` (GET, POST, PATCH, DELETE) probado de extremo a extremo.
- Documentación de arquitectura con diagramas ([`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)).
