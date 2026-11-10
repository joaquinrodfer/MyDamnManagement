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

const HEADING_TYPES = new Set(["h1", "h2", "h3", "h4", "h5"]);

/**
 * Nombre corto de lo que hay que saber de un bloque para su frontera. `other`
 * agrupa cosas que el editor pinta igual (un bloque HTML, una tabla, una
 * referencia de enlace, una cabecera setext) pero que se pegan a sus vecinos de
 * forma muy distinta, así que se distinguen por el texto. Los ítems de lista se
 * separan en TRES especies porque el número manda: una lista ordenada solo
 * interrumpe lo que tiene encima si empieza en 1 (`3. tercero` pegado a un
 * párrafo es una línea más del párrafo, no un bloque nuevo).
 */
function frontierKind(block, body) {
  if (block.type === "other") return otherKind(body);
  if (block.type === "bullet_list_item") return "bullet_item";
  if (block.type === "ordered_list_item") {
    // El número se lee del texto del bloque (mismo `ORDERED_RE` que reenumera).
    // Si no se puede leer, el caso conservador es el que pide la línea en blanco.
    return itemNumber(body) === 1 ? "ordered_item_from_1" : "ordered_item_other";
  }
  if (HEADING_TYPES.has(block.type)) return "heading";
  if (block.type === "hr") return hrKind(body);
  return block.type; // paragraph, quote, code
}

/**
 * `---` no es lo mismo que `***`, `___` o `- - -`: pegado debajo de un párrafo
 * es el subrayado de una cabecera setext, así que necesita la línea en blanco;
 * los demás temas son líneas horizontales que cortan lo que tengan encima.
 */
function hrKind(body) {
  const line = (body.split("\n", 1)[0] || "").replace(/[ \t]+$/, "");
  return /^-+$/.test(line) ? "dashes" : "hr";
}

