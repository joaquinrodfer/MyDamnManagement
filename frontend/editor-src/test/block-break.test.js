// ⏎ al final de un bloque sobre un `EditorState` de verdad: el bloque nuevo no
// materializa líneas en blanco mientras está vacío, y las materializa con el
// primer contenido -- tecleado o PEGADO, que es escribir contenido
// (t_c7756962, requisito 3). El `.md` que sale es el que se guarda.
//
// Lo que necesita el navegador (que la tecla llegue al keymap, el pintado) se
// comprueba en la app arrancada; aquí se ejercita el filtro de la transacción,
// que es donde se decide el texto final.

import test from "node:test";
import assert from "node:assert/strict";

import { EditorState } from "@codemirror/state";
import { markdown } from "@codemirror/lang-markdown";

import { addBlockBreak, blockBreakTestExtensions } from "../entry.js";
import { blockKeyEdit } from "../block-keys.js";
import { blocksFromTree } from "../block-ranges.js";

// El mismo parser que monta la vista (ver `markdownSupport` en entry.js).
const editorParser = markdown({ addKeymap: false }).language.parser;

/** Los bloques que cuenta el editor (lo que hace `computeBlocks` en entry.js). */
const blocks = (doc) => blocksFromTree(editorParser.parse(doc).topNode, doc);

const start = (doc) => EditorState.create({ doc, extensions: blockBreakTestExtensions });

/** ⏎ al final del bloque `{from, to}` -- la misma transacción que la tecla. */
function enter(state, from, to, type) {
  const text = state.doc.toString();
  const edit = blockKeyEdit({ text, pos: to, block: { from, to, type }, kind: "enter" });
  assert.ok(edit, "⏎ tenía que estar atendido");
  return state.update({
    changes: edit.from === edit.to && !edit.insert ? [] : { from: edit.from, to: edit.to, insert: edit.insert },
    selection: { anchor: edit.cursor },
    userEvent: "input",
    effects: edit.blockBreak ? [addBlockBreak.of(edit.cursor)] : [],
  }).state;
}

/** Mete `content` en el cursor con el `userEvent` que toque. */
function insert(state, content, userEvent) {
  const pos = state.selection.main.head;
  return state.update({
    changes: { from: pos, to: pos, insert: content },
    selection: { anchor: pos + content.length },
    userEvent,
  }).state;
}

test("pegar bajo un encabezado deja las dos líneas en blanco (18 px de hueco)", () => {
  let state = start("# Titulo\n\nTexto de abajo.");
  state = enter(state, 0, 8, "h1");
  assert.equal(state.doc.toString(), "# Titulo\n\nTexto de abajo.", "⏎ no toca el .md");
  state = insert(state, "hola", "input.paste");
  assert.equal(state.doc.toString(), "# Titulo\n\nhola\n\nTexto de abajo.");
  assert.equal(blocks(state.doc.toString()).length, 3, "tres bloques, no dos");
});

test("pegar bajo un encabezado con otro encabezado debajo no añade líneas de más", () => {
  let state = start("# UNO\n\n# DOS");
  state = enter(state, 0, 5, "h1");
  state = insert(state, "hola", "input.paste");
  assert.equal(state.doc.toString(), "# UNO\n\nhola\n# DOS");
});

test("la tabla de debajo no se funde con el párrafo nuevo", () => {
  // La tabla que la app pinta es un párrafo para el parser del editor: sin la
  // línea de abajo, `Z` se la comería y el editor pasaría de 3 bloques a 2.
  const doc = "# A\n\n| a | b |\n|---|---|\n| 1 | 2 |";
  assert.equal(blocks(doc).length, 2, "el encabezado y la tabla");
  let state = start(doc);
  state = enter(state, 0, 3, "h1");
  state = insert(state, "Z", "input.type");
  assert.equal(state.doc.toString(), "# A\n\nZ\n\n| a | b |\n|---|---|\n| 1 | 2 |");
  assert.equal(blocks(state.doc.toString()).length, 3, "la tabla sigue siendo un bloque aparte");
});

test("un pegado con saltos no se toca: no es la primera letra de un párrafo", () => {
  let state = start("# Titulo\n\nTexto de abajo.");
  state = enter(state, 0, 8, "h1");
  state = insert(state, "a\nb", "input.paste");
  assert.equal(state.doc.toString(), "# Titulo\na\nb\nTexto de abajo.");
});
