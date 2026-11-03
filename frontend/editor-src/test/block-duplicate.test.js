// «Duplicar bloque»: la copia entra debajo del original y, en una lista
// numerada, la lista se reenumera (el mismo criterio que al borrar).
//
// Corre sin navegador (node --test) sobre la lógica pura de block-delete.js.
// Lo que depende de CodeMirror (el menú, la transacción compuesta) se comprueba
// en la app arrancada; aquí se reproduce la misma secuencia que entry.js:
// insertar los cambios y reenumerar la lista que recibe la copia.

import test from "node:test";
import assert from "node:assert/strict";

import { duplicateBlockEdits, renumberOrderedRun } from "../block-delete.js";

/** Aplica una lista de cambios a un texto (como haría ChangeSet). */
function applyChanges(text, changes) {
  const ordered = [...changes].sort((a, b) => a.from - b.from || a.to - b.to);
  let out = "";
  let at = 0;
  for (const c of ordered) {
    assert.ok(c.from >= at, `cambios solapados: ${JSON.stringify(ordered)}`);
    out += text.slice(at, c.from) + (c.insert || "");
    at = c.to;
  }
  return out + text.slice(at);
}

/** Bloque de párrafo por texto: busca `s` en el documento. */
function para(text, s, nth = 0) {
  let at = -1;
  for (let i = 0; i <= nth; i++) at = text.indexOf(s, at + 1);
  assert.ok(at >= 0, `no está ${JSON.stringify(s)} en ${JSON.stringify(text)}`);
  return { from: at, to: at + s.length, type: "paragraph" };
}

/** Bloques de una lista numerada, una fila = un ítem. */
function numbered(text) {
  const out = [];
  let at = 0;
  for (const line of text.split("\n")) {
    out.push({ from: at, to: at + line.length, type: "ordered_list_item" });
    at += line.length + 1;
  }
  return out;
}

/** Bloques del documento DESPUÉS de insertar: los de antes corren lo que se
 * ha insertado por delante, y la copia entra como un bloque más. */
function blocksAfterInsert(blocks, changes) {
  const shiftOf = (pos) =>
    changes.filter((c) => c.from < pos).reduce((n, c) => n + c.insert.length, 0);
  const out = blocks.map((b) => ({ ...b, from: b.from + shiftOf(b.from), to: b.to + shiftOf(b.to) }));
  let acc = 0;
  for (const c of changes) {
    const bodyLength = c.insert.length - (c.copy - c.from);
    const type = blocks.find((b) => b.to === c.from)?.type ?? "paragraph";
    out.push({ from: c.copy + acc, to: c.copy + acc + bodyLength, type });
    acc += c.insert.length;
  }
  return out.sort((a, b) => a.from - b.from);
}

/** Misma secuencia que entry.js: insertar y reenumerar la lista tocada. */
function duplicate(text, blocks, indices) {
  const changes = duplicateBlockEdits({ text, blocks, indices });
  const once = applyChanges(text, changes);
  const nextBlocks = blocksAfterInsert(blocks, changes);
  const out = [];
  let shift = 0;
  for (const change of changes) {
    const at = change.copy + shift;
    shift += change.insert.length;
    const copyIndex = nextBlocks.findIndex((b) => b.from === at);
    assert.ok(copyIndex !== -1, `no encuentro la copia en ${at}: ${JSON.stringify(nextBlocks)}`);
    out.push(...renumberOrderedRun({ text: once, blocks: nextBlocks, index: copyIndex }));
  }
  return applyChanges(once, out);
}

test("duplicar un párrafo: una copia con línea en blanco", () => {
  const text = "a\n\nb";
  const blocks = [para(text, "a"), para(text, "b")];
  assert.equal(duplicate(text, blocks, [1]), "a\n\nb\n\nb");
});

test("duplicar el primero: la copia va debajo, no al final", () => {
  const text = "a\n\nb\n\nc";
  const blocks = [para(text, "a"), para(text, "b"), para(text, "c")];
  assert.equal(duplicate(text, blocks, [0]), "a\n\na\n\nb\n\nc");
});

test("duplicar dos bloques pegados: UNA copia de los dos, en orden", () => {
  const text = "a\n\nb\n\nc";
  const blocks = [para(text, "a"), para(text, "b"), para(text, "c")];
  assert.equal(duplicate(text, blocks, [0, 1]), "a\n\nb\n\na\n\nb\n\nc");
});

test("duplicar un bloque de varias líneas copia el bloque entero", () => {
  const text = "uno\ndos\n\ntres";
  const blocks = [para(text, "uno\ndos"), para(text, "tres")];
  assert.equal(duplicate(text, blocks, [0]), "uno\ndos\n\nuno\ndos\n\ntres");
});

test("duplicar un ítem de lista: se queda en LA MISMA lista (un salto)", () => {
  const text = "- uno\n- dos";
  const blocks = [
    { from: 0, to: 5, type: "bullet_list_item" },
    { from: 6, to: 11, type: "bullet_list_item" },
  ];
  assert.equal(duplicate(text, blocks, [0]), "- uno\n- uno\n- dos");
});

test("duplicar un ítem numerado reenumera la lista", () => {
  const text = "1. uno\n2. dos\n3. tres";
  const blocks = numbered(text);
  assert.equal(duplicate(text, blocks, [1]), "1. uno\n2. dos\n3. dos\n4. tres");
});

test("duplicar el primero de una numerada corre todos los números", () => {
  const text = "1. uno\n2. dos\n3. tres";
  const blocks = numbered(text);
  assert.equal(duplicate(text, blocks, [0]), "1. uno\n2. uno\n3. dos\n4. tres");
});

test("una numerada que no empieza en 1 conserva su número de arranque", () => {
  const text = "5. cinco\n6. seis";
  const blocks = numbered(text);
  assert.equal(duplicate(text, blocks, [0]), "5. cinco\n6. cinco\n7. seis");
});

test("duplicar una lista numerada DESPUÉS de un párrafo no toca el párrafo", () => {
  const text = "intro\n\n1. uno\n2. dos";
  const blocks = [
    para(text, "intro"),
    { from: 7, to: 13, type: "ordered_list_item" },
    { from: 14, to: 20, type: "ordered_list_item" },
  ];
  assert.equal(duplicate(text, blocks, [1]), "intro\n\n1. uno\n2. uno\n3. dos");
});

test("sin bloques marcados no hay cambio", () => {
  const text = "a";
  assert.deepEqual(duplicateBlockEdits({ text, blocks: [para(text, "a")], indices: [] }), []);
});

test("índices fuera de rango se ignoran", () => {
  const text = "a\n\nb";
  const blocks = [para(text, "a"), para(text, "b")];
  assert.deepEqual(duplicateBlockEdits({ text, blocks, indices: [9, -2] }), []);
});
