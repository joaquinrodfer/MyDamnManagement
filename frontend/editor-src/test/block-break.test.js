// ⏎ al final de un bloque sobre un `EditorState` de verdad: abre un BLOQUE
// VACÍO entre dos separadores (ver block-gaps.js), el cursor no se para nunca
// en un separador, y lo que se escribe en el bloque vacío cae en su línea sin
// mover nada (t_c7756962: el cursor caía en el aire de debajo del encabezado y
// bajaba de línea al escribir). El `.md` que sale es el que se guarda.
//
// Lo que necesita el navegador (que la tecla llegue al keymap, el pintado, las
// flechas por píxeles) se comprueba en la app arrancada; aquí se ejercitan la
// edición de ⏎ y los dos filtros de transacción.

import test from "node:test";
import assert from "node:assert/strict";

import { EditorSelection, EditorState } from "@codemirror/state";
import { history, isolateHistory, redo, undo } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";

import { blockGapTestExtensions, setSoftLine } from "../entry.js";
import { blockKeyEdit } from "../block-keys.js";
import { formatEdit } from "../format.js";
import { blocksFromTree } from "../block-ranges.js";

// El mismo parser que monta la vista (ver `markdownSupport` en entry.js).
const editorParser = markdown({ addKeymap: false }).language.parser;

/** Los bloques que cuenta el editor (lo que hace `computeBlocks` en entry.js). */
const blocks = (doc) => blocksFromTree(editorParser.parse(doc).topNode, doc);

const start = (doc) => EditorState.create({ doc, extensions: [history(), ...blockGapTestExtensions] });

/** ⏎ al final del bloque `{from, to}` -- la misma transacción que la tecla. */
function enter(state, from, to, type) {
  const text = state.doc.toString();
  const edit = blockKeyEdit({ text, pos: to, block: { from, to, type }, kind: "enter" });
  assert.ok(edit, "⏎ tenía que estar atendido");
  return state.update({
    changes: { from: edit.from, to: edit.to, insert: edit.insert },
    selection: { anchor: edit.cursor },
    userEvent: "input",
    annotations: isolateHistory.of("full"), // como `runBlockKey`
  }).state;
}

/** Mete `content` en el cursor con el `userEvent` que toque. */
function insert(state, content, userEvent = "input.type") {
  const pos = state.selection.main.head;
  return state.update({
    changes: { from: pos, to: pos, insert: content },
    selection: { anchor: pos + content.length },
    userEvent,
  }).state;
}

/** Mueve el cursor a `pos` como lo haría una tecla (`select`) o un clic. */
function moveTo(state, pos, userEvent = "select") {
  return state.update({ selection: { anchor: pos }, userEvent }).state;
}

/** Corre un comando de @codemirror/commands sobre el estado (undo, redo). */
function run(state, command) {
  let next = state;
  command({ state, dispatch: (tr) => (next = tr.state) });
  return next;
}

/** ⌘B con el cursor vacío: la misma edición que `runFormat` en entry.js. */
function bold(state) {
  const pos = state.selection.main.head;
  const edit = formatEdit({ text: state.doc.toString(), ranges: [{ from: pos, to: pos }], kind: "bold" });
  return state.update({
    changes: edit.changes,
    selection: EditorSelection.create(edit.selection.map((s) => EditorSelection.range(s.from, s.to))),
    userEvent: "input.format",
  }).state;
}

const lineOf = (state) => state.doc.lineAt(state.selection.main.head).number;

test("⏎ tras un H1 con texto debajo: el cursor en un bloque vacío, no en el separador", () => {
  let state = start("# Titulo\n\nTexto de abajo.");
  state = enter(state, 0, 8, "h1");
  assert.equal(state.doc.toString(), "# Titulo\n\n\n\nTexto de abajo.");
  assert.equal(lineOf(state), 3, "línea 2 es el separador; la 3, el bloque vacío");
});

test("lo que se escribe en el bloque vacío cae en su línea: nada se mueve", () => {
  let state = start("# Titulo\n\nTexto de abajo.");
  state = enter(state, 0, 8, "h1");
  state = insert(state, "w");
  assert.equal(state.doc.toString(), "# Titulo\n\nw\n\nTexto de abajo.");
  assert.equal(lineOf(state), 3, "el cursor sigue en la misma línea");
  assert.equal(blocks(state.doc.toString()).length, 3, "tres bloques, no dos");
});

test("⏎ tras el último bloque y escribir: la forma de siempre", () => {
  let state = start("Intro.\n\n# Titulo");
  state = enter(state, 8, 16, "h1");
  assert.equal(state.doc.toString(), "Intro.\n\n# Titulo\n\n");
  state = insert(state, "aaaa");
  assert.equal(state.doc.toString(), "Intro.\n\n# Titulo\n\naaaa");
  assert.equal(lineOf(state), 5);
});

