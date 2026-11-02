#!/usr/bin/env python3
"""agente-hitos (P15): propone hitos de proyecto en las notas, una vez al día.

**Es un programa, no un endpoint.** Se ejecuta desde `cron`, lee lo que el
dueño ha dejado preparado para él, pide hitos a un modelo y los deja como
**sugerencias** (P10a) en el sidecar de cada nota, donde el dueño las acepta o
las rechaza desde el editor. No escribe en ninguna nota: una sugerencia es un
parche `old_text -> new_text` que él decide aplicar, y el agente nunca puede
aceptarlo por su cuenta (`/agent/*` no tiene esa ruta, P10b).

## Qué hace, en orden

1. `GET /agent/context` para leer su **brief**: la nota de instrucciones
   `Agentes/<agente>.md` (o `Notas/Agentes/<agente>.md`, que es donde nace si
   se crea desde la app — el servidor prueba las dos y aquí se hace lo mismo,
   `BRIEF_DIRS`). El brief dice **qué notas mirar** (una ruta `.md` por línea, o
   un `[[wikilink]]` que se resuelve a `Notas/<título>.md`) y cómo quiere el
   dueño los hitos. No se inventa un contrato nuevo: el brief ya estaba en el
   contexto de P10b justo para esto.
2. `GET /agent/context?path=...` una vez por nota, para tener cuerpo, título y
   el hilo de sugerencias (aceptadas y **rechazadas con su motivo**: el canal de
   vuelta del ADR-0004).
3. Construye el prompt y pide al modelo un array JSON de hitos:
   `{"titulo", "detalle", "origen_nota", "old_text"}`.
4. Por cada hito, un parche: `old_text` es el fragmento de la nota y el
   `new_text` añade el hito detrás. **Si el `old_text` no aparece literalmente
   en la nota, la sugerencia no se crea**: el servidor la guardaría `orphaned`
   —un parche que no encaja en ningún sitio— y es peor que no proponer nada. Se
   apunta en el resumen, con el motivo.
5. `POST /agent/suggestions` por cada propuesta válida. Antes se mira lo que el
   agente ya tiene propuesto: una sugerencia **idéntica** (misma nota, mismo
   `old_text`, mismo `new_text`) no se repite, sea cual sea su estado. Es un
   cron diario: sin esto el panel se llena de clones en una semana.

## Lo que NO hace

- No toca el vault: ni lee ni escribe ficheros. Todo pasa por `/agent/*`.
- No acepta, rechaza ni responde sugerencias (no puede: P10b le da 403 en las
  rutas del dueño).
- No inventa notas: si una ruta del brief no existe, lo dice y sigue con las
  demás. Si no queda ninguna, sale con error de configuración.
- No usa SDKs: `urllib` y nada más. El proveedor del modelo se habla por HTTP
  (`openai` compatible o `anthropic`), y el programa recibe el modelo como una
  **función** que recibe el prompt y devuelve texto, así que un test puede
  pasarla enlatada sin red (ver `main(modelo=...)`).

## Variables de entorno

| Variable | Para qué | Por defecto |
| --- | --- | --- |
| `MDM_URL` | base de la API (`http://host:8000`) | `http://127.0.0.1:8000` |
| `MDM_AGENT_TOKEN` | el token de `AGENT_TOKENS` **de este agente** | — (sin él, dry-run y salida 1) |
| `MDM_AGENTE` | nombre del agente, el mismo que en `AGENT_TOKENS` | `agente-hitos` |
| `MDM_NOTAS` | rutas separadas por comas o saltos de línea, en vez del brief | — |
| `MDM_PROVEEDOR` | `openai` (compatible) o `anthropic` | `openai` |
| `MDM_MODELO` | nombre del modelo en ese proveedor | — (obligatorio) |
| `MDM_API_KEY` | clave del proveedor | — (obligatoria salvo `MDM_API_BASE` local) |
| `MDM_API_BASE` | base del proveedor (Ollama, vLLM, un proxy…) | la del proveedor |
| `MDM_TIMEOUT` | segundos para el servidor | `30` |
| `MDM_TIMEOUT_MODELO` | segundos para el modelo | `120` |
| `MDM_MAX_HITOS` | tope de hitos por ejecución | `8` |

Los secretos van en el entorno del cron, nunca en el repo: ver `LEEME.md`.

## Códigos de salida

`0` todo bien · `1` configuración (falta token, brief sin notas, token
rechazado) · `2` el servidor no responde · `3` el modelo falló (error del
proveedor o respuesta que no es JSON) · `70` fallo interno del programa (una
petición que el servidor rechaza por mal formada) · `130` interrumpido.

`--dry-run` imprime el prompt y lo que propondría, y no escribe nada. **Sí llama
al modelo**: es la forma de comprobar que el prompt y las anclas funcionan sin
ensuciar el panel. Sin `MDM_AGENT_TOKEN` no hay ni lectura posible, así que el
programa se queda en ese modo y sale con `1`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence, TextIO

VERSION = "1.0"

# Códigos de salida. Los cuatro primeros son el contrato; 70 y 130 son los
# convencionales para «fallo del propio programa» e «interrumpido», porque
# reutilizar 1/2/3 para eso mentiría sobre la causa en el log del cron.
EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_SERVIDOR = 2
EXIT_MODELO = 3
EXIT_INTERNO = 70
EXIT_INTERRUMPIDO = 130

DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_AGENTE = "agente-hitos"

# Los dos sitios donde el servidor busca el brief de un agente, en el mismo
# orden que `app/routers/agent.py:BRIEF_DIRS`. Están duplicados a propósito:
# este programa es autónomo y no puede importar el backend, y si el servidor
# cambia el orden esto sigue siendo una lista de candidatos que se prueban de
# uno en uno.
BRIEF_DIRS = ("Agentes", "Notas/Agentes")

# Topes del servidor (app/vault/suggestions.py). Se comprueban aquí antes de
# mandar nada: un 413 por un parche de 9 KB no dice qué hito era, y el resumen
# tiene que decir qué se descartó y por qué.
MAX_OLD_TEXT = 8 * 1024
MAX_NEW_TEXT = 8 * 1024
MAX_TITLE = 200
MAX_RATIONALE = 4 * 1024

# Presupuesto del prompt en bytes UTF-8. Las notas ya llegan recortadas a 20 KB
# cada una por el servidor; esto es el tope del conjunto, para que diez notas no
# manden 200 KB al modelo (y a la factura).
MAX_PROMPT_BYTES = 48 * 1024
MAX_HITOS = 8

USER_AGENT = f"mdm-agente-hitos/{VERSION}"


# --------------------------------------------------------------------------
# errores: cada uno con su código de salida
# --------------------------------------------------------------------------


class ErrorConfig(Exception):
    """Algo falta o no cuadra en la configuración de la ejecución."""


class ErrorServidor(Exception):
    """La API no está, no contesta o contesta 5xx."""


class ErrorModelo(Exception):
    """El proveedor falló, o devolvió algo que no es la lista de hitos."""


class ErrorHTTP(Exception):
    """Un 4xx del servidor, con su código, para poder decidir por caso."""

    def __init__(self, codigo: int, detalle: str) -> None:
        super().__init__(f"HTTP {codigo}: {detalle}")
        self.codigo = codigo
        self.detalle = detalle


# --------------------------------------------------------------------------
# HTTP mínimo (urllib, sin dependencias)
# --------------------------------------------------------------------------


def _detalle_de_error(bruto: bytes) -> str:
    """El `detail` del error de FastAPI, o el texto crudo recortado."""
    try:
        datos = json.loads(bruto.decode("utf-8", "replace"))
    except ValueError:
        datos = None
    if isinstance(datos, dict):
        for clave in ("detail", "message", "error"):
            valor = datos.get(clave)
            if isinstance(valor, str) and valor.strip():
                return valor.strip()
    texto = " ".join(bruto.decode("utf-8", "replace").split())
    return texto[:300] or "sin detalle"


def _peticion(
    url: str,
    *,
    token: Optional[str] = None,
    datos: Any = None,
    timeout: float = 30.0,
    cabeceras: Optional[dict[str, str]] = None,
) -> Any:
    """GET (o POST si hay `datos`) contra `url` y el JSON de vuelta.

    Lanza `ErrorHTTP` con el 4xx tal cual (quien llama decide qué significa) y
    `ErrorServidor` para lo que es «no hay servidor»: conexión rechazada, DNS,
    tiempo agotado, 5xx, o un cuerpo que no es JSON.
    """
    cab = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if cabeceras:
        cab.update(cabeceras)
    if token:
        # La cabecera, no la query: una URL acaba en el log de acceso y en el
        # historial; una cabecera no (misma razón que el ADR-0004).
        cab["X-Agent-Token"] = token

    cuerpo = None
    if datos is not None:
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        cab["Content-Type"] = "application/json; charset=utf-8"

    peticion = urllib.request.Request(
        url, data=cuerpo, headers=cab, method="POST" if cuerpo is not None else "GET"
    )
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
            bruto = respuesta.read()
    except urllib.error.HTTPError as exc:
        raise ErrorHTTP(exc.code, _detalle_de_error(exc.read())) from None
    except urllib.error.URLError as exc:
        raise ErrorServidor(f"no se pudo conectar con {url}: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise ErrorServidor(f"no se pudo conectar con {url}: {exc}") from None

    try:
        return json.loads(bruto.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ErrorServidor(f"{url} no devolvió JSON") from None


class Cliente:
    """Las tres llamadas a `/agent` que necesita el agente.

    Es una clase (y no cuatro funciones sueltas) para que los tests puedan
    pasar un doble y ejercitar los códigos de salida sin levantar un servidor.
    """

    def __init__(self, base: str, token: str, timeout: float = 30.0) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    # -- GET /agent/context -------------------------------------------------

    def contexto(self, *, path: Optional[str] = None, page_id: Optional[str] = None) -> Optional[dict]:
        """El contexto de una nota, o `None` si no existe (404).

        401/403 es configuración: el token no está o el servidor no tiene
        agentes dados de alta. 400 es una ruta que el servidor no acepta (un
        `..`, una ruta fuera del vault): también es configuración del brief.
        """
        parametros = {}
        if path is not None:
            parametros["path"] = path
        if page_id is not None:
            parametros["page_id"] = str(page_id)
        url = f"{self.base}/agent/context?{urllib.parse.urlencode(parametros)}"
        try:
            datos = _peticion(url, token=self.token, timeout=self.timeout)
        except ErrorHTTP as exc:
            if exc.codigo == 404:
                return None
            if exc.codigo in (400, 401, 403):
                raise ErrorConfig(
                    f"el servidor no acepta la petición de contexto ({exc.codigo}: "
                    f"{exc.detalle}). Revisa MDM_AGENT_TOKEN y el AGENT_TOKENS del "
                    "servidor: el nombre y el token tienen que ser de este agente"
                ) from None
            raise self._servidor(exc, "GET /agent/context") from None
        if not isinstance(datos, dict) or "page" not in datos:
            raise ErrorServidor("GET /agent/context devolvió algo que no es un contexto")
        return datos

    # -- GET /agent/suggestions --------------------------------------------

    def sugerencias(self) -> list[dict]:
        """Lo que este agente tiene propuesto, en cualquier estado."""
        url = f"{self.base}/agent/suggestions"
        try:
            datos = _peticion(url, token=self.token, timeout=self.timeout)
        except ErrorHTTP as exc:
            if exc.codigo in (401, 403, 404):
                raise ErrorConfig(
                    f"el servidor no deja listar las sugerencias de este agente "
                    f"({exc.codigo}: {exc.detalle}). Revisa MDM_AGENT_TOKEN"
                ) from None
            raise self._servidor(exc, "GET /agent/suggestions") from None
        if not isinstance(datos, list):
            raise ErrorServidor("GET /agent/suggestions no devolvió una lista")
        return [s for s in datos if isinstance(s, dict)]

    # -- POST /agent/suggestions -------------------------------------------

    def proponer(self, cuerpo: dict) -> dict:
        """Crea una sugerencia. Un 4xx sube como `ErrorHTTP` para el resumen."""
        url = f"{self.base}/agent/suggestions"
        try:
            datos = _peticion(
                url, token=self.token, datos=cuerpo, timeout=self.timeout
            )
        except ErrorHTTP as exc:
            if exc.codigo >= 500:
                raise self._servidor(exc, "POST /agent/suggestions") from None
            if exc.codigo in (401, 403):
                # El token ha dejado de valer a mitad de la pasada (revocado, o
                # cambiado el AGENT_TOKENS del servidor): es configuración, no
                # un rechazo del parche.
                raise ErrorConfig(
                    f"el servidor ya no acepta el token a mitad de la ejecución "
                    f"({exc.codigo}: {exc.detalle})"
                ) from None
            raise
        if not isinstance(datos, dict) or not datos.get("id"):
            raise ErrorServidor("POST /agent/suggestions no devolvió la sugerencia")
        return datos

    @staticmethod
    def _servidor(exc: ErrorHTTP, que: str) -> ErrorServidor:
        return ErrorServidor(f"{que} respondió {exc.codigo}: {exc.detalle}")


# --------------------------------------------------------------------------
# notas y propuestas
# --------------------------------------------------------------------------


@dataclass
class Nota:
    """Una nota tal y como la cuenta `GET /agent/context`."""

    ruta: str
    page_id: str
    titulo: str
    cuerpo: str
    recortado: bool = False
    sugerencias: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Hito:
    """Lo que el modelo devuelve por cada hito, ya normalizado a `str`."""

    titulo: str
    detalle: str
    origen_nota: str
    old_text: str


@dataclass
class Propuesta:
    """Un hito convertido en parche listo para `POST /agent/suggestions`."""

    nota: Nota
    titulo: str
    old_text: str
    new_text: str
    rationale: str
    offset: int

    @property
    def clave(self) -> tuple[str, str, str]:
        """Identidad del parche: nota + ancla + texto nuevo.

        Es lo que hace idempotente al cron. No lleva la fecha ni el motivo a
        propósito: dos ejecuciones que proponen lo mismo sobre el mismo sitio
        son la misma sugerencia aunque cambie el `rationale`.
        """
        return (self.nota.ruta, self.old_text, self.new_text)

    def cuerpo(self) -> dict[str, Any]:
        return {
            "page_id": self.nota.page_id,
            "kind": "edit",
            "old_text": self.old_text,
            "new_text": self.new_text,
            "rationale": self.rationale,
            "title": self.titulo,
            "offset": self.offset,
        }


@dataclass
class Descartada:
    titulo: str
    motivo: str


@dataclass
class Repetida:
    titulo: str
    estado: str
    suggestion_id: str


def _corta(texto: str, limite: int) -> str:
    """Recorta a `limite` bytes UTF-8 sin partir un carácter."""
    datos = texto.encode("utf-8")
    if len(datos) <= limite:
        return texto
    return datos[:limite].decode("utf-8", "ignore")


def _ruta_limpia(valor: str) -> str:
    """Normaliza una ruta del vault: sin barras invertidas, sin `./`, sin `/`."""
    limpia = (valor or "").strip().replace("\\", "/")
    while limpia.startswith("./"):
        limpia = limpia[2:]
    return limpia.lstrip("/")


_VINETA = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_CASILLA = re.compile(r"^\[[ xX]\]\s*")
_WIKILINK = re.compile(r"\[\[([^\[\]]+)\]\]")


def notas_del_brief(texto: str) -> list[str]:
    """Las notas que el dueño marcó en el brief, en orden y sin repetir.

    Dos formas, las dos que se escriben de verdad en el editor:

    - una ruta por línea, sola y terminada en `.md` (vale con viñeta, casilla o
      comillas invertidas): `- Notas/Proyectos/Web.md`;
    - un `[[wikilink]]` en cualquier línea, que se busca en `Notas/<título>.md`,
      que es donde la app crea las notas nuevas.

    Cualquier otra línea es instrucciones para el prompt, no una nota; se
    ignora aquí.
    """
    rutas: list[str] = []
    for linea in (texto or "").splitlines():
        limpia = linea.strip()
        if not limpia or limpia.startswith(("#", ">", "|")):
            continue
        candidata = _CASILLA.sub("", _VINETA.sub("", limpia)).strip()
        candidata = candidata.strip("`").strip().strip("*_").strip()
        if candidata.lower().endswith(".md") and " " not in candidata.split("/")[0]:
            ruta = _ruta_limpia(candidata)
            if ruta and ruta not in rutas:
                rutas.append(ruta)
        for enlace in _WIKILINK.findall(limpia):
            titulo = enlace.split("|", 1)[0].strip()
            if not titulo:
                continue
            ruta = _ruta_limpia(f"Notas/{titulo}.md")
            if ruta not in rutas:
                rutas.append(ruta)
    return rutas


def _hitos_de_json(texto: str) -> list[Any]:
    """El array de hitos de la respuesta del modelo, sin adornos.

    Un modelo contesta con un array, o con un objeto que lo envuelve
    (`{"hitos": [...]}`), y a veces con un bloque ```json alrededor aunque se
    le pida que no. Se aceptan las tres formas; cualquier otra cosa es un fallo
    del modelo (salida 3), no una lista vacía.
    """
    limpio = (texto or "").strip()
    if limpio.startswith("```"):
        limpio = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", limpio)
        limpio = re.sub(r"\s*```$", "", limpio).strip()
    datos: Any = None
    try:
        datos = json.loads(limpio)
    except ValueError:
        inicio, fin = limpio.find("["), limpio.rfind("]")
        if inicio >= 0 and fin > inicio:
            try:
                datos = json.loads(limpio[inicio : fin + 1])
            except ValueError:
                datos = None
    if isinstance(datos, dict):
        for clave in ("hitos", "milestones", "items", "resultado"):
            if isinstance(datos.get(clave), list):
                datos = datos[clave]
                break
    if not isinstance(datos, list):
        raise ErrorModelo(
            "el modelo no devolvió un array JSON de hitos (se esperaba "
            '[{"titulo", "detalle", "origen_nota", "old_text"}])'
        )
    return datos


