# Arquitectura

## 1. Un motor, tres productos

`CRM` y `Tareas/Proyectos` no tienen tablas propias: son instancias de `database` con un `schema_def` concreto y una vista `board` encima. Las notas son el caso más simple — una `page` con `type = note` y cuerpo en Markdown. Las tres viven en la misma tabla `pages`.

```mermaid
graph LR
    ENGINE["Motor genérico<br/>page · database · view<br/>properties: JSONB"]
    NOTES["Notas / Wiki<br/>type = note<br/>body_markdown + wikilinks"]
    CRM["CRM<br/>database 'Contactos' · vista board<br/>schema: Empresa, Fase, Valor"]
    TASKS["Tareas / Proyectos<br/>database 'Tareas' · vista board+calendar<br/>schema: Estado, Prioridad, Fecha"]

    ENGINE --> NOTES
    ENGINE --> CRM
    ENGINE --> TASKS
```

## 2. Modelo de datos

`page` es la entidad bisagra: según su `type` se resuelve contra `page_content` (nota) o contra `databases` (fila de una base de datos). Vistas, enlaces, etiquetas y adjuntos cuelgan de ahí.

```mermaid
erDiagram
    WORKSPACE ||--o{ PAGE : "1 → N"
    PAGE ||--o| PAGE_CONTENT : "1-1 · type=note"
    PAGE ||--o| DATABASES : "1-1 · type=database"
    DATABASES ||--o{ VIEW : "1 → N"
    PAGE }o--o{ TAG : "N-N · page_tags"
    PAGE ||--o{ ATTACHMENT : "1 → N"
    PAGE ||--o{ LINK : origen
    PAGE ||--o{ LINK : destino

    WORKSPACE {
        uuid id
        string name
    }
    PAGE {
        uuid id
        uuid workspace_id
        uuid parent_id
        enum type
        string title
        jsonb properties
    }
    PAGE_CONTENT {
        uuid page_id
        text body_markdown
    }
    DATABASES {
        uuid id
        uuid page_id
        jsonb schema_def
    }
    VIEW {
        uuid id
        uuid database_id
        enum type
        jsonb config
    }
    LINK {
        uuid id
        uuid source_page_id
        uuid target_page_id
        enum kind
    }
    TAG {
        uuid id
        string name
    }
    ATTACHMENT {
        uuid id
        uuid page_id
        string filename
    }
```

## 3. Arquitectura de contenedores

Dos servicios en `docker-compose.yml`: `api` (FastAPI + Uvicorn) y `db` (Postgres). El bind-mount de `./backend/app` solo existe en desarrollo, para recarga en caliente.

```mermaid
graph TD
    CLIENT["Cliente<br/>navegador / futuro frontend"] -->|":8000 — dev: localhost · prod: tailnet"| API

    subgraph COMPOSE["docker-compose.yml"]
        API["api<br/>FastAPI + uvicorn --reload<br/>:8000"] -->|"SQLAlchemy (psycopg2) :5432"| DB["db<br/>postgres:16-alpine"]
        API --- ATT[("vol: attachments")]
        DB --- DATA[("vol: db_data")]
    end
```

## 4. Flujo de una petición: `POST /pages`

El caso ya implementado y probado. Todo en una única transacción — `page` y `page_content` se confirman juntas o ninguna.

```mermaid
sequenceDiagram
    participant C as Cliente
    participant A as API (FastAPI)
    participant P as Postgres

    C->>A: POST /pages {title, body_markdown}
    A->>P: INSERT page + INSERT page_content (misma transacción)
    P-->>A: commit OK
    A-->>C: 200 · PageRead (JSON)
```

## 5. Desarrollo (Windows) → Producción (Proxmox)

El stack de contenedores no cambia entre entornos — solo cómo se llega a él.

```mermaid
graph LR
    subgraph DEV["Ahora — Windows · Docker Desktop (WSL2)"]
        C1["docker-compose<br/>api + db + volúmenes"] --> L1["http://localhost:8000<br/>acceso directo"]
    end

    subgraph PROD["~1 mes — Proxmox · LXC/VM"]
        C2["docker-compose<br/>api + db + volúmenes"] --> TS["Tailscale<br/>tailnet cifrado"] --> D2["dispositivos del tailnet<br/>sin puertos públicos"]
    end

    C1 -.->|"migración: pg_dump → pg_restore"| C2
```

