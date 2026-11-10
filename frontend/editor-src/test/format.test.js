// Negrita / cursiva / subrayado con ⌘/Ctrl+B / I / U: la lógica pura de
// format.js, tal cual, sin navegador.
//
// Fija lo que el enunciado pedía y las decisiones que tomé donde lo dejaba
// abierto (todas escritas en la cabecera de format.js):
//   aplicar, quitar, sin selección, varias líneas (una por línea), espacios de
//   los bordes fuera de la marca, prefijo de lista/cita/título intacto, nada
//   dentro de código, y `<u>` emparejado para poder pintarlo.

import test from "node:test";
import assert from "node:assert/strict";
import { ChangeSet } from "@codemirror/state";

import { formatEdit, underlineSpans } from "../format.js";

/** Aplica la tecla al documento y devuelve texto nuevo + selección nueva. */
function press(text, ranges, kind, codeRanges = []) {
  const res = formatEdit({ text, ranges, kind, codeRanges });
  if (!res) return null;
  // ChangeSet.of valida lo que devolvemos: si los cambios se solaparan o
  // estuvieran fuera del documento, esto revienta aquí y no en la app.
  ChangeSet.of(res.changes, text.length);
  let out = "";
  let prev = 0;
  for (const c of res.changes) {
    out += text.slice(prev, c.from) + c.insert;
    prev = c.to;
  }
  out += text.slice(prev);
  return { text: out, selection: res.selection };
}

const sel = (text, needle, kind, codeRanges = [], from = 0) => {
  const at = text.indexOf(needle, from);
  assert.notEqual(at, -1, `no encuentro ${JSON.stringify(needle)} en ${JSON.stringify(text)}`);
  return press(text, [{ from: at, to: at + needle.length }], kind, codeRanges);
};

const cursor = (text, pos, kind, codeRanges = []) => press(text, [{ from: pos, to: pos }], kind, codeRanges);

// ------------------------------------------------------------------- aplicar

test("negrita con texto seleccionado: envuelve y la selección se queda en el texto", () => {
  const r = sel("hola mundo", "hola", "bold");
  assert.equal(r.text, "**hola** mundo");
  assert.deepEqual(r.selection, [{ from: 2, to: 6 }]);
  assert.equal(r.text.slice(r.selection[0].from, r.selection[0].to), "hola");
});

test("cursiva con texto seleccionado: un solo asterisco a cada lado", () => {
  const r = sel("hola mundo", "mundo", "italic");
  assert.equal(r.text, "hola *mundo*");
  assert.equal(r.text.slice(r.selection[0].from, r.selection[0].to), "mundo");
});

test("subrayado: escribe <u>…</u>, no __…__ (que en Markdown es negrita)", () => {
  const r = sel("una palabra suelta", "palabra", "underline");
  assert.equal(r.text, "una <u>palabra</u> suelta");
  assert.equal(r.text.slice(r.selection[0].from, r.selection[0].to), "palabra");
});

test("los espacios de los bordes se quedan fuera de la marca", () => {
  // `** hola **` NO es negrita en Markdown: el delimitador no puede ir pegado
  // a un espacio. Por eso se recorta antes de envolver.
  const r = sel("uno  hola  dos", " hola ", "bold");
  assert.equal(r.text, "uno  **hola**  dos");
  assert.equal(r.text.slice(r.selection[0].from, r.selection[0].to), "hola");
});

// --------------------------------------------------------------------- quitar

test("negrita otra vez sobre el mismo texto: vuelve a quedar limpio", () => {
  const uno = sel("hola mundo", "hola", "bold");
  const dos = sel(uno.text, "hola", "bold");
  assert.equal(dos.text, "hola mundo");
  assert.equal(dos.text.slice(dos.selection[0].from, dos.selection[0].to), "hola");
});

test("quitar también si la selección incluye las marcas", () => {
  const r = sel("di **hola** ya", "**hola**", "bold");
  assert.equal(r.text, "di hola ya");
  assert.equal(r.text.slice(r.selection[0].from, r.selection[0].to), "hola");
});

