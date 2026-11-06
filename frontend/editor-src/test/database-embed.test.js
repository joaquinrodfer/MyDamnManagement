// Localización de los bloques `mdm-database` de un documento (P17.2) y los
// textos de sus tarjetas de error.
//
// Lo que se prueba aquí es la parte que el editor usa para decidir QUÉ pintar:
// dónde empieza y acaba cada valla cerrada y qué dijo el parser. Los casos
// trampa son los de siempre en Markdown -- una valla dentro de otra valla, una
// valla sin cerrar (que no puede tragarse el resto de la nota) y un párrafo
// que solo menciona la palabra.

import test from "node:test";
import assert from "node:assert/strict";

import {
  databaseErrorText,
  databaseNotFoundText,
  scanDatabaseBlocks,
} from "../database-embed.js";

const ID = "7a3f9c21-4b0e-4c8a-9f31-2d5e6b7a8c90";

function fence(...body) {
  return ["```mdm-database", ...body, "```"].join("\n");
}

test("un bloque cerrado: offsets y parse", () => {
  const doc = ["# Nota", "", fence(`id: ${ID}`, "view: Tablero", "title: Contactos"), "", "sigo"].join(
    "\n",
  );
  const blocks = scanDatabaseBlocks(doc);
  assert.equal(blocks.length, 1);
  assert.deepEqual(blocks[0].parse, { ok: true, id: ID, view: "Tablero", title: "Contactos" });
  // El rango es exactamente la valla: de la línea de apertura al final de la de
  // cierre, sin el salto que la sigue (si no, el widget se comería una línea).
  assert.equal(blocks[0].from, doc.indexOf("```mdm-database"));
  assert.equal(
    doc.slice(blocks[0].from, blocks[0].to),
    fence(`id: ${ID}`, "view: Tablero", "title: Contactos"),
  );
  assert.equal(doc.slice(blocks[0].to), "\n\nsigo");
});

test("sin view ni title: el parse los deja en null", () => {
  const [block] = scanDatabaseBlocks(fence(`id: ${ID}`));
  assert.deepEqual(block.parse, { ok: true, id: ID, view: null, title: null });
});

test("dos bloques en el mismo documento: cada uno con su rango", () => {
  const uno = fence(`id: ${ID}`, "title: Uno");
  const otro = fence(`id: ${ID}`, "title: Otro");
  const doc = [uno, "", "en medio", "", otro].join("\n");
  const blocks = scanDatabaseBlocks(doc);
  assert.equal(blocks.length, 2);
  assert.equal(doc.slice(blocks[0].from, blocks[0].to), uno);
  assert.equal(doc.slice(blocks[1].from, blocks[1].to), otro);
  assert.equal(blocks[0].parse.title, "Uno");
  assert.equal(blocks[1].parse.title, "Otro");
});

test("documento sin bloques: lista vacía", () => {
  assert.deepEqual(scanDatabaseBlocks(""), []);
  assert.deepEqual(scanDatabaseBlocks("# Hola\n\ntexto normal\n"), []);
});

test("un párrafo que menciona mdm-database NO es un bloque", () => {
  assert.deepEqual(scanDatabaseBlocks("esto menciona mdm-database y nada más\n"), []);
  assert.deepEqual(scanDatabaseBlocks("```\nmdm-database\n```\n"), []);
});

test("una valla de otro lenguaje se sigue viendo como código", () => {
  assert.deepEqual(scanDatabaseBlocks("```js\nconst x = 1;\n```\n"), []);
  // Y el nombre tiene que ser exacto: ```mdm-databasex no es nuestra valla.
  assert.deepEqual(scanDatabaseBlocks("```mdm-databasex\nid: " + ID + "\n```\n"), []);
});

test("un ```mdm-database DENTRO de otra valla es texto, no un bloque", () => {
  const doc = ["```js", "```mdm-database", `id: ${ID}`, "```", "```"].join("\n");
  assert.deepEqual(scanDatabaseBlocks(doc), []);
});

test("tras una valla de otro lenguaje, el siguiente bloque sí se pinta", () => {
  const doc = ["```js", "```mdm-database", "```", "", fence(`id: ${ID}`)].join("\n");
  const blocks = scanDatabaseBlocks(doc);
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].parse.id, ID);
});

