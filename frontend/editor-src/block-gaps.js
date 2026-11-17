// Las líneas en blanco ENTRE bloques: separadores y bloques vacíos.
//
// En el `.md` dos bloques van separados por UNA línea en blanco. Esa línea es
// un SEPARADOR: no es de nadie, no se escribe en ella y el cursor no se para
// ahí -- igual que en Notion, donde entre dos bloques hay aire y no una línea
// donde caer. Lo que sí es un sitio donde escribir es un BLOQUE VACÍO: el
// párrafo que abre ⏎ (o el «+» del asa) y que todavía no tiene texto. En el
// `.md` es una línea en blanco más, con su separador a cada lado:
//
//     # Título          <- bloque
//                       <- separador (no se pisa; mide el aire de la regla)
//                       <- bloque vacío (el cursor; mide una línea entera)
//                       <- separador
//     Texto de abajo.   <- bloque
//
// Escribir en el bloque vacío no mueve nada: el texto cae en esa misma línea y
// el `.md` queda `# Título\n\nx\n\nTexto de abajo.`, la forma de siempre. Así
// el bloque nuevo no necesita materializar nada al recibir la primera letra
// (lo que hacía t_c7756962 y le daba los saltos raros: el cursor estaba en el
// separador y bajaba al escribir).
//
// Qué papel tiene cada línea sale de su POSICIÓN dentro de la tirada de líneas
// en blanco: detrás de un bloque se alternan separador, vacío, separador...; al
// principio de la nota (sin bloque encima) se empieza por un vacío. Solo
// cuentan las líneas en blanco que no son de ningún bloque: dentro de una valla
// de código, o entre dos párrafos de un mismo ítem de lista, una línea en
// blanco es contenido y se pisa como cualquier otra.
//
// Lógica pura sobre un `Text` de CodeMirror y los bloques de `computeBlocks`;
// las pruebas están en test/block-gaps.test.js.

