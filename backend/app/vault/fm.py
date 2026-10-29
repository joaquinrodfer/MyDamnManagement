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
  anchors `&`/`*`, flow maps `{}`) no se rompen: la clave se marca `opaque` y se
  preserva tal cual. Si es una clave gestionada, se avisa (no se reescribe).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

KEY_RE = re.compile(r"""^([A-Za-z0-9_.\-]+|"[^"]*"|'[^']*'):(.*)$""")
OPAQUE_START = set("|>&*!{")

# Escalares que, sin comillas, YAML interpretaría como algo que no es el texto
# literal. Se citan al escribir para que un round-trip no cambie el significado.
_SPECIAL_SCALARS = {
    "true", "false", "yes", "no", "on", "off", "null", "~",
    "True", "False", "Yes", "No", "On", "Off", "Null", "NULL",
}
_SAFE_SCALAR = re.compile(r"^[A-Za-z0-9_./@+$%\-][A-Za-z0-9_:./@+$%\-]*$")


class FrontmatterError(ValueError):
    pass


@dataclass
class Entry:
    key: str | None
    start: int           # índice de línea (0-based) dentro del raw
    end: int             # exclusivo
    kind: str = "pair"   # pair | blank | junk
    value_raw: str = ""
    opaque: bool = False
    value: object = None


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


def _scalar(tok: str) -> str:
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        try:
            return json.loads(tok)
        except json.JSONDecodeError:
            return tok[1:-1]
    if len(tok) >= 2 and tok[0] == "'" and tok[-1] == "'":
        return tok[1:-1].replace("''", "'")
    # comentario al final de un escalar sin comillas: "valor  # nota"
    if " #" in tok:
        tok = tok.split(" #", 1)[0].rstrip()
    return tok


def _flow_list(tok: str) -> list[str]:
    inner = tok.strip()[1:-1].strip()
    if not inner:
        return []
    out, buf, quote = [], [], None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            out.append(_scalar("".join(buf)))
            buf = []
        else:
            buf.append(ch)
    out.append(_scalar("".join(buf)))
    return [v for v in out if v != ""]


def _content_lines(lines: list[str]) -> list[tuple[int, str]]:
    out = []
    for line in lines:
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            continue
        out.append((_indent(line), line.strip()))
    return out


def _parse_node(items: list[tuple[int, str]], i: int, indent: int):
    """Parsea un nodo (mapa o secuencia) que empieza en items[i] con `indent`."""
    if i < len(items) and items[i][1].startswith("- "):
        seq = []
        while i < len(items) and items[i][0] == indent and items[i][1].startswith("- "):
            body = items[i][1][2:].strip()
            i += 1
            m = KEY_RE.match(body)
            if m:
                item = {_scalar(m.group(1)): _parse_inline(m.group(2))}
                # claves hermanas del mismo item, más indentadas
                while i < len(items) and items[i][0] > indent:
                    sub_indent = items[i][0]
                    sub, i = _parse_node(items, i, sub_indent)
                    if isinstance(sub, dict):
                        item.update(sub)
                    else:  # lista anidada dentro de un item: no soportada
                        raise FrontmatterError("lista anidada en item de secuencia")
            else:
                item = _parse_inline(body)
            seq.append(item)
        return seq, i

    mapping = {}
    while i < len(items) and items[i][0] == indent:
        m = KEY_RE.match(items[i][1])
        if not m:
            raise FrontmatterError(f"línea no reconocida: {items[i][1]!r}")
        key = _scalar(m.group(1))
        raw_val = m.group(2).strip()
        i += 1
        if raw_val:
            mapping[key] = _parse_inline(raw_val)
            # una clave con valor inline no puede tener hijos
            while i < len(items) and items[i][0] > indent:
                i += 1
        else:
            if i < len(items) and items[i][0] > indent:
                child_indent = items[i][0]
                value, i = _parse_node(items, i, child_indent)
                mapping[key] = value
            else:
                mapping[key] = ""
    return mapping, i


def _parse_inline(raw: str):
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in OPAQUE_START:
        raise FrontmatterError(f"construcción YAML no soportada: {raw[:24]!r}")
    if raw.startswith("[") and raw.endswith("]"):
        return _flow_list(raw)
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
                    block = _content_lines(lines[start + 1:i])
                    entry.value = _parse_node(block, 0, block[0][0])[0] if block else ""
            except (FrontmatterError, IndexError) as exc:
                entry.opaque = True
                entry.value = None
                entry.error = str(exc)  # type: ignore[attr-defined]
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
            # `{}` es un flow map y el parser lo marca opaco (`{` abre una
            # construcción no soportada), así que la clave se perdería al
            # releer. Quien escribe ya omite los mapas vacíos (una vista de
            # tabla sin `config` no tiene nada que guardar).
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
        if entry.key in delete or (entry.key in updates and updates[entry.key] is None):
            seen.add(entry.key)
            continue
        if entry.key not in updates:
            out.extend(lines[entry.start:entry.end])
            continue
        seen.add(entry.key)
        if entry.opaque:
            warnings.append(
                f"clave '{entry.key}' tiene YAML no soportado: se preserva sin tocar"
            )
            out.extend(lines[entry.start:entry.end])
            continue
        # conserva el comentario de la línea si era un escalar simple
        if entry.end - entry.start == 1 and " #" in lines[entry.start] \
                and not isinstance(updates[entry.key], (list, dict)):
            comment = " #" + lines[entry.start].split(" #", 1)[1]
            out.append(dump_value(entry.key, updates[entry.key])[0] + comment)
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
