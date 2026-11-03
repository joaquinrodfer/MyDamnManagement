// Borrado y selección de bloques: qué trozo de texto desaparece.
//
// Los fallos que fija: borrar un bloque dejaba el hueco (D1: `alfa\n\nbeta\n\ngamma`
// -> `alfa\n\n\n\ngamma`), borrar el ítem del medio partía la lista en dos (D2),
// la selección múltiple se pintaba pero solo se borraba uno (D3) y ⌫ al
// principio de un ítem pegaba el texto al de arriba sin espacio (D9).
//
// Corre sin navegador (node --test) sobre la lógica pura de block-delete.js:
// los bloques se pasan tal cual los calcula el árbol de Markdown en entry.js.
// Lo que depende de CodeMirror (que Supr/Backspace lleguen, que escribir encima
// sustituya, que ⇧/Ctrl+clic acumulen) se comprueba en la app arrancada.

import test from "node:test";
import assert from "node:assert/strict";

import { blockDeleteEdits, orderedRenumberEdits, backspaceJoinEdit } from "../block-delete.js";

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

/** Bloque de ítem de lista por texto (el rango es la fila entera). */
function item(text, line, type = "bullet_list_item") {
  const at = text.split("\n").reduce(
    (acc, l, i, all) => {
      if (acc >= 0) return acc;
      return l === line ? all.slice(0, i).reduce((n, x) => n + x.length + 1, 0) : -1;
    },
    -1
  );
  assert.ok(at >= 0, `no está la fila ${JSON.stringify(line)}`);
  return { from: at, to: at + line.length, type };
}

function deleteBlocks(text, blocks, indices, insert = "") {
  const edit = blockDeleteEdits({ text, blocks, indices, insert });
  assert.ok(edit, "tenía que haber un borrado que aplicar");
  const renumber = orderedRenumberEdits({ text, blocks, indices });
  const changes = [...edit.changes, ...renumber];
  return { text: applyChanges(text, changes), cursor: edit.cursor, changes, renumber };
}

// ------------------------------------------------------------------- D1: huecos

test("D1: borrar el bloque del medio deja un solo separador", () => {
  const text = "alfa\n\nbeta\n\ngamma";
  const blocks = [para(text, "alfa"), para(text, "beta"), para(text, "gamma")];
  assert.equal(deleteBlocks(text, blocks, [1]).text, "alfa\n\ngamma");
});

test("D1: borrar el primero no deja líneas en blanco al principio", () => {
  const text = "alfa\n\nbeta\n\ngamma";
  const blocks = [para(text, "alfa"), para(text, "beta"), para(text, "gamma")];
  assert.equal(deleteBlocks(text, blocks, [0]).text, "beta\n\ngamma");
});

test("D1: borrar el último no deja líneas en blanco al final", () => {
  const text = "alfa\n\nbeta\n\ngamma";
  const blocks = [para(text, "alfa"), para(text, "beta"), para(text, "gamma")];
  assert.equal(deleteBlocks(text, blocks, [2]).text, "alfa\n\nbeta");
});

test("D1: dos bloques seguidos, el resultado no tiene aire de más", () => {
  const text = "alfa\n\nbeta\n\ngamma\n\ndelta";
  const blocks = ["alfa", "beta", "gamma", "delta"].map((s) => para(text, s));
  assert.equal(deleteBlocks(text, blocks, [1, 2]).text, "alfa\n\ndelta");
});

test("D1: dos bloques del medio no consecutivos", () => {
  const text = "a\n\nb\n\nc\n\nd";
  const blocks = ["a", "b", "c", "d"].map((s) => para(text, s));
  assert.equal(deleteBlocks(text, blocks, [1, 3]).text, "a\n\nc");
});

test("D1: borrar el único bloque vacía el documento", () => {
  const text = "alfa";
  const blocks = [para(text, "alfa")];
  assert.equal(deleteBlocks(text, blocks, [0]).text, "");
});

test("D1: el bloque de detrás pegado a una lista no se come la lista", () => {
  const text = "alfa\n\n- uno\n- dos";
  const blocks = [para(text, "alfa"), item(text, "- uno"), item(text, "- dos")];
  assert.equal(deleteBlocks(text, blocks, [0]).text, "- uno\n- dos");
});

