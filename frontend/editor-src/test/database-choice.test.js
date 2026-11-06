// Comando "/base" (P17.3): qué opciones ofrece el selector de bases y qué
// texto escribe en la nota al elegir una.
//
// Lo que se prueba aquí es la parte que el editor usa para decidir QUÉ enseñar
// y DÓNDE dejar el cursor; el repintado del menú y las llamadas a la API son de
// app.js. Los casos trampa: la base que se llama igual que otra pero vive en
// otra carpeta (hay que poder distinguirlas), el filtro con acentos y la opción
// de crear, que no puede desaparecer nunca (es la única salida cuando el vault
// no tiene ni una base).

import test from "node:test";
import assert from "node:assert/strict";

import {
  databaseBlockInsert,
  databaseChoices,
  filterDatabases,
  normalizeDatabaseEntries,
} from "../database-choice.js";

const ID = "7a3f9c21-4b0e-4c8a-9f31-2d5e6b7a8c90";
const OTRO = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d";

const BASES = normalizeDatabaseEntries([
  { id: OTRO, title: "Contactos", folder: "Clientes" },
  { id: ID, title: "Contactos", folder: "Bases" },
  { id: "11111111-2222-3333-4444-555555555555", title: "Año fiscal", folder: "Bases" },
]);

test("normaliza: descarta lo que no se puede insertar ni elegir", () => {
  const out = normalizeDatabaseEntries([
    { id: ID, title: " Contactos ", folder: " Bases " },
    { id: "", title: "Sin id" },
    { id: OTRO, title: "   " },
    null,
    { id: ID, title: "Repetida" },
  ]);
  assert.deepEqual(out, [{ id: ID, title: "Contactos", folder: "Bases" }]);
  assert.deepEqual(normalizeDatabaseEntries(undefined), []);
});

test("el selector lista por título y distingue las que se llaman igual por su carpeta", () => {
  const opciones = databaseChoices(BASES, "");
  assert.deepEqual(
    opciones.map((o) => [o.label, o.hint]),
    [
      ["Contactos", "Clientes"],
      ["Contactos", "Bases"],
      ["Año fiscal", "Bases"],
      ["Nueva base…", "crear"],
    ],
  );
  assert.equal(opciones[0].kind, "item");
  assert.equal(opciones[0].id, OTRO);
  assert.equal(opciones[3].kind, "create");
});

test("el filtro mira título y carpeta, sin mayúsculas ni acentos", () => {
  const bus = (q) => filterDatabases(BASES, q).map((e) => `${e.title}@${e.folder}`);
  assert.deepEqual(bus("cont"), ["Contactos@Clientes", "Contactos@Bases"]);
  assert.deepEqual(bus("CLIENTES"), ["Contactos@Clientes"]);
  assert.deepEqual(bus("ano"), ["Año fiscal@Bases"]);
  assert.deepEqual(bus("  año  "), ["Año fiscal@Bases"]);
  assert.deepEqual(bus("no existe"), []);
  assert.equal(bus("").length, 3);
});

test("con una búsqueda, la de crear lleva el texto escrito en la etiqueta", () => {
  const opciones = databaseChoices(BASES, "  con ");
  assert.deepEqual(
    opciones.map((o) => o.label),
    ["Contactos", "Contactos", "Crear «con»"],
  );
  const sinCoincidencias = databaseChoices(BASES, "zzz");
  assert.deepEqual(sinCoincidencias.map((o) => o.label), ["Crear «zzz»"]);
  assert.equal(sinCoincidencias.at(-1).kind, "create");
});

test("sin ninguna base el selector sigue ofreciendo crear (y nada más)", () => {
  const opciones = databaseChoices([], "");
  assert.deepEqual(opciones.map((o) => [o.kind, o.label]), [["create", "Nueva base…"]]);
  assert.deepEqual(databaseChoices(normalizeDatabaseEntries([]), "algo").map((o) => o.label), [
    "Crear «algo»",
  ]);
});

test("lo que se escribe es la valla cerrada y el cursor queda debajo, fuera", () => {
  const { text, cursorOffset } = databaseBlockInsert({ id: ID, view: "Tablero", title: "Contactos" });
  assert.equal(
    text,
    ["```mdm-database", `id: ${ID}`, "view: Tablero", "title: Contactos", "```", "", ""].join("\n"),
  );
  assert.equal(cursorOffset, text.length);
  // El cursor cae en una línea vacía DESPUÉS de la valla de cierre: nunca
  // dentro (dentro, el editor pinta la valla en crudo y lo que se escriba
  // pasaría a ser parte del bloque).
  const antes = text.slice(0, cursorOffset);
  assert.equal(antes.split("\n").at(-1), "");
  assert.ok(antes.trimEnd().endsWith("```"));
  assert.equal(antes.trimEnd().split("\n").at(-1), "```");
});

test("sin vista no se escribe `view` (abre la primera, como el bloque pintado)", () => {
  const { text } = databaseBlockInsert({ id: ID, title: "Contactos", view: null });
  assert.ok(!text.includes("view:"));
  assert.ok(text.includes("title: Contactos"));
});

test("un bloque sin id no se puede insertar", () => {
  assert.throws(() => databaseBlockInsert({ title: "Contactos" }), TypeError);
  assert.throws(() => databaseBlockInsert({ id: "", title: "Contactos" }), TypeError);
});
