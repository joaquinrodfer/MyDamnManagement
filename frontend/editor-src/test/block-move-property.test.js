// Propiedad: mover un bloque NO cambia las líneas del `.md`, solo su orden.
//
// El caso que la destapó: `alfa\n\nbeta\n\n\n\ngamma` + Alt+↑ sobre `beta`
// devolvía `beta\n\nalfa\n\ngamma` -- el hueco de cuatro saltos que había entre
// `beta` y `gamma` se tiraba porque el par origen/destino se reconstruía con el
// separador de siempre. Taparlo caso por caso no vale: el invariante es del
// documento entero, así que se comprueba con un `for` sobre documentos de
// verdad (los parsea `markdownLanguage.parser`, el mismo lenguaje que la app, y
// los bloques salen de `blocksFromTree`, la misma función que usa entry.js).
//
// Para cada documento generado y cada movimiento posible (cada bloque a cada
// posición) se comprueba, sobre el `.md` resultante:
//
//   1. el multiconjunto de LÍNEAS es el mismo (mover no gana ni pierde aire);
//   2. el número de bloques es el mismo y no cambia el texto de ninguno: los
//      mismos bloques, en otro orden;
//   3. el bloque movido cae donde se le ha pedido.
//
// Los tres caminos de la app (arrastrar el asa, «Mover arriba/abajo» del menú
// y Alt+↑ / Alt+↓) acaban todos en `moveBlockEdits` con un `slot`: el barrido
// prueba TODOS los `slot` de TODOS los bloques, que es más de lo que pueden
// producir los tres juntos.
//
// La generación es determinista (sin azar): longitudes de 2 a 6 bloques y
// huecos de 1, 2 y 4 saltos, con valla de código, cabecera y cita en medio. Si
// un hueco de un salto pega dos bloques, el documento simplemente sale con
// menos bloques: se prueba el que de verdad ha salido del parser.

import test from "node:test";
import assert from "node:assert/strict";
import { markdownLanguage } from "@codemirror/lang-markdown";

import { moveBlockEdits } from "../block-move.js";
import { blocksFromTree } from "../block-ranges.js";

function parseBlocks(text) {
  return blocksFromTree(markdownLanguage.parser.parse(text).topNode);
}

function apply(text, changes) {
  const ordered = [...changes].sort((a, b) => a.from - b.from || a.to - b.to);
  let out = "";
  let at = 0;
  for (const c of ordered) {
    assert.ok(c.from >= at, `cambios solapados: ${JSON.stringify(ordered)}`);
    out += text.slice(at, c.from) + (c.insert || "");
    at = c.to;
  }
  return out + text.slice(at);
}

/** Las líneas, sin importar el orden (mismo multiconjunto = mismo `.md`). */
const sortedLines = (s) => s.split("\n").sort();

/** Texto de cada bloque, en orden. */
const blockTexts = (text, blocks) => blocks.map((b) => text.slice(b.from, b.to));

/** El orden que debe salir: fuera el bloque `index`, dentro en `slot`. */
function expectedOrder(n, index, slot) {
  const order = [];
  for (let k = 0; k < n; k++) if (k !== index) order.push(k);
  order.splice(slot > index ? slot - 1 : slot, 0, index);
  return order;
}

// Tabla de mínimos escrita APARTE del motor (block-move.js tiene la suya): un
// salto basta cuando lo que empieza es algo que siempre corta el bloque de
// arriba (un ítem con su marcador, una cabecera, una valla, una cita); en el
// resto hace falta la línea en blanco. El test de más abajo comprueba que cada
// «1» de esta tabla se sostiene con el parser de verdad.
const ONE_NEWLINE_ENOUGH = new Set([
  "bullet_list_item",
  "ordered_list_item",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "code",
  "quote",
]);

const neededNewlines = (prev, next) => (ONE_NEWLINE_ENOUGH.has(next.type) ? 1 : 2);

const KINDS = {
  p: (k) => `parrafo ${k}`,
  h: (k) => `# titulo ${k}`,
  item: (k) => `- item ${k}`,
  code: (k) => `\`\`\`\ncodigo ${k}\n\`\`\``,
  quote: (k) => `> cita ${k}`,
};

// Dos repertorios para que salgan documentos distintos: el primero repite
// párrafos con una valla en medio, el segundo mezcla cabecera, cita e ítem.
const VARIANTS = [
  ["p", "p", "item", "p", "code", "p"],
  ["p", "h", "item", "quote", "p", "code"],
];

/** Todos los repartos de {1, 2, 4} saltos en `n - 1` fronteras. */
function gapPatterns(n) {
  const out = [];
  const acc = [];
  const walk = () => {
    if (acc.length === n - 1) {
      out.push(acc.slice());
      return;
    }
    for (const g of [1, 2, 4]) {
      acc.push(g);
      walk();
      acc.pop();
    }
  };
  walk();
  return out;
}

function buildDoc(gaps, kinds) {
  let text = "";
  for (let i = 0; i < gaps.length + 1; i++) {
    if (i) text += "\n".repeat(gaps[i - 1]);
    text += KINDS[kinds[i % kinds.length]](i);
  }
  return text;
}

