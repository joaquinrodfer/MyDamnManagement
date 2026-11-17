// Tab y ⇧Tab en una lista: meter un ítem dentro del de arriba o sacarlo un
// nivel, como en Notion. Lógica pura sobre el texto, sin CodeMirror (las
// pruebas están en test/list-indent.test.js).
//
// - Tab mete el ítem (con todo lo que cuelga de él) debajo del ítem hermano de
//   arriba: se sangra hasta donde empieza el texto de ese hermano. Si no hay
//   hermano arriba (es el primero de su lista), no pasa nada.
// - ⇧Tab lo saca al nivel de su padre. Los hermanos que venían detrás pasan a
//   colgar de él, que es lo que lee Markdown con la sangría que tienen.
// - En una numerada, los números se rehacen: el ítem que entra en un nivel
//   nuevo empieza en 1 (o sigue al último de ese nivel), y los de detrás en el
//   nivel de origen se corren para no dejar huecos.

const ITEM_RE = /^([ \t]*)(?:([-*+])|(\d+)([.)]))([ \t]+|$)/;

function parseItem(line) {
  const m = ITEM_RE.exec(line);
  if (!m) return null;
  return {
    indent: m[1].length,
    width: m[0].length, // de principio de línea a donde empieza el texto
    ordered: m[3] != null,
    num: m[3] != null ? parseInt(m[3], 10) : null,
    numFrom: m[1].length,
    numTo: m[3] != null ? m[1].length + m[3].length : null,
  };
}

const indentOf = (line) => /^[ \t]*/.exec(line)[0].length;
const isBlank = (line) => line.trim() === "";

/** Primera línea del ítem que contiene la línea `i` (la suya, o la del ítem
 * del que es continuación), o -1. */
function itemLineOf(lines, i) {
  for (let k = i; k >= 0; k--) {
    if (isBlank(lines[k])) return -1;
    if (parseItem(lines[k])) {
      if (k === i) return k;
      // Una continuación tiene que estar sangrada dentro del ítem.
      return indentOf(lines[i]) > parseItem(lines[k]).indent ? k : -1;
    }
  }
  return -1;
}

/** Última línea del ítem que empieza en `i`: lo que cuelga de él (más
 * sangrado), sin las líneas en blanco del final. */
function itemEnd(lines, i, indent) {
  let end = i;
  for (let k = i + 1; k < lines.length; k++) {
    if (isBlank(lines[k])) continue;
    if (indentOf(lines[k]) <= indent) break;
    end = k;
  }
  return end;
}

/** Cambia el número de la línea de ítem `line` por `num`. */
function withNumber(line, num) {
  const it = parseItem(line);
  if (!it || !it.ordered) return line;
  return line.slice(0, it.numFrom) + String(num) + line.slice(it.numTo);
}

/**
 * Renumera, desde la línea `from`, los ítems numerados que van seguidos al
 * nivel `indent` (saltando lo que cuelga de ellos), empezando por `start`.
 * Se para al salir de la lista: una línea menos sangrada, un ítem de viñetas
 * en ese nivel o un párrafo.
 */
function renumberRun(lines, from, indent, start) {
  let n = start;
  for (let k = from; k < lines.length; k++) {
    if (isBlank(lines[k])) continue;
    const ind = indentOf(lines[k]);
    if (ind > indent) continue;
    const it = parseItem(lines[k]);
    if (ind < indent || !it || !it.ordered) break;
    lines[k] = withNumber(lines[k], n++);
  }
}

/**
 * Tab (`dir` = 1) o ⇧Tab (`dir` = -1) con el cursor en `pos`.
 *
 * Devuelve `{from, to, insert, cursor}` (un solo cambio que cubre las líneas
 * tocadas), `{noop: true}` si el cursor está en una lista pero no hay a dónde
 * mover el ítem (la tecla se consume igual: en Notion no se sale del editor),
 * o `null` si el cursor no está en un ítem de lista.
 */
