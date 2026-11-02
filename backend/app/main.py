import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import agent_auth, auth, capture_tokens, login_page
from app.config import settings
from app.index import ensure_index, open_index
from app.repository import Repository
from app.startup import ERROR as STARTUP_ERROR
from app.startup import STARTING as STARTUP_STARTING
from app.startup import startup_index
from app.routers import (
    agent,
    capture,
    dashboard,
    databases,
    events,
    files,
    graph,
    pages,
    search,
    status,
    suggestions,
    trash,
)
from app.routers import auth as auth_routes
from app.vault.store import EMPTY_VAULT_HINT_STARTUP, VaultError
from app.vault.watcher import VaultWatcher

log = logging.getLogger("mdm.main")


app = FastAPI(
    title="MyDamnManagement API",
    # `DOCS_ENABLED=0` apaga la documentación interactiva: `/docs`, `/redoc`
    # y `/openapi.json` dan 404 (ver `Settings.docs_enabled`). Con el ajuste
    # por defecto (encendido) el comportamiento es el de siempre.
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)

app.include_router(pages.router)
# `/capture` (P13): el endpoint que llaman los atajos del sistema operativo y
# `/inbox`, la bandeja que pinta el panel. Los dos piden identidad; un token
# personal de captura solo vale para `POST /capture` (ver el middleware).
app.include_router(capture.router)
app.include_router(databases.router)
app.include_router(status.router)
app.include_router(trash.router)
app.include_router(search.router)
app.include_router(dashboard.router)
# `/files` es una ruta y no un `StaticFiles` (P1.2b): los adjuntos viven dentro
# del vault (`_attachments/`) y la URL es su ruta ahí, así que hay que resolver
# el tipo por magic bytes y decidir qué se sirve inline en cada petición.
app.include_router(files.router)
# `/events` (SSE, P8): avisos de lo que cambia en el vault. Es una ruta y no
# parte de `/pages` porque no habla de páginas sino del vault entero.
app.include_router(events.router)
# `/graph` (P9): la tabla `links` servida como nodos y aristas para el visor.
app.include_router(graph.router)
# `/pages/{id}/suggestions` y `/suggestions/{sid}/...` (P10a): los parches que
# proponen los agentes, en el sidecar de la nota. Solo backend: la interfaz va
# en su propia tarjeta.
app.include_router(suggestions.router)
# `/agent/...` (P10b): la puerta de los agentes — leer contexto, proponer y ver
# lo decidido, con `X-Agent-Token`. Sin `AGENT_TOKENS` las tres rutas dan 404
# (no existen), que es lo que hace un despliegue sin agentes configurados.
app.include_router(agent.router)
# `/auth/...` (P11a): entrar, salir, saber quién eres y crear usuarios. Ojo:
# estas rutas están DESPUÉS de `/agent` y de nada más; el middleware de abajo
# es el que decide cuáles no piden sesión.
app.include_router(auth_routes.router)


# Rutas que no piden sesión. Las tres de `/auth` son las que permiten entrar
# (`login`, `setup` y `status`, que solo dice si hay que dar de alta al
# primero); `/login` es la pantalla y `/logo.png` su favicon (el del panel es el
# mismo fichero, así que servirlo sin sesión no enseña nada que no esté ya en la
# pantalla de entrada). `/agent/...` tiene su propio permiso acotado
# (`X-Agent-Token`) y no se mezcla con la sesión del dueño.
# `/manifest.webmanifest` es el manifiesto de la PWA (P13): no lleva datos,
# solo el nombre, los iconos y `display: standalone`. Va aquí porque el
# navegador lo pide al instalar y, si no, la respuesta sería un 401 con la
# pantalla de entrada (que es justo lo que no se puede instalar).
PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/auth/login",
        "/auth/setup",
        "/auth/status",
        "/login",
        "/logo.png",
        "/manifest.webmanifest",
    }
)

# Los iconos del manifiesto (P13): planos y solo PNG. **No basta con mirar el
# prefijo** `/icons/`, porque una ruta como `/icons/%2e%2e/app.js` llega aquí
# ya decodificada, empieza por `/icons/` y `StaticFiles` la resolvería al panel
# **sin sesión** (comprobado: devolvía el `app.js` con un 200). Validando el
# nombre, solo se abren los ficheros de esa carpeta.
ICON_PATH = re.compile(r"/icons/[A-Za-z0-9_-]+\.png")


def _accepts_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "").lower()