test("al quitar, los espacios de dentro de la marca se van con ella", () => {
  const r = sel("di ** hola ** ya", "hola", "bold");
  assert.equal(r.text, "di hola ya");
});

test("quitar subrayado deja el texto tal cual estaba", () => {
  const uno = sel("una palabra suelta", "palabra", "underline");
  const dos = sel(uno.text, "palabra", "underline");
  assert.equal(dos.text, "una palabra suelta");
});

test("la negrita no se come la cursiva: dentro de **…** se quita el doble", () => {
  // Con la cursiva dentro de una negrita, quitarla deja `*hola*` (cursiva):
  // es la marca de ESA tecla, no la de la otra.
  const uno = sel("**hola**", "hola", "italic");
  assert.equal(uno.text, "*hola*");
});

// ------------------------------------------------------- sin selección (cursor)

test("sin selección: par vacío con el cursor en medio (negrita y cursiva)", () => {
  const b = cursor("hola ", 5, "bold");
  assert.equal(b.text, "hola ****");
  assert.deepEqual(b.selection, [{ from: 7, to: 7 }]);

  const i = cursor("hola ", 5, "italic");
  assert.equal(i.text, "hola **");
  assert.deepEqual(i.selection, [{ from: 6, to: 6 }]);
});

test("sin selección: el subrayado escribe <u></u> con el cursor dentro", () => {
  const r = cursor("hola ", 5, "underline");
  assert.equal(r.text, "hola <u></u>");
  assert.deepEqual(r.selection, [{ from: 8, to: 8 }]);
});

test("sin selección: otra pulsación quita el par vacío", () => {
  const uno = cursor("hola ", 5, "bold");
  const dos = cursor(uno.text, uno.selection[0].from, "bold");
  assert.equal(dos.text, "hola ");
  assert.deepEqual(dos.selection, [{ from: 5, to: 5 }]);

  const u1 = cursor("hola ", 5, "underline");
  const u2 = cursor(u1.text, u1.selection[0].from, "underline");
  assert.equal(u2.text, "hola ");
  assert.deepEqual(u2.selection, [{ from: 5, to: 5 }]);
});

test("sin selección en una nota vacía: el par vacío también cabe", () => {
  const r = cursor("", 0, "bold");
  assert.equal(r.text, "****");
  assert.deepEqual(r.selection, [{ from: 2, to: 2 }]);
});

// ---------------------------------------------------------------- varias líneas

test("tres líneas: envuelve cada una por separado, sin marcas a través del salto", () => {
  const text = "uno\ndos\ntres";
  const r = press(text, [{ from: 0, to: text.length }], "bold");
  assert.equal(r.text, "**uno**\n**dos**\n**tres**");
  // La selección sigue cubriendo lo mismo (del texto de arriba al de abajo).
  assert.equal(r.text.slice(r.selection[0].from, r.selection[0].to), "uno**\n**dos**\n**tres");
});

test("las líneas de una lista se envuelven sin meterse en el marcador", () => {
  const text = "- uno\n- dos\n- tres";
  const r = press(text, [{ from: 0, to: text.length }], "bold");
  assert.equal(r.text, "- **uno**\n- **dos**\n- **tres**");
});

test("una numerada y una cita conservan su prefijo", () => {
  const lista = "1. uno\n2. dos";
  assert.equal(press(lista, [{ from: 0, to: lista.length }], "italic").text, "1. *uno*\n2. *dos*");

  const cita = "> uno\n> dos";
  assert.equal(press(cita, [{ from: 0, to: cita.length }], "underline").text, "> <u>uno</u>\n> <u>dos</u>");
});

test("un título no se envuelve por el almohadillado", () => {
  const text = "# Titular";
  assert.equal(press(text, [{ from: 0, to: text.length }], "bold").text, "# **Titular**");
});

test("las filas vacías de en medio se saltan (no se ponen marcas sueltas)", () => {
  const text = "uno\n\ndos";
  const r = press(text, [{ from: 0, to: text.length }], "bold");
  assert.equal(r.text, "**uno**\n\n**dos**");
});

