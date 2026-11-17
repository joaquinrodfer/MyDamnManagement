// Separadores y bloques vacíos (block-gaps.js): qué papel tiene cada línea en
// blanco entre bloques, dónde se queda un cursor que cae en el aire, y qué
// hacen ⌫ y Supr en la frontera entre dos bloques -- lo de Notion.

import test from "node:test";
import assert from "node:assert/strict";

import { EditorState } from "@codemirror/state";
import { markdown } from "@codemirror/lang-markdown";

import {
  emptyBlockTail,
  gapBackspaceEdit,
  gapDeleteEdit,
  gapRole,
  leaveSeparator,
  nextLineSkippingSeparators,
} from "../block-gaps.js";
import { blocksFromTree } from "../block-ranges.js";

const parser = markdown({ addKeymap: false }).language.parser;
const docOf = (text) => EditorState.create({ doc: text }).doc;
const blocksOf = (text) => blocksFromTree(parser.parse(text).topNode, text);

/** Papeles de todas las líneas: "B" bloque, "S" separador, "E" vacío. */
function roles(text) {
  const doc = docOf(text);
  const blocks = blocksOf(text);
  const out = [];
  for (let n = 1; n <= doc.lines; n++) {
    const r = gapRole(doc, blocks, n);
    out.push(r === "sep" ? "S" : r === "empty" ? "E" : "B");
  }
  return out.join("");
}

/** Aplica un resultado `{changes, cursor}` y devuelve `[texto, cursor]`. */
function apply(text, edit) {
  const doc = docOf(text);
  const st = EditorState.create({ doc: text }).update({ changes: edit.changes });
  void doc;
  return [st.state.doc.toString(), edit.cursor];
}

const back = (text, pos) => gapBackspaceEdit(docOf(text), blocksOf(text), pos);
const del = (text, pos) => gapDeleteEdit(docOf(text), blocksOf(text), pos);

// ------------------------------------------------------------ papeles

test("entre dos bloques, una línea en blanco es un separador", () => {
  assert.equal(roles("alfa\n\nbeta"), "BSB");
});

test("separador, vacío, separador: el bloque vacío de en medio", () => {
  assert.equal(roles("# A\n\n\n\nTexto"), "BSESB");
  assert.equal(roles("# A\n\n\n\n\n\nTexto"), "BSESESB");
});

test("detrás del último bloque: separador y luego vacío", () => {
  assert.equal(roles("# A\n\n"), "BSE");
  assert.equal(roles("# A\n"), "BS");
});

test("al principio de la nota se empieza por un vacío", () => {
  assert.equal(roles(""), "E");
  assert.equal(roles("\n\nalfa"), "ESB");
});

test("las líneas en blanco de un bloque de código son contenido", () => {
  assert.equal(roles("```\nuno\n\ndos\n```\n\nfin"), "BBBBBSB");
});

test("dos líneas en blanco escritas a mano: separador y vacío", () => {
  assert.equal(roles("alfa\n\n\nbeta"), "BSEB");
});

// ------------------------------------------------------------ el cursor

test("un cursor en un separador sale hacia donde iba", () => {
  const text = "alfa\n\nbeta";
  const doc = docOf(text);
  const blocks = blocksOf(text);
  assert.equal(leaveSeparator(doc, blocks, 5, true), 6);
  assert.equal(leaveSeparator(doc, blocks, 5, false), 4);
  assert.equal(leaveSeparator(doc, blocks, 2, true), 2, "fuera de un separador no se toca");
});

test("el separador del final de la nota vuelve arriba aunque se fuera hacia delante", () => {
  const text = "alfa\n";
  assert.equal(leaveSeparator(docOf(text), blocksOf(text), 5, true), 4);
});

test("↑/↓ saltan los separadores pero paran en los bloques vacíos", () => {
  const text = "# A\n\n\n\nTexto";
  const doc = docOf(text);
  const blocks = blocksOf(text);
  assert.equal(nextLineSkippingSeparators(doc, blocks, 1, 1), 3);
  assert.equal(nextLineSkippingSeparators(doc, blocks, 3, 1), 5);
  assert.equal(nextLineSkippingSeparators(doc, blocks, 5, -1), 3);
  assert.equal(nextLineSkippingSeparators(doc, blocks, 5, 1), null);
});

