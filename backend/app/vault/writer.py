"""Escritura sobre el vault: crear, guardar, renombrar y borrar páginas."""

from __future__ import annotations

import enum
import os
import uuid

from . import fm, names, reader, vaultfs


def new_id() -> str:
    return str(uuid.uuid4())


def body_block(body: str) -> str:
    """El cuerpo tal y como se escribe bajo el frontmatter.

    Una línea en blanco entre el `---` de cierre y el texto (solo una, aunque
    el cuerpo ya empiece por salto) y un `\n` final. Vive aquí, y no duplicado
    en `store.stored_body`, para que crear y guardar escriban exactamente los
    mismos bytes: es un solo contrato y un solo escritor.
    """
    body = body if body.startswith("\n") else "\n" + body
    if not body.endswith("\n"):
        body += "\n"
    return body


def page_markdown(*, id_: str, icon: str | None, description: str | None,
                  body: str, created: str | None = None,
                  updated: str | None = None, extra: dict | None = None) -> str:
    updates: dict = {"id": id_}
    if icon is not None:
        updates["icon"] = icon
    if description is not None:
        updates["description"] = description
    updates["created"] = created or reader.now_iso()
    updates["updated"] = updated or updates["created"]
    for key, value in (extra or {}).items():
        updates[key] = value
    raw, _ = fm.update("", updates)
    return fm.compose(raw, body_block(body))


def create_page(vault_root: str, folder: str, title: str, *, icon: str | None = None,
                description: str | None = None, body: str = "",
                page_id: str | None = None, extra: dict | None = None
                ) -> tuple[reader.Page, list[str]]:
    stem, notes = names.sanitize(title)
    # `folder` lo escribe el usuario (o un cliente HTTP): se valida ANTES de
    # `makedirs`. Sin esto, folder="../../FUERA" creaba el fichero fuera del vault.
    rel_dir = vaultfs.safe_rel(vault_root, folder) if folder else ""
    directory = os.path.join(vault_root, rel_dir) if rel_dir else vault_root
    os.makedirs(directory, exist_ok=True)
    stem, collided = names.unique_stem(directory, stem)
    if collided:
        notes.append(f"colisión en {rel_dir or '.'}: nombre final '{stem}'")
    rel = f"{rel_dir}/{stem}.md" if rel_dir else f"{stem}.md"
    text = page_markdown(id_=page_id or new_id(), icon=icon, description=description,
                         body=body, extra=extra)
    vaultfs.atomic_write(os.path.join(vault_root, rel), text)
    return reader.read_file(os.path.join(vault_root, rel), rel), notes


def sin_reservadas(properties: dict, notes: list[str], donde: str) -> dict:
    """Quita las claves reservadas (`title`) de las propiedades de una fila.

    El título de una fila ES el nombre del fichero (§9.3). Si una base declara
    `title` en su `schema`, `reader.load` ya la excluye del modelo efectivo, pero
    la capa HTTP puede mandar `properties` a mano: se filtra también aquí para que
    la app no escriba nunca `title` ni se avise a sí misma de que sobra.
    """
    limpias = {}
    for key, value in properties.items():
        if key in reader.RESERVED_ROW_KEYS:
            notes.append(f"propiedad reservada {key!r} ignorada {donde}: "
                         "el título es el nombre del fichero")
            continue
        limpias[key] = value
    return limpias


