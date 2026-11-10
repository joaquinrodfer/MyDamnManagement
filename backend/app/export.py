"""Exportación de una página a Markdown, con las bases dentro ya materializadas.

El `.md` de una nota puede llevar bloques de base:

````markdown
```mdm-database
id: 7a3f9c21-4b0e-4c8a-9f31-2d5e6b7a8c90
view: Tablero
```
````

Ese bloque es una pista para el editor, no una tabla. Un `.md` exportado que se
abre fuera de la app tiene que leerse, así que en la exportación cada bloque se
sustituye por la tabla de la base —título, columnas de la vista, filas— y se
deja el resto del cuerpo byte a byte como estaba.

**Es solo lectura**: se piden los datos al repositorio (que los tiene del
índice, derivado del vault) y se compone un texto. No se escribe ni en el vault
ni en el índice.

Dos decisiones que no se ven en el resultado:

- Las columnas salen de `visible_properties` de la vista; las filas, de sus
  `filters`/`sort` (ver `app.views`). Exportar la vista «Tablero» es exportar lo
  que esa vista enseña, no la base entera en otro orden.
- La cuenta de la cabecera es la de filas **de la vista**; si el límite recorta,
  el pie (`_Mostrando 200 de 340 filas._`) dice cuántas se quedaron fuera.
"""

import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

from app.config import settings
from app.repository import Database, Page, Repository, View
from app.vault import fm
from app.views import apply_view

# El `info string` de la valla que marca un bloque de base.
DATABASE_FENCE = "mdm-database"

# Etiqueta de la primera columna: el título de la fila (el nombre del fichero).
TITLE_COLUMN = "Título"

# Valla de código según CommonMark: hasta 3 espacios de sangría y una ristra de
# tres o más ``` o ~~~. El `info` de una valla con backticks no puede llevar
# backticks, pero no hace falta ser estricto: solo se compara con mdm-database.
_FENCE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# Los caracteres que un sistema de ficheros no admite en un nombre: los mismos
# que sustituye hoy el export del navegador (`exportPageAsMarkdown`).
_BAD_FILENAME = re.compile(r'[/\\?%*:|"<>]')
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# --------------------------------------------------------------------------
# documento
# --------------------------------------------------------------------------

def export_page(repo: Repository, page: Page) -> str:
    """El `.md` completo de `page`: frontmatter + cuerpo con las bases dentro."""
    texto = front_matter(page) + materialize(repo, page.body_markdown or "")
    # Un `.md` es un fichero de texto: termina en salto de línea, como el que
    # escribe el resto de la app en el vault. Antes salía sin él cuando el
    # cuerpo no lo llevaba, y `cat`/`git diff` lo enseñaban pegado al prompt.
    return texto if texto.endswith("\n") else texto + "\n"


def front_matter(page: Page) -> str:
    """El bloque `---` de la nota, con el mismo texto que exporta hoy el panel.

    Se reproduce `yamlString`/`exportPageAsMarkdown` de `frontend/app.js` para
    que una página sin bases salga **idéntica** a la que descargaba el navegador
    antes de este endpoint: comillas dobles, saltos de línea como espacio,
    `null` como cadena vacía y dos líneas en blanco antes del cuerpo.
    """
    fields = [
        ("title", page.title),
        ("icon", page.icon),
        ("created_by", page.created_by),
        ("description", page.description),
        ("created_at", _timestamp(page.created_at)),
        ("updated_at", _timestamp(page.updated_at)),
    ]
    lines = ["---"]
    lines += [f"{key}: {_yaml_string(value)}" for key, value in fields]
    lines += ["---", "", ""]
    return "\n".join(lines)


