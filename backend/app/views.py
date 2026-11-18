"""Filtrado y ordenación de las filas de una base según la configuración de una vista.

Vive aquí y no en el router (`app/routers/databases.py`) porque hay dos
consumidores: `GET /databases/{id}/rows?view=...`, que devuelve las filas que
pinta la vista, y la exportación a Markdown (P17.4), que materializa la misma
tabla que se ve en el panel. Duplicar el filtro haría que el `.md` exportado y
la pantalla pudieran discrepar sin que ningún test lo notara.

Se filtra y ordena en Python (no en SQL): a escala personal, con todas las
filas ya en memoria, es más simple y suficientemente rápido que construir
predicados dinámicos sobre JSON. `group_by`/`visible_properties` son metadatos
para que el frontend decida cómo dibujar la vista (tablero, calendario...) —
la exportación sí usa `visible_properties` para elegir columnas, pero eso lo
decide `app.export`.

Forma del `config` de una vista (en el frontmatter de `Bases/<slug>.md`):

- `filters: [{key, op, value}]`: se combinan con **Y** (la fila se enseña si
  cumple todas). `value` no se escribe en los operadores que no lo necesitan
  (`is_empty`, `is_not_empty`, `checked`, `unchecked`). `is_closed` e `is_open`
  tampoco llevan `value` **ni necesitan `key`**: quién está cerrada lo dice la
  regla de la base (`closed: {property, values}`, ver `app.closed`), así que el
  filtro no tiene que enumerar valores ni acertar con la columna.
- `sort: [{key, dir}]`: varias claves en orden de prioridad. También se acepta
  la forma de una sola clave que escribían las versiones anteriores
  (`sort: {key, dir}`); se sigue leyendo y no se reescribe al mirarla.
- `parent_level: roots | all`: qué filas de una base con jerarquía se enseñan.
  `roots` deja solo las que no son subtareas de nadie (por defecto en tabla y
  tablero: una hija ya se ve debajo de su madre, y duplicarla es ruido); `all`
  enseña todas, indentadas bajo su madre. Sin la clave manda el tipo de vista
  (ver `app.hierarchy.level`), así que una base sin jerarquía no cambia nada.
- `start_property` / `end_property`: el **rango** de un calendario. Las dos son
  la clave de una propiedad de fecha; la barra de la fila va del primer día de
  `start_property` al último de `end_property` (los dos incluidos) y `end_property`
  puede faltar, que entonces la fila dura un día. Sin ninguna de las dos, el
  calendario coloca cada fila en un solo día (`date_property`), como siempre.
  No afectan al filtro ni al orden: son metadatos de dibujo, como `group_by`.
  `validar_rango` comprueba que nombran fechas de verdad.

Los valores del frontmatter se leen como texto (el parser de `fm` no infiere
tipos), así que los operadores comparan como número cuando los dos lados lo
son y como texto en el resto de los casos: una fecha ISO se compara bien como
texto.

El orden es **por tipo de propiedad**, y el tipo lo pasa quien llama
(`types`, de `app.properties.schema_types`): el texto se ordena natural (sin
mayúsculas ni tildes, y «Tarea 2» antes que «Tarea 10»), el número como número,
la fecha cronológicamente, la casilla por su valor, la persona por su nombre
visible y la relación por el título de la fila enlazada. Una fecha imposible
(`31-nov`) y una relación rota no tienen sitio en el orden y van al final, como
los vacíos.

Una propiedad `select`/`multiselect` puede declarar el **orden de sus valores**
(`sort_order` en el `schema` de la base, ver `app.properties.order_ranks`).
Entonces ordenar por ella usa ese orden y no el alfabético, en `asc` y en
`desc`, y como clave secundaria de un orden de varias claves. El valor que no
está declarado —el que apareció después, o el que alguien escribió a mano en un
`.md`— va **al final en las dos direcciones**: no tener sitio no es «ser el
primero». En modo `auto` (o sin `sort_order`) se ordena por el texto natural,
como el resto de una base que no declara nada.

Una propiedad `person` guarda el **id** de un usuario, pero lo que se lee y se
escribe es su nombre visible: `eq`/`neq` aceptan el nombre además del id y el
orden va por nombre. Para eso hace falta `people` (`id -> display_name`), que
resuelve quien llama; sin él se compara y se ordena por el id, que sigue siendo
determinista.
"""

