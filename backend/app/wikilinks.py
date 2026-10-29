"""Extracción de wikilinks [[Título]] / [[Título|Alias]].

Solo el análisis del texto: la resolución (qué página tiene ese título) y el
guardado de los enlaces viven en `app/repository.py`, que es quien conoce el
índice. Los enlaces se resuelven al guardar, por título exacto sin distinguir
mayúsculas dentro del mismo workspace; renombrar una página no repara
automáticamente los wikilinks que otras páginas ya guardaron apuntando al
título antiguo — se corrigen la próxima vez que esa página de origen se vuelve
a guardar.
"""

import re

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def extract_titles(body_markdown: str) -> list[str]:
    seen: list[str] = []
    for match in WIKILINK_RE.finditer(body_markdown or ""):
        title = match.group(1).strip()
        if title and title not in seen:
            seen.append(title)
    return seen
