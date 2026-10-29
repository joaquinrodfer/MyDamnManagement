from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración de la app, leída de variables de entorno (ver .env.example).

    Ya no hay `DATABASE_URL`: la app vive de un único fichero SQLite
    (`INDEX_PATH`), y a partir de P1.2 ese índice será derivado del vault, que
    es lo que de verdad hay que respaldar. En Docker Compose, INDEX_PATH apunta
    a un volumen fuera del repo (`/data`).
    """

    index_path: str = "/data/index.sqlite"

    # Dónde se sirven el panel y los adjuntos subidos (imágenes de cabecera).
    # Configurables para poder arrancar la app en tests sin /app.
    frontend_dir: str = "/app/frontend"
    attachments_dir: str = "/app/attachments"

    # Sin autenticación todavía (uso personal en solitario) -- "creador" de
    # una página se rellena con esto. El día que haya login de verdad, se
    # sustituye por el usuario autenticado sin tocar el resto del modelo.
    default_user_name: str = "Tú"
    default_icon: str = "📄"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
