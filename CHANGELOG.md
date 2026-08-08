# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). Versionado según [SemVer](https://semver.org/lang/es/): cada fase del roadmap avanza la versión menor (`0.x.0`) hasta que el proyecto se considere estable.

## [Sin publicar]

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
