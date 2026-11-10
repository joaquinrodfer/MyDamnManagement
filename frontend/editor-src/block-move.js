// Movimiento de bloques: lógica pura, sin CodeMirror.
//
// Un bloque no es un objeto del modelo: es un rango de texto calculado con el
// árbol de Markdown (ver `blocksFromTree` en block-ranges.js). Moverlo es, por
// tanto, decidir QUÉ trozo de texto cambia de sitio -- eso se calcula aquí y se
// prueba con `node --test`; aplicarlo (una transacción de CodeMirror) es cosa
// de entry.js. Mismo patrón que block-delete.js y block-keys.js.
//
// Reglas (las mismas fronteras que al borrar, ver block-delete.js):
//
// - Se mueve el bloque ENTERO, del `from` al `to` que da el árbol. Eso es lo
//   que deja intacta una valla de código (```): es un rango, no una línea, y
//   aunque el cursor esté dentro se mueve la valla con su apertura y su cierre.
// - INVARIANTE: mover no cambia las líneas del `.md`, solo su orden. Un bloque
//   que se va de entre B y C no se lleva el aire que había entre B y C: la
//   frontera que dejan atrás sigue entre ellos, y los saltos de los huecos que
//   se rehacen se reparten sobre el documento entero (`planSeparators`), no
//   sobre el par origen/destino. El total de saltos no cambia nunca.
// - Ninguna frontera baja de lo mínimo que necesita su pareja de bloques (con
//   menos, Markdown leería los dos como uno: `- alfa\ntexto` es un solo ítem).
//   Si con el presupuesto que hay no cabe, el movimiento no se hace: antes no
//   mover que cambiar el `.md`.
// - Un hueco que NO se puede conservar (su pareja de bloques ya no es vecina)
//   se rehace con la frontera de siempre: salto simple entre dos ítems de la
//   misma lista y línea en blanco en cualquier otro caso.
// - Un hueco que se rehace no puede llevar nada que no sea espacio (un
//   comentario HTML vive ahí y no es bloque para el árbol): preferible no
//   mover a perder ese texto.
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