def _hito(bruto: Any) -> Hito:
    """Un elemento del array, con los cuatro campos como texto."""

    def texto(clave: str) -> str:
        valor = bruto.get(clave)
        if valor is None:
            return ""
        return str(valor).strip() if isinstance(valor, (str, int, float)) else ""

    return Hito(
        titulo=texto("titulo"),
        detalle=texto("detalle"),
        origen_nota=texto("origen_nota"),
        old_text=bruto.get("old_text") if isinstance(bruto.get("old_text"), str) else "",
    )


def _nota_de_contexto(ruta: str, datos: dict) -> Nota:
    pagina = datos.get("page") or {}
    return Nota(
        ruta=_ruta_limpia(str(pagina.get("path") or ruta)),
        page_id=str(pagina.get("id") or ""),
        titulo=str(pagina.get("title") or ""),
        cuerpo=str(datos.get("body") or ""),
        recortado=bool(datos.get("body_truncated")),
        sugerencias=[s for s in (datos.get("suggestions") or []) if isinstance(s, dict)],
    )


def _buscar_nota(origen: str, notas: Sequence[Nota]) -> Optional[Nota]:
    """La nota que el modelo nombró: por ruta exacta, por título o por nombre."""
    clave = _ruta_limpia(origen)
    if not clave:
        return None
    for nota in notas:
        if clave == nota.ruta:
            return nota
    for nota in notas:
        nombre = nota.ruta.rsplit("/", 1)[-1]
        if clave in (nota.titulo, nombre, nombre[:-3]):
            return nota
    return None


