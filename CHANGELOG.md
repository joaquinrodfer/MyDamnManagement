# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). Versionado según [SemVer](https://semver.org/lang/es/): cada fase del roadmap avanza la versión menor (`0.x.0`) hasta que el proyecto se considere estable.

## [Sin publicar]

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
