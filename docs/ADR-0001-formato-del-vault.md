# ADR 0001 — Formato del vault y del índice

- **Estado:** aceptado (2026-10-29)
- **Contexto:** MyDamnManagement v1.0 — notas y bases de datos con almacenamiento en ficheros `.md`, interfaz tipo Notion, uso personal sobre Tailscale.
- **Validación:** spike P0 (`t_6fea05dc`), ejecutado y revisado de forma independiente. `run_p0.py`: 108/109 comprobaciones; el único fallo es de escala en el montaje 9p de desarrollo (21 s), que en disco local son 0,32 s con 5.033 ficheros.

Este ADR fija el contrato que P1 implementa.

## Decisiones

**D1 — Los ficheros son la verdad; el índice es reconstruible.** Ninguna escritura puede existir solo en SQLite. El índice se puede borrar y rehacer sin pérdida.

**D2 — Sin vigilante de ficheros en v1.0.** El índice se reconstruye al arrancar y con `POST /reindex`. Un solo escritor: la API.

**D2b — Un vault vacío no es un vault borrado.** Reindexar borra el índice, así que ni el arranque ni `POST /reindex` lo hacen cuando el vault no existe o no tiene ningún `.md`: se niegan con el motivo. La salida es explícita y depende de si el servicio está vivo: `POST /reindex?force=true` con la API en marcha y **`VAULT_ALLOW_EMPTY=1`** en el arranque (el endpoint no puede usarse cuando lo que falla es el propio arranque). Desde P2 ese arranque deja el índice **vacío**: las bases y sus filas también son ficheros, así que si el vault está vacío de verdad no queda nada que indexar (en P1.2a sobrevivían porque solo vivían en el índice). `VAULT_ALLOW_EMPTY` no cubre una raíz inexistente: un volumen sin montar no se lee como vault vacío y el arranque sigue fallando.

**D3 — SQLite como índice, no Postgres.** `JSONB`→JSON, `tsvector`→FTS5, sin servicio de base de datos ni puerto 5432.

**D4 — La jerarquía son carpetas.** Mover un fichero de carpeta es mover la página; `parent_id` se deriva de la ruta.

**D5 — Nada de estética en v1.0.**

**D6 — Los bloques no cambian.** Siguen siendo rangos calculados sobre el Markdown.

**D7 — No hay interoperabilidad con Obsidian.** El formato es propio: `.md` + YAML, legible y portable, sin perseguir compatibilidad con Dataview ni con las convenciones de Obsidian.

## Contrato del vault

```
vault/
  Notas/Reunión con Acme.md
  Notas/Proyectos/MyDamnManagement.md
  Bases/Contactos.md              # la base de datos es un fichero
  Bases/Contactos/Acme.md         # cada fila es un fichero
  _attachments/2026/10/abc.png
  .trash/2026-10-29T18-04-11/<ruta original>
```

Nota:

```markdown
---
id: 7f3c1a2e-...          # identidad estable; sobrevive a renombrados y movimientos
icon: "📄"
description: "una línea opcional"
created: 2026-10-28T20:35:00Z
updated: 2026-10-29T09:12:44Z
---

Cuerpo en Markdown, tal cual.
```

Base de datos (`Bases/Contactos.md`): `type: database`, `schema` y `views` en el frontmatter, y su `id` es el de la página (un fichero, una identidad). Sus filas son ficheros en `Bases/Contactos/`, con las propiedades en el frontmatter y **sin** campo `title`: el título es el nombre del fichero. Una base que cuelga de una nota vive en la carpeta de hijas de esa nota (`Notas/Proyecto/Contactos.md`, con sus filas en `Notas/Proyecto/Contactos/`): la jerarquía son carpetas (D4) y así el `parent_id` se deduce de la ruta. Una base no cuelga de otra base.

Cada vista va con su `id` (UUID), `name`, `type` y su configuración al mismo nivel (`group_by: fase`). Una vista escrita a mano con un `id` que no es UUID (`v1`) se indexa igual: se le da uno derivado y estable de su base, su nombre y su posición, y se materializa al guardarla desde la app. Las filas llevan además `created`/`updated`, para que sus fechas sobrevivan a un reindexado sin depender del `mtime` del fichero.

### Reglas de escritura

