"""Jerarquía entre filas de una misma base: subtareas.

Una fila puede ser hija de otra fila **de su misma base**. El vínculo se declara
con una propiedad de tipo `parent_row` (una sola por base) y su celda guarda el
**id** de la fila madre, en el frontmatter del `.md` de la hija —el mismo
criterio que `relation`: id, nunca título, porque los títulos cambian y el
vínculo se rompería al renombrar:

    ---
    id: 9f3c1a2b-...
    title: Escribir el capítulo 2
    subtarea_de: 7a1b4c5d-...   # id de la fila madre
    ---

**Por qué una propiedad y no un campo del esquema** (`hierarchy: {...}`): la
celda vive en el `.md` de la fila, que es la verdad, y todo lo que ya existe
—lectura, coacción por tipo, escritura byte a byte, migración de claves al
renombrar la propiedad, índice, filtros, orden y exportación— es exactamente lo
de una propiedad. Un campo aparte obligaría a duplicar esas piezas y a mantener
dos caminos de lectura en paralelo.

Reglas que decide este módulo y que ningún otro sitio puede contradecir:

- **Madre e hija son de la misma base.** El id de la celda se valida contra las
  filas de la base; un id de otra base (o que no existe) es un 400 al guardar.
- **Sin ciclos.** Una fila no puede ser su propia madre ni colgar de una
  descendiente suya: `validate_parent` lo rechaza con un 400 que se entiende.
  Un ciclo solo podría entrar editando el `.md` a mano, así que el resto de
  funciones lo toleran sin colgarse (`arrange` no visita dos veces una fila).
- **Cerrada.** La decide la **regla de la base** (`closed: {property, values}`
  en su `.md`, ver `app.closed`), no este módulo: aquí solo se cuenta. Una base
  sin regla no tiene estado y ninguna hija cuenta como cerrada (el contador sale
  «0 de N», que es la verdad). Antes la regla era un marcador `done` por
  propiedad; se sigue leyendo para no romper las bases que ya lo tenían.
- **El contador** (`child_counts`) cuenta solo las hijas DIRECTAS: «2 de 3» es
  «dos de sus tres hijas», no de toda la descendencia. Las nietas se ven al
  abrir la hija, que es una madre a su vez.
- **Orden.** Las hijas van justo detrás de su madre, en el orden en que las dejó
  la vista (filtros y `sort`), y dentro de esa rama se repite la regla. Una hija
  cuya madre no está en la lista (la filtró la vista) se queda en su sitio como
  una fila suelta: no se esconde nada de lo que el filtro dejó pasar.
- **Nivel de la vista** (`parent_level` en su `config`, en el `.md`): `roots`
  (solo las filas que no son hijas de ninguna de la base) o `all`. Por defecto
  `roots` en tabla y tablero —una hija se ve bajo su madre, no dos veces— y
  `all` en lista y calendario, donde la jerarquía se lee indentada.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Optional

from fastapi import HTTPException

from app import closed
from app.vault.reader import PARENT_ROW, relation_id

if TYPE_CHECKING:  # pragma: no cover - solo para los tipos
    from app.repository import Page

# Niveles que entiende una vista. El `.md` guarda la palabra tal cual.
LEVEL_ROOTS = "roots"
LEVEL_ALL = "all"
LEVELS = (LEVEL_ROOTS, LEVEL_ALL)

# Vista cuyo nivel por defecto es «solo madres»: la tabla y el tablero enseñan
# una fila por sitio, y una hija ya se ve debajo de su madre.
_DEFAULT_ROOTS = frozenset({"table", "board"})

def parent_property(schema_def: Iterable[dict]) -> Optional[dict]:
    """La propiedad `parent_row` del schema, si la hay (la primera)."""
    for prop in schema_def or []:
        if isinstance(prop, dict) and prop.get("type") == PARENT_ROW and prop.get("key"):
            return prop
    return None


def parent_key(schema_def: Iterable[dict]) -> Optional[str]:
    """La clave de la celda que guarda la madre, o `None` si la base no tiene."""
    prop = parent_property(schema_def)
    return str(prop["key"]) if prop else None


def parent_of(row: "Page | dict", key: Optional[str]) -> Optional[str]:
    """El id de la madre de `row` (canónico), o `None` si no tiene.

    Acepta una fila del repositorio o el mapa de propiedades: hay llamadas que
    tienen uno y no el otro (la serialización, los filtros). Una celda que no es
    un id se lee como «sin madre»: el `.md` es del usuario y la lectura no puede
    tumbar el índice (ver `reader._parent_value`).
    """
    if not key:
        return None
    props = row if isinstance(row, dict) else (getattr(row, "properties", None) or {})
    return relation_id(props.get(key))


def child_counts(
    rows: Iterable["Page"], key: Optional[str], rule: Optional[dict]
) -> dict[str, tuple[int, int]]:
    """`{id_de_madre: (hijas, cerradas)}` de todas las filas de la base.

    Se calcula sobre TODAS las filas de la base, no sobre las que deja pasar la
    vista: «1 de 3» es la verdad de la fila, y no puede cambiar porque la vista
    esté filtrando por otra cosa. `rule` es la regla de la base (`Database.closed`,
    ya resuelta); sin ella, ninguna hija cuenta como cerrada.
    """
    if not key:
        return {}
    cuenta: dict[str, list[int]] = {}
    for row in rows:
        pid = parent_of(row, key)
        if not pid:
            continue
        par = cuenta.setdefault(pid, [0, 0])
        par[0] += 1
        if closed.is_closed(rule, row.properties or {}):
            par[1] += 1
    return {pid: (par[0], par[1]) for pid, par in cuenta.items()}


def level(config: dict, view_type: Optional[str]) -> str:
    """El nivel de la vista: `roots` o `all`.

    `parent_level` se guarda en el `config` de la vista (en el `.md`, como
    `filters` o `sort`). Lo que no sea uno de los dos valores se lee como el
    valor por defecto —un `.md` editado a mano no puede dejar la vista en un
    estado raro— y el defecto depende del tipo: tabla y tablero solo madres.
    """
    raw = str((config or {}).get("parent_level") or "").strip().lower()
    if raw in LEVELS:
        return raw
    return LEVEL_ROOTS if str(view_type or "") in _DEFAULT_ROOTS else LEVEL_ALL


def only_roots(rows: list["Page"], key: Optional[str]) -> list["Page"]:
    """Las filas que no cuelgan de otra fila DE ESTA lista.

    Una fila cuya madre no está en la lista (la filtró la vista, o su `.md`
    apunta a un id que ya no existe) cuenta como raíz: esconderla sería perder
    un dato que el filtro no excluyó.

    Un ciclo escrito a mano —el 400 lo impide por API, el `.md` es del
    usuario— tampoco esconde nada: si la cadena de madres solo da vueltas no
    hay raíz de la que colgar, así que las filas que lo forman se tratan como
    raíces y se ven. Es la misma tolerancia de `arrange`: un dato raro no deja
    la vista vacía ni cuelga la petición.
    """
    if not key:
        return list(rows)
    por_id = {str(row.id): row for row in rows}

    def es_raiz(row: "Page") -> bool:
        actual = parent_of(row, key)
        if actual is None or actual not in por_id:
            return True            # sin madre en la lista: es raíz
        vistas = {str(row.id)}
        while actual in por_id and actual not in vistas:
            vistas.add(actual)
            actual = parent_of(por_id[actual], key)
        # Termina en una raíz de la lista -> no es raíz; da vueltas -> sí.
        return actual in vistas

    return [row for row in rows if es_raiz(row)]


def arrange(rows: list["Page"], key: Optional[str]) -> list["Page"]:
    """Coloca cada hija justo detrás de su madre, en el orden que ya traían.

    El orden de entrada (el de los filtros y el `sort` de la vista) manda: aquí
    solo se agrupan las ramas. Un ciclo —que solo puede venir de un `.md`
    editado a mano— no cuelga la petición: cada fila se emite una vez y lo que
    quede sin visitar se añade al final en su orden.
    """
    if not key or not rows:
        return list(rows)
    por_id = {str(row.id): row for row in rows}
    hijas: dict[str, list["Page"]] = {}
    for row in rows:
        pid = parent_of(row, key)
        if pid and pid in por_id and pid != str(row.id):
            hijas.setdefault(pid, []).append(row)
    salida: list["Page"] = []
    vistas: set[str] = set()

    def emitir(row: "Page") -> None:
        rid = str(row.id)
        if rid in vistas:
            return
        vistas.add(rid)
        salida.append(row)
        for hija in hijas.get(rid, []):
            emitir(hija)

    for row in rows:
        pid = parent_of(row, key)
        # Arranca la rama por su raíz, no por la hija que caiga antes en el
        # orden: así el orden de arriba decide entre ramas y no dentro de una.
        if pid in por_id and pid != str(row.id):
            continue
        emitir(row)
    for row in rows:          # ciclos o restos: una vez cada una
        emitir(row)
    return salida


def validate_parent(
    schema_def: Iterable[dict],
    properties: dict,
    *,
    row_id: Optional[str],
    rows: Iterable["Page"],
) -> None:
    """Comprueba la celda `parent_row` de una fila antes de guardarla.

    `rows` son **todas** las filas de la base (no solo las visibles). Rechaza
    con 400, y con el nombre visible de la propiedad, las tres cosas que no
    pueden pasar: que la madre no sea de esta base, que la fila sea su propia
    madre y que la madre sea una descendiente suya (ciclo).

    Un ciclo se detecta subiendo por la cadena de madres desde la que se quiere
    poner: si en algún momento se llega a la fila que se está guardando, el
    vínculo cerraría el círculo. La cadena se corta sola si el `.md` ya traía un
    ciclo (`vistas`), así que un dato raro a mano no deja esto en bucle.
    """
    key = parent_key(schema_def)
    if not key or key not in properties:
        return
    prop = parent_property(schema_def) or {}
    nombre = f"«{prop.get('name') or key}»"
    raw = properties.get(key)
    if raw is None:
        return                     # sin madre: la clave se va del `.md`

    madre = relation_id(raw)
    if madre is None:
        raise HTTPException(
            400,
            f"{nombre}: {raw!r} no es el id de una fila. Una subtarea guarda el "
            "id de su fila madre, no su título.",
        )

    por_id = {str(row.id): row for row in rows}
    if madre not in por_id:
        raise HTTPException(
            400,
            f"{nombre}: la fila {madre} no es de esta base. Una subtarea solo "
            "puede colgar de una fila de su misma base.",
        )

    if row_id is not None and madre == str(row_id):
        raise HTTPException(
            400, f"{nombre}: una fila no puede ser su propia madre."
        )

    if row_id is None:
        return                     # una fila nueva no puede cerrar un ciclo

    titulo = _titulo(por_id.get(str(row_id))) or "esta fila"
    camino: list[str] = []          # de la madre hacia arriba, con nombres
    vistas: set[str] = set()
    actual: Optional[str] = madre
    while actual and actual not in vistas:
        if actual == str(row_id):
            cadena = " → ".join([*camino, titulo])
            raise HTTPException(
                400,
                f"{nombre}: no se puede colgar «{titulo}» de "
                f"«{_titulo(por_id.get(madre)) or madre}» porque ya cuelga de "
                f"ella ({cadena}): sería un ciclo. Elige otra fila madre.",
            )
        vistas.add(actual)
        fila = por_id.get(actual)
        camino.append(_titulo(fila) or actual)
        actual = parent_of(fila, key) if fila is not None else None


def _titulo(row: Any) -> str:
    return str(getattr(row, "title", "") or "")
