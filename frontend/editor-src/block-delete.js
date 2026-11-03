// Borrado de bloques: lógica pura, sin CodeMirror.
//
// Un bloque no es un objeto del modelo: es un rango de texto calculado con el
// árbol de Markdown (ver `computeBlocks` en entry.js). Borrar un bloque es,
// por tanto, decidir QUÉ trozo de texto desaparece -- eso se calcula aquí y se
// prueba con `node --test`; aplicarlo (una transacción de CodeMirror) es cosa
// de entry.js. Mismo patrón que block-keys.js y link-create.js.
//
// La regla, cerrada con el dueño: borrar un bloque se lleva el bloque **y su
// separador**, así que el `.md` no gana líneas en blanco (D1: `alfa\n\nbeta\n\ngamma`
// menos `beta` es `alfa\n\ngamma`, nunca `alfa\n\n\n\ngamma`). En una lista, el
// ítem borrado desaparece con su salto de línea -- la lista sigue siendo UNA
// lista (D2) -- y los números que quedan se reenumeran desde el primero de la
// lista (decisión: el editor enseña el fuente, así que enseñar `1, 3, 4` sería
// mentir sobre cómo se lee la lista; Markdown ignora el número, pero el `.md`
// es lo que el usuario ve).
//
// Y el ⌫ al principio de un ítem (D9) fusiona con el ítem de arriba dejando un
// espacio (`- uno dos`), nunca `- uno- dos`.

const BULLET_RE = /^([ \t]*)([-*+])[ \t]+/;
const ORDERED_RE = /^([ \t]*)(\d+)([.)])[ \t]+/;

function isSpace(ch) {
  return ch === " " || ch === "\t" || ch === "\n";
}

function onlyWhitespace(s) {
  return /^[ \t\n]*$/.test(s);
}

/** Fila lógica que contiene `pos` (el `\n` de `pos` cuenta como parte de ella). */
function lineAt(text, pos) {
  const from = text.lastIndexOf("\n", pos - 1) + 1;
  const nl = text.indexOf("\n", pos);
  return { from, to: nl === -1 ? text.length : nl };
}

/**
 * Trozo de texto que se lleva borrar los bloques `a..b` (índices consecutivos
 * ya fusionados), separadores incluidos:
 *
 * - si queda algún bloque detrás, se lleva todo lo que hay entre el bloque y
 *   el siguiente que sobrevive (que son solo saltos de línea: el hueco de D1);
 * - si no queda nada detrás, se lleva además el separador de delante y el
 *   hueco final del documento, para que el `.md` no acabe en líneas en blanco.
 */
function deletedSpan(text, blocks, a, b) {
  let from = blocks[a].from;
  let to = blocks[b].to;
  if (b + 1 < blocks.length) {
    const next = blocks[b + 1].from;
    if (next >= to && onlyWhitespace(text.slice(to, next))) to = next;
    return { from, to };
  }
  if (to < text.length && onlyWhitespace(text.slice(to))) to = text.length;
  let p = from;
  while (p > 0 && isSpace(text[p - 1])) p--;
  from = p;
  return { from, to };
}

/**
 * Cambios de texto para borrar (o sustituir) bloques.
 *
 * `blocks` es la lista de `{from, to, type}` que calcula entry.js, `indices`
 * los bloques marcados y `insert` el texto con el que se sustituyen (vacío =
 * borrar). Con `insert`, el texto entra en el sitio de los bloques marcados y
 * los separadores de alrededor se conservan (escribir encima de un bloque no
 * debe pegar el texto al vecino); sin él, se van también los separadores.
 *
 * Devuelve `{changes, cursor}` o `null` si no hay nada que hacer. `changes`
 * nunca se solapan y van en orden de documento.
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
  runs.forEach(([a, b], n) => {
    let change;
    if (n === 0 && insert) {
      // Sustituir: se queda el hueco de antes y el de después.
      change = { from: blocks[a].from, to: blocks[b].to, insert };
    } else {
      const { from, to } = deletedSpan(text, blocks, a, b);
      change = { from, to, insert: "" };
    }
    if (cursor === null) cursor = change.from + (change.insert || "").length;
    // Un trozo que empieza donde acaba el anterior (dos bloques pegados,
    // p. ej. dos ítems de lista sin línea en blanco en medio) se fusiona en
    // uno: ChangeSet rechaza dos cambios que se tocan por el mismo borde.
    const prev = changes[changes.length - 1];
    if (prev && change.from <= prev.to) {
      prev.to = Math.max(prev.to, change.to);
    } else {
      changes.push(change);
    }
  });

  return { changes, cursor };
}

/**
 * ⏎ (y ⇧⏎) con bloques marcados: los marcados se sustituyen por UN bloque
 * vacío en su sitio, con el cursor dentro y listo para escribir.
 *
 * Es el mismo trozo que borrar (`blockDeleteEdits` con `insert: ""`, bloque +
 * separador), pero dejando los dos saltos que convierten el hueco en un bloque
 * de verdad. Sin esto, ⏎ sobre un bloque marcado metía su salto DENTRO del
 * rango marcado, entre los separadores que ya había: `uno / dos / tres` menos
 * `dos` acababa en `uno\n\n\n\n\n\ntres` (cinco líneas en blanco) y así se
 * guardaba en el `.md`.
 */
