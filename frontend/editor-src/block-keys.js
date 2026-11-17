// Teclas de bloque (⏎, ⇧⏎ y ⌘/⌃⏎): lógica pura, sin CodeMirror.
//
// Los bloques no son un objeto aparte del documento: son rangos de texto que
// se calculan con el árbol de Markdown (ver `computeBlocks` en entry.js) y el
// `.md` sigue separándolos con UNA línea en blanco. Aquí solo se decide QUÉ
// cambio de texto aplica cada tecla, dados el documento, el cursor y el bloque
// donde está el cursor; aplicarlo (una transacción de CodeMirror) es cosa de
// entry.js. Así se prueba con `node --test`, sin navegador, igual que
// link-create.js.
//
// Reglas (decisión del dueño, cerrada):
//   ⏎             -> bloque nuevo ... salvo donde el tipo tiene su propia
//                    continuación: otro punto en una lista, siguiente número
//                    en una numerada, otro `>` en una cita, y un salto dentro
//                    de un bloque de código (una valla no se parte nunca).
//   ⇧⏎ / ⌘⏎ / ⌃⏎ -> salto de línea DENTRO del bloque (en una lista, con la
//                    sangría del ítem; en una cita, repitiendo el `>`).
//   Ítem o cita vacíos + ⏎ -> se sale: el marcador desaparece y queda un
//                    párrafo normal. Es el mismo camino con uno o con veinte
//                    ítems (D4/D5).

const BULLET_RE = /^([ \t]*)([-*+])([ \t]+)/;
const ORDERED_RE = /^([ \t]*)(\d+)([.)])([ \t]+)/;
const QUOTE_PREFIX_RE = /^(?:[ \t]*>[ \t]?)+/;
const HEADING_RE = /^h[1-5]$/;
const SPACE_RE = /[ \t]/;

/** Fila lógica que contiene `pos` (el cursor justo antes de un `\n` cuenta
 * como parte de esa fila, no de la siguiente). */
function lineAt(text, pos) {
  const from = text.lastIndexOf("\n", pos - 1) + 1;
  const nl = text.indexOf("\n", pos);
  return { from, to: nl === -1 ? text.length : nl };
}

/**
 * Ajusta el punto de corte para no dejar espacios colgando: los que hay justo
 * antes del cursor se van con la parte de arriba y los que hay justo después
 * se van con la de abajo. Es lo que evita el `>  larga` de D11 (y un espacio
 * final que en Markdown es un salto duro).
 */
function cutAround(text, blockFrom, blockTo, pos) {
  let from = pos;
  let to = pos;
  while (from > blockFrom && SPACE_RE.test(text[from - 1])) from--;
  while (to < blockTo && SPACE_RE.test(text[to])) to++;
  return { from, to };
}

function leadingNewlines(s) {
  let n = 0;
  while (s[n] === "\n") n++;
  return n;
}

function insertAt(pos, insert) {
  return { from: pos, to: pos, insert, cursor: pos + insert.length };
}

// ------------------------------------------------- ¿el de abajo se lo come?

/**
 * ¿El texto que se escribe encima de este bloque acabaría DENTRO de él?
 *
 * La pregunta la contesta el PARSER, no una lista de casos. Se compone el texto
 * tal como quedaría sin la línea en blanco -- el párrafo nuevo (`x`) y, pegado
 * debajo, el bloque de abajo ENTERO tal y como lo da `computeBlocks` (algunas
 * construcciones necesitan su segunda línea para saberse: una tabla es la
 * cabecera más la fila de guiones) -- y se mira el primer nodo de primer nivel:
 * si acaba justo en la `x`, el de abajo empieza bloque propio y su línea en
 * blanco se puede quitar; si se alarga, se ha comido la línea.
 *
 * `parser` es el `Parser` de Lezer del propio editor -- ver `markdownParser` en
 * entry.js, el mismo `LanguageSupport` que monta el `EditorView` --, así que la
 * regla no puede separarse de lo que se pinta: una tabla (CommonMark no las
 * tiene: es un párrafo más), una numerada que empieza en 2, un `[id]: /url` o
 * lo que venga mañana salen solos, sin mantener dos criterios. El test de
 * árbitro de test/block-keys.test.js parsea con ESE parser y compara.
 */
export function gluesBelow(parser, text) {
  if (!text) return false;
  const first = parser.parse("x\n" + text).topNode.firstChild;
  return !(first && first.from === 0 && first.to === 1);
}

/**
 * Lo que se teclea (o se pega) en el hueco de un bloque nuevo abierto con ⏎ al
 * final de otro, con las líneas en blanco que necesita alrededor (ver
 * `blockBreak`):
 *
 * - la de ARRIBA va siempre: es la que da el hueco de la regla (18 px detrás de
 *   un H1, 14 detrás de un H2–H4) y la que impide que el texto se pegue al
 *   bloque de arriba. Con un bloque debajo que no se lo come, la que ya había
 *   entre los dos hace ese papel y el `.md` no gana nada;
 * - la de ABAJO solo cuando el bloque de abajo se comería el texto
 *   (`gluesBelow`): sin ella, el párrafo nuevo y el de abajo serían uno.
 */
