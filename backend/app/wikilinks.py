"""Extracción de enlaces de un cuerpo de página.

Solo el análisis del texto: la resolución (qué página tiene ese título, qué
página hay detrás de una base incrustada) y el guardado de los enlaces viven en
`app/links.py` y `app/repository.py`, que son quienes conocen el índice. Los
enlaces se resuelven al guardar, por título exacto sin distinguir mayúsculas
dentro del mismo workspace; renombrar una página no repara automáticamente los
wikilinks que otras páginas ya guardaron apuntando al título antiguo — se
corrigen la próxima vez que esa página de origen se vuelve a guardar.

Desde P17.6 una base incrustada (```mdm-database) también es un enlace: se lee
su `id` aquí y `links.py` lo resuelve a la página de la base.
"""

import re

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

# Bases incrustadas (P17.2): la valla ```mdm-database. El cuerpo es YAML de
# juguete `clave: valor` y el `id` es el de la FILA de `databases`, no el
# `page_id` de su página.
DATABASE_INFO = "mdm-database"

# Misma valla que ve el editor (`frontend/editor-src/database-embed.js`):
# acentos de 3 o más, con la info string detrás y sangría opcional. El .md se
# escribe a mano y el parser del editor también la admite; si aquí fuéramos más
# estrictos, una base pintada en la nota no contaría como enlace.
_FENCE_RE = re.compile(r"^[ \t]*(`{3,})(.*)$")

# UUID en forma canónica 8-4-4-4-12 (la misma que valida `parseDatabaseBlock`).
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Valores que el YAML de juguete del bloque no admite (listas, mapas, anclas,
# bloques multilínea): las mismas dos reglas que `parseScalar` en el editor.
# Un bloque así no se pinta en la nota, así que tampoco puede ser un enlace.
_UNSUPPORTED_VALUE_RE = re.compile(r"^[\[\{>\|&\*!%@]|^-([ \t]|$)")


def extract_titles(body_markdown: str) -> list[str]:
    seen: list[str] = []
    for match in WIKILINK_RE.finditer(body_markdown or ""):
        title = match.group(1).strip()
        if title and title not in seen:
            seen.append(title)
    return seen


def extract_database_ids(body_markdown: str) -> list[str]:
    """Ids de las bases incrustadas en el cuerpo, en orden y sin repetir.

    Es el equivalente en Python de `scanDatabaseBlocks` (editor): solo cuentan
    los bloques **cerrados** con la valla `mdm-database`, y una valla de otro
    lenguaje se salta entera -- un ```js que documente el formato no es una
    base. Un bloque sin cerrar tampoco cuenta: mientras se escribe no hay forma
    de saber dónde acaba, y el editor tampoco lo pinta.

    Un bloque cuyo `id` falta, no es un uuid o el YAML no se entiende se
    descarta **sin lanzar**: es lo mismo que el editor pinta como tarjeta de
    error, y una nota a medio escribir no puede tumbar un guardado ni un
    reindexado. El id sale en minúsculas, que es como lo guarda la app.
    """
    ids: list[str] = []
    open_len: int | None = None
    is_database = False
    body_lines: list[str] = []

    for raw in (body_markdown or "").split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        match = _FENCE_RE.match(line)

        if open_len is None:
            if match is not None:
                open_len = len(match.group(1))
                is_database = match.group(2).strip() == DATABASE_INFO
                body_lines = []
        elif match is not None and match.group(2).strip() == "":
            # Cierre: sin info string y con acentos al menos tantos como la
            # apertura. Con menos, la línea es texto del bloque.
            if len(match.group(1)) >= open_len:
                if is_database:
                    database_id = _database_id_from_block(body_lines)
                    if database_id and database_id not in ids:
                        ids.append(database_id)
                open_len = None
                is_database = False
        elif is_database:
            body_lines.append(line)

    return ids


def _database_id_from_block(lines: list[str]) -> str | None:
    """El `id` de un bloque, o None si el bloque no es válido.

    Réplica del parser del editor (`parseDatabaseBlock`): `clave: valor` sin
    listas ni anidamiento, comillas simples o dobles opcionales, claves
    desconocidas ignoradas. `id` es obligatorio y tiene que ser un uuid.
    """
    found: str | None = None
    entries = 0

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entries += 1
        key, sep, rest = line.partition(":")
        if not sep or not key.strip():
            return None
        value = rest.strip()
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                return None
            value = value[1:-1]
        elif _UNSUPPORTED_VALUE_RE.match(value):
            return None
        if not value:
            continue
        if key.strip() == "id":
            found = value

    if entries == 0 or found is None or not _UUID_RE.match(found):
        return None
    return found.lower()
