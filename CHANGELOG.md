# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). Versionado según [SemVer](https://semver.org/lang/es/): cada fase del roadmap avanza la versión menor (`0.x.0`) hasta que el proyecto se considere estable.

## [Sin publicar]

### Añadido

- **Editor con más pinta de Notion**: la prosa del editor pasa de fuente monoespaciada a la misma familia sans-serif del resto del panel (`--font-body`, la fuente del sistema en cada SO) -- el código (en línea y de bloque) se queda explícitamente en `--font-mono`, para que siga leyéndose como código.
- **Jerarquía de encabezados más marcada**: H1 pasa de 1.7em a 1.9em, H2 de 1.4em a 1.5em, H3 de 1.15em a 1.25em -- más diferencia real entre un título y un párrafo suelto, como en Notion.
- **Separación real entre bloques**: cada bloque lleva ahora un hueco por encima según su tipo (un H1 deja mucho más aire que un párrafo suelto; los ítems de una misma lista quedan juntos, sin hueco extra entre ellos) -- antes el único "espacio" entre bloques era la línea en blanco colapsada a ~5px, sin distinguir tipos. Viable sin miedo a que las flechas ↑/↓ vuelvan a saltarse bloques porque ya no dependen de la altura en píxeles de cada línea (ver más abajo).
- **Asa de bloque más grande y solo visible al pasar el ratón**: el `+`/`⋮⋮` de cada bloque pasa de 14px a 18px y de estar siempre visible (tenue) a invisible por defecto, apareciendo solo al pasar el ratón por esa línea -- salvo que el bloque esté seleccionado, que se queda visible igual (`:has()` sobre el propio asa).

### Corregido

