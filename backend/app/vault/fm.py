"""Parser y escritor de frontmatter YAML *del subconjunto que usa el vault*.

Motivo de no usar PyYAML en este spike: no está en backend/requirements.txt y el
spike trabaja sin red. Pero además es una decisión de diseño que se argumenta en
HALLAZGOS.md: PyYAML no hace round-trip (destruye comentarios y estilo) y
`yaml.safe_load` convierte escalares sueltos a bool/número/fecha ("Norway
problem"), lo que rompe `title: No` o `empresa: 3M`.

Diseño:
- El parser devuelve SOLO cadenas, listas y diccionarios. Nunca infiere tipos.
  La conversión (number, checkbox, date...) la hace la capa de modelo a partir
  del `schema` de la base, que es la única fuente de verdad del tipo.
- Cada clave de primer nivel es una `Entry` con su rango de líneas. El escritor
  sustituye SOLO el rango de las claves que gestiona la app y deja el resto
  byte a byte igual: claves desconocidas, comentarios, orden y estilo sobreviven.
- Las construcciones que este subconjunto no entiende (block scalars `|`/`>`,
  anchors `&`/`*`, tags `!`) no se rompen: la clave se marca `opaque` y se
  preserva tal cual. Si es una clave gestionada, se avisa (no se reescribe).
  Los flow maps `{ clave: valor }` de escalares sí se leen donde los usa un
  `.md` escrito a mano: dentro de un bloque (los `widths` de una vista) y en
  una lista de mapas (`schema: [{key: …, type: …}]`). En el primer nivel un
  `clave: {…}` sigue siendo una propiedad suelta que el índice no representa,
  y se preserva sin interpretar. Dentro de un flow, un trozo que no abre
  `{}`/`[]` es texto (`etiquetas: [*importante, otro]`): no hay alias que
  resolver y una etiqueta no puede vaciar la clave de `data`.
- Las colecciones en flow tienen **tope de anidamiento** (`MAX_FLOW_DEPTH`) y
  las claves de bloque también (`MAX_BLOCK_DEPTH`). El subconjunto se lee con
  recursión, así que sin tope la profundidad de un `.md` fabricado es la de la
  pila: `RecursionError` no lo capturaba nadie (`parse` atrapa
  `FrontmatterError`/`IndexError`), `POST /reindex` devolvía 500 y el índice de
  arranque no se construía (`/health` en `error` y la base sin vistas hasta
  editar el fichero a mano). Al pasarse el tope, la entrada queda `opaque` con
  `FrontmatterError` —el `.md` no se toca y `views` lo cuenta con fichero y
  línea—, igual que con un `widths: |`.
- Una comilla solo abre escalar donde YAML la admitiría: al principio de un
  trozo o justo detrás de lo que abre un valor (el `:` de la clave, o el
  `[`/`{`/`,` de un flow anidado, o sea `_ABRE_ESCALAR`). Si no, es texto: un
  apóstrofo dentro de un valor sin comillas no puede desactivar la partición
  por comas (`{texto: O'Brien, fase: lead}`) ni juntar los elementos de una
  lista (`[it's, fine]`). Una comilla que abre y no cierra es un
  `FrontmatterError` —el flujo no se puede partir con seguridad— en vez de
  devolver el resto del mapa como si fuera un valor.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

KEY_RE = re.compile(r"""^([A-Za-z0-9_.\-]+|"[^"]*"|'[^']*'):(.*)$""")
# Primer carácter de un valor de PRIMER nivel que no se interpreta: la entrada
# se marca `opaque` y se preserva tal cual. Un `{` aquí es un mapa en una clave
# suelta —una propiedad de la fila—, que el índice no tiene dónde poner; dentro
# de un bloque sí se leen los flow maps (ver `_parse_inline`), que es donde
# viven los `widths`/`filters` de una vista.
OPAQUE_START = set("|>&*!{")
# Dentro de un bloque (el valor de una clave anidada o un item de una lista) el
# flow map sí se lee: solo siguen siendo opacas las construcciones de YAML que
# este subconjunto no sabe representar.
OPAQUE_INLINE = set("|>&*!")

