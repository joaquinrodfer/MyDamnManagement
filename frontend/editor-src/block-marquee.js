// Selección por rectángulo, como en el escritorio de Windows: se pulsa en el
// fondo de la página (donde no hay texto ni bloque), se arrastra y quedan
// marcados los bloques que toca el rectángulo.
//
// Aquí solo la parte pura -- qué bloques caen dentro y cómo se combinan con lo
// que ya estaba marcado --, con sus pruebas en test/block-marquee.test.js. El
// ratón, el dibujo del rectángulo y el desplazamiento automático están en
// entry.js (`beginMarquee`).

/** Píxeles que hay que mover el ratón para que el clic pase a ser rectángulo. */
export const MARQUEE_SLOP = 4;

/**
 * Los bloques que toca el rectángulo.
 *
 * - `bands`: por bloque, su franja vertical `{top, bottom}` (coordenadas del
 *   documento, las de `lineBlockAt`); `null` si no se sabe.
 * - `rect`: el rectángulo, `{left, right, top, bottom}`, en las mismas
 *   coordenadas verticales y en las horizontales de la pantalla.
 * - `column`: `{left, right}`, la columna del texto (donde empiezan y acaban
 *   los bloques). Un rectángulo que se queda en el margen no toca nada.
 *
 * Cuenta rozar: basta con que se solapen, como en el escritorio.
 */
export function blocksInMarquee(bands, rect, column) {
  const hits = new Set();
  if (rect.right < column.left || rect.left > column.right) return hits;
  bands.forEach((band, i) => {
    if (band && band.bottom > rect.top && band.top < rect.bottom) hits.add(i);
  });
  return hits;
}

/**
 * Lo marcado al final: `base` es lo que había al empezar y `hits` lo que toca
 * el rectángulo ahora.
 *
 * - `"replace"` (sin teclas): solo lo del rectángulo.
 * - `"add"` (Mayús): lo de antes más lo del rectángulo.
 * - `"toggle"` (Ctrl/⌘): lo del rectángulo cambia de estado, como en Windows.
 */
export function mergeMarquee(base, hits, mode) {
  if (mode === "add") return new Set([...base, ...hits]);
  if (mode === "toggle") {
    const next = new Set(base);
    for (const i of hits) next.has(i) ? next.delete(i) : next.add(i);
    return next;
  }
  return new Set(hits);
}

/** El modo según las teclas pulsadas al empezar. */
export function marqueeMode(event) {
  if (event.ctrlKey || event.metaKey) return "toggle";
  if (event.shiftKey) return "add";
  return "replace";
}

export function sameSet(a, b) {
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
}