def _user_from_cookie(request: Request):
    """Usuario de la cookie de sesión, o `None`. Empuja la caducidad."""
    token = request.cookies.get(auth.COOKIE_NAME)
    if not token:
        return None
    conn = open_index(settings.index_path)
    try:
        return auth.user_for_token(conn, token)
    finally:
        conn.close()


@app.middleware("http")
async def session_is_required(request: Request, call_next):
    """La puerta (P11a): sin sesión no pasa nada salvo lo que la abre.

    Dos respuestas para el mismo «no»: `401 {"detail": "no autenticado"}` para
    la API (el `fetch` del panel lo ve y manda a `/login`) y una redirección
    para la navegación normal del navegador (`Accept: text/html`), que si no
    se quedaría mirando un JSON con la pantalla en blanco. La app es una SPA,
    así que `/` sí exige sesión: lo que no puede es romperse — con sesión sirve
    el panel de siempre, sin ella lleva al login y vuelve a donde ibas.

    El orden de los middleware importa: este se declara ANTES que
    `agent_token_is_scoped` y que `user_token_is_scoped`, y Starlette envuelve
    al último declarado por fuera, así que los permisos acotados (el del agente
    y el del token de captura) se evalúan primero y sus 403 («un token solo
    abre /agent/*», «un token de captura solo abre POST /capture») no dependen
    de que además haya sesión.

    Un token de captura sí vale para `POST /capture`: en ese caso el middleware
    de fuera ya ha dejado el usuario en `request.state` y aquí no se mira la
    cookie (no hay).
    """
    path = request.url.path
    # Los iconos del manifiesto también son públicos (P13): son los mismos
    # bytes que `/logo.png`, que ya lo era. El navegador los pide al instalar
    # la PWA sin la cookie de la página (la petición del manifiesto y sus
    # iconos no lleva credenciales), así que detrás de la puerta darían 401
    # justo en el momento en que hacen falta.
    if (
        path in PUBLIC_PATHS
        or ICON_PATH.fullmatch(path)
        or path == "/agent"
        or path.startswith("/agent/")
    ):
        return await call_next(request)

    # El usuario puede venir ya resuelto por el middleware del token de
    # captura (P13). Solo pasa en `POST /capture`: cualquier otra ruta con ese
    # token se ha cortado antes con 403.
    user = getattr(request.state, "user", None)
    if user is None:
        user = _user_from_cookie(request)
    if user is None:
        if _accepts_html(request):
            destino = path
            if request.url.query:
                destino += "?" + request.url.query
            return RedirectResponse(login_page.redirect_to_login(destino), status_code=303)
        return JSONResponse(status_code=401, content={"detail": "no autenticado"})

    # Lo lee `app.dependencies.get_current_user` (y con él los routers que
    # necesitan saber de quién es la nota).
    request.state.user = user
    return await call_next(request)


@app.middleware("http")
async def agent_token_is_scoped(request: Request, call_next):
    """Un token de agente solo abre `/agent/...`; el resto es 403.

    No es la autenticación del dueño —desde P11a eso es la sesión— sino un
    permiso **acotado**: si un token se filtra, quien lo tenga puede leer
    contexto y proponer sugerencias, y nada más. Sin esto, poner la cabecera no cambiaba
    nada y el token era una etiqueta con ínfulas de credencial.

    Un token inválido no se rechaza aquí: no identifica a nadie, así que la
    petición sigue su curso normal (401 en `/agent`; en el resto decide la
    puerta de la sesión, que no mira cabeceras). Rechazarlo aquí sería
    inventar una autenticación que no es la de esta puerta.
    """
    token = request.headers.get("x-agent-token")
    if token and agent_auth.resolve(token) is not None:
        path = request.url.path
        if path != "/agent" and not path.startswith("/agent/"):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "X-Agent-Token solo da acceso a /agent/*: "
                        "las rutas del dueño no aceptan peticiones de agente"
                    )
                },
            )
    return await call_next(request)


def _capture_token(request: Request) -> Optional[str]:
    """El token personal de captura de la petición, si lo trae (P13).

    Dos formas para lo mismo: `Authorization: Bearer <token>` es lo que usan
    `curl` y los scripts, y `X-User-Token` es lo cómodo en un atajo que no deja
    montar un esquema de autenticación. Se lee de cabeceras y no de la query a
    propósito: una URL acaba en el log de acceso y en el historial del cliente.
    """
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    explicit = request.headers.get("x-user-token")
    if explicit and explicit.strip():
        return explicit.strip()
    return None


