// Pintado de flechas tipográficas y de `<u>x</u>`, sobre un EditorState de
// verdad (con el parser de Markdown, que es de donde salen las zonas de
// código). Las dos cosas comparten la misma definición de "esto es código"
// (`codeRangesOf`), así que se prueban juntas: si una se olvida de
// `InlineCode` y la otra no, se cae aquí.
//
// Las funciones puras reciben el estado y los rangos visibles (en la app, la
// vista les pasa `state` y `visibleRanges`/`viewport`); con el documento
// entero como rango visible basta para fijar el comportamiento.

import test from "node:test";
import assert from "node:assert/strict";

import { EditorSelection, EditorState } from "@codemirror/state";
import { markdown } from "@codemirror/lang-markdown";
import { ensureSyntaxTree, syntaxTree, syntaxTreeAvailable } from "@codemirror/language";

import { buildArrowDecorations, underlineDecorations } from "../entry.js";

function stateOf(doc, cursor = doc.length) {
  return EditorState.create({
    doc,
    selection: EditorSelection.range(cursor),
    extensions: [markdown({ addKeymap: false })],
  });
}

function visibleRanges(state) {
  return [{ from: 0, to: state.doc.length }];
}

/** Las decoraciones como datos planos: posición, clase de las de `mark` y
 * glifo de las de reemplazo. */
function decorationsOf(set, length) {
  const out = [];
  set.between(0, length, (from, to, value) => {
    out.push({
      from,
      to,
      class: value.spec && value.spec.class,
      glyph: value.widget && value.widget.glyph,
    });
  });
  return out;
}

function arrowsOf(doc, cursor) {
  const state = stateOf(doc, cursor);
  return decorationsOf(buildArrowDecorations(state, visibleRanges(state)), state.doc.length);
}

function underlinesOf(doc, cursor) {
  const state = stateOf(doc, cursor);
  return decorationsOf(underlineDecorations(state, visibleRanges(state)), state.doc.length);
}

// ------------------------------------------------------------ flechas `->`

test("`a -> b` dentro de un código en línea se queda literal: ni una flecha", () => {
  const doc = "Muestra: `a -> b` literal";
  assert.deepEqual(arrowsOf(doc), []);
});

test("las tres flechas (`->`, `<-`, `<->`) se respetan dentro del código en línea", () => {
  assert.deepEqual(arrowsOf("`a -> b`"), []);
  assert.deepEqual(arrowsOf("`a <- b`"), []);
  assert.deepEqual(arrowsOf("`a <-> b`"), []);
});

test("fuera de código la flecha sí se pinta", () => {
  assert.deepEqual(arrowsOf("a -> b"), [{ from: 2, to: 4, class: undefined, glyph: "→" }]);
});

test("en la misma línea: la de dentro del código no, la de fuera sí", () => {
  const doc = "`a -> b` luego a -> b";
  const arrows = arrowsOf(doc);
  assert.equal(arrows.length, 1);
  assert.equal(arrows[0].from, doc.lastIndexOf("->"));
  assert.equal(arrows[0].glyph, "→");
});

test("en una valla y en código sangrado tampoco (lo que ya funcionaba)", () => {
  assert.deepEqual(arrowsOf("```\nptr->campo\n```"), []);
  assert.deepEqual(arrowsOf("    ptr->campo\n"), []);
});

// -------------------------------------------------------- subrayado `<u>`

test("`<u>x</u>` dentro de un código en línea no se pinta: etiquetas a la vista y nada subrayado", () => {
  const doc = "Muestra: `<u>x</u>` literal";
  assert.deepEqual(underlinesOf(doc), []);
});

test("con el cursor dentro del bloque, tampoco (las etiquetas siguen sin ocultarse)", () => {
  const doc = "Muestra: `<u>x</u>` literal";
  assert.deepEqual(underlinesOf(doc, 12), []);
});

test("fuera de código sí: se esconden las etiquetas y se subraya el texto", () => {
  const doc = "Muestra: <u>x</u> literal";
  const decs = underlinesOf(doc);
  assert.equal(decs.length, 3);
  assert.deepEqual(
    decs.map((d) => [d.from, d.to, d.class || "replace"]),
    [
      [9, 12, "replace"],
      [12, 13, "cm-mdm-underline"],
      [13, 17, "replace"],
    ]
  );
});

test("con el cursor dentro, las etiquetas se ven tenues en vez de esconderse", () => {
  const doc = "Muestra: <u>x</u> literal";
  const decs = underlinesOf(doc, 12);
  assert.deepEqual(
    decs.map((d) => d.class || "replace"),
    ["cm-mdm-mark", "cm-mdm-underline", "cm-mdm-mark"]
  );
});

