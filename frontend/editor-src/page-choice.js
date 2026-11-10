// Opciones del comando "/page" del menú "/".
//
// El comando ya no se limita a crear una página hija: con el mismo gesto se
// enlaza con una que ya existe o se renombra una que choca con el nombre
// escrito (el dueño lo pidió tal cual: «al hacer /page deberíamos de poner la
// opción de enlazar a otras páginas y cambiarle el nombre»). Aquí vive la parte
// que no necesita DOM ni red -- qué se ofrece, en qué orden y cómo se llama
// cada opción --, probada con `node --test` en test/page-choice.test.js.
//
// Las tres clases de opción que devuelve `pageChoices`:
//
//   - `link`   : una página que ya existe. Al elegirla NO se crea nada y NO se
//                navega: se escribe `[[Título]]` en el cuerpo, que es lo que
//                crea el backlink (ver app.js, desplegable «Notas que enlazan
//                aquí»).
//   - `create` : crear una página nueva con lo escrito, hija de la nota en la
//                que se está (app.js navega a ella después de guardar la
//                anterior).
//   - `rename` : renombrar una página que se llama igual que lo escrito y que
//                es la nota actual o una hija suya. Sin esto, crear con ese
//                nombre dejaría dos iguales (o un «(2)» que nadie pidió): el
//                servidor desambigua en silencio porque el disco no admite dos
//                ficheros con el mismo nombre.

import { isLinkableTitle } from "./link-create.js";
import { databaseFilterRange } from "./database-choice.js";

