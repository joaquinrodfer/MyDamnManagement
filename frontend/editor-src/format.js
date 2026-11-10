// Negrita, cursiva y subrayado (⌘/Ctrl + B / I / U): lógica pura, sin
// CodeMirror. Igual que block-keys.js: aquí se decide QUÉ cambia en el
// documento y dónde queda la selección; aplicarlo (una transacción) es cosa de
// entry.js. Así se prueba con `node --test`, sin navegador.
//
// Decisiones (las que el enunciado dejaba abiertas), todas cerradas:
//
//  1. La marca se PONE y se QUITA con la misma tecla (alternancia):
//     - con selección, se envuelve y la selección se queda sobre el texto
//       (dentro de las marcas, no encima de ellas);
//     - si ya estaba envuelta -- o la selección incluye las propias marcas --
//       se quitan las marcas y la selección vuelve a cubrir el texto limpio.
//       Si entre la marca y el texto había espacios, se van con ella: al
//       quitar no queda un hueco dentro (es lo que pasa en `** hola **`).
//     - sin selección (cursor), se escribe el par vacío con el cursor en medio
//       (`**|**`), y otra pulsación lo quita.
//  2. Espacios de los bordes: los de los extremos de la selección se quedan
//     FUERA de la marca. No es cosmética: en Markdown `** hola **` no es
//     negrita (el delimitador no puede ir pegado a un espacio).
//  3. Varias líneas: se envuelve CADA LÍNEA por separado. Meter marcas a
//     través de un salto de línea partiría el párrafo en dos y, en una lista,
//     dejaría el marcador dentro. Todo va en una sola lista de cambios, así
//     que es una transacción y un solo ⌘Z.
//  4. El prefijo de la línea (cita `> `, viñeta `- `, número `1. `, título
//     `# `) NO se envuelve: `- **uno**`, no `**- uno**`. Se respeta la
//     sangría que haya delante.
//  5. Dentro de código no se toca nada: ni con el cursor ni con la selección
//     en un bloque de vallas, en código sangrado o en un `código` en línea.
//     En ese caso `formatEdit` devuelve null y quien llama se come la tecla
//     (el atajo no hace nada, pero tampoco la deja al navegador).
//  6. Subrayado no es `__x__` (en Markdown eso es negrita): se escribe
//     `<u>x</u>`, que es lo que renderizan Obsidian y GitHub. El `.md` dice
//     exactamente lo que se ve.

export const FORMATS = {
  bold: { open: "**", close: "**" },
  italic: { open: "*", close: "*" },
  underline: { open: "<u>", close: "</u>" },
};

const SPACE_RE = /[ \t]/;
// Prefijo de línea que no es texto de la línea: cita, viñeta, número, título.
const QUOTE_PREFIX_RE = /^(?:[ \t]*>[ \t]?)+/;
const MARKER_PREFIX_RE = /^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+|#{1,6}[ \t]+)/;

function isSpaceAt(text, i) {
  return i >= 0 && i < text.length && SPACE_RE.test(text[i]);
}

/** Retrocede sobre espacios desde `pos` (sin bajar de `floor`). */
function skipSpacesLeft(text, pos, floor) {
  let p = pos;
  while (p > floor && isSpaceAt(text, p - 1)) p--;
  return p;
}

/** Avanza sobre espacios desde `pos` (sin pasar de `ceil`). */
function skipSpacesRight(text, pos, ceil) {
  let p = pos;
  while (p < ceil && isSpaceAt(text, p)) p++;
  return p;
}

/**
 * Primer carácter de contenido de una fila: salta el prefijo de cita y el
 * marcador de lista/título. Solo se usa cuando el tramo empieza la fila.
 */
function contentStart(text, lineFrom, lineTo) {
  const line = text.slice(lineFrom, lineTo);
  const quote = QUOTE_PREFIX_RE.exec(line);
  const afterQuote = quote ? quote[0].length : 0;
  const rest = line.slice(afterQuote);
  const marker = MARKER_PREFIX_RE.exec(rest);
  return lineFrom + afterQuote + (marker ? marker[0].length : 0);
}

