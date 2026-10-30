# Tres Direcciones Visuales para MyDamnManagement

Este documento define **tres direcciones visuales y de usabilidad genuinamente distintas** para MyDamnManagement. Todas respetan las restricciones del proyecto: cero dependencias externas, cero fuentes remotas (solo pilas del sistema), compatibilidad estricta con los selectores de `frontend/app.js` y corrección de los problemas críticos detectados en `AUDITORIA.md` (contraste AA/AAA real, objetivos de toque de al menos `28–32 px`, foco de teclado visible `:focus-visible`, resolución de la colisión del índice con `Backlinks` y ancho de lectura ergonómico).

> **Metodología de medición de contraste (fórmula explícita WCAG 2.x):**
> Todos los ratios de contraste de este documento se han calculado sobre los valores sRGB hexadecimales exactos de cada `:root` y `html[data-theme]` mediante la fórmula oficial de luminancia relativa WCAG 2.1/2.2: para cada canal $C \in \{R, G, B\} / 255$, se linealiza con $c = C / 12.92$ si $C \le 0.04045$ y $c = ((C + 0.055) / 1.055)^{2.4}$ en caso contrario; se obtiene $L = 0.2126 r + 0.7152 g + 0.0722 b$ y el ratio $\text{CR} = (L_{\max} + 0.05) / (L_{\min} + 0.05)$ redondeado a 2 decimales.

---

## Resumen comparativo de un vistazo

| Dimensión | Dirección A: **Cuaderno de Archivo** *(Editorial cálido)* | Dirección B: **Mesa de Operaciones** *(Precisión técnica / IDE)* | Dirección C: **Tablero Suizo** *(Brutalismo estructural)* |
| :--- | :--- | :--- | :--- |
| **Frase que la define** | Un cuaderno de archivo en papel crema cálido donde la nota es una página literaria con serifas y las bases de datos son fichas limpias. | Una consola de operaciones de alta densidad con chasis oscuro o neutro frío, bordes de precisión y foco en velocidad de teclado. | Un sistema editorial suizo de rejilla visible, trazos negros nítidos (`1.5px–2px`), esquinas rectas (`0px`), sombras duras y acento bermellón. |
| **Carácter principal** | Calma, lectura larga, calidez táctil (inspiración iA Writer / Bear / Notion clásico en papel). | Densidad, escaneo rápido de tablas/Kanban, rigor técnico (inspiración Linear / Obsidian / Raycast). | Claridad radical, contraste máximo, personalidad gráfica inconfundible (inspiración Müller-Brockmann / Teenage Engineering). |
| **Tipografía de títulos** | **Serif del sistema** (`Georgia`, `Iowan Old Style`) — contraste literario frente a la interfaz sans. | **Sans geométrica/sistema** (`system-ui`) con `letter-spacing: -0.02em` y metadatos en `--font-mono`. | **Sans grotesca en peso 800** combinada con etiquetas numeradas en `--font-mono` mayúscula. |
| **Radios (`border-radius`)** | Suaves y orgánicos (`6px` controles, `10px` tarjetas, `14px` modales). | Contenidos y técnicos (`4px` controles, `6px` tarjetas/tablas, `8px` modales). | **Cero (`0px`)** en toda la interfaz (esquinas vivas y ortogonales). |
| **Sombras** | Difusas y cálidas (`0 10px 28px rgba(42, 33, 24, 0.10)`). | Sutiles con anillo interior de 1px (`0 4px 16px rgba(0, 0, 0, 0.28)`). | **Sombra dura desplazada sin difuminar** (`3px 3px 0 #141413`). |
| **Lienzo vs. Barra lateral** | Barra lateral en tono arena (`#f2ede4`) y lienzo principal en papel marfil luminoso (`#fbf9f5`). | Barra lateral en pizarra (`#0f1117` / `#f3f4f8`) y lienzo principal elevado (`#161922` / `#ffffff`). | Cuadrícula de bordes continuos a sangre sobre papel hueso (`#f4f1ea`) y paneles blancos (`#ffffff`). |

---

## Dirección A — «Cuaderno de Archivo» (Editorial Cálido y Táctil)

### 1. Definición y qué la hace distinta
> **«Un cuaderno de archivo en papel marfil donde escribir notas largas descansa la vista y consultar bases de datos se siente como hojear fichas editoriales.»**