## 6. Wikilinks: de texto a backlink

Los wikilinks se resuelven **al guardar**, no al leer — el cuerpo se escanea, se buscan páginas cuyo título coincida (sin distinguir mayúsculas) y se recalculan los `Link` salientes de esa página desde cero cada vez.

```mermaid
sequenceDiagram
    participant C as Cliente
    participant A as API (FastAPI)
    participant P as Postgres

    C->>A: PATCH /pages/{id} {body_markdown: "...[[Trámites EPSJ]]..."}
    A->>A: extraer títulos referenciados (wikilinks.py)
    A->>P: DELETE links salientes de {id}
    A->>P: SELECT pages WHERE lower(title) IN (títulos)
    A->>P: INSERT un Link por cada página encontrada
    P-->>A: commit OK
    A-->>C: 200 · PageRead
```

Limitación conocida: renombrar una página no repara los wikilinks que otras páginas ya guardaron apuntando al título antiguo — se corrigen la próxima vez que esa página de origen se vuelva a guardar.

## 7. El motor de `database` + `view`, probado con dos dominios reales

Esto es lo que hace innecesario tener código de "CRM" o de "Tareas": el mismo endpoint genérico, con dos `schema_def` distintos, produce dos productos distintos.

```mermaid
graph TD
    ENGINE["POST /databases<br/>{title, schema_def}"]
    CRM_DEF["Contactos<br/>schema_def: empresa(text), fase(select), valor(number)"]
    TASK_DEF["Tareas<br/>schema_def: estado(select), prioridad(select), hecha(checkbox)"]

    ENGINE --> CRM_DEF
    ENGINE --> TASK_DEF

    CRM_DEF --> CRM_ROWS["filas: Acme, Beta, Gamma…<br/>POST /databases/{id}/rows"]
    TASK_DEF --> TASK_ROWS["filas: Escribir memoria…<br/>POST /databases/{id}/rows"]

    CRM_ROWS --> CRM_VIEW["vista board 'Ganados'<br/>filters: fase = ganado<br/>sort: valor desc"]
    CRM_VIEW --> CRM_RESULT["GET /databases/{id}/rows?view={id}<br/>→ Beta (5000), Gamma (800)"]
```

`properties.py` valida cada fila contra el `schema_def` de su database al guardar (claves conocidas, tipo básico, opciones de `select`) — sin eso, un CRM y un gestor de tareas comparten tabla sin ninguna garantía de forma, y los errores se descubrirían leyendo, no escribiendo.

### Fase 3: las plantillas son datos, no motor nuevo

`POST /databases/from-template` no añade capacidades al motor — llama exactamente a lo mismo que `POST /databases` + `POST /databases/{id}/views`, varias veces, con el `schema_def` y las `views` que trae `backend/app/templates.py` en vez de escritos a mano. Si en algún momento hiciera falta lógica propia para "crear un CRM" que no fuera reducible a datos sobre el motor genérico, sería la señal de que el motor no es tan genérico como parece.

## 8. Panel visual: cliente puro sobre la misma API

`frontend/` no es una app aparte con su propio backend — es HTML/CSS/JS servido como archivos estáticos por `api` (`StaticFiles`, montado al final de las rutas) que llama a los mismos endpoints que se probaron por `curl`. No hay build, no hay framework, no hay estado en el servidor propio del frontend.

```mermaid
graph LR
    B["Navegador"] -->|"GET /"| S["StaticFiles<br/>frontend/index.html + app.js"]
    B -->|"fetch /pages/tree, /pages/{id},<br/>/databases/{id}/rows, /search…"| API["Misma API FastAPI"]
    API --> DB[(Postgres)]
```

Un matiz importante: el editor resuelve `[[wikilinks]]` **en el cliente**, contra el árbol ya cargado (`state.pagesById`) — es puramente visual, para que el enlace se vea coloreado y navegable al escribir. El backlink real (`Link` en Postgres) solo existe si la página de origen se ha *guardado* después de que la página destino existiera; son dos resoluciones independientes y pueden desincronizarse temporalmente (se vio al probarlo: el editor ya mostraba el enlace resuelto antes de que `Backlinks` en la página destino pasara de 0 a 1).