# Tope de anidamiento de las colecciones en flow (`{a: {b: [c]}}`). Un `.md`
# real no pasa de dos o tres niveles; sin tope, la profundidad era la de la
# pila y un fichero fabricado tumbaba el índice entero (ver el docstring del
# módulo). Es un tope de *lectura*, no de formato: al pasarse, la entrada queda
# `opaque` con aviso y el `.md` no se toca.
MAX_FLOW_DEPTH = 8
# Lo mismo para las claves de bloque encadenadas (`a:` → `b:` → `c:`…). Nadie
# escribe eso en un `.md`, pero `_parse_node` también es recursivo y con 2.000
# niveles agotaba la pila igual que un flow: el tope es holgado porque solo
# evita la pila, no describe el formato.
MAX_BLOCK_DEPTH = 64

# Caracteres tras los que una comilla puede abrir un escalar entrecomillado:
# principio del trozo (vacío), el `:` de la clave y la apertura/coma de un flow
# anidado. Detrás de cualquier otra cosa —`O'Brien`— la comilla es texto.
_ABRE_ESCALAR = ":[{,"

# Escalares que, sin comillas, YAML interpretaría como algo que no es el texto
# literal. Se citan al escribir para que un round-trip no cambie el significado.
_SPECIAL_SCALARS = {
    "true", "false", "yes", "no", "on", "off", "null", "~",
    "True", "False", "Yes", "No", "On", "Off", "Null", "NULL",
}
_SAFE_SCALAR = re.compile(r"^[A-Za-z0-9_./@+$%\-][A-Za-z0-9_:./@+$%\-]*$")


class FrontmatterError(ValueError):
    """Lo que el subconjunto no sabe leer.

    `linea` es el índice (0-based) dentro del frontmatter donde está la
    construcción que falló, cuando se conoce: es lo que convierte «esta clave
    no se ha podido leer» en algo que el dueño puede ir a corregir.
    """

    def __init__(self, mensaje: str, linea: int | None = None):
        super().__init__(mensaje)
        self.linea = linea


class _FlowTooDeep(FrontmatterError):
    """El flow anida más de `MAX_FLOW_DEPTH` niveles: no se sigue leyendo.

    Es un `FrontmatterError` con nombre propio porque `_flow_item` cae a texto
    ante cualquier fallo del trozo —así una etiqueta rara (`*importante`) no se
    lleva la clave de `data`—, pero esto no es un trozo raro: es una
    construcción que no se va a leer, y callarla dejaría el mapa a medias sin
    aviso. Se relanza para que la entrada quede `opaque` y `views` lo cuente
    con el fichero y la línea.
    """


@dataclass
class Entry:
    key: str | None
    start: int           # índice de línea (0-based) dentro del raw
    end: int             # exclusivo
    kind: str = "pair"   # pair | blank | junk
    value_raw: str = ""
    opaque: bool = False
    value: object = None
    error: str = ""       # por qué quedó `opaque`, si fue al interpretarla
    error_line: int = -1  # línea (índice en el raw) donde falló, si se sabe


@dataclass
class Frontmatter:
    raw: str
    entries: list[Entry] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    @property
    def lines(self) -> list[str]:
        return self.raw.split("\n")


# --------------------------------------------------------------------------
# lectura
# --------------------------------------------------------------------------