Se distingue de **B** y **C** porque **invierte la frialdad industrial actual**: sustituye los grises azulados (`#eff1f4`) por tonos papel y piedra cálida (`#fbf9f5` y `#f2ede4`), reserva la tipografía **Serif del sistema** para los títulos de documentos y encabezados de prosa, e invierte la relación de brillo entre barra lateral y lienzo (el documento abierto es la superficie más clara y limpia de la pantalla, mientras la barra lateral pasa a un segundo plano cálido y silencioso).

### 2. Tokens concretos

#### Paleta de color — Modo Claro (por defecto) y Modo Oscuro (Noche cálida)

| Token CSS | Hex (Claro) | Hex (Oscuro) | Uso exacto y contraste medido (WCAG 2.x) |
| :--- | :--- | :--- | :--- |
| `--bg` | `#f2ede4` | `#171513` | Fondo de la barra lateral (`aside`), cabeceras de tabla y carriles Kanban. |
| `--panel` | `#fbf9f5` | `#1f1c19` | Lienzo principal de lectura (`main`), tarjetas Kanban y diálogos. |
| `--surface-elevated` | `#ffffff` | `#282420` | Inputs activos, menús flotantes y filas destacadas. |
| `--panel-border` | `#d5ccb8` | `#38322c` | Divisor decorativo suave entre paneles (`1.52:1` sobre `--panel` y `1.37:1` sobre `--bg` en claro; `1.34:1` sobre `--panel` y `1.44:1` sobre `--bg` en oscuro). |
| `--border-strong` | `#877c6a` | `#7d7265` | Borde de inputs, botones `.btn` y modales (cumple WCAG 1.4.11 $\ge 3:1$ en todas las superficies): en claro da **4.10:1** sobre `--surface-elevated` (`#ffffff`), **3.90:1** sobre `--panel` (`#fbf9f5`) y **3.52:1** sobre `--bg` (`#f2ede4`); en oscuro da **3.28:1** sobre `--surface-elevated` (`#282420`), **3.61:1** sobre `--panel` (`#1f1c19`) y **3.87:1** sobre `--bg` (`#171513`). |
| `--ink` | `#221f1b` | `#ede8df` | Texto principal. Contraste sobre `--panel`: **15.60:1** (claro) / **13.90:1** (oscuro); sobre `--bg`: **14.07:1** (claro) / **14.92:1** (oscuro). |
| `--ink-muted` | `#5c5449` | `#a89f91` | Texto secundario, metadatos y etiquetas. Contraste sobre `--panel`: **7.08:1** (claro) / **6.49:1** (oscuro); sobre `--bg`: **6.39:1** (claro) / **6.97:1** (oscuro). |
| `--accent` | `#9e4722` | `#e07b4f` | Terracota tostado (claro) / Ámbar cálido (oscuro) para botones primarios y foco `:focus-visible`. Contraste sobre `--panel`: **5.91:1** (claro) / **5.75:1** (oscuro). |
| `--accent-contrast` | `#ffffff` | `#171513` | Texto sobre `.btn-primary`. Contraste: **6.22:1** (claro `#ffffff` sobre `#9e4722`) / **6.17:1** (oscuro `#171513` sobre `#e07b4f`). |
| `--accent-ink` | `#7d3516` | `#f2a680` | Wikilinks, pestañas activas y nodo activo del árbol. Contraste sobre `--panel`: **8.35:1** (claro) / **8.51:1** (oscuro); sobre `--bg`: **7.53:1** (claro) / **9.14:1** (oscuro). |
| `--accent-soft` | `rgba(158, 71, 34, 0.11)` | `rgba(224, 123, 79, 0.16)` | Fondo de fila activa en el árbol y selección de texto en el editor. |
| `--ok` | `#237a43` | `#4fbf82` | Indicador `api`/`db` activo y estados completados. Contraste sobre `--panel`: **5.07:1** (claro) / **7.35:1** (oscuro); sobre `--bg`: **4.58:1** (claro) / **7.90:1** (oscuro). |
| `--down` | `#b32d2d` | `#eb6f6f` | Acciones destructivas y errores. Contraste sobre `--panel`: **6.00:1** (claro) / **5.68:1** (oscuro); sobre `--bg`: **5.41:1** (claro) / **6.10:1** (oscuro). |
| `--code-bg` | `#eae3d5` | `#292521` | Bloques de código, citas y estado `:hover` suave. |

