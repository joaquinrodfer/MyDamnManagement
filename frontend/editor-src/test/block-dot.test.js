// El punto de «escribir un bloque debajo» (ver blockDotPlugin en entry.js):
// qué bloque le toca a una posición, cuándo el puntero está de verdad DENTRO del
// bloque, y que el clic lleve a `insertBlockAfter` el MISMO índice que pinta el
// widget. Hasta ahora esto solo se había comprobado a mano en la app, así que
// se habría roto sin que la suite avisara.
//
// Las posiciones se prueban con los bloques de VERDAD (el parser de
// markdownLanguage + blocksFromTree), no con rangos inventados: los bordes que
// importan son los del parser -- el `to` de un encabezado o de un ítem incluye
// la posición de su salto de línea, y la línea en blanco que va detrás ya no es
// de nadie -- y una copia a mano escondería justo el fallo que se busca.

import test from "node:test";
import assert from "node:assert/strict";

import { markdownLanguage } from "@codemirror/lang-markdown";

import {
  BlockDotWidget,
  blockDotWidget,
  blockIndexAtPos,
  dotBlockIndex,
  dotTextFamily,
  headingLevelAtLineStart,
  pointerInsideBlock,
  pressBlockDot,
} from "../entry.js";
import { blocksFromTree } from "../block-ranges.js";

function blocksOf(text) {
  return blocksFromTree(markdownLanguage.parser.parse(text).topNode, text);
}

/** `document` mínimo: el widget solo crea spans con clase, dataset y title. */
function fakeDocument() {
  return {
    createElement(tag) {
      return {
        tagName: String(tag).toUpperCase(),
        className: "",
        title: "",
        dataset: {},
        children: [],
        append(...kids) {
          this.children.push(...kids);
        },
      };
    },
  };
}

function withoutDocument(fn) {
  const prev = globalThis.document;
  globalThis.document = fakeDocument();
  try {
    return fn();
  } finally {
    globalThis.document = prev;
  }
}

// ------------------------------------------------------- índice de la posición
// (a) Un hueco no es de nadie: ahí no hay punto.

test("una posición en el hueco entre dos bloques no es de ningún bloque", () => {
  const text = "alfa\n\nbeta";
  const blocks = blocksOf(text);
  assert.equal(blocks.length, 2);
  // 5 es el principio de la línea en blanco: no hay bloque que la contenga.
  assert.equal(blockIndexAtPos(blocks, 5), -1);
  // Sin coordenada (posAtCoords devolvió null) tampoco.
  assert.equal(blockIndexAtPos(blocks, null), -1);
  assert.equal(blockIndexAtPos([], 0), -1);
});

// (b) Primera y última posición de un bloque: su índice, con los bordes del
// parser (el `to` se lleva el salto de línea; el hueco empieza después).

test("la primera y la última posición de un bloque son del bloque", () => {
  const text = "alfa\n\nbeta";
  const blocks = blocksOf(text);
  const [first, second] = blocks;
  assert.equal(blockIndexAtPos(blocks, first.from), 0);
  assert.equal(blockIndexAtPos(blocks, first.to), 0);
  // El final del texto también: el cursor pegado al último carácter sigue
  // estando en su bloque (si no, el punto desaparecía en el último bloque).
  assert.equal(blockIndexAtPos(blocks, second.to), 1);
  assert.equal(blockIndexAtPos(blocks, second.to + 1), -1);
});

test("en un encabezado, la posición de su salto de línea aún es suya", () => {
  const text = "# Titulo\n\nparrafo";
  const blocks = blocksOf(text);
  assert.equal(blocks[0].type, "h1");
  assert.equal(blockIndexAtPos(blocks, blocks[0].from), 0);
  assert.equal(blockIndexAtPos(blocks, blocks[0].to), 0);
  assert.equal(blockIndexAtPos(blocks, blocks[0].to + 1), -1); // ya la línea en blanco
  assert.equal(blockIndexAtPos(blocks, blocks[1].from), 1);
});

test("en una lista, el salto de línea es del ítem y el hueco empieza después", () => {
  const text = "- uno\n\n- dos";
  const blocks = blocksOf(text);
  assert.equal(blocks.length, 2);
  assert.equal(blockIndexAtPos(blocks, blocks[0].to), 0); // el \n que cierra el ítem
  assert.equal(blockIndexAtPos(blocks, blocks[0].to + 1), -1); // la línea en blanco
  assert.equal(blockIndexAtPos(blocks, blocks[1].from), 1);
});

