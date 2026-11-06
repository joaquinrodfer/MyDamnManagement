// Bloque de base dentro de una página: la valla `mdm-database`.
//
// La sintaxis está cerrada (informe INFORME-BASES-EN-PAGINAS.md, §7.3):
//
//   ```mdm-database
//   id: 7a3f9c21-4b0e-4c8a-9f31-2d5e6b7a8c90
//   view: Tablero
//   title: Contactos
//   ```
//
// `id` es el id de la FILA de `databases`, NO el page_id de la página que la
// representa: en el repo son dos UUID distintos y `/databases/{id}` espera el
// primero (ver el comentario sobre `database_id` en frontend/app.js).
//
// `view` es el nombre de la vista; si falta (o no existe), la app abre la
// primera, que es el criterio de siempre. `title` es SOLO informativo: sirve
// para leer el .md a mano y para el aviso cuando la base ya no está; nunca es
// la identidad de nada.
//
// El cuerpo es YAML de juguete: `clave: valor`, sin listas ni anidamiento.
// Cualquier clave que no sea `id`, `view` o `title` se IGNORA (no es error):
// así se puede añadir sintaxis en el futuro sin romper notas viejas.
//
// Nada aquí toca el DOM ni la red: son funciones puras, probadas con
// `node --test` en test/database-block.test.js. Pintar la base y validar que
// exista (necesita la API) son tarjetas aparte.

const INFO = "mdm-database";

// Una valla de apertura o de cierre: acentos (3 o más) y, detrás, la info
// string (vacía en la de cierre). Se admite sangría porque el .md se escribe
// a mano y el editor no la prohíbe.
const FENCE_RE = /^[ \t]*(`{3,})(.*)$/;

// UUID en forma canónica 8-4-4-4-12. No se mira la versión: el id lo genera
// la base de datos, aquí solo se comprueba que tenga forma de uuid.
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

// Valores que este parser no entiende (listas, mapas, anclas, bloques
// multilínea). El formato es `clave: valor` y nada más.
const UNSUPPORTED_START_RE = /^[[{>|&*!%@]/;
const UNSUPPORTED_DASH_RE = /^-([ \t]|$)/;

function stripCr(line) {
  return line.endsWith("\r") ? line.slice(0, -1) : line;
}

/** ¿La fila es una valla (de apertura o de cierre)? */
function fenceInfo(line) {
  const m = FENCE_RE.exec(stripCr(line));
  return m ? m[2].trim() : null;
}

function isFenceLine(line) {
  return fenceInfo(line) !== null;
}

/**
 * ¿La fila `i` abre un bloque de base? Es la puerta que usa el editor: solo
 * devuelve `true` cuando la info string de la valla es exactamente
 * `mdm-database`. Una valla sin lenguaje, una con otro lenguaje (```js) o un
 * párrafo que mencione la palabra no son bloques de base.
 */
export function isDatabaseFence(lines, i) {
  if (!Array.isArray(lines)) {
    throw new TypeError("isDatabaseFence necesita las líneas del documento");
  }
  const line = lines[i];
  if (typeof line !== "string") return false;
  return fenceInfo(line) === INFO;
}

/** Valor escalar del YAML de juguete: sin comillas, o entre comillas. */
function parseScalar(raw) {
  const s = raw.trim();
  if (s === "") return { ok: true, value: null };

  const quote = s[0];
  if (quote === '"' || quote === "'") {
    if (s.length < 2 || s[s.length - 1] !== quote) return { ok: false };
    const inner = s.slice(1, -1);
    if (quote === "'") {
      // En YAML, dentro de comillas simples la comilla se escapa doblándola.
      return { ok: true, value: inner.replace(/''/g, "'") };
    }
    let out = "";
    for (let i = 0; i < inner.length; i++) {
      const ch = inner[i];
      if (ch !== "\\") {
        out += ch;
        continue;
      }
      const next = inner[i + 1];
      if (next === "\\" || next === '"') {
        out += next;
        i++;
      } else {
        out += ch;
      }
    }
    return { ok: true, value: out };
  }

  if (UNSUPPORTED_START_RE.test(s) || UNSUPPORTED_DASH_RE.test(s)) return { ok: false };
  return { ok: true, value: s };
}

