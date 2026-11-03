// Borrado de bloques: lógica pura, sin CodeMirror.
//
// Un bloque no es un objeto del modelo: es un rango de texto calculado con el
// árbol de Markdown (ver `computeBlocks` en entry.js). Borrar un bloque es,
// por tanto, decidir QUÉ trozo de texto desaparece -- eso se calcula aquí y se
// prueba con `node --test`; aplicarlo (una transacción de CodeMirror) es cosa
// de entry.js. Mismo patrón que block-keys.js y link-create.js.
//
// LA REGLA (una sola, para todas las operaciones que tocan esta frontera):
// cuando una lista, una cita o un bloque de código queda SEGUIDO de otro
// bloque, entre ellos va una línea en blanco. Si no, Markdown lee la primera
// línea del bloque siguiente como continuación del anterior y el `.md` miente
// sobre lo que se ve: `- uno\n- dos\n\nparrafo` menos `- dos` tiene que ser
// `- uno\n\nparrafo`, nunca `- uno\nparrafo`. La única frontera que se queda
// en un salto simple es la de dos ítems de lista seguidos (es el separador que
// tienen dentro de UNA lista); ahí se conserva el que había, para no convertir
// una lista suelta en apretada ni al revés.
//
// De ahí salen las cuatro piezas:
// - borrar (D1/D2): el bloque y su separador, dejando la frontera que toque;
// - escribir encima de un ítem marcado: el texto hereda el marcador (`2. X`),
//   así sigue siendo un bloque de la misma lista;
// - convertir: el bloque convertido queda como bloque propio, con su línea en
//   blanco si deja de ser un ítem;
// - ⌫ al principio de un ítem (D9): fusiona con el de arriba dejando un
//   espacio (`- uno dos`), nunca `- uno- dos`, y solo si el ÁRBOL dice que la
//   fila es un ítem (dentro de una valla de código `- dos` es texto literal).
// - ⌫ en las filas de una valla de código (apertura, primera de contenido y
//   cierre): la tecla se consume sin tocar el texto, porque unir esa fila con
//   la de arriba convierte la valla en parte del párrafo y el `.md` pierde el
//   bloque de código (o se queda sin cerrar y se traga lo de detrás).

const BULLET_RE = /^([ \t]*)([-*+])[ \t]+/;
const ORDERED_RE = /^([ \t]*)(\d+)([.)])[ \t]+/;
const LIST_ITEM_TYPES = new Set(["bullet_list_item", "ordered_list_item"]);

function isListItem(block) {
  return !!block && LIST_ITEM_TYPES.has(block.type);
}

function isSpace(ch) {
  return ch === " " || ch === "\t" || ch === "\n";
}

function onlyWhitespace(s) {
  return /^[ \t\n]*$/.test(s);
}

/** ¿Hay línea en blanco en este hueco? (un `\n` solo, no). */
function hasBlankLine(s) {
  return /\n[ \t]*\n/.test(s);
}

/** Fila lógica que contiene `pos` (el `\n` de `pos` cuenta como parte de ella). */
function lineAt(text, pos) {
  const from = text.lastIndexOf("\n", pos - 1) + 1;
  const nl = text.indexOf("\n", pos);
  return { from, to: nl === -1 ? text.length : nl };
}

/** ¿El texto nuevo empieza como ítem de lista? (para respetar su frontera) */
function startsAsListItem(s) {
  const line = s.split("\n", 1)[0];
  return BULLET_RE.test(line) || ORDERED_RE.test(line);
}

/**
 * Separador que tiene que quedar entre dos bloques que ahora quedan juntos
 * (`prev` puede no existir: entonces no hay nada que separar).
 *
 * - dos ítems de lista que estaban pegados siguen pegados (`\n`) -- una lista,
 *   no dos listas;
 * - cualquier otra frontera, una línea en blanco (`\n\n`).
 */
function joinSeparator(prev, next, gap) {
  if (!prev) return "";
  if (isListItem(prev) && isListItem(next) && !hasBlankLine(gap)) return "\n";
  return "\n\n";
}

