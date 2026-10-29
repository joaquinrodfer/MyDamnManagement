# ADR 0001 — Formato del vault y del índice

- **Estado:** aceptado (2026-10-29)
- **Contexto:** MyDamnManagement v1.0 — notas y bases de datos con almacenamiento en ficheros `.md`, interfaz tipo Notion, uso personal sobre Tailscale.
- **Validación:** spike P0 (`t_6fea05dc`), ejecutado y revisado de forma independiente. `run_p0.py`: 108/109 comprobaciones; el único fallo es de escala en el montaje 9p de desarrollo (21 s), que en disco local son 0,32 s con 5.033 ficheros.

Este ADR fija el contrato que P1 implementa.

## Decisiones

**D1 — Los ficheros son la verdad; el índice es reconstruible.** Ninguna escritura puede existir solo en SQLite. El índice se puede borrar y rehacer sin pérdida.

**D2 — Sin vigilante de ficheros en v1.0.** El índice se reconstruye al arrancar y con `POST /reindex`. Un solo escritor: la API.

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

Base de datos (`Bases/Contactos.md`): `type: database`, `schema` y `views` en el frontmatter. Sus filas son ficheros en `Bases/Contactos/`, con las propiedades en el frontmatter y **sin** campo `title`: el título es el nombre del fichero.

### Reglas de escritura

- **Escritura atómica**: `x.md.tmp` + `rename()`.
- **Nombre de fichero legible** (decisión del dueño): el fichero se llama como el título saneado y se renombra al cambiar el título. El `id` del frontmatter es la identidad.
- **Saneado**: caracteres prohibidos fuera, NFC al leer y al escribir, colisión con ` (2)`, ` (3)` sin distinguir mayúsculas, truncado a 180 bytes, nombres reservados de Windows evitados. Escapar del vault (`..`, `/`, symlinks) es error, no se sanea en silencio.
- **`title` es clave reservada**: una base de datos no puede declarar una propiedad llamada `title`.
- **`id` duplicado** (fichero copiado a mano): se acepta como página duplicada, se avisa, y al guardar la copia se le materializa un `id` propio.
- **Codificación**: UTF-8 sin BOM, saltos `\n`.
- **Tolerancia**: CRLF, BOM, Latin-1 y frontmatter sin cerrar se leen con aviso y nunca abortan el reindexado.
- **Claves desconocidas del frontmatter**: se preservan reescribiendo solo las claves que gestiona la app.
- **Papelera**: borrar mueve a `.trash/<fecha UTC>/<ruta original>`; restaurar devuelve a su sitio.

### Enlaces

Wikilinks resueltos por título, con desempate por carpeta más cercana → ruta más corta → orden alfabético, avisando del número de candidatas y deduplicando. Enlaces a encabezados: v1.1.

## Contrato del índice

Tablas `pages`, `databases`, `rows`, `links`, `pages_fts` (+ internas de FTS5). `PRAGMA user_version` fija la versión del esquema: un índice de versión desconocida se tira y se reconstruye, nunca se migra.

Búsqueda: FTS5 con `unicode61 remove_diacritics 2` y prefijo. `reunion` encuentra `Reunión`. Coste aceptado: `ñ` se pliega a `n`.

Idempotencia: reconstruir dos veces, o borrar y reconstruir, produce el mismo contenido en las 9 tablas, comparado por hash de volcado ordenado.

## Consecuencias

- Rebuild de 5.033 ficheros: 0,32 s en disco local; ~21 s sobre el montaje 9p de desarrollo. El objetivo de 5 s se mide en disco local. Mitigación si el vault acaba en red: reindexado incremental por `mtime` + tamaño.
- Un solo escritor: el despliegue debe garantizar un único contenedor.
- Índice corrupto: debe detectarse al arrancar y reconstruirse, no reventar.
- FTS5 depende de la versión de SQLite (mínimo 3.27): fijarla en la imagen.
- Temporales huérfanos: el lector los ignora; conviene barrerlos al arrancar.
