// Mover una base incrustada (```mdm-database) como cualquier otro bloque.
//
// La base es una VALLA: para el árbol de Markdown es un `FencedCode`, así que
// `blocksFromTree` la cuenta como un bloque de tipo «code» y el motor de mover
// la trata como a cualquier valla -- entera, de la apertura al cierre. Lo que
// fallaba no era esto sino el asa (ver `blockHandleDom` en entry.js: el widget
// que pinta la tabla tapaba la primera línea, y con ella el asa). Estas pruebas
// fijan lo que tiene que seguir siendo verdad por el lado del motor, incluido
// el día que el widget cambie de forma (t_45697398):
//
//   1. la valla es UN bloque, y su texto incluye el `id` y el `view`;
//   2. moverla no cambia el multiconjunto de líneas del `.md` (el invariante de
//      block-move-property.test.js, aquí con la valla de base dentro);
//   3. el bloque que se mueve es el MISMO texto -- con su id, su view y su
//      title --, y `scanDatabaseBlocks` lo sigue leyendo igual después;
//   4. se puede reordenar con los párrafos de alrededor y con otra base, sin
//      fusionarse con ninguna de las dos;
//   5. Alt+↑/↓ con el cursor DENTRO de la valla la mueve en UNA transacción.

import test from "node:test";
import assert from "node:assert/strict";
import { markdownLanguage } from "@codemirror/lang-markdown";

import { blocksFromTree } from "../block-ranges.js";
import { moveBlockEdits } from "../block-move.js";
import { scanDatabaseBlocks } from "../database-embed.js";
import { runMoveBlockKey } from "../entry.js";

const ID_A = "11111111-1111-4111-8111-111111111111";
const ID_B = "22222222-2222-4222-8222-222222222222";

/** La valla tal cual se escribe en el `.md` (database-block.js). */
function valla(id, title, view = "Tabla") {
  return ["```mdm-database", `id: ${id}`, `view: ${view}`, `title: ${title}`, "```"].join("\n");
}

const BASE_A = valla(ID_A, "Contactos");
const BASE_B = valla(ID_B, "Tareas", "Tablero");

const NOTA = ["parrafo uno", "", BASE_A, "", "parrafo dos"].join("\n");

function blocksOf(text) {
  return blocksFromTree(markdownLanguage.parser.parse(text).topNode, text);
}

/** El `.md` sin importar el orden de las líneas (mismo multiconjunto). */
const sortedLines = (s) => s.split("\n").sort();

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

/** Lo que lee la app de las vallas de un documento: `[{id, view}]` en orden. */
function basesOf(text) {
  return scanDatabaseBlocks(text).map((b) => ({
    id: b.parse.id,
    view: b.parse.view,
    ok: b.parse.ok,
  }));
}

test("la valla de base es UN bloque de tipo «code», del ``` de apertura al de cierre", () => {
  const blocks = blocksOf(NOTA);
  assert.equal(blocks.length, 3, "parrafo + base + parrafo");
  assert.equal(blocks[1].type, "code");
  assert.equal(NOTA.slice(blocks[1].from, blocks[1].to), BASE_A, "la valla entera, con su id y su view");
  // Y el lector de la app la ve igual: un bloque de base válido, con su id.
  assert.deepEqual(basesOf(NOTA), [{ id: ID_A, view: "Tabla", ok: true }]);
});

test("mover la base arriba y abajo la lleva entera y no cambia las líneas del .md", () => {
  const blocks = blocksOf(NOTA);
  const casos = [
    { slot: 0, porque: "arriba del todo" },
    { slot: 3, porque: "abajo del todo" },
  ];
  for (const { slot, porque } of casos) {
    const edit = moveBlockEdits({ text: NOTA, blocks, index: 1, slot });
    assert.ok(edit, `el movimiento tiene que caber (${porque})`);
    assert.equal(edit.changes.length, 1, "un solo cambio de texto");
    const out = apply(NOTA, edit.changes);
    assert.deepEqual(sortedLines(out), sortedLines(NOTA), `las líneas cambian (${porque}): «${out}»`);
    assert.deepEqual(basesOf(out), [{ id: ID_A, view: "Tabla", ok: true }], `la base se pierde (${porque})`);
    // Mismos tres bloques, y el de la base es el mismo texto: ni una línea de
    // la valla se queda atrás.
    const after = blocksOf(out);
    assert.equal(after.length, 3, `el número de bloques cambia (${porque})`);
    const texts = after.map((b) => out.slice(b.from, b.to));
    assert.ok(texts.includes(BASE_A), `la valla ya no está entera (${porque}): ${JSON.stringify(texts)}`);
  }
});

