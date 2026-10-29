"""Modelo en memoria del vault y lectura desde disco.

Reglas de clasificación (derivadas de §2 del roadmap, no inventadas):

- Un `.md` con `type: database` en el frontmatter ES una base de datos.
- Las FILAS de esa base son los `.md` que están en la carpeta hermana con su
  mismo nombre: `Bases/Contactos.md` -> `Bases/Contactos/*.md`. Solo los hijos
  directos; una subcarpeta dentro de la carpeta de filas no es una fila.
- Cualquier otro `.md` es una página. Su sitio en el árbol lo da la CARPETA:
  `Notas/Proyectos/x.md` cuelga del nodo `Notas/Proyectos`. Si existe
  `Notas/Proyectos.md`, esa carpeta además tiene página propia (nodo con
  contenido); si no, es una carpeta contenedora sin página.
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import fm

IGNORED_DIRS = {".trash", "_attachments", ".git", ".obsidian", ".stversions"}
SKIP_FILES = {".DS_Store", "Thumbs.db"}

# Carpeta de las bases de datos en la raíz del vault (ADR: `Bases/`). El lector
# la necesita para reconocer una fila huérfana (`Bases/X/y.md` sin
# `Bases/X.md`) aunque todavía no haya ninguna base indexada; `store` la
# reexporta para que el nombre viva en un solo sitio.
DATABASES_DIR = "Bases"

TYPES = {"text", "number", "select", "multiselect", "checkbox", "date", "url", "relation"}


@dataclass
class Page:
    id: str
    path: str                 # relativo al vault, con separadores '/'
    title: str                # nombre del fichero sin .md
    type: str = "note"        # note | database | database_row
    icon: str | None = None
    description: str | None = None
    created: str | None = None
    updated: str | None = None
    mtime: float = 0.0
    size: int = 0
    content_hash: str = ""
    body: str = ""
    frontmatter: dict = field(default_factory=dict)      # tal cual, cadenas
    extra_keys: dict = field(default_factory=dict)       # claves que no gestiona la app
    database_id: str | None = None
    properties: dict = field(default_factory=dict)       # solo filas, ya coercionadas
    synthetic_id: bool = False                           # id derivado de la ruta
    warnings: list[str] = field(default_factory=list)    # incidencias de ESTE fichero

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)


@dataclass
class Database:
    page: Page
    schema: list[dict]
    views: list[dict]
    rows: list["Page"] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.page.id

    @property
    def path(self) -> str:
        return self.page.path

    @property
    def title(self) -> str:
        return self.page.title


@dataclass
class Folder:
    path: str
    name: str
    page: Page | None = None
    folders: dict[str, "Folder"] = field(default_factory=dict)
    pages: list[Page] = field(default_factory=list)


@dataclass
class Vault:
    root: str
    pages: dict[str, Page] = field(default_factory=dict)       # id -> Page
    by_path: dict[str, Page] = field(default_factory=dict)
    databases: dict[str, Database] = field(default_factory=dict)
    rows: list[Page] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _titles: dict[str, list[Page]] | None = None

    # ---- consultas de modelo ----
    def titles(self) -> dict[str, list[Page]]:
        """Índice título(NFC+casefold) -> páginas. Sin esto resolver N enlaces
        cuesta O(N * páginas); con 5.000 notas son ~50 millones de comparaciones."""
        if self._titles is None:
            idx: dict[str, list[Page]] = {}
            for page in self.pages.values():
                idx.setdefault(title_key(page.title), []).append(page)
            self._titles = idx
        return self._titles

    def tree(self) -> Folder:
        root = Folder("", "")
        for page in self.pages.values():
            if page.type == "database_row":
                continue
            node = root
            for part in [p for p in page.folder.split("/") if p]:
                node = node.folders.setdefault(part, Folder(
                    f"{node.path}/{part}".strip("/"), part))
            node.pages.append(page)
        for node in _walk_folders(root):
            own = self.by_path.get(f"{node.path}/{node.name}.md" if node.path else f"{node.name}.md")
            if own is not None and own.type == "note":
                node.page = own
        return root

    def notes(self) -> list[Page]:
        return [p for p in self.pages.values() if p.type == "note"]

    def anonymous_note(self) -> Page:  # comodidad en pruebas
        return next(p for p in self.notes() if p.type == "note")


def _walk_folders(node: Folder):
    yield node
    for child in node.folders.values():
        yield from _walk_folders(child)


def title_key(title: str) -> str:
    """Clave de resolución de wikilinks: NFC + casefold.

    NFC aquí además de en `read_file` para que cualquier Page construida a mano
    (pruebas, capa HTTP) compare igual que las leídas del disco. La usan la
    resolución (`Vault.titles`), el desempate de `index.resolve` y la deducción
    de enlaces de `index._wikilinks`: una sola clave para las tres o el mismo
    enlace escrito en NFC y en NFD cuenta dos veces.
    """
    return unicodedata.normalize("NFC", title).casefold()


# Claves del frontmatter de una FILA que la app gestiona y no puede haber en el
# `schema` de una base: el título es el nombre del fichero (decisión §9.3) y el
# resto son las claves de la propia fila (`id`, fechas, icono, tipo). Se
# rechazan al leer el `schema` (se ignoran con aviso, el fichero del usuario no
# se toca) y se filtran al crear/guardar filas, de modo que la app no pueda
# escribir encima de la identidad de una fila porque una base declaró una
# propiedad llamada `id`. Ver HALLAZGOS §9.3.
RESERVED_ROW_KEYS = frozenset({"title", "id", "icon", "description", "created", "updated", "type"})


def name_in_nfc(rel_path: str) -> bool:
    """¿El nombre de fichero (sin .md) ya está en NFC?

    Un vault tocado por macOS puede tener el mismo título visible en NFD; el
    desempate de wikilinks prefiere el que ya está en NFC (ver `index.resolve`).
    """
    basename = os.path.basename(rel_path)[:-3]
    return unicodedata.normalize("NFC", basename) == basename


# --------------------------------------------------------------------------
# lectura
# --------------------------------------------------------------------------

def _coerce(prop: dict, raw):
    ptype = prop.get("type")
    if raw is None or raw == "":
        return None
    if ptype == "number":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if ptype == "checkbox":
        return str(raw).strip().lower() in ("true", "yes", "1", "sí", "si")
    if ptype == "multiselect":
        if isinstance(raw, list):
            return raw
        return [v.strip() for v in str(raw).split(",") if v.strip()]
    if isinstance(raw, list):
        return ", ".join(str(v) for v in raw)
    return raw


def row_properties(schema: list[dict], frontmatter: dict) -> dict:
    """Propiedades de una fila, coaccionadas contra el `schema` de su base.

    Solo las claves que el fichero **trae**: una propiedad sin valor no se
    escribe en el `.md` (ver `writer.row_markdown`) y por tanto tampoco aparece
    aquí. Así el `properties_json` del índice es exactamente lo que dice el
    fichero, y el índice escrito por la API y el reconstruido desde el disco
    coinciden (`POST /reindex` no inventa claves a `null`).
    """
    properties: dict = {}
    for prop in schema:
        if not isinstance(prop, dict) or "key" not in prop:
            continue
        key = prop["key"]
        if key in frontmatter:
            properties[key] = _coerce(prop, frontmatter[key])
    return properties


# Namespace de los ids de vista derivados: una vista escrita a mano (el ejemplo
# del ROADMAP usa `id: v1`) no es un UUID y la API la identifica por UUID. En
# vez de tirarla, se le da uno determinista a partir de su base, su nombre y su
# posición: dos reindexados dan el mismo, y al guardarla desde la app se
# materializa el UUID en el fichero.
_VIEW_ID_NAMESPACE = uuid.UUID("5f2a1c4b-6d1e-5b21-9d1a-0c7e3f5a8b42")


def view_id(raw_id, database_page_id: str, index: int, name: str) -> tuple[str, bool]:
    """(id de la vista, si hubo que derivarlo)."""
    try:
        return str(uuid.UUID(str(raw_id).strip())), False
    except (TypeError, ValueError, AttributeError):
        key = f"{database_page_id}:{index}:{name}"
        return str(uuid.uuid5(_VIEW_ID_NAMESPACE, key)), True


def views_from_frontmatter(
    raw, database_page_id: str, path: str, warnings: list[str]
) -> list[dict]:
    """Normaliza las `views` del frontmatter a lo que guarda el índice.

    En el fichero una vista es `{id, name, type, ...config}` (ROADMAP §2.3:
    `group_by` al mismo nivel que `name`). Se acepta también la forma con
    `config:` anidado, para no perder una vista escrita con el otro criterio.
    """
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        warnings.append(f"{path}: 'views' no es una lista; se ignora")
        return []
    views: list[dict] = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"{path}: vista inválida (no es un mapa): {item!r}")
            continue
        name = str(item.get("name") or "").strip()
        vtype = str(item.get("type") or "table").strip()
        config = {k: v for k, v in item.items() if k not in ("id", "name", "type")}
        nested = config.pop("config", None)
        if isinstance(nested, dict):
            config = {**nested, **config}
        elif nested not in (None, ""):
            warnings.append(f"{path}: 'config' de la vista {name!r} no es un mapa: se ignora")
        vid, derived = view_id(item.get("id"), database_page_id, position, name)
        if derived:
            warnings.append(
                f"{path}: la vista {name or position!r} no tiene un 'id' UUID "
                f"({item.get('id')!r}); se identifica con {vid} y se materializa al guardarla"
            )
        views.append({"id": vid, "name": name or "Vista", "type": vtype, "config": config})
    return views


def database_schema(page: Page, path: str, warnings: list[str]) -> list[dict]:
    """Esquema efectivo de una base: lo que la app entiende de su frontmatter.

    Se usa al reindexar (`load`) y al escribir una base o una fila
    (`store._index_database`), para que el `schema_json` del índice salga del
    mismo criterio en los dos caminos. `title` se descarta: es clave reservada
    (el título de una fila es el nombre del fichero). El fichero del usuario no
    se toca —se lee entero, con su `title` en el schema— y solo se excluye del
    modelo efectivo.
    """
    raw = page.frontmatter.get("schema")
    if raw in (None, ""):
        if "schema" not in page.frontmatter:
            warnings.append(
                f"{path}: base sin 'schema' legible (¿YAML no soportado, p. ej. 'schema: >'?); "
                "queda sin propiedades en el índice")
        return []
    if not isinstance(raw, list):
        warnings.append(f"schema no es lista en {path}")
        return []
    effective: list[dict] = []
    for prop in raw:
        if isinstance(prop, dict) and str(prop.get("key")) in RESERVED_ROW_KEYS:
            # `title` no puede ser una propiedad de la base: el título de una
            # fila es el nombre del fichero (§9.3). Si el `schema` la declara,
            # se ignora en el modelo (si no, la app la volvería a escribir al
            # guardar y se avisaría a sí misma de que sobra).
            warnings.append(
                f"{path}: clave de schema reservada {prop.get('key')!r} "
                "(el título es el nombre del fichero): se ignora en el "
                "índice y al guardar")
            continue
        effective.append(prop)
        if not isinstance(prop, dict) or "key" not in prop:
            warnings.append(f"propiedad de schema inválida en {path}: {prop!r}")
        elif prop.get("type") not in TYPES:
            warnings.append(f"tipo desconocido {prop.get('type')!r} en {path}:{prop.get('key')}")
    return effective


def read_file(abs_path: str, rel_path: str, st=None) -> Page:
    st = st if st is not None else os.stat(abs_path)
    warnings: list[str] = []
    with open(abs_path, "r", encoding="utf-8", newline="") as fh:
        try:
            text = fh.read()
        except UnicodeDecodeError:
            # Un fichero guardado en Latin-1 no puede tumbar el reindexado entero
            # (probado: pasaba). Se relee con reemplazo y se avisa.
            warnings.append("no es UTF-8 válido: leído con reemplazo (U+FFFD)")
    if warnings:
        with open(abs_path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()
    if "\r" in text:
        # Regla del contrato: saltos '\n'. Un fichero tocado en Windows/Notepad
        # llega con CRLF; si se reescribe sin normalizar, el YAML queda con '\r'
        # dentro de cada línea y el fichero acaba con saltos mezclados.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        warnings.append("saltos CRLF normalizados a \\n")
    if text.startswith("\ufeff"):
        text = text[1:]
        warnings.append("BOM UTF-8 eliminado")
    try:
        raw, body = fm.split_document(text)
    except fm.FrontmatterError as exc:
        # Frontmatter abierto y sin cerrar: el fichero es del usuario, manda él.
        warnings.append(f"frontmatter roto ({exc}): se lee como nota sin frontmatter")
        raw, body = None, text
    data = fm.parse(raw).data if raw is not None else {}
    # El título se deriva del NOMBRE DEL FICHERO, y se normaliza a NFC al leer.
    # Normalizar sólo al escribir (names.sanitize) no basta: un fichero con el
    # nombre en NFD creado por fuera (macOS, unzip, rsync) convivía con su
    # equivalente NFC como dos páginas de título distinto, y una de ellas quedaba
    # sin backlinks y sin aviso. Ver HALLAZGOS §8/§9.6.
    raw_title = os.path.basename(rel_path)[:-3]
    title = unicodedata.normalize("NFC", raw_title)
    if title != raw_title:
        warnings.append("nombre en NFD: título normalizado a NFC al leer "
                        "(el nombre en disco no se toca)")
    page = Page(
        id=str(data.get("id") or ""),
        path=rel_path.replace(os.sep, "/"),
        title=title,
        type=str(data.get("type") or "note"),
        icon=data.get("icon") or None,
        description=data.get("description") or None,
        created=data.get("created") or None,
        updated=data.get("updated") or None,
        mtime=st.st_mtime,
        size=st.st_size,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        body=body,
        frontmatter=data,
        warnings=warnings,
    )
    return page


def walk_markdown(root: str) -> list[tuple[str, os.stat_result]]:
    """Recorre el vault con `os.scandir` una sola vez.

    Pública porque la detección de deriva (`app.vault.drift`) necesita
    exactamente esto: cada `.md` con su `stat` y sin leer ninguno.

    `os.walk` + `os.stat` cuesta DOS llamadas al sistema por fichero. Con
    `scandir`, `DirEntry.stat()` sale del propio readdir en los sistemas de
    ficheros que devuelven atributos (ext4, xfs, apfs); en un montaje 9p de
    WSL no los devuelve y hay que pedirlo igual. Es la forma que debe usar
    producción: en un FS normal ahorra media E/S.
    """
    found: list[tuple[str, os.stat_result]] = []

    def scan(abs_dir: str, rel_dir: str) -> None:
        try:
            entries = list(os.scandir(abs_dir))
        except (FileNotFoundError, NotADirectoryError):
            return
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if entry.is_dir(follow_symlinks=False):
                if entry.name in IGNORED_DIRS:
                    continue
                scan(entry.path, rel)
            elif entry.name.lower().endswith(".md") and entry.name not in SKIP_FILES:
                found.append((rel.replace(os.sep, "/"), entry.stat(follow_symlinks=False)))

    scan(root, "")
    found.sort(key=lambda item: item[0])   # orden determinista (id duplicado, etc.)
    return found


def load(root: str) -> Vault:
    vault = Vault(root=root)
    candidates: list[tuple[str, os.stat_result]] = walk_markdown(root)

    # 1) páginas y bases
    for rel, st in candidates:
        page = read_file(os.path.join(root, rel), rel, st)
        for w in page.warnings:
            vault.warnings.append(f"{rel}: {w}")
        page.database_id = None
        if not page.id:
            # Un .md creado a mano (un editor cualquiera, unzip, rsync) no trae
            # `id`. Se le da uno derivado de la ruta: así el índice es estable
            # entre rebuilds, pero renombrarlo por fuera lo convierte en página
            # nueva.
            page.id = "path:" + rel
            page.synthetic_id = True
            vault.warnings.append(f"sin 'id' en frontmatter: {rel} (id derivado de la ruta)")
        if page.type == "database":
            schema = database_schema(page, rel, vault.warnings)
            views = views_from_frontmatter(
                page.frontmatter.get("views"), page.id, rel, vault.warnings
            )
        vault.by_path[page.path] = page
        _merge_page(vault, page)      # puede reasignar page.id si el id está duplicado
        if page.type == "database":
            # Se registra DESPUÉS del merge: si el fichero de la base traía un id
            # duplicado, la clave tiene que ser la que finalmente tiene la página.
            vault.databases[page.id] = Database(page=page, schema=schema, views=views)

    # 2) filas: hijos directos de la carpeta hermana de una base
    for db in vault.databases.values():
        folder = db.path[:-3]  # 'Bases/Contactos.md' -> 'Bases/Contactos'
        for rel, _st in candidates:
            if os.path.dirname(rel) != folder:
                continue
            row = vault.by_path[rel]
            if row.type == "database":
                vault.warnings.append(f"una base dentro de una carpeta de filas: {rel}")
                continue
            row.type = "database_row"
            row.database_id = db.id
            row.properties = row_properties(db.schema, row.frontmatter)
            # `title` ya no lo escribe la app (decisión 9.3), pero el usuario puede
            # añadirlo a mano. No manda: manda el nombre del fichero. Si difiere,
            # se avisa en vez de obedecerlo en silencio.
            fm_title = row.frontmatter.get("title")
            if isinstance(fm_title, str) and fm_title.strip() \
                    and unicodedata.normalize("NFC", fm_title.strip()) != row.title:
                vault.warnings.append(
                    f"{rel}: 'title' del frontmatter ({fm_title!r}) no coincide con el "
                    f"nombre del fichero ({row.title!r}); manda el nombre")
            # `title` NO está en `managed`: si el usuario lo escribe a mano es una
            # clave suya (se preserva al guardar) y además se avisa arriba.
            # `created`/`updated` son de la app (los escribe `writer.row_markdown`):
            # la identidad y las fechas de una fila no son propiedades.
            managed = {"id", "created", "updated"} | {
                p["key"] for p in db.schema if isinstance(p, dict) and "key" in p
            }
            row.extra_keys = {k: v for k, v in row.frontmatter.items() if k not in managed}
            db.rows.append(row)
            vault.rows.append(row)

    # 3) claves desconocidas de páginas normales
    for page in vault.pages.values():
        if page.type == "database":
            managed = {"id", "type", "icon", "description", "created", "updated", "schema", "views"}
        elif page.type == "database_row":
            continue
        else:
            managed = {"id", "type", "icon", "description", "created", "updated"}
        page.extra_keys = {k: v for k, v in page.frontmatter.items() if k not in managed}

    # 4) filas huérfanas: ficheros en una carpeta que parece de filas pero cuya
    #    base ya no existe. Se indexan como notas (el fichero manda) y se avisa.
    #    El conjunto de carpetas con base se calcula UNA vez: la primera versión
    #    preguntaba `any(... for p in vault.by_path.values())` dentro del bucle de
    #    ficheros, o sea O(n²) — 25 millones de comparaciones con 5.000 notas,
    #    medido en 7 s de CPU (ver HALLAZGOS.md §6).
    db_folders = {p.folder for p in vault.pages.values() if p.type == "database"}
    orphans: dict[str, list[str]] = {}
    for rel, _st in candidates:
        parent = os.path.dirname(rel)
        if not parent:
            continue
        folder = os.path.basename(parent)
        grandparent = os.path.dirname(parent)
        db_rel = f"{grandparent}/{folder}.md" if grandparent else f"{folder}.md"
        if db_rel in vault.by_path:
            continue
        # Una carpeta de filas es la hermana de una base: está dentro de la
        # carpeta de bases, o al lado de una base ya indexada (una base que
        # cuelga de una nota vive en la carpeta de esa nota).
        if grandparent == DATABASES_DIR or grandparent in db_folders:
            orphans.setdefault(parent, []).append(rel)
    for folder, files in orphans.items():
        vault.warnings.append(
            f"{len(files)} fichero(s) en {folder}/ sin base '{os.path.basename(folder)}.md': "
            "se indexan como notas (fila huérfana)")
    return vault


def _merge_page(vault: Vault, page: Page) -> None:
    """Indexa la página; si su `id` ya existe, el otro fichero recibe un id de ruta.

    Regla (HALLAZGOS §10.11): ante un `id` duplicado —copiar un `.md` con `cp`,
    restaurar un backup, duplicar en el explorador— **ningún fichero se pierde del
    índice**. Se queda con el `id` del frontmatter el primero que se procesa, que
    es el que ordena primero por ruta (`_walk_markdown` ordena): es determinista y
    no depende de `mtime`, que cambia al editar (si dependiera de `mtime`, la
    identidad saltaría de un fichero a otro cada vez que se guardara uno). El otro
    se indexa con un id derivado de la ruta (`path:<ruta>`), también determinista,
    así que la idempotencia sigue intacta, y recibe un `id` propio la primera vez
    que se guarde desde la app (`save_page`). No se escribe nada en disco durante
    el reindexado: el lector no muta el vault.

    Consecuencia asumida (HALLAZGOS §7c): "el primero por ruta" puede ser la
    copia. Copiar `Inicio.md` a `Archivo/Inicio copia.md` deja el `id` real en la
    copia y el ORIGINAL pasa a `path:Inicio.md`; si luego se guarda el original,
    se materializa un `id` nuevo y quien tuviera abierto el id viejo (pestaña,
    favorito, estado de UI) apunta a la copia. La capa HTTP debe tratar el `id`
    como opaco: si recibe una petición con un `id` que ya no existe, responder con
    la ruta y el id nuevos (o 404 con `replaced_by`) en vez de creer que la página
    se ha borrado; P1 tiene que decidir si además emite un evento de "identidad
    cambiada". Duplicar desde la app no pasa por aquí: asigna un `id` nuevo.
    """
    existing = vault.pages.get(page.id)
    if existing is None:
        vault.pages[page.id] = page
        return
    original = page.id
    synthetic = "path:" + page.path
    page.id = synthetic
    page.synthetic_id = True
    vault.warnings.append(
        f"id duplicado {original}: '{existing.path}' y '{page.path}'; el segundo se "
        f"indexa con id derivado de la ruta ({synthetic!r}) y recibirá un id propio "
        "al guardarlo desde la app")
    vault.pages[synthetic] = page


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