#### Escala tipográfica
- `--font-serif`: `Iowan Old Style, "Palatino Linotype", Palatino, Georgia, "Times New Roman", serif` (aplicada a `.note-title`, `.db-title`, `.cm-mdm-h1` y `.cm-mdm-h2`).
- `--font-body`: `-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif` (cuerpo de nota, tablas, controles).
- `--font-mono`: `"Cascadia Code", "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace` (código, metadatos, atajos).
- Tamaños:
  - Título de nota (`.note-title`): `32px` (`font-weight: 700`, `line-height: 1.2`, `letter-spacing: -0.015em`).
  - Título de base de datos (`.db-title`): `28px` (`font-weight: 700`).
  - Encabezados en editor: `H1` `1.75em`, `H2` `1.4em`, `H3` `1.15em`.
  - Prosa del editor (`.cm-content`): `16px`, `line-height: 1.72`, contenedor `.note-sheet` con `max-width: 720px` centrado con margen amplio.
  - Interfaz (árbol, tablas, botones): `13.5px`; cabeceras de sección y tabla: `11px` (`letter-spacing: 0.07em`, `uppercase`).

#### Radios, sombras y espaciado
- **Radios:** `--radius-sm: 6px` (botones, inputs, filas del árbol), `--radius-md: 10px` (tarjetas Kanban, `.table-wrap`, citas), `--radius-lg: 14px` (modales `<dialog>` y menús flotantes), `--radius-pill: 999px` (pestañas `.view-tab` y etiquetas `select`).
- **Sombras:**
  - Tarjeta Kanban en reposo: `0 1px 3px rgba(34, 31, 27, 0.06)`.
  - Flotantes y modales: `0 12px 32px rgba(34, 31, 27, 0.14), 0 2px 6px rgba(34, 31, 27, 0.06)`.
- **Espaciado y objetivos táctiles:**
  - `.icon-btn`: sube de `20×20 px` a **`28×28 px`** (`gap: 4px` en controles de navegación).
  - `.tree-row`: `padding: 6px 10px` (alto `32 px`).
  - `main`: `padding: 32px 48px 72px` (a `1024 px` se reduce a `24px 28px 60px`, y `aside` baja a `236px`).
  - El formulario `.inline-form` ("+ Nueva fila") pasa a una rejilla compacta (`.inline-form-grid`) que ocupa `~110 px` de alto en vez de `541 px`.

### 3. Qué gana y qué pierde
- **Qué gana:**
  - **Lectura y escritura sobresalientes:** La combinación de papel cálido (`#fbf9f5`), prosa a `16px` con `line-height: 1.72` y ancho acotado (`720px`) elimina la fatiga visual en notas largas.
  - **Jerarquía inmediata:** La distinción entre títulos en serifa y controles en sans-serif separa de un vistazo qué es *contenido del usuario* y qué es *herramienta*.
- **Qué pierde:**
  - **Menor densidad vertical:** Al dar más aire a la prosa (`16px`), a las filas del árbol (`32px`) y a las celdas de tabla (`padding: 10px 14px`), caben aproximadamente un **15% menos de filas en pantalla** sin hacer scroll que en la Dirección B.

---

## Dirección B — «Mesa de Operaciones» (Precisión Técnica, Estilo Linear / Obsidian)

### 1. Definición y qué la hace distinta
> **«Una estación de trabajo técnica de alta densidad, pensada para quien vive entre tablas, tableros Kanban y atajos de teclado durante horas.»**

Se distingue de **A** y **C** porque **prioriza la densidad de información, la velocidad de escaneo y el confort en sesiones largas**: adopta una arquitectura de capas con contraste milimétrico (tanto en su modo oscuro nativo carbón-azulado como en su modo claro pizarra), sustituye el aspecto de "documento literario" por el de una herramienta de ingeniería moderna (como Linear o Raycast), compacta las filas y convierte las propiedades de las bases de datos en pastillas semánticas codificadas por estado.

### 2. Tokens concretos

#### Paleta de color — Modo Oscuro (por defecto en el mockup para mostrar su punto fuerte) y Modo Claro

