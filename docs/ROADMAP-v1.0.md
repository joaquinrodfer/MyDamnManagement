# Roadmap v1.0 — MyDamnManagement

Estado: **borrador para revisión** (2026-10-29). No hay nada implementado de este documento todavía.

Definición de partida, en palabras del dueño del proyecto:

> Quiero un Obsidian (todo en local, en archivos `.md`) pero con la funcionalidad de Notion (bloques, bases de datos). v1.0 es ese punto **sin** bases de datos dentro de páginas. La estética no es prioridad: habrá un agente diseñador más adelante. Se usa como cerebro personal, accesible por Tailscale desde un servidor local.

Aclaración importante de esa frase: **no se va a interactuar con Obsidian**. "Obsidian" aquí significa *el modelo de almacenamiento* (ficheros `.md` en disco, propiedad del usuario, backup trivial), y "Notion" significa *la interfaz y la funcionalidad* (bloques, bases de datos, vistas). La única puerta de entrada es la app. No hay interoperabilidad, ni importación desde un vault de Obsidian, ni edición desde otras herramientas.

---

## 0. Qué significa "v1.0 terminada"

Una checklist cerrada, no una sensación:

- [ ] El vault es autosuficiente: copiar la carpeta y leer los `.md` a mano da todas las notas y bases, sin la app. Nada esencial vive solo en el índice.
- [ ] Si edito un `.md` por fuera y reinicio la app, mis cambios están ahí y la app los ve.
- [ ] Si borro el índice SQLite, la app lo reconstruye desde el vault sin perder nada.
- [ ] Puedo crear, renombrar, mover y borrar notas desde la app, y el disco refleja el resultado.
- [ ] Puedo crear una base de datos con propiedades tipadas, añadir filas, **editar sus valores** y **mover una tarjeta de columna** en el tablero.
- [ ] Los wikilinks y los backlinks funcionan entre ficheros del vault.
- [ ] La búsqueda encuentra texto del cuerpo y devuelve resultados con el índice reconstruido desde cero.
- [ ] Borrar algo lo manda a la papelera, no lo destruye.
- [ ] Copiar/rsync de la carpeta del vault es un backup completo y restaurable.
- [ ] `pytest` corre en verde en un contenedor sin servicios externos, y el CI lo ejecuta en cada push.

Todo lo que no esté en esa lista no es v1.0.

---

## 1. Decisiones tomadas (y por qué)

**D1 — Los ficheros son la verdad; la base de datos es un índice reconstruible (opción C).**
El vault en disco manda. SQLite solo acelera consultas y se puede borrar y reconstruir. Consecuencia directa: ninguna escritura puede existir solo en la base de datos. Si el proceso muere entre "escribo el fichero" y "actualizo el índice", gana el fichero y el índice se corrige en el siguiente arranque o `POST /reindex`.

**D2 — Sin vigilante de ficheros en v1.0.**
No hay `fs.watch`/chokidar ni resolución de conflictos en caliente. El índice se reconstruye al arrancar y cuando se pide explícitamente. Los cambios hechos por fuera se recogen ahí. Esto evita la parte más cara (concurrencia, merges, dos editores) sin renunciar a que los ficheros sean tuyos. Vigilante y conflictos: v1.1.

**D3 — SQLite como índice, no Postgres.**
Un solo proceso escritor, sin servicio aparte, sin puerto 5432 expuesto, backup = copiar la carpeta. Implica: `JSONB` → `JSON`, `tsvector` → FTS5, `UUID` → `TEXT`, y retirar `psycopg2` del contenedor. Alembic se queda (funciona con SQLite) o se sustituye por un esquema versionado propio; se decide en P0.

**D4 — La jerarquía de páginas son carpetas.**
La jerarquía en disco son carpetas: mover un fichero de carpeta es mover la página. `parent_id` deja de ser un campo de la base de datos y pasa a derivarse de la ruta. Un `.md` suelto en la raíz es una página de primer nivel; una carpeta con `.md` dentro es una rama del árbol.

**D5 — Nada de estética en v1.0.**
Se mantiene el HTML/CSS actual sin rediseño. Cualquier tentación de "ya que estamos, mejoro la cabecera" se rechaza en revisión.

