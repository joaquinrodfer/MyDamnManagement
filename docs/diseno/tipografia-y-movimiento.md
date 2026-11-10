# Tipografía, jerarquía y movimiento (10 nov 2026)

Documento de la tarjeta *«Diseño: tipografía sin serifa, jerarquía título/H1, iconos
centrados y fluidez»*. Recoge lo que se aplicó en `frontend/index.html` y
`frontend/app.js`, con las medidas tomadas del navegador antes y después.

Punto de partida, medido con `getComputedStyle` sobre una nota real:

| Elemento | Antes | Después |
| --- | --- | --- |
| Título de página `.note-title` | `"Cascadia Code"` **24 px** / 600 | pila del sistema **30 px** / 600 |
| H1 del cuerpo | pila del sistema **28,5 px** (1.9em) / 700 | **24 px** / 600 |
| H2 | 22,5 px (1.5em) / 700 | **20 px** / 600 |
| Párrafo | 15 px / 400 | 15 px / 400 |
| Título de base `.db-title` | `"Cascadia Code"` 22 px | pila del sistema 30 px |
| Cabecera de tabla `.rows-table th` | `"Cascadia Code"` 10,5 px | pila del sistema 11,5 px |

El defecto de fondo era doble: el título de la página iba en **monoespaciada**
mientras el cuerpo iba en sans (dos familias en la misma pantalla), y el H1 del
cuerpo (28,5 px) era **mayor** que el título de la página (24 px), así que el
documento parecía no tener título.

## 1. Una sola familia

```css
/* :root */
--font-body: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, "Helvetica Neue", Arial, sans-serif;
--font-mono: "Cascadia Code", "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
```

`--font-mono` queda **solo** para código y para el valor de un token:

- `.cm-mdm-code` (código en línea del editor)
- `#note-body .cm-mdm-line-code` (líneas de bloque de código)
- `.backlinks-help code`
- `.token-reveal input` (el secreto de un token de captura)

Todo lo demás que iba en mono pasó a `--font-body` sin cambiar tamaño ni
`letter-spacing`: cabecera de marca, indicadores `api`/`db`, chip de usuario,
bandeja («Procesar»), rótulos de sección de la barra lateral y del inicio,
Backlinks/Índice/Exportar, contador de sugerencias, `.mdm-context-menu-label`,
`.mdm-slash-menu-abbrev`, título de la previsualización de enlace, títulos de
backlinks, título de base, cabecera de tabla, cabecera de carril del tablero,
metadatos de tarjeta, `.cal-dow`, agenda del calendario, `.list-row-prop`,
`.field label`, `.user-admin-badge`, `.user-handle`, `.token-meta`.

Dos ajustes de tamaño que van con el cambio de familia, y solo esos:

- `.rows-table th` 10,5 → **11,5 px**: las versalitas de una sans pesan menos que
  las de una monoespaciada; a 10,5 px la cabecera se leía peor que antes.
- Los metadatos con números o fechas (`status-strip`, `dashboard-item-meta`,
  `sug-when`, `token-meta`) llevan ahora `font-variant-numeric: tabular-nums`,
  que es lo que aportaba la monoespaciada de forma gratis.

## 2. Escala y jerarquía

```css
/* :root */
--text-note-title: 30px;   /* título de página y de base */
--text-h1: 24px;
--text-h2: 20px;
--text-h3: 17px;
--text-h4: 15px;
--text-h5: 13px;

/* móvil (≤768 px), donde el cuerpo es 16 px */
--text-h4: 17px;
--text-h5: 15px;
```

Orden intocable: **título (30) > H1 (24) > H2 (20) > H3 (17)**. En móvil el
cuerpo sube a 16 px, así que H4/H5 se suben a 17/15: si no, un H4 de 15 px se
leería más pequeño que el texto que encabeza.

Pesos: 600 en todos los niveles (antes 700). Con una sola familia y estos
tamaños, el 700 de antes engordaba los H1/H2 y los hacía competir con el título.

- H4 deja de ir en versalitas: a 15 px, en mayúsculas competía con el texto
  normal en vez de leerse como encabezado.
- H5 sí las conserva (13 px, por debajo del cuerpo): es el nivel que hace de
  etiqueta, y ahí las versalitas con `letter-spacing: 0.04em` sí ayudan.

Espaciado entre bloques (`cm-mdm-space-*`, en `#note-body`):

| Bloque | Antes | Después |
| --- | --- | --- |
| normal | 6 px | 6 px |
| H1 | 34 px | **28 px + 4 px abajo** |
| H2 | 26 px | **22 px + 3 px abajo** |
| H3 | 20 px | **18 px** |
| H4 | 14 px | 14 px |
| H5 | 12 px | 12 px |

El aire de arriba queda en ~1,15 × el tamaño del encabezado. Los 4/3 px de
abajo son lo que impide que un H1/H2 se apoye en el párrafo siguiente (antes el
hueco de abajo lo ponía solo el `padding-top: 6px` del bloque siguiente).

**Acoplamiento que hay que respetar:** el asa de bloque (`+` / `⋮⋮`) se coloca
con `top:` igual al `padding-top` de cada nivel —
`#note-body .cm-mdm-space-h* > .cm-mdm-block-handle-wrap`. Si se cambia un
`padding-top`, se cambia el `top` del asa en el mismo commit, o el asa deja de
seguir al texto.

## 3. Iconos

- `.note-icon-btn` pasa de `text-align: center` + tamaño en `em` a
  `display: inline-flex; align-items: center; justify-content: center`, caja de
  **38×38** (lo que mide la primera línea del título: 30 px × 1.25) y
  `margin-top: 4px` (el mismo aire que respira el título).