export function blockBreakInsert(parser, typed, below) {
  return "\n" + typed + (below != null && gluesBelow(parser, below) ? "\n" : "");
}

// ---------------------------------------------------------------- ⏎ en seco

/**
 * Parte un bloque que no tiene continuación propia (párrafo, título, otros):
 * lo que queda detrás del cursor pasa a ser un bloque NUEVO.
 *
 * Al final del bloque (el caso de ⏎ detrás de un encabezado, t_c7756962) el
 * bloque nuevo NO materializa líneas en blanco: el cursor se queda en la línea
 * de debajo, que es la línea en blanco que YA separa del bloque siguiente -- o,
 * si no había ninguna, una sola que se crea ahora --, y al final de la nota la
 * línea de debajo a secas. Así el hueco que se ve en pantalla es el que dicta
 * la regla de huecos del bloque de arriba (el de UNA línea en blanco), no la
 * suma de líneas materializadas más el hueco del bloque vacío: pulsar ⏎ detrás
 * de un H1 dejaba tres líneas en blanco (18 + 18 + 18 px) para un párrafo
 * nuevo vacío.
 *
 * `blockBreak` dice que esa línea es el bloque nuevo: la línea en blanco que lo
 * separa del bloque de arriba se materializa en cuanto se escribe la primera
 * letra (ver `blockBreakField` en entry.js).
 *
 * Detrás de una fila del MEDIO del bloque, el salto que ya había cuenta como
 * uno de los dos separadores y solo hace falta uno más -- si no, saldría un
 * hueco de dos líneas en blanco.
 */
function splitBlock(text, pos, block) {
  const { from, to } = cutAround(text, block.from, block.to, pos);
  if (to === block.to) {
    // El cursor va al principio de la línea de debajo: la línea en blanco que
    // ya había no se toca (el `.md` no cambia); si no la había, se crea.
    if (text.startsWith("\n\n", block.to)) return { from, to, insert: "", cursor: block.to + 1, blockBreak: true };
    return { from, to, insert: "\n", cursor: from + 1, blockBreak: true };
  }
  const tail = text.slice(to, block.to);
  const gap = tail.startsWith("\n") ? "\n" : "\n\n";
  return { from, to, insert: gap, cursor: from + gap.length + leadingNewlines(tail), blockBreak: false };
}

/**
 * Sale de una lista o de una cita: quita el marcador de la fila vacía y deja
 * el cursor en un párrafo normal.
 *
 * - Si no queda nada detrás, la fila se convierte en la línea en blanco que
 *   separa el párrafo nuevo (el `.md` recorta los saltos finales, así que en
 *   disco el resultado es `- uno` a secas, sin `- ` colgando).
 * - Si hay contenido detrás (salir del medio de una lista), la fila del cursor
 *   queda ENTRE dos líneas en blanco: la de arriba, para que el párrafo que se
 *   teclea no sea continuación perezosa del ítem (`- uno\nparrafo` son un solo
 *   bloque en Markdown y al recargar la nota el párrafo desaparece), y la de
 *   abajo, para que la lista que sigue siga siendo un bloque aparte. Las filas
 *   en blanco que ya hubiera detrás se consumen y se reconstruye el hueco, así
 *   que el resultado no depende de cuántos saltos hubiera.
 */
function exitMarkerLine(text, line) {
  const hasBreak = line.to < text.length;
  const nextContent = hasBreak ? text.slice(line.to + 1) : "";
  if (nextContent.trim() === "") {
    return { from: line.from, to: hasBreak ? line.to + 1 : line.to, insert: "\n", cursor: line.from + 1 };
  }
  // Se consume el marcador y las líneas en blanco que hubiera detrás, y se
  // deja el hueco de un párrafo: una línea en blanco arriba, otra abajo.
  let contentStart = line.to + 1;
  while (contentStart < text.length) {
    const nl = text.indexOf("\n", contentStart);
    const rowEnd = nl === -1 ? text.length : nl;
    if (text.slice(contentStart, rowEnd).trim() !== "") break;
    contentStart = nl === -1 ? text.length : nl + 1;
  }
  return { from: line.from, to: contentStart, insert: "\n\n\n", cursor: line.from + 1 };
}

