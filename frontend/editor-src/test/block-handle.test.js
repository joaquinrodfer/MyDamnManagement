// El asa de un bloque, como en Notion: el «+» y los seis puntos de agarrar.
//
// Los seis puntos se quitaron un tiempo (t_c2c98f7b) por un malentendido: la
// «bolita» que el dueño no quería era el punto suelto del margen. Este test
// sujeta el orden (primero el «+», pegados al texto los puntos) y que todos
// lleven el índice del bloque, que es de donde lo leen el marcar, el arrastrar
// y el menú.

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

test("el asa lleva el «+» y, detrás, los seis puntos", () => {
  const wrap = withDocument(() => blockHandleDom(3));
  assert.equal(wrap.className, "cm-mdm-block-handle-wrap");
  assert.equal(wrap.children.length, 2);
  const [add, grip] = wrap.children;
  assert.equal(add.className, "cm-mdm-block-add");
  assert.equal(add.textContent, "+");
  assert.equal(grip.className, "cm-mdm-block-grip");
  assert.equal(grip.dataset.blockIndex, "3");
});

test("la banda lleva el índice del bloque (de ahí lo leen marcar y arrastrar)", () => {
  const wrap = withDocument(() => blockHandleDom(7));
  assert.equal(wrap.dataset.blockIndex, "7");
  // El «+» sigue llevando el suyo: su clic inserta el bloque de después.
  assert.equal(wrap.children[0].dataset.blockIndex, "7");
});
