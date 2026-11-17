// Marcar un bloque NO deja una selección de texto viva (t_c2c98f7b, punto 3).
//
// La marca del bloque es una clase del bloque, no un rango de CodeMirror. Pero
// vaciar sólo el DOM no basta: si el ESTADO conservaba un rango (el usuario
// venía de arrastrar el ratón sobre un párrafo), el `view.focus()` de después y
// la sincronización del propio editor volvían a pintarlo, y `Ctrl+C` copiaba el
// texto de un bloque que el usuario creía haber cogido entero. Este test sujeta
// las dos mitades del arreglo: con rango vivo, la transacción lo colapsa dentro
// del bloque marcado; sin rango, marcar no mueve el cursor.
//
// La vista es un doble, como en test/block-move-view.test.js: `selectBlocks`
// sólo mira la selección, el campo de bloques y lo que despacha.

import test from "node:test";
import assert from "node:assert/strict";

import { selectBlocks } from "../entry.js";

const BLOCKS = [
  { from: 0, to: 8, type: "heading" },
  { from: 10, to: 32, type: "paragraph" },
  { from: 34, to: 58, type: "paragraph" },
];

function viewWith(ranges, selected = new Set()) {
  return {
    state: {
      selection: { ranges },
      doc: { length: 60, toString: () => "" },
      field: () => ({ blocks: BLOCKS, selected }),
    },
    dispatches: [],
    dispatch(tr) {
      this.dispatches.push(tr);
    },
  };
}

function withWindow(fn) {
  const prev = globalThis.window;
  globalThis.window = { getSelection: () => null };
  try {
    return fn();
  } finally {
    if (prev === undefined) delete globalThis.window;
    else globalThis.window = prev;
  }
}

test("marcar un bloque con texto seleccionado colapsa el rango en el estado", () => {
  // «afo uno de la nota.»: el rango que dejaba vivo el defecto.
  const view = viewWith([{ from: 14, to: 30, anchor: 14, head: 30, empty: false }]);
  withWindow(() => selectBlocks(view, new Set([1])));

  const tr = view.dispatches[0];
  assert.ok(tr, "tiene que despachar");
  assert.ok(tr.selection, "el rango viejo no puede sobrevivir en el estado");
  // `selection` viaja como el rango que el despacho normaliza: colapsado.
  assert.equal(tr.selection.empty, true, "el rango se colapsa, no se conserva");
  // Dentro del bloque marcado (su arranque), no donde estaba el texto.
  assert.equal(tr.selection.from, BLOCKS[1].from);
});

test("con varios bloques marcados se colapsa en el primero, no en el de la selección", () => {
  const view = viewWith([{ from: 40, to: 52, anchor: 40, head: 52, empty: false }]);
  withWindow(() => selectBlocks(view, new Set([2, 0])));

  const tr = view.dispatches[0];
  assert.equal(tr.selection.empty, true);
  assert.equal(tr.selection.from, BLOCKS[0].from);
});

test("sin selección de texto, marcar un bloque no mueve el cursor", () => {
  const view = viewWith([{ from: 20, to: 20, anchor: 20, head: 20, empty: true }]);
  withWindow(() => selectBlocks(view, new Set([1])));

  const tr = view.dispatches[0];
  assert.ok(tr, "tiene que despachar la marca");
  assert.equal(
    Object.prototype.hasOwnProperty.call(tr, "selection"),
    false,
    "el caret se queda donde el usuario lo dejó",
  );
});

test("la marca despacha el conjunto de bloques que se le pasa", () => {
  const view = viewWith([{ from: 20, to: 20, anchor: 20, head: 20, empty: true }]);
  withWindow(() => selectBlocks(view, new Set([0, 2])));

  const tr = view.dispatches[0];
  // El despacho lleva los efectos como un único efecto (`effects: efecto`),
  // igual que lo manda `selectBlocks`: la marca y nada más.
  assert.deepEqual([...tr.effects.value].sort(), [0, 2]);
});
