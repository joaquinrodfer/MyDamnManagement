// Bases dentro de una nota (P17.2): lo que se puede probar sin DOM.
//
// El editor pinta la valla `mdm-database` (ver database-block.js) como una
// tabla de solo lectura en cuanto el cursor NO está dentro, y deja el texto
// crudo -- editable, como cualquier bloque de código -- en cuanto entra. Aquí
// viven las dos piezas que no necesitan navegador:
//
//   - `scanDatabaseBlocks(texto)`: dónde empieza y acaba cada bloque CERRADO
//     del documento, con lo que devolvió el parser.
//   - los textos de las tarjetas de error, para que el widget y la app digan
//     exactamente lo mismo (y no se puedan desincronizar).
//
// Lo que NO hace: no mira la selección (eso es cosa de entry.js, que descarta
// los bloques que toca el cursor), no toca el DOM y no pide nada a la red.

import { parseDatabaseBlock } from "./database-block.js";

const INFO = "mdm-database";

// Valla de apertura o de cierre: acentos (3 o más) y, detrás, la info string.
// Se admite sangría porque el .md se escribe a mano y el parser de P17.1
// también la admite -- las dos piezas tienen que ver el mismo bloque.
const FENCE_RE = /^[ \t]*(`{3,})(.*)$/;

/**
 * Localiza los bloques de base CERRADOS de un documento.
 *
 * Devuelve `[{from, to, parse}]`, con `from`/`to` en offsets del texto:
 * `from` es el inicio de la línea de apertura y `to` el final de la línea de
 * cierre (sin el `\n` que la sigue). `parse` es lo que devuelve
 * `parseDatabaseBlock` sobre el cuerpo: `{ok: true, id, view, title}` o
 * `{ok: false, motivo}`.
 *
 * Un bloque SIN cerrar no se devuelve nunca: mientras se escribe (o si el
 * autor se dejó el cierre) no hay forma de saber dónde acaba, y pintarlo
 * tragaría el resto de la nota. Se ve crudo, que es benigno.
 *
 * Las vallas de OTRO lenguaje se saltan enteras: una valla `mdm-database`
 * escrita dentro de un bloque ```js es texto, no un bloque de base -- igual
 * que en Markdown, donde la primera valla sin lenguaje cierra el bloque.
 */
export function scanDatabaseBlocks(text) {
  const input = String(text ?? "");
  const blocks = [];
  let open = null; // {len, from, bodyLines, kind} -- kind: "db" | "other"
  let lineStart = 0;

  while (lineStart <= input.length) {
    const nl = input.indexOf("\n", lineStart);
    const lineEnd = nl === -1 ? input.length : nl;
    // CRLF: el \r no forma parte del contenido de la línea.
    const line = input.slice(lineStart, lineEnd).replace(/\r$/, "");
    const m = FENCE_RE.exec(line);

    if (open === null) {
      if (m) {
        const info = m[2].trim();
        open = {
          len: m[1].length,
          kind: info === INFO ? "db" : "other",
          from: lineStart,
          bodyLines: [],
        };
      }
    } else if (m && m[2].trim() === "") {
      // Cierre: sin info string y con acentos al menos tantos como la apertura
      // (lo que pide Markdown; con menos, la línea es texto del bloque).
      if (m[1].length >= open.len) {
        if (open.kind === "db") {
          blocks.push({
            from: open.from,
            to: lineEnd,
            parse: parseDatabaseBlock(open.bodyLines.join("\n")),
          });
        }
        open = null;
      }
    } else if (open.kind === "db") {
      open.bodyLines.push(line);
    }

    if (lineEnd === input.length) break;
    lineStart = lineEnd + 1;
  }

  return blocks;
}

/** Tarjeta para una valla que el parser no entiende (id que no es uuid, YAML
 * roto...). Lleva el motivo tal cual porque es lo que el usuario tiene que
 * arreglar en el texto. */
export function databaseErrorText(parse) {
  const motivo = parse && parse.motivo ? parse.motivo : "valla inválida";
  return `Bloque de base no válido: ${motivo}`;
}

/** Tarjeta para una base que ya no está (404). `title` es solo informativo y
 * puede faltar: entonces se enseña el id, que es lo único que identifica. El
 * texto del bloque se queda en el documento tal cual -- nunca se borra. */
export function databaseNotFoundText(title, id) {
  const name = String(title ?? "").trim();
  return `Base no encontrada: ${name || String(id ?? "")}`;
}