/** Qué es un bloque `other`, por su texto. */
function otherKind(body) {
  const head = body.replace(/^[ \t]*/, "");
  if (head.startsWith("<")) {
    // Un `<pre>`, un `<script>` o un `<?...?>` acaban en su cierre (condiciones
    // 1-5 de CommonMark): detrás de ellos un salto basta. El resto de bloques
    // HTML (6-7) se come las líneas de debajo hasta la línea en blanco.
    if (/^<(script|pre|style|textarea)([ \t>]|$)/i.test(head)) return "html-closed";
    if (/^<[?]|^<![A-Za-z]|^<!\[CDATA\[/.test(head)) return "html-closed";
    return "html";
  }
  if (/^\[[^\]]*\]:/.test(head)) return "ref"; // LinkReference (`[ref]: url`)
  // Cabecera setext: ACABA en su subrayado (`---` o `===`), y el texto de encima
  // puede ser de varias líneas ("titulo uno\ntitulo dos\n---" es una sola
  // cabecera). Como una cabecera, acaba en su línea; una tabla nunca termina en
  // un subrayado (sus filas llevan barras).
  const last = body.split("\n").pop() ?? "";
  if (/^[ \t]{0,3}(=+|-+)[ \t]*$/.test(last)) return "setext";
  return "table"; // Table (y cualquier otro `other`: se trata como tabla)
}

// Nombres de etiqueta que empiezan un bloque HTML «de verdad» (condición 6 de
// CommonMark). Es la lista del estándar: un nombre que no esté aquí es una
// etiqueta suelta (condición 7) y esa NO corta un párrafo.
const HTML_BLOCK_TAGS = new Set(
  (
    "address article aside base basefont blockquote body caption center col colgroup dd details " +
    "dialog dir div dl dt fieldset figcaption figure footer form frame frameset h1 h2 h3 h4 h5 h6 " +
    "head header hr html iframe legend li link main menu menuitem nav noframes ol optgroup option p " +
    "param search section summary table tbody td tfoot th thead title tr track ul"
  ).split(" ")
);

/**
 * ¿Este bloque HTML corta un párrafo pegado encima? Las condiciones 1-6 de
 * CommonMark sí; la 7 (una etiqueta suelta de nombre desconocido, `<x-foo>`) no,
 * y entonces hace falta la línea en blanco igual que delante de un párrafo.
 */
function htmlInterrupts(body) {
  const line = (body.split("\n", 1)[0] || "").replace(/^[ \t]{0,3}/, "");
  if (/^<(script|pre|style|textarea)([ \t>]|$)/i.test(line)) return true;
  if (/^<[?]|^<![A-Za-z]|^<!\[CDATA\[/.test(line)) return true;
  const tag = /^<\/?([A-Za-z][A-Za-z0-9-]*)([ \t]|\/?>|$)/.exec(line);
  return !!tag && HTML_BLOCK_TAGS.has(tag[1].toLowerCase());
}

// Parejas que necesitan la LÍNEA EN BLANCO aunque lo de abajo empiece un bloque
// que normalmente se separa con un salto: lo de arriba se continúa con la
// primera línea de abajo (un párrafo, una cita o un ítem se la comen) o la lee
// como parte de sí mismo (una tabla, donde `---` es la fila de guiones).
//
// `ordered_item_other` (una lista numerada que NO empieza en 1) pide la línea en
// blanco detrás de un párrafo, una cita, un ítem de viñeta o una tabla, porque
// no interrumpe: `3. tercero` con un salto es la línea siguiente de ese
// párrafo/cita/ítem, y detrás de una tabla una fila más. Entre dos ítems de la
// misma lista numerada, en cambio, basta un salto (la lista sigue). Medido con
// el parser pareja por pareja: ver `minNewlines` y la tabla de
// test/block-move-property.test.js.
const NEEDS_BLANK_AFTER = {
  paragraph: new Set(["paragraph", "setext", "dashes", "ref", "ordered_item_other"]),
  quote: new Set(["paragraph", "setext", "ref", "quote", "ordered_item_other"]),
  bullet_item: new Set(["paragraph", "setext", "ref", "ordered_item_other"]),
  // Entre dos ítems de la misma lista numerada basta un salto, empiece en 1 o
  // no: `1. a\n3. b` son dos ítems de UNA lista (los números no tienen por qué
  // ser consecutivos para el parser).
  ordered_item_from_1: new Set(["paragraph", "setext", "ref"]),
  ordered_item_other: new Set(["paragraph", "setext", "ref"]),
  table: new Set(["paragraph", "setext", "dashes", "table", "ref", "ordered_item_other"]),
};

// Tipos que ACABAN en su propia línea: detrás de ellos un salto basta, también
// delante de un `---` (que detrás de un párrafo sería una cabecera setext, pero
// aquí ya no tiene párrafo que subrayar).
const CLOSED = new Set(["heading", "setext", "hr", "dashes", "code", "ref", "html-closed"]);

/**
 * Saltos MÍNIMOS para que Markdown siga leyendo DOS bloques y no uno. Es el
 * suelo: por debajo de esto la frontera es incorrecta aunque haya que dejar de
 * mover. Se mide por PAREJA (no solo mirando el de abajo) y con el parser de
 * verdad, bloque por bloque y por los dos lados: la tabla de
 * test/block-move-property.test.js comprueba que lo que dice esto es lo que
 * hace `markdownLanguage.parser`, probando un salto y dos en cada pareja.
 *
 * Lo que salió de medirlo:
 *
 * - detrás de un bloque HTML de los que van hasta la línea en blanco, SIEMPRE
 *   dos: se come las líneas de debajo (`<div>x</div>` + `# titulo` con un salto
 *   es UN bloque). Un `<pre>`/`<script>`/`<?...?>` acaba en su cierre y no;
 * - dos citas con un salto son UNA cita (`quote -> quote`: dos);
 * - detrás de una tabla, dos delante de un párrafo, un `---`, otra tabla o una
 *   referencia de enlace: cualquier línea que no sea fila la corta, pero `---`
 *   y las filas se leen como parte de la tabla;
 * - detrás de un párrafo, una cita o un ítem, dos delante de un párrafo, otra
 *   cita, una referencia de enlace o un `---` (detrás de un párrafo, `---` es
 *   una cabecera setext; detrás de una cita o un ítem, una línea suelta sigue
 *   siendo suya);
 * - detrás de una cabecera, una valla, un `---`, una referencia o una cabecera
 *   setext, uno basta: acaban en su línea (la tabla anterior pedía dos delante
 *   de un `---` y no hacía falta, y eso hacía que `Alt+↑` no hiciera nada);
 * - una lista ordenada solo interrumpe lo de arriba si empieza en 1: `3.` y
 *   `7)` piden la línea en blanco detrás de un párrafo, una cita, un ítem o una
 *   tabla (entre dos ítems de la misma lista numerada basta un salto).
 *
 * `bodyPrev` y `bodyNext` son los textos de los dos bloques TAL COMO VAN A
 * QUEDAR en el documento nuevo (ya reenumerados: un `2.` que se queda solo pasa
 * a `1.` y entonces sí interrumpe), no como estaban.
 */
export function minNewlines(prev, next, bodyPrev, bodyNext) {
  const a = frontierKind(prev, bodyPrev);
  const b = frontierKind(next, bodyNext);
  if (a === "html") return 2; // se come la línea siguiente
  if (CLOSED.has(a)) return 1; // acaba en su línea: no se continúa con nada
  const blank = NEEDS_BLANK_AFTER[a];
  if (!blank) return 2; // tipo que no se conoce: antes dos que uno
  if (b === "html" && !htmlInterrupts(bodyNext)) return 2;
  return blank.has(b) ? 2 : 1;
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