@app.middleware("http")
async def user_token_is_scoped(request: Request, call_next):
    """Un token de captura solo abre `POST /capture`; el resto es 403 (P13).

    Es la misma idea que `agent_token_is_scoped`, para el otro secreto acotado
    que tiene la app: un token que vive en un script de escritorio (o en el
    portapapeles de un atajo) no puede ser una llave maestra del vault. Si se
    filtra, quien lo tenga apunta notas en la bandeja y nada más: no lee
    páginas, no borra y no crea tokens.

    Un token desconocido **no** se rechaza aquí: no identifica a nadie, así que
    la petición sigue su curso y decide la puerta de la sesión (401 en todo lo
    que no sea público). Rechazarlo aquí sería inventar una autenticación que
    no es la de esta puerta.

    Se declara después de los otros dos, así que es el middleware de fuera: su
    403 no depende de que haya sesión ni de qué más venga en las cabeceras.
    """
    token = _capture_token(request)
    if token is None:
        return await call_next(request)

    conn = open_index(settings.index_path)
    try:
        found = capture_tokens.resolve(conn, token)
        if found is None:
            return await call_next(request)
        if request.method != "POST" or request.url.path != "/capture":
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "un token de captura solo abre POST /capture: el resto "
                        "de la API es de la sesión del dueño"
                    )
                },
            )
        # Aquí sí: el token acaba de usarse para lo único que puede.
        capture_tokens.mark_used(conn, found.token.id)
    finally:
        conn.close()

    request.state.user = found.user
    request.state.capture_token = found.token
    return await call_next(request)


def _es_puerta_o_estatico(path: str) -> bool:
    """¿Esta ruta se puede servir con el índice todavía a medias?

    Solo lo que no lee ni escribe el índice: `/health` (que es el que dice
    cómo va), `/login` (la pantalla de entrada, que se pide justo cuando no
    hay nada que enseñar) y los ficheros del panel --sus `.js`, `.css`,
    iconos y el `index.html` de `/`--, que son estáticos puros. El resto de
    rutas de la API tocan el índice y esperan.

    La comprobación es «¿existe ese fichero en `frontend/`?», no una lista de
    nombres: una lista se queda corta en cuanto se añade un asset, y lo que
    sobra (una ruta nueva que no es un fichero) cae del lado seguro —503
    hasta que el índice esté listo--.
    """
    if path in ("/", "/health", "/login"):
        return True
    rel = path.lstrip("/")
    partes = Path(rel).parts
    if not rel or ".." in partes:
        return False
    return (Path(settings.frontend_dir) / rel).is_file()


def _no_listo(foto: dict) -> JSONResponse:
    """`503` con el progreso, mientras el índice se construye (o si falló).

    Se elige `503` y no esperar dentro de la petición: con un vault de 12.000
    notas el reindexado dura minutos, y sujetar un hilo del pool por petición
    (el panel pide varias al abrirse) acaba agotando los workers de uvicorn
    para no servir nada mejor. Y nunca se devuelve lo que hay: la lista de
    páginas de un índice a medio construir está vacía o falta por la mitad, y
    eso el panel no lo puede distinguir de un vault vacío.
    """
    if foto["status"] == STARTUP_STARTING:
        percent = foto["percent"]
        donde = f" ({percent} %)" if percent is not None else ""
        detalle = (
            f"el índice se está construyendo{donde}: {foto['phase']}. "
            "Vuelve en unos segundos."
        )
    else:
        detalle = (
            "el índice no está disponible: "
            + (foto["error"] or "el arranque del índice falló")
        )
    return JSONResponse(
        status_code=503,
        content={
            "status": foto["status"],
            "detail": detalle,
            "progress": {
                "phase": foto["phase"],
                "done": foto["done"],
                "total": foto["total"],
                "percent": foto["percent"],
                "elapsed_seconds": foto["elapsed_seconds"],
            },
        },
        # En la cabecera, que es donde lo mira un cliente HTTP: son los
        # segundos que puede tardar en volver a preguntar sin martillear.
        headers={"Retry-After": "5"},
    )