Dos IDs que no son el mismo, y un bug real que salió de ahí: una `page` de `type=database` tiene su propio `id` (fila `pages`) y, por separado, el `id` de la fila `databases` que la describe (`DatabaseDef.id`). `GET /pages/tree` construye sus nodos a partir de `pages`, así que devolvía el primero — pero `GET /databases/{id}` espera el segundo. El árbol y la búsqueda navegaban con el id equivocado y cada clic sobre una base de datos fallaba con 404 sin ningún aviso visible. Se corrigió incluyendo `database_id` en cada nodo del árbol; quedó documentado aquí porque es fácil reintroducir el mismo error en cualquier endpoint nuevo que mezcle ambos ids.

### El editor: única pieza compilada del frontend

`frontend/app.js` sigue siendo JS plano sin build, pero el editor de notas es CodeMirror 6 (`frontend/editor-src/entry.js`, compilado una vez con esbuild a `frontend/vendor/editor.bundle.js`, vendorizado y commiteado). La razón por la que esto no podía ser un `<textarea>` con un panel de vista previa aparte: un `<textarea>` es texto plano uniforme, no puede tener una línea con letra grande (un `# Encabezado`) y otra con letra normal a la vez — eso exige gestión real de cursor/selección sobre contenido con formato mixto, que es exactamente lo que resuelve un editor de verdad y no una caja de texto nativa del navegador.

CodeMirror aplica clases CSS (`cm-mdm-h1`, `cm-mdm-mark`, `cm-mdm-wikilink`...) vía un `HighlightStyle` sobre el árbol de sintaxis de `@lezer/markdown`; los colores y tamaños concretos viven en el `<style>` de `index.html`, no en el bundle del editor — así el editor no sabe nada de temas claro/oscuro, solo de qué es cada cosa. Detalle no obvio: `HeaderMark`/`EmphasisMark` (los `#`/`**` en sí) llegan con **dos clases a la vez** (p. ej. `cm-mdm-h1 cm-mdm-mark`), porque heredan el tag del encabezado que los contiene Y tienen su propio tag de marcador — hace falta un selector combinado (`.cm-mdm-h1.cm-mdm-mark`) con más especificidad para que el marcador gane en tamaño sobre el encabezado. El caret usa `caret-color` en vez de la extensión `drawSelection()` de CodeMirror — no la necesitamos y así hay una capa menos.

### Autoguardado: debounce + flush, no un modelo de "documento colaborativo"

El editor no manda cada pulsación al servidor — programa un `PATCH` 2s después del último cambio (`setTimeout` que se reinicia con cada pulsación nueva). El único punto delicado es no perder ese margen de <2s cuando el usuario navega antes de que salte el temporizador: `clearMain()` (llamada al entrar en cualquier otra vista) fuerza el guardado pendiente primero, y `beforeunload` hace lo mismo al cerrar la pestaña. No hay resolución de conflictos ni bloqueo optimista — con un único usuario escribiendo, no hace falta; si esto pasa a ser multiusuario de verdad, este es el punto exacto que necesitaría revisarse.

## Edición por bloques + selección múltiple (en diseño, no construido aún)

Se pidió edición por bloques al estilo Notion (párrafo, H1–H4, listas, código, convertibles entre sí) con selección múltiple de bloques y de páginas, y menú contextual con acciones en lote. Es, con diferencia, la pieza más grande pedida hasta ahora — más que todo lo anterior junto — así que antes de construirla merece dejar por escrito la disyuntiva real:

- **Bloques de verdad**: `body_markdown` deja de ser un string y pasa a ser una lista de objetos `{id, type, content}` (tabla `block` o JSONB). Es como funciona Notion por dentro. Coste: reescribir wikilinks/backlinks/búsqueda/vista previa para operar sobre una lista de bloques en vez de un string, más un editor por bloque con su propia gestión de cursor (Enter crea bloque, Backspace al principio fusiona con el anterior...) — el mismo problema de cursor que ya se evitó al elegir CodeMirror en vez de `contenteditable` a pelo, multiplicado por cada bloque.
- **Bloques "vistos", no "de verdad"**: `body_markdown` sigue siendo un único string (nada de lo ya construido — wikilinks, backlinks, búsqueda — cambia). CodeMirror ya parsea el documento en un árbol de sintaxis que distingue párrafo/encabezado/lista/código; con eso basta para calcular, en cada momento, qué rango de texto es "un bloque". Seleccionar un bloque = seleccionar ese rango (CodeMirror admite selecciones múltiples de forma nativa). Convertir un bloque = reescribir el texto de ese rango (quitar `## ` y poner `- `, etc.). Borrar en lote = borrar esos rangos. Todo el motor de backend sigue intacto.

