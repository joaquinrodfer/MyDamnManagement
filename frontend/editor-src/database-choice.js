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
 * Lo que se escribe en la nota al elegir una base: la valla cerrada y un
 * párrafo nuevo debajo, con el cursor en él (`cursorOffset`).
 *
 * Lanza `TypeError` sin `id` (lo mismo que `serializeDatabaseBlock`): un bloque
 * de base sin id no apunta a nada.
 */
export function databaseBlockInsert(block) {
  const text = serializeDatabaseBlock(block) + "\n\n";
  return { text, cursorOffset: text.length };
}
