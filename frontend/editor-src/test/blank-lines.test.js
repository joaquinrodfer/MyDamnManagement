// Cuántas líneas en blanco hay delante de un bloque: el número que
// blockSpacingDecorations le pasa a CSS (`--mdm-blank`) para que el aire del
// bloque NO se sume al que ya ocupan esas líneas. De ahí sale el pedido del
// dueño -- una línea en blanco entre dos párrafos deja UNA línea de aire y dos
// dejan el doble --, así que la cuenta es lo que hay que fijar.
//
// Se prueba sobre documentos de verdad (EditorState), no sobre un texto con
// párrafos inventados: la cuenta depende de dónde acaban los rangos del parser
// (el ítem de una lista se lleva su salto de línea) y de no salirse del hueco
// entre los dos bloques (una línea en blanco DENTRO de una valla es del bloque
// de código, no aire).

import test from "node:test";
import assert from "node:assert/strict";

import { EditorState } from "@codemirror/state";

import { blankLinesBefore } from "../entry.js";

function docOf(text) {
  return EditorState.create({ doc: text }).doc;
}

/** El bloque `n`-ésimo separado por una o más líneas en blanco, con el mismo
 * reparto que el parser: de principio de su línea al final de su texto. */
function para(text, n) {
  const starts = [];
  let pos = 0;
  for (const part of text.split(/\n{2,}/)) {
    const from = text.indexOf(part, pos);
    starts.push({ from, to: from + part.length });
    pos = from + part.length;
  }
  return starts[n];
}

const between = (text, n) => blankLinesBefore(docOf(text), para(text, n), para(text, n - 1));

test("una línea en blanco delante del párrafo cuenta 1", () => {
  assert.equal(between("alfa\n\nbeta", 1), 1);
});

test("dos líneas en blanco cuentan 2 (el hueco delata cuántas hay)", () => {
  assert.equal(between("alfa\n\n\nbeta", 1), 2);
  assert.equal(between("alfa\n\n\n\nbeta", 1), 3);
});

test("sin línea en blanco no hay nada que descontar", () => {
  // «alfa\nbeta» es UN párrafo para el parser, así que los dos trozos van a mano.
  const text = "alfa\nbeta";
  const doc = docOf(text);
  assert.equal(blankLinesBefore(doc, { from: 5, to: 9 }, { from: 0, to: 4 }), 0);
});

test("el primer bloque no tiene hueco delante", () => {
  const text = "alfa\n\nbeta";
  assert.equal(blankLinesBefore(docOf(text), para(text, 0), para(text, 0)), 0);
});

test("una línea en blanco dentro de una valla es del bloque de código", () => {
  const text = "```\nalfa\n\nbeta\n```\n\ndelta";
  const doc = docOf(text);
  const code = { from: 0, to: text.indexOf("\n\ndelta") };
  const delta = { from: text.indexOf("delta"), to: text.length };
  assert.equal(blankLinesBefore(doc, delta, code), 1);

  // Y el bloque de la valla no hereda la línea en blanco que lleva dentro.
  const beforeCode = { from: 0, to: 0 };
  assert.equal(blankLinesBefore(doc, code, beforeCode), 0);
});

test("ítems de una lista: el que se lleva su salto no inventa líneas en blanco", () => {
  const text = "- alfa\n- beta";
  const doc = docOf(text);
  const first = { from: 0, to: text.indexOf("\n") + 1 }; // ListItem incluye el salto
  const second = { from: text.indexOf("- beta"), to: text.length };
  assert.equal(blankLinesBefore(doc, second, first), 0);
});

test("con línea en blanco entre dos listas, cuenta la de en medio", () => {
  const text = "- alfa\n\n- beta";
  const doc = docOf(text);
  const first = { from: 0, to: text.indexOf("\n") + 1 };
  const second = { from: text.indexOf("- beta"), to: text.length };
  assert.equal(blankLinesBefore(doc, second, first), 1);
});
