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
  return blocksFromTree(markdownLanguage.parser.parse(text).topNode, text);
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
const IMPOSSIBLE = 4;

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
  return IMPOSSIBLE; // ni con tres se separan: no hay frontera que escribir
}

const KINDS = {
  p: (k) => `parrafo ${k}`,
  h: (k) => `# titulo ${k}`,
  item: (k) => `- item ${k}`,
  code: (k) => `\`\`\`\ncodigo ${k}\n\`\`\``,
  // Código INDENTADO (4 espacios, sin valla): el bloque empieza en su LÍNEA y
  // la sangría es suya, no del hueco entre bloques (t_9d3b6d3b). Con la sangría
  // fuera del rango, mover el bloque lo dejaba en `codigo ${k}` -- ya no era
  // código -- y el de al lado pasaba a serlo.
  icode: (k) => `    codigo ${k}`,
  quote: (k) => `> cita ${k}`,
  html: (k) => `<div>bloque ${k}</div>`,
  table: (k) => `| a | b |\n| --- | --- |\n| ${k} | x |`,
};

// Cuatro repertorios para que salgan documentos distintos: el primero repite
// párrafos con una valla en medio, el segundo mezcla cabecera, cita e ítem, el
// tercero lleva DOS citas seguidas y un bloque HTML, que son las parejas donde
// el motor fusionaba bloques (dos citas con un salto son una sola cita, y un
// bloque HTML se come la línea de debajo), y el cuarto código indentado, cuya
// sangría se quedaba en el hueco.
const VARIANTS = [
  ["p", "p", "item", "p", "code", "p"],
  ["p", "h", "item", "quote", "p", "code"],
  ["p", "quote", "quote", "html", "item", "p"],
  ["p", "icode", "p", "icode", "h", "p"],
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
        let impossible = false;
        for (let q = 0; q + 1 < n; q++) {
          const m = parserMin(texts[ord[q]], texts[ord[q + 1]]);
          if (m === IMPOSSIBLE) impossible = true;
          need += m;
        }
        let have = 0;
        for (let k = 0; k + 1 < n; k++) have += countNewlines(text.slice(blocks[k].to, blocks[k + 1].from));
        // Una pareja que no se separa con NINGUNA línea en blanco justifica el
        // rechazo ella sola: no hay hueco, por grande que sea, que la separe.
        if (!impossible && need <= have) stats.refusedWrong++;
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
  "cabecera h6": "###### titulo",
  "---": "---",
  "***": "***",
  "___": "___",
  valla: "```\ncodigo\n```",
  // Código INDENTADO: su cuerpo empieza en la línea, con los cuatro espacios.
  // Sin ellos el parser no lo reconoce (`codigo` es un párrafo) y el mínimo de
  // la frontera se medía sobre un bloque que no existe.
  "codigo indentado": "    codigo",
  "codigo indentado de dos lineas": "    uno\n    dos",
  "valla ~~~": "~~~\ncodigo\n~~~",
  cita: "> cita",
  "cita de dos lineas": "> cita\n> seguida",
  "cita con lista": "> - a\n> - b",
  "cita anidada": "> > cita",
  item: "- item",
  "item con continuacion": "- item\n  con continuacion",
  "item anidado": "- item\n  - anidado",
  numerado: "1. numero",
  // El NÚMERO de una lista ordenada cambia la pareja: solo interrumpe lo de
  // arriba si empieza en 1. Con `3.` o `7)` hace falta la línea en blanco
  // detrás de un párrafo, una cita, un ítem o una tabla (sin ella el bloque de
  // arriba se come la línea y los dos bloques son uno).
  "numerado 3": "3. tercero",
  "numerado 7)": "7) numero",
  "numerado 3 con continuacion": "3. tercero\n   con continuacion",
  // Bloques VACÍOS: el cuerpo es solo el marcador. Se leen distinto que sus
  // hermanos con texto en los dos sentidos: un ítem vacío NO interrumpe un
  // párrafo (`parrafo\n- ` es UN bloque), pero detrás de él un salto basta
  // (`- \nparrafo` son DOS: no hay nada que continuar). La cita vacía `>`, lo
  // mismo. Con la tabla de tipos esto se colaba en los dos sentidos; medido del
  // parser sale solo.
  "item vacio": "- ",
  "item vacio sin espacio": "-",
  "numerado vacio": "1.",
  "numerado vacio parentesis": "1)",
  "cita vacia": ">",
  tabla: "| a | b |\n| --- | --- |\n| 1 | 2 |",
  referencia: "[ref]: http://x",
  html: "<div>x</div>",
  "html cerrado": "<pre>\nx\n</pre>",
  // Condición 7 de CommonMark (una etiqueta suelta): es un bloque HTML, pero NO
  // interrumpe un párrafo -- con un salto se lo come.
  "html condicion 7": "<x-foo>",
  setext: "titulo\n---",
  "setext =": "titulo\n===",
  // Una cabecera setext puede tener VARIAS líneas de texto encima: su
  // subrayado es la última, no la segunda (con la segunda se tomaba por tabla
  // y se le daba el mínimo de una tabla).
  "setext de dos lineas": "titulo uno\ntitulo dos\n---",
};