- `.note-title-row` pasa de `align-items: baseline` a `flex-start`: el emoji ya
  no cuelga del baseline del título.
- `.note-icon-btn .svg-icon` → 24 px: el icono por defecto (SVG) tenía 20 px
  dentro de una caja de 38 al lado de un emoji de 26; ahora pesan igual.
- `.note-savestatus-icon .svg-icon` → 12 px: el ✓ medía 16 dentro de una caja de
  12 y se salía 2 px por cada lado.

Medido con `getBoundingClientRect()` (centro del SVG menos centro de su botón):

| Grupo | dx / dy máximo |
| --- | --- |
| Cabecera (`header .icon-btn`, 7 botones) | 0,00 / 0,00 px |
| Barra lateral (`#btn-new-note`, `.tree-row .type-icon`) | 0,00 / 0,00 px |
| Nota (`.note-icon-btn`, `.note-savestatus-icon`) | 0,00 / 0,00 px |
| Tabla (`.row-del`, `.cell-del`) | 0,00 / 0,00 px |

La caja del icono de página mide 38×38 y su centro queda a **+0,25 px** del
centro de la primera línea del título.

## 4. Movimiento

Duraciones y curva que ya existían en `:root` y que ahora usan todos los
elementos interactivos:

```css
--dur-fast: 120ms;    /* hover, foco, filas, botones, pestañas */
--dur-normal: 160ms;  /* apertura/cierre de diálogos, cambio de vista */
--ease-out: cubic-bezier(0.16, 1, 0.3, 1);
--ease-std: ease;
```

| Qué | Propiedad | Duración | Notas |
| --- | --- | --- | --- |
| `.btn`, `.view-tab`, `.template-btn`, `.tree-row`, `.search-hit`, `.dashboard-item`, `.inbox-item`, `.inbox-open`, `.list-row`, `.user-menu-item`, `.error-toast-close`, `.cal-event`, `.cal-nav .btn`, `.mdm-slash-menu-item`, `.mdm-context-menu-item` | `background-color`, `border-color`, `color` | 120 ms | `.btn` añade `filter` (el `brightness` del primario) |
| `.rows-table td`, `.rows-table th` | `background-color` | 120 ms | el hover tiñe la fila entera sin saltos de celda a celda |
| `.icon-btn`, `.board-col`, `.note-icon-btn` | `background-color`, `color`, `border-color` | 120 ms | |
| `.board-card` | `border-color`, `box-shadow` | 120 ms | se quitó `transform`: no había ninguno y no debe haberlo |
| `dialog` (abrir y cerrar) | `opacity`, `transform`, `overlay`, `display` | 160 ms | `translateY(-4px)` → `none`; `@starting-style` + `allow-discrete` para que también anime el cierre sin tocar el JS |
| `dialog::backdrop` | `background-color` | 160 ms | de `rgba(0,0,0,0)` a `rgba(0,0,0,.65)` |
| `.db-view.is-entering` (cambio de vista de una base) | `animation: mdm-view-in` | 160 ms | **solo opacidad**, sin transform: el layout no se mueve. Se aplica únicamente al cambiar de pestaña, no al repintar tras guardar |

Lo que **no** se anima, a propósito: el contenido de la nota al cargar (se lee
desde el primer fotograma), la aparición de la nota al navegar, y nada que use
`transform: scale` sobre texto.

El spinner de guardado (`--dur-spin: 700ms`) es la única animación por encima de
200 ms que queda: es un indicador de progreso, no una transición, y también se
apaga con `prefers-reduced-motion`.

## 5. `prefers-reduced-motion`

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation: none !important;
    transition: none !important;
    scroll-behavior: auto !important;
  }
}
```

Comprobado con `Emulation.setEmulatedMedia` (`prefers-reduced-motion: reduce` →
`matchMedia(...).matches === true`):

```
header .icon-btn -> transition: none 0s | animation: none 0s
.tree-row        -> transition: none 0s | animation: none 0s
dialog           -> transition: none 0s | animation: none 0s
```

## 6. Estados de los elementos tocados

| Elemento | hover | foco | activo / seleccionado |
| --- | --- | --- | --- |
| `.view-tab` | borde `--border-input`, texto `--ink`, fondo `--code-bg` | anillo `--accent` (`:focus-visible` global) | borde y texto de acento sobre `--accent-soft` |
| `.btn` | borde `--accent`, texto `--accent-ink` | anillo global | `.btn-primary` con `filter: brightness(1.1)` |
| `.note-icon-btn` | fondo `--code-bg` | anillo global | — |
| `.tree-row` | fondo `--code-bg` | anillo global | `.active` en `--accent-soft` |
| `.rows-table td` | fila entera en `--code-bg` | anillo interior en la celda | — |
| `dialog` | — | foco dentro (el nativo) | abierto: opacidad 1, sin desplazamiento |

## 7. Lo que queda abierto

- Las **tablas Markdown** del editor se siguen viendo como texto crudo (`| a | b |`):
  el editor no las pinta. Para la captura de «nota con tabla» se usó una base
  incrustada (` ```mdm-database `), que sí es una tabla real. No se toca aquí.
- Un bloque ` ```mdm-database ` **a mitad de documento** hace que el editor deje
  de pintar las líneas siguientes hasta que se desplaza (comprobado también con
  el `frontend/` sin tocar). Es comportamiento previo, no de esta tarjeta; queda
  anotado por si se quiere abrir tarjeta propia.
- El H5 va en versalitas: si al dueño no le gusta, es una línea
  (`.cm-mdm-h5 { text-transform: none; letter-spacing: 0 }`).