/** Escribe un valor para que `parseScalar` lo devuelva idéntico. */
function formatScalar(value) {
  const s = String(value);
  const bare =
    s !== "" &&
    !/^\s|\s$/.test(s) &&
    !/[:#'"[\]{}]/.test(s) &&
    !UNSUPPORTED_START_RE.test(s) &&
    !UNSUPPORTED_DASH_RE.test(s);
  if (bare) return s;
  if (!s.includes("'")) return `'${s}'`;
  return `"${s.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

/**
 * Lee un bloque de base del texto de una valla.
 *
 * Acepta las dos formas, porque el editor tiene las dos a mano: la valla
 * entera (con la línea de apertura y la de cierre) y solo el cuerpo. La
 * apertura, si está, tiene que ser NUESTRA valla -- ` ```js ` no es un bloque
 * de base y se rechaza como YAML inválido. La de cierre es opcional: mientras
 * se escribe, el bloque aún no la tiene.
 *
 * Devuelve `{ok: true, id, view, title}` (con `null` en `view`/`title` si no
 * vienen) o `{ok: false, motivo}`, con el motivo en castellano y listo para
 * enseñar al usuario. NO comprueba que la base exista: eso necesita la API.
 */
export function parseDatabaseBlock(texto) {
  const lines = String(texto ?? "").split(/\r?\n/);
  let from = 0;
  let to = lines.length;

  if (from < to && isFenceLine(lines[from])) {
    if (!isDatabaseFence(lines, from)) return { ok: false, motivo: "yaml inválido" };
    from += 1;
  }
  while (to > from && stripCr(lines[to - 1]).trim() === "") to -= 1;
  if (to - 1 >= from && isFenceLine(lines[to - 1])) to -= 1;

  const body = lines.slice(from, to);
  const data = { id: null, view: null, title: null };
  let seen = 0;

  for (const raw of body) {
    const line = stripCr(raw).trim();
    if (line === "" || line.startsWith("#")) continue;
    seen += 1;

    const at = line.indexOf(":");
    const key = at === -1 ? "" : line.slice(0, at).trim();
    if (!key) return { ok: false, motivo: "yaml inválido" };

    const parsed = parseScalar(line.slice(at + 1));
    if (!parsed.ok) return { ok: false, motivo: "yaml inválido" };

    // Claves desconocidas: se ignoran a propósito (compatibilidad hacia
    // delante). Un valor vacío (`view:` o `view: ""`) cuenta como ausente.
    const value = parsed.value === "" ? null : parsed.value;
    if (key === "id") data.id = value;
    else if (key === "view") data.view = value;
    else if (key === "title") data.title = value;
  }

  if (seen === 0) return { ok: false, motivo: "valla vacía" };
  if (data.id === null) return { ok: false, motivo: "sin id" };
  if (!UUID_RE.test(data.id)) return { ok: false, motivo: "id no es un uuid" };

  return { ok: true, id: data.id, view: data.view, title: data.title };
}

/**
 * Reconstruye el texto de la valla a partir de `{id, view, title}`.
 *
 * `view` y `title` se omiten si son `null`/vacío, así que
 * `parseDatabaseBlock(serializeDatabaseBlock(x))` devuelve el mismo `x`.
 * Lanza `TypeError` sin `id`: un bloque de base sin id no es un bloque.
 */
export function serializeDatabaseBlock(block = {}) {
  const { id, view = null, title = null } = block;
  if (id === null || id === undefined || String(id) === "") {
    throw new TypeError("serializeDatabaseBlock necesita el id de la base");
  }

  const lines = ["```" + INFO, "id: " + formatScalar(id)];
  for (const key of ["view", "title"]) {
    const value = key === "view" ? view : title;
    if (value !== null && value !== undefined && String(value) !== "") {
      lines.push(key + ": " + formatScalar(value));
    }
  }
  lines.push("```");
  return lines.join("\n");
}
