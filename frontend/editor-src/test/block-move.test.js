// Mover bloques e insertar uno arriba: qué trozo de texto cambia de sitio.
//
// Los fallos que fija: insertar arriba dejaba el bloque nuevo pegado al de
// arriba (o metía dos líneas en blanco de más), mover un párrafo por encima o
// por debajo de una valla partía el bloque de código, y reordenar una lista
// numerada dejaba los números como estaban (3. uno / 1. dos / 2. tres).
//
// Corre sin navegador (node --test) sobre la lógica pura de block-move.js: los
// bloques se pasan tal cual los calcula el árbol de Markdown en entry.js. Lo
// que depende de CodeMirror (que Alt+↑/↓ llegue, que un solo ⌘Z lo deshaga,
// que el arrastre pinte la línea de inserción) se comprueba en la app
// arrancada y en test/block-move-view.test.js.

import test from "node:test";
import assert from "node:assert/strict";

import { insertBlockEdit, moveBlockEdits } from "../block-move.js";

/** Aplica los cambios de un resultado (como haría ChangeSet). */
function apply(text, changes) {
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

/** Bloques consecutivos por su texto, como los daría `computeBlocks`. */
function blocksOf(text, specs) {
  let at = 0;
  return specs.map(([s, type]) => {
    const from = text.indexOf(s, at);
    assert.ok(from >= 0, `no está ${JSON.stringify(s)} en ${JSON.stringify(text)}`);
    at = from + s.length;
    return { from, to: at, type };
  });
}

function move(text, specs, index, slot, cursor = null) {
  const blocks = blocksOf(text, specs);
  const edit = moveBlockEdits({ text, blocks, index, slot, cursor });
  return edit ? { ...edit, text: apply(text, edit.changes), blocks } : null;
}

const sortedLines = (s) => s.split("\n").sort();

// ------------------------------------------------------- insertar arriba / abajo

test("insertar arriba pone el bloque nuevo ANTES del actual y el de abajo no se mueve", () => {
  const text = "alfa\n\nbeta";
  const blocks = blocksOf(text, [
    ["alfa", "paragraph"],
    ["beta", "paragraph"],
  ]);
  const edit = insertBlockEdit({ blocks, index: 1, where: "above" });
  assert.deepEqual(edit.changes, [{ from: 6, to: 6, insert: "\n\n" }]);

  // Lo que hace el editor al teclear en el bloque nuevo: primero entra el par
  // de saltos y el cursor cae entre los dos huecos; el texto va ahí.
  const withGap = apply(text, edit.changes);
  const typed = withGap.slice(0, edit.cursor) + "nuevo" + withGap.slice(edit.cursor);
  assert.equal(typed, "alfa\n\nnuevo\n\nbeta");
  assert.equal(edit.gapFrom, 6);
});

test("insertar debajo sigue siendo lo de siempre (el par va detrás del bloque)", () => {
  const text = "alfa\n\nbeta";
  const blocks = blocksOf(text, [
    ["alfa", "paragraph"],
    ["beta", "paragraph"],
  ]);
  const edit = insertBlockEdit({ blocks, index: 0, where: "below" });
  assert.deepEqual(edit.changes, [{ from: 4, to: 4, insert: "\n\n" }]);
  assert.equal(edit.cursor, 6);
  const withGap = apply(text, edit.changes);
  const typed = withGap.slice(0, edit.cursor) + "nuevo" + withGap.slice(edit.cursor);
  assert.equal(typed, "alfa\n\nnuevo\n\nbeta");
});

// -------------------------------------------------------------- mover arriba / abajo

test("mover arriba: el bloque sube uno y el de encima baja", () => {
  const out = move(
    "alfa\n\nbeta\n\ngamma",
    [
      ["alfa", "paragraph"],
      ["beta", "paragraph"],
      ["gamma", "paragraph"],
    ],
    2,
    1
  );
  assert.equal(out.text, "alfa\n\ngamma\n\nbeta");
});

test("mover abajo: el bloque baja uno", () => {
  const out = move(
    "alfa\n\nbeta\n\ngamma",
    [
      ["alfa", "paragraph"],
      ["beta", "paragraph"],
      ["gamma", "paragraph"],
    ],
    0,
    2
  );
  assert.equal(out.text, "beta\n\nalfa\n\ngamma");
});

test("mover no añade ni quita líneas: el .md queda con las mismas, en otro orden", () => {
  const text = "uno\n\ndos\n\ntres\n\ncuatro";
  const out = move(
    text,
    [
      ["uno", "paragraph"],
      ["dos", "paragraph"],
      ["tres", "paragraph"],
      ["cuatro", "paragraph"],
    ],
    2,
    0
  );
  assert.deepEqual(sortedLines(out.text), sortedLines(text));
  assert.equal(out.text.split("\n").length, text.split("\n").length);
});

test("el cursor se queda dentro del bloque movido (Alt+↑ repetido sigue con el mismo)", () => {
  const text = "alfa\n\nbeta\n\ngamma";
  const blocks = blocksOf(text, [
    ["alfa", "paragraph"],
    ["beta", "paragraph"],
    ["gamma", "paragraph"],
  ]);
  const edit = moveBlockEdits({ text, blocks, index: 2, slot: 1, cursor: blocks[2].from + 2 });
  assert.equal(edit.cursor, 6 + 2); // gamma empieza en 6 tras moverlo
});

test("mover a donde ya está no es un movimiento", () => {
  const text = "alfa\n\nbeta";
  const blocks = blocksOf(text, [
    ["alfa", "paragraph"],
    ["beta", "paragraph"],
  ]);
  assert.equal(moveBlockEdits({ text, blocks, index: 0, slot: 0 }), null);
  assert.equal(moveBlockEdits({ text, blocks, index: 0, slot: 1 }), null);
  assert.equal(moveBlockEdits({ text, blocks, index: 2, slot: 1 }), null);
  assert.equal(moveBlockEdits({ text, blocks, index: 0, slot: 9 }), null);
});

// ---------------------------------------------------------------- valla de código

test("mover un párrafo por ENCIMA de una valla no la toca", () => {
  const text = "p1\n\n```\nconst x = 1;\n```\n\np2";
  const out = move(
    text,
    [
      ["p1", "paragraph"],
      ["```\nconst x = 1;\n```", "code"],
      ["p2", "paragraph"],
    ],
    2,
    1
  );
  assert.equal(out.text, "p1\n\np2\n\n```\nconst x = 1;\n```");
  assert.equal((out.text.match(/```/g) || []).length, 2);
});

test("mover un párrafo por DEBAJO de una valla no la toca", () => {
  const text = "p1\n\np2\n\n```\nconst x = 1;\n```";
  const out = move(
    text,
    [
      ["p1", "paragraph"],
      ["p2", "paragraph"],
      ["```\nconst x = 1;\n```", "code"],
    ],
    1,
    3
  );
  assert.equal(out.text, "p1\n\n```\nconst x = 1;\n```\n\np2");
  assert.equal((out.text.match(/```/g) || []).length, 2);
});

test("con el cursor dentro de la valla se mueve el bloque de código entero", () => {
  const text = "p1\n\n```\nuno\ndos\n```\n\np2";
  const blocks = blocksOf(text, [
    ["p1", "paragraph"],
    ["```\nuno\ndos\n```", "code"],
    ["p2", "paragraph"],
  ]);
  const inside = blocks[1].from + 8; // dentro del código, no en la valla
  const edit = moveBlockEdits({ text, blocks, index: 1, slot: 3, cursor: inside });
  const out = apply(text, edit.changes);
  assert.equal(out, "p1\n\np2\n\n```\nuno\ndos\n```");
  assert.equal((out.match(/```/g) || []).length, 2);
  // Y el cursor sigue dentro del bloque movido, al final del documento.
  assert.ok(edit.cursor > out.indexOf("```\nuno") && edit.cursor <= out.length);
});

// --------------------------------------------------------------------- listas

test("mover un ítem dentro de su lista no la parte: sigue siendo una sola lista", () => {
  const text = "- alfa\n- beta\n- gamma";
  const out = move(
    text,
    [
      ["- alfa", "bullet_list_item"],
      ["- beta", "bullet_list_item"],
      ["- gamma", "bullet_list_item"],
    ],
    2,
    0
  );
  assert.equal(out.text, "- gamma\n- alfa\n- beta");
  assert.ok(!out.text.includes("\n\n"), "la lista no puede partirse en dos");
});

test("una lista numerada reordenada se reenumera desde su número inicial", () => {
  const text = "1. alfa\n2. beta\n3. gamma";
  const out = move(
    text,
    [
      ["1. alfa", "ordered_list_item"],
      ["2. beta", "ordered_list_item"],
      ["3. gamma", "ordered_list_item"],
    ],
    2,
    0
  );
  assert.equal(out.text, "1. gamma\n2. alfa\n3. beta");
});

test("un ítem numerado que SALE de su lista baja uno a los que quedan y empieza en 1", () => {
  const text = "1. alfa\n2. beta\n3. gamma\n\nparrafo";
  const out = move(
    text,
    [
      ["1. alfa", "ordered_list_item"],
      ["2. beta", "ordered_list_item"],
      ["3. gamma", "ordered_list_item"],
      ["parrafo", "paragraph"],
    ],
    1,
    4
  );
  assert.equal(out.text, "1. alfa\n2. gamma\n\nparrafo\n\n1. beta");
});

test("una lista numerada que no se toca se queda con sus números", () => {
  const text = "3. tres\n7. siete\n\nparrafo";
  const out = move(
    text,
    [
      ["3. tres", "ordered_list_item"],
      ["7. siete", "ordered_list_item"],
      ["parrafo", "paragraph"],
    ],
    2,
    0
  );
  assert.equal(out.text, "parrafo\n\n3. tres\n7. siete");
});

test("un ítem de lista movido al lado de un párrafo no se le pega", () => {
  const text = "- alfa\n\nparrafo";
  const out = move(
    text,
    [
      ["- alfa", "bullet_list_item"],
      ["parrafo", "paragraph"],
    ],
    0,
    2
  );
  assert.equal(out.text, "parrafo\n\n- alfa");
});

// ------------------------------------------------------------- cosas que no se tocan

test("un hueco con un comentario HTML no se mueve: no se pierde texto", () => {
  const text = "alfa\n\n<!-- nota -->\n\nbeta\n\ngamma";
  const blocks = [
    { from: 0, to: 4, type: "paragraph" },
    { from: text.indexOf("beta"), to: text.indexOf("beta") + 4, type: "paragraph" },
    { from: text.indexOf("gamma"), to: text.indexOf("gamma") + 5, type: "paragraph" },
  ];
  assert.equal(moveBlockEdits({ text, blocks, index: 0, slot: 3 }), null);
});

test("el movimiento es UN solo cambio de texto (y por tanto una sola entrada de deshacer)", () => {
  const text = "alfa\n\nbeta\n\ngamma";
  const blocks = blocksOf(text, [
    ["alfa", "paragraph"],
    ["beta", "paragraph"],
    ["gamma", "paragraph"],
  ]);
  const edit = moveBlockEdits({ text, blocks, index: 2, slot: 0 });
  assert.equal(edit.changes.length, 1);
});
