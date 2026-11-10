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
  PAGE_CHOICES_LIMIT,
  capPageChoices,
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

// ------------------------------------------------------- tope de filas pintadas
// Un vault grande no puede pintar una fila por página en cada tecla (medido:
// 107 ms al abrir y 70-90 ms por tecla con 1.208 páginas). El tope recorta lo
// que se pinta y dice cuántas quedan fuera; lo que NO puede tocar es el orden
// ni las acciones (crear y renombrar), que no se pueden sustituir escribiendo.

/** Vault sintético: `n` páginas tituladas «Nota 0000» … «Nota 00nn». */
function vaultDe(n, prefijo = "Nota") {
  return normalizePageEntries(
    Array.from({ length: n }, (_, i) => ({
      id: `page-${String(i).padStart(4, "0")}`,
      title: `${prefijo} ${String(i).padStart(4, "0")}`,
    })),
  );
}

test("el tope recorta los enlaces y dice cuántos quedan fuera", () => {
  const { options, hidden } = capPageChoices(
    pageChoices(vaultDe(PAGE_CHOICES_LIMIT + 10), "", CONTEXTO),
  );
  assert.equal(options.length, PAGE_CHOICES_LIMIT);
  assert.equal(hidden, 10);
  // El orden es el de siempre: las primeras de la lista, tal cual.
  assert.equal(options[0].label, "Nota 0000");
  assert.equal(
    options.at(-1).label,
    `Nota ${String(PAGE_CHOICES_LIMIT - 1).padStart(4, "0")}`,
  );
  assert.equal(options.every((o) => o.kind === "link"), true);
});

test("con menos páginas que el tope no se recorta nada", () => {
  const paginas = vaultDe(3);
  const completas = pageChoices(paginas, "", CONTEXTO);
  const { options, hidden } = capPageChoices(completas);
  assert.equal(hidden, 0);
  assert.deepEqual(
    options.map((o) => o.label),
    ["Nota 0000", "Nota 0001", "Nota 0002"],
  );
});

test("el tope se puede bajar (y a 0 no deja ninguna página)", () => {
  const paginas = vaultDe(6);
  const { options, hidden } = capPageChoices(pageChoices(paginas, "", CONTEXTO), 2);
  assert.deepEqual(
    options.map((o) => o.label),
    ["Nota 0000", "Nota 0001"],
  );
  assert.equal(hidden, 4);
  const cero = capPageChoices(pageChoices(paginas, "", CONTEXTO), 0);
  assert.deepEqual(cero.options, []);
  assert.equal(cero.hidden, 6);
});

test("crear sobrevive al tope: sin ella no habría forma de crear en un vault grande", () => {
  // «Nota» no es el título exacto de ninguna, y las 60 coinciden: el tope
  // recorta los enlaces y «Crear «Nota»» sigue detrás, en su sitio.
  const paginas = vaultDe(60);
  const { options, hidden } = capPageChoices(pageChoices(paginas, "Nota", CONTEXTO));
  assert.equal(options.length, PAGE_CHOICES_LIMIT + 1);
  assert.equal(hidden, 60 - PAGE_CHOICES_LIMIT);
  assert.equal(options.at(-1).kind, "create");
  assert.equal(options.at(-1).label, "Crear «Nota»");
});

test("renombrar sobrevive al tope y la coincidencia exacta sigue la primera", () => {
  // La nota actual («Alfa») más 60 hijas que empiezan por «Alfa»: el tope deja
  // 50 enlaces, pero el primero tiene que seguir siendo la exacta y la acción
  // de renombrar no puede desaparecer.
  const hijas = Array.from({ length: 60 }, (_, i) => ({
    id: `hija-${i}`,
    title: `Alfa ${i}`,
    parent_id: ACTUAL,
  }));
  const paginas = normalizePageEntries([{ id: ACTUAL, title: "Alfa" }, ...hijas]);
  const { options, hidden } = capPageChoices(
    pageChoices(paginas, "Alfa", { currentId: ACTUAL }),
  );
  assert.equal(options[0].kind, "link");
  assert.equal(options[0].id, ACTUAL);
  assert.equal(options.at(-1).kind, "rename");
  assert.equal(hidden, 61 - PAGE_CHOICES_LIMIT);
  assert.equal(options.length, PAGE_CHOICES_LIMIT + 1);
});

test("el tope no inventa opciones si no hay ninguna", () => {
  assert.deepEqual(capPageChoices([]), { options: [], hidden: 0 });
  assert.deepEqual(capPageChoices(undefined), { options: [], hidden: 0 });
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