@app.middleware("http")
async def index_is_ready(request: Request, call_next):
    """Mientras el índice se construye (P16), la API responde `503`.

    Es el middleware de MÁS FUERA a propósito: declarado el último, Starlette
    lo envuelve por fuera de los demás, así que el 503 no depende de la
    sesión, ni del token de agente, ni de que el índice se pueda ni abrir.

    Antes de P16 el servidor no escuchaba hasta terminar el reindexado: la
    petición del `healthcheck` de Docker ni siquiera conectaba y el contenedor
    parecía caído. Ahora escucha desde el primer segundo, dice la verdad por
    `/health` y, en las rutas que no pueden contestar bien todavía, contesta
    que no puede --con el progreso y un `Retry-After`-- en vez de una lista a
    medias.
    """
    foto = startup_index.snapshot(stall_seconds=settings.startup_stall_seconds)
    if foto["status"] in (STARTUP_STARTING, STARTUP_ERROR):
        if _es_puerta_o_estatico(request.url.path):
            return await call_next(request)
        return _no_listo(foto)
    return await call_next(request)


# Vigilante del vault (P8). Vive en el módulo (y no en `app.state`) para que
# los tests y el apagado puedan pararlo sin depender del objeto `app`.
watcher: Optional[VaultWatcher] = None

# El reindexado de arranque va en un hilo (P16): si el proceso se apaga antes
# de que termine, el hilo puede terminar después y llamar a `_start_watcher`.
# Esta bandera es lo que evita que un vigilante nazca en un proceso que ya se
# está apagando (y que sondee un vault que ya no le toca).
_shutting_down = False


def _index_has_no_notes(conn) -> bool:
    return conn.execute(
        "SELECT count(*) FROM pages WHERE type = 'note'"
    ).fetchone()[0] == 0


@app.on_event("startup")
def on_startup() -> None:
    """Arranque en dos fases (P16): lo rápido aquí, el reindexado en un hilo.

    Antes de P16 esto reindexaba el vault entero **antes** de que uvicorn
    empezara a escuchar: con 12.000 notas son minutos con el socket cerrado,
    que para Docker es indistinguible de un contenedor muerto (y con
    `restart: unless-stopped` es un bucle de reinicios).

    La fase que se queda aquí es la que tiene que pasar antes de aceptar una
    sola petición: el esquema del índice (no hay Alembic ni Postgres: se crea
    o se migra aquí, `PRAGMA user_version`), el workspace, la estructura del
    vault vacío, el administrador de `ADMIN_USER` y las comprobaciones que
    significan «no arranques» (un vault que no está donde dice, un índice
    ilegible). Cuesta milisegundos y sus errores siguen siendo un
    `RuntimeError` que mata el arranque, con el mismo texto que antes.

    El reindexado --lo que tardaba minutos-- se va a `app.startup`: el
    servidor contesta desde el primer segundo, `/health` dice `starting` con
    el progreso y las rutas del índice devuelven `503` hasta que termina.
    Sigue reindexando en CADA arranque: es el precio de D1 (los ficheros son
    la verdad) y no cambia con esto.
    """
    # Un proceso que vuelve a arrancar (en los tests la app arranca una vez por
    # `TestClient`, en el mismo proceso) no está apagándose: la bandera que
    # frena al vigilante se limpia aquí.
    global _shutting_down
    _shutting_down = False
    startup_index.reset()

    info = ensure_index(settings.index_path)
    if info.recreated:
        log.warning("índice creado de cero en %s (%s)", info.path, info.reason)

    conn = open_index(settings.index_path)
    try:
        repo = Repository(conn)
        repo.ensure_workspace()
        if _index_has_no_notes(conn):
            # Instalación nueva (el índice no tiene notas): se crea la
            # estructura del vault, que es lo que espera el primer POST /pages.
            # Con notas en el índice no se crea nada: si el vault no está,
            # `check_can_rebuild()` se niega a vaciarlas y el arranque falla en
            # claro en vez de servir datos sin fichero detrás.
            repo.vault.ensure_structure()
        elif settings.vault_allow_empty and not Path(settings.vault_path).is_dir():
            # `VAULT_ALLOW_EMPTY` vale para un vault que existe y está vacío
            # (vaciado a mano, restore parcial del volumen). Una raíz que no
            # existe es un volumen sin montar y nunca se lee como vault vacío.
            raise RuntimeError(
                f"no se puede arrancar: la raíz del vault {settings.vault_path!r} "
                "no existe. VAULT_ALLOW_EMPTY=1 no cubre ese caso (un volumen sin "
                "montar no es un vault vacío): monta el directorio y vuelve a "
                "arrancar."
            )
        try:
            # La comprobación de `rebuild`, sin indexar: un vault que no está
            # donde dice (o vacío con páginas en el índice) sigue parando el
            # arranque aquí, en la fase bloqueante. Es un error de
            # configuración, no un reindexado lento, y un `503` lo haría
            # parecer un problema pasajero.
            repo.check_can_rebuild(
                force=settings.vault_allow_empty,
                empty_hint=EMPTY_VAULT_HINT_STARTUP,
            )
        except VaultError as exc:
            raise RuntimeError(
                f"no se puede arrancar: {exc} "
                "(el índice no se puede reconstruir desde el vault)"
            ) from exc
    finally:
        conn.close()

    _admin_from_env()

    # Fase lenta: hilo propio (daemon). `on_shutdown` no lo espera: el índice
    # es derivado y el arranque siguiente lo reconstruye.
    startup_index.start(_rebuild_index_in_background)


