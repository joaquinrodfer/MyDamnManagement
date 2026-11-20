// Selección por rectángulo desde el fondo de la página: qué bloques toca y
// cómo se combina con lo que ya estaba marcado (ver block-marquee.js).

import test from "node:test";
import assert from "node:assert/strict";

import { blocksInMarquee, marqueeMode, mergeMarquee, sameSet } from "../block-marquee.js";

const BANDS = [
  { top: 0, bottom: 40 },
  { top: 64, bottom: 90 },
  { top: 114, bottom: 200 },
  null, // un bloque que no se ha podido medir no se marca
  { top: 230, bottom: 260 },
];
const COLUMN = { left: 100, right: 800 };

test("marca los bloques que el rectángulo roza", () => {
  const hits = blocksInMarquee(BANDS, { left: 50, right: 300, top: 80, bottom: 120 }, COLUMN);
  assert.deepEqual([...hits], [1, 2]);
});

test("un rectángulo entre dos bloques no marca nada", () => {
  const hits = blocksInMarquee(BANDS, { left: 50, right: 300, top: 92, bottom: 110 }, COLUMN);
  assert.equal(hits.size, 0);
});

test("un rectángulo que se queda en el margen no marca nada", () => {
  assert.equal(blocksInMarquee(BANDS, { left: 10, right: 90, top: 0, bottom: 300 }, COLUMN).size, 0);
  assert.equal(blocksInMarquee(BANDS, { left: 810, right: 900, top: 0, bottom: 300 }, COLUMN).size, 0);
});

test("de arriba abajo del todo marca todos los que se conocen", () => {
  const hits = blocksInMarquee(BANDS, { left: 0, right: 900, top: -50, bottom: 400 }, COLUMN);
  assert.deepEqual([...hits], [0, 1, 2, 4]);
});

test("sin teclas, el rectángulo sustituye lo marcado", () => {
  assert.deepEqual([...mergeMarquee(new Set([0]), new Set([2, 3]), "replace")], [2, 3]);
});

test("con Mayús, se suma a lo marcado", () => {
  assert.deepEqual([...mergeMarquee(new Set([0]), new Set([2]), "add")].sort(), [0, 2]);
});

test("con Ctrl, cambia el estado de lo que toca", () => {
  assert.deepEqual([...mergeMarquee(new Set([0, 2]), new Set([2, 3]), "toggle")].sort(), [0, 3]);
});

test("las teclas eligen el modo", () => {
  assert.equal(marqueeMode({}), "replace");
  assert.equal(marqueeMode({ shiftKey: true }), "add");
  assert.equal(marqueeMode({ ctrlKey: true }), "toggle");
  assert.equal(marqueeMode({ metaKey: true, shiftKey: true }), "toggle");
});

test("sameSet compara sin orden", () => {
  assert.ok(sameSet(new Set([1, 2]), new Set([2, 1])));
  assert.ok(!sameSet(new Set([1, 2]), new Set([1])));
});
