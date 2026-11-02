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