import re
import unicodedata
from datetime import datetime
from typing import Any, Mapping, Optional

from app import closed, hierarchy
from app.properties import option_key
from app.repository import Page

# Operadores que no llevan `value`: la condición es sobre el estado del valor.
SIN_VALOR = frozenset({"is_empty", "is_not_empty", "checked", "unchecked"})

# Claves del `config` que declaran el **rango** de un calendario: de qué
# propiedad de fecha sale el primer día de la barra y de cuál el último. Sin
# ellas el calendario coloca cada fila en un solo día (`date_property`), como
# siempre.
INICIO_DE_RANGO = "start_property"
FIN_DE_RANGO = "end_property"
CLAVES_DE_RANGO = (INICIO_DE_RANGO, FIN_DE_RANGO)


def validar_rango(
    config: Mapping[str, Any] | None, schema_def: list[dict]
) -> Optional[str]:
    """Motivo por el que el rango de una vista no vale, o `None` si vale.

    Un calendario con rango declara `start_property` y `end_property` con la
    **clave interna** de dos propiedades de fecha, igual que `group_by` o
    `date_property`. Se valida contra el `schema` de la base, que el esquema de
    la petición no tiene:

    - la clave tiene que existir en el schema. Una vista que nombra una
      propiedad que ya no está dejaría el calendario cayendo a la primera fecha
      en silencio, y eso es peor que decir que la vista está mal;
    - tiene que ser de tipo `date`: una relación o un texto no son un día;
    - las dos, si están, no pueden ser la misma: un rango de la misma fecha
      consigo misma no dice nada.

    `end_property` es opcional: un rango con solo inicio son filas de un día
    (el día de inicio). Sin ninguna de las dos claves la vista no declara rango
    y el calendario se comporta como antes (`date_property`).

    Devuelve el motivo en castellano, listo para un 400, o `None`.
    """
    if not config:
        return None
    props = {
        str(p.get("key")): p
        for p in (schema_def or [])
        if isinstance(p, dict) and p.get("key")
    }
    elegidas: dict[str, str] = {}
    for clave in CLAVES_DE_RANGO:
        valor = config.get(clave)
        if valor is None or (isinstance(valor, str) and not valor.strip()):
            continue
        if not isinstance(valor, str):
            return f"«{clave}» tiene que ser la clave de una propiedad de fecha."
        prop = props.get(valor.strip())
        if prop is None:
            return (
                f"La vista declara «{clave}: {valor}», pero esa propiedad no "
                "existe en la base. Corrige la vista o vuelve a elegir la fecha."
            )
        if prop.get("type") != "date":
            return (
                f"«{prop.get('name') or valor}» no es una propiedad de fecha, y "
                f"«{clave}» tiene que serlo: un rango se mide en días."
            )
        elegidas[clave] = valor.strip()
    if len(set(elegidas.values())) != len(elegidas):
        return (
            "El inicio y el fin del rango no pueden ser la misma propiedad: "
            "«{0}» ya es el inicio.".format(next(iter(elegidas.values())))
        )
    return None

# Operadores que pregunta por la semántica de «cerrada» de la base. No llevan
# `value` ni `key`: la respuesta la da la regla de la base, no una celda.
CLOSED_OPS = frozenset({"is_closed", "is_open"})

# Operadores que comparan magnitudes (número, o fecha ISO como texto).
COMPARACIONES = frozenset({"gt", "gte", "lt", "lte", "before", "after"})


