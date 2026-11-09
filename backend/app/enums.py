"""Enumeraciones del modelo, en su propio módulo.

Antes vivían en `app/models.py`, junto al modelo de SQLAlchemy. Ese modelo ya no
existe (el índice SQLite se consulta desde `app/repository.py`), pero las
enumeraciones siguen siendo el contrato de la API: los valores que se guardan en
el índice y los que valida `app/schemas.py`.
"""

import enum


class PageType(str, enum.Enum):
    note = "note"
    database = "database"
    database_row = "database_row"


class ViewType(str, enum.Enum):
    table = "table"
    board = "board"
    calendar = "calendar"
    list = "list"


class LinkKind(str, enum.Enum):
    wikilink = "wikilink"
    relation = "relation"
    # Base incrustada en el cuerpo (valla ```mdm-database, P17.6): es una arista
    # de la página que la lleva a la página de la base. Se guarda aparte de
    # `wikilink` para que el bloque y un `[[título]]` a la misma base no choquen
    # en el UNIQUE de `links` ni se confundan al contar.
    embed = "embed"
