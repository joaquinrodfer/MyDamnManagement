// Movimiento de bloques: lógica pura, sin CodeMirror.
//
// Un bloque no es un objeto del modelo: es un rango de texto calculado con el
// árbol de Markdown (ver `computeBlocks` en entry.js). Moverlo es, por tanto,
// decidir QUÉ trozo de texto cambia de sitio -- eso se calcula aquí y se prueba
// con `node --test`; aplicarlo (una transacción de CodeMirror) es cosa de
// entry.js. Mismo patrón que block-delete.js y block-keys.js.
//
// Reglas (las mismas fronteras que al borrar, ver block-delete.js):
//
// - Se mueve el bloque ENTERO, del `from` al `to` que da el árbol. Eso es lo
//   que deja intacta una valla de código (```): es un rango, no una línea, y
//   aunque el cursor esté dentro se mueve la valla con su apertura y su cierre.
// - Entre dos bloques que ya eran vecinos se conserva SU hueco tal cual (el
//   `.md` no gana ni pierde aire al reordenar). Donde la vecindad es nueva, se
//   usa la frontera de siempre: salto simple entre dos ítems de la misma lista
//   y línea en blanco en cualquier otro caso -- si no, Markdown leería el
//   primer renglón del bloque de detrás como continuación del de delante.
// - Los huecos que se PISAN (los de al lado del bloque que se va y el del
//   destino, que se parte en dos) se reconstruyen, así que si llevan algo que
//   no sea espacio (un comentario HTML, que no es bloque para `computeBlocks`)
//   el movimiento se rechaza: preferible no mover a perder ese texto.
// - Una lista numerada reordenada se reenumera desde el número con el que
//   empezaba su lista original. Una lista que no se ha tocado se queda como
//   esté (aunque sus números no sean 1..n): nadie la ha reordenado.
// - El resultado es UN cambio de texto (y quien lo aplica lo despacha en UNA
//   transacción): un solo ⌘Z deshace el movimiento entero.

const ORDERED_RE = /^([ \t]*)(\d+)([.)])/;
const LIST_ITEM_TYPES = new Set(["bullet_list_item", "ordered_list_item"]);

function isListItem(block) {
  return !!block && LIST_ITEM_TYPES.has(block.type);
}

function isOrdered(block) {
  return !!block && block.type === "ordered_list_item";
}

/** Número con el que empieza un ítem numerado, o null. */
function itemNumber(blockText) {
  const m = ORDERED_RE.exec(blockText);
  return m ? Number(m[2]) : null;
}

/** El mismo ítem con otro número, conservando sangría y separador. */
function withNumber(blockText, number) {
  return blockText.replace(/^([ \t]*)\d+/, `$1${number}`);
}

/**
 * Saltos consecutivos justo ANTES de `at`, sin pasar de `floor` (el final del
 * bloque de arriba), y justo DESPUÉS, sin pasar de `ceil` (el principio del de
 * abajo). Son los que ya separan el hueco donde va el bloque nuevo: la
 * frontera de siempre pone una línea en blanco (dos saltos) entre párrafos,
 * pero entre dos ítems de la misma lista hay un solo salto.
 */
function newlinesBefore(text, at, floor) {
  let p = at;
  while (p > floor && text[p - 1] === "\n") p--;
  return at - p;
}

function newlinesAfter(text, at, ceil) {
  let p = at;
  while (p < ceil && text[p] === "\n") p++;
  return p - at;
}

/**
 * Separador entre dos bloques que NO eran vecinos: la regla de siempre.
 * Dos ítems del mismo tipo de lista van con un salto (siguen siendo UNA
 * lista); cualquier otro par, con una línea en blanco.
 */
function freshSeparator(prev, next) {
  return isListItem(prev) && isListItem(next) && prev.type === next.type ? "\n" : "\n\n";
}

/**
 * Mapa índice -> lista numerada del documento original: `{start, members}`,
 * donde `start` es el número del primer ítem y `members` los índices de la
 * tirada contigua. Sirve para reenumerar solo las listas tocadas, desde el
 * número con el que empezaban (no desde 1: una lista que continúa otra puede
 * empezar en 5 y eso no se toca).
 */