test("en una valla y en código sangrado tampoco se pinta (lo que ya funcionaba)", () => {
  assert.deepEqual(underlinesOf("```\n<u>x</u>\n```"), []);
  assert.deepEqual(underlinesOf("    <u>x</u>\n"), []);
});

test("mezcla: solo se pinta el `<u>` que está fuera del código en línea", () => {
  const doc = "`<u>no</u>` y <u>si</u>";
  const decs = underlinesOf(doc);
  assert.equal(decs.length, 3);
  assert.equal(decs[0].from, 14); // el `<u>` de después del código
  assert.equal(decs[1].class, "cm-mdm-underline");
  assert.equal(doc.slice(decs[1].from, decs[1].to), "si");
});

// ------------------------------------- pegado al cierre del código en línea
// Los rangos del árbol son semiabiertos (`to` excluido): el `to` de un
// `InlineCode` es la posición justo detrás del backtick de cierre, donde puede
// empezar otra construcción. Si `isInsideCode` metiera esa posición en el
// código, lo que va pegado al cierre se quedaría sin pintar.

test("pegada al cierre: la flecha que empieza justo detrás del backtick sí se pinta", () => {
  const doc = "`x`->y";
  assert.deepEqual(arrowsOf(doc), [{ from: 3, to: 5, class: undefined, glyph: "→" }]);
});

test("pegado al cierre: el `<u>` que empieza justo detrás del backtick sí se pinta", () => {
  const doc = "Pegado: `x`<u>y</u> fin";
  const decs = underlinesOf(doc);
  assert.deepEqual(
    decs.map((d) => [d.from, d.to, d.class || "replace"]),
    [
      [11, 14, "replace"],
      [14, 15, "cm-mdm-underline"],
      [15, 19, "replace"],
    ]
  );
});

// ---------------------------------------- un par que cruza el borde del código
// `isInsideCode` mira las dos etiquetas, no solo la apertura: si `</u>` cae
// dentro del código en línea, es texto literal y no se puede ocultar (ni
// subrayar el trozo que queda entre las dos). Antes solo se consultaba la
// apertura, así que un par como `<u>x` + backtick + `y</u> z` + backtick
// escondía el `</u>` de dentro y el subrayado se metía en los backticks.

test("si el `</u>` cae dentro del código en línea, el par no se pinta", () => {
  const doc = "B3 abre fuera/cierra dentro: <u>x `y</u> z` fin.";
  assert.deepEqual(underlinesOf(doc), []);
});

test("el cruce al revés (abre dentro, cierra fuera) tampoco se pinta (lo que ya pasaba)", () => {
  assert.deepEqual(underlinesOf("`<u>x` y</u> fin"), []);
});

test("el par que cruza el borde no se pinta, pero el `<u>` normal de después sí", () => {
  const doc = "<u>x `y</u> z` fin y <u>si</u>";
  const decs = underlinesOf(doc);
  assert.equal(decs.length, 3);
  assert.equal(decs[0].from, doc.indexOf("<u>si</u>"));
  assert.equal(doc.slice(decs[1].from, decs[1].to), "si");
  assert.equal(decs[1].class, "cm-mdm-underline");
});

// ------------------------------------------ notas grandes: árbol parcial
// En una nota larga el árbol de sintaxis es PARCIAL: el parser de CodeMirror
// trabaja con presupuesto de tiempo y deja sin parsear lo que no ha llegado a
// tocar (al abrir, solo los primeros ~3 KB). Una zona de código que el árbol
// todavía no conoce no existe, así que el final de la nota se pintaba con la
// flecha y el `<u>` DENTRO del `código` en línea -- el fallo original, solo en
// documentos grandes. Ahora `codeRangesOf` fuerza el parseo hasta el final del
// tramo visible antes de leer el árbol.
//
// Estos tests NO dependen de cuánto tarde el parser: antes de medir se fuerza
// el parseo ENTERO del documento (`parseWholeNote`, que insiste hasta que el
// parser dice que ha llegado al final, sin reloj de por medio) y solo entonces
// se piden las decoraciones. Lo que se fija es el comportamiento -- que el
// `código` en línea del final se respeta cuando el parser ya conoce el
// documento --, no que quepa en el presupuesto de 100 ms de `codeRangesOf` en
// una máquina cargada. Lo que impide que el test se vuelva decorativo es que
// `syntaxTree(state)` sigue devolviendo la instantánea PARCIAL del arranque (lo
// comprueba el primer test): si `codeRangesOf` volviera a `syntaxTree(state)` a
// secas, seguiría leyendo ese árbol corto y los tests caerían igual.