function listEnter(text, pos, block) {
  const line = lineAt(text, pos);
  const lineText = text.slice(line.from, line.to);
  const m = (block.type === "ordered_list_item" ? ORDERED_RE : BULLET_RE).exec(lineText);
  if (!m) return null; // fila de continuación de un ítem: que decida el keymap general
  const markerEnd = line.from + m[0].length;
  if (text.slice(markerEnd, block.to).trim() === "") return exitMarkerLine(text, line);

  // El cursor nunca se coloca antes del marcador: partir ahí dejaría un `- `
  // huérfano encima del ítem.
  const at = Math.max(pos, markerEnd);
  const marker =
    block.type === "ordered_list_item"
      ? m[1] + String(parseInt(m[2], 10) + 1) + m[3] + m[4] // 2. -> 3.
      : m[1] + m[2] + m[3]; //                                -  -> -
  return insertAt(at, "\n" + marker);
}

function quoteEnter(text, pos, block) {
  const line = lineAt(text, pos);
  const lineText = text.slice(line.from, line.to);
  const prefix = (QUOTE_PREFIX_RE.exec(lineText) || ["> "])[0];
  const contentFrom = line.from + prefix.length;
  if (text.slice(contentFrom, block.to).trim() === "") return exitMarkerLine(text, line);
  const at = Math.max(pos, contentFrom);
  const { from, to } = cutAround(text, block.from, Math.min(block.to, line.to), at);
  return { from, to, insert: "\n" + prefix, cursor: from + 1 + prefix.length };
}

// ------------------------------------------------------- ⇧⏎ / ⌘⏎ (salto suave)

function listSoftBreak(text, pos, block) {
  const line = lineAt(text, pos);
  const lineText = text.slice(line.from, line.to);
  const m = (block.type === "ordered_list_item" ? ORDERED_RE : BULLET_RE).exec(lineText);
  if (!m) return splitSoft(text, pos, block);
  // La continuación de un ítem se sangra a la altura del texto del ítem, para
  // que siga siendo el MISMO ítem ("- uno\n  dos") y no un párrafo suelto.
  const indent = m[1] + " ".repeat(m[0].length - m[1].length);
  const at = Math.max(pos, line.from + m[0].length);
  return insertAt(at, "\n" + indent);
}

function quoteSoftBreak(text, pos, block) {
  const line = lineAt(text, pos);
  const prefix = (QUOTE_PREFIX_RE.exec(text.slice(line.from, line.to)) || ["> "])[0];
  const at = Math.max(pos, line.from + prefix.length);
  const { from, to } = cutAround(text, block.from, Math.min(block.to, line.to), at);
  return { from, to, insert: "\n" + prefix, cursor: from + 1 + prefix.length };
}

function splitSoft(text, pos, block) {
  const { from, to } = cutAround(text, block.from, block.to, pos);
  return { from, to, insert: "\n", cursor: from + 1 };
}

/**
 * Cambio de texto que aplica una tecla de bloque.
 *
 * @param {Object} opts
 * @param {string} opts.text    documento completo
 * @param {number} opts.pos     cursor (posición colapsada)
 * @param {{from: number, to: number, type: string}|null} opts.block
 *        bloque que contiene el cursor, tal y como lo da `computeBlocks`
 *        (null: no hay bloque ahí -- nota vacía, fila en blanco entre bloques)
 * @param {"enter"|"soft"} [opts.kind] `enter` = ⏎; `soft` = ⇧⏎, ⌘⏎ o ⌃⏎
 * @returns {{from: number, to: number, insert: string, cursor: number,
 *            blockBreak?: boolean}|null}
 *        null cuando la tecla no es de este módulo (que decida CodeMirror).
 *        `blockBreak` marca el bloque vacío que se abre al final de otro:
 *        mientras esté vacío no lleva líneas en blanco materializadas.
 */
export function blockKeyEdit({ text, pos, block, kind = "enter" }) {
  if (!block || pos < block.from || pos > block.to) return null;
  const type = block.type;

  if (type === "code") {
    // Una valla no se parte nunca: ni ⏎ ni ⇧⏎ crean bloque, y la línea en
    // blanco con significado dentro del código no aparece (D7).
    return insertAt(pos, "\n");
  }

  if (kind === "soft") {
    if (type === "bullet_list_item" || type === "ordered_list_item") return listSoftBreak(text, pos, block);
    if (type === "quote") return quoteSoftBreak(text, pos, block);
    if (type === "hr") return null;
    // Un título ATX es UNA línea: no hay salto dentro de él. Se trata como ⏎
    // (bloque nuevo) para que el `.md` siga separando bloques con una línea
    // en blanco en vez de dejar un título y un párrafo pegados.
    if (HEADING_RE.test(type)) return splitBlock(text, pos, block);
    return splitSoft(text, pos, block);
  }

  if (type === "bullet_list_item" || type === "ordered_list_item") return listEnter(text, pos, block);
  if (type === "quote") return quoteEnter(text, pos, block);
  if (type === "hr") return null;
  return splitBlock(text, pos, block);
}
