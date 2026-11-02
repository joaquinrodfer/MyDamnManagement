// Teclas del editor de notas: ⏎ crea bloque, ⇧⏎/⌘⏎ saltan línea dentro del
// bloque, y cada tipo (lista, numerada, cita, título, código) se comporta.
//
// El fallo que fija: ⏎ era un salto de línea y Mayús+Enter creaba bloque (al
// revés de lo decidido); la cita no se cerraba nunca con ⏎ (D5), y salir de
// una lista de un solo ítem no funcionaba (D4), mientras que con dos sí.
//
// Corre sin navegador ni dependencias (node --test) sobre la lógica pura de
// block-keys.js: el bloque donde está el cursor se le pasa tal cual lo calcula
// el árbol de Markdown en entry.js. Lo que depende de CodeMirror (que el
// desplegable de "[[" gane el Enter, el orden de los keymaps) se comprueba en
// la app arrancada.

import test from "node:test";
import assert from "node:assert/strict";

import { blockKeyEdit } from "../block-keys.js";

/** Aplica la tecla al documento y devuelve el documento y el cursor nuevos. */
function apply(text, pos, block, kind = "enter") {
  const edit = blockKeyEdit({ text, pos, block, kind });
  assert.ok(edit, `la tecla tenía que estar atendida: ${JSON.stringify(text)} @${pos}`);
  return {
    text: text.slice(0, edit.from) + edit.insert + text.slice(edit.to),
    cursor: edit.cursor,
  };
}

const para = (from, to) => ({ from, to, type: "paragraph" });

// --------------------------------------------------------------- ⏎ = bloque

test("⏎ en un párrafo abre un bloque nuevo, separado por una línea en blanco", () => {
  const r = apply("uno", 3, para(0, 3));
  assert.equal(r.text, "uno\n\n");
  assert.equal(r.cursor, 5);
});

test("⏎ a mitad de palabra parte el bloque en dos", () => {
  const r = apply("abcdef", 3, para(0, 6));
  assert.equal(r.text, "abc\n\ndef");
  assert.equal(r.cursor, 5); // el cursor abre el bloque de abajo
});

test("⏎ al final de una fila de dentro del bloque no añade una línea de más", () => {
  // "uno\ndos" es UN párrafo con un salto blando: al partir, el salto que ya
  // había cuenta como uno de los dos separadores.
  const r = apply("uno\ndos", 3, para(0, 7));
  assert.equal(r.text, "uno\n\ndos");
  assert.equal(r.cursor, 5);
});

test("⏎ en una nota vacía no crea nada (que decida CodeMirror)", () => {
  assert.equal(blockKeyEdit({ text: "", pos: 0, block: null, kind: "enter" }), null);
});

// ------------------------------------------------- ⇧⏎ / ⌘⏎ = salto de línea

test("⇧⏎ mete un salto dentro del bloque, no un bloque nuevo", () => {
  const r = apply("uno", 3, para(0, 3), "soft");
  assert.equal(r.text, "uno\n");
  assert.equal(r.cursor, 4);
});

test("⌘⏎/⌃⏎ hace lo mismo que ⇧⏎", () => {
  const shift = apply("abcdef", 3, para(0, 6), "soft");
  const cmd = apply("abcdef", 3, para(0, 6), "soft");
  assert.equal(cmd.text, shift.text);
  assert.equal(cmd.text, "abc\ndef");
});

test("dos ⇧⏎ seguidos no dejan tres líneas en blanco", () => {
  const uno = apply("A", 1, para(0, 1), "soft");
  assert.equal(uno.text, "A\n");
  // El cursor queda en la fila vacía de detrás, que ya no pertenece al
  // párrafo: la tecla no se atiende y CodeMirror mete UN salto. Dos gestos
  // suman una línea en blanco, no tres.
  const dos = blockKeyEdit({ text: uno.text, pos: uno.cursor, block: para(0, 1), kind: "soft" });
  assert.equal(dos, null);
  const final = uno.text + "\n";
  assert.equal(final, "A\n\n");
  assert.ok(!final.includes("\n\n\n"));
});

// ------------------------------------------------------- listas de viñetas

test("⏎ en una viñeta crea otro punto", () => {
  const r = apply("- uno", 5, { from: 0, to: 5, type: "bullet_list_item" });
  assert.equal(r.text, "- uno\n- ");
  assert.equal(r.cursor, 8);
});

test("⏎ en el punto vacío sale de la lista: con UN ítem", () => {
  const r = apply("- uno\n- ", 8, { from: 6, to: 8, type: "bullet_list_item" });
  assert.equal(r.text, "- uno\n\n");
  assert.equal(r.cursor, 7);
  assert.ok(!r.text.slice(6).includes("- "), "no puede quedar un punto huérfano");
});

test("⏎ en el punto vacío sale de la lista: con DOS ítems, igual", () => {
  const text = "- uno\n- dos\n- ";
  const r = apply(text, text.length, { from: 12, to: 14, type: "bullet_list_item" });
  assert.equal(r.text, "- uno\n- dos\n\n");
  assert.equal(r.cursor, 13);
  assert.ok(!r.text.slice(12).includes("- "));
});

