// Movimiento de bloques: lógica pura, sin CodeMirror. Lo único de fuera es el
// parser de Markdown (`markdownLanguage.parser`): a él se le pregunta cuántos
// saltos necesita cada frontera, en vez de mantener una tabla de tipos.
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
// - Un bloque empieza en su LÍNEA, no en su primer carácter con contenido (ver
//   `lineStartOf` en block-ranges.js): la sangría -- los cuatro espacios de un
//   bloque de código indentado -- es SUYA y viaja con él. Dejándola en el hueco
//   se perdía en cuanto el hueco había que rehacerlo, y el bloque dejaba de ser
//   código (t_9d3b6d3b).
// - INVARIANTE: mover no cambia las líneas del `.md`, solo su orden. Un bloque
//   que se va de entre B y C no se lleva el aire que había entre B y C: la
//   frontera que dejan atrás sigue entre ellos, y los saltos de los huecos que
//   se rehacen se reparten sobre el documento entero (`planSeparators`), no
//   sobre el par origen/destino. El total de saltos no cambia nunca.
// - Ninguna frontera baja de lo mínimo que necesita su pareja de bloques (con
//   menos, Markdown leería los dos como uno: `- alfa\ntexto` es un solo ítem).
//   Si con el presupuesto que hay no cabe, el movimiento no se hace: antes no
//   mover que cambiar el `.md`. Tampoco se hace si NINGUNA línea en blanco
//   separa la pareja (dos bloques de código indentado seguidos son uno; un
//   bloque indentado detrás de un ítem es continuación del ítem).
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

import { markdownLanguage } from "@codemirror/lang-markdown";

import { blocksFromTree } from "./block-ranges.js";

const ORDERED_RE = /^([ \t]*)(\d+)([.)])/;

/**
 * Lo que devuelve `minNewlines` cuando NINGÚN número de líneas en blanco separa
 * la pareja: no hay frontera que escribir, así que el movimiento no se hace.
 */
const NO_SEPARATION = 4;
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
 * El hueco que va entre dos bloques con el número de saltos pedido, sin tirar
 * lo que se pueda conservar: un hueco puede llevar espacios en sus líneas «en
 * blanco» (`a\n\n   \n\nb`) y esos espacios son texto del `.md` como
 * cualquier otro. Si faltan saltos se añaden al final (la línea en blanco queda
 * pegada al bloque de abajo); si sobran, se quitan líneas en blanco enteras,
 * empezando por las que no llevan nada, para que las que llevan espacios duren
 * lo que puedan.
 */
function fitGap(gap, count) {
  const parts = gap.split("\n");
  const have = parts.length - 1;
  if (count === have) return gap;
  if (count > have) return gap + "\n".repeat(count - have);
  let left = have - count;
  for (const blankOnly of [true, false]) {
    for (let i = parts.length - 2; i >= 1 && left > 0; i--) {
      if (parts.length <= 2) break;
      if (blankOnly ? parts[i] !== "" : !/^[ \t]*$/.test(parts[i])) continue;
      parts.splice(i, 1);
      left--;
    }
  }
  // Un hueco de puros saltos no tiene nada que quitar de en medio: se rehace.
  return left === 0 ? parts.join("\n") : "\n".repeat(count);
}

/**
 * Los bloques que ve el parser en un documento, con su TEXTO (los rangos y los
 * tipos salen de `blocksFromTree`, la misma función que usa entry.js, para que
 * la medida no sea sobre bloques inventados).
 */
function parsedBlocks(doc) {
  const root = markdownLanguage.parser.parse(doc).topNode;
  return blocksFromTree(root, doc).map((b) => ({ type: b.type, text: doc.slice(b.from, b.to) }));
}

// Caché de mínimos por pareja (tipos + textos), con tope de entradas y de
// caracteres: no es un dato del documento, solo una memoización, así que
// tirarla no cambia ninguna respuesta. Un documento largo repite parejas entre
// movimientos y el segundo ya no parsea nada.
const MIN_CACHE = new Map();
const MIN_CACHE_MAX_ENTRIES = 4096;
const MIN_CACHE_MAX_CHARS = 1 << 20; // ~1 MB de textos
const MIN_CACHE_MAX_KEY = 1 << 15; // dos bloques de más de 16 KB no se cachean
let minCacheChars = 0;