def _rebuild_index_in_background(job) -> None:
    """El reindexado de arranque, ya con el servidor aceptando peticiones.

    Cualquier fallo se apunta en `job` (`/health` pasa a `503` con el motivo)
    en vez de tumbar el proceso: el contenedor tiene que poder contarlo. El
    vigilante del vault arranca **después**, contra un índice ya al día (si no,
    su primera pasada vería el vault entero como cambiado y llenaría `/events`
    de avisos que nadie ha provocado).
    """
    try:
        conn = open_index(settings.index_path)
    except Exception as exc:  # noqa: BLE001 - se cuenta por /health
        job.fail(
            "el índice no se puede abrir para reconstruirlo: "
            f"{type(exc).__name__}: {exc}"
        )
        return
    try:
        repo = Repository(conn)
        try:
            stats = repo.reindex(
                force=settings.vault_allow_empty,
                empty_hint=EMPTY_VAULT_HINT_STARTUP,
                progress=job.progress,
            )
        except VaultError as exc:
            job.fail(f"el vault no se puede leer: {exc}")
            return
        if settings.vault_allow_empty:
            log.warning(
                "VAULT_ALLOW_EMPTY=1: se acepta un vault vacío a propósito; "
                "el índice se queda con %d notas (las bases, filas y vistas se "
                "conservan, sin padre)",
                stats.notes,
            )
        log.info(
            "índice reconstruido desde %s: %d notas, %d enlaces en %.2f s (%d avisos)",
            settings.vault_path,
            stats.notes,
            stats.links,
            stats.seconds,
            len(stats.warnings),
        )
        job.succeed(
            f"{stats.notes} notas y {stats.links} enlaces en {stats.seconds:.2f} s"
        )
    except Exception as exc:  # noqa: BLE001 - se cuenta por /health
        job.fail(
            "el índice no se pudo reconstruir: "
            f"{type(exc).__name__}: {exc}"
        )
        return
    finally:
        conn.close()

    _start_watcher()


def _admin_from_env() -> None:
    """Crea el primer administrador desde `ADMIN_USER`/`ADMIN_PASSWORD`.

    Solo cuando el índice no tiene ningún usuario: con usuarios ya dentro no
    se toca nada (ni se renombra ni se resetea la contraseña de nadie). Es la
    alternativa a la pantalla de alta para un despliegue desatendido; sin las
    dos variables, el alta se hace desde `/login`.
    """
    if not settings.admin_user and not settings.admin_password:
        return
    if not settings.admin_user or not settings.admin_password:
        # Una sola de las dos no crea nada (y no es un error de arranque: la
        # salida sigue siendo la pantalla de alta). Se dice para que no parezca
        # que la variable se ignoró en silencio.
        log.warning(
            "ADMIN_USER y ADMIN_PASSWORD van juntas: con solo una puesta no se "
            "crea ningún usuario; el alta se pide en /login"
        )
        return
    conn = open_index(settings.index_path)
    try:
        if auth.count_users(conn):
            return
        try:
            auth.create_user(
                conn,
                username=settings.admin_user,
                display_name=settings.admin_user,
                password=settings.admin_password,
                is_admin=True,
            )
        except auth.AuthError as exc:
            raise RuntimeError(
                f"no se puede arrancar: ADMIN_USER/ADMIN_PASSWORD no válidos ({exc})"
            ) from exc
        log.info(
            "primer usuario %r creado desde ADMIN_USER (administrador)", settings.admin_user
        )
    finally:
        conn.close()