function orderedRunMap(text, blocks) {
  const map = new Array(blocks.length).fill(null);
  let i = 0;
  while (i < blocks.length) {
    if (!isOrdered(blocks[i])) {
      i++;
      continue;
    }
    let end = i;
    while (end + 1 < blocks.length && isOrdered(blocks[end + 1])) end++;
    const start = itemNumber(text.slice(blocks[i].from, blocks[i].to)) ?? 1;
    const members = [];
    for (let k = i; k <= end; k++) members.push(k);
    for (let k = i; k <= end; k++) map[k] = { start, members };
    i = end + 1;
  }
  return map;
}

/**
 * Reenumera, en el orden NUEVO, las listas numeradas que el movimiento ha
 * reordenado. `runs` es el mapa del documento original; `texts` los textos de
 * los bloques ya reordenados (se modifican aquí).
 *
 * Se considera intacta una tirada que sigue siendo la MISMA lista original con
 * los mismos ítems y en el mismo orden; cualquier otra (un ítem que entra, uno
 * que sale, o los mismos en otro orden) se reenumera desde el número con el
 * que empezaba su lista. Un ítem que se queda solo es una lista nueva y
 * empieza en 1.
 */
function renumberMovedRuns(order, texts, runs, blocks) {
  let p = 0;
  while (p < order.length) {
    if (!isOrdered(blocks[order[p]])) {
      p++;
      continue;
    }
    let q = p;
    while (q + 1 < order.length && isOrdered(blocks[order[q + 1]])) q++;
    const group = order.slice(p, q + 1);
    const original = runs[group[0]] ? runs[group[0]].members : group;
    const untouched = group.length === original.length && group.every((v, x) => v === original[x]);
    if (!untouched) {
      const first = Math.min(...group);
      const start = group.length === 1 ? 1 : (runs[first] ? runs[first].start : 1);
      group.forEach((blockIndex, n) => {
        texts[blockIndex] = withNumber(texts[blockIndex], start + n);
      });
    }
    p = q + 1;
  }
}

/**
 * Cambio de texto para mover el bloque `index` a la posición `slot`.
 *
 * `slot` va en coordenadas del documento ORIGINAL: 0 = antes del primer
 * bloque, `blocks.length` = después del último. Mover un bloque a donde ya
 * está (`slot === index` o `slot === index + 1`) no es un movimiento: null.
 *
 * `cursor` (opcional) es la posición que hay que conservar dentro del bloque
 * movido: se mantiene el mismo desplazamiento desde su principio, para que
 * Alt+↑ repetido siga moviendo el mismo bloque y no salte al de al lado.
 *
 * Devuelve `{from, to, insert, cursor, changes}` (un solo cambio) o null.
 */
export function moveBlockEdits({ text, blocks, index, slot, cursor = null }) {
  const s = String(text ?? "");
  if (!Array.isArray(blocks) || blocks.length < 2) return null;
  const n = blocks.length;
  const i = Number(index);
  const at = Number(slot);
  if (!Number.isInteger(i) || i < 0 || i >= n) return null;
  if (!Number.isInteger(at) || at < 0 || at > n) return null;
  if (at === i || at === i + 1) return null;

  const gapOf = (a, b) => s.slice(blocks[a].to, blocks[b].from);
  // Los huecos que se reconstruyen: los dos que rodean al bloque que se va y
  // el del destino (se parte en dos para meter el bloque en medio). Si alguno
  // lleva texto -- un comentario HTML vive ahí y no es bloque para el árbol --
  // no se mueve nada.
  const touched = [];
  if (i > 0) touched.push(gapOf(i - 1, i));
  if (i + 1 < n) touched.push(gapOf(i, i + 1));
  if (at > 0 && at < n) touched.push(gapOf(at - 1, at));
  if (touched.some((gap) => gap.trim() !== "")) return null;

  const order = [];
  for (let k = 0; k < n; k++) if (k !== i) order.push(k);
  order.splice(at > i ? at - 1 : at, 0, i);

  const texts = blocks.map((b) => s.slice(b.from, b.to));
  renumberMovedRuns(order, texts, orderedRunMap(s, blocks), blocks);

  // Entre dos bloques que ya eran vecinos se conserva SU hueco. Un
  // intercambio invierte el par (el de abajo pasa a estar encima), y ese par
  // sigue siendo la misma frontera: si solo se mirara `b === a + 1`, subir un
  // bloque un puesto reconstruía el hueco con el separador de siempre y
  // `alfa\n\n\n\nbeta` volvía como `beta\n\nalfa` (dos líneas a la basura).
  const sepFor = (a, b) => {
    if (b === a + 1) return gapOf(a, b);
    if (a === b + 1) return gapOf(b, a);
    return freshSeparator(blocks[a], blocks[b]);
  };

  let insert = "";
  let movedAt = 0;
  for (let p = 0; p < n; p++) {
    const k = order[p];
    if (k === i) movedAt = insert.length;
    insert += texts[k];
    if (p + 1 < n) insert += sepFor(k, order[p + 1]);
  }

  const from = blocks[0].from;
  const to = blocks[n - 1].to;
  // El cursor se queda a la misma altura DENTRO del bloque movido (si venía de
  // otro sitio, al principio). El bloque puede haber cambiado de número, así
  // que se recorta a lo que ocupe ahora.
  const wanted = cursor === null || cursor === undefined ? blocks[i].from : Number(cursor);
  const offset = Math.max(0, Math.min(wanted - blocks[i].from, texts[i].length));
  return {
    from,
    to,
    insert,
    cursor: from + movedAt + offset,
    changes: [{ from, to, insert }],
  };
}

