// Cuerpo canónico del documento: sin las líneas en blanco de los extremos.
//
// Decisión D8 (opción a): los bloques vacíos no existen. Un párrafo vacío no
// es un bloque en Markdown -- el `.md` se guarda sin ellos (ver
// `canonical_body` en backend/app/vault/store.py, que ya los tiraba al
// escribir) -- así que el editor deja de prometerlos: el documento se recorta
// en cuanto el cursor sale de ese hueco y lo que se guarda es lo que se ve.
//
// Se recortan TODOS los saltos de los extremos, no uno: el backend guarda un
// solo `\n` de separación tras el `---` y quita el resto, de modo que ni una
// ni dos líneas en blanco al principio sobreviven a una recarga (el caso
// `Intro Intro x` acaba en `x`). Recortar solo una dejaría el mismo salto
// invisible que esta tarjeta viene a quitar.
//
// Lo de en medio ESCRITO A MANO no se toca: una línea en blanco entre dos
// bloques es el separador y se guarda tal cual (`alfa\n\n\n\nbeta` de una nota
// vieja sigue siendo `alfa\n\n\n\nbeta` al recargar). Lo único que se colapsa
// en medio es el hueco que ha creado el propio editor -- el «+» del asa, o ⏎ al
// final de un bloque que tiene contenido detrás --, y solo mientras siga
// vacío y el cursor lo haya abandonado: `emptyGapRange` es la regla que lo
// comprueba y `collapseEmptyGaps` la que lo aplica.

/** Texto sin los saltos de línea de los extremos. */
export function canonicalBody(text) {
  return String(text ?? "")
    .replace(/^\n+/, "")
    .replace(/\n+$/, "");
}

/**
 * Dónde empieza y dónde acaba el contenido: el hueco de los extremos es
 * `[0, lead)` y `(end, length]`. `lead` es también la posición del primer
 * carácter de contenido y `end` la del último + 1.
 */
export function outerBlankRange(text) {
  const s = String(text ?? "");
  const lead = s.length - s.replace(/^\n+/, "").length;
  const end = s.replace(/\n+$/, "").length;
  return { lead, end };
}

/**
 * ¿Está `pos` dentro del hueco de los extremos? Es la condición para NO
 * recortar: mientras el cursor esté ahí, ese hueco es el bloque nuevo que se
 * está creando (Mayús+Intro o el «+»), y quitarlo bajo los dedos sería peor
 * que dejarlo colapsado. En cuanto el cursor sale, el hueco desaparece.
 */
export function cursorInOuterBlanks(text, pos) {
  const { lead, end } = outerBlankRange(text);
  return pos < lead || pos > end;
}

/**
 * Tramo de saltos de línea consecutivos que contiene `pos`, o null si ahí no
 * hay un salto. Sirve para saber hasta dónde llega el hueco en el que está el
 * cursor (un bloque vacío es un tramo de `\n` entre dos bloques).
 */
export function newlineRunAt(text, pos) {
  const s = String(text ?? "");
  const p = Math.max(0, Math.min(Number(pos) || 0, s.length));
  let from = p;
  while (from > 0 && s[from - 1] === "\n") from--;
  let to = p;
  while (to < s.length && s[to] === "\n") to++;
  return from === to ? null : { from, to };
}

/**
 * ¿Sigue el hueco `{from, to}` siendo el bloque vacío que creó el editor?
 *
 * Devuelve el tramo que hay que quitar para colapsarlo (los dos saltos que lo
 * forman) o null si ya no lo es. Lo es mientras:
 *
 * - el par de saltos siga tal cual en `text` (nadie ha escrito entre ellos), y
 * - a uno de los dos lados siga habiendo el salto que hacía de separador (el
 *   par se inserta delante o detrás de él, según el comando). Si a los dos
 *   lados hay texto, alguien escribió (o pegó) en el bloque nuevo y ese hueco
 *   ya es contenido.
 *
 * Quitar solo el par (y no los saltos de alrededor) es lo que deja intacto un
 * `alfa\n\n\n\nbeta` escrito a mano: el Enter que se pulse encima añade su par
 * y este se va al abandonarlo, pero los cuatro saltos de la nota siguen ahí.
 */
export function emptyGapRange(text, gap) {
  const s = String(text ?? "");
  const from = Number(gap?.from);
  const to = Number(gap?.to);
  if (!Number.isInteger(from) || !Number.isInteger(to)) return null;
  if (from < 0 || to > s.length || to - from !== 2) return null;
  if (s.slice(from, to) !== "\n\n") return null;
  if (to >= s.length) return null;
  // Basta con que uno de los lados siga siendo el salto separador: el «+» y
  // ⏎ al final de un bloque insertan el par DELANTE del separador que había y
  // ⏎ sobre un bloque marcado lo inserta DETRÁS. Si a los dos lados hay texto,
  // se ha escrito dentro del bloque nuevo y ese hueco ya es contenido.
  const before = from > 0 ? s[from - 1] : "";
  if (before !== "\n" && s[to] !== "\n") return null;
  return { from, to };
}