**D6 — El alcance de bloques en v1.0 no cambia.**
Los bloques siguen siendo rangos calculados sobre el Markdown: sirven para editar, convertir, borrar y `/comando`. Referencias de bloque (`[[Nota#^abc]]`), embeds y arrastrar bloques son v1.1+. No se reescribe `body_markdown` a lista de objetos.

---

## 2. El contrato del vault (lo más importante del documento)

Si esto se elige mal, P1 se tira. Por eso P0 es un spike que lo demuestra antes de construir nada.

### 2.1 Estructura

```
vault/
  Notas/
    Reunión con Acme.md          # una nota = un fichero
    Proyectos/
      MyDamnManagement.md        # subcarpeta = subpágina
  Bases/
    Contactos.md                 # la BASE DE DATOS es un fichero
    Contactos/
      Acme.md                    # cada FILA es un fichero
      Globex.md
  _attachments/
    2026/10/abc123.png           # imágenes de cabecera y adjuntos
  .trash/
    2026-10-29T18-04-11/
      Reunión con Acme.md        # lo borrado, con su ruta original en el nombre o sidecar
```

### 2.2 Nota

```markdown
---
id: 7f3c1a2e-...          # uuid estable; sobrevive a renombrados y movimientos
icon: "📄"
description: "una línea opcional"
created: 2026-10-28T20:35:00Z
updated: 2026-10-29T09:12:44Z
---

# Contenido en Markdown, tal cual
```

- El **título** es el nombre del fichero (sin `.md`). Renombrar el fichero renombra la página.
- `id` es la identidad real. Si alguien renombra o mueve el fichero por fuera, el `id` evita que la app lo trate como página nueva y pierda sus backlinks.
- Las fechas van con offset UTC (ya arreglado en el paquete de bugs).

### 2.3 Base de datos

`Bases/Contactos.md`:

```markdown
---
id: 9b1d...
type: database
icon: "🗄"
schema:
  - key: empresa
    name: Empresa
    type: text
  - key: fase
    name: Fase
    type: select
    options: [Lead, Negociación, Cerrada]
views:
  - id: v1
    name: Tablero
    type: board
    group_by: fase
---
```

Las **filas** son ficheros en `Bases/Contactos/`, con las propiedades en frontmatter:

```markdown
---
id: 3a7f...
empresa: Acme S.L.
fase: Negociación
valor: 12000
---
Notas libres de la fila, opcionales.
```

Ventaja de esta forma: las propiedades viajan con el fichero y sobreviven a cualquier copia del vault, y el `.md` se lee a mano sin la app. El `title` **no** se duplica en el frontmatter: el nombre del fichero es el único origen del título (decisión cerrada en P0).

### 2.4 Reglas de escritura

- **Escritura atómica**: escribir a `x.md.tmp` y `rename()` encima. Nunca un fichero a medias.
- **Nombre de fichero legible (decisión del dueño).** El fichero se llama como el título saneado (`Reunión con Acme.md`). Cambiar el título **renombra el fichero**; el `id` del frontmatter es la identidad y sobrevive al renombrado. Caracteres prohibidos saneados, NFC al leer y al escribir, colisión con ` (2)`, ` (3)` sin distinguir mayúsculas, truncado a 180 bytes, y nombres reservados de Windows evitados. Las reglas exactas quedan fijadas en el ADR de P0.
- **Un solo escritor**: la API. Los cambios externos entran por reindexado.
- **Codificación**: UTF-8 sin BOM, saltos `\n`.
- **Frontmatter escrito por la app**: se preservan claves desconocidas que haya añadido el usuario (no se pisa el YAML entero).

### 2.5 Lo que se pierde y hay que aceptar

- Wikilinks se resuelven **por título**, como ya hace la app hoy: es lo que el usuario escribe. Dos notas con el mismo nombre en carpetas distintas: se resuelve por carpeta más cercana, luego ruta más corta, luego orden alfabético, y se avisa del número de candidatas.
- Enlaces a encabezados (`[[Nota#Sección]]`) siguen sin resolverse a nivel de bloque: v1.1.
- El historial de versiones no existe. Git lo cubre si el dueño lo pone encima del vault.

**No hay interoperabilidad con Obsidian.** El formato es propio: `.md` + YAML, legible y portable, pero nadie va a abrir el vault con otra app. Por eso no se persigue compatibilidad con Dataview, ni con la sintaxis de propiedades de Obsidian, ni con sus convenciones de enlaces.

