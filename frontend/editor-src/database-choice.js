// Opciones del comando "/base" del menú "/" (P17.3).
//
// La app da la lista de bases del vault con su carpeta (sale del árbol de
// páginas); aquí solo se decide QUÉ se ofrece y CÓMO se llama cada opción, y
// qué texto se escribe en la nota. Nada de DOM ni de red: son funciones puras,
// probadas con `node --test` en test/database-choice.test.js.
//
// El texto que se escribe es la valla `mdm-database` de database-block.js MÁS
// un párrafo vacío debajo: el cursor tiene que quedar FUERA de la valla --
// dentro se pinta en crudo (ver databaseBlockDecorations en entry.js) y lo que
// se teclee pasaría a formar parte del bloque.

import { serializeDatabaseBlock } from "./database-block.js";

/** Sin mayúsculas ni acentos: buscar «ano» tiene que encontrar «Año». */
function plegar(texto) {
  return String(texto)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

/**
 * Normaliza lo que da la app: `[{id, title, folder}]`.
 *
 * El `id` es el de la FILA `databases` (el que espera `/databases/{id}` y el
 * que va en el bloque), no el `page_id` de la página que la representa. Una
 * entrada sin id no sirve para nada (no se puede insertar) y sin título no se
 * puede ni elegir, así que las dos se descartan.
 */
export function normalizeDatabaseEntries(list) {
  const out = [];
  const vistos = new Set();
  for (const raw of Array.isArray(list) ? list : []) {
    const id = raw?.id == null ? "" : String(raw.id);
    const title = raw?.title == null ? "" : String(raw.title).trim();
    if (!id || !title) continue;
    if (vistos.has(id)) continue; // el árbol no repite ids; por si acaso
    vistos.add(id);
    out.push({ id, title, folder: raw?.folder == null ? "" : String(raw.folder).trim() });
  }
  return out;
}

/** Bases cuyo título o carpeta contienen `query` (da igual mayúsculas o acentos). */
export function filterDatabases(entries, query) {
  const q = plegar(String(query ?? "").trim());
  const lista = Array.isArray(entries) ? entries : [];
  if (!q) return lista.slice();
  return lista.filter((e) => plegar(e.title).includes(q) || plegar(e.folder).includes(q));
}

/**
 * Opciones del selector: las bases que pasan el filtro y, al final y siempre,
 * la de crear una nueva.
 *
 * La de crear lleva el texto del filtro en su etiqueta («Crear «Con»») cuando
 * lo hay: es lo que el usuario ya escribió y es el nombre que llevará la base
 * si elige esa opción (el editor pasa ese mismo texto al modo "naming"). Va la
 * última y no la primera a propósito -- con Enter se elige la primera opción, y
 * cuando hay bases que coinciden lo que se quiere es abrir una, no crear otra
 * parecida.
 */
export function databaseChoices(entries, query) {
  const q = String(query ?? "").trim();
  const opciones = filterDatabases(entries, q).map((e) => ({
    kind: "item",
    id: e.id,
    label: e.title,
    hint: e.folder,
    entry: e,
  }));
  opciones.push({
    kind: "create",
    label: q ? `Crear «${q}»` : "Nueva base…",
    hint: "crear",
  });
  return opciones;
}

/**
 * Tramo que ocupa lo tecleado en el selector de bases: desde `from` hasta el
 * FINAL DE LA LÍNEA, no hasta el caret.
 *
 * El filtro -- y, al crear, el nombre que llevará la base -- se escribe dentro
 * del documento, así que lo que hay que sustituir (o borrar al cancelar) es
 * todo lo escrito en esa línea. Cortando en el caret, con el cursor a mitad del
 * texto la base se creaba con el nombre truncado y el resto del filtro se
 * quedaba de párrafo suelto detrás del bloque.
 *
 * `doc` es el documento del editor: basta con que tenga `length` y
 * `lineAt(pos).to` (el `Text` de CodeMirror). Sin DOM ni red, como el resto del
 * módulo; `from` se recorta al documento por si llega de un estado viejo.
 */
export function databaseFilterRange(doc, from) {
  const length = Math.max(0, Number(doc?.length) || 0);
  const start = Math.min(Math.max(Number(from) || 0, 0), length);
  return { from: start, to: doc.lineAt(start).to };
}

/**
 * El cambio que borra el filtro tecleado en el selector, o `null` si no hay
 * nada que borrar (filtro vacío: despachar una transacción vacía solo
 * estorbaría en el historial).
 *
 * Es `databaseFilterRange` con el caso «no hay texto» resuelto, para que quien
 * lo despacha -- el plugin de entry.js, al cerrar el selector desde fuera --
 * no tenga que repetir el recorte.
 */
export function databaseFilterClear(doc, from) {
  const { from: start, to } = databaseFilterRange(doc, from);
  return to > start ? { from: start, to } : null;
}

/**
 * Lo que se escribe en la nota al elegir una base: la valla cerrada y un
 * párrafo nuevo debajo, con el cursor en él (`cursorOffset`).
 *
 * `context` (`{before, after}`: el texto del documento a cada lado del tramo
 * que se sustituye) decide las líneas en blanco que hacen falta para que el
 * bloque quede separado como cualquier otro -- UNA por delante y UNA por
 * detrás, ni pegado al párrafo ni con aire de más:
 *
 *   - Antes: a mitad de una línea con texto, el bloque necesita su propia
 *     línea y una en blanco (`"\n\n"`); al principio de una línea cuya de
 *     arriba tiene texto, falta solo la blanca (`"\n"`); si ya hay una
 *     blanca (o el bloque abre el documento), ninguna.
 *   - Después: el cambio se lleva por delante los saltos de línea que siguen
 *     al tramo (`dropAfter`), de modo que lo que venía detrás quede a UNA
 *     línea en blanco de la valla -- sin esto el salto de la propia línea más
 *     el `"\n\n"` dejaban dos o tres.
 *
 * Sin `context` se mantiene el texto de siempre (valla + `"\n\n"`, cursor al
 * final): es el caso de una nota que acaba ahí.
 *
 * Lanza `TypeError` sin `id` (lo mismo que `serializeDatabaseBlock`): un bloque
 * de base sin id no apunta a nada.
 */
export function databaseBlockInsert(block, context) {
  const fence = serializeDatabaseBlock(block);
  const withContext = context !== undefined && context !== null;
  const before = withContext && typeof context.before === "string" ? context.before : "";
  const after = withContext && typeof context.after === "string" ? context.after : "";

  let prefix = "";
  if (withContext) {
    const lineas = before.split("\n");
    const actual = lineas[lineas.length - 1]; // lo escrito antes del tramo en su línea
    const anterior = lineas.length > 1 ? lineas[lineas.length - 2] : null;
    if (actual.trim() !== "") prefix = "\n\n"; // a mitad de párrafo: línea propia + blanca
    else if (anterior !== null && anterior.trim() !== "") prefix = "\n"; // falta la blanca
  }

  const text = prefix + fence + "\n\n";
  const salto = withContext ? /^\n+/.exec(after) : null;
  const dropAfter = salto ? salto[0].length : 0;
  // Con contexto el cursor va en la línea en blanco de debajo (lo que venía
  // detrás se escribe después); sin contexto, al final del texto, como antes.
  const cursorOffset = withContext ? prefix.length + fence.length + 1 : text.length;
  return { text, cursorOffset, dropAfter };
}