La segunda opción es sustancialmente menos trabajo y no toca nada que ya funcione, a cambio de que "bloque" sea una vista calculada sobre el texto en vez de un objeto con identidad propia (sin reordenar por arrastre "gratis", por ejemplo — habría que construirlo aparte si se quiere). Para lo que se ha pedido (seleccionar, convertir tipo, borrar en lote) cubre el 100% sin ese coste. Pendiente de confirmar con el usuario antes de empezar.

## Decisiones y por qué

| Decisión | Elegido | Por qué |
| --- | --- | --- |
| Storage de notas | Postgres, no archivos `.md` | Elimina el problema de sincronizar archivos entre dispositivos — un único servidor es la fuente de verdad |
| Propiedades de `database` | JSONB flexible | Añadir un campo nuevo no requiere migración de esquema |
| Migraciones | Alembic desde Fase 1 | `create_all()` sirvió para iterar rápido en Fase 0; Alembic entra ahora que el modelo tiene datos reales que preservar. La FK circular `pages` ↔ `databases` rompe el autogenerate por defecto — ver comentario en la migración inicial |
| Resolución de wikilinks | Por título, al guardar (no al leer) | Guardar es poco frecuente, leer es constante — recalcular en cada lectura sería más caro sin necesidad |
| Búsqueda | Postgres `tsvector`/`tsquery` (`spanish`) | A esta escala (1 usuario) no justifica una pieza aparte tipo Meilisearch; se puede migrar si hace falta |
| Validación de `properties` | Ligera (claves + tipo básico), no estricta | Coherente con "JSONB flexible": atrapa errores obvios (typos, opciones inválidas) sin forzar campos obligatorios ni bloquear la iteración del schema |
| Filtro/orden de vistas | En Python sobre filas ya cargadas, no SQL dinámico sobre JSONB | A esta escala (una persona, cientos de filas como mucho) es más simple y igual de rápido que construir predicados SQL contra JSONB; se puede mover a SQL si el volumen lo justifica |
| Acceso remoto | Tailscale, sin auth propia (aún) | Uso personal en solitario; `user_id`/`workspace_id` ya están en el esquema para añadir auth real sin rediseñar |
| API | REST | Un solo tipo de cliente, sin problema real de over/under-fetching a esta escala |
| Frontend | HTML/CSS/JS plano, sin build, servido por `api` | "Un pequeño panel" no justifica un segundo contenedor, un bundler ni un framework; cuando el proyecto lo pida (Fase 4), esto es lo primero que se reemplaza sin tocar la API |
| Editor de notas | CodeMirror 6, vendorizado (build único, no en cada edición) | Se planteó React+Vite para todo el frontend; el problema real (editor bueno) lo resuelve una librería de edición dedicada, no el framework — así que se compiló solo esa pieza y el resto sigue sin build |
| Caché de estáticos | `Cache-Control: no-cache` (revalida por ETag) | Sin cabecera, el navegador puede servir una versión vieja de `app.js`/`index.html` sin avisar — pasó de verdad durante el desarrollo del editor |
| Guardado | Autoguardado con debounce de 2s + flush al navegar/cerrar | Un botón "Guardar" explícito no encaja con un editor con formato en vivo; a un solo usuario no hace falta resolución de conflictos, solo no perder el margen del debounce |
| Imagen de cabecera | Un campo `header_image_path` en `page` + `/files` estático | Más simple que enrutar por el modelo `Attachment` genérico (que sigue sin usarse); si attachments arbitrarios se vuelven necesarios, se generaliza entonces |
| "Obligatorio" en icono/creador | Aplicado por la API al crear, no por `NOT NULL` en la columna | Garantiza el invariante para páginas nuevas sin forzar una migración de backfill sobre las que ya existen |