// (c) Un bloque multilínea devuelve su índice en TODAS sus líneas: el punto va
// en la primera, pero el bloque sigue siendo el mismo en las de abajo.

test("un bloque de varias líneas es el mismo bloque en todas ellas", () => {
  const text = "> uno\n> dos\n> tres";
  const blocks = blocksOf(text);
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].type, "quote");
  for (const pos of [0, text.indexOf("> dos"), text.indexOf("> tres"), text.length]) {
    assert.equal(blockIndexAtPos(blocks, pos), 0);
  }
});

test("una valla de código con líneas dentro sigue siendo un solo bloque", () => {
  const text = "```\nalfa\n\nbeta\n```";
  const blocks = blocksOf(text);
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].type, "code");
  // La línea en blanco de dentro es del bloque de código, no un hueco.
  for (let pos = 0; pos <= text.length; pos++) assert.equal(blockIndexAtPos(blocks, pos), 0);
});

test("un párrafo partido en dos líneas es un bloque en las dos", () => {
  const text = "alfa\nbeta";
  const blocks = blocksOf(text);
  assert.equal(blocks.length, 1);
  assert.equal(blockIndexAtPos(blocks, 0), 0);
  assert.equal(blockIndexAtPos(blocks, text.indexOf("beta")), 0);
  assert.equal(blockIndexAtPos(blocks, text.length), 0);
});

// ------------------------------------------------ puntero dentro del bloque

const BLOCK = { from: 10, to: 20 };
/** `coordsAtPos` de mentira: el bloque ocupa de `top` a `bottom`. */
const coordsOf = (top, bottom) => () => ({ top, bottom });

test("el puntero cuenta como dentro con 4 px de gracia arriba y abajo", () => {
  const coords = coordsOf(100, 124);
  assert.equal(pointerInsideBlock(BLOCK, 100, coords), true);
  assert.equal(pointerInsideBlock(BLOCK, 96, coords), true); // justo los 4 px de arriba
  assert.equal(pointerInsideBlock(BLOCK, 95.9, coords), false);
  assert.equal(pointerInsideBlock(BLOCK, 128, coords), true); // justo los 4 px de abajo
  assert.equal(pointerInsideBlock(BLOCK, 128.1, coords), false);
  // Un bloque de dos líneas: la gracia va sobre el borde de ABAJO del último.
  assert.equal(pointerInsideBlock(BLOCK, 171.9, coordsOf(100, 168)), true);
  assert.equal(pointerInsideBlock(BLOCK, 172.1, coordsOf(100, 168)), false);
});

test("sin bloque, sin coordenadas o con una sola línea no se rompe", () => {
  assert.equal(pointerInsideBlock(null, 100, coordsOf(100, 124)), false);
  assert.equal(pointerInsideBlock(BLOCK, 100, () => null), false);
  // Si `coordsAtPos(to, 1)` no da nada se usa el top: vale el alto de una línea.
  const onlyTop = (pos) => (pos === BLOCK.from ? { top: 100, bottom: 124 } : null);
  assert.equal(pointerInsideBlock(BLOCK, 127, onlyTop), true);
  assert.equal(pointerInsideBlock(BLOCK, 129, onlyTop), false);
});

// ------------------------------------------------------------- el widget
// Lo que el CSS necesita del widget: el índice que se pulsa (`data-block-index`)
// y la familia del bloque, que es la que pone el cuerpo del texto con el que se
// centra el punto (`cm-mdm-dot-hN` -> --text-hN en index.html).

test("el widget pinta el índice del bloque que le toca", () => {
  const wrap = withoutDocument(() => new BlockDotWidget(3, "paragraph").toDOM());
  assert.equal(wrap.className, "cm-mdm-block-dot-wrap");
  assert.equal(wrap.children.length, 1);
  const dot = wrap.children[0];
  assert.equal(dot.className, "cm-mdm-block-dot");
  assert.equal(dot.dataset.blockIndex, "3");
  assert.equal(dot.title, "Escribir un bloque debajo");
});

test("el punto de un encabezado lleva el cuerpo del encabezado; el del texto, no", () => {
  const cls = (family) => withoutDocument(() => new BlockDotWidget(0, family).toDOM()).className;
  for (const level of ["h1", "h2", "h3", "h4", "h5"]) {
    assert.equal(cls(level), `cm-mdm-block-dot-wrap cm-mdm-dot-${level}`);
  }
  // Sin familia (texto normal: párrafo, ítem, código, cita) no hay clase y el
  // CSS usa el 1em de la línea, que es el cuerpo del texto.
  for (const family of ["", undefined, null]) {
    assert.equal(cls(family), "cm-mdm-block-dot-wrap");
  }
});

