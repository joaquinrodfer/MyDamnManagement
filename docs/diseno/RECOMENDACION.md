# Recomendación de Diseño y Coste de Implementación

## 1. Cuál recomiendo: **Dirección A («Cuaderno de Archivo»)** — con la compactación de tablas y formularios de la **Dirección B**

Mi recomendación para MyDamnManagement es elegir la **Dirección A («Cuaderno de Archivo»)** como piel principal del producto (con sus dos temas claro/oscuro basados en papel cálido y piedra, jerarquía tipográfica con serifas del sistema en títulos de página y el lienzo de lectura más luminoso que la barra lateral), **incorporando el diseño compacto de tablas y formularios en rejilla de la Dirección B**.

### Por qué es la mejor decisión para el producto

1. **Encaja con la naturaleza real de MyDamnManagement ("todo es un fichero `.md` en tu disco"):**
   - A diferencia de un clon corporativo frío de Jira/Linear, el corazón de MyDamnManagement es un *vault* de notas Markdown con bases de datos acopladas.
   - La **Dirección A** hace que abrir una nota se sienta como escribir en un cuaderno bien editado (`max-width: 720px`, `16px`, `line-height: 1.72`, títulos en `Georgia`/`Palatino`), mientras que las tablas y tableros Kanban ganan orden gracias a las etiquetas tipo pastilla y los carriles delimitados.
2. **Resuelve de raíz la inversión de jerarquía actual:**
   - Hoy (`release/v1.0`), la barra lateral es blanca (`#ffffff`) y el documento es gris frío (`#eff1f4`), por lo que la barra lateral compite con la nota. En la **Dirección A**, la barra lateral pasa a un tono arena silencioso (`#f2ede4` en claro / `#171513` en oscuro) y el documento abierto es la superficie protagonista (`#fbf9f5` / `#1f1c19`).
3. **Mantiene la continuidad con el acento cálido original sin sus fallos de contraste:**
   - La app actual ya intentaba usar un tono tierra/cobre (`#b8763a`), pero fallaba el contraste WCAG AA en botones primarios (**3.69:1** en claro y **2.38:1** en oscuro). La **Dirección A** conserva esa identidad cálida evolucionándola a terracota tostado (`#9e4722` con **5.89:1** en claro y `#e07b4f` con texto oscuro `#171513` a **6.92:1** en oscuro).
4. **Honestidad sobre las otras dos opciones:**
   - **¿Cuándo elegir la Dirección B («Mesa de Operaciones»)?** Si el uso principal del dueño va a ser **80% bases de datos, CRM y tableros Kanban en modo oscuro** y solo 20% notas cortas. En ese escenario, la densidad de `12.5px–13px` y el acento índigo de la Dirección B aprovechan mejor el espacio vertical.
   - **¿Cuándo elegir la Dirección C («Tablero Suizo»)?** Si se busca una **pieza de autor con personalidad gráfica radical** y contraste máximo (WCAG AAA en casi toda la pantalla). Es la más llamativa en una captura, pero sus bordes negros de `1.5px–2px` y sombras duras cansan más en sesiones largas de escritura.

---

## 2. Qué costaría aplicar cada dirección (archivos y commits)

Independientemente de la dirección visual elegida, **ninguna toca `backend/`, ni el esquema SQLite, ni los endpoints, ni `frontend/editor-src/entry.js`** (el bundle de CodeMirror solo emite clases `.cm-mdm-*` cuyo aspecto se controla íntegramente desde `frontend/index.html`).

Sin embargo, **arreglar los problemas graves de usabilidad de la auditoría** (navegación con `Tab`, colisión del índice flotante con `Backlinks` y formulario de nueva fila en rejilla) sí requiere tocar `frontend/app.js` con cuidado para no romper ninguno de los tests existentes.

### Comparativa de coste

