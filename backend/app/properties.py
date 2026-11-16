"""Validación ligera de `properties` de una fila contra el schema_def de su database.

Deliberadamente laxa: valida que las claves existan en el schema y hace una
comprobación de tipo básica (numérico, booleano, opciones de select), pero
no fuerza campos obligatorios ni formatos estrictos de fecha/url — eso puede
endurecerse más adelante si hace falta, sin romper filas ya guardadas.

La excepción es **`relation`**: una celda de relación es el `id` de una fila de
otra base, así que al guardar se valida de verdad (el id existe, es de la base
destino y el número de ids cuadra con `multiple`). Al leer se sigue siendo
laxo: un `.md` con texto libre donde ahora hay una relación se lee como «sin
valor» y no se reescribe (ver `app.vault.reader._relation_value`).

Lo mismo vale para **`parent_row`** (subtareas): la celda es el `id` de una
fila de la **misma** base. Aquí solo se valida su forma —un id, no una lista ni
un título—; que la fila exista, que sea de esta base y que el vínculo no cierre
un ciclo lo comprueba `app.hierarchy`, que es quien tiene las filas.

Aquí vive también el **plan** del schema: la interfaz solo pide el nombre
visible de una propiedad y la clave se deriva detrás. La clave no se enseña
nunca, así que el cliente no tiene que saber snake_case; y cuando el nombre de
una propiedad existente cambia, el plan dice qué claves hay que migrar en las
filas y en las vistas.
"""

import re
import unicodedata
from typing import Any, Callable, Iterable, Mapping, Optional

from fastapi import HTTPException

from app.vault.reader import (
    PARENT_ROW,
    PERSON,
    RELATION,
    RESERVED_ROW_KEYS,
    relation_id,
    relation_multiple,
)

# `refs(ids)` devuelve `{id: (título, id de su base)}` de las filas que existan.
# Lo resuelve el repositorio en **una** consulta por respuesta: ni una por
# celda ni una por fila (N+1).
RowRefs = Callable[[Iterable[str]], Mapping[str, tuple[str, str]]]

# `users(ids)` devuelve `{id: (display_name, username, color)}` de los usuarios
# que existan. Al serializar una respuesta se llama una vez con todos los ids;
# al validar una fila se llama una vez por celda `person` (las filas se guardan
# de una en una), que es una consulta por celda y no por respuesta.
UserRefs = Callable[[Iterable[str]], Mapping[str, tuple[str, Optional[str], Optional[str]]]]

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Nombre visible de la columna del título cuando la base no la ha renombrado.
DEFAULT_TITLE_NAME = "Título"


def slug_key(name: str, taken: set[str]) -> str:
    """La clave interna de un nombre visible: `Fecha de entrega` -> `fecha_de_entrega`.

    Sin tildes (NFD + fuera las marcas), en minúsculas, y todo lo que no sea
    letra o dígito se convierte en `_` (varios seguidos son uno, sin `_` en los
    extremos). Si el nombre no deja nada (`···`), la clave es `propiedad`; si
    empieza por dígito, se prefija `p_` porque el contrato exige empezar por
    letra. Si choca con una clave ya usada, sufijo numérico: `fecha_2`, `fecha_3`.

    Las claves de `RESERVED_ROW_KEYS` (`title`, `id`, `type`, `icon`,
    `description`, `created`, `updated`) están siempre ocupadas: son la
    identidad del fichero de la fila, no propiedades, y el lector las descarta
    del schema. Sin esto, llamar «Title» a una propiedad derivaba `title`, la
    columna desaparecía en silencio y el siguiente guardado la borraba del
    `.md`; con esto la clave es `title_2` y la propiedad funciona.
    """
    plano = unicodedata.normalize("NFKD", str(name or ""))
    plano = "".join(c for c in plano if not unicodedata.combining(c)).lower()
    base = re.sub(r"[^a-z0-9]+", "_", plano).strip("_")
    if not base:
        base = "propiedad"
    if not base[0].isalpha():
        base = f"p_{base}"
    ocupadas = set(taken) | RESERVED_ROW_KEYS
    key = base
    attempt = 2
    while key in ocupadas:
        key = f"{base}_{attempt}"
        attempt += 1
    return key


