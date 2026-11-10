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

import { minNewlines, moveBlockEdits } from "../block-move.js";
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

/** Saltos que hay en un trozo de documento (los huecos son saltos y espacios). */
function countNewlines(s) {
  let n = 0;
  for (const ch of s) if (ch === "\n") n++;
  return n;
}

/**
 * Saltos MÍNIMOS que pide el PARSER para una pareja concreta de bloques, sin
 * tabla propia: se prueban uno, dos y tres saltos y se queda con el primero con
 * el que siguen saliendo dos bloques con su mismo texto. Así, cuando el motor
 * rechaza un movimiento, se puede comprobar si de verdad no cabía, tanto si
 * pidió de más como si pidió de menos.
 */
function parserMin(textA, textB) {
  for (const g of [1, 2, 3]) {
    const doc = textA + "\n".repeat(g) + textB;
    const blocks = parseBlocks(doc);
    if (
      blocks.length === 2 &&
      doc.slice(blocks[0].from, blocks[0].to) === textA &&
      doc.slice(blocks[1].from, blocks[1].to) === textB
    ) {
      return g;
    }
  }
  return 4; // ni con tres se separan: no debería pasar
}

const KINDS = {
  p: (k) => `parrafo ${k}`,
  h: (k) => `# titulo ${k}`,
  item: (k) => `- item ${k}`,
  code: (k) => `\`\`\`\ncodigo ${k}\n\`\`\``,
  quote: (k) => `> cita ${k}`,
  html: (k) => `<div>bloque ${k}</div>`,
  table: (k) => `| a | b |\n| --- | --- |\n| ${k} | x |`,
};

