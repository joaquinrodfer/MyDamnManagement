// Borrado y selección de bloques: qué trozo de texto desaparece.
//
// Los fallos que fija: borrar un bloque dejaba el hueco (D1: `alfa\n\nbeta\n\ngamma`
// -> `alfa\n\n\n\ngamma`), borrar el ítem del medio partía la lista en dos (D2),
// la selección múltiple se pintaba pero solo se borraba uno (D3) y ⌫ al
// principio de un ítem pegaba el texto al de arriba sin espacio (D9).
//
// Y la regla que comparten todos: entre una lista, una cita o un bloque de
// código y el bloque que venga detrás va SIEMPRE una línea en blanco (si no,
// Markdown lee la primera línea de detrás como continuación del anterior), con
// la única excepción de dos ítems de lista seguidos, que van en el mismo salto.
//
// Corre sin navegador (node --test) sobre la lógica pura de block-delete.js:
// los bloques se pasan tal cual los calcula el árbol de Markdown en entry.js.
// Lo que depende de CodeMirror (que Supr/Backspace lleguen, que escribir encima
// sustituya, que ⇧/Ctrl+clic acumulen) se comprueba en la app arrancada.

import test from "node:test";
import assert from "node:assert/strict";

import {
  blockDeleteEdits,
  backspaceJoinEdit,
  backspaceWouldBreakFence,
  convertBlockEdits,
  newBlockEdit,
  orderedRenumberEdits,
} from "../block-delete.js";

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

/** Bloque de código: de la primera valla al final del documento. */
function code(text, at = text.indexOf("```")) {
  assert.ok(at >= 0, "no hay valla en el documento");
  return { from: at, to: text.length, type: "code" };
}