- Vista previa de un wikilink al pasar el ratón, duplicada y una copia imposible de cerrar salvo recargando (issue [#1](../../issues/1)): un clic sin `Ctrl` sobre un wikilink resuelto lo sustituye por su marcado en crudo justo bajo el propio ratón, lo que el navegador interpreta como un `mouseover` nuevo sin que el hover anterior se haya cerrado -- `startLinkPreview()` creaba la tarjeta nueva sin quitar la que ya hubiera, dejándola huérfana en el DOM. Ahora cierra cualquier tarjeta previa antes de crear una.
- Las flechas `↑`/`↓` podían saltarse varios bloques de golpe -- en un caso llegaba a comerse casi toda una nota corta de un solo toque (issue [#2](../../issues/2)): CodeMirror calcula a qué línea saltar por distancia en píxeles, y con varias líneas colapsadas a ~5px seguidas (separadores entre bloques, línea horizontal) esa distancia abarcaba de sobra varias líneas colapsadas a la vez. Sustituidas por una navegación propia que se mueve por líneas *lógicas* del documento (actual ± 1) en vez de por píxeles, con memoria de la columna "objetivo" entre pulsaciones consecutivas (como cualquier editor de texto normal) -- inmune a cualquier cosa que colapse o esconda contenido, ahora o en el futuro.

## [0.9.2] — Imagen de `api` autocontenida (lista para Proxmox)

### Corregido

- `backend/Dockerfile` solo copiaba `backend/`; `frontend/` llegaba al contenedor exclusivamente por el bind-mount de desarrollo (`./frontend:/app/frontend:ro` en `docker-compose.yml`), que no existe fuera de esta máquina. Fuera de un entorno con ese bind-mount puesto (p. ej. un despliegue en Proxmox sin bind-mounts), `/app/frontend` se creaba vacío y el panel no se servía. El contexto de build de `api` pasa a ser la raíz del repo (`context: .`, no `./backend`) para que el Dockerfile pueda copiar `frontend/` (incluido `vendor/editor.bundle.js`, ya compilado) dentro de la imagen. En desarrollo no cambia nada — los bind-mounts se superponen a lo ya copiado, para seguir con recarga en caliente sin reconstruir.
- Añadido `.dockerignore` en la raíz (antes no hacía falta, el contexto era solo `backend/`): excluye `node_modules/` (incluido `frontend/editor-src/node_modules/`, ~15 MB que no pintan nada en la imagen), `__pycache__/`, `.venv/`, adjuntos/backups locales y `.env`.

### Verificado

- Build de la imagen con el contexto nuevo: `/app/frontend` dentro de la imagen trae `index.html`/`app.js`/`logo.png`/`vendor/editor.bundle.js` sin `node_modules`.
- Contenedor de prueba levantado desde la imagen SIN ningún bind-mount (`docker run`, no `docker compose`), conectado solo por red al `db` ya existente: `/`, `/health` y `/vendor/editor.bundle.js` responden 200 con el contenido real — simula exactamente el caso de Proxmox.
- Stack de desarrollo (`docker compose up -d --build`) reconstruido y probado de nuevo tras el cambio: panel, dashboard y hot-reload del frontend sin cambios de comportamiento.

## [0.9.0] — Editor al estilo Notion: comandos, iconos reales, autoformato

### Añadido

- **Menú `/` (comando de bloque)**: escribir `/` al principio de un bloque vacío abre un menú flotante con los tipos de bloque disponibles (párrafo `p`, títulos `h1`-`h5`, lista `list`, lista numerada `num`, código `code`, línea horizontal `horizontal`). Se filtra en vivo por lo que se escribe después de la `/` (prefijo de la abreviatura o substring de la etiqueta), se navega con `↑`/`↓`, se confirma con `Enter` o clic, se cancela con `Escape` dejando el texto tal cual.
- **Línea horizontal** (`---`) como bloque nuevo: seleccionable/convertible igual que el resto, se renderiza como una regla visual cuando el cursor no está en esa línea y vuelve a mostrarse como markdown editable al entrar en ella (mismo patrón cursor-aware que wikilinks y flechas).
- **`Mayús+Enter` crea un bloque nuevo**; `Enter` a solas sigue bajando de línea dentro del mismo bloque (comportamiento sin cambios, ahora explícitamente distinto del anterior).
- **Selector de emoji real** para el icono de página (`openEmojiPicker()` en `app.js`): rejilla con buscador por palabra clave, sustituye al campo de texto que había antes.
- **Autoformato tipográfico**: `->`, `<-`, `<->` se muestran como `→`, `←`, `↔` (igual que Obsidian/Notion) salvo dentro de un bloque de código o mientras el cursor está sobre ellos, momento en que vuelven a su forma editable en texto plano.
- **Wikilinks con aspecto de enlace normal**: un `[[Página]]` resuelto oculta los corchetes y se ve como un enlace de verdad, con una tarjeta de vista previa al pasar el ratón (contenido real de la página destino, con debounce de 350ms). Sigue siendo editable en markdown en cuanto el cursor entra en él.
- Logo de la app (`public/MDMLogo.png`, copiado a `frontend/logo.png`) como favicon y en la cabecera del panel.
- Indicador de guardado con icono de progreso (girando mientras `Guardando…`, marca al terminar) movido a la parte superior de la nota, junto con los backlinks ahora colapsados en un desplegable arriba (antes listados al final de la página).
- El cuerpo del editor ocupa todo el ancho de la nota, sin caja/borde propio — se siente como la propia página, no como un widget dentro de ella.

### Quitado

- Botón "Borrar página": redundante con el borrado por clic derecho ya disponible en el árbol de la barra lateral.

### Corregido

- Quitar la imagen de cabecera y subir otra inmediatamente después seguía mostrando la antigua hasta recargar: el mount `/files` no llevaba `Cache-Control: no-cache` (solo lo llevaban `app.js`/`index.html` desde la 0.6.0) — mismo nombre de archivo fijo por página (`header.<ext>`), así que el navegador la servía de caché sin revalidar. Añadido el mismo header ahí, más un `?t=timestamp` en el cliente para invalidar de raíz.
- La tarjeta de vista previa de un wikilink se quedaba en pantalla si se navegaba a otra página justo después de que apareciera (el hover no se limpiaba al cambiar de vista). `clearMain()` ahora también cierra cualquier preview y cualquier menú `/` abierto.
- Un wikilink a una página de tipo `database` (p. ej. `[[Tareas]]`) navegaba a una página inexistente: la navegación siempre llamaba a "abrir nota", nunca a "abrir database" — mismo tipo de bug que el de la 0.6.0 (dos UUID distintos para lo mismo, `Page.id` vs `DatabaseDef.id`). Nuevo `navigateToPage()` en `app.js` decide cuál de las dos según el tipo de nodo antes de navegar.
- Menú contextual de bloque: al abrirlo sobre un único bloque, ahora muestra su tipo actual (p. ej. "Título 1") como cabecera, antes del listado "Convertir a" — antes no había forma de saber qué tipo era el bloque seleccionado sin probar a convertirlo.

### Corregido / aprendido (CodeMirror)

- `Decoration.replace({..., block: true})` (necesaria para la línea horizontal) solo puede venir de un `StateField`, nunca de un `ViewPlugin` — CodeMirror lo rechaza con "Block decorations may not be specified via plugins". El plugin de la línea horizontal se reescribió como `StateField` (`hrField`).
- El menú `/` necesita `view.coordsAtPos()` para posicionarse junto al cursor, pero leer el layout del DOM de forma síncrona dentro de `ViewPlugin.update()` está prohibido ("Reading the editor layout isn't allowed during an update"). Probado primero con `requestAnimationFrame`: funciona en un navegador normal, pero no es la mejor opción — un frame de composición es más de lo que hace falta (solo hay que salir de la pila de la transacción en curso, no esperar a pintar). Cambiado a `setTimeout(fn, 0)`, que además es más robusto en pestañas en segundo plano.

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
