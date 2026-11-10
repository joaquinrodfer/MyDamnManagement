// El comando del keymap de formato, tal cual: ⌘/Ctrl+B / I / U.
//
// El test puro (format.test.js) fija QUÉ cambia en el documento; este fija que
// la tecla haga UNA sola transacción -- un solo ⌘Z deshace el formato aunque
// la selección sean varias líneas --, que la selección se quede sobre el texto
// y que dentro de código la tecla se consuma sin tocar nada.
//
// El estado es un EditorState de verdad (con el parser de Markdown, que es de
// donde sale el árbol usado para saber qué es código); la vista es un doble,
// como en test/block-move-view.test.js: el comando solo mira la selección, el
// documento y el árbol.

import test from "node:test";
import assert from "node:assert/strict";

import { EditorSelection, EditorState } from "@codemirror/state";
import { markdown } from "@codemirror/lang-markdown";

import { runFormat } from "../entry.js";

function viewAt(doc, ranges) {
  const state = EditorState.create({
    doc,
    selection: EditorSelection.create(ranges.map((r) => EditorSelection.range(r.from, r.to))),
    extensions: [markdown({ addKeymap: false })],
  });
  return {
    state,
    dispatches: [],
    dispatch(spec) {
      this.dispatches.push(spec);
      this.state = this.state.update(spec).state;
    },
    focus() {},
  };
}

function documentOf(view) {
  return view.state.doc.toString();
}

function selectionOf(view) {
  return view.state.selection.ranges.map((r) => ({ from: r.from, to: r.to }));
}

test("⌘B sobre una palabra: una transacción, el texto envuelto y la selección encima", () => {
  const view = viewAt("hola mundo", [{ from: 0, to: 4 }]);
  assert.equal(runFormat(view, "bold"), true);
  assert.equal(view.dispatches.length, 1);
  assert.equal(view.dispatches[0].userEvent, "input.format");
  assert.equal(documentOf(view), "**hola** mundo");
  assert.deepEqual(selectionOf(view), [{ from: 2, to: 6 }]);
});

test("⌘I y ⌘U también van en una sola transacción", () => {
  const i = viewAt("hola mundo", [{ from: 5, to: 10 }]);
  runFormat(i, "italic");
  assert.equal(i.dispatches.length, 1);
  assert.equal(documentOf(i), "hola *mundo*");

  const u = viewAt("hola mundo", [{ from: 5, to: 10 }]);
  runFormat(u, "underline");
  assert.equal(u.dispatches.length, 1);
  assert.equal(documentOf(u), "hola <u>mundo</u>");
});

test("otra pulsación quita el formato (misma tecla, alternancia)", () => {
  const view = viewAt("hola mundo", [{ from: 0, to: 4 }]);
  runFormat(view, "bold");
  const inner = selectionOf(view)[0];
  view.state = view.state.update({ selection: EditorSelection.range(inner.from, inner.to) }).state;
  runFormat(view, "bold");
  assert.equal(documentOf(view), "hola mundo");
  assert.deepEqual(selectionOf(view), [{ from: 0, to: 4 }]);
});

test("tres líneas: UNA sola transacción (un solo ⌘Z) con las tres envueltas", () => {
  const doc = "uno\ndos\ntres";
  const view = viewAt(doc, [{ from: 0, to: doc.length }]);
  runFormat(view, "bold");
  assert.equal(view.dispatches.length, 1);
  assert.equal(documentOf(view), "**uno**\n**dos**\n**tres**");
});

test("dentro de un bloque de código la tecla se consume y no toca nada", () => {
  const doc = "```\ncodigo\n```";
  const view = viewAt(doc, [{ from: 4, to: 10 }]);
  assert.equal(runFormat(view, "bold"), true);
  assert.equal(view.dispatches.length, 0);
  assert.equal(documentOf(view), doc);
});

test("en un `código` en línea tampoco", () => {
  const doc = "usa `codigo` y mas";
  const view = viewAt(doc, [{ from: 5, to: 11 }]);
  assert.equal(runFormat(view, "underline"), true);
  assert.equal(view.dispatches.length, 0);
  assert.equal(documentOf(view), doc);
});

test("sin selección: escribe el par vacío y deja el cursor en medio", () => {
  const view = viewAt("hola ", [{ from: 5, to: 5 }]);
  runFormat(view, "bold");
  assert.equal(documentOf(view), "hola ****");
  assert.deepEqual(selectionOf(view), [{ from: 7, to: 7 }]);
});

test("dos rangos, uno dentro de código: el de fuera se envuelve y el código no se toca", () => {
  const doc = "uno\n\n```\ncodigo\n```";
  const view = viewAt(doc, [{ from: 0, to: 3 }, { from: 9, to: 15 }]);
  assert.equal(runFormat(view, "bold"), true);
  assert.equal(view.dispatches.length, 1);
  assert.equal(documentOf(view), "**uno**\n\n```\ncodigo\n```");
  assert.deepEqual(selectionOf(view), [{ from: 2, to: 5 }]);
});
