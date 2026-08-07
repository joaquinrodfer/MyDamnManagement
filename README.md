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

**Fase 0** — esqueleto funcionando de extremo a extremo: Docker Compose (Postgres + FastAPI), modelo de datos completo, CRUD de páginas probado.

## Cómo arrancarlo (desarrollo)

Requiere Docker Desktop (Windows, backend WSL2 recomendado).

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:8000/health
```

Documentación interactiva de la API en [http://localhost:8000/docs](http://localhost:8000/docs). Panel de estado de los servicios en [http://localhost:8000/](http://localhost:8000/).

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

## Roadmap

- [x] **Fase 0** — Compose + Postgres + API mínima
- [ ] **Fase 1** — Notas/wiki: árbol de páginas, wikilinks + backlinks, búsqueda full-text, Alembic
- [ ] **Fase 2** — Motor de `database` + `view` genéricos (tabla, board, calendario)
- [ ] **Fase 3** — CRM y Tareas como plantillas del motor de la Fase 2
- [ ] **Fase 4** — Frontend, despliegue en Proxmox + Tailscale, backups automatizados

## Despliegue objetivo

Desarrollo en Windows con Docker Desktop; a producción sobre un LXC/VM de Proxmox personal, accesible solo a través de una red [Tailscale](https://tailscale.com/) (sin exponer puertos públicos, sin gestión de cuentas por ahora — uso personal en solitario). El stack de contenedores no cambia entre ambos entornos.

## Licencia

[MIT](LICENSE)