test("valla SIN cerrar: no se devuelve (se ve cruda, no se traga la nota)", () => {
  assert.deepEqual(scanDatabaseBlocks(`\`\`\`mdm-database\nid: ${ID}\n`), []);
  assert.deepEqual(scanDatabaseBlocks(`\`\`\`mdm-database\nid: ${ID}`), []);
});

test("el cierre con info string no cierra el bloque", () => {
  const doc = ["```mdm-database", `id: ${ID}`, "```js", "```"].join("\n");
  const blocks = scanDatabaseBlocks(doc);
  assert.equal(blocks.length, 1);
  // El ```js es una línea más del cuerpo; el bloque acaba en el ``` seco.
  assert.deepEqual(blocks[0].parse, { ok: true, id: ID, view: null, title: null });
});

test("un cierre más corto que la apertura no cierra (regla de Markdown)", () => {
  const doc = ["````mdm-database", `id: ${ID}`, "```", "````"].join("\n");
  const blocks = scanDatabaseBlocks(doc);
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].to, doc.length);
});

test("CRLF: los bloques se leen igual y los offsets siguen siendo del texto", () => {
  const doc = ["# Nota", "", "```mdm-database", `id: ${ID}`, "title: Contactos", "```", ""].join(
    "\r\n",
  );
  const [block] = scanDatabaseBlocks(doc);
  assert.deepEqual(block.parse, { ok: true, id: ID, view: null, title: "Contactos" });
  const raw = doc.slice(block.from, block.to);
  assert.equal(raw.split("\r\n")[0], "```mdm-database");
  // `to` cae en el \n, así que el \r del cierre entra en el rango (inocuo: el
  // editor alinea el reemplazo con líneas enteras de todas formas).
  assert.ok(raw.replace(/\r$/, "").endsWith("```"));
  assert.equal(doc.slice(block.to), "\n");
});

test("con sangría (el .md se escribe a mano) también se encuentra", () => {
  const doc = ["  ```mdm-database", `  id: ${ID}`, "  ```", ""].join("\n");
  const [block] = scanDatabaseBlocks(doc);
  assert.deepEqual(block.parse, { ok: true, id: ID, view: null, title: null });
  assert.equal(block.from, 0);
});

test("un bloque al final del documento, sin salto detrás", () => {
  const doc = "texto\n\n" + fence(`id: ${ID}`);
  const [block] = scanDatabaseBlocks(doc);
  assert.equal(block.to, doc.length);
});

test("bloque roto: el motivo lo da el parser, no un texto inventado aquí", () => {
  assert.deepEqual(scanDatabaseBlocks(fence("id: pepito"))[0].parse, {
    ok: false,
    motivo: "id no es un uuid",
  });
  assert.deepEqual(scanDatabaseBlocks(fence("view: Tablero"))[0].parse, {
    ok: false,
    motivo: "sin id",
  });
  assert.deepEqual(scanDatabaseBlocks(fence("id: ["))[0].parse, {
    ok: false,
    motivo: "yaml inválido",
  });
  assert.deepEqual(scanDatabaseBlocks(["```mdm-database", "```"].join("\n"))[0].parse, {
    ok: false,
    motivo: "valla vacía",
  });
});

test("un bloque roto se localiza igual: el texto se queda para poder arreglarlo", () => {
  const doc = "antes\n" + fence("id: pepito") + "\nafter";
  const [block] = scanDatabaseBlocks(doc);
  assert.equal(block.parse.ok, false);
  assert.equal(doc.slice(block.from, block.from + 15), "```mdm-database");
  assert.ok(doc.slice(block.from, block.to).endsWith("```"));
});

test("tarjeta de error: lleva el motivo del parser tal cual", () => {
  assert.equal(
    databaseErrorText({ ok: false, motivo: "id no es un uuid" }),
    "Bloque de base no válido: id no es un uuid",
  );
  assert.equal(databaseErrorText(null), "Bloque de base no válido: valla inválida");
});

test("tarjeta de base no encontrada: título si lo hay, si no el id", () => {
  assert.equal(databaseNotFoundText("Contactos", ID), "Base no encontrada: Contactos");
  assert.equal(databaseNotFoundText(null, ID), `Base no encontrada: ${ID}`);
  assert.equal(databaseNotFoundText("   ", ID), `Base no encontrada: ${ID}`);
  assert.equal(databaseNotFoundText("", ""), "Base no encontrada: ");
});
