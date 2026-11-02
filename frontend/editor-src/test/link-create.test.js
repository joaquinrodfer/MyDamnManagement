// Prueba de regresión de la opción «Crear «…»» del autocompletado de [[.
//
// El fallo que fija: con espacios al principio o al final de lo tecleado, el
// título se recortaba para el POST pero la guarda comparaba el texto del
// documento (crudo) contra ese título recortado. Resultado: la nota se creaba
// en el servidor y el enlace no se insertaba, sin avisar.
//
// Corre sin navegador ni dependencias (node --test), sobre la lógica pura de
// link-create.js; el `npm test` del CI lo ejecuta.

import test from "node:test";
import assert from "node:assert/strict";

import {
  createLinkTitle,
  isLinkableTitle,
  linkMarkup,
  resolveCreatedLinkTitle,
} from "../link-create.js";

test("el título que va al POST va sin espacios sobrantes", () => {
  assert.equal(createLinkTitle("  Mi nota  "), "Mi nota");
  assert.equal(createLinkTitle(""), "");
  assert.equal(createLinkTitle(null), "");
});

test("crear con espacio al final inserta el título real del servidor", () => {
  // Documento: "[[Mi nota " (espacio final) -- el fragmento bajo "[[" es crudo.
  const fragment = "[[Mi nota ";
  const real = resolveCreatedLinkTitle(fragment, "Mi nota ", "Mi nota");
  assert.equal(real, "Mi nota");
  assert.equal(linkMarkup(real), "[[Mi nota]]");
});

test("crear con espacio al principio inserta el título real del servidor", () => {
  const fragment = "[[  Mi nota";
  const real = resolveCreatedLinkTitle(fragment, "  Mi nota", "Mi nota");
  assert.equal(linkMarkup(real), "[[Mi nota]]");
});

test("el enlace se escribe con el título que devuelve el servidor, no con lo tecleado", () => {
  // El servidor normaliza "Informe: abril" -> "Informe abril".
  const fragment = "[[Informe: abril ";
  const real = resolveCreatedLinkTitle(fragment, "Informe: abril ", "Informe abril");
  assert.equal(real, "Informe abril");
  assert.equal(linkMarkup(real), "[[Informe abril]]");
});

test("si el documento ya cambió bajo el [[ no se inserta nada", () => {
  // El usuario siguió escribiendo mientras se creaba la nota: el fragmento que
  // se iba a sustituir ya no es el mismo.
  assert.equal(resolveCreatedLinkTitle("[[Mi nota y más", "Mi nota ", "Mi nota"), null);
  assert.equal(resolveCreatedLinkTitle("[[Otra cosa", "Mi nota ", "Mi nota"), null);
  assert.equal(resolveCreatedLinkTitle(null, "Mi nota ", "Mi nota"), null);
});

test("sin título del servidor no se inserta nada", () => {
  assert.equal(resolveCreatedLinkTitle("[[Mi nota ", "Mi nota ", ""), null);
  assert.equal(resolveCreatedLinkTitle("[[Mi nota ", "Mi nota ", null), null);
  assert.equal(resolveCreatedLinkTitle("[[Mi nota ", "Mi nota ", "   "), null);
});

test("un título que el formato no puede representar no es enlazable", () => {
  // `]` corta el wikilink en el primer cierre: [[Notas [2026]]] se lee como
  // [[Notas [2026. Por eso la lista no debe ofrecerlo (entry.js lo filtra).
  assert.equal(isLinkableTitle("Notas [2026]"), false);
  // `|` convierte lo que sigue en alias: [[a|b]] enlaza con "a".
  assert.equal(isLinkableTitle("A|B"), false);
  // Un `[` suelto sí resuelve: el corte es el cierre.
  assert.equal(isLinkableTitle("Notas [2026"), true);
  assert.equal(isLinkableTitle("Notas (2026)"), true);
  // Ni vacío ni solo espacios: no hay título que insertar.
  assert.equal(isLinkableTitle(""), false);
  assert.equal(isLinkableTitle("   "), false);
  assert.equal(isLinkableTitle(null), false);
});

test("el enlace no arrastra los espacios sobrantes del título del servidor", () => {
  // El servidor puede devolver el título con espacios de más: no se escriben.
  assert.equal(linkMarkup("  Mi nota  "), "[[Mi nota]]");
});
