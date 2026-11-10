// Alt+↑ / Alt+↓: el comando del keymap, tal cual.
//
// El test puro de `moveBlockEdits` fija QUÉ texto cambia de sitio; este fija
// que la tecla haga UNA sola transacción (un solo ⌘Z deshace el movimiento
// entero), que el cursor se quede dentro del bloque movido y que en un extremo
// la tecla se consuma sin tocar el documento.
//
// La vista es un doble, como en test/backspace-fence.test.js: el comando solo
// mira la selección, el documento y el campo de bloques.

import test from "node:test";
import assert from "node:assert/strict";

import { runMoveBlockKey } from "../entry.js";

function viewAt(text, pos, blocks, selected = new Set()) {
  return {
    state: {
      selection: { main: { empty: true, from: pos, to: pos, head: pos } },
      doc: { toString: () => text, length: text.length },
      field: () => ({ blocks, selected }),
    },
    dispatches: [],
    dispatch(tr) {
      this.dispatches.push(tr);
    },
    focus() {},
  };
}

function blocksOf(text) {
  // Igual que `computeBlocks`: párrafos separados por líneas en blanco.
  const out = [];
  for (const part of text.split(/\n{2,}/)) {
    if (!part) continue;
    const from = text.indexOf(part, out.length ? out[out.length - 1].to : 0);
    out.push({ from, to: from + part.length, type: "paragraph" });
  }
  return out;
}

const text = "uno\n\ndos\n\ntres";

test("Alt+↑ mueve el bloque del cursor en UNA transacción", () => {
  const blocks = blocksOf(text);
  const view = viewAt(text, text.indexOf("tres"), blocks);
  assert.equal(runMoveBlockKey(view, -1), true);
  assert.equal(view.dispatches.length, 1);
  const tr = view.dispatches[0];
  assert.equal(tr.changes.length, 1);
  // El cursor sigue dentro del bloque movido (no se va al principio del todo).
  assert.ok(tr.selection.anchor > 0);
  assert.equal(tr.userEvent, "move.block");
});

test("Alt+↓ baja el bloque del cursor", () => {
  const blocks = blocksOf(text);
  const view = viewAt(text, 0, blocks);
  assert.equal(runMoveBlockKey(view, 1), true);
  assert.equal(view.dispatches.length, 1);
  assert.deepEqual(view.dispatches[0].changes, [{ from: 0, to: text.length, insert: "dos\n\nuno\n\ntres" }]);
});

test("en el primer bloque Alt+↑ se consume sin tocar nada", () => {
  const blocks = blocksOf(text);
  const view = viewAt(text, 0, blocks);
  assert.equal(runMoveBlockKey(view, -1), true);
  assert.equal(view.dispatches.length, 0);
});

test("en el último bloque Alt+↓ se consume sin tocar nada", () => {
  const blocks = blocksOf(text);
  const view = viewAt(text, text.length, blocks);
  assert.equal(runMoveBlockKey(view, 1), true);
  assert.equal(view.dispatches.length, 0);
});

test("con varios bloques marcados la tecla no hace nada (mover es de uno)", () => {
  const blocks = blocksOf(text);
  const view = viewAt(text, 0, blocks, new Set([0, 1]));
  assert.equal(runMoveBlockKey(view, 1), false);
  assert.equal(view.dispatches.length, 0);
});

test("con un bloque marcado con el asa, Alt+↓ lo mueve", () => {
  const blocks = blocksOf(text);
  const view = viewAt(text, 0, blocks, new Set([0]));
  assert.equal(runMoveBlockKey(view, 1), true);
  assert.equal(view.dispatches.length, 1);
  assert.equal(view.dispatches[0].changes[0].insert, "dos\n\nuno\n\ntres");
});