| Token CSS | Hex (Oscuro) | Hex (Claro) | Uso exacto y contraste medido (WCAG 2.x) |
| :--- | :--- | :--- | :--- |
| `--bg` | `#0f1117` | `#f3f4f8` | Fondo de la barra lateral (`aside`), barra de estado superior y carriles Kanban (`.board-col`). |
| `--panel` | `#161922` | `#ffffff` | Superficie principal de trabajo (`main`), tabla de datos y controles de entrada. |
| `--surface-elevated` | `#1e222e` | `#f8f9fc` | Modales `<dialog>`, tarjetas Kanban, botones `.btn`, menús flotantes y cabeceras `<th>`. |
| `--panel-border` | `#2a2f3d` | `#d8dce6` | Líneas divisorias decorativas de `1px` entre paneles (`1.31:1` sobre `--panel` en oscuro; `1.37:1` sobre `--panel` en claro). |
| `--border-input` | `#6c7690` | `#798295` | Borde de inputs, `select`, `.btn` y desplegables (cumple WCAG 1.4.11 $\ge 3:1$ en todas las superficies): en oscuro da **3.50:1** sobre `--surface-elevated` (`#1e222e`), **3.87:1** sobre `--panel` (`#161922`) y **4.16:1** sobre `--bg` (`#0f1117`); en claro da **3.51:1** sobre `--bg` (`#f3f4f8`), **3.67:1** sobre `--surface-elevated` (`#f8f9fc`) y **3.86:1** sobre `--panel` (`#ffffff`). |
| `--ink` | `#eef0f6` | `#11141c` | Texto primario. Contraste sobre `--panel`: **15.41:1** (oscuro) / **18.41:1** (claro); sobre `--bg`: **16.56:1** (oscuro) / **16.75:1** (claro). |
| `--ink-muted` | `#949db3` | `#525a6e` | Texto secundario, iconos y cabeceras de columna. Contraste sobre `--panel`: **6.46:1** (oscuro) / **6.89:1** (claro); sobre `--bg`: **6.95:1** (oscuro) / **6.27:1** (claro). |
| `--accent` | `#5e6ad2` | `#434ce6` | Índigo eléctrico para anillo `:focus-visible` y bordes activos (**3.74:1** sobre `#161922` en oscuro / **6.15:1** sobre `#ffffff` en claro). |
| `--accent-btn` | `#4e5ac0` | `#3b44d1` | Fondo de botón primario `.btn-primary` y píldora activa. |
| `--accent-contrast` | `#ffffff` | `#ffffff` | Texto blanco sobre `.btn-primary` (`--accent-btn`). Contraste: **5.92:1** (`#ffffff` sobre `#4e5ac0` en oscuro) y **7.16:1** (`#ffffff` sobre `#3b44d1` en claro). |
| `--accent-ink` | `#9ba4ff` | `#323ab8` | Wikilinks, pestaña activa y selección en el árbol. Contraste sobre `--panel`: **7.65:1** (oscuro) / **8.63:1** (claro); sobre `--bg`: **8.22:1** (oscuro) / **7.85:1** (claro). |
| `--accent-soft` | `rgba(94, 106, 210, 0.18)` | `rgba(67, 76, 230, 0.10)` | Fondo de ítem activo en barra lateral y selección del editor. |
| `--ok` | `#3dd68c` | `#18794e` | Estado online `api`/`db` y columna "Hecho". Contraste sobre `--panel`: **9.36:1** (oscuro) / **5.41:1** (claro); sobre `--bg`: **10.06:1** (oscuro) / **4.92:1** (claro). |
| `--warn` | `#f5a623` | `#b45309` | Estado "En curso" / fechas próximas. Contraste sobre `--panel`: **8.66:1** (oscuro) / **5.02:1** (claro). |
| `--down` | `#f26464` | `#c81e1e` | Errores, fechas vencidas y borrado. Contraste sobre `--panel`: **5.67:1** (oscuro) / **5.74:1** (claro); sobre `--bg`: **6.09:1** (oscuro) / **5.22:1** (claro). |
| `--code-bg` | `#202534` | `#eaedf4` | Bloques de código, teclado (`kbd`) y hover de filas. |

#### Escala tipográfica
- `--font-body`: `-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, "Helvetica Neue", Arial, sans-serif`.
- `--font-mono`: `"Cascadia Code", "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`.
- Tamaños y pesos:
  - Título de nota (`.note-title`): `24px` y base de datos (`.db-title`): `21px` en `--font-body` (`font-weight: 700`, `letter-spacing: -0.02em`), eliminando el salto discordante a monoespaciada.
  - Cuerpo de nota (`.cm-content`): `14.5px`, `line-height: 1.62`, contenedor `.note-workspace` con `max-width: 780px`.
  - Árbol lateral (`13px`) y celdas de tabla (`12.5px`, `font-variant-numeric: tabular-nums`), permitiendo ver más columnas y filas sin truncamiento prematuro.
  - Metadatos, contadores de columna Kanban y rutas: `11px–11.5px` en `--font-mono`.