/**
 * Trozo de texto que se lleva borrar los bloques `a..b` (índices consecutivos
 * ya fusionados), separadores incluidos, y el que deja en su lugar.
 *
 * - si queda algún bloque detrás, se lleva el hueco de delante y el de detrás
 *   y deja UN separador (`joinSeparator`): un solo hueco, sin aire de más y
 *   sin pegar una lista al bloque que venía después;
 * - si no queda nada detrás, se lleva además el hueco de delante y el del
 *   final del documento, para que el `.md` no acabe en líneas en blanco.
 */
function deletedSpan(text, blocks, a, b) {
  const prev = a > 0 ? blocks[a - 1] : null;
  const gapStart = prev ? prev.to : 0;
  const gap = text.slice(gapStart, blocks[a].from);
  const next = b + 1 < blocks.length ? blocks[b + 1] : null;

  if (next) {
    // Entre los bloques solo puede haber saltos de línea; si hubiera otra
    // cosa (un comentario HTML) no se toca el hueco de delante.
    if (!onlyWhitespace(gap)) return { from: blocks[a].from, to: next.from, insert: "" };
    return { from: gapStart, to: next.from, insert: joinSeparator(prev, next, gap) };
  }

  let to = blocks[b].to;
  if (to < text.length && onlyWhitespace(text.slice(to))) to = text.length;
  let from = blocks[a].from;
  if (onlyWhitespace(text.slice(gapStart, from))) from = gapStart;
  else while (from > 0 && isSpace(text[from - 1])) from--;
  return { from, to, insert: "" };
}

/** Marcador (sangría incluida) de la primera línea de un ítem, o null. */
function listMarker(text, block) {
  if (!isListItem(block)) return null;
  const line = text.slice(block.from, lineAt(text, block.from).to);
  const mark = BULLET_RE.exec(line) || ORDERED_RE.exec(line);
  return mark ? mark[0] : null;
}

/**
 * Sustituir los bloques `a..b` por `insert` (escribir o pegar encima).
 *
 * Si el primer bloque es un ítem de lista y el texto cabe en una línea, hereda
 * el marcador (`2. X`): el texto tecleado sigue siendo un bloque de la misma
 * lista, no una línea pegada al ítem de arriba (que es lo que hacía
 * `1. uno\nX\n2. tres`). Si el texto tiene varias líneas ya no es un ítem: va
 * como bloque propio con su línea en blanco antes y después.
 */
function replaceSpan(text, blocks, a, b, insert) {
  const first = blocks[a];
  const marker = listMarker(text, first);

  if (marker) {
    // El texto hereda el marcador del ítem (`2. X`) y las líneas de
    // continuación se sangran a la altura del texto: sigue siendo UN bloque de
    // la misma lista, no una línea pegada al ítem de arriba (`1. uno\nX`).
    const indent = " ".repeat(marker.length);
    const [head, ...tail] = insert.split("\n");
    const body = [head, ...tail.map((line) => (line ? indent + line : ""))].join("\n");
    return { from: first.from, to: blocks[b].to, insert: marker + body, keepsFirstItem: a };
  }
  return { from: first.from, to: blocks[b].to, insert };
}

/**
 * Cambios de texto para borrar (o sustituir) bloques.
 *
 * `blocks` es la lista de `{from, to, type}` que calcula entry.js, `indices`
 * los bloques marcados e `insert` el texto con el que se sustituyen (vacío =
 * borrar).
 *
 * Devuelve `{changes, cursor, keepsFirstItem}` o `null` si no hay nada que
 * hacer. `changes` nunca se solapan y van en orden de documento.
 * `keepsFirstItem` es el índice del bloque que sobrevive como ítem (cuando el
 * texto hereda el marcador), para que la renumeración no lo cuente como
 * borrado.
 */