def _linea_del_hito(hito: Hito) -> str:
    """El renglón que se añade detrás del ancla, en el Markdown de la nota."""
    detalle = " ".join(hito.detalle.split())
    linea = f"- **{hito.titulo}**"
    return f"{linea} — {detalle}" if detalle else linea


def preparar(
    hitos: Sequence[Any],
    notas: Sequence[Nota],
    existentes: Sequence[dict],
    agente: str,
    max_hitos: int = MAX_HITOS,
) -> tuple[list[Propuesta], list[Descartada], list[Repetida]]:
    """Convierte la respuesta del modelo en propuestas, descartes y repetidas.

    Es una función pura (sin HTTP) a propósito: aquí vive el criterio de P15
    —ancla literal, topes de tamaño, idempotencia— y es lo que se prueba sin
    red. El orden de las comprobaciones importa para que el motivo que se lee
    en el resumen sea el de verdad:

    1. los cuatro campos, `titulo` incluido (sin él no hay nada que enseñar);
    2. la nota de origen tiene que ser una de las que se leyeron;
    3. el ancla: no vacía, y **literalmente** dentro del cuerpo (si la nota se
       leyó recortada, se dice: el ancla puede estar en lo que no se vio);
    4. los topes del servidor, antes de mandar nada;
    5. idempotencia: la misma nota con el mismo ancla y el mismo texto nuevo ya
       propuesta —en cualquier estado— no se vuelve a proponer.
    """
    propuestas: list[Propuesta] = []
    descartadas: list[Descartada] = []
    repetidas: list[Repetida] = []

    conocidas: dict[tuple[str, str, str], dict] = {}
    for sugerencia in existentes:
        ruta = _ruta_limpia(str(sugerencia.get("path") or ""))
        clave = (ruta, str(sugerencia.get("old_text") or ""), str(sugerencia.get("new_text") or ""))
        conocidas[clave] = sugerencia

    vistas: set[tuple[str, str, str]] = set()

    for posicion, bruto in enumerate(hitos, start=1):
        if posicion > max_hitos:
            descartadas.append(
                Descartada(
                    titulo=_nombre_corto(bruto),
                    motivo=f"por encima del tope de {max_hitos} hitos por ejecución",
                )
            )
            continue
        if not isinstance(bruto, dict):
            descartadas.append(
                Descartada(titulo=f"#{posicion}", motivo="no es un objeto JSON")
            )
            continue

        hito = _hito(bruto)
        if not hito.titulo:
            descartadas.append(
                Descartada(titulo=f"#{posicion}", motivo="sin `titulo`")
            )
            continue
        if not hito.origen_nota:
            descartadas.append(
                Descartada(titulo=hito.titulo, motivo="sin `origen_nota`")
            )
            continue

        nota = _buscar_nota(hito.origen_nota, notas)
        if nota is None:
            descartadas.append(
                Descartada(
                    titulo=hito.titulo,
                    motivo=f"`origen_nota` = {hito.origen_nota!r} no es ninguna de las notas leídas",
                )
            )
            continue

        ancla = hito.old_text
        if not ancla.strip():
            descartadas.append(
                Descartada(
                    titulo=hito.titulo,
                    motivo=f"sin `old_text` (no hay dónde anclar el hito en {nota.ruta})",
                )
            )
            continue
        if ancla not in nota.cuerpo:
            sufijo = (
                " (la nota se leyó recortada: el ancla puede estar en lo que no se vio)"
                if nota.recortado
                else ""
            )
            descartadas.append(
                Descartada(
                    titulo=hito.titulo,
                    motivo=f"el ancla no aparece literalmente en {nota.ruta}{sufijo}",
                )
            )
            continue

        separador = "" if ancla.endswith("\n") else "\n"
        nuevo = f"{ancla}{separador}{_linea_del_hito(hito)}"
        if nuevo == ancla:                       # detalle y título vacíos: no propone nada
            descartadas.append(
                Descartada(titulo=hito.titulo, motivo="el parche no cambia nada")
            )
            continue
        if len(ancla.encode("utf-8")) > MAX_OLD_TEXT or len(nuevo.encode("utf-8")) > MAX_NEW_TEXT:
            descartadas.append(
                Descartada(
                    titulo=hito.titulo,
                    motivo=(
                        "el parche supera el tope del servidor "
                        f"({MAX_OLD_TEXT} B el ancla, {MAX_NEW_TEXT} B el texto nuevo)"
                    ),
                )
            )
            continue

        titulo = _corta(hito.titulo, MAX_TITLE)
        rationale = _corta(
            f"El agente {agente} propone este hito a partir de lo que dice "
            f"{nota.ruta}. Acéptalo para añadirlo detrás de «{_corta(hito.titulo, 80)}».",
            MAX_RATIONALE,
        )
        propuesta = Propuesta(
            nota=nota,
            titulo=titulo,
            old_text=ancla,
            new_text=nuevo,
            rationale=rationale,
            offset=nota.cuerpo.find(ancla),
        )

        if propuesta.clave in vistas:
            repetidas.append(
                Repetida(titulo=hito.titulo, estado="repetida en esta ejecución", suggestion_id="")
            )
            continue
        vistas.add(propuesta.clave)

        previa = conocidas.get(propuesta.clave)
        if previa is not None:
            repetidas.append(
                Repetida(
                    titulo=hito.titulo,
                    estado=str(previa.get("status") or "?"),
                    suggestion_id=str(previa.get("id") or ""),
                )
            )
            continue
        propuestas.append(propuesta)

    return propuestas, descartadas, repetidas


