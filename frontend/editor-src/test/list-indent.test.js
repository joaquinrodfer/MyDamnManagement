// Tab / ⇧Tab en listas (list-indent.js): meter y sacar ítems, como en Notion.

import test from "node:test";
import assert from "node:assert/strict";

import { listIndentEdit } from "../list-indent.js";

/** Aplica la tecla con el cursor en la posición de `|` y devuelve el texto
 * con `|` donde queda el cursor. */
function press(marked, dir) {
  const pos = marked.indexOf("|");
  const text = marked.replace("|", "");
  const edit = listIndentEdit(text, pos, dir);
  if (!edit) return null;
  if (edit.noop) return "noop";
  const out = text.slice(0, edit.from) + edit.insert + text.slice(edit.to);
  return out.slice(0, edit.cursor) + "|" + out.slice(edit.cursor);
}

test("Tab mete una viñeta debajo de la de arriba", () => {
  assert.equal(press("- uno\n- d|os", 1), "- uno\n  - d|os");
});

test("Tab se lleva lo que cuelga del ítem", () => {
  assert.equal(press("- uno\n- d|os\n  - hijo\n- tres", 1), "- uno\n  - d|os\n    - hijo\n- tres");
});

test("Tab en el primer ítem de la lista no hace nada (pero se consume)", () => {
  assert.equal(press("- u|no\n- dos", 1), "noop");
});

test("Tab fuera de una lista no es cosa de este módulo", () => {
  assert.equal(press("hola| mundo", 1), null);
});

test("Tab en una numerada: el ítem empieza en 1 y los de detrás se corren", () => {
  assert.equal(press("1. a\n2. b|\n3. c", 1), "1. a\n   1. b|\n2. c");
});

test("Tab en una numerada con hijos ya numerados sigue la cuenta", () => {
  assert.equal(press("1. a\n   1. x\n2. b|\n3. c", 1), "1. a\n   1. x\n   2. b|\n2. c");
});

test("⇧Tab saca una viñeta al nivel del padre", () => {
  assert.equal(press("- uno\n  - d|os", -1), "- uno\n- d|os");
});

test("⇧Tab: los hermanos de detrás pasan a colgar del ítem", () => {
  assert.equal(press("- a\n  - b|\n  - c", -1), "- a\n- b|\n  - c");
});

test("⇧Tab en una numerada: sigue al padre y rehace los números", () => {
  assert.equal(press("1. a\n   1. b|\n   2. c\n2. d", -1), "1. a\n2. b|\n   1. c\n3. d");
});

test("⇧Tab en el nivel de arriba no hace nada", () => {
  assert.equal(press("- u|no", -1), "noop");
});

test("en una línea de continuación del ítem, mueve el ítem entero", () => {
  assert.equal(press("- uno\n- dos\n  si|gue", 1), "- uno\n  - dos\n    si|gue");
});
