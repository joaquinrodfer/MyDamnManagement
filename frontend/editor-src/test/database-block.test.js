// Parser y validador de la valla `mdm-database` (bloque de base en una
// página). Casos del formato cerrado del informe §7.3, incluidos los que
// tienen que seguir fallando: sin id, id que no es uuid, YAML roto y valla
// vacía. Y el gate del editor (`isDatabaseFence`), que no puede confundir una
// valla de código normal ni una palabra suelta en un párrafo.

import test from "node:test";
import assert from "node:assert/strict";

import {
  isDatabaseFence,
  parseDatabaseBlock,
  serializeDatabaseBlock,
} from "../database-block.js";

const ID = "7a3f9c21-4b0e-4c8a-9f31-2d5e6b7a8c90";

function fence(...body) {
  return ["```mdm-database", ...body, "```"].join("\n");
}

test("valla completa: id, view y title", () => {
  const r = parseDatabaseBlock(fence(`id: ${ID}`, "view: Tablero", "title: Contactos"));
  assert.deepEqual(r, { ok: true, id: ID, view: "Tablero", title: "Contactos" });
});

test("sin view ni title: null en los dos", () => {
  assert.deepEqual(parseDatabaseBlock(fence(`id: ${ID}`)), {
    ok: true,
    id: ID,
    view: null,
    title: null,
  });
});

test("view vacío o solo comillas: cuenta como ausente", () => {
  assert.equal(parseDatabaseBlock(fence(`id: ${ID}`, "view:")).view, null);
  assert.equal(parseDatabaseBlock(fence(`id: ${ID}`, 'view: ""')).view, null);
});

test("solo el cuerpo (sin las líneas de valla) también se lee", () => {
  assert.deepEqual(parseDatabaseBlock(`id: ${ID}\nview: Tablero`), {
    ok: true,
    id: ID,
    view: "Tablero",
    title: null,
  });
});

test("valla sin cerrar (mientras se escribe) se lee igual", () => {
  assert.deepEqual(parseDatabaseBlock(`\`\`\`mdm-database\nid: ${ID}`), {
    ok: true,
    id: ID,
    view: null,
    title: null,
  });
});

test("sin id: error 'sin id'", () => {
  assert.deepEqual(parseDatabaseBlock(fence("view: Tablero", "title: Contactos")), {
    ok: false,
    motivo: "sin id",
  });
});

test("id que no es uuid: error 'id no es un uuid'", () => {
  assert.deepEqual(parseDatabaseBlock(fence("id: pepito")), {
    ok: false,
    motivo: "id no es un uuid",
  });
  assert.deepEqual(parseDatabaseBlock(fence(`id: ${ID.slice(0, -1)}`)), {
    ok: false,
    motivo: "id no es un uuid",
  });
});

test("YAML roto: error 'yaml inválido'", () => {
  for (const body of ["id: [", `id: ${ID}\nbasura`, `id: ${ID}\ntitle: "sin cerrar`, ': valor']) {
    assert.deepEqual(parseDatabaseBlock(fence(...body.split("\n"))), {
      ok: false,
      motivo: "yaml inválido",
    });
  }
});

test("valla vacía: error 'valla vacía'", () => {
  assert.deepEqual(parseDatabaseBlock(""), { ok: false, motivo: "valla vacía" });
  assert.deepEqual(parseDatabaseBlock("\`\`\`mdm-database\n\`\`\`"), {
    ok: false,
    motivo: "valla vacía",
  });
});

test("valla con otro lenguaje no es un bloque de base", () => {
  assert.deepEqual(parseDatabaseBlock(["```js", `id: ${ID}`, "```"].join("\n")), {
    ok: false,
    motivo: "yaml inválido",
  });
});

test("comillas simples y dobles se quitan", () => {
  const r = parseDatabaseBlock(fence(`id: "${ID}"`, "view: 'Tablero'", 'title: "Contactos"'));
  assert.deepEqual(r, { ok: true, id: ID, view: "Tablero", title: "Contactos" });
});

test("comillas simples: la comilla se escapa doblándola", () => {
  assert.equal(parseDatabaseBlock(fence(`id: ${ID}`, "title: 'O''Brien'")).title, "O'Brien");
});