def save_page(vault_root: str, page: reader.Page, *, icon=None, description=None,
              body: str | None = None, properties: dict | None = None) -> list[str]:
    """Reescribe la página conservando claves desconocidas y su formato."""
    notes: list[str] = []
    abs_path = os.path.join(vault_root, vaultfs.safe_rel(vault_root, page.path))
    with open(abs_path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        text = fh.read()
    # mismo contrato que al leer: '\n' y sin BOM
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")

    updates: dict = {"updated": reader.now_iso()}
    if page.synthetic_id:
        # Un .md creado a mano no trae 'id'. Si se guarda desde la app sin
        # escribirlo, la identidad sigue colgando de la ruta y el primer
        # renombrado la convierte en página nueva. Se materializa al guardar.
        new_id_ = new_id()
        updates["id"] = new_id_
        page.warnings.append(f"id materializado al guardar: {new_id_}")
        page.id = new_id_
        page.synthetic_id = False
    if icon is not None:
        updates["icon"] = icon
    if description is not None:
        updates["description"] = description
    if page.type == "database_row" and properties:
        # NO se escribe `title`: el título de una fila es el nombre del fichero
        # (decisión 9.3). Escribirlo aquí dejaba dos verdades que divergían en
        # cuanto alguien renombraba el fichero por fuera. Si el `schema` declaraba
        # `title` o la capa HTTP lo manda, se filtra (con aviso) en vez de escribirlo.
        updates.update(sin_reservadas(properties, notes, "al guardar la fila"))
    new_text, warnings = fm.rewrite(text, updates, body if body is not None else None)
    vaultfs.atomic_write(abs_path, new_text)
    return notes + warnings


def rename_page(vault_root: str, page: reader.Page, new_title: str
                ) -> tuple[reader.Page, list[str]]:
    """Renombra el fichero (el título ES el nombre). El `id` no se toca.

    OJO: no reescribe los `[[wikilinks]]` entrantes. Los enlaces se resuelven por
    título (`index.resolve`), así que tras renombrar quedan con `to_id NULL` y la
    página pierde sus backlinks. Mover de carpeta conservando el nombre sí los
    conserva. Decisión pendiente para P1 (HALLAZGOS §10.10).
    """
    notes: list[str] = []
    stem, san_notes = names.sanitize(new_title)
    notes += san_notes
    rel_dir = os.path.dirname(page.path)
    directory = os.path.join(vault_root, rel_dir) if rel_dir else vault_root
    stem, collided = names.unique_stem(directory, stem)
    if collided:
        notes.append(f"colisión: nombre final '{stem}'")
    new_rel = vaultfs.safe_rel(vault_root, rel_dir, f"{stem}.md")
    vaultfs.move(os.path.join(vault_root, vaultfs.safe_rel(vault_root, page.path)),
                 os.path.join(vault_root, new_rel))
    page.path = new_rel
    page.title = stem
    return page, notes


def delete_page(vault_root: str, page: reader.Page) -> str:
    """Mueve a `.trash/<fecha>/<ruta original>`. Devuelve la ruta nueva."""
    return vaultfs.to_trash(vault_root, page.path)


# --------------------------------------------------------------------------
# bases de datos y filas
# --------------------------------------------------------------------------


def _plain(value):
    """El valor tal y como se escribe: un `Enum` de la API, por su valor.

    `payload.schema_def` llega como `model_dump()` de pydantic, que devuelve el
    miembro del `Enum` y no su cadena; escribirlo tal cual dejaría
    `type: PropertyType.text` en el fichero.
    """
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _sin_vacios(mapping: dict) -> dict:
    """Copia un mapa sin las claves sin valor (`None`) ni los mapas vacíos.

    El contrato dice que en el `.md` va lo que hay: una propiedad sin valor no
    se escribe `a: null` (el parser de `fm` no infiere tipos y la leería como la
    cadena `"null"`), y un `config: {}` de una vista de tabla no se escribe (un
    flow map vacío es opaco para el parser y la clave se perdería al releer).

    Al **guardar** no se usa para las propiedades: ahí un `None` es «bórrame
    esta clave» y `fm.update` ya lo entiende así.
    """
    out: dict = {}
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)) and not value:
            continue
        out[key] = _plain(value)
    return out


def view_entry(view: dict) -> dict:
    """Una vista con la forma del fichero: `id`, `name`, `type` y su `config`
    al mismo nivel (ROADMAP §2.3: `group_by: fase`)."""
    return {
        "id": view["id"],
        "name": view["name"],
        "type": str(getattr(view.get("type"), "value", view.get("type")) or "table"),
        **_sin_vacios(view.get("config") or {}),
    }


def property_entries(properties: dict) -> dict:
    """Propiedades de una fila tal y como se escriben: sin `title` ni vacías."""
    return _sin_vacios({k: v for k, v in properties.items()
                        if k not in reader.RESERVED_ROW_KEYS})


def schema_entries(schema: list[dict]) -> list[dict]:
    """El `schema` tal y como se escribe: sin claves sin valor.

    `options: None` de una propiedad de texto no se escribe: el parser no
    infiere tipos y `options: null` volvería como la cadena `"null"`, que no es
    una lista de opciones.
    """
    return [_sin_vacios(prop) for prop in schema if isinstance(prop, dict)]


