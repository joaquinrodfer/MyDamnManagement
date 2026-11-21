// Imágenes en el cuerpo: qué línea es una imagen y dónde entra una nueva.

import test from "node:test";
import assert from "node:assert/strict";
import { Text } from "@codemirror/state";

import { blockInsertAt, imageOfBlockText } from "../entry.js";

test("una línea con solo una imagen de la app es una imagen", () => {
  assert.deepEqual(imageOfBlockText("![Portada del informe](/files/_attachments/2026/11/abc-portada.png)"), {
    alt: "Portada del informe",
    url: "/files/_attachments/2026/11/abc-portada.png",
  });
  assert.deepEqual(imageOfBlockText("![](https://example.com/a.png)"), { alt: "", url: "https://example.com/a.png" });
});

test("no es imagen si hay más texto o la url no es de fiar", () => {
  assert.equal(imageOfBlockText("Mira esto ![x](/files/a.png)"), null);
  assert.equal(imageOfBlockText("![x](/files/a.png) y más"), null);
  assert.equal(imageOfBlockText("![x](javascript:alert(1))"), null);
  assert.equal(imageOfBlockText("[x](/files/a.png)"), null);
});

test("una imagen nueva va en la línea vacía o detrás del bloque", () => {
  const doc = Text.of(["Primer párrafo", "sigue aquí", "", "", "Otro"]);
  const blocks = [
    { from: 0, to: 25, type: "paragraph" },
    { from: 28, to: 32, type: "paragraph" },
  ];
  assert.deepEqual(blockInsertAt(doc, blocks, 5), { from: 25, to: 25, before: "\n\n", after: "" });
  assert.deepEqual(blockInsertAt(doc, blocks, 27), { from: 27, to: 27, before: "", after: "" });
});