def _timestamp(value: Optional[datetime]) -> Optional[str]:
    """ISO 8601 en UTC con sufijo `Z`, que es lo que devuelve la API.

    Pydantic serializa un `datetime` con zona UTC como `...Z`, y ese es el texto
    que el export del navegador ponía en el frontmatter. Si aquí saliera
    `+00:00` el fichero cambiaría aunque la nota no hubiera cambiado.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _yaml_string(value: Any) -> str:
    """El escalar entre comillas dobles, como `yamlString` en el panel."""
    clean = re.sub(r"\r?\n", " ", "" if value is None else str(value))
    clean = clean.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{clean}"'


# --------------------------------------------------------------------------
# cuerpo: vallas de base -> tablas
# --------------------------------------------------------------------------

def materialize(repo: Repository, body: str) -> str:
    """Recorre el cuerpo y sustituye cada bloque `mdm-database` por su tabla.

    Se salta las vallas de código normales (un ```python con un ejemplo de
    bloque dentro no es un bloque de verdad) y una valla de base sin cerrar se
    deja como está: no hay cuerpo que leer y cortar el documento sería peor.
    """
    lines = body.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        opening = _FENCE.match(lines[i])
        if opening is None:
            out.append(lines[i])
            i += 1
            continue

        fence = opening.group("fence")
        info = opening.group("info").strip()
        close = _find_close(lines, i + 1, fence)
        if info != DATABASE_FENCE or close is None:
            # Una valla normal se copia **entera**, sin mirar dentro: el ejemplo
            # de un bloque `mdm-database` dentro de un ```python es texto (el
            # caso típico, una nota que documenta el formato), no una base que
            # exportar. Sin valla de cierre no hay cuerpo que leer: se copia el
            # resto tal cual y se acaba.
            if close is None:
                out.extend(lines[i:])
                break
            out.extend(lines[i:close + 1])
            i = close + 1
            continue

        block = "\n".join(lines[i + 1:close])
        rendered = _render_block(repo, lines[i:close + 1], block)
        # El rótulo de la tabla nunca se pega a lo que hubiera antes: un
        # párrafo acabado en la línea anterior dejaría el `**Contactos**` como
        # continuación suya. Y el bloque tampoco se pega a lo que venga
        # después, que si no la tabla y el párrafo siguiente se leen como uno.
        if out and out[-1].strip():
            out.append("")
        out.extend(rendered)
        if close + 1 < len(lines) and lines[close + 1].strip():
            out.append("")
        i = close + 1

    return "\n".join(out)


def _find_close(lines: list[str], start: int, fence: str) -> Optional[int]:
    """Índice de la línea que cierra la valla abierta con `fence`, si la hay."""
    char = fence[0]
    closing = re.compile(r"^ {0,3}" + re.escape(char) + "{" + str(len(fence)) + r",}\s*$")
    for index in range(start, len(lines)):
        if closing.match(lines[index]):
            return index
    return None


def _render_block(repo: Repository, fence_lines: list[str], raw: str) -> list[str]:
    """Una valla de base -> tabla, o la valla intacta más el aviso."""
    config = _block_config(raw)
    raw_id = str(config.get("id") or "").strip()
    database = _get_database(repo, raw_id)

    if database is None:
        # Nunca una tabla vacía en silencio: se deja el bloque como estaba
        # (para poder arreglarlo a mano) y se dice debajo qué no se encontró.
        hint = str(config.get("title") or "").strip()
        return list(fence_lines) + [f"> ⚠️ Base no encontrada: {hint or raw_id or '(sin id)'}"]

    view = _resolve_view(repo, database, config.get("view"))
    columns = _visible_columns(database, view)
    rows = repo.list_rows(database.id)
    if view is not None:
        rows = apply_view(rows, view.config or {})

    total = len(rows)
    limit = max(0, int(settings.export_max_rows))
    shown = rows[:limit]

    lines = [_caption(database, view, total), ""]
    lines += _table(columns, shown, database)
    if total > len(shown):
        lines += ["", f"_Mostrando {len(shown)} de {total} filas._"]
    return lines


def _block_config(raw: str) -> dict:
    """`id:` y `view:` del bloque, con el parser de frontmatter de la app."""
    try:
        return fm.parse(raw).data or {}
    except fm.FrontmatterError:
        # Un bloque ilegible no es una base: se trata como «no encontrada».
        return {}


def _get_database(repo: Repository, raw_id: str) -> Optional[Database]:
    if not raw_id:
        return None
    try:
        database_id = uuid.UUID(raw_id)
    except (ValueError, AttributeError, TypeError):
        return None
    return repo.get_database(database_id)


def _resolve_view(repo: Repository, database: Database, wanted: Any) -> Optional[View]:
    """La vista indicada por nombre (o id); si no existe, la primera."""
    views = repo.list_views(database.id)
    if not views:
        return None
    name = str(wanted or "").strip()
    if name:
        folded = name.casefold()
        for view in views:
            if view.name.casefold() == folded:
                return view
        try:
            view_id = uuid.UUID(name)
        except (ValueError, AttributeError, TypeError):
            pass
        else:
            for view in views:
                if view.id == view_id:
                    return view
    return views[0]


def _visible_columns(database: Database, view: Optional[View]) -> list[dict]:
    """Las propiedades de la vista, en su orden; sin vista, todo el esquema."""
    properties = [p for p in database.schema_def if isinstance(p, dict) and p.get("key")]
    if view is None:
        return properties

    visible = (view.config or {}).get("visible_properties")
    if isinstance(visible, list) and visible:
        by_key = {p["key"]: p for p in properties}
        chosen = [by_key[key] for key in visible if key in by_key]
        # Una vista que pide columnas que ya no existen se queda sin ninguna:
        # mejor el esquema entero que una tabla de solo títulos.
        return chosen or properties
    return properties


# --------------------------------------------------------------------------
# tabla
# --------------------------------------------------------------------------

def _caption(database: Database, view: Optional[View], total: int) -> str:
    """`**Contactos** — vista Tablero (12 filas)`.

    Un solo formato para todos los casos: título en negrita, raya, qué se está
    enseñando y la cuenta. Cuando la base no tiene vistas se exportan todas las
    columnas, y el rótulo lo dice en ese mismo hueco (`todas las columnas`) en
    vez de cambiar la forma de la frase.
    """
    alcance = f"vista {view.name}" if view is not None else "todas las columnas"
    return f"**{database.title}** — {alcance} ({_row_count(total)})"


def _row_count(total: int) -> str:
    return "1 fila" if total == 1 else f"{total} filas"


def _table(columns: list[dict], rows: list[Page], database: Database) -> list[str]:
    """La tabla de la base, con la columna del título como una columna más.

    Si la base ha renombrado esa columna (`title_name`) o la ha quitado
    (`title_hidden`), el `.md` exportado dice lo mismo que la pantalla: el
    mismo rótulo, y sin la columna si no se enseña.
    """
    muestra_titulo = not database.title_hidden
    headers = ([] if not muestra_titulo else [database.title_name or TITLE_COLUMN])
    headers += [_escape(str(p.get("name") or p["key"])) for p in columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        cells = [] if not muestra_titulo else [_escape(row.title or "Sin título")]
        cells += [_cell((row.properties or {}).get(p["key"]), p) for p in columns]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _cell(value: Any, prop: dict) -> str:
    """Un valor de propiedad como celda de tabla. Vacío -> celda vacía."""
    if value is None or value == "" or value == []:
        return ""
    prop_type = prop.get("type")
    if prop_type == "checkbox":
        # El mismo vocabulario que la tabla del panel: ✓ marcado, — sin marcar.
        return "✓" if value else "—"
    if isinstance(value, list):
        return _escape(", ".join(str(item) for item in value))
    if prop_type == "date":
        return _escape(_short_date(value))
    if isinstance(value, bool):
        return "sí" if value else "no"
    if isinstance(value, float) and value.is_integer():
        # El índice guarda los `number` como float; el panel enseña 2500, no
        # 2500.0, y el `.md` exportado tiene que decir lo mismo.
        return str(int(value))
    return _escape(str(value))


def _short_date(value: Any) -> str:
    """`2026-11-12T00:00:00+00:00` -> `2026-11-12` (ISO corto, como el panel)."""
    text = str(value).strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T ].*)?$", text)
    if match:
        return match.group(1)
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return text


def _escape(text: str) -> str:
    """Lo justo para que la tabla no se rompa.

    Un `|` parte la celda en dos y un salto de línea abre una fila nueva: el
    primero se escapa con `\\|` y el segundo se convierte en `<br>`, que es la
    forma de saltar línea dentro de una celda en Markdown (y se lee en cualquier
    visor, no solo en la app).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n+", "<br>", text)
    return text.replace("|", "\\|")


# --------------------------------------------------------------------------
# descarga
# --------------------------------------------------------------------------

def attachment_filename(title: Optional[str]) -> str:
    """`<slug>.md`, con los mismos reemplazos que el export del navegador."""
    slug = _BAD_FILENAME.sub("-", title or "Sin título")
    slug = _CONTROL.sub("-", slug).strip() or "Sin título"
    return f"{slug}.md"


def content_disposition(title: Optional[str]) -> str:
    """`attachment; filename="<slug>.md"`, con `filename*` si no es ASCII.

    Las cabeceras HTTP son ASCII: un título con acentos o emoji no cabe en
    `filename="..."` (Starlette lo mandaría en latin-1 y un cliente que lea
    UTF-8 —como el `TestClient` de la suite— revienta al decodificarlo). En ese
    caso se manda un `filename` ASCII de respaldo y el nombre real en
    `filename*` (RFC 5987), que es lo que respeta el navegador al guardar.
    """
    name = attachment_filename(title)
    if name.isascii():
        return f'attachment; filename="{name}"'
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ).strip()
    fallback = ascii_name if ascii_name not in ("", ".md") else "export.md"
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name)}'