/** Índice del último bloque que empieza en `pos` o antes (búsqueda binaria). */
function lastBlockStartingAtOrBefore(blocks, pos) {
  let lo = 0;
  let hi = blocks.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (blocks[mid].from <= pos) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

/** ¿`pos` cae dentro de algún bloque? */
export function insideBlock(blocks, pos) {
  const i = lastBlockStartingAtOrBefore(blocks, pos);
  return i >= 0 && pos <= blocks[i].to;
}

/** ¿La línea `n` es una línea en blanco entre bloques (no de un bloque)? */
export function isGapLine(doc, blocks, n) {
  if (n < 1 || n > doc.lines) return false;
  const line = doc.line(n);
  if (line.length !== 0) return false;
  return !insideBlock(blocks, line.from);
}

/**
 * Papel de la línea `n`: `"sep"` (separador), `"empty"` (bloque vacío) o
 * `null` si no es una línea en blanco entre bloques.
 */
export function gapRole(doc, blocks, n) {
  if (!isGapLine(doc, blocks, n)) return null;
  let k = 0;
  let m = n - 1;
  while (m >= 1 && isGapLine(doc, blocks, m)) {
    k++;
    m--;
  }
  const even = k % 2 === 0;
  if (m < 1) return even ? "empty" : "sep"; // tirada al principio de la nota
  return even ? "sep" : "empty";
}

/** ¿La posición `pos` está en un separador? */
export function onSeparator(doc, blocks, pos) {
  return gapRole(doc, blocks, doc.lineAt(pos).number) === "sep";
}

/**
 * Dónde se queda un cursor que ha caído en un separador: el principio de la
 * línea de debajo si iba hacia delante, el final de la de arriba si iba hacia
 * atrás. Un separador al final de la nota no tiene línea de debajo: se vuelve
 * arriba. `pos` que no está en un separador se devuelve tal cual.
 */
export function leaveSeparator(doc, blocks, pos, forward) {
  const line = doc.lineAt(pos);
  if (gapRole(doc, blocks, line.number) !== "sep") return pos;
  if (forward && line.number < doc.lines) return doc.line(line.number + 1).from;
  if (line.number > 1) return doc.line(line.number - 1).to;
  return line.number < doc.lines ? doc.line(line.number + 1).from : pos;
}

/**
 * La línea más cercana a `n` en la dirección `dir` (±1) que no es un
 * separador, o `null` si no hay ninguna. Es el paso de ↑/↓: de bloque a bloque
 * (o a un bloque vacío), sin pararse en el aire de en medio.
 */
export function nextLineSkippingSeparators(doc, blocks, n, dir) {
  for (let m = n + dir; m >= 1 && m <= doc.lines; m += dir) {
    if (gapRole(doc, blocks, m) !== "sep") return m;
  }
  return null;
}

// ------------------------------------------------------------ ⌫ y Supr

const TEXT_TYPES = new Set(["paragraph", "h1", "h2", "h3", "h4", "h5", "bullet_list_item", "ordered_list_item", "quote"]);
const HEADING_TYPES = new Set(["h1", "h2", "h3", "h4", "h5"]);
const HEADING_MARK_RE = /^[ \t]*#{1,6}(?:[ \t]+|$)/;
const LIST_MARK_RE = /^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?/;
const QUOTE_MARK_RE = /^[ \t]*>[ \t]?/;

function blockStartingAt(blocks, pos) {
  return blocks.find((b) => b.from === pos) || null;
}

function blockContaining(blocks, pos) {
  const i = lastBlockStartingAtOrBefore(blocks, pos);
  return i >= 0 && pos <= blocks[i].to ? blocks[i] : null;
}

/** Largo del marcador con el que empieza la primera línea de `block`
 * (`# `, `- `, `1. `, `> `...): lo que NO se lleva al fusionarlo con el de
 * arriba, que se queda con su propio tipo. */
export function leadingMarkLength(doc, block) {
  const first = doc.lineAt(block.from);
  const text = doc.sliceString(block.from, first.to);
  let re = null;
  if (HEADING_TYPES.has(block.type)) re = HEADING_MARK_RE;
  else if (block.type === "bullet_list_item" || block.type === "ordered_list_item") re = LIST_MARK_RE;
  else if (block.type === "quote") re = QUOTE_MARK_RE;
  const m = re && re.exec(text);
  return m ? m[0].length : 0;
}

/**
 * Une el bloque `lower` al final de `upper` (⌫ al principio de `lower`, o Supr
 * al final de `upper`): el texto de abajo sube a la última línea del de arriba,
 * sin su marcador, y el de arriba conserva su tipo -- lo que hace Notion.
 * Solo entre bloques de texto; con código, tablas o una línea horizontal en
 * medio no se fusiona nada.
 */
function joinEdit(doc, upper, lower) {
  if (!TEXT_TYPES.has(upper.type) || !TEXT_TYPES.has(lower.type)) return null;
  const cut = lower.from + leadingMarkLength(doc, lower);
  return { changes: { from: upper.to, to: cut, insert: "" }, cursor: upper.to };
}

/**
 * ⌫ con el cursor vacío en `pos`. Cubre lo que pasa en la frontera entre
 * bloques y deja lo demás al resto de teclas (devuelve `null`):
 *
 * - en un bloque vacío: desaparece él y el separador de encima; el cursor
 *   queda al final de lo de arriba;
 * - al principio de un encabezado: deja de serlo (se quita el `#`), como en
 *   Notion; el texto no se mueve de sitio;
 * - al principio de un bloque con un separador encima: si arriba hay un bloque
 *   vacío, se va el vacío; si hay un bloque de texto, se fusionan; si hay una
 *   línea horizontal, se borra; con código o una tabla arriba, el cursor sube
 *   al final de ese bloque sin borrar nada.
 */
export function gapBackspaceEdit(doc, blocks, pos) {
  const line = doc.lineAt(pos);
  const role = gapRole(doc, blocks, line.number);

  if (role === "empty") {
    if (line.number === 1) return null;
    const above = doc.line(line.number - 1);
    if (gapRole(doc, blocks, line.number - 1) !== "sep") return null;
    if (above.number === 1) return null;
    return { changes: { from: above.from - 1, to: line.from, insert: "" }, cursor: above.from - 1 };
  }
  if (role) return null;

  const block = blockContaining(blocks, pos);
  if (!block || doc.lineAt(block.from).number !== line.number) return null;

  // ⌫ al principio de un encabezado o de una cita: pasa a párrafo (en una
  // cita, se quita el `>` de todas sus filas).
  if (HEADING_TYPES.has(block.type) || block.type === "quote") {
    const mark = leadingMarkLength(doc, block);
    if (mark > 0 && pos >= block.from && pos <= block.from + mark) {
      if (block.type !== "quote") {
        return { changes: { from: block.from, to: block.from + mark, insert: "" }, cursor: block.from };
      }
      const changes = [];
      for (let n = line.number; n <= doc.lineAt(block.to).number; n++) {
        const row = doc.line(n);
        const m = QUOTE_MARK_RE.exec(row.text);
        if (m && m[0].length) changes.push({ from: row.from, to: row.from + m[0].length, insert: "" });
      }
      return { changes, cursor: block.from };
    }
  }

  if (pos !== line.from || line.number < 3) return null;
  if (gapRole(doc, blocks, line.number - 1) !== "sep") return null;
  const aboveNo = line.number - 2;
  const above = doc.line(aboveNo);
  if (gapRole(doc, blocks, aboveNo) === "empty") {
    return { changes: { from: above.from, to: line.from, insert: "" }, cursor: above.from };
  }
  const upper = blockContaining(blocks, above.to);
  if (!upper) return null;
  if (upper.type === "hr") {
    return { changes: { from: upper.from, to: line.from, insert: "" }, cursor: upper.from };
  }
  return joinEdit(doc, upper, block) || { changes: [], cursor: upper.to };
}

/**
 * Supr con el cursor vacío en `pos`, el espejo de ⌫:
 *
 * - en un bloque vacío: desaparece él y el separador de debajo; el cursor se
 *   queda en su sitio, que ahora es el principio del bloque de abajo;
 * - al final de un bloque con un separador debajo: si debajo hay un bloque
 *   vacío, se va; si hay un bloque de texto, sube y se fusiona.
 */
export function gapDeleteEdit(doc, blocks, pos) {
  const line = doc.lineAt(pos);
  // Hace falta un separador debajo y algo debajo del separador.
  if (pos !== line.to || line.number + 2 > doc.lines) return null;
  const role = gapRole(doc, blocks, line.number);
  if (role === "sep") return null;
  if (gapRole(doc, blocks, line.number + 1) !== "sep") return null;

  if (role === "empty") {
    return { changes: { from: line.from, to: line.from + 2, insert: "" }, cursor: line.from };
  }
  const upper = blockContaining(blocks, pos);
  if (!upper || upper.to !== line.to) return null;
  const belowNo = line.number + 2;
  const below = doc.line(belowNo);
  if (gapRole(doc, blocks, belowNo) === "empty") {
    return { changes: { from: line.to, to: below.from, insert: "" }, cursor: line.to };
  }
  const lower = blockStartingAt(blocks, below.from);
  if (!lower) return null;
  return joinEdit(doc, upper, lower) || { changes: [], cursor: pos };
}

// ------------------------------------------------- escribir en un bloque vacío

/**
 * Lo que hay que añadir detrás de lo que se escribe en un bloque vacío para que
 * no se pegue al bloque de abajo: un salto (su separador) si la línea de debajo
 * tiene texto. Pasa en notas escritas a mano (`alfa\n\n\nbeta`: separador,
 * vacío y `beta` sin separador) y al principio de la nota. Con el separador ya
 * puesto (lo normal) no hace falta nada.
 */
export function emptyBlockTail(doc, blocks, pos) {
  const line = doc.lineAt(pos);
  if (gapRole(doc, blocks, line.number) !== "empty") return "";
  if (line.number >= doc.lines) return "";
  return doc.line(line.number + 1).length > 0 ? "\n" : "";
}