export function newBlockEdit({ text, blocks, indices }) {
  const edit = blockDeleteEdits({ text, blocks, indices });
  if (!edit) return null;
  const [first, ...rest] = edit.changes;
  // El bloque vacío es el hueco que queda ENTRE los dos separadores; dónde
  // cae el cursor depende de por dónde empieza el trozo borrado: si es el
  // principio del propio bloque, el hueco está justo ahí; si el trozo se
  // lleva además el separador de delante (borrar el último bloque), el hueco
  // empieza después de los dos saltos nuevos.
  const firstBlock = blocks[[...indices].map(Number).sort((a, b) => a - b)[0]];
  const cursor = firstBlock && first.from === firstBlock.from ? first.from : first.from + 2;
  return {
    changes: [{ from: first.from, to: first.to, insert: "\n\n" }, ...rest],
    cursor,
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
 * Devuelve cambios de texto que sustituyen solo el número (nunca el texto del
 * ítem) y no se solapan con los de `blockDeleteEdits` (empiezan justo donde
 * acaba el borrado del ítem anterior).
 */
export function orderedRenumberEdits({ text, blocks, indices }) {
  const del = new Set([...indices].map(Number));
  if (!del.size) return [];

  const changes = [];
  let i = 0;
  while (i < blocks.length) {
    if (blocks[i].type !== "ordered_list_item") {
      i++;
      continue;
    }
    // Una lista = ítems seguidos, cada uno en la línea siguiente al anterior.
    let end = i;
    while (
      end + 1 < blocks.length &&
      blocks[end + 1].type === "ordered_list_item" &&
      blocks[end + 1].from === blocks[end].to + 1
    ) {
      end++;
    }
    let touched = false;
    for (let k = i; k <= end; k++) if (del.has(k)) touched = true;

    if (touched) {
      const firstLine = text.slice(blocks[i].from, blocks[i].to);
      const firstMark = ORDERED_RE.exec(firstLine);
      if (firstMark) {
        let number = Number(firstMark[2]);
        for (let k = i; k <= end; k++) {
          const line = text.slice(blocks[k].from, blocks[k].to);
          const mark = ORDERED_RE.exec(line);
          if (!mark) continue;
          if (!del.has(k)) {
            const at = blocks[k].from + mark[1].length;
            if (mark[2] !== String(number)) {
              changes.push({ from: at, to: at + mark[2].length, insert: String(number) });
            }
            number += 1;
          }
        }
      }
    }
    i = end + 1;
  }
  return changes;
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
 * Devuelve `{from, to, insert, cursor}` o `null` cuando no aplica (no estamos
 * al principio de la fila, la fila no es un ítem, o estamos en la posición 0).
 */
export function backspaceJoinEdit({ text, pos }) {
  const line = lineAt(text, pos);
  if (pos !== line.from) return null;

  const source = text.slice(line.from, line.to);
  const marker = BULLET_RE.exec(source) || ORDERED_RE.exec(source);
  if (!marker) return null;

  const markerEnd = line.from + marker[0].length;
  const before = text.slice(0, line.from);

  if (line.from === 0) {
    // Primer ítem del documento: no hay arriba nada con lo que fusionar; se
    // queda como está (⌫ en la posición 0 no hace nada).
    return null;
  }

  const prevLine = lineAt(text, line.from - 1);
  const prevSource = text.slice(prevLine.from, prevLine.to);
  const prevIsItem = BULLET_RE.test(prevSource) || ORDERED_RE.test(prevSource);

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