export function blockDeleteEdits({ text, blocks, indices, insert = "" }) {
  const del = [...new Set([...indices].map(Number))]
    .filter((i) => Number.isInteger(i) && i >= 0 && i < blocks.length)
    .sort((a, b) => a - b);
  if (!del.length) return null;

  // Índices consecutivos => un solo trozo (borrar un rango entero es UN
  // cambio, no una suma de borrados que se pisen).
  const runs = [];
  for (const i of del) {
    const last = runs[runs.length - 1];
    if (last && i === last[1] + 1) last[1] = i;
    else runs.push([i, i]);
  }

  const changes = [];
  let cursor = null;
  let keepsFirstItem = null;
  runs.forEach(([a, b], n) => {
    let change;
    if (n === 0 && insert) {
      change = replaceSpan(text, blocks, a, b, insert);
      if (change.keepsFirstItem !== undefined) keepsFirstItem = change.keepsFirstItem;
    } else {
      change = deletedSpan(text, blocks, a, b);
    }
    const next = { from: change.from, to: change.to, insert: change.insert || "" };
    if (cursor === null) cursor = next.from + next.insert.length;
    // Dos trozos que se tocan (bloques de longitud cero) se fusionan en uno:
    // ChangeSet rechaza cambios que empiezan donde acaba el anterior.
    const prev = changes[changes.length - 1];
    if (prev && next.from < prev.to) {
      prev.to = Math.max(prev.to, next.to);
    } else {
      changes.push(next);
    }
  });

  return { changes, cursor, keepsFirstItem };
}

/**
 * ⏎ (y ⇧⏎) con bloques marcados: los marcados se sustituyen por UN bloque
 * vacío en su sitio, con el cursor dentro y listo para escribir.
 *
 * Es el mismo trozo que borrar (bloque + separador), pero dejando los dos
 * saltos que convierten el hueco en un bloque de verdad. Sin esto, ⏎ sobre un
 * bloque marcado metía su salto DENTRO del rango marcado, entre los
 * separadores que ya había: `uno / dos / tres` menos `dos` acababa en
 * `uno\n\n\n\n\n\ntres` (cinco líneas en blanco) y así se guardaba en el `.md`.
 */
export function newBlockEdit({ text, blocks, indices }) {
  const edit = blockDeleteEdits({ text, blocks, indices });
  if (!edit) return null;
  const del = [...new Set([...indices].map(Number))]
    .filter((i) => Number.isInteger(i) && i >= 0 && i < blocks.length)
    .sort((a, b) => a - b);
  const firstIndex = del[0];
  let last = firstIndex;
  for (let i = 1; i < del.length && del[i] === last + 1; i++) last = del[i];
  const atEnd = last + 1 >= blocks.length;

  const [first, ...rest] = edit.changes;
  // El bloque vacío es el hueco que queda ENTRE los dos separadores. Si había
  // bloque detrás, el hueco empieza donde empezaba el bloque borrado; si era
  // el último, el trozo borrado se llevó el separador de delante y el hueco
  // son los dos saltos que se insertan al final.
  const from = atEnd ? first.from : firstIndex > 0 ? blocks[firstIndex].from : first.from;
  return {
    changes: [{ from, to: first.to, insert: "\n\n" }, ...rest],
    cursor: atEnd ? first.from + 2 : from,
  };
}

/**
 * Reenumeración de las listas numeradas tocadas por un borrado.
 *
 * Solo se reenumeran las listas que pierden algún ítem (una lista intacta se
 * deja tal cual, aunque sus números no sean 1..n): de cada lista afectada, los
 * ítems que sobreviven se numeran desde el número con el que empezaba la
 * lista, conservando la sangría y el separador (`.` o `)`).
 *
 * Una lista es una tirada de bloques `ordered_list_item` seguidos en el árbol;
 * el hueco entre ellos puede ser un salto (lista apretada) o una línea en
 * blanco (lista suelta) -- da igual, Markdown la lee como una sola lista.
 *
 * `keepFirst` (opcional) es el índice de un ítem que NO desaparece aunque esté
 * en `indices`: es el caso de escribir encima de un ítem, donde el texto
 * hereda el marcador y sigue ocupando el mismo sitio.
 *
 * Devuelve cambios de texto que sustituyen solo el número (nunca el texto del
 * ítem) y no se solapan con los de `blockDeleteEdits`.
 */
