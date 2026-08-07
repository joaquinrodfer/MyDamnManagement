# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). Versionado según [SemVer](https://semver.org/lang/es/): cada fase del roadmap avanza la versión menor (`0.x.0`) hasta que el proyecto se considere estable.

## [Sin publicar]

## [0.1.0] — Fase 0: esqueleto

### Añadido

- Docker Compose con `api` (FastAPI + Uvicorn) y `db` (Postgres 16).
- Modelo de datos completo: `workspace`, `page`, `page_content`, `databases`, `view`, `link`, `tag`, `attachment`.
- CRUD de `/pages` (GET, POST, PATCH, DELETE) probado de extremo a extremo.
- Documentación de arquitectura con diagramas ([`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)).
