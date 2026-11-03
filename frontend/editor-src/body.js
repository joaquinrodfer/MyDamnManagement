// Cuerpo canónico del documento: sin las líneas en blanco de los extremos.
//
// Decisión D8 (opción a): los bloques vacíos de los extremos NO existen. Un
// párrafo vacío no es un bloque en Markdown -- el `.md` se guarda sin ellos
// (ver `canonical_body` en backend/app/vault/store.py, que ya los tiraba al
// escribir) -- así que el editor deja de prometerlos: el documento se recorta
// en cuanto el cursor sale de ese hueco y lo que se guarda es lo que se ve.
//
// Se recortan TODOS los saltos de los extremos, no uno: el backend guarda un
// solo `\n` de separación tras el `---` y quita el resto, de modo que ni una
// ni dos líneas en blanco al principio sobreviven a una recarga (el caso
// `Intro Intro x` acaba en `x`). Recortar solo una dejaría el mismo salto
// invisible que esta tarjeta viene a quitar.
//
// Lo de en medio no se toca: una línea en blanco entre dos bloques ES el
// separador de bloques y se guarda tal cual.

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