/** Pasa el invariante por un documento: lo mueve todo a todas partes. */
function checkDocument(text, stats) {
  const blocks = parseBlocks(text);
  if (blocks.length < 2) return;
  const n = blocks.length;
  const texts = blockTexts(text, blocks);
  stats.docs++;
  stats.maxBlocks = Math.max(stats.maxBlocks, n);
  if (blocks.some((b) => b.type === "code")) stats.withCode++;
  if (blocks.some((b) => b.type === "h1" || b.type === "h2" || b.type === "h3")) stats.withHeading++;

  for (let index = 0; index < n; index++) {
    for (let slot = 0; slot <= n; slot++) {
      const where = `«${text}» moviendo el bloque ${index} a ${slot}`;
      const edit = moveBlockEdits({ text, blocks, index, slot });
      if (slot === index || slot === index + 1) {
        assert.equal(edit, null, `mover a donde ya está no es un movimiento: ${where}`);
        continue;
      }
      if (!edit) {
        stats.refused++;
        // Un rechazo tiene que estar justificado: ni con el mínimo en todas
        // las fronteras caben los saltos que hay. (Tabla de mínimos aparte.)
        const ord = expectedOrder(n, index, slot);
        let need = 0;
        for (let q = 0; q + 1 < n; q++) need += neededNewlines(blocks[ord[q]], blocks[ord[q + 1]]);
        let have = 0;
        for (let k = 0; k + 1 < n; k++) have += blocks[k + 1].from - blocks[k].to;
        if (need <= have) stats.refusedWrong++;
        continue;
      }
      stats.moves++;
      assert.equal(edit.changes.length, 1, `un solo cambio de texto: ${where}`);
      const out = apply(text, edit.changes);

      assert.deepEqual(sortedLines(out), sortedLines(text), `las líneas cambian: ${where}`);
      const after = parseBlocks(out);
      assert.equal(after.length, n, `el número de bloques cambia: ${where}`);
      assert.deepEqual(
        blockTexts(out, after).slice().sort(),
        texts.slice().sort(),
        `algún bloque cambia de texto: ${where}`
      );
      assert.deepEqual(
        blockTexts(out, after),
        expectedOrder(n, index, slot).map((k) => texts[k]),
        `el orden no es el pedido: ${where}`
      );
    }
  }
}

test("la tabla de mínimos sale del parser: un salto solo vale si los bloques siguen siendo dos", () => {
  // Si `minNewlines` dijera «uno basta» donde el parser pega los bloques, el
  // motor los juntaría y el `.md` perdería un bloque sin perder líneas. Aquí
  // se comprueba, con el parser de verdad, cada pareja a la que se le permite
  // un solo salto.
  const sources = {
    paragraph: "parrafo",
    h1: "# titulo",
    bullet_list_item: "- item",
    ordered_list_item: "1. numero",
    code: "```\nx\n```",
    quote: "> cita",
    hr: "---",
  };
  for (const [prevType, prev] of Object.entries(sources)) {
    for (const [nextType, next] of Object.entries(sources)) {
      // Dos citas seguidas son UNA cita (un Blockquote con dos párrafos), así
      // que esa pareja nunca llega a ser «dos bloques vecinos»: la tabla no se
      // aplica y no hay nada que comprobar.
      if (prevType === "quote" && nextType === "quote") continue;
      const doc = `${prev}\n${next}`;
      const blocks = parseBlocks(doc);
      const where = `«${doc}» (${prevType} + ${nextType})`;
      if (neededNewlines({ type: prevType }, { type: nextType }) === 1) {
        assert.equal(blocks.length, 2, `con un salto bastaría, pero salen ${blocks.length} bloques: ${where}`);
      } else {
        // La tabla puede ser conservadora (pedir dos donde uno vale), pero no
        // al revés: lo que pide dos es porque con uno se pegan... o porque un
        // `---` detrás de un párrafo es una cabecera setext y hay que dejarlo.
        assert.ok(blocks.length >= 1, where);
      }
      // Y con dos saltos (la frontera de siempre) siempre son dos bloques.
      assert.equal(parseBlocks(`${prev}\n\n${next}`).length, 2, `con dos saltos no se separan: ${where}`);
    }
  }
});