---

## 3. El índice SQLite

Tablas mínimas:

| Tabla | Para qué |
|---|---|
| `pages` | id, path, title, type, icon, description, created, updated, mtime, content_hash |
| `databases` | id, schema_json, views_json |
| `rows` | id, database_id, properties_json |
| `links` | from_id, to_id, kind (`wikilink`/`relation`), target_title crudo |
| `pages_fts` | FTS5 sobre título + cuerpo, tokenizer con soporte de acentos |

Reglas:

- `path` es único. `id` es único.
- Reindexado completo: recorrer el vault, parsear frontmatter, `UPSERT`, y borrar del índice lo que ya no existe en disco. Debe ser **idempotente** y lo bastante rápido para ~5.000 ficheros (objetivo: menos de 5 s).
- Reindexado incremental (opcional en v1.0): comparar `mtime` + tamaño antes de reparsear.
- Deriva detectable: `GET /status` informa de ficheros modificados por fuera desde el último reindexado.
- El índice se guarda fuera del vault (`/data/index.sqlite`) para que el vault quede limpio y portable.

---

## 4. API: qué cambia

- Se mantienen las rutas actuales (`/pages`, `/databases`, `/search`, `/dashboard`); cambia su implementación por debajo.
- **Nuevo** `POST /reindex` — reconstruye desde el vault. Devuelve recuento y duración.
- **Nuevo** `GET /status` ampliado — salud + deriva índice/disco + versión del esquema.
- **Nuevo** `GET /pages/{id}/raw` (opcional) — el `.md` tal cual, para depurar.
- **Borrado** pasa a mover a `.trash/` y devuelve 204 igual. `DELETE /trash/{...}` para vaciar, si se implementa.
- **Renombrar** = mover fichero: `PATCH /pages/{id}` con título nuevo provoca `rename()`.
- Los adjuntos viven en el vault (`_attachments/`), servidos desde `/files` con la validación por magic bytes ya hecha.

Compatibilidad: el export manual de `.md` que ya existe deja de tener sentido como función y pasa a ser "descargar el fichero original".

---

## 5. Paquetes

Cada paquete se cierra con revisión del `revisor` y no se da por bueno sin verificación ejecutada.

### P0 — Spike y contrato del vault (S · ~0,5 día)
Escribir un prototipo aislado (fuera del repo, en scratch) que: genere 20 notas y 2 bases de datos con el formato de §2, las relea, reconstruya un índice SQLite desde cero y busque en FTS5. Sin tocar la app.
**Hecho cuando:** el prototipo corre de punta a punta, el índice se reconstruye idempotente, y las decisiones abiertas (Alembic sí/no, tokenizer FTS5 con acentos, `title` duplicado en filas) están escritas como ADR corto en `docs/`.

### P1 — Notas en ficheros (L · ~2-3 días, en dos entregas)

Se parte en dos para que cada una sea revisable:

**P1.1 — Índice SQLite y capa de repositorio.** Fuera Postgres: el índice de §3 y un repositorio que los routers usan en vez de las sesiones ORM, sin cambiar ni un comportamiento visible. Incluye el primer `pytest` ejecutable sin servicios externos, que es lo que permite verificar todo lo que venga después. Migración de una vez de los datos actuales.

**P1.2 — Los ficheros como verdad.** Sustituir la persistencia de páginas: guardar = escribir `.md`, leer = leer del índice, borrar = mover a `.trash/`, renombrar = `rename()`. Reindexado completo al arrancar y `POST /reindex`. Migración del contenido actual al vault.

**Hecho cuando:** los criterios 1-4 y 8 de §0 pasan en una prueba real con el vault en un directorio temporal, y ningún endpoint escribe solo en la base de datos.

### P2 — Filas y bases de datos en disco (M · ~1-2 días)
`Bases/X.md` + carpeta de filas, propiedades en frontmatter, schema y vistas en el frontmatter de la base. Alta/baja/edición de filas escribiendo ficheros. Reindexado de bases de datos.
**Hecho cuando:** crear una base desde plantilla CRM, añadir 3 filas, borrar una, reiniciar, y el estado coincide exactamente entre disco e índice.