#### Radios, sombras y espaciado
- **Radios:** `--radius-sm: 4px` (botones, inputs, celdas editables), `--radius-md: 6px` (tarjetas Kanban, contenedor de tabla, bloques de código), `--radius-lg: 8px` (diálogos y menús flotantes).
- **Sombras:**
  - Tarjetas Kanban: `0 1px 2px rgba(0, 0, 0, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.04)`.
  - Flotantes y diálogos: `0 16px 40px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.07)`.
- **Espaciado y objetivos táctiles:**
  - `.icon-btn`: **`28×28 px`** con borde sutil en `:hover` y anillo `:focus-visible` de `2px solid var(--accent)`.
  - Columnas Kanban (`.board-col`): anchura `256px` con contenedor propio (`background: var(--bg)`, `border: 1px solid var(--panel-border)`, `padding: 10px`, `border-radius: 6px`) que hace explícita el área donde soltar tarjetas.
  - Cabecera superior compacta (`40px` de alto) e índice de nota integrado en la barra de herramientas superior derecha (`.note-topbar-right`) para que jamás pise `Backlinks`.

### 3. Qué gana y qué pierde
- **Qué gana:**
  - **Máxima eficiencia en bases de datos y tableros:** Caben hasta **12–14 filas visibles** en tabla a `800px` de alto y las columnas Kanban tienen estructura clara de contenedor con contadores tipo insignia.
  - **Ergonomía de teclado superior:** El contraste de bordes de inputs (`--border-input` $\ge 3.50:1$), filas seleccionadas y anillos `:focus-visible` índigo destacan con nitidez quirúrgica.
- **Qué pierde:**
  - **Menor calidez literaria:** Para quien use la app exclusivamente como diario personal o escritura creativa en prosa, el tono es más sobrio y funcional que el de la Dirección A.

---

## Dirección C — «Tablero Suizo» (Neo-Brutalismo Estructural / Alto Contraste)

### 1. Definición y qué la hace distinta
> **«Una cuadrícula tipográfica suiza sin adornos: bordes estructurales negros de 1.5 px, esquinas a 0 grados, sombras sólidas desplazadas y acento bermellón.»**

Se distingue radicalmente de **A** y **B** porque **renuncia a las sombras difusas, las transparencias suaves y los bordes redondeados**: toda la interfaz es una retícula arquitectónica visible (inspirada en el diseño gráfico suizo de los años 60 y en interfaces industriales como Teenage Engineering). Cada botón, tarjeta Kanban, celda de tabla y diálogo está delimitado por trazos sólidos de alto contraste (`#141413` sobre papel técnico `#f4f1ea`), haciendo que los límites clicables, el foco de teclado y la jerarquía sean imposibles de confundir.

### 2. Tokens concretos

#### Paleta de color — Modo Claro (Papel Técnico + Tinta Negra) y Modo Oscuro (Terminal Ámbar/Carbono)

