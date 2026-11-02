from typing import Optional

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

    # OBSOLETO desde P11a: el "creador" de una página es el `display_name` del
    # usuario de la sesión (`app/auth.py`), no un literal. Se deja el ajuste
    # para no romper un `.env` viejo y porque `app.repository.reply_to_suggestion`
    # lo usa como último recurso cuando le llaman sin autor (scripts, tests);
    # ninguna ruta HTTP lo usa ya. Desaparecerá cuando no quede nadie que
    # llame al repositorio sin sesión.
    default_user_name: str = "Tú"
    default_icon: str = "📄"

    # --- Usuarios y sesiones (P11a) -------------------------------------
    # `ADMIN_USER`/`ADMIN_PASSWORD` son opcionales y SOLO se usan en el primer
    # arranque, cuando el índice no tiene ningún usuario: si están, se crea ese
    # administrador al arrancar y `/` no pide el alta. Con usuarios ya en el
    # índice no se toca nada (ni se resetea la contraseña de nadie). El
    # `display_name` del admin creado es su propio username.
    admin_user: str = ""
    admin_password: str = ""

    # Días de vida de una sesión. Es **deslizante**: cada petición con cookie
    # válida la empuja a `ahora + SESSION_DAYS`, con un tope absoluto de 90
    # días desde que se abrió (`app.auth.ABSOLUTE_DAYS`).
    session_days: int = 30

    # `Secure` en la cookie de sesión. `None` (por defecto) lo deduce de cada
    # petición: `X-Forwarded-Proto: https` o esquema https. `SESSION_COOKIE_SECURE=1`
    # lo fuerza; `=0` lo apaga (útil en un desarrollo por http en otra máquina).
    session_cookie_secure: Optional[bool] = None

    # --- Puerta para agentes (P10b) -------------------------------------
    # `AGENT_TOKENS="token1:agente-hitos,token2:agente-revision"`: pares
    # token:nombre separados por comas (el orden es el del ejemplo: primero el
    # token, luego el nombre). El nombre del agente viaja dentro del token: el
    # cuerpo de la petición no puede elegirlo. Vacía o ausente -> los
    # endpoints `/agent/...` no existen (404), que es la forma más barata de no
    # anunciar una superficie que no está configurada.
    agent_tokens: str = ""

    # Tope del cuerpo que devuelve `GET /agent/context`, en bytes UTF-8. Una
    # nota de varios MB no puede volcarse entera a un LLM de una vez: se corta
    # y se dice (`body_truncated: true`).
    agent_context_max_body: int = 20 * 1024

    # Tope de sugerencias pendientes, para que un agente en bucle no llene una
    # nota de ruido. Dos contadores independientes: por nota (da igual quién
    # las proponga) y por agente (en todo el vault). Por encima, 429.
    agent_max_pending_note: int = 20
    agent_max_pending_agent: int = 20

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