export function orderedRenumberEdits({ text, blocks, indices, keepFirst = null }) {
  const del = new Set([...indices].map(Number));
  if (!del.size) return [];
  const removed = (k) => del.has(k) && k !== keepFirst;

  const changes = [];
  let i = 0;
  while (i < blocks.length) {
    if (blocks[i].type !== "ordered_list_item") {
      i++;
      continue;
    }
    let end = i;
    while (end + 1 < blocks.length && blocks[end + 1].type === "ordered_list_item") end++;

    let touched = false;
    for (let k = i; k <= end; k++) if (removed(k)) touched = true;

    if (touched) changes.push(...runNumberEdits(text, blocks, i, end, removed));
    i = end + 1;
  }
  return changes;
}

/**
 * Cambios de número para una tirada contigua de ítems numerados: los renumera
 * desde el número con el que empieza la lista, conservando la sangría y el
 * separador (`.` o `)`). `skip(k)` deja un ítem fuera del recuento (el que
 * desaparece en un borrado, o el que hereda el marcador al escribir encima).
 */
function runNumberEdits(text, blocks, i, end, skip = () => false) {
  const firstMark = ORDERED_RE.exec(text.slice(blocks[i].from, blocks[i].to));
  if (!firstMark) return [];
  const changes = [];
  let number = Number(firstMark[2]);
  for (let k = i; k <= end; k++) {
    if (skip(k)) continue;
    const mark = ORDERED_RE.exec(text.slice(blocks[k].from, blocks[k].to));
    if (!mark) continue;
    const at = blocks[k].from + mark[1].length;
    if (mark[2] !== String(number)) {
      changes.push({ from: at, to: at + mark[2].length, insert: String(number) });
    }
    number += 1;
  }
  return changes;
}

/**
 * Reenumerar la lista numerada que contiene el bloque `index` (su tirada
 * contigua de ítems), desde el número con el que empieza. Lo usa «Duplicar»:
 * la copia entra con el número del original y los de detrás se corren uno, con
 * el mismo criterio que un borrado (solo se reenumera la lista tocada).
 *
 * `text` y `blocks` tienen que ser los del documento YA insertado -- la copia
 * forma parte de la lista a reenumerar.
 */
export function renumberOrderedRun({ text, blocks, index }) {
  const k = Number(index);
  if (!Number.isInteger(k) || k < 0 || k >= blocks.length) return [];
  if (blocks[k].type !== "ordered_list_item") return [];
  let start = k;
  while (start > 0 && blocks[start - 1].type === "ordered_list_item") start--;
  let end = k;
  while (end + 1 < blocks.length && blocks[end + 1].type === "ordered_list_item") end++;
  return runNumberEdits(text, blocks, start, end);
}

/**
 * Cambios de texto para duplicar bloques («Duplicar bloque» del menú): una
 * copia del bloque -- o del grupo marcado, como UNA copia seguida -- justo
 * debajo del original.
 *
 * La frontera es la misma de siempre: dos ítems de la misma lista van con un
 * salto (`- uno\n- uno` sigue siendo UNA lista) y cualquier otro par, con una
 * línea en blanco (`a\n\na`). En una lista numerada los números los arregla
 * `renumberOrderedRun` sobre el documento ya insertado; aquí la copia sale con
 * el número del original.
 *
 * Cada cambio lleva además `copy`: la posición (en coordenadas del texto
 * original) donde empieza la copia, para que quien aplica sepa qué bloque del
 * documento nuevo tiene que reenumerar.
 */
export function duplicateBlockEdits({ text, blocks, indices }) {
  const sel = [...new Set([...indices].map(Number))]
    .filter((i) => Number.isInteger(i) && i >= 0 && i < blocks.length)
    .sort((a, b) => a - b);
  if (!sel.length) return [];

  const runs = [];
  for (const i of sel) {
    const last = runs[runs.length - 1];
    if (last && i === last[1] + 1) last[1] = i;
    else runs.push([i, i]);
  }

  return runs.map(([a, b]) => {
    const body = text.slice(blocks[a].from, blocks[b].to);
    const sameList =
      isListItem(blocks[a]) && isListItem(blocks[b]) && blocks[a].type === blocks[b].type;
    const sep = sameList ? "\n" : "\n\n";
    return { from: blocks[b].to, to: blocks[b].to, insert: sep + body, copy: blocks[b].to + sep.length };
  });
}

