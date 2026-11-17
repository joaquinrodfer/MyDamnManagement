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

import { EditorSelection, EditorState } from "@codemirror/state";
import { history, redo, undo } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";

import { addBlockBreak, blockBreakField, blockBreakTestExtensions } from "../entry.js";
import { blockKeyEdit } from "../block-keys.js";
import { formatEdit } from "../format.js";
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

/** Mueve la selección sin tocar el documento: ↓ y ↑, o un clic. */
function moveTo(state, pos) {
  return state.update({ selection: { anchor: pos } }).state;
}

test("salir de la línea y volver no pierde la línea en blanco de la primera letra", () => {
  // ⏎, ↓ y ↑ (o un clic) y después la primera letra: el cursor sale de la línea
  // del bloque nuevo y vuelve al mismo sitio. La marca no puede caerse por un
  // movimiento de cursor -- si se cae, `W` se pega al párrafo de abajo, los dos
  // bloques se funden en uno y el encabezado pierde su hueco (18 px → 0).
  let state = start("# A\n\nTexto");
  state = enter(state, 0, 3, "h1");
  assert.equal(state.doc.toString(), "# A\n\nTexto", "⏎ no toca el .md");
  const gap = state.selection.main.head;
  state = moveTo(state, state.doc.line(3).from); // ↓
  state = moveTo(state, gap); //                    ↑
  state = insert(state, "W", "input.type");
  assert.equal(state.doc.toString(), "# A\n\nW\n\nTexto");
  assert.equal(blocks(state.doc.toString()).length, 3, "el texto nuevo y el de abajo son bloques distintos");
});

// --- Ronda 5: la materialización depende de DÓNDE cae el contenido, no de cómo
// llega. Ctrl+B, deshacer + escribir, o escribir en otro sitio y volver: el
// primer contenido del bloque nuevo cae en su línea en blanco y tiene que
// llevarse las suyas, venga por el `userEvent` que venga.

/** El estado con historial, como en la app (⌘Z / ⌘⇧Z). */
const startWithHistory = (doc) =>
  EditorState.create({ doc, extensions: [history(), ...blockBreakTestExtensions] });

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

test("⏎ → ⌘B → letra: la negrita es el primer contenido y se lleva sus líneas", () => {
  let state = startWithHistory("# A\n\nTexto");
  state = enter(state, 0, 3, "h1");
  state = bold(state);
  assert.equal(state.doc.toString(), "# A\n\n****\n\nTexto", "las marcas ya son contenido");
  state = insert(state, "w", "input.type");
  assert.equal(state.doc.toString(), "# A\n\n**w**\n\nTexto");
  assert.equal(blocks(state.doc.toString()).length, 3, "el párrafo nuevo no se funde con el de abajo");
});

test("⏎ → letra → deshacer → letra: deshacer devuelve la marca", () => {
  let state = startWithHistory("# A\n\nTexto");
  state = enter(state, 0, 3, "h1");
  state = insert(state, "w", "input.type");
  assert.equal(state.doc.toString(), "# A\n\nw\n\nTexto");
  state = run(state, undo);
  assert.equal(state.doc.toString(), "# A\n\nTexto", "vuelve al estado del ⏎");
  state = insert(state, "w", "input.type");
  assert.equal(state.doc.toString(), "# A\n\nw\n\nTexto");
  assert.equal(blocks(state.doc.toString()).length, 3);
});

test("⏎ → letra → deshacer → pegar: igual que teclear", () => {
  let state = startWithHistory("# A\n\nTexto");
  state = enter(state, 0, 3, "h1");
  state = insert(state, "w", "input.type");
  state = run(state, undo);
  state = insert(state, "hola", "input.paste");
  assert.equal(state.doc.toString(), "# A\n\nhola\n\nTexto");
  assert.equal(blocks(state.doc.toString()).length, 3);
});

test("deshacer y rehacer no duplican las líneas en blanco", () => {
  let state = startWithHistory("# A\n\nTexto");
  state = enter(state, 0, 3, "h1");
  state = insert(state, "w", "input.type");
  state = run(state, undo);
  state = run(state, redo);
  assert.equal(state.doc.toString(), "# A\n\nw\n\nTexto", "rehacer deja lo que había, nada más");
  assert.equal(state.field(blockBreakField), null, "y la marca no vuelve: ya se usó");
  state = moveTo(state, 6); // detrás de la `w` (rehacer deja el cursor al final de lo insertado)
  state = insert(state, "x", "input.type");
  assert.equal(state.doc.toString(), "# A\n\nwx\n\nTexto", "y lo siguiente se escribe en su sitio");
});

test("escribir en otro sitio, deshacer y volver al hueco: la marca sigue", () => {
  let state = startWithHistory("# A\n\nTexto");
  state = enter(state, 0, 3, "h1");
  const gap = state.selection.main.head;
  state = moveTo(state, state.doc.length);
  state = insert(state, "!", "input.type"); // en otro sitio: la marca se mueve, no se pierde
  state = run(state, undo);
  state = moveTo(state, gap);
  state = insert(state, "W", "input.type");
  assert.equal(state.doc.toString(), "# A\n\nW\n\nTexto");
  assert.equal(blocks(state.doc.toString()).length, 3);
});

test("escribir encima del hueco desplaza la marca con el texto", () => {
  // Una letra en el encabezado (antes del hueco) mueve la línea en blanco una
  // posición: la marca tiene que seguirla.
  let state = startWithHistory("# A\n\nTexto");
  state = enter(state, 0, 3, "h1");
  const gap = state.selection.main.head;
  state = moveTo(state, 3);
  state = insert(state, "B", "input.type");
  state = moveTo(state, gap + 1);
  state = insert(state, "W", "input.type");
  assert.equal(state.doc.toString(), "# AB\n\nW\n\nTexto");
});

test("un contenido con saltos suelta la marca y deshacerlo la devuelve", () => {
  // El pegado multilínea va aparte (t_608d3c57): aquí solo se fija que no deja
  // una marca colgando y que ⌘Z vuelve al estado del ⏎ con la marca viva.
  let state = startWithHistory("# A\n\nTexto");
  state = enter(state, 0, 3, "h1");
  state = insert(state, "a\nb", "input.paste");
  assert.equal(state.doc.toString(), "# A\na\nb\nTexto");
  state = insert(state, "c", "input.type");
  assert.equal(state.doc.toString(), "# A\na\nbc\nTexto", "sin marca: se escribe sin más");
  state = run(state, undo);
  state = run(state, undo);
  assert.equal(state.doc.toString(), "# A\n\nTexto");
  state = insert(state, "w", "input.type");
  assert.equal(state.doc.toString(), "# A\n\nw\n\nTexto");
});

test("salir de la línea y volver con un encabezado debajo conserva su hueco", () => {
  let state = start("# A\n\n# B");
  state = enter(state, 0, 3, "h1");
  const gap = state.selection.main.head;
  state = moveTo(state, state.doc.line(3).from);
  state = moveTo(state, gap);
  state = insert(state, "W", "input.type");
  assert.equal(state.doc.toString(), "# A\n\nW\n# B", "una línea en blanco detrás del H1");
  assert.equal(blocks(state.doc.toString()).length, 3);
});
