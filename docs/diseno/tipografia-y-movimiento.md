# Tipografía, jerarquía y movimiento (10 nov 2026)

Documento de la tarjeta *«Diseño: tipografía sin serifa, jerarquía título/H1, iconos
centrados y fluidez»*. Recoge lo que se aplicó en `frontend/index.html` y
`frontend/app.js`, con las medidas tomadas del navegador antes y después.

Sustituye, en lo que toca a **tipografía y movimiento**, a lo que dicen los
tokens de `SPEC-B.md` §2 (`--text-db-title`, el título de base a 22 px, el H1 a
1.9em...). `SPEC-B.md` se queda como histórico de la fase B; este documento es
la referencia vigente de la escala, de las familias y de las duraciones.

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
/* :root — escritorio */
--text-note-title: 30px;   /* título de página */
--text-db-title: var(--text-note-title);   /* título de base: misma escala */
--text-h1: 24px;
--text-h2: 20px;
--text-h3: 17px;
--text-h4: 15px;
--text-h5: 13px;

/* :root dentro de @media (max-width: 768px), donde el cuerpo es 16 px */
--text-note-title: 28px;
--text-db-title: 24px;
--text-h3: 18px;
--text-h4: 17px;
--text-h5: 16px;
```

Orden intocable: **título > H1 > H2 > H3 > H4 > H5**, en escritorio y en móvil.

| | título | H1 | H2 | H3 | H4 | H5 | cuerpo |
| --- | --- | --- | --- | --- | --- | --- | --- |
| escritorio (1280) | 30 | 24 | 20 | 17 | 15 | 13 | 15 |
| móvil (375) | **28** | 24 | 20 | **18** | **17** | **16** | 16 |

En móvil el cuerpo sube a 16 px, así que la escala se recoloca entera por encima
de él: H3 18, H4 17 y H5 16 (= cuerpo, distinguido por el peso 600, las
versalitas y el color atenuado). El título baja a 28 pero **sigue por encima
del H1** (24): es la condición que el dueño pidió, y en ≤768 px se cumple con
4 px de diferencia, no igualando los dos como estaba antes.

`--text-db-title` existe solo por el móvil: `.db-title` es un `<input>` que no
parte la línea, y a 28 px el texto de un título largo no cabe en los 343 px de
caja que quedan a 375 de ancho (salía recortado). A 24 px cabe, y es el mismo
tamaño que tenía antes de esta tarjeta.

Pesos: 600 en todos los niveles (antes 700). Con una sola familia y estos
tamaños, el 700 de antes engordaba los H1/H2 y los hacía competir con el título.

- H4 deja de ir en versalitas: a 15 px, en mayúsculas competía con el texto
  normal en vez de leerse como encabezado.
- H5 sí las conserva: en escritorio son 13 px (por debajo del cuerpo, 15) y en
  móvil 16 (= cuerpo), y en ambos casos es el nivel que hace de etiqueta: las
  versalitas con `letter-spacing: 0.04em` y el color atenuado lo separan del
  párrafo sin necesidad de ser más grande que él.

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

En móvil (≤768 px) la caja del botón crece a 44×44 (zona táctil) y la fila
vuelve a `align-items: flex-start` (en la ronda anterior estaba en `center`, y
eso dejaba el emoji **2,5 px** por debajo de la primera línea: la fila mide 48
porque suma los 4 px del margen inferior del título). Con `flex-start`, medido a
375 px: **+0,5 px** del centro de la primera línea, tanto con un título de una
línea como de dos. Los demás grupos a 375 px siguen a 0,00 / 0,00
(cabecera 7 botones, `.tree-row`, `.row-del` a 44×44).

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