| Dirección | Archivos que se tocan | Líneas estimadas | Commits razonables | Riesgo técnico |
| :--- | :--- | :--- | :--- | :--- |
| **Dirección A** *(Cuaderno de Archivo)* | `frontend/index.html`<br>`frontend/app.js` | ~220 líneas CSS en `index.html`<br>~45 líneas JS en `app.js` | **4 commits** | **Bajo.** Usa las mismas estructuras DOM actuales; solo añade contenedor de ancho de lectura y atributos de teclado. |
| **Dirección B** *(Mesa de Operaciones)* | `frontend/index.html`<br>`frontend/app.js` | ~260 líneas CSS en `index.html`<br>~75 líneas JS en `app.js` | **5 commits** | **Bajo–Medio.** Para que las celdas `select`/`multiselect` se vean como pastillas coloreadas (`.pill`) sin romper `paintCell`/`editCell`, hay que ajustar el renderizado de celdas en `app.js`. |
| **Dirección C** *(Tablero Suizo)* | `frontend/index.html`<br>`frontend/app.js` | ~250 líneas CSS en `index.html`<br>~55 líneas JS en `app.js` | **4–5 commits** | **Medio.** Los bordes de `2px` y sombras desplazadas (`box-shadow: 3px 3px 0`) exigen ajustar márgenes y `overflow` en contenedores con scroll (`.table-wrap`, `.board`) para que no se recorten las sombras. |

---

## 3. Plan de ejecución recomendado (en 4 paquetes/commits pequeños)

Sea cual sea la dirección que elija el dueño, propongo aplicarla en este orden estricto para que cada commit deje los 116 tests en verde y sea fácil de revisar:

1. **Commit 1 — Tokens, contraste WCAG AA y áreas de toque (`frontend/index.html`):**
   - Actualizar `:root` y `@media (prefers-color-scheme: dark)` con la paleta elegida (corrigiendo `.btn-primary` de `3.69:1` / `2.38:1` a `> 5.4:1`, bordes de inputs a `>= 3.0:1` y `.cm-mdm-mark` sin opacidad ilegible).
   - Subir `.icon-btn`, `.row-del` y `.error-toast-close` de `20×20 px` / `19×17 px` a `28×28 px` con separación segura.
2. **Commit 2 — Fin del solapamiento del índice y ergonomía de lectura (`frontend/index.html` + `frontend/app.js`):**
   - Integrar `.note-outline-dropdown` junto a `.note-backlinks-dropdown` en `.note-topbar-right` (o desplazar su anclaje debajo de `.note-topbar`) para que nunca vuelva a tapar el botón `⇩` ni `Backlinks` a `1280 px` ni a `1024 px`.
   - Acotar el ancho de lectura de la nota (`max-width: 720px`) y hacer que el botón `+ Imagen de cabecera` sea discreto cuando no hay portada.
3. **Commit 3 — Bases de datos: tabla, Kanban con carriles y formulario compacto (`frontend/index.html` + `frontend/app.js`):**
   - Dar contenedor visual a `.board-col` (incluyendo estado visual al arrastrar con `dragover`), jerarquía a la primera columna de `.rows-table` y pastillas legibles en tarjetas.
   - Convertir `.inline-form` ("+ Nueva fila") en una rejilla compacta horizontal y ampliar `dialog` a `min(520px, 92vw)` con scroll interno en `#prop-rows`.
4. **Commit 4 — Accesibilidad de teclado completa (`frontend/app.js` + `frontend/index.html`):**
   - Añadir `tabindex="0"`, `role` y activación con `Enter`/`Espacio` en `.tree-row`, `.dashboard-item`, `.board-card`, `.note-outline-item`, `.backlink-item`, `.search-hit` y celdas `<td>` de `.rows-table`.
   - Añadir `:focus-within` en `.cm-mdm-block-handle-wrap` para que los controles de bloque (`+` y `⋮⋮`) no dependan exclusivamente del ratón.
