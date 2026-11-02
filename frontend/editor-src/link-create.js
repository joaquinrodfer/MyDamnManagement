// Lógica pura de la opción «Crear «…»» del autocompletado de wikilinks: qué
// título se manda a la API, qué fragmento del documento se sustituye y con qué
// texto se queda al final. Vive aparte de entry.js (que necesita el DOM) para
// poder probarla sin navegador: ver test/link-create.test.js, que corre en CI.

export const LINK_OPEN = "[[";
export const LINK_CLOSE = "]]";

/** Título que viaja a la API: lo tecleado, sin espacios sobrantes. */
export function createLinkTitle(typed) {
  return String(typed == null ? "" : typed).trim();
}

// Un título que el formato del wikilink no puede representar. `WIKILINK_RE`
// (aquí y en backend/app/wikilinks.py: `\[\[([^\]|]+)(?:\|[^\]]+)?\]\]`)
// corta en el primer `]` y en el primer `|`, así que `[[Notas [2026]]]` se lee
// como `[[Notas [2026` -- sin arista -- y `[[a|b]]` enlaza con `a` y llama `b`
// al alias. `names.sanitize()` ya impide crear esos títulos desde la app, pero
// un `.md` copiado a mano (o creado antes del arreglo) los puede traer: esos
// no se ofrecen en la lista.
const LINK_TITLE_RE = /^[^\]|]+$/;

/** ¿Se puede escribir `[[title]]` y volver a leer `title` al resolverlo? */
export function isLinkableTitle(title) {
  const limpio = createLinkTitle(title);
  return limpio !== "" && LINK_TITLE_RE.test(limpio);
}

/** Cuántos destinos de la lista se pueden enlazar de verdad. Lo usa la lista
 * cuando sale vacía: con cero destinos enlazables el vault no tiene notas que
 * ofrecer («Aún no hay notas»), y con alguno lo que no hay es coincidencia con
 * lo escrito («Sin coincidencias»). Contar la lista SIN filtrar hacía que un
 * vault con solo títulos no enlazables dijera «Sin coincidencias». */
export function countLinkableTitles(targets) {
  let total = 0;
  for (const target of targets || []) {
    if (isLinkableTitle(target && target.title)) total += 1;
  }
  return total;
}

/** Lo que se escribe en el documento para enlazar con `title`. */
export function linkMarkup(title) {
  return `${LINK_OPEN}${createLinkTitle(title)}${LINK_CLOSE}`;
}

/** Título real que hay que insertar tras crear la nota, o null si no hay que
 * tocar el documento.
 *
 * `fragment` es el texto CRUDO que sigue bajo el "[[", con los espacios que se
 * teclearon: es lo que hay en el documento, así que se compara sin recortar.
 * El título para el POST sí va recortado (createLinkTitle). Mezclar las dos
 * cosas dejaba sin insertar el enlace cuando lo escrito acababa en espacio,
 * aunque la nota ya se hubiera creado en el servidor.
 *
 * null también cuando el usuario siguió escribiendo (o cambió de nota) durante
 * la creación: bajo "[[" ya no está el fragmento que se estaba sustituyendo y
 * escribir encima sería peor que no hacer nada. */
export function resolveCreatedLinkTitle(fragment, typed, createdTitle) {
  const title = createLinkTitle(createdTitle);
  if (!title) return null;
  const typedText = typed == null ? "" : String(typed);
  if (fragment !== `${LINK_OPEN}${typedText}`) return null;
  return title;
}
