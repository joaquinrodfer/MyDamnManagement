# MyDamnManagement

Notas en Markdown + bases de datos tipo Notion + CRM + gestor de tareas — autoalojado, sin depender de terceros. Un único motor genérico de páginas/bases de datos/vistas del que salen los tres productos.

## Qué es

No hay tres subsistemas separados. Hay un motor con tres entidades:

- **`page`** — una página (nota, base de datos, o fila de una base de datos).
- **`database`** — define un esquema de propiedades tipadas (texto, número, fecha, relación...).
- **`view`** — una forma de mirar esas filas (tabla, tablero kanban, calendario).

CRM y Tareas/Proyectos son configuraciones de `database` + `view`, no código aparte. Notas y wiki son páginas con `type = note` y cuerpo en Markdown, con wikilinks y backlinks.

El detalle completo, con diagramas, está en [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Estado actual

Hay un **panel visual real** en [http://localhost:8000/](http://localhost:8000/) — árbol de páginas, editor de notas con vista previa de wikilinks y backlinks en vivo, y tablas/tableros para las bases de datos. Es HTML/CSS/JS plano (`frontend/index.html` + `frontend/app.js`), sin build ni dependencias, servido por la propia API. Adelanta la parte de interfaz de la Fase 4 porque hacía falta poder ver y tocar lo de las Fases 1–2 sin `curl`.

## Cómo arrancarlo (desarrollo)

Requiere Docker Desktop (Windows, backend WSL2 recomendado).

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:8000/health
```

El propio contenedor `api` aplica las migraciones de Alembic (`alembic upgrade head`) antes de arrancar Uvicorn — no hay que hacer nada aparte. Si generas una migración nueva tras cambiar `backend/app/models.py`:

```bash
docker compose run --rm api alembic revision --autogenerate -m "descripción del cambio"
docker compose run --rm api alembic upgrade head
```

**Revisa siempre el archivo generado antes de aplicarlo.** `pages` y `databases` se referencian mutuamente, y autogenerate no resuelve bien ese ciclo (avisa con un `SAWarning` y puede ordenar las tablas de forma que la migración falle al aplicarse). Es exactamente lo que pasó en la migración inicial — se corrigió a mano separando la FK circular con `create_foreign_key`; usa esa migración como referencia si vuelve a pasar.

Documentación interactiva de la API en [http://localhost:8000/docs](http://localhost:8000/docs). Panel visual en [http://localhost:8000/](http://localhost:8000/) (estado de `api`/`db` en la cabecera, árbol de páginas y bases de datos a la izquierda).

## Panel visual

- **Páginas**: `+` crea una nota (`Sin título`, lista para renombrar). Editor con título, cuerpo Markdown, vista previa con `[[wikilinks]]` resueltos en vivo contra el árbol cargado, y lista de backlinks.
- **Bases de datos**: `+` abre el diálogo de creación — nombre y propiedades (clave, nombre visible, tipo; `select`/`multiselect` piden opciones separadas por coma). El formulario de "+ Nueva fila" genera automáticamente el input correcto según el tipo declarado (texto, número, `select`, checkbox, fecha).
- **Vistas**: `+ Vista` crea una vista `table`/`board`/`list`/`calendar`; para `board` se elige la propiedad de agrupación y las columnas salen de sus `options` (o de los valores encontrados si no las tiene).
- **Buscar**: el cuadro de la barra lateral llama a `/search` con un pequeño debounce.

## Endpoints disponibles

| Endpoint       | Método | Qué hace                                              |
| -------------- | ------ | ------------------------------------------------------ |
| `/`            | GET    | Panel de estado (frontend estático, sin build)           |
| `/health`      | GET    | Comprobación de vida del servicio                       |
| `/status`      | GET    | Estado agregado de `api` y `db` (usado por el panel)     |
| `/pages`       | GET    | Lista todas las páginas del workspace                   |
| `/pages`       | POST   | Crea página + `page_content` en una transacción         |
| `/pages/{id}`  | GET    | Lee una página con su cuerpo                             |
| `/pages/{id}`  | PATCH  | Actualiza título, icono o cuerpo                         |
| `/pages/{id}`  | DELETE | Borra la página (cascada a contenido/adjuntos/etiquetas) |
| `/pages/tree`  | GET    | Árbol de páginas por `parent_id`                        |
| `/pages/{id}/backlinks` | GET | Páginas que enlazan a esta vía `[[wikilink]]`     |
| `/search?q=`   | GET    | Búsqueda full-text (título + cuerpo) en español          |

Los wikilinks se escriben como `[[Título de la página]]` (o `[[Título\|Alias]]`) en `body_markdown`; se resuelven por título exacto (sin distinguir mayúsculas) dentro del mismo workspace al guardar la página.

### Motor de `database` + `view`

| Endpoint | Método | Qué hace |
| --- | --- | --- |
| `/databases` | POST | Crea una `database` (página + `schema_def`) |
| `/databases` | GET | Lista todas las databases |
| `/databases/{id}` | GET / PATCH / DELETE | Leer, editar (título/icono/schema) o borrar (arrastra sus filas) |
| `/databases/{id}/rows` | POST | Crea una fila; valida `properties` contra el `schema_def` |
| `/databases/{id}/rows` | GET | Lista filas; con `?view={id}` aplica el filtro/orden de esa vista |
| `/databases/{id}/rows/{id}` | GET / PATCH / DELETE | Leer, editar (merge parcial de `properties`) o borrar una fila |
| `/databases/{id}/views` | POST / GET | Crear o listar vistas (`table`\|`board`\|`calendar`\|`list`) |
| `/databases/{id}/views/{id}` | PATCH / DELETE | Editar o borrar una vista |

Una `view.config` tiene esta forma: `{"filters": [{"key":"fase","op":"eq","value":"ganado"}], "sort": {"key":"valor","dir":"desc"}, "group_by": "fase", "visible_properties": ["fase","valor"]}`. `filters` y `sort` los aplica el backend; `group_by` y `visible_properties` son metadatos para que el frontend decida cómo dibujar la vista.

## Roadmap

- [x] **Fase 0** — Compose + Postgres + API mínima
- [x] **Fase 1** — Notas/wiki: árbol de páginas, wikilinks + backlinks, búsqueda full-text, Alembic
- [x] **Fase 2** — Motor de `database` + `view` genéricos (tabla, board, calendario)
- [x] **Panel visual** (adelanto de Fase 4) — árbol, editor de notas, tablas/tableros, todo sin build
- [ ] **Fase 3** — CRM y Tareas como plantillas preconfiguradas (crear ambas con un clic, no a mano cada vez)
- [ ] **Fase 4** — Despliegue en Proxmox + Tailscale, backups automatizados, editor más rico

## Despliegue objetivo

Desarrollo en Windows con Docker Desktop; a producción sobre un LXC/VM de Proxmox personal, accesible solo a través de una red [Tailscale](https://tailscale.com/) (sin exponer puertos públicos, sin gestión de cuentas por ahora — uso personal en solitario). El stack de contenedores no cambia entre ambos entornos.

## Licencia

[MIT](LICENSE)