function inCode(codeRanges, from, to) {
  return codeRanges.some((c) => from >= c.from && to <= c.to);
}

function insertAt(pos, insert) {
  return { from: pos, to: pos, insert };
}

function deleteRange(from, to) {
  return { from, to, insert: "" };
}

/**
 * Posición `p` en el documento nuevo. `assoc` > 0 cuenta lo que se inserta
 * justo en `p` como si fuera antes (el texto queda detrás); `assoc` < 0 no.
 */
function mapPos(p, changes, assoc) {
  let shift = 0;
  for (const c of changes) {
    if (c.to < p) shift += c.insert.length - (c.to - c.from);
    else if (c.to === p) {
      if (c.from < p) shift += c.insert.length - (c.to - c.from);
      else if (assoc > 0) shift += c.insert.length;
    }
  }
  return p + shift;
}

/**
 * Un tramo de una sola línea: quitar si ya estaba envuelto, envolver si no.
 *
 * @returns {{changes: Array<{from:number,to:number,insert:string}>,
 *            core: {from:number,to:number}}|null} null si ese tramo no se toca
 *   (vacío o solo espacios, o dentro de código).
 */
function segmentEdit(text, segFrom, segTo, lineFrom, lineTo, mark, codeRanges) {
  // El prefijo de la línea (`> `, `- `, `1. `, `# `) no es texto que envolver.
  let from = segFrom <= lineFrom ? contentStart(text, lineFrom, segTo) : segFrom;
  from = skipSpacesRight(text, from, segTo);
  const to = skipSpacesLeft(text, segTo, from);
  if (from >= to) return null; // nada que envolver (fila vacía, o solo el marcador)
  if (inCode(codeRanges, from, to)) return null;

  // (a) La selección YA incluye las marcas: `**hola**` seleccionado entero.
  if (to - from >= mark.open.length + mark.close.length &&
      text.startsWith(mark.open, from) &&
      text.endsWith(mark.close, to)) {
    return {
      changes: [
        deleteRange(from, from + mark.open.length),
        deleteRange(to - mark.close.length, to),
      ],
      core: { from: from + mark.open.length, to: to - mark.close.length },
    };
  }

  // (b) Las marcas están justo FUERA (con o sin espacios entre medias):
  // `**hola**` con "hola" seleccionado, o `** hola **`.
  const outerFrom = skipSpacesLeft(text, from, lineFrom);
  const outerTo = skipSpacesRight(text, to, lineTo);
  const openFrom = outerFrom - mark.open.length;
  const closeTo = outerTo + mark.close.length;
  if (openFrom >= lineFrom &&
      closeTo <= lineTo &&
      text.slice(openFrom, outerFrom) === mark.open &&
      text.slice(outerTo, closeTo) === mark.close) {
    return {
      // Los espacios de dentro se van con la marca: al quitar no queda hueco.
      changes: [
        deleteRange(openFrom, from),
        deleteRange(to, closeTo),
      ],
      core: { from, to },
    };
  }

  // (c) Envolver.
  return {
    changes: [insertAt(from, mark.open), insertAt(to, mark.close)],
    core: { from, to },
  };
}

/**
 * Cursor sin selección: par vacío con el cursor en medio, o quitarlo si ya
 * está justo en medio de ese par.
 */
function cursorEdit(text, pos, mark, codeRanges) {
  if (inCode(codeRanges, pos, pos)) return null;
  const openFrom = pos - mark.open.length;
  const closeTo = pos + mark.close.length;
  const already =
    openFrom >= 0 &&
    text.slice(openFrom, pos) === mark.open &&
    text.slice(pos, closeTo) === mark.close &&
    !inCode(codeRanges, openFrom, closeTo);
  if (already) {
    return {
      changes: [deleteRange(openFrom, pos), deleteRange(pos, closeTo)],
      core: { from: pos, to: pos, cursor: true, extra: 0 },
    };
  }
  // Un solo cambio (no dos inserciones en la misma posición): así también vale
  // en un documento vacío y al final del texto.
  return {
    changes: [insertAt(pos, mark.open + mark.close)],
    core: { from: pos, to: pos, cursor: true, extra: mark.open.length },
  };
}