test("quitar en varias líneas: cada una vuelve a quedar limpia", () => {
  const text = "**uno**\n**dos**";
  const r = press(text, [{ from: 0, to: text.length }], "bold");
  assert.equal(r.text, "uno\ndos");
});

// ------------------------------------------------------------------- el código

const fenceDoc = "antes\n\n```\ncodigo **aqui**\n```\n\ndespues";
const fenceRange = { from: fenceDoc.indexOf("```"), to: fenceDoc.lastIndexOf("```") + 3 };

test("dentro de un bloque de vallas no se aplica nada", () => {
  const pos = fenceDoc.indexOf("codigo") + 3;
  assert.equal(cursor(fenceDoc, pos, "bold", [fenceRange]), null);
  // Selección entera dentro de la valla, tampoco.
  const dentro = { from: fenceDoc.indexOf("codigo"), to: fenceDoc.indexOf("codigo") + 6 };
  assert.equal(press(fenceDoc, [dentro], "bold", [fenceRange]), null);
});

test("en un `código` en línea tampoco", () => {
  const text = "usa `codigo x` y mas";
  const code = { from: text.indexOf("`"), to: text.lastIndexOf("`") + 1 };
  assert.equal(cursor(text, text.indexOf("codigo") + 1, "bold", [code]), null);
  const dentro = { from: text.indexOf("codigo"), to: text.indexOf("codigo") + 6 };
  assert.equal(press(text, [dentro], "italic", [code]), null);
});

test("fuera del código sí, y el texto de la valla se queda quieto", () => {
  const r = sel(fenceDoc, "antes", "bold", [fenceRange]);
  assert.equal(r.text, "**antes**\n\n```\ncodigo **aqui**\n```\n\ndespues");
});

test("una selección que cruza la valla envuelve lo de fuera y deja el código intacto", () => {
  const r = press(fenceDoc, [{ from: 0, to: fenceDoc.length }], "bold", [fenceRange]);
  assert.equal(r.text, "**antes**\n\n```\ncodigo **aqui**\n```\n\n**despues**");
});

test("un texto que se parece al marcador pero está en código no se toca", () => {
  // La valla tiene `**aqui**` dentro: pulsar negrita fuera no debe quitarlo.
  const pos = fenceDoc.indexOf("despues") + 3;
  const r = cursor(fenceDoc, pos, "bold", [fenceRange]);
  assert.equal(r.text, fenceDoc.slice(0, pos) + "****" + fenceDoc.slice(pos));
});

// ------------------------------------------------------- <u>…</u> para pintarlo

test("underlineSpans empareja <u>…</u> y separa etiquetas de texto", () => {
  const text = "una <u>palabra</u> y <u>otra</u>";
  const spans = underlineSpans(text);
  assert.equal(spans.length, 2);
  assert.deepEqual(
    spans.map((s) => text.slice(s.innerFrom, s.innerTo)),
    ["palabra", "otra"]
  );
  assert.equal(text.slice(spans[0].openFrom, spans[0].openTo), "<u>");
  assert.equal(text.slice(spans[0].closeFrom, spans[0].closeTo), "</u>");
});

test("underlineSpans no cruza un salto de línea ni inventa parejas sueltas", () => {
  assert.deepEqual(underlineSpans("con <u>una\nsola</u> pareja"), []);
  assert.deepEqual(underlineSpans("<u>sin cerrar"), []);
  assert.deepEqual(underlineSpans("</u> al revés <u>"), []);
});

test("underlineSpans devuelve posiciones del documento con el offset", () => {
  const spans = underlineSpans("<u>x</u>", 10);
  assert.deepEqual(spans, [
    { openFrom: 10, openTo: 13, innerFrom: 13, innerTo: 14, closeFrom: 14, closeTo: 18 },
  ]);
});

test("escribir <u> y volver a quitarlo no deja rastro en el documento", () => {
  const original = "esto es palabra fin";
  const uno = sel(original, "palabra", "underline");
  const dos = sel(uno.text, "palabra", "underline");
  assert.equal(uno.text, "esto es <u>palabra</u> fin");
  assert.equal(dos.text, original);
});
