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
//
// Lo mismo vale para el bloque vacío que se crea en MEDIO (el «+» del asa, ⏎
// al final de un bloque con contenido detrás): se colapsa al abandonarlo, pero
// solo si lo ha creado el editor -- `emptyGapRange` / `collapseEmptyGaps` --,
// así que un `alfa\n\n\n\nbeta` escrito a mano se sigue guardando tal cual.

import test from "node:test";
import assert from "node:assert/strict";

import {
  canonicalBody,
  collapseEmptyGaps,
  cursorInOuterBlanks,
  emptyGapRange,
  newlineRunAt,
  outerBlankRange,
} from "../body.js";

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

// ------------------------------------------ bloque vacío de en medio (t_e9087348)

test("el hueco del + en medio: doc con 4 saltos, se colapsa a los 2 de separador", () => {
  // `alfa\n\nbeta` + «+» en el primer bloque -> se inserta «\n\n» en 4.
  const doc = "alfa\n\n\n\nbeta";
  assert.deepEqual(emptyGapRange(doc, { from: 4, to: 6 }), { from: 4, to: 6 });
  assert.deepEqual(collapseEmptyGaps(doc, [{ from: 4, to: 6 }]), {
    text: "alfa\n\nbeta",
    ranges: [{ from: 4, to: 6 }],
  });
});

test("sin marcas no se toca nada: las líneas en blanco de en medio son del usuario", () => {
  assert.deepEqual(collapseEmptyGaps("alfa\n\n\n\nbeta", []), {
    text: "alfa\n\n\n\nbeta",
    ranges: [],
  });
});

test("si se escribe en el bloque nuevo, el hueco es contenido", () => {
  // «+» y luego teclear: el par queda delante del texto, no de un salto.
  const doc = "alfa\n\ngamma\n\nbeta";
  assert.equal(emptyGapRange(doc, { from: 4, to: 6 }), null);
  assert.deepEqual(collapseEmptyGaps(doc, [{ from: 4, to: 6 }]), { text: doc, ranges: [] });
});

test("alfa\\n\\n\\n\\nbeta escrito a mano + ⏎: se va el par del bloque nuevo, no los suyos", () => {
  // Cuatro saltos de la nota + el par que inserta ⏎ al final de «alfa».
  const doc = "alfa\n\n\n\n\n\nbeta";
  assert.deepEqual(collapseEmptyGaps(doc, [{ from: 4, to: 6 }]), {
    text: "alfa\n\n\n\nbeta",
    ranges: [{ from: 4, to: 6 }],
  });
});

test("el hueco también vale pegado al separador de DELANTE (⏎ sobre un bloque marcado)", () => {
  // `alfa\n\nbeta\n\ngamma` con beta marcado: el par se inserta detrás del
  // separador que ya había, así que el hueco queda entre dos saltos.
  const doc = "alfa\n\n\n\ngamma";
  assert.deepEqual(emptyGapRange(doc, { from: 6, to: 8 }), { from: 6, to: 8 });
  assert.deepEqual(collapseEmptyGaps(doc, [{ from: 6, to: 8 }]), {
    text: "alfa\n\ngamma",
    ranges: [{ from: 6, to: 8 }],
  });
});

test("un hueco ya inexistente o desplazado no rompe nada", () => {
  assert.equal(emptyGapRange("alfabeta", { from: 4, to: 6 }), null);
  assert.equal(emptyGapRange("alfa\n\nbeta", { from: 4, to: 4 }), null);
  assert.equal(emptyGapRange("alfa\n\nbeta", null), null);
  assert.deepEqual(collapseEmptyGaps("alfa\n\nbeta", [{ from: 4, to: 6 }]), {
    text: "alfa\n\nbeta",
    ranges: [],
  });
});

test("dos marcas en el mismo hueco se quitan una sola vez", () => {
  assert.deepEqual(collapseEmptyGaps("alfa\n\n\n\nbeta", [{ from: 4, to: 6 }, { from: 4, to: 6 }]), {
    text: "alfa\n\nbeta",
    ranges: [{ from: 4, to: 6 }],
  });
});

test("dos huecos distintos en el mismo documento se quitan los dos", () => {
  // alfa |+en blanco| beta |+en blanco| gamma
  const doc = "alfa\n\n\n\nbeta\n\n\n\ngamma";
  assert.deepEqual(collapseEmptyGaps(doc, [{ from: 4, to: 6 }, { from: 12, to: 14 }]), {
    text: "alfa\n\nbeta\n\ngamma",
    ranges: [{ from: 4, to: 6 }, { from: 12, to: 14 }],
  });
});

test("un par de saltos al final del documento no es un hueco interior", () => {
  assert.equal(emptyGapRange("alfa\n\n", { from: 4, to: 6 }), null);
});

test("newlineRunAt: el tramo de saltos que contiene la posición", () => {
  assert.deepEqual(newlineRunAt("alfa\n\n\n\nbeta", 6), { from: 4, to: 8 });
  assert.deepEqual(newlineRunAt("alfa\n\n\n\nbeta", 4), { from: 4, to: 8 });
  assert.equal(newlineRunAt("alfa\n\nbeta", 2), null); // dentro de «alfa»
  assert.equal(newlineRunAt("beta", 0), null);
  assert.deepEqual(newlineRunAt("\n\n", 1), { from: 0, to: 2 });
});