test("espacios de más alrededor de claves y valores", () => {
  const r = parseDatabaseBlock(fence(`  id :   ${ID}   `, "   view:    Tablero  "));
  assert.deepEqual(r, { ok: true, id: ID, view: "Tablero", title: null });
});

test("CRLF: las líneas con \\r final se leen igual", () => {
  const text = ["```mdm-database", `id: ${ID}`, "view: Tablero", "```", ""].join("\r\n");
  assert.deepEqual(parseDatabaseBlock(text), {
    ok: true,
    id: ID,
    view: "Tablero",
    title: null,
  });
});

test("clave desconocida se ignora y el bloque sigue siendo válido", () => {
  assert.deepEqual(parseDatabaseBlock(fence(`id: ${ID}`, "filtro: x", "title: Contactos")), {
    ok: true,
    id: ID,
    view: null,
    title: "Contactos",
  });
});

test("comentarios y líneas en blanco se saltan", () => {
  const text = fence("# la base de contactos", "", `id: ${ID}`, "", "view: Tablero");
  assert.deepEqual(parseDatabaseBlock(text), {
    ok: true,
    id: ID,
    view: "Tablero",
    title: null,
  });
});

test("ida y vuelta: parse(serialize(x)) es x", () => {
  const cases = [
    { id: ID, view: null, title: null },
    { id: ID, view: "Tablero", title: "Contactos" },
    { id: ID, view: "Todas las filas", title: "Contactos (2026)" },
    { id: ID, view: null, title: "Contactos: los de siempre" },
    { id: ID, view: "Vista #1", title: 'Contactos "viejos"' },
    { id: ID, view: null, title: "O'Brien" },
    { id: ID, view: null, title: "O'Brien, \"el jefe\"" },
    { id: ID, view: null, title: "- no es una lista" },
  ];
  for (const x of cases) {
    const text = serializeDatabaseBlock(x);
    const r = parseDatabaseBlock(text);
    assert.equal(r.ok, true, `no vuelve: ${JSON.stringify(x)} -> ${text}`);
    assert.deepEqual({ id: r.id, view: r.view, title: r.title }, x);
  }
});

test("serialize: la valla que se escribe es la del formato", () => {
  assert.equal(
    serializeDatabaseBlock({ id: ID, view: "Tablero", title: "Contactos" }),
    ["```mdm-database", `id: ${ID}`, "view: Tablero", "title: Contactos", "```"].join("\n")
  );
});

test("serialize sin id revienta (un bloque de base sin id no es un bloque)", () => {
  assert.throws(() => serializeDatabaseBlock({ view: "Tablero" }), TypeError);
});

test("isDatabaseFence: solo la info string exacta", () => {
  const lines = [
    "```mdm-database",
    "```",
    "```js",
    "```mdm-database-otra",
    "texto con ```mdm-database dentro",
    "  ```mdm-database",
    "``` mdm-database",
    "```mdm-database x",
    "````mdm-database",
    "~~~mdm-database",
  ];
  assert.equal(isDatabaseFence(lines, 0), true);
  assert.equal(isDatabaseFence(lines, 1), false, "valla sin lenguaje");
  assert.equal(isDatabaseFence(lines, 2), false, "valla con lenguaje");
  assert.equal(isDatabaseFence(lines, 3), false, "info string más larga");
  assert.equal(isDatabaseFence(lines, 4), false, "la palabra dentro de un párrafo");
  assert.equal(isDatabaseFence(lines, 5), true, "valla con sangría");
  assert.equal(isDatabaseFence(lines, 6), true, "espacio antes de la info string");
  assert.equal(isDatabaseFence(lines, 7), false, "info string con más cosas");
  assert.equal(isDatabaseFence(lines, 8), true, "valla de cuatro acentos");
  assert.equal(isDatabaseFence(lines, 9), false, "tilde no es nuestro formato");
});

test("isDatabaseFence: CRLF e índices que no existen", () => {
  assert.equal(isDatabaseFence(["```mdm-database\r"], 0), true);
  assert.equal(isDatabaseFence(["```mdm-database"], 3), false);
  assert.equal(isDatabaseFence([], 0), false);
  assert.throws(() => isDatabaseFence("```mdm-database", 0), TypeError);
});

test("isDatabaseFence respeta el recorte detrás de la info string", () => {
  assert.equal(isDatabaseFence(["```mdm-database   "], 0), true);
});
