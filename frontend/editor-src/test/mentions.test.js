// Menciones @: qué personas ofrece la lista para lo escrito.

import test from "node:test";
import assert from "node:assert/strict";

import { mentionOptions } from "../entry.js";

const GENTE = [
  { username: "marta", display_name: "Marta Gómez" },
  { username: "luis", display_name: "Luis Ortega" },
  { username: "mario_r", display_name: "Mario Ruiz" },
  { username: "sara", display_name: "Sara Álvarez" },
];

test("sin escribir nada salen todas, por nombre", () => {
  assert.deepEqual(mentionOptions(GENTE, "").map((o) => o.user), ["luis", "mario_r", "marta", "sara"]);
});

test("primero las que empiezan por lo escrito, sin acentos ni mayúsculas", () => {
  assert.deepEqual(mentionOptions(GENTE, "mar").map((o) => o.user), ["mario_r", "marta"]);
  assert.deepEqual(mentionOptions(GENTE, "alv").map((o) => o.user), ["sara"]);
  assert.deepEqual(mentionOptions(GENTE, "ORT").map((o) => o.user), ["luis"]);
});

test("nada coincide, nada se ofrece", () => {
  assert.deepEqual(mentionOptions(GENTE, "zzz"), []);
});
