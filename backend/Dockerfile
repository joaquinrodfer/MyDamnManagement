FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

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
COPY backend/ .
COPY frontend/ ./frontend/

RUN mkdir -p /app/attachments

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