def split_document(text: str) -> tuple[str | None, str]:
    """Devuelve (raw_frontmatter, cuerpo). Si no hay frontmatter, (None, texto)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    raise FrontmatterError("frontmatter abierto (---) sin cerrar")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar_end(text: str) -> int:
    """Índice donde termina el escalar entrecomillado con el que empieza `text`.

    Devuelve 0 si el escalar no va entrecomillado (o la comilla no se cierra):
    en ese caso no hay nada que proteger y el `#` es comentario en cuanto
    aparece.
    """
    tok = text.lstrip()
    offset = len(text) - len(tok)          # espacios de delante: el índice es del texto
    if not tok or tok[0] not in "\"'":
        return 0
    quote = tok[0]
    i = 1
    while i < len(tok):
        ch = tok[i]
        if quote == '"' and ch == "\\":      # `\"` no cierra la comilla
            i += 2
            continue
        if ch == quote:
            if quote == "'" and tok[i + 1:i + 2] == "'":   # `''` escapa la simple
                i += 2
                continue
            return offset + i + 1
        i += 1
    return 0                                  # comilla sin cerrar: no se parte


def _split_comment(text: str) -> tuple[str, str | None]:
    """Parte `valor # comentario`: (valor, lo que va tras la `#`, o None).

    Un `#` solo abre comentario de cola si va precedido de espacio **y queda
    fuera del escalar**. El escritor (`dump_scalar`) entrecomilla justo los
    valores que llevan ` #`, así que mirar la línea entera —como se hacía—
    convertía `title: "Pedido #3"` en `Pedido` al reescribirlo, y el trozo
    perdido se acumulaba en cada escritura.
    """
    for i in range(max(1, _scalar_end(text)), len(text)):
        if text[i] == "#" and text[i - 1] in " \t":
            return text[:i], text[i + 1:]
    return text, None


def _scalar(tok: str) -> str:
    tok, _comment = _split_comment(tok.strip())
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        try:
            return json.loads(tok)
        except json.JSONDecodeError:
            return tok[1:-1]
    if len(tok) >= 2 and tok[0] == "'" and tok[-1] == "'":
        return tok[1:-1].replace("''", "'")
    return tok


def _abre_escalar(previo: str | None) -> bool:
    """¿Una comilla aquí puede abrir un escalar entrecomillado?

    `previo` es el último carácter **no-espacio** del trozo que se está
    partiendo (`None` si aún no hay ninguno con contenido). Solo al principio
    del trozo o justo detrás de lo que abre un valor: el `:` de la clave, o el
    `[`/`{`/`,` de un flow anidado. Cualquier otra comilla está dentro de un
    texto sin comillas (`O'Brien`) y es un carácter más.

    Antes recibía el `buf` del trozo y hacía `"".join(buf).strip()` por cada
    comilla: el trozo entero se reconstruía una vez por comilla, así que una
    línea con N comillas costaba O(N × longitud del trozo). Un `widths` de
    78 KB con 40.000 comillas tardaba ~9 s y se llevaba el reindex por delante.
    Ahora el último carácter no-espacio lo lleva quien recorre el trozo y
    consultarlo es O(1).
    """
    return previo is None or previo in _ABRE_ESCALAR


def _flow_split(inner: str) -> list[str]:
    """Parte el interior de un flow (`a, b`) por comas de **primer nivel**.

    Las comas de dentro de unas comillas o de un `[]`/`{}` anidado no parten:
    `[{a: 1}, {b: 2}]` son dos mapas, no cuatro trozos.

    Una comilla solo abre escalar donde la admitiría YAML (`_abre_escalar`).
    Antes bastaba con que apareciera, así que un apóstrofo dentro de un valor
    sin comillas desactivaba la partición por comas y se comía el resto del
    mapa **sin aviso**: `{texto: O'Brien, fase: lead}` se leía como un solo
    par (`fase` desaparecía de la vista) y en una lista `[it's, fine]` era un
    único elemento. Un escalar que abre comilla y no la cierra tampoco se
    calla: sin saber dónde acaba el valor, el resto del mapa se devolvería como
    si fuera parte de él, así que se avisa con `FrontmatterError`.
    """
    partes: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    depth = 0
    # Último carácter no-espacio del trozo en curso (`None` = todavía ninguno).
    # Se lleva al vuelo porque es lo único que `_abre_escalar` necesita, y
    # reconstruirlo desde `buf` en cada comilla era lo que hacía el coste
    # cuadrático (ver `_abre_escalar`).
    previo: str | None = None
    i = 0
    while i < len(inner):
        ch = inner[i]
        if quote:
            if ch == quote:
                if quote == "'" and inner[i + 1:i + 2] == "'":
                    buf.append("''")          # `''` escapa la comilla simple
                    previo = "'"
                    i += 2
                    continue
                quote = None
        elif ch in "\"'" and _abre_escalar(previo):
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            partes.append("".join(buf))
            buf = []
            previo = None
            i += 1
            continue
        buf.append(ch)
        if not ch.isspace():
            previo = ch
        i += 1
    if quote:
        raise FrontmatterError(
            f"comilla {quote} sin cerrar en un flow: {''.join(buf)[:24]!r}"
        )
    partes.append("".join(buf))
    return partes


def _flow_item(trozo: str, depth: int = 0):
    """Un valor dentro de un flow (`[a, b]`, `{a: b}`) leído como lo que es.

    Solo `{`/`[` abren una colección —lo único que este subconjunto
    representa—; todo lo demás es un escalar. Una lista en flow se parte por
    comas, así que un trozo como `*importante`, `!urgente` o `{borrador}` es
    **texto**: en el bloque de un `.md` escrito a mano eso son etiquetas, y
    pasar cada trozo por `_parse_inline` lo hacía reventar, con lo que la
    entrada entera quedaba `opaque` y la clave desaparecía de `data` sin
    aviso (`etiquetas: [*importante, otro]` se leía como texto en la lista de
    siempre). Si el trozo abre `{`/`[` pero no es una colección válida, también
    cae a texto: no se pierde nada del índice por no entender un trozo.
    """
    trozo = trozo.strip()
    if trozo.startswith(("{", "[")):
        try:
            return _parse_inline(trozo, depth)
        except _FlowTooDeep:
            # Un trozo que no se entiende es texto, pero un anidamiento fuera
            # de tope no: callarlo dejaría el mapa medio leído y sin aviso.
            raise
        except FrontmatterError:
            pass
    return _scalar(trozo)


def _flow_list(tok: str, depth: int = 0) -> list:
    if depth >= MAX_FLOW_DEPTH:
        raise _FlowTooDeep(
            f"flow anidado a más de {MAX_FLOW_DEPTH} niveles: "
            f"{tok.strip()[:24]!r}"
        )
    inner = tok.strip()[1:-1].strip()
    if not inner:
        return []
    out = [_flow_item(parte, depth + 1) for parte in _flow_split(inner)]
    return [v for v in out if v != ""]


def _flow_map(tok: str, depth: int = 0) -> dict:
    """`{ clave: valor, … }`: el mapa de escalares de una línea.

    Es la forma en la que un `.md` escrito a mano escribe los `widths` de una
    vista (`widths: { fase: 220, valor: 150 }`), el mismo dato que la app baja
    en bloque. Se lee en vez de marcar la entrada `opaque` —si se marca, la
    clave desaparece de `data` y la base se queda sin vistas sin un aviso—.

    Los valores pasan por `_flow_item`, así que un anidado (`{a: {b: 1}}`) o
    una lista (`{a: [1, 2]}`) también se leen y un valor que no es colección
    (`{nota: *importante}`) se queda como texto. Solo revienta con
    `FrontmatterError` lo que rompe la estructura del mapa —sin `}`, o una
    parte sin `clave:`—, y entonces la entrada queda `opaque` como cualquier
    otra construcción no soportada.
    """
    if depth >= MAX_FLOW_DEPTH:
        raise _FlowTooDeep(
            f"flow anidado a más de {MAX_FLOW_DEPTH} niveles: "
            f"{tok.strip()[:24]!r}"
        )
    raw = tok.strip()
    if not raw.endswith("}"):
        # `widths: { fase: 220 }  # el ancho de la app no es parte del mapa: se
        # quita el comentario de cola antes de exigir el cierre. Solo se parte
        # cuando el valor no acaba ya en `}`, así que un `#` entre comillas
        # (`{ "a": "x #y" }`) no se toca.
        sin_comentario, comentario = _split_comment(raw)
        if comentario is None or not sin_comentario.strip().endswith("}"):
            raise FrontmatterError(f"flow map sin cerrar: {raw[:24]!r}")
        raw = sin_comentario.strip()
    inner = raw[1:-1].strip()
    if not inner:
        return {}
    out: dict = {}
    for parte in _flow_split(inner):
        trozo = parte.strip()
        if not trozo:
            continue
        m = KEY_RE.match(trozo)
        if not m:
            raise FrontmatterError(f"flow map sin clave: {trozo!r}")
        out[_scalar(m.group(1))] = _flow_item(m.group(2), depth + 1)
    return out


def _content_lines(lines: list[str], first: int = 0) -> list[tuple[int, str, int]]:
    """`(sangría, texto, línea)` de cada línea con contenido.

    `first` es el índice de `lines[0]` dentro del frontmatter: el tercer valor
    es la línea real (del raw), la que se cita cuando una construcción no se
    entiende.
    """
    out = []
    for offset, line in enumerate(lines):
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            continue
        out.append((_indent(line), line.strip(), first + offset))
    return out


def _inline_en(linea: int, raw: str):
    """`_parse_inline` que apunta en qué línea del frontmatter falló."""
    try:
        return _parse_inline(raw)
    except FrontmatterError as exc:
        if exc.linea is None:
            raise FrontmatterError(str(exc), linea) from None
        raise


def _parse_node(
    items: list[tuple[int, str, int]], i: int, indent: int, depth: int = 0
):
    """Parsea un nodo (mapa o secuencia) que empieza en items[i] con `indent`.

    `depth` es el nivel de anidamiento por indentación: con 2.000 claves
    encadenadas (`a:` → `b:` → `c:`…) la recursión agotaba la pila igual que
    un flow, así que se corta en `MAX_BLOCK_DEPTH` con aviso.
    """
    if depth > MAX_BLOCK_DEPTH:
        donde = items[i][1][:24] if i < len(items) else ""
        raise FrontmatterError(
            f"claves encadenadas a más de {MAX_BLOCK_DEPTH} niveles: {donde!r}"
        )
    if i < len(items) and items[i][1].startswith("- "):
        seq = []
        while i < len(items) and items[i][0] == indent and items[i][1].startswith("- "):
            body = items[i][1][2:].strip()
            linea = items[i][2]
            i += 1
            m = KEY_RE.match(body)
            if m:
                item = {_scalar(m.group(1)): _inline_en(linea, m.group(2))}
                # claves hermanas del mismo item, más indentadas
                while i < len(items) and items[i][0] > indent:
                    sub_indent = items[i][0]
                    sub, i = _parse_node(items, i, sub_indent, depth + 1)
                    if isinstance(sub, dict):
                        item.update(sub)
                    else:  # lista anidada dentro de un item: no soportada
                        raise FrontmatterError("lista anidada en item de secuencia")
            else:
                item = _inline_en(linea, body)
            seq.append(item)
        return seq, i

    mapping = {}
    while i < len(items) and items[i][0] == indent:
        m = KEY_RE.match(items[i][1])
        if not m:
            raise FrontmatterError(f"línea no reconocida: {items[i][1]!r}")
        key = _scalar(m.group(1))
        raw_val = m.group(2).strip()
        linea = items[i][2]
        i += 1
        if raw_val:
            mapping[key] = _inline_en(linea, raw_val)
            # una clave con valor inline no puede tener hijos
            while i < len(items) and items[i][0] > indent:
                i += 1
        else:
            if i < len(items) and items[i][0] > indent:
                child_indent = items[i][0]
                value, i = _parse_node(items, i, child_indent, depth + 1)
                mapping[key] = value
            else:
                mapping[key] = ""
    return mapping, i


def _parse_inline(raw: str, depth: int = 0):
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in OPAQUE_INLINE:
        raise FrontmatterError(f"construcción YAML no soportada: {raw[:24]!r}")
    if raw.startswith("{"):
        return _flow_map(raw, depth)
    if raw.startswith("[") and raw.endswith("]"):
        return _flow_list(raw, depth)
    return _scalar(raw)


def parse(raw: str) -> Frontmatter:
    if not raw.strip():
        return Frontmatter(raw, [], {})
    lines = raw.split("\n")
    entries: list[Entry] = []
    data: dict = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            entries.append(Entry(None, i, i + 1, "blank"))
            i += 1
            continue
        m = KEY_RE.match(line) if _indent(line) == 0 else None
        if not m:
            entries.append(Entry(None, i, i + 1, "junk"))
            i += 1
            continue
        key = _scalar(m.group(1))
        start = i
        i += 1
        while i < len(lines):
            nxt = lines[i]
            if nxt.strip() and _indent(nxt) == 0:
                break
            i += 1
        entry = Entry(key, start, i, "pair", value_raw=m.group(2).strip())
        if entry.value_raw and entry.value_raw[0] in OPAQUE_START:
            entry.opaque = True
        else:
            try:
                if entry.value_raw:
                    entry.value = _parse_inline(entry.value_raw)
                else:
                    block = _content_lines(lines[start + 1:i], start + 1)
                    entry.value = _parse_node(block, 0, block[0][0])[0] if block else ""
            except (FrontmatterError, IndexError) as exc:
                entry.opaque = True
                entry.value = None
                entry.error = str(exc)
                linea = getattr(exc, "linea", None)
                entry.error_line = -1 if linea is None else linea
        entries.append(entry)
        if not entry.opaque:
            data[key] = entry.value
    return Frontmatter(raw, entries, data)


# --------------------------------------------------------------------------
# escritura de escalares
# --------------------------------------------------------------------------

def needs_quotes(value: str) -> bool:
    if value == "":
        return True
    if value in _SPECIAL_SCALARS:
        return True
    if value != value.strip():
        return True
    if value[0] in "-?:,[]{}#&*!|>'\"%@`":
        return True
    if ": " in value or " #" in value or value.endswith(":"):
        return True
    if value.lower().startswith(("true", "false", "null")) and value not in _SPECIAL_SCALARS:
        return False
    return not _SAFE_SCALAR.match(value)


def dump_scalar(value: str) -> str:
    if isinstance(value, str) and needs_quotes(value):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _dump_inline_list(values: list) -> str:
    return "[" + ", ".join(dump_scalar(str(v)) for v in values) + "]"


def _dump_map_item(item: dict, indent: int) -> list[str]:
    """Un `dict` como item de secuencia: `- clave: valor` y el resto debajo.

    Las líneas de continuación van a `indent + 2`, alineadas con la clave del
    primer item (`"- "` ocupa lo mismo que dos espacios), que es lo que espera
    el parser al leer las claves hermanas de un item de secuencia.
    """
    pad = " " * indent
    lines: list[str] = []
    first = True
    for key, value in item.items():
        block = dump_value(key, value, indent + 2)
        if first:
            lines.append(f"{pad}- " + block[0].lstrip(" "))
            lines.extend(block[1:])
            first = False
        else:
            lines.extend(block)
    return lines


def dump_value(key: str, value, indent: int = 0) -> list[str]:
    """Serializa una clave gestionada a líneas YAML.

    Cubre lo que pide el contrato del vault y el parser entiende: escalares,
    listas de escalares (en línea), listas de mapas (`schema`, `views`, los
    `filters` y el `sort` de una vista) y mapas anidados. Hasta P2 solo bajaba
    dos niveles, y un `dict` dentro de un item de secuencia se escribía como su
    `repr` de Python: ilegible a mano y perdido al releer.
    """
    pad = " " * indent
    if isinstance(value, bool):
        return [f"{pad}{key}: {'true' if value else 'false'}"]
    if isinstance(value, float) and value.is_integer():
        return [f"{pad}{key}: {int(value)}"]
    if isinstance(value, (int, float)):
        return [f"{pad}{key}: {value}"]
    if value is None:
        return [f"{pad}{key}: null"]
    if isinstance(value, list):
        if not value:
            return [f"{pad}{key}: []"]
        if all(not isinstance(v, (dict, list)) for v in value):
            return [f"{pad}{key}: {_dump_inline_list(value)}"]
        out = [f"{pad}{key}:"]
        for item in value:
            if isinstance(item, dict):
                out.extend(_dump_map_item(item, indent + 2))
            else:
                out.append(f"{pad}  - {dump_scalar(str(item))}")
        return out
    if isinstance(value, dict):
        if not value:
            # Un mapa vacío solo se puede escribir en flow (`{}`): en bloque
            # no hay nada que poner. Dentro de un bloque el parser lo vuelve a
            # leer como `{}`; en el primer nivel se preservaría sin interpretar
            # (es una propiedad suelta), así que quien escribe ya omite los
            # mapas vacíos: una vista sin `config` no tiene nada que guardar.
            return [f"{pad}{key}: {{}}"]
        out = [f"{pad}{key}:"]
        for k, v in value.items():
            out.extend(dump_value(k, v, indent + 2))
        return out
    return [f"{pad}{key}: {dump_scalar(str(value))}"]


def update(raw: str, updates: dict, delete: set[str] | None = None
           ) -> tuple[str, list[str]]:
    """Reescribe solo las claves de `updates`. Devuelve (raw_nuevo, avisos).

    `None` como valor en `updates` significa "borrar la clave".
    """
    delete = delete or set()
    fm = parse(raw)
    lines = fm.lines
    out: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for entry in fm.entries:
        if entry.key is None:
            out.extend(lines[entry.start:entry.end])
            continue
        # Una entrada `opaque` (block scalar `|`/`>`, flow map `{}`, ancla, tag)
        # no se interpretó, así que el escritor no sabe qué valor representa:
        # se preserva tal cual **aunque el llamante pida borrarla**. Si no, un
        # `None` se la llevaría por delante sin aviso (B2, ronda 1), que es justo
        # la pérdida que el módulo promete no hacer.
        if entry.opaque:
            seen.add(entry.key)
            if entry.key in delete or entry.key in updates:
                warnings.append(
                    f"clave '{entry.key}' tiene YAML no soportado: se preserva sin tocar"
                )
            out.extend(lines[entry.start:entry.end])
            continue
        if entry.key in delete or (entry.key in updates and updates[entry.key] is None):
            seen.add(entry.key)
            continue
        if entry.key not in updates:
            out.extend(lines[entry.start:entry.end])
            continue
        seen.add(entry.key)
        # Conserva el comentario de cola de un escalar simple, pero solo si la
        # `#` está **fuera** del valor: `title: "Pedido #3"` no tiene comentario
        # y `fase: lead # nota` sí. Se mira solo la parte de la línea que hay
        # tras la clave, para no confundir una `#` de la propia clave.
        comment = None
        # El escalar está en la primera línea de la entrada: las siguientes, si
        # hay alguna, están en blanco (el `raw` puede acabar en salto de línea).
        if all(not ln.strip() for ln in lines[entry.start + 1:entry.end]) \
                and not isinstance(updates[entry.key], (list, dict)):
            m = KEY_RE.match(lines[entry.start])
            _valor, comment = _split_comment(
                m.group(2) if m else lines[entry.start])
        if comment is not None:
            out.append(dump_value(entry.key, updates[entry.key])[0] + " #" + comment)
        else:
            out.extend(dump_value(entry.key, updates[entry.key]))

    for key, value in updates.items():
        if key in seen or value is None:
            continue
        if not out or out[-1].strip() != "":
            pass
        out.extend(dump_value(key, value))

    while out and out[-1].strip() == "":
        out.pop()
    return "\n".join(out), warnings


def compose(new_raw: str, body: str) -> str:
    return f"---\n{new_raw}\n---\n{body}"


def rewrite(text: str, updates: dict, body: str | None = None) -> tuple[str, list[str]]:
    try:
        raw, old_body = split_document(text)
        extra: list[str] = []
    except FrontmatterError as exc:
        # No se puede tirar el fichero del usuario ni perder texto: el bloque
        # roto pasa a ser cuerpo y se le pone delante un frontmatter válido.
        raw, old_body = None, text
        extra = [f"frontmatter roto ({exc}): se conserva como cuerpo y se escribe uno nuevo"]
    new_raw, warnings = update(raw or "", updates)
    return compose(new_raw, old_body if body is None else body), extra + warnings