- **Escritura atómica**: `x.md.tmp` + `rename()`.
- **Nombre de fichero legible** (decisión del dueño): el fichero se llama como el título saneado y se renombra al cambiar el título. El `id` del frontmatter es la identidad.
- **Saneado**: caracteres prohibidos fuera, NFC al leer y al escribir, colisión con ` (2)`, ` (3)` sin distinguir mayúsculas, truncado a 180 bytes, nombres reservados de Windows evitados. Escapar del vault (`..`, `/`, symlinks) es error, no se sanea en silencio.
- **`title` es clave reservada**: una base de datos no puede declarar una propiedad llamada `title`.
- **`id` duplicado** (fichero copiado a mano): se acepta como página duplicada, se avisa, y al guardar la copia se le materializa un `id` propio.
- **`id` de vista duplicado** (el mismo caso, en el frontmatter de una base): la primera base por ruta se queda con los `id` del fichero y la copia recibe ids derivados y estables (uuid5 de su base, posición y nombre), con aviso; al guardar la copia se materializan en su fichero. Copiar un `.md` nunca deja el índice sin poder reconstruirse.
- **Codificación**: UTF-8 sin BOM, saltos `\n`.
- **Tolerancia**: CRLF, BOM, Latin-1 y frontmatter sin cerrar se leen con aviso y nunca abortan el reindexado.
- **Claves desconocidas del frontmatter**: se preservan reescribiendo solo las claves que gestiona la app.
- **Claves que añade la app** además de las del contrato: `created_by` (quién creó la página; hoy sin login, el valor de `DEFAULT_USER_NAME`) y `header_image` (la imagen de cabecera). Van en el frontmatter y se leen de ahí al reindexar: si solo vivieran en el índice, el primer reindexado las perdería. Son una extensión de este ADR, no parte del mínimo.
- **Adjuntos dentro del vault**: todo adjunto (hoy, la imagen de cabecera) vive en `_attachments/<año>/<mes>/<nombre>`, con año y mes UTC del momento en que se subió. `_attachments/` no se indexa. Copiar la carpeta del vault es por tanto un backup completo, imágenes incluidas, y la copia arranca sola.
- **`header_image` es una URL que *es* la ruta del vault**: `/files/_attachments/2026/10/header-<id>.png`. Se sirve con el `Content-Type` decidido por los magic bytes del propio fichero (PNG, JPEG, GIF, WebP), `X-Content-Type-Options: nosniff` y descarga forzada para todo lo demás: el vault no es un servidor de ficheros, y nada de lo que haya dentro se ejecuta en el origen de la app (no hay CSP, porque el panel usa estilos y scripts inline). Fuera de `_attachments/`, 404.
- **Papelera**: borrar mueve a `.trash/<fecha UTC>/<ruta original>`. `GET /trash` lista lo que hay (ruta original, fecha del borrado y tamaño) y `POST /trash/restore` devuelve un fichero a su sitio y reindexa. Borrar una base mueve su `.md` y los de todas sus filas; borrar una fila mueve solo su fichero. Si la ruta original está ocupada — por un fichero, o por un fichero donde debería ir una de sus carpetas — es **409** con el motivo: nunca un `(2)` silencioso, que dejaría dos versiones sin decir cuál es cuál, y nunca un 500 mudo por el estado del disco.

### Enlaces

Wikilinks resueltos por título, con desempate por carpeta más cercana → ruta más corta → orden alfabético, avisando del número de candidatas y deduplicando. Enlaces a encabezados: v1.1.

## Contrato del índice

Tablas `pages`, `databases`, `rows`, `links`, `pages_fts` (+ internas de FTS5). `PRAGMA user_version` fija la versión del esquema: un índice de versión desconocida se tira y se reconstruye, nunca se migra.

Búsqueda: FTS5 con `unicode61 remove_diacritics 2` y prefijo. `reunion` encuentra `Reunión`. Coste aceptado: `ñ` se pliega a `n`.

Idempotencia: reconstruir dos veces, o borrar y reconstruir, produce el mismo contenido en las tablas de contenido, comparado por hash de volcado ordenado.

`meta` es una tabla clave-valor con metadatos **del índice**, no del vault: hoy solo `last_reindex_at`. Queda fuera de esa huella (cambia en cada reindexado por definición) y es lo que permite que `GET /status` diga la deriva del índice contra el disco: ficheros modificados, añadidos y desaparecidos desde el último reindexado, comparando `mtime` + tamaño, con un `stat` por fichero y sin leer cuerpos. Subir el esquema a la versión 2 fue solo para añadir `meta`, con migración desde la 1: los índices existentes no se tiran. Desde P2 la deriva cuenta **todos** los `.md` indexados (notas, bases y filas): no queda ningún fichero del vault que el índice ignore a propósito, así que un `added` es siempre algo que la app todavía no conoce y desaparece al reindexar.

## Consecuencias

- Rebuild de 5.033 ficheros: 0,32 s en disco local; ~21 s sobre el montaje 9p de desarrollo. El objetivo de 5 s se mide en disco local. Mitigación si el vault acaba en red: reindexado incremental por `mtime` + tamaño.
- Deriva de `/status` (P1.2b, medida con 5.000 notas): **~20–40 ms** en disco local (22 ms en la medición original y 36 ms al repetirla sobre el overlay de WSL) y ~7,2 s sobre el montaje 9p de desarrollo (`/opt/data`, medido: 7,1–7,5 s) — el mismo factor que el reindexado, porque es la misma pasada de `scandir`. Un `stat` por fichero y ningún cuerpo leído: en el disco donde corre la app no se nota (el panel pide `/status` una vez al cargar).
- Un solo escritor: el despliegue debe garantizar un único contenedor.
- Índice corrupto: debe detectarse al arrancar y reconstruirse, no reventar.
- FTS5 depende de la versión de SQLite (mínimo 3.27): fijarla en la imagen.
- Temporales huérfanos: el lector los ignora; conviene barrerlos al arrancar.
