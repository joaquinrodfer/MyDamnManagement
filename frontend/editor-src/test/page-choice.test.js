// Comando "/page": qué ofrece su selector (enlazar con una página que ya
// existe, crearla o renombrar la que choca con el nombre escrito) y qué tramo
// del documento ocupa lo tecleado.
//
// Lo que se prueba aquí es la parte que el editor usa para decidir QUÉ enseñar
// y qué hacer al elegir; la llamada a la API, la navegación y el repintado del
// menú son de app.js. Los casos trampa: el nombre que ya existe (no se puede
// ofrecer crear, o el `[[título]]` queda ambiguo), el nombre de la nota actual
// y el de una hija (ahí sí toca renombrar), y el filtro con acentos.

import test from "node:test";
import assert from "node:assert/strict";

import { Text } from "@codemirror/state";

import {
  filterPages,
  findExactPage,
  normalizePageEntries,
  pageChoices,
  pageFilterClear,
  pageFilterRange,
} from "../page-choice.js";

const ACTUAL = "11111111-1111-4111-8111-111111111111";
const HIJA = "22222222-2222-4222-8222-222222222222";
const OTRA = "33333333-3333-4333-8333-333333333333";
const BASE = "44444444-4444-4444-8444-444444444444";

const PAGINAS = normalizePageEntries([
  { id: OTRA, title: "Proyecto Alfa", folder: "Clientes" },
  { id: HIJA, title: "Alfa", parent_id: ACTUAL, folder: "Proyectos" },
  { id: ACTUAL, title: "Proyectos", parent_id: null, folder: "" },
  { id: BASE, title: "Año fiscal", type: "database", folder: "Proyectos" },
]);

const CONTEXTO = { currentId: ACTUAL, currentParentId: null };

test("normaliza: descarta lo que no se puede elegir ni enlazar", () => {
  const out = normalizePageEntries([
    { id: HIJA, title: " Alfa ", parent_id: ACTUAL, folder: " Proyectos " },
    { id: "", title: "Sin id" },
    { id: OTRA, title: "   " },
    { id: HIJA, title: "Repetida" },
    { id: "55555555-5555-4555-8555-555555555555", title: "Notas [2026]" },
    null,
  ]);
  assert.deepEqual(out, [
    { id: HIJA, title: "Alfa", type: "note", parentId: ACTUAL, folder: "Proyectos" },
  ]);
  assert.deepEqual(normalizePageEntries(undefined), []);
});

test("el tipo solo distingue base de nota, y la raíz llega como null", () => {
  const [base, nota] = normalizePageEntries([
    { id: BASE, title: "Contactos", type: "database", parent_id: "" },
    { id: OTRA, title: "Otra", type: "otra-cosa" },
  ]);
  assert.equal(base.type, "database");
  assert.equal(base.parentId, null);
  assert.equal(nota.type, "note");
  assert.equal(nota.parentId, null);
});

test("filtra sin acentos ni mayúsculas, primero las que empiezan por lo escrito", () => {
  assert.deepEqual(
    filterPages(PAGINAS, "alf").map((p) => p.title),
    ["Alfa", "Proyecto Alfa"],
  );
  assert.deepEqual(
    filterPages(PAGINAS, "ano").map((p) => p.title),
    ["Año fiscal"],
  );
  // La carpeta también cuenta: «proyectos» encuentra lo que vive ahí.
  assert.deepEqual(
    filterPages(PAGINAS, "proyectos").map((p) => p.title),
    ["Proyectos", "Alfa", "Año fiscal"],
  );
  assert.deepEqual(filterPages(PAGINAS, "zzz"), []);
});

test("sin texto escrito no hay opción de crear: solo la lista del vault", () => {
  const opciones = pageChoices(PAGINAS, "", CONTEXTO);
  assert.deepEqual(
    opciones.map((o) => [o.kind, o.label]),
    [
      ["link", "Alfa"],
      ["link", "Año fiscal"],
      ["link", "Proyecto Alfa"],
      ["link", "Proyectos"],
    ],
  );
  assert.equal(opciones.every((o) => o.kind === "link"), true);
});