/**
 * Cambios de texto para convertir bloques (menú «Convertir a»).
 *
 * `convert(raw, block)` devuelve el texto nuevo del bloque. La misma regla de
 * frontera que al borrar: el bloque convertido queda como bloque propio, así
 * que si deja de ser un ítem de lista se le pone línea en blanco antes y
 * después (`1. uno / 2. dos / 3. tres` menos convertir `2. dos` en párrafo
 * tiene que dar un párrafo, no una línea pegada al ítem de arriba).
 */
export function convertBlockEdits({ text, blocks, indices, convert }) {
  const del = [...new Set([...indices].map(Number))]
    .filter((i) => Number.isInteger(i) && i >= 0 && i < blocks.length)
    .sort((a, b) => a - b);
  if (!del.length) return [];

  const runs = [];
  for (const i of del) {
    const last = runs[runs.length - 1];
    if (last && i === last[1] + 1) last[1] = i;
    else runs.push([i, i]);
  }

  return runs.map(([a, b]) => {
    const prev = a > 0 ? blocks[a - 1] : null;
    const next = b + 1 < blocks.length ? blocks[b + 1] : null;

    const parts = [];
    for (let k = a; k <= b; k++) {
      const out = convert(text.slice(blocks[k].from, blocks[k].to), blocks[k]);
      parts.push({ text: out, item: startsAsListItem(out) });
    }
    let body = parts[0].text;
    for (let k = 1; k < parts.length; k++) {
      body += (parts[k - 1].item && parts[k].item ? "\n" : "\n\n") + parts[k].text;
    }
    const firstIsItem = parts[0].item;
    const lastIsItem = parts[parts.length - 1].item;

    let from = blocks[a].from;
    let lead = "";
    const gapBefore = text.slice(prev ? prev.to : 0, blocks[a].from);
    if (onlyWhitespace(gapBefore)) {
      from = prev ? prev.to : 0;
      lead = prev ? (isListItem(prev) && firstIsItem ? "\n" : "\n\n") : "";
    }

    let to = blocks[b].to;
    let trail = "";
    if (next) {
      if (onlyWhitespace(text.slice(blocks[b].to, next.from))) {
        to = next.from;
        trail = isListItem(next) && lastIsItem ? "\n" : "\n\n";
      }
    } else if (onlyWhitespace(text.slice(to))) {
      to = text.length;
    }

    return { from, to, insert: lead + body + trail };
  });
}

/**
 * ⌫ al principio de un ítem de lista (D9).
 *
 * Fusión con el ítem de arriba: se va el salto de línea y el marcador del ítem
 * actual, y queda UN espacio entre los dos textos (`- uno dos`). Si el ítem es
 * el primero de la lista (arriba no hay otro ítem), se le quita el marcador y
 * el texto sube a párrafo normal, que es lo que ya hace ⌫ con el resto de
 * marcas de Markdown.
 *
 * `blocks` es obligatorio: la fila tiene que ser un ítem del ÁRBOL, no solo
 * parecerlo. Dentro de una valla de código `- dos` es texto literal y ⌫ no
 * puede comerse el `- ` (se perderían caracteres escritos y la valla
 * cambiaría de contenido).
 *
 * Devuelve `{from, to, insert, cursor}` o `null` cuando no aplica (no estamos
 * al principio de la fila, la fila no es un ítem, o estamos en la posición 0).
 */
export function backspaceJoinEdit({ text, pos, blocks }) {
  if (!Array.isArray(blocks)) {
    throw new TypeError("backspaceJoinEdit necesita los bloques del árbol (blocks)");
  }
  const line = lineAt(text, pos);
  if (pos !== line.from) return null;

  const isItemAt = (from) => blocks.some((b) => isListItem(b) && b.from === from);
  if (!isItemAt(line.from)) return null;

  const source = text.slice(line.from, line.to);
  const marker = BULLET_RE.exec(source) || ORDERED_RE.exec(source);
  if (!marker) return null;

  const markerEnd = line.from + marker[0].length;

  if (line.from === 0) {
    // Primer ítem del documento: no hay arriba nada con lo que fusionar; se
    // queda como está (⌫ en la posición 0 no hace nada).
    return null;
  }

  const prevLine = lineAt(text, line.from - 1);
  const prevIsItem = isItemAt(prevLine.from);

  if (prevIsItem) {
    // Los espacios que colgaban del ítem de arriba se van con la fusión.
    let cut = prevLine.to;
    while (cut > prevLine.from && isSpace(text[cut - 1])) cut--;
    const insert = markerEnd < line.to ? " " : "";
    return {
      from: cut,
      to: markerEnd,
      insert,
      cursor: cut + insert.length,
    };
  }

  // Primero de la lista (o lista suelta): el marcador desaparece y el texto
  // se queda como párrafo. El salto de línea de arriba no se toca.
  return { from: line.from, to: markerEnd, insert: "", cursor: line.from };
}