function deleteBlocks(text, blocks, indices, insert = "") {
  const edit = blockDeleteEdits({ text, blocks, indices, insert });
  assert.ok(edit, "tenía que haber un borrado que aplicar");
  const renumber = orderedRenumberEdits({ text, blocks, indices, keepFirst: edit.keepsFirstItem });
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

test("D1: borrar el ÚLTIMO ítem de una lista no se come el bloque de detrás", () => {
  const text = "- uno\n- dos\n\nparrafo";
  const blocks = [item(text, "- uno"), item(text, "- dos"), para(text, "parrafo")];
  assert.equal(deleteBlocks(text, blocks, [1]).text, "- uno\n\nparrafo");
});

test("D1: lo mismo con una lista numerada", () => {
  const text = "1. uno\n2. dos\n\nparrafo";
  const blocks = [
    item(text, "1. uno", "ordered_list_item"),
    item(text, "2. dos", "ordered_list_item"),
    para(text, "parrafo"),
  ];
  assert.equal(deleteBlocks(text, blocks, [1]).text, "1. uno\n\nparrafo");
});

test("D1: el último ítem no se pega a un bloque de código que viene detrás", () => {
  const text = "- uno\n- dos\n\n```\ncodigo\n```";
  const blocks = [item(text, "- uno"), item(text, "- dos"), code(text)];
  assert.equal(deleteBlocks(text, blocks, [1]).text, "- uno\n\n```\ncodigo\n```");
});

test("D1: el último ítem no se pega a una cita que viene detrás", () => {
  const text = "- uno\n- dos\n\n> cita";
  const blocks = [
    item(text, "- uno"),
    item(text, "- dos"),
    { from: text.indexOf("> cita"), to: text.length, type: "quote" },
  ];
  assert.equal(deleteBlocks(text, blocks, [1]).text, "- uno\n\n> cita");
});

test("D1: varios marcados que acaban en ítem de lista + párrafo", () => {
  const text = "- a\n- b\n\nx\n\ny";
  const blocks = [item(text, "- a"), item(text, "- b"), para(text, "x"), para(text, "y")];
  assert.equal(deleteBlocks(text, blocks, [1, 2]).text, "- a\n\ny");
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

test("D2: una lista suelta (con línea en blanco entre ítems) también se reenumera", () => {
  const text = "1. uno\n\n2. dos\n\n3. tres";
  const blocks = [
    item(text, "1. uno", "ordered_list_item"),
    item(text, "2. dos", "ordered_list_item"),
    item(text, "3. tres", "ordered_list_item"),
  ];
  assert.equal(deleteBlocks(text, blocks, [1]).text, "1. uno\n\n2. tres");
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

test("D3: escribir encima de un ítem marcado hereda el marcador (sigue siendo bloque)", () => {
  const text = "1. uno\n2. dos\n3. tres";
  const blocks = [
    item(text, "1. uno", "ordered_list_item"),
    item(text, "2. dos", "ordered_list_item"),
    item(text, "3. tres", "ordered_list_item"),
  ];
  assert.equal(deleteBlocks(text, blocks, [1], "X").text, "1. uno\n2. X\n3. tres");
});

test("D3: lo mismo con una viñeta", () => {
  const text = "- uno\n- dos\n- tres";
  const blocks = [item(text, "- uno"), item(text, "- dos"), item(text, "- tres")];
  assert.equal(deleteBlocks(text, blocks, [1], "X").text, "- uno\n- X\n- tres");
});

test("D3: escribir encima de dos ítems marcados deja un ítem y renumera el resto", () => {
  const text = "1. uno\n2. dos\n3. tres\n4. cuatro";
  const blocks = [
    item(text, "1. uno", "ordered_list_item"),
    item(text, "2. dos", "ordered_list_item"),
    item(text, "3. tres", "ordered_list_item"),
    item(text, "4. cuatro", "ordered_list_item"),
  ];
  assert.equal(deleteBlocks(text, blocks, [1, 2], "X").text, "1. uno\n2. X\n3. cuatro");
});

test("D3: escribir encima del último ítem de una lista no se come lo de detrás", () => {
  const text = "1. uno\n2. dos\n\nparrafo";
  const blocks = [
    item(text, "1. uno", "ordered_list_item"),
    item(text, "2. dos", "ordered_list_item"),
    para(text, "parrafo"),
  ];
  assert.equal(deleteBlocks(text, blocks, [1], "X").text, "1. uno\n2. X\n\nparrafo");
});

test("D3: un texto de varias líneas encima de un ítem se queda dentro del ítem", () => {
  const text = "1. uno\n2. dos\n3. tres";
  const blocks = [
    item(text, "1. uno", "ordered_list_item"),
    item(text, "2. dos", "ordered_list_item"),
    item(text, "3. tres", "ordered_list_item"),
  ];
  assert.equal(deleteBlocks(text, blocks, [1], "X\nY").text, "1. uno\n2. X\n   Y\n3. tres");
});

// ------------------------------------------- ⏎ con bloques marcados

test("⏎ con un bloque marcado deja UN bloque vacío, no cinco líneas en blanco", () => {
  const text = "uno\n\ndos\n\ntres";
  const blocks = ["uno", "dos", "tres"].map((s) => para(text, s));
  const edit = newBlockEdit({ text, blocks, indices: [1] });
  assert.equal(applyChanges(text, edit.changes), "uno\n\n\n\ntres");
  assert.equal(edit.cursor, 5); // dentro del bloque vacío: escribir ahí da "uno\n\nnuevo\n\ntres"
});

test("⏎ con dos bloques marcados deja uno solo vacío", () => {
  const text = "alfa\n\nbeta\n\ngamma\n\ndelta";
  const blocks = ["alfa", "beta", "gamma", "delta"].map((s) => para(text, s));
  const edit = newBlockEdit({ text, blocks, indices: [1, 2] });
  assert.equal(applyChanges(text, edit.changes), "alfa\n\n\n\ndelta");
});

test("⏎ con el último bloque marcado deja el hueco al final, sin pegarlo", () => {
  const text = "uno\n\ndos";
  const blocks = ["uno", "dos"].map((s) => para(text, s));
  const edit = newBlockEdit({ text, blocks, indices: [1] });
  assert.equal(applyChanges(text, edit.changes), "uno\n\n");
  assert.equal(edit.cursor, 5); // al final, en la línea vacía
});

test("⏎ con el primer bloque marcado deja el hueco delante", () => {
  const text = "uno\n\ndos";
  const blocks = ["uno", "dos"].map((s) => para(text, s));
  const edit = newBlockEdit({ text, blocks, indices: [0] });
  assert.equal(applyChanges(text, edit.changes), "\n\ndos");
  assert.equal(edit.cursor, 0);
});

// -------------------------------------------------------- D9: ⌫ en un ítem

const bulletBlocks = (text) => text.split("\n").map((line) => item(text, line));

test("D9: ⌫ al principio de un ítem fusiona con el de arriba dejando un espacio", () => {
  const text = "- uno\n- dos";
  const edit = backspaceJoinEdit({ text, pos: 6, blocks: bulletBlocks(text) });
  assert.deepEqual(edit, { from: 5, to: 8, insert: " ", cursor: 6 });
  assert.equal(text.slice(0, edit.from) + edit.insert + text.slice(edit.to), "- uno dos");
});

test("D9: lo mismo con números", () => {
  const text = "1. uno\n2. dos";
  const blocks = [item(text, "1. uno", "ordered_list_item"), item(text, "2. dos", "ordered_list_item")];
  const edit = backspaceJoinEdit({ text, pos: 7, blocks });
  assert.equal(text.slice(0, edit.from) + edit.insert + text.slice(edit.to), "1. uno dos");
});

test("D9: la viñeta fusionada conserva la marca del primer ítem", () => {
  const text = "* uno\n- dos";
  const edit = backspaceJoinEdit({ text, pos: 6, blocks: bulletBlocks(text) });
  assert.equal(text.slice(0, edit.from) + edit.insert + text.slice(edit.to), "* uno dos");
});

test("D9: en el primer ítem de la lista el marcador se va y queda un párrafo", () => {
  const text = "parrafo\n\n- dos";
  const blocks = [para(text, "parrafo"), item(text, "- dos")];
  const edit = backspaceJoinEdit({ text, pos: 9, blocks });
  assert.equal(text.slice(0, edit.from) + edit.insert + text.slice(edit.to), "parrafo\n\ndos");
});

test("D9: un ítem vacío no deja el espacio colgando", () => {
  const text = "- uno\n- ";
  const edit = backspaceJoinEdit({ text, pos: 6, blocks: bulletBlocks(text) });
  assert.equal(text.slice(0, edit.from) + edit.insert + text.slice(edit.to), "- uno");
});

test("D9: ⌫ en mitad de la fila no es asunto de esta regla", () => {
  const text = "- uno\n- dos";
  assert.equal(backspaceJoinEdit({ text, pos: 9, blocks: bulletBlocks(text) }), null);
});

test("D9: ⌫ en la posición 0 no hace nada", () => {
  const text = "- uno";
  assert.equal(backspaceJoinEdit({ text, pos: 0, blocks: bulletBlocks(text) }), null);
});

test("D9: ⌫ fuera de una lista no se toca", () => {
  const text = "uno\ndos";
  const blocks = [{ from: 0, to: text.length, type: "paragraph" }];
  assert.equal(backspaceJoinEdit({ text, pos: 4, blocks }), null);
});

test("D9: ⌫ dentro de una valla de código NO borra el `- ` (es texto, no un ítem)", () => {
  const text = "```\n- uno\n- dos\n```";
  assert.equal(backspaceJoinEdit({ text, pos: 10, blocks: [code(text, 0)] }), null);
});

test("D9: ⌫ entre dos líneas que parecen lista pero son un párrafo", () => {
  const text = "texto\n- dos";
  const blocks = [{ from: 0, to: text.length, type: "paragraph" }];
  assert.equal(backspaceJoinEdit({ text, pos: 6, blocks }), null);
});

test("D9: sin los bloques del árbol no adivina (mejor fallar que comerse texto)", () => {
  assert.throws(() => backspaceJoinEdit({ text: "- uno\n- dos", pos: 6 }), TypeError);
});

// --------------------------------------------------- ⌫ y la valla del código
//
// Un ⌫ que una una valla con la fila de arriba convierte la valla en texto: el
// `.md` deja de tener bloque de código (` ```codigo `) o, si es el cierre, el
// bloque se queda sin cerrar y al recargar se traga todo lo que venga detrás.
// Las tres filas de la valla (apertura, primera de contenido y cierre) se
// consumen sin tocar nada.

/** Bloques de un documento con una sola valla (y un párrafo detrás, si lo hay). */
function fenced(text, paragraphAt = -1) {
  const close = text.lastIndexOf("```");
  const blocks = [{ from: 0, to: close + 3, type: "code" }];
  if (paragraphAt >= 0) blocks.push({ from: paragraphAt, to: text.length, type: "paragraph" });
  return blocks;
}

test("valla: ⌫ al principio de la primera línea de contenido no une con la apertura", () => {
  const text = "```\n- uno\n```";
  assert.equal(backspaceWouldBreakFence({ text, pos: 4, blocks: fenced(text) }), true);
});

test("valla: ⌫ al principio de la valla de cierre no une el cierre con el código", () => {
  const text = "```\ncodigo\n```\n\nparrafo";
  const blocks = fenced(text, text.indexOf("parrafo"));
  assert.equal(backspaceWouldBreakFence({ text, pos: text.indexOf("```", 3), blocks }), true);
});

test("valla: con lenguaje, ⌫ en la primera línea de contenido tampoco toca la valla", () => {
  const text = "```js\nuno\n```";
  assert.equal(backspaceWouldBreakFence({ text, pos: text.indexOf("uno"), blocks: fenced(text) }), true);
});

test("valla: ⌫ al principio de la apertura con texto encima se consume (no deja `parrafo``` `)", () => {
  const text = "parrafo\n```\nuno\n```";
  const open = text.indexOf("```");
  const blocks = [para(text, "parrafo"), { from: open, to: text.length, type: "code" }];
  assert.equal(backspaceWouldBreakFence({ text, pos: open, blocks }), true);
});

test("valla: con línea en blanco encima, ⌫ en la apertura solo quita el hueco (no se consume)", () => {
  const text = "parrafo\n\n```\nuno\n```";
  const open = text.indexOf("```");
  const blocks = [para(text, "parrafo"), { from: open, to: text.length, type: "code" }];
  assert.equal(backspaceWouldBreakFence({ text, pos: open, blocks }), false);
});

test("valla: ⌫ entre dos líneas de contenido sí fusiona (la tecla no se consume)", () => {
  const text = "```\nuno\ndos\n```";
  assert.equal(backspaceWouldBreakFence({ text, pos: text.indexOf("dos"), blocks: fenced(text) }), false);
});

test("valla: ⌫ en mitad de la fila de la valla no es asunto de esta regla", () => {
  const text = "```\ncodigo\n```";
  const blocks = fenced(text);
  assert.equal(backspaceWouldBreakFence({ text, pos: text.indexOf("```", 3) + 1, blocks }), false);
});

test("valla: un bloque de código con sangría no tiene valla que romper", () => {
  const text = "    uno\n    dos";
  const blocks = [{ from: 0, to: text.length, type: "code" }];
  assert.equal(backspaceWouldBreakFence({ text, pos: text.indexOf("    dos"), blocks }), false);
});

test("valla: fuera de un bloque de código no aplica", () => {
  const text = "uno\ndos";
  const blocks = [{ from: 0, to: text.length, type: "paragraph" }];
  assert.equal(backspaceWouldBreakFence({ text, pos: 4, blocks }), false);
});

test("valla: sin los bloques del árbol no adivina (mejor fallar que comerse la valla)", () => {
  assert.throws(() => backspaceWouldBreakFence({ text: "```\nuno\n```", pos: 4 }), TypeError);
});

// ------------------------------------------------------------- convertir

test("convertir un ítem en párrafo lo deja como bloque propio, con línea en blanco", () => {
  const text = "1. uno\n2. dos\n3. tres";
  const blocks = [
    item(text, "1. uno", "ordered_list_item"),
    item(text, "2. dos", "ordered_list_item"),
    item(text, "3. tres", "ordered_list_item"),
  ];
  const changes = convertBlockEdits({
    text,
    blocks,
    indices: [1],
    convert: (raw) => raw.replace(/^\d+[.)][ \t]+/, ""),
  });
  assert.equal(applyChanges(text, changes), "1. uno\n\ndos\n\n3. tres");
});

test("convertir un párrafo en código conserva una línea en blanco a cada lado", () => {
  const text = "alfa\n\nbeta\n\ngamma";
  const blocks = [para(text, "alfa"), para(text, "beta"), para(text, "gamma")];
  const changes = convertBlockEdits({
    text,
    blocks,
    indices: [1],
    convert: (raw) => "```\n" + raw + "\n```",
  });
  assert.equal(applyChanges(text, changes), "alfa\n\n```\nbeta\n```\n\ngamma");
});

test("convertir dos ítems seguidos deja dos párrafos, no una línea pegada", () => {
  const text = "1. uno\n2. dos\n3. tres";
  const blocks = [
    item(text, "1. uno", "ordered_list_item"),
    item(text, "2. dos", "ordered_list_item"),
    item(text, "3. tres", "ordered_list_item"),
  ];
  const changes = convertBlockEdits({
    text,
    blocks,
    indices: [0, 1],
    convert: (raw) => raw.replace(/^\d+[.)][ \t]+/, ""),
  });
  assert.equal(applyChanges(text, changes), "uno\n\ndos\n\n3. tres");
});

test("convertir un ítem en otro ítem no mete líneas en blanco en la lista", () => {
  const text = "- uno\n- dos\n- tres";
  const blocks = [item(text, "- uno"), item(text, "- dos"), item(text, "- tres")];
  const changes = convertBlockEdits({
    text,
    blocks,
    indices: [1],
    convert: (raw) => "1. " + raw.replace(/^[-*+][ \t]+/, ""),
  });
  assert.equal(applyChanges(text, changes), "- uno\n1. dos\n- tres");
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