def _start_watcher() -> None:
    """Arranca el vigilante del vault si `WATCH_ENABLED` lo permite.

    Se arranca **después** del reindexado de arranque: el vigilante sondea
    contra un índice ya al día, así que su primera pasada no tiene nada que
    reindexar y no llega ningún aviso por cambios que nadie ha hecho.
    """
    global watcher
    if _shutting_down:
        log.warning(
            "el índice terminó de construirse durante el apagado: no se arranca "
            "el vigilante"
        )
        return
    if not settings.watch_enabled:
        log.info(
            "WATCH_ENABLED=0: no se vigila el vault; los cambios hechos en los "
            "`.md` por fuera se recogen al arrancar y con POST /reindex"
        )
        return
    watcher = VaultWatcher(
        vault_path=settings.vault_path,
        index_path=settings.index_path,
        interval=settings.watch_interval,
    )
    watcher.start()
    log.info(
        "vigilando %s cada %s s (WATCH_INTERVAL); los avisos salen por /events",
        settings.vault_path,
        watcher.interval,
    )


@app.on_event("shutdown")
def on_shutdown() -> None:
    """Para el vigilante. Sin esto, el hilo seguiría sondeando tras cerrar."""
    global watcher, _shutting_down
    _shutting_down = True
    if watcher is not None:
        watcher.stop()
        watcher = None
        log.info("vigilante del vault detenido")


@app.get("/health")
def health():
    """Estado honesto del proceso y del índice (P7 y P16).

    Tres respuestas y ninguna mentira:

    - `200 {"status": "ok"}`: el índice está reconstruido. Es lo único que
      significa «listo»; hasta P16 esto era un literal, y durante un
      reindexado de minutos el contenedor se declaraba sano sin tener índice.
    - `200 {"status": "starting", "progress": {...}}`: se está construyendo.
      Sigue siendo `200` porque el proceso está vivo y haciendo lo que tiene
      que hacer --el `healthcheck` del compose lo acepta--, pero dice que no
      está listo, así que nadie puede confundirlo con el caso anterior.
    - `503 {"status": "error", "detail": ...}`: el reindexado falló o lleva
      `STARTUP_STALL_SECONDS` sin avanzar. El `healthcheck` falla y
      `docker inspect` enseña el motivo.
    """
    foto = startup_index.snapshot(stall_seconds=settings.startup_stall_seconds)
    if foto["status"] == STARTUP_STARTING:
        return {"status": "starting", "progress": _progreso(foto)}
    if foto["status"] == STARTUP_ERROR:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "detail": foto["error"] or "el índice no se pudo construir",
                "stalled": foto["stalled"],
                "progress": _progreso(foto),
            },
        )
    return {"status": "ok"}


def _progreso(foto: dict) -> dict:
    return {
        "phase": foto["phase"],
        "done": foto["done"],
        "total": foto["total"],
        "percent": foto["percent"],
        "elapsed_seconds": foto["elapsed_seconds"],
    }


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    """El manifiesto de la PWA (P13), con su tipo MIME.

    `StaticFiles` no conoce la extensión `.webmanifest` y la serviría como
    `application/octet-stream`; un manifiesto con ese tipo no vale para
    instalar. Esta ruta se declara antes del montaje del panel (que es el
    último y por eso gana a todo lo demás), así que se sirve con el tipo
    correcto y sin caché. **No hay service worker**: la app necesita el
    servidor, y prometer lo contrario sería mentir en el manifiesto.
    """
    return FileResponse(
        Path(settings.frontend_dir) / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/login", include_in_schema=False)
def login_page_view(request: Request, next_url: str = Query("/", alias="next")):
    """La pantalla de entrada (P11a). Pública: es lo que pide la sesión.

    Si ya hay sesión se sale de aquí hacia donde se iba: volver al login con
    la cookie puesta es una pantalla que no sirve para nada.
    """
    conn = open_index(settings.index_path)
    try:
        user = auth.user_for_token(conn, request.cookies.get(auth.COOKIE_NAME))
        needs_setup = auth.count_users(conn) == 0
    finally:
        conn.close()
    if user is not None:
        return RedirectResponse(login_page.safe_next(next_url), status_code=303)
    return HTMLResponse(login_page.render(needs_setup=needs_setup, next_url=next_url))


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles normal, pero fuerza al navegador a revalidar (If-None-Match)
    en vez de servir de caché sin preguntar. Sin esto, un cambio en app.js o
    index.html puede quedarse "pegado" en el navegador de forma invisible —
    el ETag sigue haciendo baratas las recargas que no cambiaron nada."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Montado al final a propósito: así no tapa /health, /status, /pages, /trash,
# /files ni /docs, y sigue sirviendo cualquier otra ruta (incluida "/") como
# archivos estáticos.
app.mount(
    "/",
    NoCacheStaticFiles(directory=settings.frontend_dir, html=True),
    name="frontend",
)