test("D1: el bloque de delante de una lista no deja hueco ni se come la viñeta", () => {
  const text = "- uno\n- dos\n\nparrafo";
  const blocks = [item(text, "- uno"), item(text, "- dos"), para(text, "parrafo")];
  assert.equal(deleteBlocks(text, blocks, [2]).text, "- uno\n- dos");
});

test("D1: el cursor queda donde empezaba el bloque borrado", () => {
  const text = "alfa\n\nbeta\n\ngamma";
  const blocks = [para(text, "alfa"), para(text, "beta"), para(text, "gamma")];
  const edit = blockDeleteEdits({ text, blocks, indices: [1] });
  assert.equal(edit.cursor, 6);
});

test("borrar un bloque de varias líneas se lleva todas sus líneas", () => {
  const text = "uno\ndos\n\ntres";
  const blocks = [{ from: 0, to: 7, type: "paragraph" }, para(text, "tres")];
  assert.equal(deleteBlocks(text, blocks, [0]).text, "tres");
});

// -------------------------------------------------------------------- D2: listas

test("D2: borrar el ítem del medio de una lista numerada deja UNA lista y renumera", () => {
  const text = "1. uno\n2. dos\n3. tres\n4. cuatro";
  const blocks = [
    item(text, "1. uno", "ordered_list_item"),
    item(text, "2. dos", "ordered_list_item"),
    item(text, "3. tres", "ordered_list_item"),
    item(text, "4. cuatro", "ordered_list_item"),
  ];
  const r = deleteBlocks(text, blocks, [1]);
  assert.equal(r.text, "1. uno\n2. tres\n3. cuatro");
  assert.ok(!/\n\n/.test(r.text), "la línea en blanco partiría la lista en dos");
});

test("D2: borrar el ítem del medio de una lista de viñetas deja una sola lista", () => {
  const text = "- uno\n- dos\n- tres";
  const blocks = [item(text, "- uno"), item(text, "- dos"), item(text, "- tres")];
  assert.equal(deleteBlocks(text, blocks, [1]).text, "- uno\n- tres");
});

test("D2: borrar el primer ítem renumera desde el número de la lista", () => {
  const text = "1. uno\n2. dos\n3. tres";
  const blocks = [
    item(text, "1. uno", "ordered_list_item"),
    item(text, "2. dos", "ordered_list_item"),
    item(text, "3. tres", "ordered_list_item"),
  ];
  assert.equal(deleteBlocks(text, blocks, [0]).text, "1. dos\n2. tres");
});

test("D2: borrar el último ítem no renumera lo que ya estaba bien", () => {
  const text = "1. uno\n2. dos\n3. tres";
  const blocks = [
    item(text, "1. uno", "ordered_list_item"),
    item(text, "2. dos", "ordered_list_item"),
    item(text, "3. tres", "ordered_list_item"),
  ];
  const r = deleteBlocks(text, blocks, [2]);
  assert.equal(r.text, "1. uno\n2. dos");
  assert.deepEqual(r.renumber, []);
});

test("D2: una lista que empieza en otro número conserva su arranque", () => {
  const text = "3. tres\n4. cuatro\n5. cinco";
  const blocks = [
    item(text, "3. tres", "ordered_list_item"),
    item(text, "4. cuatro", "ordered_list_item"),
    item(text, "5. cinco", "ordered_list_item"),
  ];
  assert.equal(deleteBlocks(text, blocks, [1]).text, "3. tres\n4. cinco");
});

test("D2: una lista numerada intacta no se toca al borrar un párrafo", () => {
  const text = "alfa\n\n1. uno\n3. tres";
  const blocks = [
    para(text, "alfa"),
    item(text, "1. uno", "ordered_list_item"),
    item(text, "3. tres", "ordered_list_item"),
  ];
  const r = deleteBlocks(text, blocks, [0]);
  assert.equal(r.text, "1. uno\n3. tres");
  assert.deepEqual(r.renumber, []);
});

test("D2: el separador `)` de la lista se conserva al reenumerar", () => {
  const text = "1) uno\n2) dos\n3) tres";
  const blocks = [
    item(text, "1) uno", "ordered_list_item"),
    item(text, "2) dos", "ordered_list_item"),
    item(text, "3) tres", "ordered_list_item"),
  ];
  assert.equal(deleteBlocks(text, blocks, [0]).text, "1) dos\n2) tres");
});

// ------------------------------------------------- D3: varios bloques marcados