### P3 — Editar filas en la interfaz (M · ~1-2 días)
Formulario de fila en tabla y en tablero; arrastrar tarjeta entre columnas escribe la propiedad de agrupación; añadir y quitar propiedades desde la UI. Aquí se convierte en "CRM usable".
**Hecho cuando:** mover una tarjeta de Lead a Negociación, recargar y ver el cambio; y el mismo cambio aparece en el `.md` de la fila.

### P4 — Papelera y backup (S · ~0,5 día)
`.trash/` con fecha, restaurar, vaciar. Healthcheck real. Documento de backup: rsync del vault + cómo restaurar. (El índice no necesita backup.)
**Hecho cuando:** borrar una nota, encontrarla en `.trash/`, restaurarla, y `rsync` de ida y vuelta reconstruye un vault idéntico.

### P5 — Búsqueda y enlaces sobre ficheros (M · ~1 día)
FTS5 con acentos; wikilinks y backlinks resueltos contra el índice de ficheros; `[[Nota#Sección]]` sigue fuera. El cliente deja de indexar el árbol por su cuenta (hoy hay dos verdades, cliente y servidor).
**Hecho cuando:** los backlinks coinciden con los enlaces reales del vault tras editar un fichero por fuera y reindexar.

### P6 — Tests y CI (M · ~1 día)
`pytest` + `TestClient` sobre SQLite y un vault temporal: contrato de la API, propiedades, cascadas (que ahora son movimientos a papelera), reindexado idempotente, migración de formato. Adoptar el arnés de jsdom que ya construyó el `revisor` como `frontend/tests/` para el autoguardado y el editor. Workflow de GitHub Actions que lo corra en cada push.
**Hecho cuando:** `pytest` verde en un contenedor limpio sin servicios, y el CI falla de verdad cuando se rompe algo a propósito.

### P7 — Endurecer y desplegar en Tailscale (S-M · ~0,5-1 día)
Un contenedor: volumen del vault montado, sin Postgres, sin 5432. `tailscale serve` delante, sin publicar el puerto crudo. Decisión sobre `/docs` y sobre auth: **hoy cualquiera en la tailnet tiene lectura y escritura total**, incluido borrar; para un cerebro personal eso es demasiado. Mínimo: token de API o binding a `tailscale serve` con identidad. Documento de arranque y restore.
**Hecho cuando:** desde otro dispositivo de la tailnet se usa la app por HTTPS, y sin credencial no se puede escribir.

---

## 6. Riesgos

| Riesgo | Mitigación |
|---|---|
| Elegir mal el formato del vault y rehacerlo | P0 antes de P1; el spike es descartable |
| Dos escrituras simultáneas corrompen un fichero | Escritura atómica + un solo escritor (la API) |
| Cambios externos pisados sin avisar | Reindexado al arrancar + deriva visible en `/status`; conflicto explícito en v1.1 |
| Perder datos en la migración Postgres → vault | La migración es de una vez y deja Postgres intacto como respaldo hasta validar |
| CRLF/UTF-8/NFC rompen los títulos | Normalizar nombres (NFC) al leer y al escribir, y documentarlo en el ADR de P0 |
| La app se expone sin auth en la tailnet | P7 antes de meter notas reales; papelera (P4) antes también |
| Índice y disco divergen en silencio | `POST /reindex` + deriva en `/status` + test de idempotencia en P6 |

---

## 7. Fuera del alcance de v1.0

Bases de datos **dentro** de una página (inline). Vistas lista y calendario. Referencias y embeds de bloque. Arrastrar bloques. Multiusuario, permisos por página, historial de versiones. Vigilante de ficheros y resolución de conflictos. Plugins/extensiones. Móvil nativo. Cualquier rediseño estético.

---

## 8. Deuda abierta que no bloquea v1.0 pero debe quedar anotada

- `timestamptz` real en el índice (hoy las fechas se arreglan al serializar).
- Los tres bugs y el paquete 1 están en la rama `fix/errores-red-autoguardado`, sin fusionar a `main` ni publicar (el contenedor no tiene credenciales de GitHub).
- La carrera de beacons solapados (tarjeta `t_d3d9899e`).
- El `downgrade` de la migración inicial no borra los ENUM (irrelevante si se abandona Postgres, pero hay que confirmarlo al migrar).