/**
 * Cambio para el «+» del asa y para «Insertar bloque arriba» del menú:
 * un bloque nuevo vacío encima (`above`) o debajo (`below`) del bloque
 * `index`, con el cursor dentro.
 *
 * El hueco que se inserta es un TRAMO DE SALTOS y el cursor va dentro, de modo
 * que la primera línea que se teclee quede separada de sus dos vecinos. Hacen
 * falta dos saltos a cada lado: entre párrafos la frontera ya tiene su línea
 * en blanco (`\n\n`), pero entre dos ítems de la MISMA lista solo hay un
 * salto, y ahí un par pegado al ítem de arriba convertía lo tecleado en una
 * línea de continuación suya. Por eso se cuentan los saltos que ya hay y se
 * añaden los que falten:
 *
 * - encima: los de delante del bloque (`1. uno\n|2. dos` -> `\n\n\n`, con
 *   el cursor tras los dos primeros) y siempre dos detrás;
 * - debajo: siempre dos delante y los que falten detrás (`- alfa|\n- beta`
 *   -> `\n\n\n`, con el cursor tras los dos primeros).
 *
 * El tramo entero es el hueco del bloque nuevo: el editor lo colapsa si se
 * abandona sin escribir (`emptyGapRange`, body.js, admite tramos de más de
 * dos saltos), así que no quedan líneas en blanco de más en el `.md`.
 *
 * Devuelve `{changes, cursor, gapFrom, gapTo}`: `[gapFrom, gapTo)` es el tramo
 * insertado en el documento nuevo, que es la marca que espera `addEmptyGap`.
 */
export function insertBlockEdit({ text, blocks, index, where = "below" }) {
  if (!Array.isArray(blocks)) return null;
  const k = Number(index);
  if (!Number.isInteger(k) || k < 0 || k >= blocks.length) return null;
  const s = String(text ?? "");
  const block = blocks[k];
  if (where === "above") {
    const prev = k > 0 ? blocks[k - 1] : null;
    const before = prev ? newlinesBefore(s, block.from, prev.to) : 0;
    const lead = prev ? Math.max(0, 2 - before) : 0;
    const insert = "\n".repeat(lead + 2);
    return {
      changes: [{ from: block.from, to: block.from, insert }],
      cursor: block.from + lead,
      gapFrom: block.from,
      gapTo: block.from + insert.length,
    };
  }
  const next = k + 1 < blocks.length ? blocks[k + 1] : null;
  const after = next ? newlinesAfter(s, block.to, next.from) : 0;
  const tail = next ? Math.max(0, 2 - after) : 0;
  const insert = "\n".repeat(2 + tail);
  const at = block.to;
  return {
    changes: [{ from: at, to: at, insert }],
    cursor: at + 2,
    gapFrom: at,
    gapTo: at + insert.length,
  };
}