test("dos bases se reordenan entre sí y con los párrafos de alrededor", () => {
  const doc = ["Empieza.", "", BASE_A, "", "En medio.", "", BASE_B, "", "Final."].join("\n");
  const blocks = blocksOf(doc);
  assert.equal(blocks.length, 5, "parrafo + base + parrafo + base + parrafo");
  // La segunda base arriba del todo, y luego la primera al final: el orden de
  // las dos cambia y los párrafos se quedan donde estaban entre ellas.
  const arriba = moveBlockEdits({ text: doc, blocks, index: 3, slot: 0 });
  assert.ok(arriba, "la segunda base sube");
  const paso1 = apply(doc, arriba.changes);
  assert.deepEqual(sortedLines(paso1), sortedLines(doc), "las líneas cambian al subir la segunda base");
  assert.deepEqual(
    basesOf(paso1).map((b) => b.id),
    [ID_B, ID_A],
    `el orden de las bases no es el pedido: «${paso1}»`
  );
  const b1 = blocksOf(paso1);
  const abajo = moveBlockEdits({ text: paso1, blocks: b1, index: 2, slot: 5 });
  assert.ok(abajo, "la primera base baja");
  const paso2 = apply(paso1, abajo.changes);
  assert.deepEqual(sortedLines(paso2), sortedLines(doc), "las líneas cambian al bajar la primera base");
  assert.deepEqual(
    basesOf(paso2).map((b) => b.id),
    [ID_B, ID_A],
    `el orden de las bases no es el pedido: «${paso2}»`
  );
  // Y la nota sigue teniendo cinco bloques: ninguna base se ha comido a su
  // párrafo vecino, ni al revés.
  assert.equal(blocksOf(paso2).length, 5, `se fusionan bloques: «${paso2}»`);
});

test("una base junto a otra base son dos bloques: no se fusionan al quedar vecinas", () => {
  // Las dos vallas seguidas, con una sola línea en blanco en medio: Markdown
  // cierra la primera en su ``` y abre la segunda en el suyo.
  const doc = [BASE_A, "", BASE_B].join("\n");
  assert.equal(blocksOf(doc).length, 2);
  assert.deepEqual(
    basesOf(doc).map((b) => b.id),
    [ID_A, ID_B]
  );
  // Y el motor acepta dejarlas vecinas (no hay «aire» que perder: mover no
  // cambia el número de líneas).
  const conParrafo = ["parrafo uno", "", BASE_A, "", BASE_B].join("\n");
  const blocks = blocksOf(conParrafo);
  assert.equal(blocks.length, 3);
  const edit = moveBlockEdits({ text: conParrafo, blocks, index: 2, slot: 0 });
  assert.ok(edit, "la segunda base sube por delante del párrafo y de la primera");
  const out = apply(conParrafo, edit.changes);
  assert.deepEqual(sortedLines(out), sortedLines(conParrafo));
  assert.equal(blocksOf(out).length, 3, `se fusionan bloques: «${out}»`);
  assert.deepEqual(
    basesOf(out).map((b) => b.id),
    [ID_B, ID_A]
  );
});

test("Alt+↓ con el cursor DENTRO de la valla mueve la base entera en UNA transacción", () => {
  const blocks = blocksOf(NOTA);
  const dentro = blocks[1].from + 3; // dentro de la línea de apertura
  const view = {
    state: {
      selection: { main: { empty: true, from: dentro, to: dentro, head: dentro } },
      doc: { toString: () => NOTA, length: NOTA.length },
      field: () => ({ blocks, selected: new Set() }),
    },
    dispatches: [],
    dispatch(tr) {
      this.dispatches.push(tr);
    },
    focus() {},
  };
  assert.equal(runMoveBlockKey(view, 1), true, "la tecla es del editor");
  assert.equal(view.dispatches.length, 1, "un solo ⌘Z deshace el movimiento entero");
  const tr = view.dispatches[0];
  assert.equal(tr.changes.length, 1);
  assert.equal(tr.userEvent, "move.block");
  const out = apply(NOTA, tr.changes);
  assert.deepEqual(sortedLines(out), sortedLines(NOTA), `las líneas cambian: «${out}»`);
  assert.deepEqual(basesOf(out), [{ id: ID_A, view: "Tabla", ok: true }]);
  // El cursor se queda DENTRO de la base movida: dos Alt+↓ seguidos mueven el
  // mismo bloque, no el de al lado.
  const after = blocksOf(out);
  const movida = after.find((b) => out.slice(b.from, b.to) === BASE_A);
  assert.ok(tr.selection.anchor >= movida.from && tr.selection.anchor <= movida.to, "el cursor se sale del bloque");
});