/** Saltos que tiene un hueco (los huecos son solo saltos y espacios). */
function countNewlines(gap) {
  let n = 0;
  for (const ch of gap) if (ch === "\n") n++;
  return n;
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
 * Saltos de la frontera de siempre entre dos bloques que NO eran vecinos:
 * dos ítems del mismo tipo de lista van con un salto (siguen siendo UNA
 * lista); cualquier otro par, con una línea en blanco.
 */
function separatorNewlines(prev, next) {
  return isListItem(prev) && isListItem(next) && prev.type === next.type ? 1 : 2;
}

function freshSeparator(prev, next) {
  return "\n".repeat(separatorNewlines(prev, next));
}

/**
 * Tipos que empiezan bloque aunque estén pegados al de arriba: un marcador de
 * lista al principio de una línea es un ítem nuevo, y una cabecera, una valla
 * o una cita interrumpen lo que tengan encima. Con esos, un salto basta.
 * (Una línea horizontal NO: `texto\n---` es una cabecera setext, no un `---`.)
 */
const INTERRUPTING = new Set([
  "bullet_list_item",
  "ordered_list_item",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "code",
  "quote",
]);

/**
 * Saltos MÍNIMOS para que Markdown siga leyendo dos bloques y no uno. Es el
 * suelo: por debajo de esto la frontera es incorrecta aunque haya que dejar de
 * mover. Todo lo que no interrumpe (un párrafo, una tabla, un bloque HTML)
 * necesita la línea en blanco: pegado a un párrafo o a un ítem, su primer
 * renglón se lee como continuación.
 */
function minNewlines(prev, next) {
  return INTERRUPTING.has(next.type) ? 1 : 2;
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
 * También es null cuando no se puede mover sin cambiar las líneas del `.md`
 * (ver el invariante de arriba): es preferible no mover a reescribir aire.
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

  // Los huecos del documento original, uno por frontera: `gaps[k]` va del
  // final del bloque k al principio del k+1.
  const gaps = [];
  for (let k = 0; k + 1 < n; k++) gaps.push(s.slice(blocks[k].to, blocks[k + 1].from));

  // El orden nuevo, en índices del documento original.
  const order = [];
  for (let k = 0; k < n; k++) if (k !== i) order.push(k);
  order.splice(at > i ? at - 1 : at, 0, i);

  // Cada frontera del documento nuevo: qué hueco puede reutilizar tal cual (el
  // de la misma pareja de bloques, en el mismo orden o al revés -- un
  // intercambio deja la pareja al revés y su hueco sigue siendo el suyo) y
  // cuántos saltos necesita como mínimo. Cada hueco se va con una sola
  // frontera.
  const bounds = [];
  const kept = new Set();
  for (let p = 0; p + 1 < n; p++) {
    const a = order[p];
    const b = order[p + 1];
    const k = b === a + 1 ? a : a === b + 1 ? b : null;
    const bd = { a, b, min: minNewlines(blocks[a], blocks[b]), gap: null, count: 0 };
    if (k !== null && !kept.has(k)) {
      kept.add(k);
      bd.gap = gaps[k];
      // Se conserva el hueco, pero si se queda corto para la pareja al revés
      // se rellena: `- alfa\ntexto` acabaría siendo un solo bloque.
      bd.count = Math.max(countNewlines(bd.gap), bd.min);
    } else {
      bd.count = separatorNewlines(blocks[a], blocks[b]);
    }
    bounds.push(bd);
  }

  // Presupuesto: TODOS los saltos que hay entre bloques en este documento. Es
  // el aire que hay que repartir y es todo el que hay: mover no cambia el
  // total (lo que el bloque deja al irse acaba en otra frontera, no se tira).
  let budget = 0;
  for (let k = 0; k + 1 < n; k++) {
    if (!kept.has(k) && /\S/.test(gaps[k])) {
      // Un hueco que se rehace no puede llevar texto: un comentario HTML vive
      // en un hueco (no es bloque para el árbol) y se perdería.
      return null;
    }
    budget += countNewlines(gaps[k]);
  }

  let total = 0;
  for (const bd of bounds) total += bd.count;

  if (total > budget) {
    // No cabe. Se aprieta primero lo nuevo y luego lo conservado, pero nunca
    // por debajo del mínimo de cada frontera; si aun así no cabe, no se mueve.
    let over = total - budget;
    for (const onlyFresh of [true, false]) {
      for (const bd of bounds) {
        if (onlyFresh !== (bd.gap === null)) continue;
        const take = Math.min(bd.count - bd.min, over);
        if (take > 0) {
          bd.count -= take;
          over -= take;
        }
        if (over === 0) break;
      }
      if (over === 0) break;
    }
    if (over > 0) return null;
    total = budget;
  }

  if (total < budget) {
    // Sobra aire: se queda donde el bloque ha dejado su hueco, entre sus dos
    // ex-vecinos. Si estaba en un extremo (no hay hueco que juntar), al lado
    // de donde ha ido a parar.
    const target =
      bounds.find((bd) => bd.a === i - 1 && bd.b === i + 1) ||
      bounds.find((bd) => bd.a === i) ||
      bounds.find((bd) => bd.b === i);
    target.count += budget - total;
  }

  const texts = blocks.map((b) => s.slice(b.from, b.to));
  renumberMovedRuns(order, texts, orderedRunMap(s, blocks), blocks);

  let insert = "";
  let movedAt = 0;
  for (let p = 0; p < n; p++) {
    const k = order[p];
    if (k === i) movedAt = insert.length;
    insert += texts[k];
    if (p + 1 < n) {
      const bd = bounds[p];
      // El hueco conservado se copia tal cual (con sus espacios, si los tenía)
      // mientras no haya que tocarlo.
      insert += bd.gap !== null && countNewlines(bd.gap) === bd.count
        ? bd.gap
        : "\n".repeat(bd.count);
    }
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