def apply_view(
    rows: list[Page],
    config: dict,
    *,
    parent_key: str | None = None,
    view_type: str | None = None,
    people: Mapping[str, str] | None = None,
    person_keys: frozenset[str] | set[str] = frozenset(),
    closed_rule: Mapping[str, Any] | None = None,
    orders: Mapping[str, Mapping[str, int]] | None = None,
    types: Mapping[str, str] | None = None,
    refs: Mapping[str, tuple[str, str]] | None = None,
) -> list[Page]:
    """Aplica los `filters`, el `sort` y el nivel de una vista sobre sus filas.

    `parent_key` es la clave de la propiedad `parent_row` de la base, si la
    tiene. Con ella, la vista puede quedarse solo con las madres (el
    `parent_level` de su `config`, por defecto `roots` en tabla y tablero) y las
    hijas se colocan detrás de su madre en el resultado: el orden de las ramas
    lo decide el `sort` de la vista, y dentro de una rama se repite la regla
    (`app.hierarchy.arrange`). Sin `parent_key` —una base sin jerarquía— nada de
    esto se aplica y el resultado es exactamente el de siempre.

    El nivel se aplica ANTES que los `filters`: «solo madres» elige qué filas
    son las de la vista y el filtro se aplica a esas. Consecuencia conocida: un
    filtro sobre una propiedad que solo tienen las hijas deja la vista vacía
    (la madre no lo cumple), porque una fila no se enseña «porque su hija
    cumple». Heredar el filtro hacia la madre es una decisión de producto aparte
    y hoy no se hace.

    `people` e `person_keys` son para las propiedades `person`: el `.md` guarda
    el id de un usuario y el nombre visible lo resuelve el servidor. Sin
    `person_keys` todo se comporta como siempre.

    `closed_rule` es la regla de qué cuenta como fila cerrada de ESTA base
    (`Database.closed`). Solo la usan los filtros `is_closed`/`is_open`; sin
    regla, nada está cerrado y `is_open` deja pasar todo, que es la verdad de
    una base que no declara estado.

    `orders` es `{clave: {valor: puesto}}` de las propiedades que declaran el
    orden de sus valores (`app.properties.order_ranks`). Sin `orders`, o con
    una clave que no está, el orden es el de siempre (alfabético o numérico):
    las bases que no declaran nada no cambian.

    `types` es `{clave: tipo}` del `schema` de la base (`app.properties.
    schema_types`) y `refs` es `{id: (título, base)}` de las filas que
    enlazan las relaciones. Con ellos cada clave ordena **según su tipo**:
    texto natural sin mayúsculas ni tildes, número, fecha, casilla, persona
    por su nombre visible y relación por el título de la fila enlazada. Sin
    `types` todo se ordena como antes (texto/número), que es lo que necesitan
    las pruebas de la regla sin pasar por un servidor.
    """
    gente = people or {}
    claves_persona = person_keys or frozenset()
    if parent_key and hierarchy.level(config, view_type) == hierarchy.LEVEL_ROOTS:
        rows = hierarchy.only_roots(rows, parent_key)

    for f in config.get("filters") or []:
        if not isinstance(f, dict):
            continue
        op = f.get("op", "eq")
        # «cerrada»/«abierta» no son una comparación sobre una celda: lo decide
        # la regla de la base, y por eso funcionan sin `key` y sin enumerar los
        # valores de estado (que es lo que obligaba a repetirlos en cada filtro).
        if op in CLOSED_OPS:
            quiere_cerrada = op == "is_closed"
            rows = [
                r
                for r in rows
                if closed.is_closed(closed_rule, r.properties or {}) == quiere_cerrada
            ]
            continue
        key, value = f.get("key"), f.get("value")
        if not key:
            continue
        es_persona = key in claves_persona
        rows = [
            r
            for r in rows
            if _match(
                (r.properties or {}).get(key), op, value, persona=es_persona, people=gente
            )
        ]

    claves = _sort_clauses(config.get("sort"))
    # La PRIMERA clave es la que manda, así que se ordena de la menos
    # significativa a la más significativa: `sorted` es estable y cada pasada
    # conserva el orden de la anterior (así se hace un orden por varias claves
    # sin construir una tupla de longitud variable).
    puestos = orders or {}
    tipos = types or {}
    for key, direccion in reversed(claves):
        tipo = tipos.get(key)
        if tipo is None and key in claves_persona:
            tipo = "person"
        rows = _sort_rows(
            rows,
            key,
            direccion == "desc",
            tipo=tipo,
            persona=tipo == "person",
            people=gente,
            orden=puestos.get(key),
            refs=refs,
        )

    if parent_key:
        rows = hierarchy.arrange(rows, parent_key)
    return rows


def sort_keys(config: Mapping[str, Any] | None) -> list[str]:
    """Las claves de orden de una vista, en orden de prioridad.

    La usa quien prepara `apply_view` para resolver solo lo que el orden
    necesita: si ninguna clave es una relación, no hay que ir a buscar los
    títulos de las filas enlazadas.
    """
    return [key for key, _ in _sort_clauses((config or {}).get("sort"))]