/**
 * Saltos MÍNIMOS para que Markdown siga leyendo DOS bloques y no uno. Es el
 * suelo: por debajo de esto la frontera es incorrecta aunque haya que dejar de
 * mover.
 *
 * No hay tabla de tipos: se le PREGUNTA AL PARSER, que es el que decide. Para
 * la pareja concreta -- `bodyPrev` y `bodyNext` tal como van a quedar escritos
 * en el documento nuevo (ya reenumerados: un `2.` que se queda solo pasa a `1.`
 * y entonces sí interrumpe) -- se prueba un salto, luego dos y luego tres, y se
 * devuelve el primero con el que el parser sigue dando DOS bloques, del mismo
 * tipo y con el mismo texto cada uno. Con un salto ya salen dos no se prueba el
 * de dos (cortocircuito).
 *
 * Así no queda fuera ningún caso por la forma del bloque: un ítem vacío (`- `,
 * `1.`) no interrumpe un párrafo pero detrás de él basta un salto; la cita
 * vacía `>`; un `3.` que no empieza en 1; dos citas seguidas; un bloque HTML que
 * se come la línea de abajo; un `---` que detrás de un párrafo es una cabecera
 * setext. La regla no depende del tipo del nodo sino de cómo lo lee CommonMark
 * en ese contexto exacto, y eso solo lo sabe el parser.
 *
 * Hay parejas que NO se separan con ninguna línea en blanco, y entonces se
 * devuelve `NO_SEPARATION`: dos bloques de código indentado seguidos son UNO
 * (las líneas en blanco de en medio son contenido del código) y un bloque
 * indentado detrás de un ítem de lista es continuación del ítem. Quien llama
 * tiene que negarse a ponerlos juntos, no escribir un hueco que no separa:
 * `moveBlockEdits` devuelve null (antes no mover que cambiar el `.md`).
 */
export function minNewlines(prev, next, bodyPrev, bodyNext) {
  const a = String(bodyPrev ?? "");
  const b = String(bodyNext ?? "");
  const key = `${prev.type}\u0000${next.type}\u0000${a}\u0000${b}`;
  const cached = MIN_CACHE.get(key);
  if (cached !== undefined) return cached;

  let need = NO_SEPARATION;
  for (let gap = 1; gap <= 3; gap++) {
    const doc = a + "\n".repeat(gap) + b;
    const blocks = parsedBlocks(doc);
    if (
      blocks.length === 2 &&
      blocks[0].type === prev.type &&
      blocks[1].type === next.type &&
      blocks[0].text === a &&
      blocks[1].text === b
    ) {
      need = gap;
      break;
    }
  }

  if (key.length <= MIN_CACHE_MAX_KEY) {
    if (MIN_CACHE.size >= MIN_CACHE_MAX_ENTRIES || minCacheChars + key.length > MIN_CACHE_MAX_CHARS) {
      MIN_CACHE.clear();
      minCacheChars = 0;
    }
    MIN_CACHE.set(key, need);
    minCacheChars += key.length;
  }
  return need;
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

  // El texto de cada bloque en el documento nuevo, con las listas numeradas que
  // el movimiento ha reordenado ya reenumeradas: el mínimo de una frontera
  // depende del número (`2.` que se queda solo pasa a `1.` e interrumpe), así
  // que se mide sobre lo que de verdad va a quedar escrito.
  const texts = blocks.map((b) => s.slice(b.from, b.to));
  renumberMovedRuns(order, texts, orderedRunMap(s, blocks), blocks);

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
    const bd = { a, b, min: minNewlines(blocks[a], blocks[b], texts[a], texts[b]), gap: null, count: 0 };
    // Una pareja que no se separa con ninguna línea en blanco (dos bloques de
    // código indentado seguidos, un bloque indentado detrás de un ítem) no se
    // puede dejar vecina: escribir el hueco las fusionaría y el `.md` cambiaría.
    if (bd.min >= NO_SEPARATION) return null;
    if (k !== null && !kept.has(k)) {
      kept.add(k);
      bd.gap = gaps[k];
      // Se conserva el hueco, pero si se queda corto para la pareja al revés
      // se rellena: `- alfa\ntexto` acabaría siendo un solo bloque.
      bd.count = Math.max(countNewlines(bd.gap), bd.min);
    } else {
      // La frontera de siempre, pero nunca por debajo del mínimo de la pareja
      // (el parser puede pedir tres saltos para alguna).
      bd.count = Math.max(separatorNewlines(blocks[a], blocks[b]), bd.min);
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

  let insert = "";
  let movedAt = 0;
  for (let p = 0; p < n; p++) {
    const k = order[p];
    if (k === i) movedAt = insert.length;
    insert += texts[k];
    if (p + 1 < n) {
      const bd = bounds[p];
      // El hueco conservado se copia tal cual (con sus espacios, si los tenía)
      // mientras no haya que tocarlo; si hay que cambiarle los saltos, se
      // quitan o se añaden líneas en blanco sin tirar los espacios de las demás.
      insert += bd.gap !== null ? fitGap(bd.gap, bd.count) : "\n".repeat(bd.count);
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