test("pegar varias líneas en el bloque vacío no se funde con el de abajo (t_608d3c57)", () => {
  let state = start("# A\n\nTexto");
  state = enter(state, 0, 3, "h1");
  state = insert(state, "a\nb", "input.paste");
  assert.equal(state.doc.toString(), "# A\n\na\nb\n\nTexto");
  assert.equal(blocks(state.doc.toString()).length, 3);
});

test("la tabla de debajo no se funde con el párrafo nuevo", () => {
  const doc = "# A\n\n| a | b |\n|---|---|\n| 1 | 2 |";
  let state = start(doc);
  state = enter(state, 0, 3, "h1");
  state = insert(state, "Z");
  assert.equal(state.doc.toString(), "# A\n\nZ\n\n| a | b |\n|---|---|\n| 1 | 2 |");
  assert.equal(blocks(state.doc.toString()).length, 3, "la tabla sigue siendo un bloque aparte");
});

test("⌘B en el bloque vacío y escribir", () => {
  let state = start("# A\n\nTexto");
  state = enter(state, 0, 3, "h1");
  state = bold(state);
  state = insert(state, "w");
  assert.equal(state.doc.toString(), "# A\n\n**w**\n\nTexto");
  assert.equal(blocks(state.doc.toString()).length, 3);
});

test("⌘Z quita lo escrito y deja el bloque vacío; otro ⌘Z quita el bloque", () => {
  let state = start("# A\n\nTexto");
  state = enter(state, 0, 3, "h1");
  state = insert(state, "w");
  state = run(state, undo);
  assert.equal(state.doc.toString(), "# A\n\n\n\nTexto");
  assert.equal(lineOf(state), 3, "el cursor vuelve al bloque vacío");
  state = run(state, undo);
  assert.equal(state.doc.toString(), "# A\n\nTexto");
  state = run(state, redo);
  state = run(state, redo);
  assert.equal(state.doc.toString(), "# A\n\nw\n\nTexto");
});

test("salir del bloque vacío y volver: sigue ahí, y escribir no se come el de abajo", () => {
  let state = start("# A\n\nTexto");
  state = enter(state, 0, 3, "h1");
  const gap = state.selection.main.head;
  state = moveTo(state, state.doc.length);
  state = moveTo(state, gap);
  state = insert(state, "W");
  assert.equal(state.doc.toString(), "# A\n\nW\n\nTexto");
});

test("el cursor no se para en un separador: hacia delante sigue, hacia atrás vuelve", () => {
  let state = start("alfa\n\nbeta");
  // De la «a» final de alfa (4) a la línea 2 (5), hacia delante: a beta.
  state = moveTo(state, 4);
  state = moveTo(state, 5);
  assert.equal(state.selection.main.head, 6, "hacia delante, al principio de beta");
  state = moveTo(state, 5);
  assert.equal(state.selection.main.head, 4, "hacia atrás, al final de alfa");
  // Un clic en el aire: al final del bloque de arriba.
  state = moveTo(state, 8);
  state = moveTo(state, 5, "select.pointer");
  assert.equal(state.selection.main.head, 4);
});

test("en una nota escrita a mano con dos líneas en blanco, escribir en el vacío no se come lo de abajo", () => {
  // `alfa`, separador, vacío y `beta` pegado: se le pone su separador.
  let state = start("alfa\n\n\nbeta");
  state = moveTo(state, 6);
  state = insert(state, "x");
  assert.equal(state.doc.toString(), "alfa\n\nx\n\nbeta");
  assert.equal(state.selection.main.head, 7, "el cursor detrás de la x, no del salto nuevo");
});

test("salir de la línea y volver con un encabezado debajo conserva sus separadores", () => {
  let state = start("# A\n\n# B");
  state = enter(state, 0, 3, "h1");
  const gap = state.selection.main.head;
  state = moveTo(state, state.doc.length);
  state = moveTo(state, gap);
  state = insert(state, "W");
  assert.equal(state.doc.toString(), "# A\n\nW\n\n# B");
  assert.equal(blocks(state.doc.toString()).length, 3);
});

test("⇧⏎ al final de un párrafo: la línea vacía es del párrafo, no un separador", () => {
  // El salto suave deja el cursor en una línea que por su posición sería el
  // separador; la marca de `softLineField` la mantiene en el párrafo.
  let state = start("uno\n\nY");
  state = state.update({
    changes: { from: 3, to: 3, insert: "\n" },
    selection: { anchor: 4 },
    effects: setSoftLine.of(4),
    userEvent: "input",
  }).state;
  assert.equal(state.selection.main.head, 4, "el cursor se queda en la línea nueva");
  state = insert(state, "x");
  assert.equal(state.doc.toString(), "uno\nx\n\nY");
  assert.equal(blocks(state.doc.toString()).length, 2, "sigue siendo un solo párrafo");
});

test("sin la marca del salto suave, esa misma línea es aire", () => {
  let state = start("uno\n\nY");
  state = state.update({ changes: { from: 3, to: 3, insert: "\n" }, selection: { anchor: 4 }, userEvent: "input" }).state;
  assert.notEqual(state.selection.main.head, 4);
});