test("el mínimo del motor es el que mide el parser, pareja por pareja y por los dos lados", (t) => {
  // El motor ya no tiene tabla: `minNewlines` pregunta al parser. Esta prueba
  // es la red que lo fija -- se compara, para cada pareja del repertorio, lo que
  // dice `minNewlines` con una medida HECHA APARTE aquí (probar uno, dos y tres
  // saltos y mirar cuántos bloques salen), y en los dos sentidos: un «2» de más
  // (un movimiento que se queda sin hacer sin motivo) y un «1» de menos (dos
  // bloques que se pegan) salen los dos como fallo.
  let pairs = 0;
  let ones = 0;
  let twos = 0;
  let inseparables = 0;
  for (const [nameA, a] of Object.entries(PAIR_SAMPLES)) {
    for (const [nameB, b] of Object.entries(PAIR_SAMPLES)) {
      const doc = `${a}\n\n${b}`;
      const blocks = parseBlocks(doc);
      const where = `«${a}» + «${b}» (${nameA} + ${nameB})`;
      const real = parserMin(a, b);
      if (real === IMPOSSIBLE) {
        // Hay parejas que no se separan con ninguna línea en blanco: dos bloques
        // de código indentado seguidos son UNO (las líneas en blanco de en medio
        // son texto del código) y un bloque indentado detrás de un ítem de lista
        // es continuación del ítem. Con dos saltos, pues, no salen dos bloques;
        // lo que se exige es que el motor lo diga (`NO_SEPARATION`), porque de
        // eso depende de que se NIEGUE a ponerlos juntos en vez de fusionarlos.
        assert.ok(blocks.length < 2, `con dos saltos sí se separan: ${where}`);
        const solo = (s) => parseBlocks(s)[0] || { type: "?" };
        assert.equal(
          minNewlines(solo(a), solo(b), a, b),
          IMPOSSIBLE,
          `el motor no dice que no hay separación posible: ${where}`
        );
        inseparables++;
        continue;
      }
      // Con dos saltos tienen que salir dos bloques: es la frontera de siempre,
      // la que pone `separatorNewlines` cuando rehace un hueco.
      assert.equal(blocks.length, 2, `con dos saltos no se separan: ${where}`);
      assert.equal(minNewlines(blocks[0], blocks[1], a, b), real, `el mínimo no es el del parser: ${where}`);
      pairs++;
      if (real === 1) ones++;
      else twos++;
    }
  }
  // Todo el repertorio, pareja por pareja y por los dos lados: 34 x 34 = 1.156.
  // Si se añade un tipo nuevo hay que añadirlo aquí, y el mínimo del motor tiene
  // que seguir saliendo del parser para todas sus parejas.
  assert.ok(pairs >= 1156, `parejas probadas: ${pairs}`);
  assert.ok(ones > 100 && twos > 20, `parejas de un salto: ${ones}, de dos: ${twos}`);
  // Y que las inseparables del repertorio sigan ahí (código indentado con código
  // indentado, e indentado detrás de un ítem): si un día se pudieran separar, el
  // motor tendría que dejar de rechazarlas.
  assert.ok(inseparables >= 4, `parejas sin separación posible: ${inseparables}`);
  t.diagnostic(`parejas: ${pairs} (un salto: ${ones}, dos: ${twos}), sin separación: ${inseparables}`);
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

test("un ítem numerado que no empieza en 1 no interrumpe: mover no lo pega a un párrafo", () => {
  // La deuda de `t_bd8c1b31`, segunda vuelta: `3. tercero` no interrumpe un
  // párrafo (solo lo hace una lista numerada que empieza en 1), así que con un
  // solo salto el párrafo se lo come. El motor metía todos los ítems en el
  // mismo cajón, decía que un salto bastaba, y al apretar el presupuesto el
  // documento pasaba de 3 bloques a 2 (`parrafo 0\n- item 1\n\n3. tercero`
  // movido dejaba `- item 1\n\nparrafo 0\n3. tercero`).
  const text = "parrafo 0\n- item 1\n\n3. tercero";
  const blocks = parseBlocks(text);
  assert.deepEqual(blocks.map((b) => b.type), ["paragraph", "bullet_list_item", "ordered_list_item"]);
  // Bajar el párrafo lo dejaría pegado al `3.` sin sitio para la línea en
  // blanco que pide la pareja: se rechaza, no se fusiona.
  assert.equal(moveBlockEdits({ text, blocks, index: 0, slot: 2 }), null);
  // En todas las demás direcciones, o no se mueve o se mueven los tres
  // bloques con sus mismas líneas.
  for (let index = 0; index < 3; index++) {
    for (let slot = 0; slot <= 3; slot++) assertMoveKeepsBlocks(text, index, slot);
  }
  // Con el párrafo en su línea en blanco, mover sí cabe y el `3.` sigue siendo
  // un bloque aparte.
  const roomy = "parrafo 0\n\n- item 1\n\n3. tercero";
  assert.equal(parseBlocks(roomy).length, 3);
  const out = assertMoveKeepsBlocks(roomy, 0, 2);
  assert.ok(out, "con sitio de sobra el movimiento se hace");
  assert.equal(parseBlocks(out).length, 3);
  assert.ok(out.endsWith("\n\n3. tercero"), `el \`3.\` sigue separado del párrafo: «${out}»`);
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

test("un ítem de lista vacío o una cita vacía: mover no los pega a un párrafo", () => {
  // Cuarta vuelta de la misma deuda: un bloque cuyo cuerpo es SOLO el marcador
  // (`- `, `-`, `1.`, `1)`, `>`) se lee al revés que su hermano con texto, y en
  // los dos sentidos. Como `next` NO interrumpe (`parrafo\n- ` es UN párrafo: la
  // línea `- ` se lee como continuación suya), así que pide la línea en blanco;
  // como `prev`, en cambio, un salto basta (`- \nparrafo` son DOS bloques: un
  // ítem vacío no tiene nada que continuar). La tabla de tipos decía «uno» en
  // los dos sentidos: al apretar el presupuesto los pegaba.
  const empties = ["- ", "-", "1.", "1)", ">"];
  let docsWithTwo = 0;
  for (const empty of empties) {
    for (const text of [`${empty}\nparrafo`, `parrafo\n${empty}\nparrafo`, `${empty}\n\nparrafo`]) {
      const blocks = parseBlocks(text);
      // El documento de partida puede tener los bloques que tenga (si el vacío
      // se ha comido el párrafo, ya son uno y no hay nada que mover); lo que no
      // puede es PERDER bloques al mover.
      if (blocks.length >= 2) docsWithTwo++;
      const label = `«${text}» (${blocks.length} bloques)`;
      for (let index = 0; index < blocks.length; index++) {
        for (let slot = 0; slot <= blocks.length; slot++) {
          const edit = moveBlockEdits({ text, blocks, index, slot });
          if (!edit) continue;
          const out = apply(text, edit.changes);
          assert.equal(
            parseBlocks(out).length,
            blocks.length,
            `el número de bloques cambia en ${label} moviendo ${index} a ${slot}: «${out}»`
          );
          assert.deepEqual(sortedLines(out), sortedLines(text), `las líneas cambian en ${label}: «${out}»`);
        }
      }
    }
  }
  // Que el repertorio no se haya quedado en documentos de un solo bloque.
  assert.ok(docsWithTwo >= 10, `documentos con dos bloques o más: ${docsWithTwo}`);

  // El caso exacto que fusionaba: `- \nparrafo` son dos bloques y subir el
  // párrafo los dejaba en uno (`parrafo\n- `), porque el motor creía que un
  // salto bastaba delante del ítem vacío.
  const text = "- \nparrafo";
  const blocks = parseBlocks(text);
  assert.deepEqual(blocks.map((b) => b.type), ["bullet_list_item", "paragraph"]);
  assert.equal(moveBlockEdits({ text, blocks, index: 1, slot: 0 }), null);
  // Lo mismo con el numerado vacío.
  const numbered = "1.\nparrafo";
  const numberedBlocks = parseBlocks(numbered);
  assert.deepEqual(numberedBlocks.map((b) => b.type), ["ordered_list_item", "paragraph"]);
  assert.equal(moveBlockEdits({ text: numbered, blocks: numberedBlocks, index: 1, slot: 0 }), null);

  // Con la línea en blanco sí cabe, y el ítem vacío sigue siendo un bloque.
  const roomy = "- \n\nparrafo";
  assert.equal(parseBlocks(roomy).length, 2);
  const out = assertMoveKeepsBlocks(roomy, 1, 0);
  assert.ok(out, "con la línea en blanco el movimiento se hace");
  assert.equal(parseBlocks(out).length, 2);
});

test("un bloque de código indentado se mueve con su sangría y no deja de ser código", () => {
  // El `from` de un bloque indentado estaba en el CONTENIDO (`codigo 1`), no en
  // su línea: los cuatro espacios se quedaban en el HUECO y, en cuanto el hueco
  // había que rehacerlo, el bloque viajaba sin ellos -- dejaba de ser código y
  // el bloque de al lado pasaba a serlo (`"parrafo 0\n\n    codigo 1"` movido
  // daba `"codigo 1\n\n    parrafo 0"`). Tarjeta t_9d3b6d3b.
  const text = "parrafo 0\n\n    codigo 1";
  const blocks = parseBlocks(text);
  assert.deepEqual(blocks.map((b) => b.type), ["paragraph", "code"]);
  assert.equal(
    text.slice(blocks[1].from, blocks[1].to),
    "    codigo 1",
    "la sangría es del bloque, no del hueco"
  );
  const edit = moveBlockEdits({ text, blocks, index: 0, slot: 2 });
  assert.ok(edit, "el movimiento cabe");
  const out = apply(text, edit.changes);
  assert.equal(out, "    codigo 1\n\nparrafo 0");
  assert.deepEqual(parseBlocks(out).map((b) => b.type), ["code", "paragraph"]);
  assert.deepEqual(sortedLines(out), sortedLines(text));

  // Con tres bloques y un hueco de un solo salto además BAJABA el número de
  // bloques (3 -> 2): el código, ya sin sangría, y el párrafo de debajo se
  // leían como uno.
  const three = "parrafo 0\n\n    codigo 1\nparrafo 2";
  assert.equal(parseBlocks(three).length, 3);
  for (let index = 0; index < 3; index++) {
    for (let slot = 0; slot <= 3; slot++) assertMoveKeepsBlocks(three, index, slot);
  }
  const first = moveBlockEdits({ text: three, blocks: parseBlocks(three), index: 0, slot: 2 });
  assert.equal(apply(three, first.changes), "    codigo 1\nparrafo 0\n\nparrafo 2");
  assert.equal(parseBlocks(apply(three, first.changes)).length, 3);

  // El tabulador tampoco es del hueco: `\t---` es un bloque de CÓDIGO (la
  // tabulación es la sangría), no una línea horizontal, y mover no se lo lleva.
  const tabbed = "parrafo\n\n\t---";
  const tabBlocks = parseBlocks(tabbed);
  assert.deepEqual(tabBlocks.map((b) => b.type), ["paragraph", "code"]);
  const tabEdit = moveBlockEdits({ text: tabbed, blocks: tabBlocks, index: 0, slot: 2 });
  assert.ok(tabEdit, "el movimiento cabe");
  assert.equal(apply(tabbed, tabEdit.changes), "\t---\n\nparrafo");

  // De varias líneas se mueve entero, con la sangría de todas sus líneas.
  const multi = "parrafo\n\n    uno\n    dos\n\nparrafo dos";
  const multiOut = assertMoveKeepsBlocks(multi, 0, 2);
  assert.ok(multiOut, "el movimiento cabe");
  assert.ok(multiOut.startsWith("    uno\n    dos\n\n"), `el bloque se mueve entero: «${multiOut}»`);
  assert.deepEqual(parseBlocks(multiOut).map((b) => b.type), ["code", "paragraph", "paragraph"]);
});

test("dos bloques de código indentado no se ponen al lado: se fusionarían", () => {
  // Dos bloques de código indentado seguidos son UNO para el parser (las líneas
  // en blanco de en medio son contenido del código), igual que un bloque
  // indentado detrás de un ítem de lista es continuación del ítem: no hay
  // ninguna línea en blanco que los separe. El motor se NIEGA a ponerlos juntos
  // -- antes no mover que escribir un `.md` con un bloque menos.
  const text = "parrafo 0\n\n    codigo 1\n\nparrafo 2\n\n    codigo 3";
  const blocks = parseBlocks(text);
  assert.deepEqual(blocks.map((b) => b.type), ["paragraph", "code", "paragraph", "code"]);
  // Bajar el párrafo al final, o subirlo al principio, dejaría los dos bloques
  // de código seguidos: se rechaza.
  assert.equal(moveBlockEdits({ text, blocks, index: 2, slot: 4 }), null);
  assert.equal(moveBlockEdits({ text, blocks, index: 2, slot: 0 }), null);
  // Los movimientos que NO los juntan sí se hacen, con todas sus líneas.
  const out = assertMoveKeepsBlocks(text, 0, 3);
  assert.ok(out, "el movimiento cabe");
  assert.equal(out, "    codigo 1\n\nparrafo 2\n\nparrafo 0\n\n    codigo 3");
  assert.equal(parseBlocks(out).length, 4);
});