def _nombre_corto(bruto: Any) -> str:
    if isinstance(bruto, dict) and bruto.get("titulo"):
        return str(bruto.get("titulo"))
    return "?"


# --------------------------------------------------------------------------
# el prompt
# --------------------------------------------------------------------------


def construir_prompt(
    agente: str,
    notas: Sequence[Nota],
    brief: Optional[str] = None,
    brief_ruta: Optional[str] = None,
    max_bytes: int = MAX_PROMPT_BYTES,
    max_hitos: int = MAX_HITOS,
) -> str:
    """El prompt que se le manda al modelo: instrucciones, brief y notas.

    El formato de salida es fijo y está escrito aquí, no en el brief: el brief
    es del dueño y puede decir «sé breve»; el contrato del parche lo pone el
    programa, porque es el que lo va a validar. El brief va **después** de la
    forma de la respuesta para que el dueño no pueda cambiarla sin querer.
    """
    cabecera = [
        f"Eres «{agente}», un agente que propone hitos para los proyectos del dueño",
        "de este vault de notas. No escribes en las notas: propones parches que él",
        "acepta o rechaza.",
        "",
        "Devuelve SOLO un array JSON, sin texto alrededor y sin bloque de código.",
        "Cada elemento tiene exactamente estas cuatro claves:",
        "",
        '  {"titulo": "...", "detalle": "...", "origen_nota": "...", "old_text": "..."}',
        "",
        "Reglas de cada campo:",
        '- "origen_nota": la ruta exacta de una de las notas de abajo, tal cual sale',
        '  en su línea "## Nota: <ruta>".',
        '- "old_text": un fragmento copiado LITERALMENTE de esa nota (un encabezado o',
        "  una línea entera, con sus espacios y tildes). Es donde se va a insertar el",
        "  hito: si no aparece exacto en la nota, la propuesta se descarta y no llega",
        '  al dueño. Prefiere el encabezado de la sección de hitos o la última línea',
        "  del proyecto.",
        '- "titulo": corto, sin punto final, menos de 80 caracteres.',
        '- "detalle": una frase que explique el hito con lo que dice la nota.',
        "",
        f"Como mucho {max_hitos} hitos. Si no hay nada que proponer, devuelve [].",
        "No repitas lo que ya está en la nota ni lo que el dueño ya rechazó: abajo",
        "está el hilo de sugerencias, con su estado y su motivo.",
        "",
    ]
    if brief_ruta and brief:
        cabecera += [f"## Instrucciones del dueño ({brief_ruta})", "", brief.strip(), ""]
    elif brief:
        cabecera += ["## Instrucciones del dueño", "", brief.strip(), ""]
    else:
        cabecera += [
            "## Instrucciones del dueño",
            "",
            "(no hay brief: el dueño todavía no ha escrito qué quiere de este agente)",
            "",
        ]

    cuerpo = "\n".join(cabecera)
    usados = len(cuerpo.encode("utf-8"))
    omitidas: list[str] = []

    for nota in notas:
        bloque = [f"## Nota: {nota.ruta}", f"título: {nota.titulo}"]
        if nota.recortado:
            bloque.append("(cuerpo recortado por el servidor: solo los primeros 20 KB)")
        bloque += ["", nota.cuerpo.rstrip(), ""]
        if nota.sugerencias:
            bloque.append("### Sugerencias de esta nota (canal de vuelta)")
            for sugerencia in nota.sugerencias:
                estado = str(sugerencia.get("status") or "?")
                titulo = str(sugerencia.get("title") or "(sin título)")
                motivo = sugerencia.get("reason")
                linea = f"- [{estado}] {titulo}"
                if estado == "rejected" and motivo:
                    linea += f" — rechazada porque: {motivo}"
                bloque.append(linea)
            bloque.append("")
        texto = "\n".join(bloque)
        gasto = len(texto.encode("utf-8"))
        if usados + gasto > max_bytes:
            omitidas.append(nota.ruta)
            continue
        cuerpo += texto + "\n"
        usados += gasto

    if omitidas:
        cuerpo += (
            "## Notas no incluidas\n\n"
            "No caben en el prompt de esta ejecución: "
            + ", ".join(omitidas)
            + "\n"
        )
    return cuerpo