def _sort_clauses(raw: Any) -> list[tuple[str, str]]:
    """Las claves de orden normalizadas, aceptando la forma de lista y la de mapa.

    La forma de mapa (`sort: {key, dir}`) es la de las vistas que ya están
    escritas en los `.md`; la de lista (`sort: [{key, dir}]`) es la que hace
    falta para ordenar por varias claves. Las dos se leen igual.
    """
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [
        (str(item["key"]), str(item.get("dir") or "asc"))
        for item in raw
        if isinstance(item, dict) and item.get("key")
    ]


def _sort_rows(
    rows: list[Page],
    key: str,
    descendente: bool,
    *,
    tipo: str | None = None,
    persona: bool = False,
    people: Mapping[str, str] | None = None,
    orden: Mapping[str, int] | None = None,
    refs: Mapping[str, tuple[str, str]] | None = None,
) -> list[Page]:
    """Ordena por una clave **según el tipo de la propiedad**.

    Cada tipo ordena lo suyo (`tipo` sale del `schema` de la base):

    - `text`, `url` y el título: natural, sin distinguir mayúsculas ni tildes y
      con los números como números («Tarea 2» antes que «Tarea 10»).
    - `number`: numérico, aunque el valor llegue como texto del `.md`.
    - `date`: cronológico; una fecha imposible (`31-nov`) no tiene sitio.
    - `checkbox`: en `asc` van primero las sin marcar y en `desc` las marcadas
      (es un booleano: `false < true`), y se decide así para que la dirección
      diga algo en vez de dejarlo al azar.
    - `select`/`multiselect`: con orden declarado (`orden`), por su puesto; una
      multiselección por su valor **más importante** (el puesto más bajo). Sin
      orden declarado (modo `auto`), por el texto natural y, en una
      multiselección, por el valor **alfabéticamente menor**.
    - `person`: por el **nombre visible** de la persona, no por su id.
    - `relation`: por el **título** de la fila enlazada, no por su id; una
      relación rota (el destino ya no existe) no tiene sitio.

    Las filas **sin valor** van SIEMPRE al final, en las dos direcciones, y
    también las que tienen un valor que no se puede ordenar en su tipo (fecha
    imposible, relación rota, valor fuera del orden declarado). Entre esas se
    ordena por su texto, para que el resultado no dependa de cómo llegaron.

    `reverse=True` pondría las vacías delante, así que se separan antes: un
    hueco no es «lo más pequeño», es la ausencia de dato.
    """
    con_sitio: list[tuple[tuple, Page]] = []
    sin_sitio: list[tuple[tuple, Page]] = []
    vacias: list[Page] = []
    for row in rows:
        valor = row.title if key == "title" else _valor(row, key)
        if _vacio(valor):
            vacias.append(row)
            continue
        clave = _clave_de_orden(
            valor, tipo, persona=persona, people=people, orden=orden, refs=refs
        )
        if clave is None:
            sin_sitio.append(((_natural(_texto_de_valor(valor)),), row))
        else:
            con_sitio.append((clave, row))

    con_sitio.sort(key=lambda par: par[0], reverse=descendente)
    sin_sitio.sort(key=lambda par: par[0], reverse=descendente)
    return [row for _, row in con_sitio] + [row for _, row in sin_sitio] + vacias


def _clave_de_orden(
    valor: Any,
    tipo: str | None,
    *,
    persona: bool = False,
    people: Mapping[str, str] | None = None,
    orden: Mapping[str, int] | None = None,
    refs: Mapping[str, tuple[str, str]] | None = None,
) -> tuple | None:
    """La clave de orden de un valor, o `None` si no se puede ordenar en su tipo.

    El primer elemento de la tupla separa los tramos cuando el tipo no basta
    (por ejemplo, el número de una fecha): así un `int` nunca se compara con un
    `tuple` y el orden no revienta con un `.md` escrito a mano.
    """
    if orden:
        # Un orden declarado manda sobre el tipo: es el usuario quien ha dicho
        # en qué orden van sus valores (y quien llama sin `types` —una prueba
        # de la regla, o una base sin schema— también lo recibe).
        puesto = _puesto(valor, orden)
        return None if puesto is None else (0, puesto)
    if tipo == "number":
        numero = _numero(valor)
        return None if numero is None else (0, numero)
    if tipo == "date":
        fecha = _fecha(valor)
        return None if fecha is None else (0, fecha)
    if tipo == "checkbox":
        # `False < True`: en asc, primero las sin marcar; en desc, las marcadas.
        return (0, 1 if _marcado(valor) else 0)
    if tipo == "person" or persona:
        if isinstance(valor, list):
            nombres = [(people or {}).get(str(v).strip(), v) for v in valor if not _vacio(v)]
            return None if not nombres else (0, min(_natural(n) for n in nombres))
        nombre = (people or {}).get(str(valor).strip())
        return (0, _natural(nombre if nombre is not None else valor))
    if tipo == "relation":
        titulos = _titulos_enlazados(valor, refs or {})
        return None if not titulos else (0, min(_natural(t) for t in titulos))
    # Texto, url, título y los valores de una selección en modo `auto`. Una
    # multiselección se ordena por su valor alfabéticamente menor.
    if isinstance(valor, list):
        textos = [_texto_de_valor(v) for v in valor if not _vacio(v)]
        return None if not textos else (0, min(_natural(t) for t in textos))
    return (0, _natural(valor))


