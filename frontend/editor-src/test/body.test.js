// D8 (a): los bloques vacíos de los extremos no existen.
//
// El `.md` ya se guardaba sin ellos (el backend canónico quita el salto
// separador y los finales de línea), así que el editor no puede prometer un
// bloque que desaparece al recargar. `canonicalBody` es la regla que comparten
// el guardado y el recorte del propio documento, y `cursorInOuterBlanks` la
// que decide cuándo se puede recortar (nunca bajo el cursor).
//
// Los tres casos del informe, tal cual: `Intro Intro x`, `y Intro Intro` y
// `A Mayús+Intro Mayús+Intro`.

import test from "node:test";
import assert from "node:assert/strict";

import { canonicalBody, cursorInOuterBlanks, outerBlankRange } from "../body.js";

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