def plan_schema(
    current: list[dict], requested: list[dict]
) -> tuple[list[dict], dict[str, str], list[str]]:
    """Traduce lo que pide el diálogo al `schema` que se guarda.

    `requested` trae, por propiedad, su `key` (la que tenía: la identidad de la
    fila en el diálogo; vacía en una propiedad nueva) y el **nombre** que ha
    escrito el usuario. Devuelve `(schema_final, renombres, borradas)`:

    - `schema_final`: las propiedades con su clave definitiva. Una entrada con
      `key` vacía la deriva del nombre; una con `key` desconocida la respeta (la
      API sigue aceptando claves explícitas, como los tests y las plantillas).
    - `renombres`: `{clave_vieja: clave_nueva}` de las propiedades cuyo **nombre
      ha cambiado**. Una clave vieja que el usuario no ha tocado no se
      «normaliza» sola: las bases con `proximo_contacto` siguen como están
      hasta que alguien renombre esa propiedad de verdad.
    - `borradas`: claves que ya no están en el schema (para limpiar las vistas
      que las nombran en `group_by`, `date_property`, `filters`, `sort` o
      `visible_properties`).

    Dos propiedades no pueden quedar con la misma clave; si el nombre nuevo
    choca con una existente, se le pone sufijo numérico (la clave es interna,
    el usuario solo ve nombres).
    """
    actuales = {str(p.get("key")): p for p in current if isinstance(p, dict) and p.get("key")}
    renombres: dict[str, str] = {}

    def nombre(item: dict) -> str:
        return str(item.get("name") or "").strip()

    # Primero, qué claves se quedan: las que vienen con su clave de siempre y
    # el mismo nombre. Todo lo demás (nombre cambiado) es un renombrado.
    conservadas: set[str] = set()
    for item in requested:
        key = str(item.get("key") or "").strip()
        if key in actuales and nombre(item).casefold() == nombre(actuales[key]).casefold():
            conservadas.add(key)
    # Las claves que se van a renombrar siguen ocupadas hasta que la migración
    # las libere en las filas: una clave nueva no puede caer sobre una de ellas
    # (el rename de la fila pisaría el valor de otra propiedad en la misma
    # escritura).
    fuentes = {k for k in actuales if k not in conservadas}
    taken: set[str] = set(conservadas)

    finales: list[dict[str, Any]] = []
    for item in requested:
        key = str(item.get("key") or "").strip()
        # La propiedad de antes con esa clave, si la hay: una relación conserva
        # su base destino y su `multiple` cuando la petición no los trae.
        origen = actuales.get(key)
        name = nombre(item)
        if not name:
            raise HTTPException(400, "Cada propiedad necesita un nombre")
        if key in actuales and key not in conservadas:
            # Renombrado: el nombre visible ha cambiado. La clave nueva se
            # deriva del nombre y se migra en filas y vistas. La propia clave
            # se permite (un cambio de espacios que deja el mismo slug no
            # reescribe ninguna fila).
            nueva = slug_key(name, (taken | fuentes) - {key})
            if nueva != key:
                renombres[key] = nueva
                taken.add(nueva)
            key = nueva
        elif key and key not in actuales:
            # Clave explícita de una propiedad nueva (plantillas y API directa).
            if not KEY_RE.match(key):
                raise HTTPException(400, f"Clave no válida: {key!r}")
            if key in RESERVED_ROW_KEYS:
                raise HTTPException(
                    400,
                    f"«{name}» no puede guardarse con la clave {key!r}: es una "
                    "clave de la propia fila (identidad, tipo, icono, "
                    "descripción, fechas o título)",
                )
            if key in taken:
                raise HTTPException(400, f"Clave repetida: {key!r}")
        elif not key:
            key = slug_key(name, taken)
        taken.add(key)
        prop: dict[str, Any] = {"key": key, "name": name, "type": item.get("type")}
        if item.get("options"):
            prop["options"] = list(item["options"])
        if item.get("type") == RELATION:
            _plan_relation(prop, item, origen)
        if item.get("type") == PARENT_ROW:
            if any(otra.get("type") == PARENT_ROW for otra in finales):
                otra = next(p for p in finales if p.get("type") == PARENT_ROW)
                raise HTTPException(
                    400,
                    f"Una base solo puede tener una propiedad de subtareas: "
                    f"«{otra.get('name')}» ya lo es. Cambia el tipo de «{name}» "
                    "o quita una de las dos: la jerarquía de la base es una.",
                )
        _plan_done(prop, item, origen, name)
        finales.append(prop)

    claves_finales = {p["key"] for p in finales}
    # Una clave renombrada ya no está entre las finales, pero no es una
    # propiedad borrada: sus vistas se traducen, no se vacían.
    borradas = [k for k in actuales if k not in claves_finales and k not in renombres]
    return finales, renombres, borradas


