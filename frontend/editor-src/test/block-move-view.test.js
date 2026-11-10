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
  // `true`, no `false`: la tecla se consume. Si se dejara pasar, el keymap de
  // CodeMirror la usaría para `moveLineDown` y movería una línea suelta.
  assert.equal(runMoveBlockKey(view, 1), true);
  assert.equal(view.dispatches.length, 0);
});

test("si el movimiento no cabe, la tecla se consume y el documento no se toca", () => {
  // `> cita uno\n---\n> cita uno` no tiene saltos para reordenar sin pegar las
  // dos citas, así que `moveBlockTo` no mueve nada. La tecla tiene que quedar
  // consumida: devolver `false` hacía que CodeMirror móviera UNA LÍNEA
  // (`moveLineDown`), que dejaba `---\n> cita uno\n> cita uno` (2 bloques).
  const text = "> cita uno\n---\n> cita uno";
  const blocks = [
    { from: 0, to: 10, type: "quote" },
    { from: 11, to: 14, type: "hr" },
    { from: 15, to: 25, type: "quote" },
  ];
  for (const [pos, dir] of [
    [0, 1],
    [10, 1],
    [15, -1],
    [25, -1],
  ]) {
    const view = viewAt(text, pos, blocks);
    assert.equal(runMoveBlockKey(view, dir), true, `Alt${dir < 0 ? "↑" : "↓"} en ${pos} se consume`);
    assert.equal(view.dispatches.length, 0, `no se toca el documento desde ${pos}`);
  }
});

test("Alt+↑/↓ con el cursor en el HUECO entre bloques se consume y no escribe nada", () => {
  // Clic sobre la línea en blanco: el cursor queda FUERA de los dos bloques (la
  // segunda `\n` del hueco). El comando devolvía `false` y la tecla caía en el
  // keymap de CodeMirror, que la usa para `moveLineDown`: «parrafo 0\n\n    codigo 1»
  // acababa en «parrafo 0\n    codigo 1\n», con el bloque de código dentro del
  // párrafo (2 bloques -> 1) y el `.md` guardado así. Sin bloque que mover, la
  // tecla es nuestra igual: se consume y no se toca el documento.
  const docs = [
    // El caso de la tarjeta: párrafo + bloque de código indentado.
    "parrafo 0\n\n    codigo 1",
    // Hueco en medio de tres bloques: el gesto movía la LÍNEA en blanco.
    "    codigo 1\n\nparrafo\n\n    codigo 2",
    // Hueco de una sola línea en blanco entre un párrafo y una cita.
    "uno\n\n> dos",
  ];
  let probados = 0;
  for (const doc of docs) {
    const blocks = blocksOf(doc);
    assert.ok(blocks.length >= 2, `el documento tiene bloques: ${JSON.stringify(doc)}`);
    for (let i = 1; i < blocks.length; i++) {
      // Todas las posiciones del hueco: desde justo detrás del bloque de arriba
      // hasta justo delante del de abajo (excluidos los extremos, que ya son de
      // un bloque).
      for (let pos = blocks[i - 1].to + 1; pos < blocks[i].from; pos++) {
        for (const dir of [-1, 1]) {
          const view = viewAt(doc, pos, blocks);
          const tecla = `Alt${dir < 0 ? "↑" : "↓"} en el hueco (${pos}) de ${JSON.stringify(doc)}`;
          assert.equal(runMoveBlockKey(view, dir), true, `${tecla} se consume`);
          assert.equal(view.dispatches.length, 0, `${tecla} no escribe nada`);
          probados++;
        }
      }
    }
  }
  assert.ok(probados >= 6, `se han probado los huecos (${probados})`);
});

test("con un bloque marcado con el asa, Alt+↓ lo mueve", () => {
  const blocks = blocksOf(text);
  const view = viewAt(text, 0, blocks, new Set([0]));
  assert.equal(runMoveBlockKey(view, 1), true);
  assert.equal(view.dispatches.length, 1);
  assert.equal(view.dispatches[0].changes[0].insert, "dos\n\nuno\n\ntres");
});