// Tres repertorios para que salgan documentos distintos: el primero repite
// párrafos con una valla en medio, el segundo mezcla cabecera, cita e ítem y el
// tercero lleva DOS citas seguidas y un bloque HTML, que son las parejas donde
// el motor fusionaba bloques (dos citas con un salto son una sola cita, y un
// bloque HTML se come la línea de debajo).
const VARIANTS = [
  ["p", "p", "item", "p", "code", "p"],
  ["p", "h", "item", "quote", "p", "code"],
  ["p", "quote", "quote", "html", "item", "p"],
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
        // Un rechazo tiene que estar justificado: ni con el mínimo que pide el
        // PARSER en cada frontera (medido aquí, no copiado del motor) caben los
        // saltos que hay. Si el motor pidiera de más, el movimiento se quedaría
        // sin hacer aunque hubiera sitio -- eso cuenta como rechazo injusto.
        const ord = expectedOrder(n, index, slot);
        let need = 0;
        for (let q = 0; q + 1 < n; q++) need += parserMin(texts[ord[q]], texts[ord[q + 1]]);
        let have = 0;
        for (let k = 0; k + 1 < n; k++) have += countNewlines(text.slice(blocks[k].to, blocks[k + 1].from));
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

// Parejas para medir la tabla de mínimos: un representante de cada cosa que se
// comporta distinto al pegarse a su vecina (dos citas, un `<div>` que se come la
// línea de abajo, `***` que corta y `---` que es setext, una tabla, una
// referencia, una cabecera setext, una valla...). Los textos son los que da el
// parser (`blocksFromTree`), no una versión «bonita» de ellos.
const PAIR_SAMPLES = {
  parrafo: "parrafo",
  "parrafo de dos lineas": "parrafo\ncon dos lineas",
  cabecera: "# titulo",
  "cabecera h2": "## titulo",
  "---": "---",
  "***": "***",
  valla: "```\ncodigo\n```",
  cita: "> cita",
  "cita de dos lineas": "> cita\n> seguida",
  "cita con lista": "> - a\n> - b",
  item: "- item",
  "item con continuacion": "- item\n  con continuacion",
  "item anidado": "- item\n  - anidado",
  numerado: "1. numero",
  tabla: "| a | b |\n| --- | --- |\n| 1 | 2 |",
  referencia: "[ref]: http://x",
  html: "<div>x</div>",
  "html cerrado": "<pre>\nx\n</pre>",
  setext: "titulo\n---",
  // Una cabecera setext puede tener VARIAS líneas de texto encima: su
  // subrayado es la última, no la segunda (con la segunda se tomaba por tabla
  // y se le daba el mínimo de una tabla).
  "setext de dos lineas": "titulo uno\ntitulo dos\n---",
};

test("la tabla de mínimos del motor es la del parser, pareja por pareja y por los dos lados", (t) => {
  // El mínimo no es «lo de abajo interrumpe o no»: depende de la PAREJA. Aquí se
  // compara, para cada pareja del repertorio, lo que dice `minNewlines` con lo
  // que hace el parser de verdad, y en los dos sentidos: un «2» de más (un
  // movimiento que se queda sin hacer sin motivo) y un «1» de menos (dos
  // bloques que se pegan) salen los dos como fallo.
  let pairs = 0;
  let ones = 0;
  let twos = 0;
  for (const [nameA, a] of Object.entries(PAIR_SAMPLES)) {
    for (const [nameB, b] of Object.entries(PAIR_SAMPLES)) {
      const doc = `${a}\n\n${b}`;
      const blocks = parseBlocks(doc);
      const where = `«${a}» + «${b}» (${nameA} + ${nameB})`;
      // Con dos saltos siempre tienen que salir dos bloques: es la frontera de
      // siempre, la que pone `separatorNewlines` cuando rehace un hueco.
      assert.equal(blocks.length, 2, `con dos saltos no se separan: ${where}`);
      const real = parserMin(a, b);
      assert.equal(minNewlines(blocks[0], blocks[1], doc), real, `el mínimo no es el del parser: ${where}`);
      pairs++;
      if (real === 1) ones++;
      else twos++;
    }
  }
  assert.ok(pairs >= 300, `parejas probadas: ${pairs}`);
  assert.ok(ones > 100 && twos > 20, `parejas de un salto: ${ones}, de dos: ${twos}`);
  t.diagnostic(`parejas: ${pairs} (un salto: ${ones}, dos: ${twos})`);
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

// ---------------------------------------------------------------- fronteras
// Los tres casos de la deuda de `t_f01cd7d6`, con el parser de verdad: lo que
// se comprueba no es una tabla, es que el `.md` resultante sigue teniendo los
// mismos bloques (y las mismas líneas) que el de partida.

function assertMoveKeepsBlocks(text, index, slot) {
  const blocks = parseBlocks(text);
  const where = `«${text}» moviendo el bloque ${index} a ${slot}`;
  const edit = moveBlockEdits({ text, blocks, index, slot });
  if (!edit) return null;
  const out = apply(text, edit.changes);
  assert.equal(parseBlocks(out).length, blocks.length, `el número de bloques cambia: ${where} -> «${out}»`);
  assert.deepEqual(sortedLines(out), sortedLines(text), `las líneas cambian: ${where} -> «${out}»`);
  return out;
}

test("dos citas con un `---` en medio: mover no las pega", () => {
  // El caso que destapó la deuda: con un `---` en medio, bajar la primera cita
  // la dejaba pegada a la otra (`---\n> cita uno\n> cita uno`), que el parser
  // lee como UNA cita: 3 bloques pasaban a 2 sin perder ni una línea.
  const text = "> cita uno\n---\n> cita uno";
  const blocks = parseBlocks(text);
  assert.deepEqual(blocks.map((b) => b.type), ["quote", "hr", "quote"]);
  // Tal como está (un salto por frontera, dos saltos en total) no hay sitio
  // para reordenar sin pegar las citas: se rechaza, que es lo correcto.
  assert.equal(moveBlockEdits({ text, blocks, index: 0, slot: 2 }), null);
  assert.equal(moveBlockEdits({ text, blocks, index: 2, slot: 0 }), null);
  // Y en todas las demás direcciones, o no se mueve o se mueven los tres.
  for (let index = 0; index < 3; index++) {
    for (let slot = 0; slot <= 3; slot++) assertMoveKeepsBlocks(text, index, slot);
  }
  // Con la línea en blanco que pide la pareja de citas sí se mueve, y las dos
  // citas siguen siendo dos.
  const roomy = "> cita uno\n\n---\n\n> cita uno";
  assert.equal(parseBlocks(roomy).length, 3);
  const out = assertMoveKeepsBlocks(roomy, 0, 2);
  assert.ok(out, "con sitio de sobra el movimiento se hace");
  assert.equal(parseBlocks(out).length, 3);
});

test("un bloque HTML no se come el bloque de debajo al mover (cabecera, ítem, valla, cita, tabla)", () => {
  // Un bloque HTML se come las líneas de debajo hasta la línea en blanco: con un
  // solo salto detrás de él, el bloque de abajo desaparecía (el `.md` quedaba
  // con las mismas líneas, pero con un bloque menos).
  for (const next of ["# titulo", "- item", "```\nx\n```", "> cita", "| a |\n| - |"]) {
    const docs = [
      `<div>x</div>\n\n${next}\n\nparrafo`, // el HTML arriba
      `parrafo\n<div>x</div>\n\n${next}`, // el HTML en medio, pegado al de arriba
    ];
    for (const text of docs) {
      const blocks = parseBlocks(text);
      assert.equal(blocks.length, 3, `el documento de partida es de 3 bloques: «${text}» -> ${blocks.length}`);
      for (let index = 0; index < 3; index++) {
        for (let slot = 0; slot <= 3; slot++) assertMoveKeepsBlocks(text, index, slot);
      }
    }
  }
  // El caso exacto que perdía el bloque: bajar el párrafo dejaba el HTML y la
  // cabecera con un solo salto en medio.
  const text = "parrafo\n<div>x</div>\n\n# titulo";
  const out = assertMoveKeepsBlocks(text, 0, 3);
  assert.ok(out, "el movimiento cabe (el `---`/HTML de arriba deja aire de sobra)");
  assert.equal(parseBlocks(out).length, 3);
  assert.ok(out.startsWith("<div>x</div>\n\n# titulo"), `el HTML sigue separado de la cabecera: «${out}»`);
});

test("detrás de un ítem/cabecera/valla/cita, un `---` no pide línea en blanco", () => {
  // La tabla vieja pedía dos saltos delante de un `---` siempre, y con eso
  // rechazaba movimientos que sí cabían (`- vineta\n---` es un ítem y una línea
  // horizontal, no una cabecera setext).
  const text = "- vineta\n---\nparrafo A\n\nparrafo B";
  const blocks = parseBlocks(text);
  assert.deepEqual(blocks.map((b) => b.type), ["bullet_list_item", "hr", "paragraph", "paragraph"]);
  const edit = moveBlockEdits({ text, blocks, index: 3, slot: 0 });
  assert.ok(edit, "el movimiento cabe: el `---` detrás del ítem solo necesita un salto");
  const out = apply(text, edit.changes);
  assert.equal(out, "parrafo B\n\n- vineta\n---\nparrafo A");
  assert.equal(parseBlocks(out).length, 4);
});