// ------------------------------------------------------------ valla de código
//
// ⌫ al principio de la primera línea de contenido o de la valla de cierre no
// puede unir esa fila con la de arriba: la valla se convierte en texto
// (` ```codigo `, `codigo``` `) y el bloque de código deja de existir en el
// `.md`. En el cierre, además, el bloque se queda sin cerrar y al recargar se
// traga todo lo que venga detrás -- corrupción del fichero, no un detalle de
// pintado. También se consume en la valla de apertura cuando la fila de arriba
// tiene texto (`parrafo``` `); con una línea en blanco encima no hay nada que
// romper y decide el Backspace de siempre. El resto de ⌫ dentro de la valla
// (fusionar dos líneas de contenido) sigue como siempre, y para salir del
// bloque están el borrado del bloque y «Convertir a».

const FENCE_RE = /^[ \t]*(`{3,}|~{3,})/;

/** Filas de texto que ocupa un bloque, sin la fila vacía del salto final. */
function blockLines(text, block) {
  const lines = [];
  let at = block.from;
  while (at <= block.to) {
    const line = lineAt(text, at);
    lines.push(line);
    if (line.to >= block.to) break;
    at = line.to + 1;
  }
  while (lines.length > 1 && lines[lines.length - 1].from >= block.to) lines.pop();
  return lines;
}

/**
 * ¿Rompería el ⌫ en `pos` la valla de un bloque de código?
 *
 * `true` = la tecla tiene que consumirse sin tocar el texto. Solo pasa en las
 * filas de un bloque CON valla (uno de código por sangría no tiene valla que
 * romper) donde unir de verdad rompe el bloque: la primera de contenido, la de
 * cierre y la de apertura -- esta última solo si la fila de arriba tiene texto.
 *
 * `blocks` es obligatorio, igual que en `backspaceJoinEdit`: sin el árbol no se
 * puede saber si la fila es valla, texto de dentro o un párrafo que empieza por
 * tres acentos.
 */
export function backspaceWouldBreakFence({ text, pos, blocks }) {
  if (!Array.isArray(blocks)) {
    throw new TypeError("backspaceWouldBreakFence necesita los bloques del árbol (blocks)");
  }
  const block = blocks.find((b) => b.type === "code" && pos >= b.from && pos <= b.to);
  if (!block) return false;

  const lines = blockLines(text, block);
  if (!lines.length) return false;
  const lineText = (line) => text.slice(line.from, line.to);
  // Bloque de código con sangría (sin valla): unir filas no rompe nada.
  if (!FENCE_RE.test(lineText(lines[0]))) return false;

  const atLineStart = (line) => !!line && pos === line.from;

  // Primera línea de contenido: unirla mete siempre la fila en la valla.
  if (atLineStart(lines[1])) return true;

  // Valla de apertura: solo rompe si la fila de arriba tiene texto, porque
  // entonces la valla pasa a ser el final de ese párrafo (`parrafo``` `). Con
  // una línea en blanco encima, unir solo quita el hueco y la valla se queda
  // entera: ahí decide el Backspace de siempre.
  if (atLineStart(lines[0])) {
    if (pos === 0) return false;
    const above = lineAt(text, pos - 1);
    return text.slice(above.from, above.to).trim() !== "";
  }

  // Valla de cierre: unirla con el código deja el bloque sin cerrar, y al
  // recargar se traga todo lo que venga detrás.
  const closing = lines[lines.length - 1];
  return lines.length > 2 && atLineStart(closing) && FENCE_RE.test(lineText(closing));
}
