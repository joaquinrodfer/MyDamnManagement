// ⌫ en las filas de una valla de código: el comando del keymap se consume.
//
// El test puro de `backspaceJoinEdit` no cazaba este fallo: cuando la fila es
// la valla, la primera de contenido o el cierre, aquella función devuelve null
// (no es un ítem del árbol) y el Backspace POR DEFECTO de CodeMirror unía la
// fila con la de arriba -- el `.md` perdía el bloque de código (` ```codigo `)
// o se quedaba sin cerrar, y al recargar se tragaba lo de detrás.
//
// Por eso aquí se prueba el comando tal cual (`runBackspaceJoin`): devuelve
// `true` (tecla consumida, sin transacción) en esas filas y `false` en el
// resto, que es lo que deja decidir al Backspace de siempre. La vista es un
// doble: el comando solo mira la selección, el documento y el campo de
// bloques.

import test from "node:test";
import assert from "node:assert/strict";

import { runBackspaceJoin } from "../entry.js";

function viewAt(text, pos, blocks) {
  return {
    state: {
      selection: { main: { empty: true, from: pos, to: pos, head: pos } },
      doc: { toString: () => text },
      field: () => ({ blocks }),
    },
    dispatched: 0,
    dispatch() {
      this.dispatched += 1;
    },
  };
}

/** Bloque de código de la primera valla hasta el final de la de cierre. */
function fenced(text, paragraphAt = -1) {
  const close = text.lastIndexOf("```");
  const blocks = [{ from: 0, to: close + 3, type: "code" }];
  if (paragraphAt >= 0) blocks.push({ from: paragraphAt, to: text.length, type: "paragraph" });
  return blocks;
}

test("valla: ⌫ en la primera línea de contenido consume la tecla y no edita nada", () => {
  const text = "```\n- uno\n```";
  const view = viewAt(text, 4, fenced(text));
  assert.equal(runBackspaceJoin(view), true);
  assert.equal(view.dispatched, 0);
});

test("valla: ⌫ en la valla de cierre consume la tecla (no deja el bloque sin cerrar)", () => {
  const text = "```\ncodigo\n```\n\nparrafo";
  const view = viewAt(text, text.indexOf("```", 3), fenced(text, text.indexOf("parrafo")));
  assert.equal(runBackspaceJoin(view), true);
  assert.equal(view.dispatched, 0);
});

test("valla: con lenguaje, ⌫ en la primera línea de contenido no rompe la valla", () => {
  const text = "```js\nuno\n```";
  const view = viewAt(text, text.indexOf("uno"), fenced(text));
  assert.equal(runBackspaceJoin(view), true);
  assert.equal(view.dispatched, 0);
});

test("valla: ⌫ entre dos líneas de contenido no se consume (lo hace el Backspace de siempre)", () => {
  const text = "```\nuno\ndos\n```";
  const view = viewAt(text, text.indexOf("dos"), fenced(text));
  assert.equal(runBackspaceJoin(view), false);
  assert.equal(view.dispatched, 0);
});

test("valla: con texto seleccionado el comando no interviene", () => {
  const text = "```\nuno\n```";
  const view = viewAt(text, 4, fenced(text));
  view.state.selection.main = { empty: false, from: 3, to: 8, head: 8 };
  assert.equal(runBackspaceJoin(view), false);
  assert.equal(view.dispatched, 0);
});