test("D3: borrar dos bloques marcados borra los dos (y solo los dos)", () => {
  const text = "uno\n\ndos\n\ntres\n\ncuatro";
  const blocks = ["uno", "dos", "tres", "cuatro"].map((s) => para(text, s));
  assert.equal(deleteBlocks(text, blocks, [1, 2]).text, "uno\n\ncuatro");
});

test("D3: escribir encima de un bloque sustituye su texto sin pegarse al vecino", () => {
  const text = "alfa\n\nbeta\n\ngamma";
  const blocks = [para(text, "alfa"), para(text, "beta"), para(text, "gamma")];
  const edit = blockDeleteEdits({ text, blocks, indices: [1], insert: "X" });
  assert.equal(applyChanges(text, edit.changes), "alfa\n\nX\n\ngamma");
  assert.equal(edit.cursor, 7);
});

test("D3: escribir encima de DOS bloques marcados deja un solo bloque con el texto", () => {
  const text = "alfa\n\nbeta\n\ngamma\n\ndelta";
  const blocks = ["alfa", "beta", "gamma", "delta"].map((s) => para(text, s));
  const edit = blockDeleteEdits({ text, blocks, indices: [1, 2], insert: "X" });
  assert.equal(applyChanges(text, edit.changes), "alfa\n\nX\n\ndelta");
});

test("D3: escribir encima de una lista marcada no deja huecos", () => {
  const text = "1. uno\n2. dos\n3. tres";
  const blocks = [
    item(text, "1. uno", "ordered_list_item"),
    item(text, "2. dos", "ordered_list_item"),
    item(text, "3. tres", "ordered_list_item"),
  ];
  const r = deleteBlocks(text, blocks, [1], "X");
  assert.equal(r.text, "1. uno\nX\n2. tres");
});

// -------------------------------------------------------- D9: ⌫ en un ítem

test("D9: ⌫ al principio de un ítem fusiona con el de arriba dejando un espacio", () => {
  const text = "- uno\n- dos";
  const edit = backspaceJoinEdit({ text, pos: 6 });
  assert.deepEqual(edit, { from: 5, to: 8, insert: " ", cursor: 6 });
  assert.equal(text.slice(0, edit.from) + edit.insert + text.slice(edit.to), "- uno dos");
});

test("D9: lo mismo con números", () => {
  const text = "1. uno\n2. dos";
  const edit = backspaceJoinEdit({ text, pos: 7 });
  assert.equal(text.slice(0, edit.from) + edit.insert + text.slice(edit.to), "1. uno dos");
});

test("D9: la viñeta fusionada conserva la marca del primer ítem", () => {
  const text = "* uno\n- dos";
  const edit = backspaceJoinEdit({ text, pos: 6 });
  assert.equal(text.slice(0, edit.from) + edit.insert + text.slice(edit.to), "* uno dos");
});

test("D9: en el primer ítem de la lista el marcador se va y queda un párrafo", () => {
  const text = "parrafo\n\n- dos";
  const edit = backspaceJoinEdit({ text, pos: 9 });
  assert.equal(text.slice(0, edit.from) + edit.insert + text.slice(edit.to), "parrafo\n\ndos");
});

test("D9: un ítem vacío no deja el espacio colgando", () => {
  const text = "- uno\n- ";
  const edit = backspaceJoinEdit({ text, pos: 6 });
  assert.equal(text.slice(0, edit.from) + edit.insert + text.slice(edit.to), "- uno");
});

test("D9: ⌫ en mitad de la fila no es asunto de esta regla", () => {
  assert.equal(backspaceJoinEdit({ text: "- uno\n- dos", pos: 9 }), null);
});

test("D9: ⌫ en la posición 0 no hace nada", () => {
  assert.equal(backspaceJoinEdit({ text: "- uno", pos: 0 }), null);
});

test("D9: ⌫ fuera de una lista no se toca", () => {
  assert.equal(backspaceJoinEdit({ text: "uno\ndos", pos: 4 }), null);
});

// ------------------------------------------------------------ casos de borde

test("sin bloques marcados no hay cambio", () => {
  assert.equal(blockDeleteEdits({ text: "a", blocks: [para("a", "a")], indices: [] }), null);
});

test("índices fuera de rango se ignoran", () => {
  const text = "a\n\nb";
  const blocks = [para(text, "a"), para(text, "b")];
  const edit = blockDeleteEdits({ text, blocks, indices: [5, -1] });
  assert.equal(edit, null);
});