const FILLER = "Parrafo de relleno con texto normal y alguna cosa mas para alargar la nota.";
const FINAL_LINE = "Final: fuera `a -> b` y `<u>x</u>` en codigo, fuera <u>si</u> y a -> b.";

function bigDoc() {
  const paragraphs = Array.from({ length: 3000 }, (_, i) => `${FILLER} (${i})`);
  return paragraphs.join("\n\n") + "\n\n" + FINAL_LINE;
}

/** Fuerza el parseo completo de la nota, insistiendo.
 *
 * `ensureSyntaxTree` hace como mucho `timeout` ms de trabajo y devuelve `null`
 * si no llega; pero cada llamada deja el parser más avanzado, así que repetirla
 * hasta que `syntaxTreeAvailable` diga que está completo llega siempre al final,
 * vaya la máquina rápida o lenta. El resultado no depende del tiempo, solo de
 * que se insista. (Una llamada única con un presupuesto enorme también valdría;
 * así ni siquiera hace falta elegir un número "suficientemente grande".) */
function parseWholeNote(state) {
  const end = state.doc.length;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (syntaxTreeAvailable(state, end)) return;
    ensureSyntaxTree(state, end, 200);
  }
  assert.fail(`la nota no se ha parseado entera tras 100 intentos (${syntaxTree(state).length}/${end})`);
}

/** Estado de la nota grande con el parser ya al final del documento.
 *
 * Ojo: el parseo completo NO se refleja en `syntaxTree(state)`, que sigue
 * devolviendo el árbol parcial del arranque (el campo de estado solo se
 * actualiza con una transacción). El árbol completo vive en el contexto del
 * parser y es lo que devuelve `ensureSyntaxTree`. */
function bigNoteState() {
  const state = stateOf(bigDoc());
  parseWholeNote(state);
  return state;
}

/** El tramo visible de la app al llegar al final de la nota (Ctrl+Fin): la
 * última línea, no el documento entero. */
function finalLineRanges(state) {
  return [{ from: state.doc.length - FINAL_LINE.length, to: state.doc.length }];
}

test("nota grande: el árbol del campo arranca parcial y sigue parcial tras parsear entero (si no, el test no probaría nada)", () => {
  const state = stateOf(bigDoc());
  assert.ok(state.doc.length > 100000, `la nota tiene que ser grande (${state.doc.length})`);
  const atStart = syntaxTree(state).length;
  assert.ok(
    atStart < state.doc.length,
    `el árbol tiene que empezar parcial (${atStart}/${state.doc.length})`
  );
  parseWholeNote(state);
  // Y tras forzar el parseo completo sigue igual: `ensureSyntaxTree` avanza el
  // parser pero no publica el resultado en el campo de estado. Por eso preparar
  // la nota aquí no le regala la respuesta a `syntaxTree(state)` y el arreglo
  // (`ensureSyntaxTree` en `codeRangesOf`) sigue siendo necesario: sin él, las
  // decoraciones se calculan sobre estos ~3 KB y los dos tests de abajo fallan.
  assert.equal(
    syntaxTree(state).length,
    atStart,
    `syntaxTree(state) no debería moverse sin una transacción (${syntaxTree(state).length}/${state.doc.length})`
  );
});

test("nota grande: la flecha del `código` al final no se pinta, la de fuera sí", () => {
  const state = bigNoteState();
  const doc = state.doc.toString();
  const arrows = decorationsOf(buildArrowDecorations(state, finalLineRanges(state)), doc.length);
  assert.deepEqual(
    arrows.map((a) => doc.slice(a.from, a.to)),
    ["->"] // solo la de «a -> b», la última de la línea, que está fuera del código
  );
  assert.equal(arrows[0].glyph, "→");
});

test("nota grande: el `<u>` del `código` al final no se pinta, el de fuera sí", () => {
  const state = bigNoteState();
  const doc = state.doc.toString();
  const decs = decorationsOf(underlineDecorations(state, finalLineRanges(state)), doc.length);
  const underlines = decs.filter((d) => d.class === "cm-mdm-underline");
  assert.equal(underlines.length, 1, "solo el <u>si</u> de fuera del código");
  assert.equal(doc.slice(underlines[0].from, underlines[0].to), "si");
  // Las etiquetas del de fuera se esconden (2 reemplazos); las del de dentro,
  // ni tocadas: si se hubiera colado, habría 2 subrayados y 4 reemplazos.
  assert.equal(decs.filter((d) => !d.class).length, 2);
});