test("propiedad: mover no cambia las líneas, solo el orden de los bloques (2 a 6 bloques, huecos de 1, 2 y 4)", (t) => {
  const stats = { docs: 0, moves: 0, refused: 0, refusedWrong: 0, maxBlocks: 0, withCode: 0, withHeading: 0 };
  for (let n = 2; n <= 6; n++) {
    const patterns = gapPatterns(n);
    // Con 6 bloques hay 243 repartos y probarlos todos no aporta: se toma uno
    // de cada tres. Con 2, 3 y 4 van todos.
    const step = n >= 6 ? 3 : 1;
    for (let p = 0; p < patterns.length; p += step) {
      for (const kinds of VARIANTS) checkDocument(buildDoc(patterns[p], kinds), stats);
    }
  }
  // Que el corpus sea el que dice ser (si la generación se queda corta, la
  // prueba pasaría sin haber tocado nada interesante).
  assert.ok(stats.docs >= 150, `documentos probados: ${stats.docs}`);
  assert.equal(stats.maxBlocks, 6, `bloques del documento más largo: ${stats.maxBlocks}`);
  assert.ok(stats.withCode > 20, `documentos con valla: ${stats.withCode}`);
  assert.ok(stats.withHeading > 20, `documentos con cabecera: ${stats.withHeading}`);
  // Un movimiento solo se rechaza cuando de verdad no cabe -- cuando hasta el
  // mínimo de cada frontera suma más saltos de los que hay en el documento.
  // Si se rechazara alguno con sitio, sería un fallo del motor.
  assert.equal(stats.refusedWrong, 0, `movimientos rechazados con sitio de sobra: ${stats.refusedWrong}`);
  t.diagnostic(
    `documentos: ${stats.docs}, movimientos: ${stats.moves}, ` +
      `rechazados por no caber: ${stats.refused}, con valla: ${stats.withCode}`
  );
});

// Una lista NUMERADA sí cambia de texto a propósito: se reenumera desde el
// número con el que empezaba su lista. Aquí se comprueba lo que tiene que
// seguir siendo verdad: mismas líneas salvo el número, y ninguna tirada de
// ítems numerados con los números salidos de orden.
const skeleton = (line) => line.replace(/^[ \t]*\d+[.)][ \t]+/, "");

function orderedRunsOk(text, blocks) {
  const numbers = [];
  let last = null;
  for (const b of blocks) {
    if (b.type !== "ordered_list_item") {
      last = null;
      continue;
    }
    const n = Number((/^[ \t]*(\d+)/.exec(text.slice(b.from, b.to)) || [])[1]);
    assert.ok(Number.isInteger(n), `un ítem numerado sin número: «${text.slice(b.from, b.to)}»`);
    if (last !== null) {
      assert.equal(n, last + 1, `tirada numerada desordenada en «${text}»: ${numbers.concat(n).join(",")}`);
    }
    numbers.push(n);
    last = n;
  }
}

test("propiedad: con listas numeradas, mover conserva las líneas (quitando el número) y numera seguido", (t) => {
  const stats = { docs: 0, moves: 0, refused: 0, maxBlocks: 0, withCode: 0, withHeading: 0 };
  const kinds = ["num", "num", "p", "num", "code"];
  const withNumbered = {
    ...KINDS,
    // Numeración de verdad: cada tirada de ítems va 1, 2, 3... (si no, el
    // documento de partida ya tendría los números mal y no se probaría nada).
    num: (k, counter) => `${counter}. numero ${k}`,
  };
  const makeText = (n, gaps) => {
    let out = "";
    let counter = 0;
    for (let i = 0; i < n; i++) {
      if (i) out += "\n".repeat(gaps[i - 1]);
      const kind = kinds[i % kinds.length];
      counter = kind === "num" ? counter + 1 : 0;
      out += withNumbered[kind](i, counter);
    }
    return out;
  };
  for (let n = 2; n <= 5; n++) {
    const patterns = gapPatterns(n);
    const step = n >= 5 ? 4 : 1;
    for (let p = 0; p < patterns.length; p += step) {
      const text = makeText(n, patterns[p]);
      const blocks = parseBlocks(text);
      if (blocks.length < 2) continue;
      stats.docs++;
      stats.maxBlocks = Math.max(stats.maxBlocks, blocks.length);
      if (blocks.some((b) => b.type === "code")) stats.withCode++;
      // Un párrafo pegado a un ítem (un solo salto) acaba DENTRO del ítem, así
      // que la tirada puede salir 1, 2, 1 ya de partida: ahí no hay nada que
      // exigirle al motor. Se comprueba la numeración solo en los documentos
      // que ya venían bien numerados.
      let healthy = true;
      try {
        orderedRunsOk(text, blocks);
      } catch {
        healthy = false;
      }
      for (let index = 0; index < blocks.length; index++) {
        for (let slot = 0; slot <= blocks.length; slot++) {
          const edit = moveBlockEdits({ text, blocks, index, slot });
          if (!edit) {
            if (slot !== index && slot !== index + 1) stats.refused++;
            continue;
          }
          stats.moves++;
          const out = apply(text, edit.changes);
          assert.deepEqual(
            out.split("\n").map(skeleton).sort(),
            text.split("\n").map(skeleton).sort(),
            `se pierden o cambian líneas moviendo el bloque ${index} a ${slot} de «${text}»`
          );
          const after = parseBlocks(out);
          assert.equal(after.length, blocks.length, `cambia el número de bloques en «${text}»`);
          if (healthy) orderedRunsOk(out, after);
        }
      }
    }
  }
  assert.ok(stats.docs >= 20, `documentos con listas numeradas: ${stats.docs}`);
  assert.ok(stats.moves > 200, `movimientos con listas numeradas: ${stats.moves}`);
  t.diagnostic(`documentos con listas numeradas: ${stats.docs}, movimientos: ${stats.moves}, rechazados: ${stats.refused}`);
});