| Token CSS | Hex (Claro) | Hex (Oscuro) | Uso exacto y contraste medido (WCAG 2.x) |
| :--- | :--- | :--- | :--- |
| `--bg` | `#f4f1ea` | `#121211` | Fondo estructural de páginas y carriles. |
| `--panel` | `#ffffff` | `#1a1a18` | Superficie de tarjetas, tablas, cabecera y diálogos. |
| `--panel-border` | `#141413` | `#e6e3dc` | **Borde estructural de alto contraste (`1.5px–2px solid`)**. Contraste sobre `--panel`: **18.43:1** (claro) / **13.60:1** (oscuro); sobre `--bg`: **16.34:1** (claro) / **14.63:1** (oscuro). |
| `--border-subtle` | `#8e887b` | `#6d6b64` | Rejilla interior vertical entre celdas de tabla (cumple WCAG 1.4.11 $\ge 3:1$ en ambos temas): **3.52:1** sobre `--panel` (`#ffffff`) y **3.12:1** sobre `--bg` (`#f4f1ea`) en claro; **3.27:1** sobre `--panel` (`#1a1a18`) y **3.51:1** sobre `--bg` (`#121211`) en oscuro. |
| `--ink` | `#141413` | `#f4f1ea` | Tinta principal. Contraste sobre `--panel`: **18.43:1** (claro) / **15.45:1** (oscuro); sobre `--bg`: **16.34:1** (claro) / **16.62:1** (oscuro). |
| `--ink-muted` | `#4a4843` | `#b5b0a4` | Texto secundario y etiquetas técnicas. Contraste sobre `--panel`: **9.13:1** (claro) / **8.06:1** (oscuro); sobre `--bg`: **8.10:1** (claro) / **8.67:1** (oscuro) — cumple **WCAG AAA**. |
| `--accent` | `#d9381e` | `#ff5a36` | Bermellón internacional para acciones primarias, indicadores activos y foco. Contraste sobre `--panel`: **4.63:1** (claro) / **5.62:1** (oscuro). |
| `--accent-contrast` | `#ffffff` | `#121211` | Texto sobre `.btn-primary`. Contraste: **4.63:1** (`#ffffff` sobre `#d9381e` en claro) / **6.04:1** (`#121211` sobre `#ff5a36` en oscuro). |
| `--accent-ink` | `#b02710` | `#ff7a5c` | Enlaces, wikilinks y estado activo. Contraste sobre `--panel`: **6.68:1** (claro) / **6.80:1** (oscuro); sobre `--bg`: **5.92:1** (claro) / **7.31:1** (oscuro). |
| `--accent-soft` | `#ffe6e0` | `#3a1d16` | Fondo sólido (sin alfa) para fila activa del árbol y pestaña activa. |
| `--highlight` | `#f5d94c` | `#d4af37` | Marcador amarillo industrial para selección y contadores destacados (con texto `#141413`, contraste **13.09:1** en claro / **8.77:1** en oscuro). |
| `--ok` | `#157f3b` | `#4ce080` | Estado `api`/`db` OK. Contraste sobre `--panel`: **5.08:1** (claro) / **10.19:1** (oscuro); sobre `--bg`: **4.51:1** (claro) / **10.96:1** (oscuro). |
| `--down` | `#b81d13` | `#ff6b6b` | Peligro y borrado. Contraste sobre `--panel`: **6.52:1** (claro) / **6.28:1** (oscuro); sobre `--bg`: **5.78:1** (claro) / **6.75:1** (oscuro). |
| `--code-bg` | `#e8e4da` | `#262623` | Bloques de código, citas y cabeceras técnicas. |

#### Escala tipográfica
- `--font-body`: `"Helvetica Neue", Arial, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif`.
- `--font-mono`: `"Cascadia Code", "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`.
- Tamaños y pesos:
  - Títulos (`.note-title`, `.db-title`): `28px` en `--font-body`, `font-weight: 800`, `letter-spacing: -0.03em`, acompañados de una barra superior de metadatos en `--font-mono` (`11.5px`, `uppercase`).
  - Cabeceras de sección (`.sidebar-section-head`, `.board-col-head`, `.rows-table th`): `11px–12px` en `--font-mono`, `font-weight: 700–800`, `text-transform: uppercase`, `letter-spacing: 0.06em–0.08em`.
  - Prosa (`.cm-content`): `15.5px`, `line-height: 1.65`, ficha `.note-sheet` con `max-width: 760px`.

#### Radios, sombras y espaciado
- **Radios:** **`0px` en todos los componentes** (`border-radius: 0`), excepto los puntos de estado `.dot` si se desea o cuadrados con borde de `1.5px`.
- **Sombras:** Sombra dura ortogonal sin desenfoque:
  - Botones, tarjetas Kanban y desplegables: `box-shadow: 3px 3px 0 var(--panel-border)`.
  - Al pasar el ratón o enfocar (`:hover`, `:focus-visible`): desplazamiento táctil `transform: translate(-1px, -1px); box-shadow: 4px 4px 0 var(--accent)`.
  - En ficha de nota y diálogos `<dialog>`: `box-shadow: 7px 7px 0 var(--panel-border)`.
- **Espaciado y objetivos táctiles:**
  - `.icon-btn`: **`30×30 px`** enmarcados en casillas con borde de `1.5px solid var(--panel-border)`.
  - Tablas con rejilla completa (`border-right: 1px solid var(--border-subtle)` en celdas) que permite seguir filas anchas sin perder la línea.

### 3. Qué gana y qué pierde
- **Qué gana:**
  - **Personalidad propia y accesibilidad visual extrema (WCAG AAA en casi todo el texto):** Ningún control parece deshabilitado por error ni se confunde con el fondo; un usuario con baja visión o en un monitor mal calibrado distingue cada botón, celda editable y columna Kanban al instante.
  - **Feedback físico inmediato:** Las sombras duras que responden al foco y al clic hacen que arrastrar tarjetas o pulsar botones sea inequívoco.
- **Qué pierde:**
  - **Peso visual alto:** Tantos bordes de `1.5px–2px` y sombras sólidas reclaman atención constante; a usuarios que buscan una interfaz "invisible" al escribir les puede resultar demasiado gráfica o intensa.