def _plan_relation(prop: dict, item: dict, origen: Optional[dict]) -> None:
    """Lo que una propiedad `relation` guarda además de su tipo.

    `target_database` es la base a la que apunta y `multiple` si enlaza varias
    filas. Si la petición **no** los trae y la propiedad ya era una relación, se
    conservan: el diálogo de propiedades de la interfaz todavía no pregunta por
    ellos (va en la tarjeta siguiente) y guardar el resto de la base no puede
    dejar la relación sin destino. `target_database: ""` los borra a propósito.

    No se comprueba aquí que la base destino exista: el `.md` es la verdad y la
    base puede llegar después (o haberse borrado, que es lo que la lectura
    avisa con `warning`). Lo que sí se valida es la celda, contra una base que
    exista de verdad (ver `validate_properties`).
    """
    crudo = item.get("target_database")
    if crudo is None:
        target = relation_id((origen or {}).get("target_database"))
    elif str(crudo).strip() == "":
        target = None
    else:
        target = relation_id(crudo) or str(crudo).strip()
    if target:
        prop["target_database"] = target

    multiple = item.get("multiple")
    if multiple is None:
        multiple = (origen or {}).get("multiple")
    if relation_multiple({"multiple": multiple}):
        prop["multiple"] = True


def _plan_done(prop: dict, item: dict, origen: Optional[dict], name: str) -> None:
    """El marcador `done` de una propiedad, que dice qué cuenta como «cerrada».

    Es lo que usa el contador de subtareas (`app.hierarchy.is_closed`): un
    `checkbox` con `done: true` (marcado = cerrada) o un `select`/`multiselect`
    con `done: [valor, ...]` (la celda vale uno de ellos = cerrada).

    Si la petición no lo trae se conserva el que ya tenía la propiedad: el
    diálogo de propiedades no pregunta por él, y guardar el resto de la base no
    puede desactivar el contador sin que nadie lo haya pedido. `done: false`
    (o una lista vacía) lo quita.

    Un marcador que no cabe en el tipo **pedido en esta petición** es un 400
    (quien llama se ha equivocado). Si venía heredado —la propiedad era una
    selección con `done: [Hecho]` y ahora pasa a texto— se suelta: el contador
    deja de contar cerradas, que es la verdad de esa base, y guardar el resto
    del schema no puede fallar por un marcador que el usuario no ha escrito.

    Lo mismo con las opciones de la propiedad: un `done` que nombre un valor
    que no está en `options` no puede contar ninguna fila —la celda nunca
    valdría ese valor—, así que el contador se quedaría en «0 de N» para
    siempre y sin aviso. Es un 400 si viene pedido; si venía heredado se suelta,
    como el caso del tipo (renombrar una opción no puede romper el guardado).

    La comparación es la del lector (`app.closed.is_closed`): sin mayúsculas ni
    espacios de sobra. Un `done: [HECHA]` sobre `options: [hecha]` cuenta, así
    que no se rechaza; se guarda con la grafía de la opción, que es la que la
    celda puede tener. Si el marcador viene heredado y la opción solo ha
    cambiado de grafía, se queda (reescrito) en vez de soltarse: soltarlo dejaba
    el contador en «0 de N» sin aviso, que es la trampa que esto cierra.
    """
    pedido = item.get("done")
    crudo = pedido if pedido is not None else (origen or {}).get("done")
    if crudo is None:
        return

    valores: list[str] = []
    if isinstance(crudo, list):
        valores = [str(v).strip() for v in crudo if str(v).strip()]
        if not valores:
            return
    elif str(crudo).strip().lower() in ("false", "0", "no", "off", "") or crudo is False:
        return

    def rechazar(motivo: str) -> None:
        if pedido is None:      # heredado: se suelta, no se rompe el guardado
            return
        raise HTTPException(400, f"«{name}»: {motivo}")

    tipo = prop.get("type")
    if tipo not in ("checkbox", "select", "multiselect"):
        # `type` llega como `PropertyType` (un `str`-enum): su `str()` es
        # «PropertyType.text», que no es lo que puede leer quien usa la API.
        etiqueta = getattr(tipo, "value", tipo)
        rechazar(
            "`done` solo tiene sentido en una casilla o una selección (es la "
            f"propiedad que dice si una fila está cerrada), y esta es de tipo «{etiqueta}»"
        )
        return
    if tipo == "checkbox":
        if valores:
            rechazar("en una casilla basta `done: true`: se cierra cuando está marcada")
            return
        prop["done"] = True
        return
    if not valores:
        rechazar(
            "en una selección, `done` es la lista de valores que cuentan como "
            "cerrada (`done: [Hecho]`)"
        )
        return
    # `done` son valores de la celda, así que tienen que ser opciones de la
    # propiedad: uno que no esté nunca coincidirá con la celda y el contador se
    # quedaría en «0 de N» sin decir nada. Se compara **sin mayúsculas** —igual
    # que el lector (`app.closed.is_closed`)— y se guarda la grafía de la
    # opción: un `done: [HECHA]` sobre `options: [hecha]` sí cuenta, así que
    # rechazarlo sería un 400 que miente, y dejarlo con la grafía tecleada
    # escribiría un valor que la celda no tiene.
    opciones = [
        str(o).strip()
        for o in (
            prop.get("options")
            if prop.get("options") is not None
            else (origen or {}).get("options")
        )
        or []
        if str(o).strip()
    ]
    if not opciones:
        # Sin opciones declaradas no hay lista contra la que validar: cualquier
        # valor de celda es válido (misma tolerancia que la celda de un select).
        prop["done"] = valores
        return
    canonica = {opcion.casefold(): opcion for opcion in opciones}
    elegidos: list[str] = []
    fuera: list[str] = []
    for valor in valores:
        real = canonica.get(valor.casefold())
        if real is None:
            fuera.append(valor)
        elif real not in elegidos:
            elegidos.append(real)
    if fuera:
        sobran = ", ".join(f"«{v}»" for v in fuera)
        rechazar(
            f"{sobran} no {'está' if len(fuera) == 1 else 'están'} entre las "
            "opciones de la propiedad, así que ninguna fila contaría como "
            f"cerrada con {'él' if len(fuera) == 1 else 'ellos'}. Opciones: "
            + ", ".join(opciones)
        )
        return
    prop["done"] = elegidos