// ------------------------------------------------------------ ⌫

test("⌫ en un bloque vacío lo quita con su separador y vuelve al final de arriba", () => {
  assert.deepEqual(apply("# A\n\n\n\nTexto", back("# A\n\n\n\nTexto", 5)), ["# A\n\nTexto", 3]);
  assert.deepEqual(apply("# A\n\n", back("# A\n\n", 5)), ["# A", 3]);
});

test("⌫ al principio de un bloque fusiona con el de arriba (sin su marcador)", () => {
  assert.deepEqual(apply("alfa\n\nbeta", back("alfa\n\nbeta", 6)), ["alfabeta", 4]);
  assert.deepEqual(apply("# Titulo\n\nbeta", back("# Titulo\n\nbeta", 10)), ["# Titulobeta", 8]);
});

test("⌫ al principio de un encabezado lo vuelve párrafo", () => {
  assert.deepEqual(apply("alfa\n\n## Sub", back("alfa\n\n## Sub", 6)), ["alfa\n\nSub", 6]);
  assert.deepEqual(apply("alfa\n\n## Sub", back("alfa\n\n## Sub", 9)), ["alfa\n\nSub", 6]);
});

test("⌫ al principio de una cita la vuelve párrafo, todas sus filas", () => {
  const text = "alfa\n\n> uno\n> dos";
  assert.deepEqual(apply(text, back(text, 6)), ["alfa\n\nuno\ndos", 6]);
});

test("⌫ con un bloque vacío encima se lleva el vacío", () => {
  const text = "alfa\n\n\n\nbeta";
  assert.deepEqual(apply(text, back(text, 8)), ["alfa\n\nbeta", 6]);
});

test("⌫ bajo una línea horizontal la quita", () => {
  const text = "alfa\n\n---\n\nbeta";
  assert.deepEqual(apply(text, back(text, 11)), ["alfa\n\nbeta", 6]);
});

test("⌫ bajo un bloque de código no fusiona: el cursor sube al final", () => {
  const text = "```\nx\n```\n\nbeta";
  const edit = back(text, 11);
  assert.deepEqual(edit.changes, []);
  assert.equal(edit.cursor, 9);
});

test("⌫ en medio de una línea, o sin separador encima, no es cosa de este módulo", () => {
  assert.equal(back("alfa\n\nbeta", 8), null);
  assert.equal(back("# A\nTexto", 4), null);
  assert.equal(back("alfa", 0), null);
});

// ------------------------------------------------------------ Supr

test("Supr al final de un bloque sube el de abajo", () => {
  assert.deepEqual(apply("alfa\n\nbeta", del("alfa\n\nbeta", 4)), ["alfabeta", 4]);
  assert.deepEqual(apply("alfa\n\n# Sub", del("alfa\n\n# Sub", 4)), ["alfaSub", 4]);
});

test("Supr con un bloque vacío debajo se lo lleva", () => {
  const text = "alfa\n\n\n\nbeta";
  assert.deepEqual(apply(text, del(text, 4)), ["alfa\n\nbeta", 4]);
});

test("Supr en un bloque vacío lo quita y deja el cursor en el de abajo", () => {
  const text = "alfa\n\n\n\nbeta";
  assert.deepEqual(apply(text, del(text, 6)), ["alfa\n\nbeta", 6]);
});

test("Supr en medio de una línea no es cosa de este módulo", () => {
  assert.equal(del("alfa\n\nbeta", 2), null);
  assert.equal(del("alfa", 4), null);
});

// ------------------------------------------------------- escribir en un vacío

test("escribir en un vacío con texto pegado debajo le pone separador", () => {
  const text = "alfa\n\n\nbeta";
  assert.equal(emptyBlockTail(docOf(text), blocksOf(text), 6), "\n");
});

test("escribir en un vacío con su separador ya puesto no añade nada", () => {
  const text = "# A\n\n\n\nTexto";
  assert.equal(emptyBlockTail(docOf(text), blocksOf(text), 5), "");
  assert.equal(emptyBlockTail(docOf("# A\n\n"), blocksOf("# A\n\n"), 5), "");
});