test("dos puntos del mismo bloque y familia son el mismo widget (no se repinta)", () => {
  assert.ok(new BlockDotWidget(2, "h1").eq(new BlockDotWidget(2, "h1")));
  assert.ok(!new BlockDotWidget(2, "h1").eq(new BlockDotWidget(3, "h1")));
  // Cambia el cuerpo del texto (un párrafo se convierte en encabezado): el
  // widget NO es el mismo, o el DOM se quedaría con la clase vieja.
  assert.ok(!new BlockDotWidget(2, "h1").eq(new BlockDotWidget(2, "")));
});

// ------------------- el cuerpo del punto sale del TEXTO de la primera línea
// El `type` del bloque no basta: `> # Titulo` es un `quote`, `- # Titulo` es un
// `bullet_list_item` y un setext es `other`, y los tres se pintan con el cuerpo
// de un H1. Decidirlo por el tipo dejaba el punto 3 px por debajo del texto.

/** Clase que le pone el punto al bloque `index` de `text` (con el parser y los
 * bloques de verdad, y el mismo cableado que `dotDecorations`: `blockDotWidget`). */
function dotClassOf(text, index = 0) {
  const tree = markdownLanguage.parser.parse(text);
  const blocks = blocksFromTree(tree.topNode, text);
  assert.ok(blocks[index], `no hay bloque ${index} en ${JSON.stringify(text)}`);
  const newline = text.indexOf("\n", blocks[index].from);
  const firstLineEnd = newline < 0 ? text.length : newline;
  const widget = blockDotWidget(tree, blocks, index, firstLineEnd);
  return withoutDocument(() => widget.toDOM()).className;
}

test("un encabezado dentro de una cita o de un ítem de lista se lleva su cuerpo", () => {
  // Los tres casos que la revisión encontró desalineados (3,0 px por debajo).
  const quote = "> # Titulo en cita";
  assert.equal(blocksOf(quote)[0].type, "quote");
  assert.equal(dotClassOf(quote), "cm-mdm-block-dot-wrap cm-mdm-dot-h1");

  const item = "- # Titulo en lista";
  assert.equal(blocksOf(item)[0].type, "bullet_list_item");
  assert.equal(dotClassOf(item), "cm-mdm-block-dot-wrap cm-mdm-dot-h1");

  const setext = "Titulo setext\n============";
  assert.equal(blocksOf(setext)[0].type, "other"); // blocksFromTree no tiene case para SetextHeading
  assert.equal(dotClassOf(setext), "cm-mdm-block-dot-wrap cm-mdm-dot-h1");
  // El setext de nivel 2 (`---`) también.
  const setext2 = "Titulo setext\n------------";
  assert.equal(blocksOf(setext2)[0].type, "other");
  assert.equal(dotClassOf(setext2), "cm-mdm-block-dot-wrap cm-mdm-dot-h2");
});

test("el nivel sale del encabezado que abre el bloque (ATX o setext)", () => {
  for (const [text, cls] of [
    ["## H2", "h2"],
    ["### H3", "h3"],
    ["#### H4", "h4"],
    ["##### H5", "h5"],
    ["###### H6", "h5"], // el 6 se pinta con la escala del 5, como en blocksFromTree
    ["   # H1 con sangría", "h1"], // la sangría es del bloque, el encabezado empieza después
    ["- ### dentro de un ítem", "h3"],
    ["1. ## dentro de una lista numerada", "h2"],
    ["> ## dentro de una cita", "h2"],
    ["> - #### cita y lista a la vez", "h4"],
  ]) {
    assert.equal(dotClassOf(text), `cm-mdm-block-dot-wrap cm-mdm-dot-${cls}`, text);
  }
});

test("el texto normal sigue sin clase (párrafo, ítem, cita, código)", () => {
  for (const text of [
    "parrafo normal",
    "alfa\nbeta", // párrafo de dos líneas
    "- item normal",
    "1. item normal",
    "> cita normal",
    "> una cita\n> de dos lineas",
    "```\ncodigo\n```",
    "    codigo indentado",
    "---",
    "#no-es-encabezado", // sin espacio detrás de la almohadilla: no hay encabezado
    "texto con # dentro",
  ]) {
    assert.equal(dotClassOf(text), "cm-mdm-block-dot-wrap", text);
  }
});