def _texto_de_valor(valor: Any) -> str:
    """El valor como texto para el orden natural (una lista, por su primero)."""
    if isinstance(valor, list):
        return " ".join(_texto_de_valor(v) for v in valor)
    return str(valor)


def _natural(valor: Any) -> tuple:
    """Clave de orden natural, sin mayúsculas ni tildes.

    Los tramos de dígitos se comparan como números: «Tarea 2» antes que
    «Tarea 10», que es lo que espera quien numera. Cada trozo va etiquetado
    (`0` texto, `1` número) para que dos tramos distintos nunca se comparen
    entre sí como tipos incompatibles.
    """
    plano = unicodedata.normalize("NFKD", str(valor))
    plano = "".join(c for c in plano if not unicodedata.combining(c)).casefold().strip()
    partes: list[tuple[int, Any]] = []
    for trozo in re.split(r"(\d+)", plano):
        if not trozo:
            continue
        partes.append((1, int(trozo)) if trozo.isdigit() else (0, trozo))
    return tuple(partes) or ((0, ""),)


def _fecha(valor: Any) -> tuple | None:
    """Una fecha ISO como clave cronológica, o `None` si no es una fecha válida.

    Un `2026-02-31` no existe y `fromisoformat` lo rechaza: esa fila no tiene
    sitio en el orden y va al final, como una vacía. Se acepta la fecha suelta
    (`2026-01-15`) y también el instante completo (`2026-01-15T10:30:00`).
    """
    texto = str(valor).strip()
    if not texto:
        return None
    try:
        momento = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (momento.toordinal(), momento.hour * 3600 + momento.minute * 60 + momento.second)


def _titulos_enlazados(
    valor: Any, refs: Mapping[str, tuple[str, str]]
) -> list[str]:
    """Los títulos de las filas que enlaza una relación, saltando las rotas.

    Una relación rota (el destino se borró) no aporta título: si no queda
    ninguno, la fila no tiene sitio y va al final «en las dos direcciones»,
    igual que el resto de valores que no se pueden ordenar.
    """
    ids = valor if isinstance(valor, list) else [valor]
    titulos: list[str] = []
    for uno in ids:
        ref = refs.get(str(uno).strip())
        if ref is not None:
            titulos.append(ref[0] or "")
    return titulos


def _puesto(value: Any, orden: Mapping[str, int]) -> int | None:
    """El puesto de un valor en el orden declarado, o `None` si no tiene sitio.

    Una multiselección se ordena por su valor **más importante** (el puesto más
    bajo): una fila con `[media, alta]` es una fila de alta, y es lo que se
    espera al mirar la columna. Si ninguno de sus valores está declarado, la
    fila no tiene sitio.
    """
    if isinstance(value, list):
        puestos = [orden[option_key(v)] for v in value if option_key(v) in orden]
        return min(puestos) if puestos else None
    return orden.get(option_key(value))


def _valor(row: Page, key: str) -> Any:
    return (row.properties or {}).get(key)


