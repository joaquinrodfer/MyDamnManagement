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