export function listIndentEdit(text, pos, dir) {
  const lines = text.split("\n");
  // Línea del cursor y desplazamiento dentro de ella.
  let at = 0;
  let row = 0;
  for (; row < lines.length; row++) {
    if (pos <= at + lines[row].length) break;
    at += lines[row].length + 1;
  }
  const col = pos - at;
  const i = itemLineOf(lines, row);
  if (i < 0) return null;
  const item = parseItem(lines[i]);
  const end = itemEnd(lines, i, item.indent);
  const out = lines.slice();
  let lastTouched = end;

  if (dir > 0) {
    // El hermano de arriba: el ítem anterior al mismo nivel.
    let sib = -1;
    for (let k = i - 1; k >= 0; k--) {
      if (isBlank(lines[k])) continue;
      const ind = indentOf(lines[k]);
      if (ind > item.indent) continue;
      if (ind === item.indent && parseItem(lines[k])) sib = k;
      break;
    }
    if (sib < 0) return { noop: true };
    const s = parseItem(lines[sib]);
    const delta = s.width - item.indent;
    if (delta <= 0) return { noop: true };
    const pad = " ".repeat(delta);
    for (let k = i; k <= end; k++) if (!isBlank(out[k])) out[k] = pad + out[k];
    if (item.ordered) {
      // En su nivel nuevo sigue al último numerado que ya colgara del hermano.
      let num = 1;
      for (let k = sib + 1; k < i; k++) {
        const it = parseItem(lines[k]);
        if (it && it.ordered && it.indent === s.width) num = it.num + 1;
      }
      out[i] = withNumber(out[i], num);
      // Y en el nivel de origen, los de detrás se corren uno.
      if (s.ordered) {
        renumberRun(out, end + 1, item.indent, s.num + 1);
        lastTouched = out.length - 1;
      }
    }
  } else {
    if (item.indent === 0) return { noop: true };
    // El padre: el primer ítem de arriba menos sangrado.
    let parent = -1;
    for (let k = i - 1; k >= 0; k--) {
      if (isBlank(lines[k])) continue;
      const it = parseItem(lines[k]);
      if (indentOf(lines[k]) < item.indent) {
        if (it) parent = k;
        break;
      }
    }
    if (parent < 0) return { noop: true };
    const p = parseItem(lines[parent]);
    const delta = item.indent - p.indent;
    for (let k = i; k <= end; k++) {
      if (isBlank(out[k])) continue;
      out[k] = out[k].slice(Math.min(delta, indentOf(out[k])));
    }
    if (item.ordered || p.ordered) {
      // Al nivel del padre: el número que sigue al suyo, y los de detrás en
      // ese nivel se corren.
      if (item.ordered) out[i] = withNumber(out[i], p.ordered ? p.num + 1 : 1);
      // Los hermanos que venían detrás cuelgan ahora de él: empiezan en 1.
      renumberRun(out, end + 1, item.indent, 1);
      if (item.ordered) {
        let k = end + 1;
        while (k < out.length && (isBlank(out[k]) || indentOf(out[k]) > p.indent)) k++;
        renumberRun(out, k, p.indent, parseItem(out[i]).num + 1);
      }
      lastTouched = out.length - 1;
    }
  }

  // Un solo cambio, de la línea del ítem a la última tocada.
  let fromPos = 0;
  for (let k = 0; k < i; k++) fromPos += lines[k].length + 1;
  let toPos = fromPos;
  for (let k = i; k <= lastTouched; k++) toPos += lines[k].length + (k < lastTouched ? 1 : 0);
  const insert = out.slice(i, lastTouched + 1).join("\n");
  if (insert === text.slice(fromPos, toPos)) return { noop: true };

  // El cursor se queda sobre el mismo carácter.
  let cursor = 0;
  for (let k = 0; k < row; k++) cursor += out[k].length + 1;
  const shift = out[row].length - lines[row].length;
  cursor += Math.max(0, Math.min(out[row].length, col + shift));
  return { from: fromPos, to: toPos, insert, cursor };
}
