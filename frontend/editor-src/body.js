// Cuerpo canónico del documento: sin las líneas en blanco de los extremos.
//
// Los bloques vacíos de los EXTREMOS no sobreviven: el `.md` se guarda sin
// ellos (ver `canonical_body` en backend/app/vault/store.py, que ya los tiraba
// al escribir), así que el editor los recorta en cuanto el cursor sale de ese
// hueco y lo que se guarda es lo que se ve.
//
// Se recortan TODOS los saltos de los extremos, no uno: el backend guarda un
// solo `\n` de separación tras el `---` y quita el resto, de modo que ni una
// ni dos líneas en blanco al principio sobreviven a una recarga (el caso
// `Intro Intro x` acaba en `x`). Recortar solo una dejaría el mismo salto
// invisible que esta tarjeta viene a quitar.
//
// Lo de en medio no se toca: una línea en blanco entre dos bloques es el
// separador, y un bloque vacío (`alfa\n\n\n\nbeta`, el que abre ⏎ o el «+»)
// es un bloque como los demás y se guarda tal cual, como en Notion (ver
// block-gaps.js).

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
 * Texto después de quitar del documento el tramo `{from, to}`.
 *
 * Sirve para el texto que se GUARDA cuando el selector de bases tiene algo
 * escrito: lo tecleado ahí es interfaz (el filtro), no contenido de la nota, y
 * tiene que quedarse fuera del `.md` pase lo que pase -- también si la pestaña
 * se cierra o se recarga de golpe, que es un guardado que no pasa por cerrar el
 * selector (ver `getValue` en entry.js).
 *
 * Por eso se resuelve sobre el texto, sin despachar ninguna transacción: lo que
 * hay en pantalla no puede cambiar bajo los dedos cuando el guardado sale con
 * la pestaña todavía viva (`visibilitychange`).
 */
export function withoutRange(text, range) {
  const s = String(text ?? "");
  const from = Math.max(0, Math.min(Number(range?.from) || 0, s.length));
  const to = Math.max(from, Math.min(Number(range?.to) || 0, s.length));
  return to <= from ? s : s.slice(0, from) + s.slice(to);
}