/** Sin mayúsculas ni acentos: buscar «ano» tiene que encontrar «Año». */
function plegar(texto) {
  return String(texto)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

/**
 * Normaliza lo que da la app: `[{id, title, type, parent_id, folder}]`.
 *
 * `id` es el `page_id` (el del árbol: es con el que navega /pages/{id}); una
 * entrada sin id no sirve ni para navegar ni para renombrar, y sin título no se
 * puede ni elegir. Los títulos que el formato del wikilink no puede
 * representar (`]`, `|`: ver isLinkableTitle) se quedan fuera: ofrecerlos en un
 * menú que escribe `[[…]]` sería ofrecer un enlace roto.
 *
 * `parent_id` se normaliza a `null` para poder comparar sin distinguir `""` de
 * `null` (la raíz llega de las dos formas según el endpoint).
 */
export function normalizePageEntries(list) {
  const out = [];
  const vistos = new Set();
  for (const raw of Array.isArray(list) ? list : []) {
    const id = raw?.id == null ? "" : String(raw.id);
    const title = raw?.title == null ? "" : String(raw.title).trim();
    if (!id || !title) continue;
    if (vistos.has(id)) continue; // el árbol no repite ids; por si acaso
    if (!isLinkableTitle(title)) continue;
    vistos.add(id);
    const parentId =
      raw?.parent_id == null || raw.parent_id === "" ? null : String(raw.parent_id);
    out.push({
      id,
      title,
      type: raw?.type === "database" ? "database" : "note",
      parentId,
      folder: raw?.folder == null ? "" : String(raw.folder).trim(),
    });
  }
  return out;
}

/** Páginas cuyo título o carpeta contienen `query` (da igual mayúsculas o
 * acentos), primero las que empiezan por lo escrito y luego por título. */
export function filterPages(entries, query) {
  const q = plegar(String(query ?? "").trim());
  const lista = Array.isArray(entries) ? entries : [];
  if (!q) {
    return lista
      .slice()
      .sort((a, b) => a.title.localeCompare(b.title, "es", { sensitivity: "base" }));
  }
  const encontradas = [];
  for (const e of lista) {
    const titulo = plegar(e.title);
    const carpeta = plegar(e.folder);
    if (!titulo.includes(q) && !carpeta.includes(q)) continue;
    encontradas.push({ entry: e, rank: titulo.startsWith(q) ? 0 : 1 });
  }
  encontradas.sort(
    (a, b) =>
      a.rank - b.rank ||
      a.entry.title.localeCompare(b.entry.title, "es", { sensitivity: "base" }),
  );
  return encontradas.map((m) => m.entry);
}

/** La página que se llama EXACTAMENTE como lo escrito, o null. */
export function findExactPage(entries, query) {
  const q = plegar(String(query ?? "").trim());
  if (!q) return null;
  for (const e of Array.isArray(entries) ? entries : []) {
    if (plegar(e.title) === q) return e;
  }
  return null;
}

/**
 * Opciones que enseña el selector de `/page`.
 *
 * `context` es `{currentId}`: la nota abierta. Sirve para saber si el nombre
 * escrito choca con ella o con una hija suya -- los dos casos en los que crear
 * una página nueva daría un duplicado que el usuario probablemente no quiere.
 *
 * Reglas (y por qué):
 *   - si hay coincidencia EXACTA no se ofrece crear: crear otra con el mismo
 *     título es lo que luego hace ambiguo un `[[título]]`;
 *   - la opción de enlazar va PRIMERO y es la que queda seleccionada: Enter
 *     sobre un nombre que ya existe enlaza (no crea ni navega), que es el gesto
 *     seguro;
 *   - renombrar solo se ofrece cuando la coincidencia exacta es la nota actual
 *     o una hija (ahí sí es un choque real al crear un duplicado);
 *   - sin texto escrito no hay opción de crear -- una página sin nombre no
 *     existe -- así que la lista es solo la del vault.
 */
export function pageChoices(entries, query, context = {}) {
  const q = String(query ?? "").trim();
  const currentId = context?.currentId == null ? null : String(context.currentId);

  const opciones = filterPages(entries, q).map((e) => ({
    kind: "link",
    id: e.id,
    label: e.title,
    hint: pageHint(e),
    entry: e,
  }));

  const exacta = findExactPage(entries, q);
  if (exacta) {
    // La coincidencia exacta, la primera: Enter enlaza con ella.
    const i = opciones.findIndex((o) => o.kind === "link" && o.id === exacta.id);
    if (i > 0) opciones.unshift(opciones.splice(i, 1)[0]);
    const esActual = currentId !== null && exacta.id === currentId;
    const esHija = currentId !== null && exacta.parentId === currentId;
    if (esActual || esHija) {
      opciones.push({
        kind: "rename",
        id: exacta.id,
        label: `Renombrar «${exacta.title}»…`,
        hint: "nombre nuevo",
        entry: exacta,
      });
    }
    return opciones;
  }

  if (q) {
    opciones.push({
      kind: "create",
      label: `Crear «${q}»`,
      hint: "página nueva",
    });
  }
  return opciones;
}

/**
 * Cuántas páginas se pintan como mucho en el selector de `/page`.
 *
 * El menú enseña 260 px con scroll (`.mdm-slash-menu` en index.html), así que
 * pintar el vault entero no se ve: solo cuesta. Medido con 1.208 páginas,
 * abrir el selector tardaba 107 ms y cada tecla 70-90 ms en repintar ~1.200
 * filas que nadie llega a mirar (extrapolado a las 12.000 notas del objetivo:
 * ~1 s al abrir y ~0,8 s por tecla). Con el tope, lo que se pinta deja de
 * depender del tamaño del vault.
 *
 * 50 filas es más de lo que se recorre con las flechas antes de escribir una
 * letra más; lo que queda fuera se avisa en una fila final («… y N más, sigue
 * escribiendo») y sigue siendo alcanzable acotando la consulta.
 */
export const PAGE_CHOICES_LIMIT = 50;

/**
 * Recorta la lista de opciones a `limit` páginas enlazables.
 *
 * Solo se recortan las de `kind: "link"` (las del vault): las acciones
 * -- «Renombrar «…»» y «Crear «…»» -- van siempre, aunque el tope esté
 * alcanzado. Son lo único que no se puede sustituir escribiendo más, y
 * dejarlas fuera convertiría un vault grande en uno donde `/page` no puede
 * crear ni renombrar.
 *
 * El orden no se toca: se conservan las primeras `limit` tal como las dejó
 * `pageChoices` (coincidencia exacta primero) y las acciones siguen detrás,
 * en su orden. Devuelve `{options, hidden}`: lo que hay que pintar y cuántas
 * páginas se han quedado fuera.
 */
export function capPageChoices(options, limit = PAGE_CHOICES_LIMIT) {
  const lista = Array.isArray(options) ? options : [];
  const tope =
    Number.isFinite(limit) && limit >= 0 ? Math.floor(limit) : PAGE_CHOICES_LIMIT;
  const enlaces = [];
  const acciones = [];
  for (const opcion of lista) {
    if (opcion?.kind === "link") enlaces.push(opcion);
    else acciones.push(opcion);
  }
  if (enlaces.length <= tope) return { options: lista.slice(), hidden: 0 };
  return {
    options: enlaces.slice(0, tope).concat(acciones),
    hidden: enlaces.length - tope,
  };
}

/** Lo que se lee a la derecha de cada página en el selector. */
function pageHint(entry) {
  const tipo = entry.type === "database" ? "base" : "";
  const carpeta = entry.folder || "raíz";
  return tipo ? `${carpeta} · ${tipo}` : carpeta;
}

/**
 * Tramo que ocupa lo escrito en el selector de páginas: desde `from` hasta el
 * FINAL DE LA LÍNEA, no hasta el caret (mismo criterio que el selector de
 * bases, y por el mismo motivo: el filtro se escribe dentro del documento, así
 * que lo que hay que sustituir -- o borrar al cancelar -- es la línea entera).
 */
export function pageFilterRange(doc, from) {
  return databaseFilterRange(doc, from);
}

/** El cambio que borra el filtro tecleado, o `null` si está vacío. */
export function pageFilterClear(doc, from) {
  const { from: start, to } = pageFilterRange(doc, from);
  return to > start ? { from: start, to } : null;
}
