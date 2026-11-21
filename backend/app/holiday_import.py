"""Importar festivos: archivo iCalendar, URL suscrita y festivos oficiales (C1b).

Tres caminos para que una empresa traiga su calendario en vez de teclearlo:

- **Archivo `.ics`** (iCalendar, RFC 5545): lo exportan Google Calendar,
  Outlook, Apple Calendar y casi cualquier calendario. Se lee una vez.
- **URL `.ics`** (`https://` o `webcal://`): un calendario publicado (los de
  festivos públicos de Google, uno compartido desde Outlook…). Se guarda como
  fuente y se vuelve a leer cada día (`sync_due`), así un festivo que añade
  quien mantiene el calendario llega solo.
- **País y región** desde Nager.Date (`date.nager.at`, público y gratuito, sin
  clave): los festivos nacionales y, si se elige región, los autonómicos. Los
  locales de un municipio no están ahí: para esos, `.ics` o a mano.

Cada importación es una **fuente** (`holiday_sources`) y sus festivos llevan su
`source_id`: actualizar una fuente borra lo suyo y lo vuelve a escribir, y
quitarla se lleva sus festivos sin tocar los que se pusieron a mano.

**Peticiones de red.** Solo el servidor sale a internet, y solo a lo que un
administrador pide. Una URL que apunta a la red interna (localhost, 10.x,
192.168.x, metadatos de la nube…) se rechaza antes de conectar y en cada
redirección: si no, «suscribirse a un calendario» serviría para leer servicios
internos desde fuera.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app import worktime

log = logging.getLogger(__name__)

MAX_BYTES = 3 * 1024 * 1024
TIMEOUT = 10.0
MAX_EVENTS = 5000
MAX_DAYS_PER_EVENT = 31
NAGER = "https://date.nager.at/api/v3"
SYNC_EVERY = timedelta(hours=20)
USER_AGENT = "Notemil (calendario de festivos)"


class ImportErrorMsg(ValueError):
    """Un fallo que se le puede contar a quien importa, en castellano."""


# --------------------------------------------------------------------------
# iCalendar
# --------------------------------------------------------------------------


def _unfold(text: str) -> list[str]:
    """Las líneas lógicas: en iCalendar una línea larga sigue en la siguiente
    si esta empieza por espacio o tabulador."""
    lineas: list[str] = []
    for cruda in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if cruda[:1] in (" ", "\t") and lineas:
            lineas[-1] += cruda[1:]
        else:
            lineas.append(cruda)
    return lineas


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",")
        .replace("\\;", ";").replace("\\\\", "\\").strip()
    )


_FECHA = re.compile(r"^(\d{4})(\d{2})(\d{2})")


def _fecha(value: str) -> Optional[date]:
    m = _FECHA.match(value.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _rrule(value: str) -> dict[str, str]:
    salida = {}
    for parte in value.split(";"):
        if "=" in parte:
            k, v = parte.split("=", 1)
            salida[k.strip().upper()] = v.strip()
    return salida


def parse_ics(text: str, years: Optional[range] = None) -> list[tuple[date, str]]:
    """`[(día, nombre)]` de los eventos de un calendario iCalendar.

    Un festivo es un evento de día entero (`DTSTART;VALUE=DATE`); de uno con
    hora se toma el día. Un evento de varios días da un festivo por día (como
    mucho `MAX_DAYS_PER_EVENT`: un «evento» de seis meses no es un festivo).
    Las repeticiones anuales (`RRULE:FREQ=YEARLY`, el «Navidad cada año») se
    despliegan en los años de `years` (por defecto, del pasado al siguiente).
    Los cancelados no cuentan.
    """
    if "BEGIN:VCALENDAR" not in text.upper():
        raise ImportErrorMsg("Eso no parece un calendario iCalendar (.ics).")
    hoy = date.today()
    years = years or range(hoy.year - 1, hoy.year + 3)
    salida: list[tuple[date, str]] = []
    evento: Optional[dict[str, Any]] = None
    eventos = 0
    for linea in _unfold(text):
        if not linea:
            continue
        nombre, _, valor = linea.partition(":")
        clave, *params = nombre.split(";")
        clave = clave.upper()
        if clave == "BEGIN" and valor.strip().upper() == "VEVENT":
            evento = {}
            continue
        if evento is None:
            continue
        if clave == "END" and valor.strip().upper() == "VEVENT":
            eventos += 1
            if eventos > MAX_EVENTS:
                raise ImportErrorMsg(f"El calendario tiene más de {MAX_EVENTS} eventos: no parece de festivos.")
            salida.extend(_dias_del_evento(evento, years))
            evento = None
            continue
        if clave == "DTSTART":
            evento["start"] = _fecha(valor)
            evento["all_day"] = any(p.upper() == "VALUE=DATE" for p in params) or len(valor.strip()) == 8
        elif clave == "DTEND":
            evento["end"] = _fecha(valor)
        elif clave == "SUMMARY":
            evento["name"] = _unescape(valor)
        elif clave == "RRULE":
            evento["rrule"] = _rrule(valor)
        elif clave == "STATUS":
            evento["status"] = valor.strip().upper()
    # Sin repetidos: el mismo día y nombre dos veces es uno.
    vistos = set()
    unicos = []
    for d, n in sorted(salida):
        if (d, n) not in vistos:
            vistos.add((d, n))
            unicos.append((d, n))
    return unicos


def _dias_del_evento(ev: dict[str, Any], years: range) -> list[tuple[date, str]]:
    inicio = ev.get("start")
    if inicio is None or ev.get("status") == "CANCELLED":
        return []
    nombre = (ev.get("name") or "Festivo")[:200]
    fin = ev.get("end")
    # En un evento de día entero `DTEND` es el día SIGUIENTE al último.
    if fin and ev.get("all_day"):
        fin = fin - timedelta(days=1)
    if not fin or fin < inicio:
        fin = inicio
    duracion = min((fin - inicio).days, MAX_DAYS_PER_EVENT - 1)
    bases = [inicio]
    regla = ev.get("rrule") or {}
    if regla.get("FREQ", "").upper() == "YEARLY":
        hasta = _fecha(regla.get("UNTIL", "")) if regla.get("UNTIL") else None
        cuantas = int(regla["COUNT"]) if regla.get("COUNT", "").isdigit() else None
        bases = []
        for n, anio in enumerate(range(inicio.year, max(years) + 1)):
            if cuantas is not None and n >= cuantas:
                break
            try:
                d = inicio.replace(year=anio)
            except ValueError:  # 29 de febrero en un año que no lo tiene
                continue
            if hasta and d > hasta:
                break
            bases.append(d)
    salida = []
    for b in bases:
        if b.year not in years:
            continue
        for i in range(duracion + 1):
            salida.append((b + timedelta(days=i), nombre))
    return salida


# --------------------------------------------------------------------------
# red: solo a internet, nunca a la red interna
# --------------------------------------------------------------------------


def _ip_publica(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        raise ImportErrorMsg(f"No se encuentra el servidor «{host}».") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise ImportErrorMsg("Esa dirección apunta a una red interna: solo se admiten calendarios de internet.")


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    partes = urllib.parse.urlsplit(url)
    if partes.scheme not in ("http", "https") or not partes.hostname:
        raise ImportErrorMsg("La dirección del calendario tiene que empezar por https://, http:// o webcal://.")
    if partes.username or partes.password:
        raise ImportErrorMsg("La dirección no puede llevar usuario ni contraseña.")
    return url


class _Redirecciones(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        nueva = normalize_url(newurl)
        _ip_publica(urllib.parse.urlsplit(nueva).hostname or "")
        return super().redirect_request(req, fp, code, msg, headers, nueva)


def fetch(url: str) -> str:
    """El texto de una URL de internet (como mucho `MAX_BYTES`)."""
    url = normalize_url(url)
    _ip_publica(urllib.parse.urlsplit(url).hostname or "")
    abridor = urllib.request.build_opener(_Redirecciones())
    peticion = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/calendar, application/json, */*"})
    try:
        with abridor.open(peticion, timeout=TIMEOUT) as resp:
            datos = resp.read(MAX_BYTES + 1)
    except ImportErrorMsg:
        raise
    except urllib.error.HTTPError as e:
        raise ImportErrorMsg(f"El servidor del calendario respondió con un error ({e.code}).") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ImportErrorMsg("No se pudo conectar con el servidor del calendario.") from None
    if len(datos) > MAX_BYTES:
        raise ImportErrorMsg("El calendario es demasiado grande (más de 3 MB).")
    return datos.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# festivos oficiales (Nager.Date)
# --------------------------------------------------------------------------

_cache: dict[str, tuple[datetime, Any]] = {}


def _nager(path: str, fetcher: Callable[[str], str]) -> Any:
    ahora = datetime.now(timezone.utc)
    previo = _cache.get(path)
    if previo and ahora - previo[0] < timedelta(hours=12):
        return previo[1]
    try:
        datos = json.loads(fetcher(f"{NAGER}{path}"))
    except ValueError:
        raise ImportErrorMsg("El servicio de festivos oficiales respondió algo que no se entiende.") from None
    _cache[path] = (ahora, datos)
    return datos


def countries(fetcher: Callable[[str], str] = None) -> list[dict[str, str]]:
    datos = _nager("/AvailableCountries", fetcher or fetch)
    return [{"code": c["countryCode"], "name": c["name"]} for c in datos if c.get("countryCode")]


def official(country: str, year: int, fetcher: Callable[[str], str] = None) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[A-Z]{2}", country or ""):
        raise ImportErrorMsg("El país es un código de dos letras (ES, PT, MX…).")
    try:
        return _nager(f"/PublicHolidays/{int(year)}/{country}", fetcher or fetch)
    except ImportErrorMsg as e:
        if "(404)" in str(e):
            raise ImportErrorMsg("No hay festivos oficiales para ese país.") from None
        raise


def regions(country: str, year: int, fetcher: Callable[[str], str] = None) -> list[str]:
    return sorted({c for h in official(country, year, fetcher) for c in (h.get("counties") or [])})


def official_days(country: str, region: Optional[str], years: range, fetcher=None) -> list[tuple[date, str]]:
    salida = []
    for anio in years:
        for h in official(country, anio, fetcher):
            condados = h.get("counties") or []
            if h.get("global") or not condados or (region and region in condados):
                d = _fecha(h.get("date", "").replace("-", ""))
                if d:
                    salida.append((d, h.get("localName") or h.get("name") or "Festivo"))
    return salida


# --------------------------------------------------------------------------
# fuentes
# --------------------------------------------------------------------------


def _years() -> range:
    hoy = date.today()
    return range(hoy.year - 1, hoy.year + 3)


def _source(conn: sqlite3.Connection, source_id: int) -> Optional[dict[str, Any]]:
    r = conn.execute("SELECT * FROM holiday_sources WHERE id = ?", (int(source_id),)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["params"] = json.loads(d.pop("params_json") or "{}")
    d["count"] = conn.execute("SELECT count(*) FROM holidays WHERE source_id = ?", (d["id"],)).fetchone()[0]
    return d


def sources(conn: sqlite3.Connection, workspace_id: Optional[str]) -> list[dict[str, Any]]:
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM holiday_sources WHERE workspace_id IS ? ORDER BY id",
            (str(workspace_id) if workspace_id else None,),
        )
    ]
    return [s for s in (_source(conn, i) for i in ids) if s]


def _escribir(conn: sqlite3.Connection, source_id: int, workspace_id: Optional[str], dias: list[tuple[date, str]]) -> int:
    conn.execute("DELETE FROM holidays WHERE source_id = ?", (source_id,))
    for d, nombre in dias:
        worktime.add_holiday(conn, workspace_id, d, nombre, source_id=source_id)
    return conn.execute("SELECT count(*) FROM holidays WHERE source_id = ?", (source_id,)).fetchone()[0]


def _dias_de(kind: str, params: dict[str, Any], fetcher) -> list[tuple[date, str]]:
    if kind == "url":
        return parse_ics(fetcher(params["url"]), _years())
    if kind == "country":
        return official_days(params["country"], params.get("region") or None, _years(), fetcher)
    raise ImportErrorMsg("Esta fuente no se puede actualizar: se importó de un archivo.")


def create_source(
    conn: sqlite3.Connection,
    workspace_id: Optional[str],
    kind: str,
    *,
    name: str = "",
    url: Optional[str] = None,
    country: Optional[str] = None,
    region: Optional[str] = None,
    content: Optional[str] = None,
    fetcher: Callable[[str], str] = None,
) -> dict[str, Any]:
    """Crea una fuente y escribe sus festivos. Si la primera lectura falla, no
    se guarda nada (una suscripción que nunca funcionó no se queda colgando)."""
    fetcher = fetcher or fetch
    if kind == "file":
        dias = parse_ics(content or "", _years())
        params: dict[str, Any] = {}
        nombre = name or "Calendario importado"
    elif kind == "url":
        url = normalize_url(url or "")
        params = {"url": url}
        dias = _dias_de("url", params, fetcher)
        nombre = name or urllib.parse.urlsplit(url).hostname or "Calendario"
    elif kind == "country":
        country = (country or "").strip().upper()
        region = (region or "").strip().upper() or None
        params = {"country": country, "region": region}
        dias = _dias_de("country", params, fetcher)
        nombre = name or (f"Festivos oficiales {region}" if region else f"Festivos oficiales {country}")
    else:
        raise ImportErrorMsg("Esa forma de importar no existe.")
    if not dias:
        raise ImportErrorMsg("El calendario no tiene ningún festivo en estos años.")
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO holiday_sources (workspace_id, name, kind, url, params_json, synced_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (str(workspace_id) if workspace_id else None, nombre[:200], kind, params.get("url"), json.dumps(params), ahora),
    )
    _escribir(conn, cur.lastrowid, str(workspace_id) if workspace_id else None, dias)
    return _source(conn, cur.lastrowid)  # type: ignore[return-value]


def sync_source(conn: sqlite3.Connection, source_id: int, fetcher: Callable[[str], str] = None) -> dict[str, Any]:
    fuente = _source(conn, source_id)
    if fuente is None:
        raise ImportErrorMsg("No existe esa fuente.")
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        dias = _dias_de(fuente["kind"], fuente["params"], fetcher or fetch)
    except ImportErrorMsg as e:
        conn.execute("UPDATE holiday_sources SET error = ? WHERE id = ?", (str(e), source_id))
        raise
    # Un calendario que de pronto viene vacío no borra lo que había: lo más
    # probable es un fallo del otro lado, no que se hayan quitado las fiestas.
    if dias:
        _escribir(conn, source_id, fuente["workspace_id"], dias)
    conn.execute("UPDATE holiday_sources SET synced_at = ?, error = NULL WHERE id = ?", (ahora, source_id))
    return _source(conn, source_id)  # type: ignore[return-value]


def delete_source(conn: sqlite3.Connection, source_id: int) -> bool:
    conn.execute("DELETE FROM holidays WHERE source_id = ?", (int(source_id),))
    return conn.execute("DELETE FROM holiday_sources WHERE id = ?", (int(source_id),)).rowcount > 0


def sync_due(conn: sqlite3.Connection, fetcher: Callable[[str], str] = None) -> int:
    """Vuelve a leer las suscripciones (URL y país) que llevan un día sin
    leerse. Un fallo se apunta en la fuente y no para a las demás."""
    limite = (datetime.now(timezone.utc) - SYNC_EVERY).isoformat(timespec="seconds")
    hechas = 0
    for r in conn.execute(
        "SELECT id FROM holiday_sources WHERE kind IN ('url', 'country')"
        " AND (synced_at IS NULL OR synced_at < ?)",
        (limite,),
    ).fetchall():
        try:
            sync_source(conn, r[0], fetcher)
            hechas += 1
        except ImportErrorMsg as e:
            log.warning("no se pudo actualizar la fuente de festivos %s: %s", r[0], e)
    return hechas
