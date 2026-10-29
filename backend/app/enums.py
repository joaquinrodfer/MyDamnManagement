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