# --------------------------------------------------------------------------
# proveedores del modelo
# --------------------------------------------------------------------------


def _peticion_modelo(url: str, cuerpo: dict, cabeceras: dict[str, str], timeout: float) -> dict:
    """POST al proveedor; cualquier fallo es un fallo del modelo (salida 3)."""
    try:
        datos = _peticion(url, datos=cuerpo, timeout=timeout, cabeceras=cabeceras)
    except ErrorHTTP as exc:
        raise ErrorModelo(f"el proveedor respondió {exc.codigo}: {exc.detalle}") from None
    except ErrorServidor as exc:
        raise ErrorModelo(f"no se pudo hablar con el proveedor: {exc}") from None
    if not isinstance(datos, dict):
        raise ErrorModelo("el proveedor devolvió algo que no es un JSON")
    return datos


def proveedor_http(
    nombre: str,
    modelo: str,
    api_key: str,
    base: Optional[str] = None,
    timeout: float = 120.0,
) -> Callable[[str], str]:
    """La función `prompt -> texto` para un proveedor, hablado por HTTP.

    Dos dialectos, los dos sin SDK: `openai` (cualquier servidor compatible,
    incluido un Ollama o un vLLM local con `MDM_API_BASE`) y `anthropic`. La
    clave es obligatoria salvo que se apunte a una base propia, que es el caso
    de un modelo local sin autenticación.
    """
    dialecto = (nombre or "openai").strip().lower()
    if not modelo:
        raise ErrorConfig("falta el modelo: MDM_MODELO o --modelo")
    if not api_key and not base:
        raise ErrorConfig(
            "falta MDM_API_KEY para el proveedor del modelo (o MDM_API_BASE si es "
            "un modelo local sin clave)"
        )

    if dialecto == "openai":
        url = (base or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        cabeceras = {}
        if api_key:
            cabeceras["Authorization"] = f"Bearer {api_key}"

        def completar(prompt: str) -> str:
            datos = _peticion_modelo(
                url,
                {
                    "model": modelo,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                },
                cabeceras,
                timeout,
            )
            try:
                contenido = datos["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise ErrorModelo(
                    "la respuesta del proveedor no trae `choices[0].message.content`"
                ) from None
            if not isinstance(contenido, str):
                raise ErrorModelo("el proveedor devolvió un mensaje que no es texto")
            return contenido

        return completar

    if dialecto == "anthropic":
        url = (base or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        cabeceras = {"anthropic-version": "2023-06-01"}
        if api_key:
            cabeceras["x-api-key"] = api_key

        def completar(prompt: str) -> str:
            datos = _peticion_modelo(
                url,
                {
                    "model": modelo,
                    "max_tokens": 2048,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                },
                cabeceras,
                timeout,
            )
            bloques = datos.get("content")
            if not isinstance(bloques, list):
                raise ErrorModelo("la respuesta del proveedor no trae `content`")
            textos = [
                b.get("text", "")
                for b in bloques
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if not texto_junto(textos):
                raise ErrorModelo("el proveedor no devolvió ningún bloque de texto")
            return texto_junto(textos)

        return completar

    raise ErrorConfig(
        f"proveedor desconocido: {nombre!r} (los que hay: openai, anthropic)"
    )


def texto_junto(textos: Sequence[str]) -> str:
    return "".join(textos).strip()


# --------------------------------------------------------------------------
# ejecución
# --------------------------------------------------------------------------


def _entorno_segundos(nombre: str, defecto: float) -> float:
    bruto = (os.environ.get(nombre) or "").strip()
    if not bruto:
        return defecto
    try:
        valor = float(bruto)
    except ValueError:
        raise ErrorConfig(f"{nombre}={bruto!r} no es un número de segundos") from None
    if valor <= 0:
        raise ErrorConfig(f"{nombre}={bruto!r} tiene que ser mayor que cero")
    return valor


def _entorno_entero(nombre: str, defecto: int) -> int:
    bruto = (os.environ.get(nombre) or "").strip()
    if not bruto:
        return defecto
    try:
        valor = int(bruto)
    except ValueError:
        raise ErrorConfig(f"{nombre}={bruto!r} no es un número entero") from None
    if valor <= 0:
        raise ErrorConfig(f"{nombre}={bruto!r} tiene que ser mayor que cero")
    return valor


def _notas_de_entorno() -> list[str]:
    bruto = os.environ.get("MDM_NOTAS") or ""
    rutas: list[str] = []
    for trozo in re.split(r"[,\n]", bruto):
        ruta = _ruta_limpia(trozo)
        if ruta and ruta not in rutas:
            rutas.append(ruta)
    return rutas


def _leer_brief(cliente: Cliente, agente: str) -> tuple[Optional[str], Optional[str]]:
    """`(ruta, cuerpo)` del brief, probando los dos sitios del servidor."""
    for carpeta in BRIEF_DIRS:
        ruta = f"{carpeta}/{agente}.md"
        datos = cliente.contexto(path=ruta)
        if datos is not None:
            return _ruta_limpia(str((datos.get("page") or {}).get("path") or ruta)), str(
                datos.get("body") or ""
            )
    return None, None


def _cargar_notas(
    cliente: Cliente, rutas: Sequence[str], omitir: Optional[str], errores: TextIO
) -> list[Nota]:
    notas: list[Nota] = []
    for ruta in rutas:
        if omitir and _ruta_limpia(ruta) == omitir:
            continue
        datos = cliente.contexto(path=ruta)
        if datos is None:
            errores.write(f"aviso: no hay ninguna nota en {ruta!r}; se salta\n")
            continue
        nota = _nota_de_contexto(ruta, datos)
        if not nota.page_id:
            errores.write(f"aviso: el contexto de {ruta!r} no trae id de página; se salta\n")
            continue
        notas.append(nota)
    return notas


def _ejecutar(
    args: argparse.Namespace,
    modelo: Optional[Callable[[str], str]],
    cliente: Optional[Cliente],
    salida: TextIO,
    errores: TextIO,
) -> int:
    base = (args.url or os.environ.get("MDM_URL") or DEFAULT_URL).rstrip("/")
    token = (os.environ.get("MDM_AGENT_TOKEN") or "").strip()
    agente = (args.agente or os.environ.get("MDM_AGENTE") or DEFAULT_AGENTE).strip()
    max_hitos = (
        args.max_hitos
        if args.max_hitos is not None
        else _entorno_entero("MDM_MAX_HITOS", MAX_HITOS)
    )
    if max_hitos < 1:
        raise ErrorConfig("--max-hitos tiene que ser al menos 1")

    if not token:
        # Sin token no hay lectura: el servidor contesta 401 a todo `/agent/*`.
        # Se queda en dry-run —no se escribe nada— pero es un error de
        # configuración y el cron tiene que verlo en el código de salida.
        salida.write(construir_prompt(agente, []))
        salida.write(
            "\n--dry-run (forzado): falta MDM_AGENT_TOKEN, así que no se ha leído "
            "ni escrito nada.\n"
        )
        raise ErrorConfig(
            "falta MDM_AGENT_TOKEN: sin él no se puede leer el contexto ni proponer"
        )

    if cliente is None:
        cliente = Cliente(base, token, timeout=_entorno_segundos("MDM_TIMEOUT", 30.0))

    brief_ruta, brief = _leer_brief(cliente, agente)
    rutas = list(args.nota or [])
    if not rutas:
        rutas = _notas_de_entorno()
    if not rutas:
        rutas = notas_del_brief(brief or "")
    rutas = [_ruta_limpia(r) for r in rutas if _ruta_limpia(r)]
    # Sin repetidas: dos veces la misma ruta en `--nota` no debe leerla dos veces
    # ni mandar el mismo cuerpo dos veces al modelo.
    ya: set[str] = set()
    unicas: list[str] = []
    for ruta in rutas:
        if ruta not in ya:
            ya.add(ruta)
            unicas.append(ruta)
    rutas = unicas

    if not rutas:
        donde = brief_ruta or f"Agentes/{agente}.md"
        raise ErrorConfig(
            "no hay ninguna nota que mirar. Escribe en el brief una ruta por línea "
            f"(p. ej. «- Notas/Proyectos/Web.md») o pasa --nota RUTA. Brief esperado: "
            f"{donde}"
        )

    notas = _cargar_notas(cliente, rutas, brief_ruta, errores)
    if not notas:
        raise ErrorConfig(
            "ninguna de las notas del brief existe en el vault: "
            + ", ".join(rutas)
        )

    prompt = construir_prompt(
        agente, notas, brief=brief, brief_ruta=brief_ruta, max_hitos=max_hitos
    )
    if args.dry_run:
        salida.write(prompt)
        salida.write("\n" + "=" * 72 + "\n")

    if modelo is None:
        modelo = proveedor_http(
            args.proveedor or os.environ.get("MDM_PROVEEDOR") or "openai",
            args.modelo or os.environ.get("MDM_MODELO") or "",
            (os.environ.get("MDM_API_KEY") or "").strip(),
            base=(os.environ.get("MDM_API_BASE") or "").strip() or None,
            timeout=_entorno_segundos("MDM_TIMEOUT_MODELO", 120.0),
        )

    respuesta = modelo(prompt)
    if not isinstance(respuesta, str):
        raise ErrorModelo("la función del modelo no devolvió texto")
    hitos = _hitos_de_json(respuesta)

    existentes = cliente.sugerencias()
    propuestas, descartadas, repetidas = preparar(
        hitos, notas, existentes, agente, max_hitos=max_hitos
    )

    modelo_etiqueta = args.modelo or os.environ.get("MDM_MODELO") or "(inyectado)"
    salida.write(
        f"agente {agente} · {base} · modelo {modelo_etiqueta} · "
        f"{len(notas)} nota{'s' if len(notas) != 1 else ''} · "
        f"{len(hitos)} hito{'s' if len(hitos) != 1 else ''} del modelo\n"
    )

    if args.dry_run:
        salida.write("\n--dry-run: no se crea nada.\n")
        salida.write(f"sugerencias que crearía: {len(propuestas)}\n")
        for propuesta in propuestas:
            salida.write(
                f"\n+ {propuesta.nota.ruta} · {propuesta.titulo}\n"
                f"  old_text: {propuesta.old_text!r}\n"
                f"  new_text: {propuesta.new_text!r}\n"
            )
        _resumen_cola(salida, descartadas, repetidas, [])
        return EXIT_OK

    creadas: list[tuple[Propuesta, dict]] = []
    rechazadas: list[tuple[Propuesta, str]] = []
    for propuesta in propuestas:
        try:
            datos = cliente.proponer(propuesta.cuerpo())
        except ErrorHTTP as exc:
            # 429 es el tope de pendientes: no es un fallo del programa ni del
            # servidor, es que el dueño tiene que decidir las que ya hay. Se
            # sigue con las demás notas y se dice en el resumen.
            if exc.codigo == 429:
                rechazadas.append((propuesta, f"tope de pendientes: {exc.detalle}"))
                continue
            raise ErrorHTTP(
                exc.codigo,
                f"al proponer «{propuesta.titulo}» en {propuesta.nota.ruta}: {exc.detalle}",
            ) from None
        creadas.append((propuesta, datos))

    salida.write(f"sugerencias creadas: {len(creadas)}\n")
    for propuesta, datos in creadas:
        salida.write(
            f"  + [{datos.get('id')}] {propuesta.nota.ruta} · {propuesta.titulo}\n"
        )
    for propuesta, motivo in rechazadas:
        salida.write(f"  ! {propuesta.nota.ruta} · {propuesta.titulo}: {motivo}\n")
    _resumen_cola(salida, descartadas, repetidas, rechazadas)
    return EXIT_OK


def _resumen_cola(
    salida: TextIO,
    descartadas: Sequence[Descartada],
    repetidas: Sequence[Repetida],
    rechazadas: Sequence[tuple[Propuesta, str]],
) -> None:
    """Las que no se crearon y por qué: es lo que se mira cuando faltan."""
    if descartadas:
        salida.write(f"\ndescartadas: {len(descartadas)}\n")
        for descartada in descartadas:
            salida.write(f"  - {descartada.titulo}: {descartada.motivo}\n")
    if repetidas:
        salida.write(f"\nya propuestas (no se repiten): {len(repetidas)}\n")
        for repetida in repetidas:
            etiqueta = repetida.estado
            if repetida.suggestion_id:
                etiqueta += f", id {repetida.suggestion_id}"
            salida.write(f"  = {repetida.titulo}: {etiqueta}\n")
    if not descartadas and not repetidas and not rechazadas:
        salida.write("\nnada más que contar: todas las propuestas entraron.\n")


def _argumentos(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    class _Parser(argparse.ArgumentParser):
        """Un argumento mal escrito sale con 1 (configuración), no con 2.

        `argparse` sale con 2 por defecto, y 2 aquí significa «el servidor no
        responde»: un cron que confunde las dos cosas manda a mirar el servidor
        cuando el problema es la línea del crontab.
        """

        def error(self, message: str) -> None:      # pragma: no cover - argparse
            self.print_usage(sys.stderr)
            raise ErrorConfig(f"argumentos: {message}")

    parser = _Parser(
        prog="hitos.py",
        description=(
            "Propone hitos de proyecto como sugerencias en el vault (P15). "
            "Lee el contexto por /agent/context, pide hitos al modelo y deja un "
            "parche por hito en el sidecar de su nota. Sin token, --dry-run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Códigos de salida: 0 bien · 1 configuración · 2 servidor caído · "
            "3 el modelo falló · 70 fallo interno · 130 interrumpido.\n"
            "Variables: MDM_URL, MDM_AGENT_TOKEN, MDM_AGENTE, MDM_NOTAS, "
            "MDM_PROVEEDOR, MDM_MODELO, MDM_API_KEY, MDM_API_BASE."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="imprime el prompt y las sugerencias que crearía; no escribe nada",
    )
    parser.add_argument(
        "--nota",
        action="append",
        metavar="RUTA",
        help="ruta del vault a mirar (repetible); por defecto, las del brief",
    )
    parser.add_argument("--agente", metavar="NOMBRE", help="nombre del agente (MDM_AGENTE)")
    parser.add_argument("--url", metavar="URL", help="base de la API (MDM_URL)")
    parser.add_argument(
        "--proveedor", choices=("openai", "anthropic"), help="proveedor del modelo"
    )
    parser.add_argument("--modelo", metavar="NOMBRE", help="modelo (MDM_MODELO)")
    parser.add_argument(
        "--max-hitos", type=int, metavar="N", help=f"tope de hitos (por defecto {MAX_HITOS})"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args(argv)


def main(
    argv: Optional[Sequence[str]] = None,
    modelo: Optional[Callable[[str], str]] = None,
    cliente: Optional[Cliente] = None,
    salida: Optional[TextIO] = None,
    errores: Optional[TextIO] = None,
) -> int:
    """Ejecuta el agente y devuelve el código de salida (no llama a `exit`).

    `modelo` y `cliente` se pueden inyectar para probarlo sin red: el primero
    recibe el prompt y devuelve texto, el segundo habla con `/agent`. Sin
    inyección, el cliente es HTTP contra `MDM_URL` y el modelo es el proveedor
    configurado. Ningún fallo sale como traza de Python: cada uno tiene su
    mensaje y su código.
    """
    salida = salida if salida is not None else sys.stdout
    errores = errores if errores is not None else sys.stderr
    try:
        args = _argumentos(argv)
        return _ejecutar(args, modelo, cliente, salida, errores)
    except ErrorConfig as exc:
        errores.write(f"configuración: {exc}\n")
        return EXIT_CONFIG
    except ErrorServidor as exc:
        errores.write(f"servidor: {exc}\n")
        return EXIT_SERVIDOR
    except ErrorModelo as exc:
        errores.write(f"modelo: {exc}\n")
        return EXIT_MODELO
    except ErrorHTTP as exc:
        # Un 4xx que no es el tope de pendientes: el servidor ha rechazado algo
        # que este programa construyó mal. No es «el servidor no responde».
        errores.write(f"error interno: el servidor rechazó la petición ({exc})\n")
        return EXIT_INTERNO
    except KeyboardInterrupt:
        errores.write("interrumpido\n")
        return EXIT_INTERRUMPIDO
    except SystemExit as exc:                    # --help y --version
        return int(exc.code or 0)
    except Exception as exc:                     # noqa: BLE001 - sin trazas al usuario
        errores.write(
            f"error interno: {type(exc).__name__}: {exc}\n"
            "(esto es un fallo del programa, no de la configuración; "
            "con MDM_DEBUG=1 sale la traza)\n"
        )
        if os.environ.get("MDM_DEBUG"):
            import traceback

            traceback.print_exc()
        return EXIT_INTERNO


if __name__ == "__main__":
    sys.exit(main())
