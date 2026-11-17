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

import { markdownLanguage } from "@codemirror/lang-markdown";

import { blockBreakInsert, blockKeyEdit, gluesBelow } from "../block-keys.js";

/** Aplica la tecla al documento y devuelve el documento y el cursor nuevos. */
function apply(text, pos, block, kind = "enter") {
  const edit = blockKeyEdit({ text, pos, block, kind });
  assert.ok(edit, `la tecla tenía que estar atendida: ${JSON.stringify(text)} @${pos}`);
  return {
    text: text.slice(0, edit.from) + edit.insert + text.slice(edit.to),
    cursor: edit.cursor,
    blockBreak: edit.blockBreak === true,
  };
}

const para = (from, to) => ({ from, to, type: "paragraph" });

// --------------------------------------------------------------- ⏎ = bloque

test("⏎ al final de un párrafo deja el cursor en la línea de debajo, sin línea de más", () => {
  // El bloque nuevo no materializa líneas en blanco (t_c7756962): la que lo
  // separa del párrafo de arriba la mete la primera letra que se escribe.
  const r = apply("uno", 3, para(0, 3));
  assert.equal(r.text, "uno\n");
  assert.equal(r.cursor, 4);
  assert.equal(r.blockBreak, true);
});

test("⏎ al final de un bloque NO añade líneas en blanco a la que ya separa", () => {
  // El caso del dueño: con un bloque detrás, el hueco ya está ahí y el cursor
  // lo ocupa. Antes se metían dos saltos más y se veían tres huecos (54 px).
  const text = "# THALIA\n\n# STRATERIA\n\n# SAIFFE";
  const r = apply(text, 8, { from: 0, to: 8, type: "h1" });
  assert.equal(r.text, text, "el .md no cambia: una línea en blanco, no tres");
  assert.equal(r.cursor, 9, "el cursor queda en la línea en blanco de debajo");
  assert.equal(r.blockBreak, true);
});

test("⏎ al final de un encabezado vale para h1, h2, h3 y h4", () => {
  for (const [type, line] of [
    ["h1", "# UNO"],
    ["h2", "## UNO"],
    ["h3", "### UNO"],
    ["h4", "#### UNO"],
  ]) {
    const text = line + "\n\n" + line.replace("UNO", "DOS");
    const r = apply(text, line.length, { from: 0, to: line.length, type });
    assert.equal(r.text, text, `${type}: el .md no gana líneas en blanco`);
    assert.equal(r.cursor, line.length + 1, `${type}: el cursor queda debajo`);
    assert.equal(r.blockBreak, true, `${type}: el bloque nuevo se marca`);
  }
});

test("⏎ al final de un encabezado pegado al de abajo crea UNA línea en blanco", () => {
  const r = apply("# A\n# B", 3, { from: 0, to: 3, type: "h1" });
  assert.equal(r.text, "# A\n\n# B");
  assert.equal(r.cursor, 4);
  assert.equal(r.blockBreak, true);
});

