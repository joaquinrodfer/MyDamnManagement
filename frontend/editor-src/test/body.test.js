// Los bloques vacíos de los extremos no sobreviven al guardado.
//
// El `.md` ya se guardaba sin ellos (el backend canónico quita el salto
// separador y los finales de línea), así que el editor no puede prometer un
// bloque que desaparece al recargar. `canonicalBody` es la regla que comparten
// el guardado y el recorte del propio documento, y `cursorInOuterBlanks` la
// que decide cuándo se puede recortar (nunca bajo el cursor).
//
// Los tres casos del informe, tal cual: `Intro Intro x`, `y Intro Intro` y
// `A Mayús+Intro Mayús+Intro`.
//
// Los de EN MEDIO sí: son bloques como en Notion (ver block-gaps.js) y se
// guardan tal cual.

import test from "node:test";
import assert from "node:assert/strict";

import { canonicalBody, cursorInOuterBlanks, outerBlankRange, withoutRange } from "../body.js";

test("D8: dos Intro al principio se van con el texto", () => {
  assert.equal(canonicalBody("\n\nx"), "x");
});

test("D8: un Intro al principio se va (el backend guarda un solo separador)", () => {
  assert.equal(canonicalBody("\nx"), "x");
});

test("D8: los saltos del final se van", () => {
  assert.equal(canonicalBody("y\n\n"), "y");
});

test("D8: los dos bloques vacíos de dos Mayús+Intro se van", () => {
  assert.equal(canonicalBody("A\n\n\n\n"), "A");
});

test("las líneas en blanco de en medio NO se tocan", () => {
  assert.equal(canonicalBody("a\n\n\nb"), "a\n\n\nb");
  assert.equal(canonicalBody("a\n\nb\n"), "a\n\nb");
});

test("un documento vacío sigue vacío", () => {
  assert.equal(canonicalBody(""), "");
  assert.equal(canonicalBody("\n\n"), "");
  assert.equal(canonicalBody(null), "");
});

test("outerBlankRange: dónde empieza y acaba el contenido", () => {
  assert.deepEqual(outerBlankRange("\n\nabc\n"), { lead: 2, end: 5 });
  assert.deepEqual(outerBlankRange("abc"), { lead: 0, end: 3 });
  assert.deepEqual(outerBlankRange("\n\n"), { lead: 2, end: 0 });
});

test("el cursor dentro del hueco de los extremos impide recortar", () => {
  // "\n\nx": el hueco de delante son las posiciones 0 y 1; 2 es la "x".
  assert.equal(cursorInOuterBlanks("\n\nx", 0), true);
  assert.equal(cursorInOuterBlanks("\n\nx", 1), true);
  assert.equal(cursorInOuterBlanks("\n\nx", 2), false);
  // "x\n\n": el hueco de detrás empieza en 1 (final de la "x").
  assert.equal(cursorInOuterBlanks("x\n\n", 1), false);
  assert.equal(cursorInOuterBlanks("x\n\n", 2), true);
  assert.equal(cursorInOuterBlanks("x\n\n", 3), true);
});

// ---- el filtro del selector de bases no es texto de la nota ----------------
//
// Se escribe dentro del documento para poder filtrar, pero lo que se guarda es
// el documento sin él: el autoguardado y el guardado de emergencia al cerrar o
// recargar la pestaña no pasan por ningún cierre del selector (`getValue` en
// entry.js -> `withoutRange` aquí).

test("withoutRange: quita el tramo y deja el texto de alrededor", () => {
  assert.equal(withoutRange("abcdef", { from: 2, to: 4 }), "abef");
});

test("withoutRange: sin tramo (filtro vacío) no cambia nada", () => {
  assert.equal(withoutRange("ab\n\ncd", { from: 3, to: 3 }), "ab\n\ncd");
  assert.equal(withoutRange("ab\n\ncd", null), "ab\n\ncd");
});

test("withoutRange: un tramo al revés o fuera del documento no rompe nada", () => {
  assert.equal(withoutRange("abcdef", { from: 4, to: 1 }), "abcdef");
  assert.equal(withoutRange("abcdef", { from: 50, to: 90 }), "abcdef");
  // Se recorta al documento, no se sale de él.
  assert.equal(withoutRange("abcdef", { from: 2, to: 99 }), "ab");
  assert.equal(withoutRange("abcdef", { from: -5, to: 2 }), "cdef");
});

test("withoutRange: el caso de la tarjeta -- el filtro no llega al .md", () => {
  // "# T\n\ncontactos.\n\nfiltro", con el selector abierto al final.
  const text = "# T\n\ncontactos.\n\nfiltro";
  const from = text.indexOf("filtro");
  assert.equal(canonicalBody(withoutRange(text, { from, to: text.length })), "# T\n\ncontactos.");
});