test("un nombre que no existe ofrece crearlo, y solo eso", () => {
  const opciones = pageChoices(PAGINAS, "Nueva idea", CONTEXTO);
  assert.deepEqual(
    opciones.map((o) => [o.kind, o.label, o.hint]),
    [["create", "Crear «Nueva idea»", "página nueva"]],
  );
});

test("nombre que ya existe: enlaza primero y NO se ofrece crear (duplicado)", () => {
  // «Proyecto Alfa» no es la nota actual ni una hija suya: solo se enlaza.
  const opciones = pageChoices(PAGINAS, "proyecto alfa", CONTEXTO);
  assert.equal(opciones[0].kind, "link");
  assert.equal(opciones[0].id, OTRA);
  assert.equal(opciones[0].hint, "Clientes");
  assert.equal(opciones.some((o) => o.kind === "create"), false);
  assert.equal(opciones.some((o) => o.kind === "rename"), false);
});

test("nombre de una hija: enlaza o renombra, nunca crea otra igual", () => {
  const opciones = pageChoices(PAGINAS, "Alfa", CONTEXTO);
  assert.deepEqual(
    opciones.map((o) => [o.kind, o.label, o.hint]),
    [
      ["link", "Alfa", "Proyectos"],
      ["link", "Proyecto Alfa", "Clientes"],
      ["rename", "Renombrar «Alfa»…", "nombre nuevo"],
    ],
  );
  assert.equal(opciones.some((o) => o.kind === "create"), false);
});

test("nombre de la nota actual: también se ofrece renombrar", () => {
  const opciones = pageChoices(PAGINAS, "Proyectos", CONTEXTO);
  assert.equal(opciones[0].kind, "link");
  assert.equal(opciones[0].id, ACTUAL);
  assert.equal(opciones.at(-1).kind, "rename");
  assert.equal(opciones.at(-1).id, ACTUAL);
  assert.equal(opciones.some((o) => o.kind === "create"), false);
});

test("el que empieza por lo escrito va antes que el que solo lo contiene", () => {
  const opciones = pageChoices(PAGINAS, "alfa", CONTEXTO);
  assert.deepEqual(
    opciones.map((o) => [o.kind, o.label]),
    [
      ["link", "Alfa"],
      ["link", "Proyecto Alfa"],
      ["rename", "Renombrar «Alfa»…"],
    ],
  );
});

test("coincidencia parcial sin exacta: las que hay delante, crear al final", () => {
  const opciones = pageChoices(PAGINAS, "alf", CONTEXTO);
  assert.deepEqual(
    opciones.map((o) => [o.kind, o.label]),
    [
      ["link", "Alfa"],
      ["link", "Proyecto Alfa"],
      ["create", "Crear «alf»"],
    ],
  );
});

test("un nombre largo que no coincide con nada: solo crear", () => {
  const opciones = pageChoices(PAGINAS, "Alfa nue", CONTEXTO);
  assert.deepEqual(
    opciones.map((o) => [o.kind, o.label]),
    [["create", "Crear «Alfa nue»"]],
  );
});

test("renombrar no se ofrece si la coincidencia es de otra carpeta", () => {
  // «Alfa» es hija de «Proyectos», pero la nota actual es otra: no hay choque.
  const otras = { currentId: OTRA };
  const opciones = pageChoices(PAGINAS, "Alfa", otras);
  assert.deepEqual(
    opciones.map((o) => o.kind),
    ["link", "link"],
  );
});

test("findExactPage compara sin acentos ni mayúsculas", () => {
  assert.equal(findExactPage(PAGINAS, "año FISCAL")?.id, BASE);
  assert.equal(findExactPage(PAGINAS, ""), null);
  assert.equal(findExactPage(PAGINAS, "no existe"), null);
});

test("el tramo del filtro llega al final de la línea, no al caret", () => {
  const doc = Text.of(["texto antes ", "Alfa", "otra línea"]);
  // Posición 13: principio de la segunda línea («Alfa»).
  assert.deepEqual(pageFilterRange(doc, 13), { from: 13, to: 17 });
  assert.deepEqual(pageFilterClear(doc, 13), { from: 13, to: 17 });
  // Línea vacía: no hay nada que borrar.
  const vacio = Text.of(["Alfa", ""]);
  assert.deepEqual(pageFilterRange(vacio, 5), { from: 5, to: 5 });
  assert.equal(pageFilterClear(vacio, 5), null);
});
