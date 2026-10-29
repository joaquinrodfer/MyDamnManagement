# `bookworm` fija la distribución, y con ella la versión de SQLite que trae
# Debian (3.40.1). El tag `python:3.12-slim` a secas no la fija: al cambiar de
# base cambia el motor sin que nada avise. Aquí los tests corren sobre 3.53.1;
# la app exige >= 3.27 (`remove_diacritics 2` de FTS5, ver `app/index.py:
# MIN_SQLITE_VERSION`) y lo comprueba en cada conexión.
FROM python:3.12-slim-bookworm

WORKDIR /app

# Si la base cambia y trae un SQLite más viejo del que el esquema necesita, el
# build falla aquí en vez de en la primera búsqueda con acentos.
RUN python -c "import sqlite3, sys; \
    v = tuple(int(p) for p in sqlite3.sqlite_version.split('.')); \
    print('SQLite de la imagen:', sqlite3.sqlite_version); \
    sys.exit(0 if v >= (3, 27, 0) else 'SQLite demasiado viejo para remove_diacritics 2')"

# Sin libpq/gcc: ya no se compila psycopg2 (no hay Postgres) ni hace falta
# ningún módulo nativo fuera de las ruedas que trae pip.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# El contexto de build es la raíz del repo (ver "context: ." en
# docker-compose.yml), no backend/ -- así se puede copiar frontend/ aquí
# dentro y la imagen queda autocontenida. En desarrollo, los bind-mounts de
# docker-compose.yml (./backend/app, ./frontend...) se superponen a esto
# para hot-reload sin reconstruir; en un despliegue sin esos bind-mounts
# (Proxmox), la imagen ya trae todo consigo -- antes, sin esto, /app/frontend
# se creaba vacío y el panel no se servía fuera de un entorno con el
# bind-mount de desarrollo puesto.
COPY backend/ ./
COPY frontend/ ./frontend/

# /data es donde vive el índice SQLite (volumen fuera del repo). Se crea aquí
# para que un contenedor limpio arranque sin montar nada.
RUN mkdir -p /app/attachments /data

EXPOSE 8000

# Sin `alembic upgrade head`: el esquema del índice se crea/migra al arrancar
# (PRAGMA user_version, ver backend/app/index.py).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
