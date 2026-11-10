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