/**
 * Cambio de texto que aplica ⌘/Ctrl+B / I / U.
 *
 * @param {Object} opts
 * @param {string} opts.text        documento completo
 * @param {Array<{from:number,to:number}>} opts.ranges  selección (uno o varios
 *        rangos; `from === to` es un cursor)
 * @param {"bold"|"italic"|"underline"} opts.kind
 * @param {Array<{from:number,to:number}>} [opts.codeRanges] zonas de código
 *        (vallas, código sangrado y `código` en línea): ahí no se aplica nada
 * @returns {{changes: Array<{from:number,to:number,insert:string}>,
 *            selection: Array<{from:number,to:number}>}|null}
 *   null si no hay nada que hacer (todo dentro de código): la tecla se come
 *   igual, pero el documento no cambia.
 *   `changes` van en coordenadas del documento ORIGINAL y `selection` en las
 *   del nuevo (listo para `EditorSelection.create`).
 */
export function formatEdit({ text, ranges, kind, codeRanges = [] }) {
  const mark = FORMATS[kind];
  if (!mark || !ranges || !ranges.length) return null;

  const changes = [];
  const cores = [];

  for (const r of ranges) {
    const from = Math.min(r.from, r.to);
    const to = Math.max(r.from, r.to);

    if (from === to) {
      const edit = cursorEdit(text, from, mark, codeRanges);
      if (!edit) continue;
      changes.push(...edit.changes);
      cores.push(edit.core);
      continue;
    }

    // Varias líneas: un tramo por línea, sin marcas a través del salto.
    let pos = from;
    const touched = [];
    while (pos <= to) {
      const nl = text.indexOf("\n", pos);
      const lineTo = nl === -1 ? text.length : nl;
      const lineFrom = text.lastIndexOf("\n", pos - 1) + 1;
      const segTo = Math.min(to, lineTo);
      if (segTo > pos) {
        const seg = segmentEdit(text, pos, segTo, lineFrom, lineTo, mark, codeRanges);
        if (seg) {
          changes.push(...seg.changes);
          touched.push(seg.core);
        }
      }
      if (lineTo >= to) break;
      pos = lineTo + 1;
    }
    if (touched.length) {
      cores.push({ from: touched[0].from, to: touched[touched.length - 1].to });
    }
  }

  if (!cores.length) return null;

  changes.sort((a, b) => a.from - b.from || a.to - b.to);
  const selection = cores.map((core) => {
    if (core.cursor) {
      const at = mapPos(core.from, changes, -1) + core.extra;
      return { from: at, to: at };
    }
    return { from: mapPos(core.from, changes, 1), to: mapPos(core.to, changes, -1) };
  });
  return { changes, selection };
}

/**
 * Subrayados `<u>…</u>` de un trozo de texto de UNA línea (o varias: los que
 * crucen un salto no cuentan). Devuelve las etiquetas y el texto subrayado por
 * separado, que es lo que entry.js pinta: etiquetas ocultas (o tenues mientras
 * se edita la línea) y el texto con el subrayado.
 *
 * El parser de Markdown entrega `<u>` y `</u>` como dos nodos HTMLTag sueltos
 * -- no envuelven nada --, así que emparejarlos es cosa de este escaneo.
 *
 * @param {string} chunk texto (el trozo visible del documento)
 * @param {number} [offset] posición de `chunk[0]` en el documento
 * @returns {Array<{openFrom:number,openTo:number,innerFrom:number,innerTo:number,
 *                  closeFrom:number,closeTo:number}>} posiciones en el documento
 */
export function underlineSpans(chunk, offset = 0) {
  const spans = [];
  const re = /<u>([^\n]*?)<\/u>/g;
  let m;
  while ((m = re.exec(chunk))) {
    const openFrom = offset + m.index;
    const openTo = openFrom + 3; // "<u>"
    const innerFrom = openTo;
    const innerTo = innerFrom + m[1].length;
    spans.push({ openFrom, openTo, innerFrom, innerTo, closeFrom: innerTo, closeTo: innerTo + 4 });
    re.lastIndex = m.index + m[0].length;
  }
  return spans;
}
