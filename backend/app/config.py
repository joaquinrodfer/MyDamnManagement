from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración de la app, leída de variables de entorno (ver .env.example).

    Ya no hay `DATABASE_URL`: la app vive de un único fichero SQLite
    (`INDEX_PATH`) y, desde P1.2, de un vault de ficheros `.md` (`VAULT_PATH`)
    que es la verdad para las notas. El índice se guarda **fuera** del vault
    (ADR-0001: el vault tiene que quedar limpio y portable, y se puede copiar
    entero como backup sin llevarse la caché). En Docker Compose, los dos
    apuntan a volúmenes fuera del repo (`/data`).
    """

    index_path: str = "/data/index.sqlite"

    # Raíz del vault: `Notas/`, `_attachments/` y `.trash/` viven aquí.
    vault_path: str = "/data/vault"

    # Escape de arranque (no hay `?force=true` cuando el servicio no arranca):
    # si el vault está vacío a propósito --vaciado a mano, restore parcial del
    # volumen del vault con el índice intacto, `VAULT_PATH` a un directorio
    # vacío--, `VAULT_ALLOW_EMPTY=1` deja arrancar con 0 notas en el índice en
    # vez de abortar. Las bases, filas y vistas del índice se conservan (quedan
    # sin padre). No cubre una raíz inexistente: un volumen sin montar nunca se
    # lee como vault vacío.
    vault_allow_empty: bool = False

    # Documentación interactiva de la API (`/docs`, `/redoc` y
    # `/openapi.json`). `DOCS_ENABLED=0` las apaga: esas tres rutas pasan a
    # dar 404 y la app no publica el esquema OpenAPI. Solo hace eso — oculta
    # rutas: NO protege nada por sí mismo, el resto de la API sigue igual de
    # accesible. Por defecto encendida, porque en desarrollo la documentación
    # es útil; en producción se apaga en `docker-compose.prod.yml`.
    docs_enabled: bool = True

    # Vigilante del vault (P8): detecta los `.md` añadidos, modificados y
    # borrados por fuera de la API y reindexa solo esos ficheros, avisando al
    # navegador por `/events`. `WATCH_ENABLED=0` lo apaga del todo (hay
    # despliegues con el vault en un volumen que no conviene recorrer cada dos
    # segundos): entonces el vault se relee al arrancar y con `POST /reindex`,
    # como antes. `WATCH_INTERVAL` son los segundos entre pasadas; cada pasada
    # es un `scandir` con un `stat` por fichero, sin leer ningún cuerpo.
    watch_enabled: bool = True
    watch_interval: float = 2.0

    # Dónde se sirve el panel. Configurable para poder arrancar la app en
    # tests sin /app. Los adjuntos ya no tienen carpeta propia: viven dentro
    # del vault (`_attachments/`, ver `app/vault/attachments.py`).
    frontend_dir: str = "/app/frontend"

    # Sin autenticación todavía (uso personal en solitario) -- "creador" de
    # una página se rellena con esto. El día que haya login de verdad, se
    # sustituye por el usuario autenticado sin tocar el resto del modelo.
    default_user_name: str = "Tú"
    default_icon: str = "📄"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