def relation_properties(schema_def: list[dict]) -> list[dict]:
    """Las propiedades `relation` del schema, en su orden."""
    return [
        prop
        for prop in schema_def
        if isinstance(prop, dict) and prop.get("type") == RELATION and prop.get("key")
    ]


def relation_ids(schema_def: list[dict], properties: Iterable[dict]) -> list[str]:
    """Los ids que referencian todas las celdas de relación, sin repetir.

    Es la lista que se resuelve de una vez al serializar una respuesta: con las
    veinte filas de una tabla, una consulta, no veinte.
    """
    ids: dict[str, None] = {}
    props = relation_properties(schema_def)
    for row in properties:
        for prop in props:
            value = (row or {}).get(prop["key"])
            items = value if isinstance(value, list) else ([value] if value else [])
            for one in items:
                ids.setdefault(str(one), None)
    return list(ids)


def related_refs(
    schema_def: list[dict],
    properties: dict[str, Any],
    refs: Mapping[str, tuple[str, str]],
) -> dict[str, list[dict[str, Any]]]:
    """`related` de una fila: `{clave: [{id, title, base_id}]}`.

    El id de la celda se queda en `properties` aunque la fila ya no exista, y
    aquí sale con `title: null`: la celda «apunta a una fila borrada», que es
    lo que el interfaz necesita para enseñar «(borrada)» en vez de una celda
    vacía que no explica nada. `base_id` es la base de la fila referenciada (la
    de la propiedad, si la fila ya no está).
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for prop in relation_properties(schema_def):
        value = (properties or {}).get(prop["key"])
        items = value if isinstance(value, list) else ([value] if value else [])
        if not items:
            continue
        target = str(prop.get("target_database") or "") or None
        filas: list[dict[str, Any]] = []
        for one in items:
            fila = refs.get(str(one))
            filas.append(
                {
                    "id": str(one),
                    "title": fila[0] if fila else None,
                    "base_id": (fila[1] if fila else None) or target,
                }
            )
        out[prop["key"]] = filas
    return out


def schema_with_relation_warnings(
    schema_def: list[dict], known_databases: set[str]
) -> list[dict]:
    """El schema tal y como se devuelve: las relaciones rotas, marcadas.

    `warning` **no** existe en el `.md` (se calcula al leer): borrar la base
    destino deja sus relaciones huérfanas, y la propiedad tiene que poder
    decirlo sin que falle el render ni el reindexado. Tampoco se guarda al
    volver a escribir la base: `plan_schema` solo copia lo que es del schema.
    """
    out: list[dict] = []
    for prop in schema_def:
        if isinstance(prop, dict) and prop.get("type") == RELATION:
            target = relation_id(prop.get("target_database"))
            if not target:
                prop = {**prop, "warning": "la propiedad no declara una base destino válida"}
            elif target not in known_databases:
                prop = {**prop, "warning": "la base destino ya no existe"}
        out.append(prop)
    return out


def validate_properties(
    schema_def: list[dict],
    properties: dict[str, Any],
    *,
    refs: Optional[RowRefs] = None,
    users: Optional[UserRefs] = None,
) -> dict[str, Any]:
    """Coacciona y valida las propiedades que llegan en una fila.

    `refs` resuelve ids de fila a `(título, base)` en una sola consulta y solo
    hace falta si la base tiene propiedades `relation`; sin él, una relación no
    se puede validar (el id podría ser de cualquier base) y se rechaza con un
    500 explícito en vez de guardar algo que no apunta a nada.

    `users` resuelve los ids de usuario de una propiedad `person`: el router
    siempre lo pasa; sin él la celda se rechaza con un 500 explícito en vez de
    guardar un id que no existe. Se consulta una vez por celda validada (al
    guardar, las filas van de una en una); al serializar, una por respuesta.
    """
    allowed = {p["key"]: p for p in schema_def}
    unknown = set(properties) - set(allowed)
    if unknown:
        raise HTTPException(400, f"Propiedades no definidas en el schema: {', '.join(sorted(unknown))}")

    return {key: _coerce(allowed[key], value, refs, users) for key, value in properties.items()}


def _coerce(
    prop: dict,
    value: Any,
    refs: Optional[RowRefs] = None,
    users: Optional[UserRefs] = None,
) -> Any:
    if value is None:
        return None

    ptype = prop.get("type")

    if ptype == RELATION:
        return _coerce_relation(prop, value, refs)

    if ptype == PARENT_ROW:
        return _coerce_parent(prop, value)
    if ptype == PERSON:
        return _coerce_person(prop, value, users)

    if ptype == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"'{prop['key']}' debe ser numérico")

    if ptype == "checkbox":
        if not isinstance(value, bool):
            raise HTTPException(400, f"'{prop['key']}' debe ser true/false")
        return value

    if ptype == "select":
        options = prop.get("options")
        if options and value not in options:
            raise HTTPException(400, f"'{prop['key']}': '{value}' no está en options {options}")
        return value

    if ptype == "multiselect":
        if not isinstance(value, list):
            raise HTTPException(400, f"'{prop['key']}' debe ser una lista")
        options = prop.get("options")
        if options:
            bad = [v for v in value if v not in options]
            if bad:
                raise HTTPException(400, f"'{prop['key']}': valores no válidos {bad}")
        return value

    return value  # text, date, url: sin validar formato en v1


def _coerce_parent(prop: dict, value: Any) -> Any:
    """Valida la FORMA de una celda `parent_row`: el id de una fila, y nada más.

    Que la fila exista, que sea de esta misma base y que el vínculo no cierre un
    ciclo lo comprueba `app.hierarchy.validate_parent` al guardar, que es quien
    tiene delante las filas de la base. Aquí se rechaza lo que no puede ser un
    id: una lista (la jerarquía es una madre, no varias), un mapa o un título.
    Un valor vacío es «sin madre» (`None`), como en cualquier celda vacía.
    """
    nombre = f"«{prop.get('name') or prop.get('key')}»"
    if isinstance(value, (list, dict)):
        raise HTTPException(
            400,
            f"{nombre} guarda UNA fila madre: manda el id de una fila, no una lista",
        )
    pid = relation_id(value)
    if pid is None:
        raise HTTPException(
            400,
            f"{nombre}: {value!r} no es el id de una fila. Una subtarea guarda el "
            "id de su fila madre, no su título.",
        )
    return pid


def _coerce_person(prop: dict, value: Any, users: Optional[UserRefs]) -> Any:
    """Valida una celda `person`: el id de un usuario que exista.

    El mensaje de error nombra el **nombre visible** de la propiedad
    (`«Responsable»`), como en `relation`: es lo único que el usuario ve en la
    tabla. Una celda vacía es «sin asignar» (`None`, la clave se va del `.md`),
    que es lo que hace que `is_empty` del filtro ya sirva sin un valor especial.
    """
    nombre = f"«{prop.get('name') or prop.get('key')}»"
    if isinstance(value, (list, dict)):
        raise HTTPException(
            400, f"{nombre} guarda una persona: manda el id de un usuario, no una lista"
        )
    user_id = str(value).strip()
    if not user_id:
        return None
    if users is None:           # pragma: no cover - el router siempre lo pasa
        raise HTTPException(
            500, f"{nombre}: no se puede comprobar el usuario sin acceso a la tabla de usuarios"
        )
    if user_id not in users([user_id]):
        raise HTTPException(400, f"{nombre}: el usuario {user_id} no existe")
    return user_id


def person_properties(schema_def: list[dict]) -> list[dict]:
    """Las propiedades `person` del schema, en su orden."""
    return [
        prop
        for prop in schema_def
        if isinstance(prop, dict) and prop.get("type") == PERSON and prop.get("key")
    ]


def person_keys(schema_def: list[dict]) -> frozenset[str]:
    """Las claves de las propiedades `person` (para filtrar y ordenar)."""
    return frozenset(prop["key"] for prop in person_properties(schema_def))


def person_ids(schema_def: list[dict], properties: Iterable[dict]) -> list[str]:
    """Los ids de usuario que referencian las celdas `person`, sin repetir.

    Misma idea que `relation_ids`: la lista se resuelve de una vez al
    serializar, no una consulta por celda.
    """
    ids: dict[str, None] = {}
    props = person_properties(schema_def)
    for row in properties:
        for prop in props:
            value = (row or {}).get(prop["key"])
            if isinstance(value, str) and value.strip():
                ids.setdefault(value.strip(), None)
    return list(ids)


def people_of(
    schema_def: list[dict],
    properties: dict[str, Any],
    users: Mapping[str, tuple[str, Optional[str], Optional[str]]],
) -> dict[str, dict[str, Any]]:
    """`people` de una fila: `{clave: {id, display_name, username, color}}`.

    El id se queda en `properties` aunque el usuario ya no exista, y aquí sale
    con `display_name: null`: la celda «apunta a alguien que ya no está», que
    es lo que el interfaz necesita para decirlo en vez de enseñar un hueco que
    no explica nada.
    """
    out: dict[str, dict[str, Any]] = {}
    for prop in person_properties(schema_def):
        value = (properties or {}).get(prop["key"])
        user_id = value.strip() if isinstance(value, str) else ""
        if not user_id:
            continue
        usuario = users.get(user_id)
        out[prop["key"]] = {
            "id": user_id,
            "display_name": usuario[0] if usuario else None,
            "username": usuario[1] if usuario else None,
            "color": usuario[2] if usuario else None,
        }
    return out


def _coerce_relation(
    prop: dict, value: Any, refs: Optional[RowRefs]
) -> Any:
    """Valida una celda de relación contra la base destino.

    El mensaje de error nombra el **nombre visible** de la propiedad
    (`«Contacto»`), nunca su clave: es lo único que el usuario ve en la tabla.
    Se valida lo que la tarjeta pide comprobar: que el id sea un id, que exista,
    que esté en la base destino y que el número de ids cuadre con `multiple`.
    """
    nombre = f"«{prop.get('name') or prop.get('key')}»"
    target = relation_id(prop.get("target_database"))
    if not target:
        raise HTTPException(
            400,
            f"{nombre} es una relación y no declara a qué base apunta: "
            "no se puede comprobar que la fila exista",
        )

    if relation_multiple(prop):
        if not isinstance(value, list):
            raise HTTPException(
                400, f"{nombre} enlaza varias filas: manda una lista de ids, no un id suelto"
            )
        crudos = value
    else:
        if isinstance(value, list):
            raise HTTPException(
                400, f"{nombre} enlaza una sola fila: manda el id, no una lista"
            )
        crudos = [value]

    ids: list[str] = []
    for crudo in crudos:
        rid = relation_id(crudo)
        if rid is None:
            raise HTTPException(400, f"{nombre}: {crudo!r} no es un id de fila válido")
        if rid not in ids:      # duplicados: una fila se enlaza una vez
            ids.append(rid)

    if not ids:
        # Una relación múltiple sin ids es «sin valor» (la clave se va del
        # `.md`, como cualquier celda vacía): `null` en la API, no `[]`.
        return None

    if refs is None:            # pragma: no cover - el router siempre lo pasa
        raise HTTPException(
            500, f"{nombre}: no se puede comprobar la relación sin acceso al índice"
        )
    encontradas = refs(ids)
    for rid in ids:
        ref = encontradas.get(rid)
        if ref is None:
            raise HTTPException(400, f"{nombre}: la fila {rid} no existe")
        if str(ref[1]) != target:
            raise HTTPException(
                400, f"{nombre}: la fila {rid} no es de la base a la que apunta esta relación"
            )

    return ids if relation_multiple(prop) else ids[0]
