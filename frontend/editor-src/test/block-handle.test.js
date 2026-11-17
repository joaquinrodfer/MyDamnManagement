// La banda del margen de un bloque: SOLO el «+» (t_c2c98f7b).
//
// El dueño pidió quitar «la bolita esa que sale a la derecha del símbolo del +»
// -- el `<span class="cm-mdm-block-handle">⋮⋮</span>`. Lo que hacía el asa se
// reparte ahora entre la propia banda (marcar y arrastrar) y el clic derecho
// sobre el bloque, así que este test sujeta las dos cosas que no pueden volver:
// la banda lleva UN control (el «+») y lleva el índice del bloque, que es de
// donde lo leen el marcar, el arrastrar y el menú (ya no hay asa con
// `data-block-index` a la que agarrarse).

import test from "node:test";
import assert from "node:assert/strict";

import { blockHandleDom } from "../entry.js";

/** `document` mínimo: aquí solo se crean spans con clase, dataset y texto. */
function fakeDocument() {
  return {
    createElement(tag) {
      return {
        tagName: String(tag).toUpperCase(),
        className: "",
        title: "",
        textContent: "",
        dataset: {},
        children: [],
        append(...kids) {
          this.children.push(...kids);
        },
        querySelector(selector) {
          const wanted = selector.replace(/^\./, "");
          for (const kid of this.children) {
            if (kid.className.split(/\s+/).includes(wanted)) return kid;
          }
          return null;
        },
      };
    },
  };
}

function withDocument(fn) {
  const prev = globalThis.document;
  globalThis.document = fakeDocument();
  try {
    return fn();
  } finally {
    globalThis.document = prev;
  }
}

test("la banda del bloque lleva un solo control: el «+»", () => {
  const wrap = withDocument(() => blockHandleDom(3));
  assert.equal(wrap.className, "cm-mdm-block-handle-wrap");
  assert.equal(wrap.children.length, 1);
  const add = wrap.children[0];
  assert.equal(add.className, "cm-mdm-block-add");
  assert.equal(add.textContent, "+");
});

test("no queda ni rastro del asa de puntos", () => {
  const wrap = withDocument(() => blockHandleDom(0));
  assert.equal(wrap.querySelector(".cm-mdm-block-handle"), null);
  const textos = wrap.children.map((kid) => kid.textContent).join("");
  assert.doesNotMatch(textos, /⋮/);
});

test("la banda lleva el índice del bloque (de ahí lo leen marcar y arrastrar)", () => {
  const wrap = withDocument(() => blockHandleDom(7));
  assert.equal(wrap.dataset.blockIndex, "7");
  // El «+» sigue llevando el suyo: su clic inserta el bloque de después.
  assert.equal(wrap.children[0].dataset.blockIndex, "7");
});