def database_markdown(*, id_: str, icon: str | None, schema: list[dict],
                      views: list[dict], created: str | None = None,
                      updated: str | None = None,
                      description: str | None = None,
                      created_by: str | None = None,
                      header_image: str | None = None) -> str:
    """`Bases/<slug>.md`: `type: database`, `schema` y `views` en el frontmatter.

    Cada vista se escribe con `id`, `name`, `type` y su `config` **al mismo
    nivel**, que es la forma del ejemplo del ROADMAP §2.3 (`group_by: fase`)
    y la que `reader.views_from_frontmatter` vuelve a leer sin ambigüedad.
    """
    now = created or reader.now_iso()
    updates: dict = {"id": id_, "type": "database"}
    if icon is not None:
        updates["icon"] = icon
    if description is not None:
        updates["description"] = description
    if created_by:
        updates["created_by"] = created_by
    if header_image:
        updates["header_image"] = header_image
    updates["created"] = now
    updates["updated"] = updated or now
    updates["schema"] = schema_entries(schema)
    updates["views"] = [view_entry(view) for view in views]
    raw, _ = fm.update("", updates)
    return fm.compose(raw, "\n")


def create_database(vault_root: str, folder: str, title: str, schema: list[dict],
                    views: list[dict] | None = None, *, icon: str | None = "🗄",
                    db_id: str | None = None, description: str | None = None,
                    created_by: str | None = None
                    ) -> tuple[reader.Page, list[str]]:
    """Crea `folder/title.md` y su carpeta de filas (mismo nombre sin .md)."""
    stem, notes = names.sanitize(title)
    rel_dir = vaultfs.safe_rel(vault_root, folder) if folder else ""
    directory = os.path.join(vault_root, rel_dir) if rel_dir else vault_root
    os.makedirs(directory, exist_ok=True)
    stem, collided = names.unique_stem(directory, stem)
    if collided:
        notes.append(f"colisión de base en {rel_dir or '.'}: nombre final '{stem}'")
    rel = f"{rel_dir}/{stem}.md" if rel_dir else f"{stem}.md"
    text = database_markdown(id_=db_id or new_id(), icon=icon, schema=schema,
                             views=views or [], description=description,
                             created_by=created_by)
    vaultfs.atomic_write(os.path.join(vault_root, rel), text)
    os.makedirs(os.path.join(vault_root, rel[:-3]), exist_ok=True)
    return reader.read_file(os.path.join(vault_root, rel), rel), notes


def row_markdown(*, id_: str, properties: dict, created: str | None = None,
                 updated: str | None = None, icon: str | None = None) -> str:
    """Frontmatter de una fila: `id`, fechas y propiedades.

    Sin `title` (decisión 9.3: el título es el nombre del fichero) y sin las
    propiedades que no tienen valor (`_sin_vacios`). `icon` va aquí y no en las
    propiedades porque el icono de la fila es de la fila, no una propiedad
    declarada en el `schema`.
    """
    now = created or reader.now_iso()
    updates: dict = {"id": id_}
    if icon is not None:
        updates["icon"] = icon
    updates["created"] = now
    updates["updated"] = updated or now
    updates.update(property_entries(properties))
    raw, _ = fm.update("", updates)
    return fm.compose(raw, "\n")


def create_row(vault_root: str, folder: str, title: str,
               properties: dict, *, body: str = "", row_id: str | None = None,
               created: str | None = None, icon: str | None = None
               ) -> tuple[reader.Page, list[str]]:
    """Escribe una fila en `folder` (la carpeta hermana de su base)."""
    stem, notes = names.sanitize(title)
    rel_dir = vaultfs.safe_rel(vault_root, folder)
    directory = os.path.join(vault_root, rel_dir)
    os.makedirs(directory, exist_ok=True)
    stem, collided = names.unique_stem(directory, stem)
    if collided:
        notes.append(f"colisión de fila en {rel_dir}: '{stem}'")
    rel = f"{rel_dir}/{stem}.md"
    props = sin_reservadas(properties, notes, "al crear la fila")
    text = row_markdown(id_=row_id or new_id(), properties=props, created=created,
                        icon=icon)
    if body:
        text = text.rstrip("\n") + ("\n" if not body.startswith("\n") else "") + body
        if not text.endswith("\n"):
            text += "\n"
    vaultfs.atomic_write(os.path.join(vault_root, rel), text)
    return reader.read_file(os.path.join(vault_root, rel), rel), notes
