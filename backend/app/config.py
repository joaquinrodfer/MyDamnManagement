from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración de la app, leída de variables de entorno (ver .env.example).

    En Docker Compose, DATABASE_URL la inyecta el propio compose (apunta al
    servicio `db`). En Proxmox no cambia nada de este archivo: solo cambia
    el valor de las variables de entorno en el .env del host.
    """

    database_url: str = "postgresql+psycopg2://mdm:mdm@localhost:5432/mydamnmanagement"

    # Sin autenticación todavía (uso personal en solitario) -- "creador" de
    # una página se rellena con esto. El día que haya login de verdad, se
    # sustituye por el usuario autenticado sin tocar el resto del modelo.
    default_user_name: str = "Tú"
    default_icon: str = "📄"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