/**
 * Dónde queda el hueco `{from, to}` después de una transacción, o null si ya
 * no mide los dos saltos que lo formaban.
 *
 * `changes` son los cambios de la transacción en coordenadas del documento
 * VIEJO (`[{from, to, insert}]`, como los da `ChangeSet.iterChanges`). Las dos
 * puntas se desplazan con el sesgo que `mapPos` llama «hacia dentro», y es
 * justo el que hace falta aquí:
 *
 * - `from` se queda pegado al par (sesgo a la derecha): lo que se teclea
 *   justo en el borde de delante queda ANTES del hueco, así que la marca se
 *   corre con el texto y el par sigue midiendo dos saltos. Con el sesgo
 *   contrario, un segundo «+» en el mismo bloque (dos toques seguidos; el
 *   bloque vacío no se ve, así que es fácil) metía su par justo en `from`, la
 *   marca pasaba a medir 4 y se descartaba: el hueco dejaba de estar vigilado
 *   y el aire se quedaba en el `.md`.
 * - `to` se queda pegado al par (sesgo a la izquierda): lo que se escribe
 *   detrás del hueco no lo alarga -- `emptyGapRange` ve el texto nuevo y
 *   decide que ese hueco ya es contenido.
 *
 * Si un cambio cae DENTRO del par, el rango deja de medir 2 y la marca se
 * descarta: ya no es un bloque vacío, es texto.
 */
export function shiftEmptyGap(gap, changes) {
  let from = Number(gap?.from);
  let to = Number(gap?.to);
  if (!Number.isInteger(from) || !Number.isInteger(to)) return null;
  const list = [];
  for (const c of changes || []) {
    const cf = Number(c?.from);
    const ct = Number(c?.to);
    if (!Number.isInteger(cf) || !Number.isInteger(ct) || cf > ct) continue;
    list.push({ from: cf, to: ct, insert: String(c?.insert ?? "") });
  }
  // De atrás hacia delante: los cambios vienen en coordenadas del documento
  // viejo y los de más abajo no mueven las posiciones de los de más arriba.
  list.sort((a, b) => a.from - b.from);
  for (let i = list.length - 1; i >= 0; i--) {
    const c = list[i];
    const delta = c.insert.length - (c.to - c.from);
    if (from > c.to) from += delta;
    else if (from >= c.from) from = c.from + c.insert.length;
    if (to > c.to) to += delta;
    else if (to > c.from) to = c.from;
  }
  return to - from === 2 ? { from, to } : null;
}

/**
 * Texto y marcas de hueco después de quitar del documento el tramo
 * `{from, to}`.
 *
 * Sirve para el texto que se GUARDA cuando el selector de bases tiene algo
 * escrito: lo tecleado ahí es interfaz (el filtro), no contenido de la nota, y
 * tiene que quedarse fuera del `.md` pase lo que pase -- también si la pestaña
 * se cierra o se recarga de golpe, que es un guardado que no pasa por cerrar el
 * selector (ver `getValue` en entry.js).
 *
 * Por eso se resuelve sobre el texto y las marcas, sin despachar ninguna
 * transacción: lo que hay en pantalla no puede cambiar bajo los dedos cuando el
 * guardado sale con la pestaña todavía viva (`visibilitychange`).
 *
 * Las marcas se desplazan con `shiftEmptyGap`, la misma regla que aplica la
 * transacción de verdad: las que caen dentro del tramo dejan de medir dos
 * saltos y desaparecen, y las de detrás se corren lo que se ha quitado.
 */
export function withoutRange(text, gaps, range) {
  const s = String(text ?? "");
  const from = Math.max(0, Math.min(Number(range?.from) || 0, s.length));
  const to = Math.max(from, Math.min(Number(range?.to) || 0, s.length));
  if (to <= from) return { text: s, gaps: Array.from(gaps || []) };
  const changes = [{ from, to, insert: "" }];
  return {
    text: s.slice(0, from) + s.slice(to),
    gaps: Array.from(gaps || [])
      .map((gap) => shiftEmptyGap(gap, changes))
      .filter(Boolean),
  };
}

/**
 * Texto sin los bloques vacíos que creó el editor y que el usuario abandonó
 * sin escribir nada, más los tramos quitados (para despacharlos como un
 * cambio de CodeMirror). Los huecos de los extremos no salen aquí: esos los
 * lleva `canonicalBody`.
 *
 * Sin marcas (una nota que nadie ha editado) devuelve el texto tal cual: las
 * líneas en blanco de en medio se guardan como están.
 */
export function collapseEmptyGaps(text, gaps) {
  const s = String(text ?? "");
  const ranges = [];
  const seen = new Set();
  for (const gap of gaps || []) {
    const r = emptyGapRange(s, gap);
    if (!r) continue;
    const key = r.from + ":" + r.to;
    if (seen.has(key)) continue;
    seen.add(key);
    ranges.push(r);
  }
  ranges.sort((a, b) => a.from - b.from);
  const kept = [];
  let lastTo = -1;
  for (const r of ranges) {
    if (r.from < lastTo) continue; // solapado con uno que ya se quita
    kept.push(r);
    lastTo = r.to;
  }
  let out = s;
  for (let i = kept.length - 1; i >= 0; i--) {
    out = out.slice(0, kept[i].from) + out.slice(kept[i].to);
  }
  return { text: out, ranges: kept };
}