test("un encabezado en una línea de MÁS ABAJO no cambia el cuerpo del punto", () => {
  // El punto va en la primera línea del bloque; si el encabezado está en la
  // segunda, el texto de arriba es normal.
  for (const text of ["> texto\n> # Titulo", "- texto\n  ### Titulo", "alfa\n# no puede interrumpir? no: dos bloques"]) {
    const tree = markdownLanguage.parser.parse(text);
    const blocks = blocksFromTree(tree.topNode, text);
    assert.equal(dotTextFamily(tree, blocks[0], text.indexOf("\n")), "", text);
  }
});

test("la familia se decide por bloque, con su índice (cableado de dotDecorations)", () => {
  const text = ["# H1", "parrafo", "> # en cita", "- normal", "Titulo\n===="].join("\n\n");
  const tree = markdownLanguage.parser.parse(text);
  const blocks = blocksFromTree(tree.topNode, text);
  assert.equal(blocks.map((b) => b.type).join(","), "h1,paragraph,quote,bullet_list_item,other");
  const families = blocks.map((b, i) => {
    const newline = text.indexOf("\n", b.from);
    const family = dotTextFamily(tree, b, newline < 0 ? text.length : newline);
    assert.equal(blockDotWidget(tree, blocks, i, newline < 0 ? text.length : newline).index, i);
    return family;
  });
  assert.deepEqual(families, ["h1", "", "h1", "", "h1"]);
});

test("headingLevelAtLineStart no se inventa encabezados donde no los hay", () => {
  const text = "parrafo\n\n# si";
  const tree = markdownLanguage.parser.parse(text);
  const heading = text.indexOf("# si");
  assert.equal(headingLevelAtLineStart(tree, 0, text.indexOf("\n")), 0);
  assert.equal(headingLevelAtLineStart(tree, heading, text.length), 1);
  // Una posición DESPUÉS del encabezado no lo encuentra: solo cuenta que empiece
  // en la primera línea del bloque que se le pasa.
  assert.equal(headingLevelAtLineStart(tree, heading + 1, text.length), 0);
  // Sin bloque (index fuera de rango) el widget no se construye.
  assert.equal(blockDotWidget(tree, [], 0, 7), null);
});

// ---------------------------------------------------- el clic del punto
// (d) El `data-block-index` que se pulsa es exactamente el que llega a
// `insertBlockAfter`: se lee del widget, no de la posición del cursor (el punto
// sigue al ratón y puede ser de un bloque distinto al del caret).

/** Un punto de verdad en el DOM no hace falta: basta con lo que usa el código,
 * `closest('.cm-mdm-block-dot')` y el `dataset` del widget. */
function fakeDotTarget(index) {
  const dot = { dataset: { blockIndex: String(index) }, closest: (sel) => (sel === ".cm-mdm-block-dot" ? dot : null) };
  // Un hijo DENTRO del punto (el ::after no es nodo, pero el clic puede caer en
  // cualquier cosa que el widget pinte dentro): manda el punto que lo envuelve.
  const inner = { closest: (sel) => (sel === ".cm-mdm-block-dot" ? dot : null) };
  return { dot, inner };
}

test("el clic en el punto pasa a insertBlockAfter el índice del widget pulsado", () => {
  for (const index of [0, 2, 7]) {
    const { dot, inner } = fakeDotTarget(index);
    for (const target of [dot, inner]) {
      const used = [];
      assert.equal(pressBlockDot(target, (i) => used.push(i)), index);
      assert.deepEqual(used, [index]); // el del data-block-index, ni uno más
    }
  }
});

test("un clic que no es en el punto no inserta nada", () => {
  let called = 0;
  assert.equal(pressBlockDot({ closest: () => null }, () => called++), -1);
  assert.equal(called, 0);
  assert.equal(dotBlockIndex(null), -1);
  assert.equal(dotBlockIndex({}), -1);
  assert.equal(dotBlockIndex({ closest: () => ({ dataset: { blockIndex: "no-es-un-numero" } }) }), -1);
  assert.equal(dotBlockIndex({ closest: () => ({ dataset: {} }) }), -1);
  assert.equal(dotBlockIndex({ closest: () => ({ dataset: { blockIndex: "-1" } }) }), -1);
});