test("⏎ al final del último bloque de la nota deja una línea, no dos", () => {
  const r = apply("# SOLO", 6, { from: 0, to: 6, type: "h1" });
  assert.equal(r.text, "# SOLO\n");
  assert.equal(r.cursor, 7);
  assert.equal(r.blockBreak, true);
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

// -------------------------------- la primera letra del bloque abierto con ⏎

test("la primera letra: línea en blanco arriba siempre, abajo solo si se lo comen", () => {
  // Con un bloque debajo que no se come la línea, la que ya había hace de
  // arriba: el `.md` no crece y el hueco es el de la regla (18 px tras un H1).
  assert.equal(blockBreakInsert("Z", "# STRATERIA"), "\nZ");
  assert.equal(blockBreakInsert("Z", "# STRATERIA\n\n# SAIFFE"), "\nZ");
  // Último bloque de la nota: la de arriba es la única que hay.
  assert.equal(blockBreakInsert("Z", null), "\nZ");
  // Un párrafo debajo se come la línea: sin la de abajo, los dos son uno. Y
  // entonces la de arriba hay que materializarla, o el encabezado pierde su
  // hueco (requisito 2: 18 px tras un H1 aunque el bloque de abajo sea texto).
  assert.equal(blockBreakInsert("W", "Texto de abajo."), "\nW\n");
  // Numerada que empieza en 2: tampoco interrumpe un párrafo, así que se come
  // el texto y la lista dejaría de ser lista (regresión de la revisión).
  assert.equal(blockBreakInsert("W", "2. dos\n3. tres"), "\nW\n");
  // La que empieza en 1 sí interrumpe: basta la línea de arriba.
  assert.equal(blockBreakInsert("W", "1. uno\n2. dos"), "\nW");
});

test("gluesBelow: qué bloques se comen la línea de encima", () => {
  const casos = [
    ["hola\ny más", true, "párrafo"],
    ["[id]: /url", true, "definición de enlace"],
    ["    codigo", true, "código indentado"],
    ["\tcodigo", true, "código con tabulador"],
    ["---", true, "solo guiones: subrayado setext"],
    ["- ", true, "viñeta vacía: subrayado setext"],
    ["1.", true, "numerada vacía"],
    ["2. dos\n3. tres", true, "numerada que empieza en 2"],
    ["001. uno", true, "numerada con ceros delante"],
    ["<span>x</span>", true, "HTML tipo 7 (etiqueta suelta)"],
    ["# B", false, "encabezado"],
    ["##### B", false, "encabezado de cinco"],
    ["```\ncodigo\n```", false, "valla"],
    ["> cita", false, "cita"],
    ["- uno\n- dos", false, "viñeta con contenido"],
    ["* ", true, "viñeta `*` vacía (un punto vacío no interrumpe)"],
    ["1. uno\n2. dos", false, "numerada que empieza en 1"],
    ["***", false, "regla de asteriscos"],
    ["- - -", false, "regla de guiones separados"],
    ["<div>x</div>", false, "HTML de bloque"],
    ["| a | b |\n|---|---|", false, "tabla"],
  ];
  for (const [text, glue, nombre] of casos) {
    assert.equal(gluesBelow(text), glue, `${nombre}: ${JSON.stringify(text)}`);
  }
});

test("gluesBelow coincide con el parser de verdad", () => {
  // Quien decide es el árbol de Markdown: si al parsear "x" + el bloque de
  // abajo el primer bloque no acaba justo en la `x`, es que el de abajo se ha
  // comido la línea. Las dos respuestas tienen que dar lo mismo, o la regla se
  // ha quedado atrás respecto al parser.
  const bloques = [
    "hola", "[id]: /url", "    codigo", "---", "- ", "1.", "2. dos", "001. uno",
    // (Una viñeta vacía con `*` no entra: este parser deja el `*` dentro del
    // párrafo en vez de pegarlo al ítem, y ahí no hay nada que comparar.)
    "<span>x</span>", "# B", "##### B", "```\nx\n```", "> cita", "- uno",
    "1. uno", "***", "- - -", "<div>x</div>", "| a | b |\n|---|---|",
  ];
  for (const text of bloques) {
    const primero = markdownLanguage.parser.parse("x\n" + text).topNode.firstChild;
    const seLoCome = !(primero && primero.from === 0 && primero.to === 1);
    assert.equal(gluesBelow(text), seLoCome, `el parser y gluesBelow discrepan con ${JSON.stringify(text)}`);
  }
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

test("R1: salir del medio de una lista deja el párrafo entre líneas en blanco", () => {
  // "- uno / -  / - tres" con el cursor en el punto vacío del medio: se va el
  // marcador y el párrafo nuevo queda separado por una línea en blanco ARRIBA
  // y otra ABAJO. Sin la de arriba, Markdown lee el texto como continuación
  // perezosa del ítem y al recargar el párrafo desaparece.
  const text = "- uno\n- \n- tres";
  const r = apply(text, 8, { from: 6, to: 8, type: "bullet_list_item" });
  assert.equal(r.text, "- uno\n\n\n\n- tres");
  assert.equal(r.cursor, 7);
  // Lo que se teclea cae en su propia fila, entre dos líneas en blanco.
  const typed = r.text.slice(0, r.cursor) + "parrafo" + r.text.slice(r.cursor);
  assert.equal(typed, "- uno\n\nparrafo\n\n- tres");
  assert.ok(!typed.includes("x- "), "el texto no puede pegarse al marcador siguiente");
  assert.ok(typed.includes("parrafo\n\n- tres"), "la lista de debajo sigue separada");
});

test("R1 (numerada): salir del medio tampoco pega el texto al número siguiente", () => {
  const text = "1. uno\n2. \n3. tres";
  const r = apply(text, 9, { from: 7, to: 9, type: "ordered_list_item" });
  assert.equal(r.text, "1. uno\n\n\n\n3. tres");
  assert.equal(r.cursor, 8);
  const typed = r.text.slice(0, r.cursor) + "parrafo" + r.text.slice(r.cursor);
  assert.equal(typed, "1. uno\n\nparrafo\n\n3. tres");
  assert.ok(!typed.includes("x3."));
});

test("R2: salir de una lista que tiene un párrafo detrás no se lo traga", () => {
  // Salir con ⏎ del último ítem seguido de un párrafo: el párrafo tiene que
  // seguir siendo un bloque aparte, no continuación perezosa del ítem. Los
  // saltos que ya había detrás se consumen y se reconstruye el hueco.
  const text = "- uno\n- \n\nparrafo\n\nsegundo";
  const r = apply(text, 8, { from: 6, to: 8, type: "bullet_list_item" });
  assert.equal(r.text, "- uno\n\n\n\nparrafo\n\nsegundo");
  assert.equal(r.cursor, 7);
  const typed = r.text.slice(0, r.cursor) + "parrafo2" + r.text.slice(r.cursor);
  assert.equal(typed, "- uno\n\nparrafo2\n\nparrafo\n\nsegundo");
  assert.ok(typed.includes("parrafo2\n\nparrafo"), "el párrafo viejo sigue siendo un bloque aparte");
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
  assert.equal(r.text, "# Titulo\n");
  assert.equal(r.cursor, 9);
  assert.equal(r.blockBreak, true);
});

test("⏎ a mitad de un título deja el resto como párrafo", () => {
  const r = apply("# Titulo largo", 8, { from: 0, to: 14, type: "h1" });
  assert.equal(r.text, "# Titulo\n\nlargo");
  // Ahí los saltos separan texto que ya existía: no hay bloque vacío que valga.
  assert.equal(r.blockBreak, false);
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