test("R1: salir del medio de una lista conserva el hueco y no pega el texto al ítem siguiente", () => {
  // "- uno / -  / - tres" con el cursor en el punto vacío del medio: se va el
  // marcador, pero el salto se queda como hueco del párrafo nuevo y el cursor
  // va detrás (fila en blanco), no delante del "- " de "tres".
  const text = "- uno\n- \n- tres";
  const r = apply(text, 8, { from: 6, to: 8, type: "bullet_list_item" });
  assert.equal(r.text, "- uno\n\n- tres");
  assert.equal(r.cursor, 6);
  // Lo que se teclea cae en la fila en blanco: no toca ninguna viñeta.
  const typed = r.text.slice(0, r.cursor) + "x" + r.text.slice(r.cursor);
  assert.equal(typed, "- uno\nx\n- tres");
  assert.ok(!typed.includes("x- "), "el texto no puede pegarse al marcador siguiente");
  assert.ok(typed.includes("\n- tres"), "el ítem de debajo conserva su viñeta");
});

test("R1 (numerada): salir del medio tampoco pega el texto al número siguiente", () => {
  const text = "1. uno\n2. \n3. tres";
  const r = apply(text, 9, { from: 7, to: 9, type: "ordered_list_item" });
  assert.equal(r.text, "1. uno\n\n3. tres");
  assert.equal(r.cursor, 7);
  const typed = r.text.slice(0, r.cursor) + "x" + r.text.slice(r.cursor);
  assert.equal(typed, "1. uno\nx\n3. tres");
  assert.ok(!typed.includes("x3."));
});

test("R2: salir de una lista que tiene un párrafo detrás no se lo traga", () => {
  // Salir con ⏎ del último ítem seguido de un párrafo: el párrafo tiene que
  // seguir siendo un bloque aparte, no continuación perezosa del ítem.
  const text = "- uno\n- \n\nparrafo\n\nsegundo";
  const r = apply(text, 8, { from: 6, to: 8, type: "bullet_list_item" });
  assert.equal(r.text, "- uno\n\n\nparrafo\n\nsegundo");
  assert.equal(r.cursor, 6); // fila en blanco justo debajo del ítem
  const typed = r.text.slice(0, r.cursor) + "x" + r.text.slice(r.cursor);
  assert.equal(typed, "- uno\nx\n\nparrafo\n\nsegundo");
  assert.ok(typed.includes("\n\nparrafo"), "el párrafo sigue separado por una línea en blanco");
});

// ------------------------------------------------------ listas numeradas

test("⏎ en una numerada pone el número siguiente", () => {
  const r = apply("1. uno", 6, { from: 0, to: 6, type: "ordered_list_item" });
  assert.equal(r.text, "1. uno\n2. ");
  assert.equal(r.cursor, 10);
});

test("⏎ en una numerada que va por 9 pone 10, no 91", () => {
  const r = apply("9. nueve", 8, { from: 0, to: 8, type: "ordered_list_item" });
  assert.equal(r.text, "9. nueve\n10. ");
});

test("⏎ en la numerada vacía sale de la lista, con uno o con dos ítems", () => {
  const uno = apply("1. uno\n2. ", 10, { from: 7, to: 10, type: "ordered_list_item" });
  assert.equal(uno.text, "1. uno\n\n");
  const dos = apply("1. uno\n2. dos\n3. ", 18, { from: 15, to: 18, type: "ordered_list_item" });
  assert.equal(dos.text, "1. uno\n2. dos\n\n");
});

// ------------------------------------------------------------------ citas

test("⏎ en una cita continúa la cita", () => {
  const r = apply("> cita", 6, { from: 0, to: 6, type: "quote" });
  assert.equal(r.text, "> cita\n> ");
  assert.equal(r.cursor, 9);
});

test("⏎ sobre una cita vacía cierra la cita (D5)", () => {
  const r = apply("> cita\n> ", 9, { from: 0, to: 9, type: "quote" });
  assert.equal(r.text, "> cita\n\n");
  assert.equal(r.cursor, 8);
  assert.ok(!r.text.slice(6).includes(">"), "no puede quedar un `> ` colgando");
});

test("partir una cita no deja doble espacio (D11)", () => {
  const r = apply("> cita larga", 7, { from: 0, to: 12, type: "quote" });
  assert.equal(r.text, "> cita\n> larga");
  assert.ok(!r.text.includes(">  "), "no puede quedar `>  larga` con dos espacios");
});

test("⇧⏎ dentro de una cita salta línea dentro de la misma cita", () => {
  const r = apply("> cita larga", 7, { from: 0, to: 12, type: "quote" }, "soft");
  assert.equal(r.text, "> cita\n> larga");
});

// ---------------------------------------------------------------- títulos

test("⏎ en un título termina el bloque y abre un párrafo normal", () => {
  const r = apply("# Titulo", 8, { from: 0, to: 8, type: "h1" });
  assert.equal(r.text, "# Titulo\n\n");
  assert.equal(r.cursor, 10);
});

test("⏎ a mitad de un título deja el resto como párrafo", () => {
  const r = apply("# Titulo largo", 8, { from: 0, to: 14, type: "h1" });
  assert.equal(r.text, "# Titulo\n\nlargo");
});

// ---------------------------------------------------------------- código

test("⏎ dentro de código mete un salto y no parte la valla", () => {
  const text = "```\nlinea1\nlinea2\n```";
  const r = apply(text, 10, { from: 0, to: text.length, type: "code" });
  assert.equal(r.text, "```\nlinea1\n\nlinea2\n```");
  assert.equal(r.text.match(/```/g).length, 2, "la valla tiene que seguir entera");
});

test("⇧⏎ dentro de código tampoco deja línea en blanco: es un solo salto (D7)", () => {
  const text = "```\nlinea1\nlinea2\n```";
  const soft = blockKeyEdit({ text, pos: 10, block: { from: 0, to: text.length, type: "code" }, kind: "soft" });
  const enter = blockKeyEdit({ text, pos: 10, block: { from: 0, to: text.length, type: "code" }, kind: "enter" });
  assert.deepEqual(soft, enter);
  assert.equal(soft.insert, "\n");
});