def _match(
    actual: Any,
    op: str,
    value: Any,
    *,
    persona: bool = False,
    people: Mapping[str, str] | None = None,
) -> bool:
    """Una condición del filtro sobre el valor de una fila."""
    if op == "is_empty":
        return _vacio(actual)
    if op == "is_not_empty":
        return not _vacio(actual)
    if op == "checked":
        return _marcado(actual)
    if op == "unchecked":
        return not _marcado(actual)
    if op == "eq":
        if persona:
            return _igual_persona(actual, value, people)
        return _igual(actual, value)
    if op == "neq":
        if persona:
            return not _igual_persona(actual, value, people)
        return not _igual(actual, value)
    if op == "contains":
        return _contiene(actual, value)
    if op == "includes":
        return _incluye(actual, value)
    if op in COMPARACIONES:
        return _compara(actual, op, value)
    # Un operador que este backend no conoce no puede fingir que filtra: deja
    # pasar la fila (mismo criterio que la versión anterior). La interfaz solo
    # ofrece los de arriba.
    return True


def _vacio(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def _marcado(value: Any) -> bool:
    """Una casilla marcada, venga como booleano o como el texto del `.md`."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "on", "si", "sí", "x"}


def _numero(value: Any) -> float | None:
    """El valor como número, o `None` si no es un número (fechas, texto)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    texto = str(value).strip()
    if not texto:
        return None
    try:
        return float(texto)
    except ValueError:
        return None


def _igual(actual: Any, esperado: Any) -> bool:
    # «es» con un valor vacío pregunta por «está vacío»: es lo que espera quien
    # compara una celda sin rellenar (y no un `eq` que nunca se cumple).
    if esperado is None or (isinstance(esperado, str) and not esperado.strip()):
        return _vacio(actual)
    if _vacio(actual):
        return False
    a, b = _numero(actual), _numero(esperado)
    if a is not None and b is not None:
        return a == b
    if isinstance(actual, list):
        if isinstance(esperado, list):
            return {_texto(v) for v in actual} == {_texto(v) for v in esperado}
        return any(_texto_eq(v, esperado) for v in actual)
    return _texto_eq(actual, esperado)


def _igual_persona(
    actual: Any, esperado: Any, people: Mapping[str, str] | None
) -> bool:
    """`eq` de una persona: por id (`people` resuelto) o por nombre visible.

    Un valor vacío pregunta por «está vacío», como en cualquier otro tipo: así
    «Responsable es —» sirve, y el selector del filtro puede mandar el nombre
    que el usuario lee en el chip sin saber el id.
    """
    if esperado is None or (isinstance(esperado, str) and not esperado.strip()):
        return _vacio(actual)
    if _vacio(actual):
        return False
    if isinstance(actual, list):  # varias personas: basta con que una coincida
        return any(_igual_persona(a, esperado, people) for a in actual)
    if _texto_eq(actual, esperado):
        return True
    nombre = (people or {}).get(str(actual).strip())
    return nombre is not None and _texto(nombre) == _texto(esperado)


def _texto(value: Any) -> str:
    return str(value).strip().casefold()


def _texto_eq(actual: Any, esperado: Any) -> bool:
    if isinstance(actual, str) and isinstance(esperado, str):
        return _texto(actual) == _texto(esperado)
    return actual == esperado


def _contiene(actual: Any, esperado: Any) -> bool:
    """`contiene`: subcadena en un texto, o un elemento en una lista."""
    if isinstance(actual, list):
        return any(_texto_eq(v, esperado) for v in actual)
    if _vacio(actual) or esperado is None or _texto(esperado) == "":
        return False
    return _texto(esperado) in _texto(actual)


def _incluye(actual: Any, esperado: Any) -> bool:
    """`incluye` de una relación: el id (o los ids) de la celda.

    Una relación simple guarda un id y una múltiple una lista; las dos
    responden a «incluye» con el id buscado.
    """
    if esperado is None or str(esperado).strip() == "":
        return False
    ids = actual if isinstance(actual, list) else [actual]
    return any(str(v).strip() == str(esperado).strip() for v in ids if v is not None)


def _compara(actual: Any, op: str, esperado: Any) -> bool:
    """`>`, `<`, `≥`, `≤`, `antes de`, `después de`.

    Un valor vacío no cumple ninguna comparación (no hay nada que comparar), y
    los dos lados se comparan como número si lo son y como texto si no — una
    fecha ISO `2026-11-11` comparada como texto da el orden correcto.
    """
    if _vacio(actual) or esperado is None or (isinstance(esperado, str) and not esperado.strip()):
        return False
    a, b = _numero(actual), _numero(esperado)
    if a is None or b is None:
        a, b = _texto(actual), _texto(esperado)
    if op in ("gt", "after"):
        return a > b
    if op == "gte":
        return a >= b
    if op in ("lt", "before"):
        return a < b
    return a <= b
