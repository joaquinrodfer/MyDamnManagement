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

import { mediaOfBlockText } from "../entry.js";

test("un archivo de la app solo en su línea es multimedia según su extensión", () => {
  const u = (ext) => `/files/_attachments/2026/11/abc-x.${ext}`;
  assert.equal(mediaOfBlockText(`[demo.mp4](${u("mp4")})`).kind, "video");
  assert.equal(mediaOfBlockText(`[nota.mp3](${u("mp3")})`).kind, "audio");
  assert.equal(mediaOfBlockText(`[informe.pdf](${u("pdf")})`).kind, "pdf");
  assert.deepEqual(mediaOfBlockText(`[hoja.xlsx](${u("xlsx")})`), { kind: "file", alt: "hoja.xlsx", url: u("xlsx") });
  assert.equal(mediaOfBlockText(`![pie](${u("png")})`).kind, "image");
});

test("un enlace normal o a otra web no es un archivo", () => {
  assert.equal(mediaOfBlockText("[Notion](https://notion.so)"), null);
  assert.equal(mediaOfBlockText("Mira [demo.mp4](/files/a.mp4) aquí"), null);
});
