// Fuente del editor de notas (CodeMirror 6). Se compila a un único archivo
// con `npm run build` (esbuild) -> ../vendor/editor.bundle.js, que es lo
// que carga la app de verdad. Este archivo nunca se sirve tal cual.
//
// Deliberadamente no fija ningún color aquí -- solo asigna clases CSS.
// El aspecto (colores claro/oscuro, tamaños) vive en frontend/index.html
// junto al resto del sistema de diseño, para no duplicar el tema en dos
// sitios.

import { ChangeSet, EditorState, EditorSelection, StateEffect, StateField, Transaction } from "@codemirror/state";
import { EditorView, keymap, Decoration, ViewPlugin, WidgetType } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { HighlightStyle, syntaxHighlighting, syntaxTree } from "@codemirror/language";
import { markdown, deleteMarkupBackward } from "@codemirror/lang-markdown";
import { tags } from "@lezer/highlight";
import {
  autocompletion,
  acceptCompletion,
  closeCompletion,
  moveCompletionSelection,
} from "@codemirror/autocomplete";
import {
  LINK_OPEN,
  countLinkableTitles,
  createLinkTitle,
  isLinkableTitle,
  linkMarkup,
  resolveCreatedLinkTitle,
} from "./link-create.js";
import { blockKeyEdit } from "./block-keys.js";
import {
  backspaceJoinEdit,
  backspaceWouldBreakFence,
  blockDeleteEdits,
  convertBlockEdits,
  duplicateBlockEdits,
  newBlockEdit,
  orderedRenumberEdits,
  renumberOrderedRun,
} from "./block-delete.js";
import { canonicalBody, collapseEmptyGaps, cursorInOuterBlanks, shiftEmptyGap } from "./body.js";
import { databaseErrorText, databaseNotFoundText, scanDatabaseBlocks } from "./database-embed.js";
import {
  databaseBlockInsert,
  databaseChoices,
  normalizeDatabaseEntries,
} from "./database-choice.js";

const highlightStyle = HighlightStyle.define([
  { tag: tags.heading1, class: "cm-mdm-h1" },
  { tag: tags.heading2, class: "cm-mdm-h2" },
  { tag: tags.heading3, class: "cm-mdm-h3" },
  { tag: tags.heading4, class: "cm-mdm-h4" },
  { tag: tags.heading5, class: "cm-mdm-h5" },
  { tag: tags.heading6, class: "cm-mdm-h5" }, // sin H6 en el menú "/"; se ve como H5
  { tag: tags.strong, class: "cm-mdm-strong" },
  { tag: tags.emphasis, class: "cm-mdm-em" },
  { tag: tags.monospace, class: "cm-mdm-code" },
  // HeaderMark/EmphasisMark/CodeMark/ListMark ("#", "**", "`", "-"...) NO
  // llevan aquí una regla fija -- su visibilidad depende de si el bloque que
  // los contiene está siendo editado ahora mismo, así que las decide
  // syntaxMarkPlugin() más abajo (StateField/ViewPlugin, no HighlightStyle
  // estático) en vez de esta lista.
]);

const WIKILINK_RE = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g;

function selectionIntersects(state, from, to) {
  return state.selection.ranges.some((r) => r.from <= to && r.to >= from);
}

// Un wikilink resuelto y que no se está editando ahora mismo (el cursor no
// está dentro) se sustituye entero por este widget -- se ve como "un enlace
// normal", sin corchetes. En cuanto el cursor entra en su rango (clic,
// flechas...) selectionIntersects deja de ser true y buildWikilinkDecorations
// vuelve a mostrar el texto crudo editable, con sus marcadores pequeños.
class WikilinkWidget extends WidgetType {
  constructor(label, pageId) {
    super();
    this.label = label;
    this.pageId = pageId;
  }
  eq(other) {
    return other.label === this.label && other.pageId === this.pageId;
  }
  toDOM() {
    const el = document.createElement("span");
    el.className = "cm-mdm-wikilink-rendered";
    el.textContent = this.label;
    el.dataset.pageId = this.pageId;
    return el;
  }
  ignoreEvent() {
    return false; // que clic/hover le lleguen a nuestros handlers
  }
}

function buildWikilinkDecorations(view, resolvePage) {
  const ranges = [];
  for (const { from, to } of view.visibleRanges) {
    const text = view.state.doc.sliceString(from, to);
    WIKILINK_RE.lastIndex = 0;
    let m;
    while ((m = WIKILINK_RE.exec(text))) {
      const start = from + m.index;
      const end = start + m[0].length;
      const innerStart = start + 2;
      const innerEnd = end - 2;
      const title = m[1].trim();
      const alias = m[2] ? m[2].trim() : null;
      const resolved = resolvePage(title);

      // Igual que en los enlaces Markdown: un Decoration.replace() que
      // cruce un salto de línea no se admite desde un ViewPlugin y tira
      // abajo el editor entero -- si el wikilink tiene un "\n" en medio
      // (Enter sin querer entre los corchetes), se deja siempre en crudo.
      if (resolved && !m[0].includes("\n") && !selectionIntersects(view.state, start, end)) {
        ranges.push(
          Decoration.replace({ widget: new WikilinkWidget(alias || title, resolved.id) }).range(start, end)
        );
        continue;
      }

      // Editando (cursor dentro) o sin resolver: texto crudo, editable, con
      // los corchetes visibles (pequeños) para que se note que hace falta
      // arreglarlo si no hay página con ese título. Clase propia
      // (cm-mdm-wikimark, no cm-mdm-mark): el resaltado de Markdown ya
      // intenta leer "[[" como el inicio de un link normal y añade su
      // propia decoración ahí -- con la misma clase quedarían dos <span>
      // anidados pisándose (mismo CSS de todos modos, pero sin el choque).
      ranges.push(Decoration.mark({ class: "cm-mdm-wikimark" }).range(start, innerStart));
      ranges.push(
        Decoration.mark({
          class: resolved ? "cm-mdm-wikilink" : "cm-mdm-wikilink-missing",
          attributes: resolved ? { "data-page-id": resolved.id } : {},
        }).range(innerStart, innerEnd)
      );
      ranges.push(Decoration.mark({ class: "cm-mdm-wikimark" }).range(innerEnd, end));
    }
  }
  return Decoration.set(ranges, true);
}

function wikilinkPlugin(resolvePage) {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = buildWikilinkDecorations(view, resolvePage);
      }
      update(update) {
        if (update.docChanged || update.viewportChanged || update.selectionSet) {
          this.decorations = buildWikilinkDecorations(update.view, resolvePage);
        }
      }
    },
    { decorations: (v) => v.decorations }
  );
}

function wikilinkClickHandler(onNavigate) {
  return EditorView.domEventHandlers({
    mousedown(event) {
      if (!(event.ctrlKey || event.metaKey)) return false;
      const target = event.target;
      if (!(target instanceof HTMLElement)) return false;
      const link = target.closest("[data-page-id]");
      if (!link) return false;
      const pageId = link.dataset.pageId;
      if (!pageId) return false;
      event.preventDefault();
      onNavigate(pageId);
      return true;
    },
  });
}

const HOVER_DELAY_MS = 350;

function wikilinkHoverHandler(onHover, onHoverEnd) {
  let timer = null;
  return EditorView.domEventHandlers({
    mouseover(event) {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return false;
      const link = target.closest("[data-page-id]");
      if (!link) return false;
      const pageId = link.dataset.pageId;
      const rect = link.getBoundingClientRect();
      clearTimeout(timer);
      timer = setTimeout(() => onHover?.(pageId, rect), HOVER_DELAY_MS);
      return false;
    },
    mouseout(event) {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return false;
      if (!target.closest("[data-page-id]")) return false;
      // si el ratón se mueve a otro elemento CON data-page-id (p.ej. de la
      // marca "[[" al texto), no cortar -- solo al salir del todo
      const related = event.relatedTarget;
      if (related instanceof HTMLElement && related.closest("[data-page-id]") === target.closest("[data-page-id]")) {
        return false;
      }
      clearTimeout(timer);
      onHoverEnd?.();
      return false;
    },
  });
}

// ------------------------------------------------------ enlaces Markdown
// "[texto](url)" -- mismo patrón cursor-aware que los wikilinks: resuelto y
// sin editar ahora mismo, se sustituye entero por un widget que se ve como
// un enlace normal (solo el texto, sin corchetes/paréntesis/URL); editando,
// se ve el Markdown crudo con los marcadores tenues. Ctrl/Cmd+clic abre la
// URL en una pestaña nueva. A diferencia de un wikilink no hace falta
// resolver nada contra el árbol de páginas -- el destino ya es la URL.

const MDLINK_RE = /\[([^\]]+)\]\(([^)\s]+)\)/g;

class MarkdownLinkWidget extends WidgetType {
  constructor(label, url) {
    super();
    this.label = label;
    this.url = url;
  }
  eq(other) {
    return other.label === this.label && other.url === this.url;
  }
  toDOM() {
    const el = document.createElement("span");
    el.className = "cm-mdm-mdlink-rendered";
    el.textContent = this.label;
    el.dataset.href = this.url;
    el.title = this.url;
    return el;
  }
  ignoreEvent() {
    return false;
  }
}

function buildMarkdownLinkDecorations(view) {
  const ranges = [];
  for (const { from, to } of view.visibleRanges) {
    const text = view.state.doc.sliceString(from, to);
    MDLINK_RE.lastIndex = 0;
    let m;
    while ((m = MDLINK_RE.exec(text))) {
      const start = from + m.index;
      const end = start + m[0].length;
      const label = m[1];
      const url = m[2];

      // Decoration.replace() que cruce un salto de línea SOLO se puede dar
      // desde un StateField, nunca desde un ViewPlugin (como éste) -- si el
      // texto del enlace tiene un "\n" en medio (p. ej. se escribió sin
      // querer un Enter a mitad del texto entre corchetes), CodeMirror lo
      // rechaza con una excepción sin capturar que se lleva por delante
      // todo el editor. En ese caso se deja siempre en crudo/editable --
      // más feo, pero no rompe nada; se corrige solo en cuanto se quite el
      // salto de línea de en medio.
      if (!m[0].includes("\n") && !selectionIntersects(view.state, start, end)) {
        ranges.push(Decoration.replace({ widget: new MarkdownLinkWidget(label, url) }).range(start, end));
        continue;
      }

      // Editando: texto crudo, con "[", "](", ")" tenues (misma clase que
      // los wikilinks en edición -- visualmente son lo mismo).
      const textStart = start + 1; // tras "["
      const textEnd = textStart + label.length;
      ranges.push(Decoration.mark({ class: "cm-mdm-wikimark" }).range(start, textStart));
      ranges.push(Decoration.mark({ class: "cm-mdm-mdlink-text" }).range(textStart, textEnd));
      ranges.push(Decoration.mark({ class: "cm-mdm-wikimark" }).range(textEnd, end));
    }
  }
  return Decoration.set(ranges, true);
}

function markdownLinkPlugin() {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = buildMarkdownLinkDecorations(view);
      }
      update(update) {
        if (update.docChanged || update.viewportChanged || update.selectionSet) {
          this.decorations = buildMarkdownLinkDecorations(update.view);
        }
      }
    },
    { decorations: (v) => v.decorations }
  );
}

function markdownLinkClickHandler() {
  return EditorView.domEventHandlers({
    mousedown(event) {
      if (!(event.ctrlKey || event.metaKey)) return false;
      const target = event.target;
      if (!(target instanceof HTMLElement)) return false;
      const link = target.closest("[data-href]");
      if (!link) return false;
      const href = link.dataset.href;
      if (!href) return false;
      event.preventDefault();
      window.open(href, "_blank", "noopener,noreferrer");
      return true;
    },
  });
}

// --------------------------------------------------------- flechas tipográficas
// "->" / "<-" / "<->" se ven como →/←/↔ cuando no se están editando (mismo
// mecanismo cursor-aware que los wikilinks). No se tocan dentro de bloques
// de código -- "ptr->campo" en una nota técnica no debería convertirse.

const ARROW_RE = /(<->)|(->)|(<-)/g;
const ARROW_GLYPHS = ["↔", "→", "←"];

class GlyphWidget extends WidgetType {
  constructor(glyph) {
    super();
    this.glyph = glyph;
  }
  eq(other) {
    return other.glyph === this.glyph;
  }
  toDOM() {
    const el = document.createElement("span");
    el.className = "cm-mdm-glyph";
    el.textContent = this.glyph;
    return el;
  }
}

function isInsideCodeBlock(state, pos) {
  return state.field(blockField).blocks.some((b) => b.type === "code" && pos >= b.from && pos <= b.to);
}

function buildArrowDecorations(view) {
  const ranges = [];
  for (const { from, to } of view.visibleRanges) {
    const text = view.state.doc.sliceString(from, to);
    ARROW_RE.lastIndex = 0;
    let m;
    while ((m = ARROW_RE.exec(text))) {
      const start = from + m.index;
      const end = start + m[0].length;
      if (selectionIntersects(view.state, start, end)) continue;
      if (isInsideCodeBlock(view.state, start)) continue;
      const glyphIdx = m[1] ? 0 : m[2] ? 1 : 2;
      ranges.push(Decoration.replace({ widget: new GlyphWidget(ARROW_GLYPHS[glyphIdx]) }).range(start, end));
    }
  }
  return Decoration.set(ranges, true);
}

function arrowPlugin() {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = buildArrowDecorations(view);
      }
      update(update) {
        if (update.docChanged || update.viewportChanged || update.selectionSet) {
          this.decorations = buildArrowDecorations(update.view);
        }
      }
    },
    { decorations: (v) => v.decorations }
  );
}

// ------------------------------------------------- bases dentro de la nota
// (P17.2) Una valla ```mdm-database (ver database-block.js) no se lee como
// código: mientras el cursor NO está dentro se sustituye ENTERA por la tabla
// de la base, y en cuanto entra vuelve el texto crudo editable -- el mismo
// trato que los wikilinks, pero en bloque.
//
// Por eso esto es un StateField y no un ViewPlugin, como hrField: CodeMirror
// no admite un reemplazo que cruce saltos de línea declarado desde un plugin
// ("Decorations that replace line breaks may not be specified via plugins").
// El reemplazo va alineado con líneas enteras (de la apertura al final del
// cierre), así que la valla ocupa exactamente su sitio en el documento.
//
// Nada de esto toca el documento: pintar el bloque (o pasar el cursor por el
// bloque) no cambia una sola letra del `.md` -- no hay `docChanged`, así que
// tampoco hay autoguardado ni un guardado a medias.

class DatabaseBlockWidget extends WidgetType {
  constructor(block, render) {
    super();
    this.from = block.from;
    this.parse = block.parse;
    this.render = render;
  }
  eq(other) {
    // Mismo bloque, mismo sitio y misma app: CodeMirror reutiliza el DOM (y no
    // se vuelve a pedir nada a la API) en cada repintado por mover el cursor.
    return (
      other.render === this.render &&
      other.from === this.from &&
      other.parse.ok === this.parse.ok &&
      other.parse.id === this.parse.id &&
      other.parse.view === this.parse.view &&
      other.parse.title === this.parse.title &&
      other.parse.motivo === this.parse.motivo
    );
  }
  ignoreEvent() {
    return true; // los clics del bloque (encabezado, «abrir») son nuestros
  }
  toDOM(view) {
    const wrap = document.createElement("div");
    wrap.className = "cm-mdm-dblock";
    wrap.dataset.databaseBlock = "1";
    // Aquí no se edita nada: para cambiar la vista o los datos se abre la
    // base entera. Ni una celda con contenteditable dentro del bloque.
    wrap.contentEditable = "false";

    // Un clic en el bloque (menos en lo que ya es un botón, como el
    // encabezado: eso lleva a la base) pone el cursor en la valla, que
    // reaparece en crudo. Con ignoreEvent() este clic no lo ve CodeMirror.
    // Vale también para la tarjeta de error: una valla rota se arregla con el
    // ratón, no solo con las flechas.
    wrap.addEventListener("mousedown", (event) => {
      if (event.target.closest("[data-db-action]")) return;
      event.preventDefault();
      view.dispatch({ selection: { anchor: this.from } });
      view.focus();
    });

    if (!this.parse.ok) {
      // Valla rota (id que no es un uuid, YAML inválido...): la tarjeta dice
      // el motivo y el texto sigue en el documento, así que se puede arreglar.
      const card = document.createElement("div");
      card.className = "cm-mdm-dblock-card";
      card.textContent = databaseErrorText(this.parse);
      wrap.appendChild(card);
      return wrap;
    }

    // Cargando: dos líneas de esqueleto con altura fija. El hueco ya está
    // reservado, así que la nota no pega un salto cuando llega la tabla.
    const skeleton = document.createElement("div");
    skeleton.className = "cm-mdm-dblock-skeleton";
    skeleton.setAttribute("aria-hidden", "true");
    skeleton.append(document.createElement("span"), document.createElement("span"));
    const body = document.createElement("div");
    body.className = "cm-mdm-dblock-body";
    body.setAttribute("aria-busy", "true"); // lo que hay ahora es el esqueleto
    wrap.append(skeleton, body);

    const info = { id: this.parse.id, view: this.parse.view, title: this.parse.title };
    const settle = () => {
      if (skeleton.isConnected) skeleton.remove();
      if (body.isConnected) body.removeAttribute("aria-busy");
    };
    const pending = (() => {
      try {
        return Promise.resolve(this.render(info, body));
      } catch (error) {
        return Promise.reject(error);
      }
    })();
    pending.then(settle, (error) => {
      if (!body.isConnected) return; // el editor ya repintó o se marchó
      settle();
      const card = document.createElement("div");
      card.className = "cm-mdm-dblock-card";
      card.textContent =
        error && error.notFound
          ? databaseNotFoundText(info.title, info.id)
          : `No se pudo pintar la base: ${(error && error.message) || "error"}`;
      body.textContent = "";
      body.appendChild(card);
    });

    return wrap;
  }
}

/** Reemplaza cada valla cerrada que el cursor no toca por su widget.
 *
 * Se recalcula entero con cada cambio de texto o de selección, porque lo que
 * se ve (crudo o pintado) depende de dónde está el cursor. Cambiar de
 * decoración NO mueve el cursor: la selección sigue en el mismo offset y la
 * valla, que nunca salió del documento, reaparece en su sitio.
 *
 * Sin callback (una app que no pinte bases) no se sustituye nada: la valla se
 * queda en crudo, que es lo benigno.
 */
function databaseBlockDecorations(state, render) {
  const ranges = [];
  if (render) {
    const doc = state.doc;
    for (const block of scanDatabaseBlocks(doc.toString())) {
      if (selectionIntersects(state, block.from, block.to)) continue;
      const widget = new DatabaseBlockWidget(block, render);
      ranges.push(
        Decoration.replace({ widget, block: true }).range(
          doc.lineAt(block.from).from,
          doc.lineAt(block.to).to
        )
      );
    }
  }
  return Decoration.set(ranges, true);
}

function databaseBlockField(render) {
  return StateField.define({
    create(state) {
      return databaseBlockDecorations(state, render);
    },
    update(deco, tr) {
      if (tr.docChanged || !tr.state.selection.eq(tr.startState.selection)) {
        return databaseBlockDecorations(tr.state, render);
      }
      return deco;
    },
    provide: (f) => EditorView.decorations.from(f),
  });
}

// ------------------------------------------------------------------ bloques
//
// "Bloque" no es un objeto en el modelo de datos -- sigue siendo el mismo
// body_markdown de siempre. Es un rango de texto calculado a partir del
// árbol de sintaxis que @lezer/markdown ya construye: cada hijo directo del
// documento es un bloque, salvo listas, cuyos ListItem se tratan cada uno
// como un bloque suelto (para poder seleccionar/convertir uno a uno, como
// en Notion). Ver docs/ARCHITECTURE.md para la comparación con un modelo de
// bloques "de verdad".

const BLOCK_TYPES = [
  { type: "paragraph", label: "Párrafo", abbrev: "p" },
  { type: "h1", label: "Título 1", abbrev: "h1" },
  { type: "h2", label: "Título 2", abbrev: "h2" },
  { type: "h3", label: "Título 3", abbrev: "h3" },
  { type: "h4", label: "Título 4", abbrev: "h4" },
  { type: "h5", label: "Título 5", abbrev: "h5" },
  { type: "bullet_list_item", label: "Lista", abbrev: "list" },
  { type: "ordered_list_item", label: "Lista numerada", abbrev: "num" },
  { type: "code", label: "Código", abbrev: "code" },
  { type: "hr", label: "Línea horizontal", abbrev: "horizontal" },
  { type: "quote", label: "Cita", abbrev: "quote" },
];

const BLOCK_TYPE_LABEL = Object.fromEntries(BLOCK_TYPES.map((t) => [t.type, t.label]));

// Comandos del menú "/" que NO son un tipo de bloque convertible -- no
// deben aparecer en el menú "Convertir a" del clic derecho (convertir un
// párrafo ya escrito "a página" no tiene sentido: crear una página es una
// acción con efecto secundario -- una llamada a la API -- no una
// transformación de texto). Por eso viven en una lista aparte y solo se
// añaden a las opciones del menú "/" (ver slashMenuExtension), nunca a
// BLOCK_TYPES.
const SLASH_EXTRA_COMMANDS = [
  { type: "page", label: "Página", abbrev: "page" },
  // "/base" (P17.3): escribe el bloque ```mdm-database de una base que ya
  // existe, o crea una colgando de la nota en la que se está. Como "/page", no
  // es una conversión de bloque: tiene efectos fuera del texto (lista de la
  // API, altas), así que vive aquí y no en BLOCK_TYPES.
  { type: "database", label: "Base", abbrev: "base" },
];
const SLASH_MENU_OPTIONS = [...BLOCK_TYPES, ...SLASH_EXTRA_COMMANDS];

function computeBlocks(state) {
  const blocks = [];
  const root = syntaxTree(state).topNode;
  for (let node = root.firstChild; node; node = node.nextSibling) {
    switch (node.name) {
      case "BlankLine":
      case "CommentBlock":
        break;
      case "BulletList":
      case "OrderedList": {
        const itemType = node.name === "BulletList" ? "bullet_list_item" : "ordered_list_item";
        for (let item = node.firstChild; item; item = item.nextSibling) {
          if (item.name === "ListItem") blocks.push({ from: item.from, to: item.to, type: itemType });
        }
        break;
      }
      case "ATXHeading1":
        blocks.push({ from: node.from, to: node.to, type: "h1" });
        break;
      case "ATXHeading2":
        blocks.push({ from: node.from, to: node.to, type: "h2" });
        break;
      case "ATXHeading3":
        blocks.push({ from: node.from, to: node.to, type: "h3" });
        break;
      case "ATXHeading4":
        blocks.push({ from: node.from, to: node.to, type: "h4" });
        break;
      case "ATXHeading5":
      case "ATXHeading6":
        blocks.push({ from: node.from, to: node.to, type: "h5" });
        break;
      case "HorizontalRule":
        blocks.push({ from: node.from, to: node.to, type: "hr" });
        break;
      case "FencedCode":
      case "CodeBlock":
        blocks.push({ from: node.from, to: node.to, type: "code" });
        break;
      case "Paragraph":
        blocks.push({ from: node.from, to: node.to, type: "paragraph" });
        break;
      case "Blockquote":
        // Un solo bloque para toda la cita, con independencia de cuántos
        // niveles anidados (">", ">>"...) tenga dentro -- solo se recorren
        // los hijos DIRECTOS del documento aquí, así que una cita anidada
        // (Blockquote dentro de otro Blockquote) nunca se cuenta aparte.
        // La profundidad de cada línea se calcula al pintarla (ver
        // quoteDecorations), no aquí.
        blocks.push({ from: node.from, to: node.to, type: "quote" });
        break;
      default:
        // HTMLBlock, Table, LinkReference... siguen siendo
        // seleccionables/borrables, solo no convertibles.
        blocks.push({ from: node.from, to: node.to, type: "other" });
    }
  }
  blocks.sort((a, b) => a.from - b.from);
  return blocks;
}

function stripBlockPrefix(text, type) {
  if (type === "h1" || type === "h2" || type === "h3" || type === "h4" || type === "h5") {
    return text.replace(/^#{1,6}[ \t]+/, "");
  }
  if (type === "bullet_list_item") return text.replace(/^[-*+][ \t]+/, "");
  if (type === "ordered_list_item") return text.replace(/^\d+[.)][ \t]+/, "");
  if (type === "code") return text.replace(/^```[^\n]*\n?/, "").replace(/\n?```[ \t]*$/, "");
  if (type === "hr") return ""; // "---" no tiene contenido que conservar
  if (type === "quote") {
    // Quita TODOS los niveles de ">" de cada línea (una cita anidada tiene
    // ">" repetidos, p. ej. "> > texto") -- convertir "a otra cosa" aplana
    // la cita del todo, no baja un solo nivel.
    return text
      .split("\n")
      .map((line) => line.replace(/^(?:[ \t]*>[ \t]?)+/, ""))
      .join("\n");
  }
  return text;
}

function applyBlockType(plain, targetType) {
  switch (targetType) {
    case "h1":
      return "# " + plain;
    case "h2":
      return "## " + plain;
    case "h3":
      return "### " + plain;
    case "h4":
      return "#### " + plain;
    case "h5":
      return "##### " + plain;
    case "bullet_list_item":
      return "- " + plain;
    case "ordered_list_item":
      return "1. " + plain;
    case "code":
      return "```\n" + plain + "\n```";
    case "hr":
      return "---";
    case "quote":
      return plain
        .split("\n")
        .map((line) => "> " + line)
        .join("\n");
    default:
      return plain;
  }
}

const setSelectedBlocks = StateEffect.define();

const blockField = StateField.define({
  create(state) {
    return { blocks: computeBlocks(state), selected: new Set() };
  },
  update(value, tr) {
    let { blocks, selected } = value;
    if (tr.docChanged) {
      blocks = computeBlocks(tr.state);
      selected = new Set(); // las posiciones ya no son válidas tras editar
    }
    let touchedByUs = false;
    for (const effect of tr.effects) {
      if (effect.is(setSelectedBlocks)) {
        selected = effect.value;
        touchedByUs = true;
      }
    }
    // Clic/cursor normal del usuario en el texto -> sale del "modo bloques"
    if (!touchedByUs && !tr.docChanged && tr.selection && selected.size > 0) {
      selected = new Set();
    }
    return { blocks, selected };
  },
});

// Huecos vacíos que ha creado el editor en MEDIO del documento: el par de
// saltos que añaden el «+» del asa y ⏎ (o ⇧⏎) con bloques marcados cuando
// queda contenido detrás. Mientras el cursor siga dentro son el bloque nuevo
// que se está creando; en cuanto se sale sin escribir nada se colapsan (D8 a),
// igual que los de los extremos -- si no, abandonar ese bloque dejaba tres
// líneas en blanco permanentes en el `.md`, que es justo el aire que (a) dice
// que no existe.
//
// Se marcan SOLO los huecos que crea el editor: un `alfa\n\n\n\nbeta` escrito a
// mano (o pegado) no lleva marca y se guarda tal cual, que es lo que ya hacía
// el backend al escribir. La regla de cuándo sigue siendo un hueco vacío está
// en `emptyGapRange` (body.js).
const addEmptyGap = StateEffect.define();

const pendingEmptyGaps = StateField.define({
  create: () => [],
  update(gaps, tr) {
    const next = [];
    // Las marcas viejas se mueven con las ediciones. La regla está en
    // `shiftEmptyGap` (body.js, con sus pruebas): lo que se inserta justo en
    // el borde de delante del hueco se queda ANTES del par y la marca se
    // desplaza con él -- si no, un segundo «+» en el mismo bloque tiraba la
    // marca y el aire volvía al `.md`. Si una edición se lleva el par, la
    // marca deja de medir 2 y se descarta.
    let changes = null;
    for (const gap of gaps) {
      if (!tr.docChanged) {
        next.push(gap);
        continue;
      }
      if (!changes) {
        changes = [];
        tr.changes.iterChanges((from, to, fromB, toB, inserted) => {
          changes.push({ from, to, insert: inserted.toString() });
        });
      }
      const moved = shiftEmptyGap(gap, changes);
      if (moved) next.push(moved);
    }
    for (const effect of tr.effects) {
      if (!effect.is(addEmptyGap)) continue;
      const from = effect.value;
      const to = from + 2;
      // Al final del documento no hay bloque vacío que quitar: ese hueco es
      // de los extremos y de esos se encarga `dropOuterBlankLines`.
      if (to >= tr.newDoc.length) continue;
      if (tr.newDoc.sliceString(from, to) !== "\n\n") continue;
      // Y que sea un bloque vacío de verdad, no el hueco que deja partir un
      // párrafo: el cursor tiene que haber quedado DENTRO del tramo de saltos.
      // Con ⏎ al principio de una línea de un párrafo, los dos saltos nuevos
      // quedan encima del texto y el cursor se va al texto de debajo; ahí no
      // hay bloque nuevo que quitar.
      const run = newlineRunInDoc(tr.newDoc, from + 1);
      if (!run) continue;
      if (!tr.state.selection.ranges.some((r) => r.from < run.to && r.to > run.from)) continue;
      next.push({ from, to });
    }
    return next;
  },
});

/** Efecto que marca el bloque vacío que acaba de insertar un comando, si lo
 * que inserta es el par de saltos de un bloque nuevo. Partir un párrafo por
 * la mitad no deja marca: ahí los dos saltos separan texto que ya existía, y
 * quitarlos al salir pegaría las dos mitades otra vez. */
function emptyGapEffect(from, insert = "\n\n") {
  return insert === "\n\n" ? [addEmptyGap.of(from)] : [];
}

// El handle de cada bloque NO es un gutter de CodeMirror: se probó primero
// con gutter() (como lineNumbers()) y las filas no se alineaban con el
// contenido real -- un gutter asume altura de línea uniforme, y aquí los
// encabezados son más altos que un párrafo normal, así que hasta el propio
// lineNumbers() de CodeMirror queda desalineado con nuestro contenido. La
// alternativa que sí alinea siempre: el handle es un widget colocado DENTRO
// de la propia línea (mismo elemento DOM que la envuelve), sacado visualmente
// al margen izquierdo con position:absolute -- como es hijo real de esa
// línea, hereda su altura exacta sin ningún cálculo aparte.
class BlockHandleWidget extends WidgetType {
  constructor(index, isSelected) {
    super();
    this.index = index;
    this.isSelected = isSelected;
  }
  eq(other) {
    return other.index === this.index && other.isSelected === this.isSelected;
  }
  toDOM() {
    const wrap = document.createElement("span");
    wrap.className = "cm-mdm-block-handle-wrap";

    const addBtn = document.createElement("span");
    addBtn.className = "cm-mdm-block-add";
    addBtn.textContent = "+";
    addBtn.dataset.blockIndex = String(this.index);
    addBtn.title = "Añadir un bloque después de este";

    const handle = document.createElement("span");
    handle.className = "cm-mdm-block-handle" + (this.isSelected ? " cm-mdm-block-handle-selected" : "");
    handle.textContent = "⋮⋮";
    handle.dataset.blockIndex = String(this.index);
    handle.title = "Clic: seleccionar bloque · Mayús/Ctrl+clic: varios · clic derecho: acciones";

    wrap.append(addBtn, handle);
    return wrap;
  }
  ignoreEvent() {
    return false; // que el mousedown/contextmenu le lleguen a nuestros handlers
  }
}

function blockHandleDecorations(state) {
  const { blocks, selected } = state.field(blockField);
  const ranges = blocks.map((b, i) =>
    Decoration.widget({ widget: new BlockHandleWidget(i, selected.has(i)), side: -1 }).range(b.from)
  );
  return Decoration.set(ranges, true);
}

function blockHandlesPlugin() {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = blockHandleDecorations(view.state);
      }
      update(update) {
        if (update.docChanged || update.state.field(blockField) !== update.startState.field(blockField)) {
          this.decorations = blockHandleDecorations(update.state);
        }
      }
    },
    { decorations: (v) => v.decorations }
  );
}

function blockHandleEventHandlers(onBlockContextMenu) {
  return EditorView.domEventHandlers({
    // ---- pulsación larga (pantalla táctil) ----
    // En el móvil no hay clic derecho ni `Supr`: sin esta puerta no había
    // manera de abrir el menú de bloque (convertir/duplicar/eliminar). La
    // pulsación larga sobre el asa abre el mismo menú que el clic derecho; el
    // toque corto sigue seleccionando el bloque como siempre. No se consume el
    // evento: el mousedown de después (el que selecciona) tiene que seguir
    // llegando, y con ratón esto ni se activa.
    pointerdown(event, view) {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return false;
      const handle = target.closest(".cm-mdm-block-handle");
      if (!handle) return false;
      if (event.pointerType === "mouse") return false;
      cancelLongPress();
      longPressOrigin = { x: event.clientX, y: event.clientY };
      longPressTimer = window.setTimeout(() => {
        longPressTimer = null;
        longPressOrigin = null;
        const idx = Number(handle.dataset.blockIndex);
        const { selected } = view.state.field(blockField);
        const indices = selected.has(idx) ? selected : new Set([idx]);
        if (indices !== selected) selectBlocks(view, indices);
        // El clic sintético que el navegador manda al soltar el dedo llegaría
        // al `document` y cerraría el menú recién abierto (ver `click`).
        suppressTapUntil = Date.now() + 1200;
        const rect = handle.getBoundingClientRect();
        openBlockMenu(view, indices, rect.left, rect.bottom, onBlockContextMenu);
        swallowTapAfterMenu();
      }, LONG_PRESS_MS);
      return false;
    },
    pointermove(event) {
      if (
        longPressOrigin &&
        Math.hypot(event.clientX - longPressOrigin.x, event.clientY - longPressOrigin.y) > LONG_PRESS_SLOP
      ) {
        cancelLongPress(); // se está desplazando, no pulsando
      }
      return false;
    },
    pointerup() {
      cancelLongPress();
      return false;
    },
    pointercancel() {
      cancelLongPress();
      return false;
    },
    mousedown(event, view) {
      const target = event.target;
      if (target instanceof HTMLElement) {
        const addBtn = target.closest(".cm-mdm-block-add");
        if (addBtn) {
          event.preventDefault();
          insertBlockAfter(view, Number(addBtn.dataset.blockIndex));
          return true;
        }

        const handle = target.closest(".cm-mdm-block-handle");
        if (handle) {
          event.preventDefault();
          // Si acaba de abrirse el menú con un toque largo, el toque que lo
          // abrió no tiene que volver a marcar ni a llevarse el foco del menú.
          if (Date.now() < suppressTapUntil) return true;
          // El mousedown también llega con el botón derecho. Ahí no se toca la
          // selección: el menú contextual actúa sobre lo que ya estuviera
          // marcado (antes, el clic derecho la reducía a ese único bloque y
          // «Eliminar bloque» borraba uno solo con dos marcados).
          if (event.button !== 0) return true;

          const idx = Number(handle.dataset.blockIndex);
          const { selected } = view.state.field(blockField);

          let next;
          if (event.shiftKey && selected.size > 0) {
            const last = Math.max(...selected);
            const [lo, hi] = idx < last ? [idx, last] : [last, idx];
            next = new Set();
            for (let i = lo; i <= hi; i++) next.add(i);
          } else if (event.ctrlKey || event.metaKey) {
            next = new Set(selected);
            next.has(idx) ? next.delete(idx) : next.add(idx);
          } else {
            next = new Set([idx]);
          }
          selectBlocks(view, next);
          // El mousedown del asa va con preventDefault (para no mover el
          // caret), así que el navegador NO da el foco al editor: sin esto,
          // marcar un bloque con el asa y pulsar Supr no hacía nada porque la
          // tecla no llegaba al editor. El foco deja la selección donde la
          // puso selectBlocks (el bloque marcado), que es donde tiene que
          // escribir y borrar el teclado.
          view.focus();
          return true;
        }
      }

      // Clic en la zona vacía bajo el último bloque (el padding-bottom de
      // .cm-content, puesto ahí a propósito -- ver el comentario en
      // index.html) -- en vez de dejar el cursor "colgado" al final del
      // último bloque existente, genera uno nuevo listo para escribir,
      // como clicar debajo del contenido en Notion. Solo se activa si el
      // clic resuelve al final del documento Y está claramente por debajo
      // de la última línea (si no, sería un clic normal dentro del propio
      // texto, que debe comportarse como siempre).
      const pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
      if (pos === null || pos !== view.state.doc.length) return false;
      const coords = view.coordsAtPos(pos, -1);
      if (!coords || event.clientY <= coords.bottom + 4) return false;
      event.preventDefault();

      const doc = view.state.doc;
      const lastLine = doc.lineAt(pos);
      if (lastLine.length === 0) {
        view.dispatch({ selection: { anchor: pos } });
      } else {
        view.dispatch({ changes: { from: pos, to: pos, insert: "\n\n" }, selection: { anchor: pos + 2 } });
      }
      view.focus();
      return true;
    },
    // Corrección de un clic mal resuelto cerca de un widget de bloque (la
    // línea horizontal, y en general cualquier línea de continuación de un
    // bloque de varias líneas justo debajo de uno) -- issue #4 en GitHub,
    // confirmado por el usuario que sigue pasando específicamente con un
    // "---" encima. El cálculo interno de CodeMirror por coordenadas
    // (posAtCoords, el que usa por defecto al procesar el propio clic) no
    // siempre acierta ahí; en vez de intentar reproducir/parchear ese
    // cálculo, se compara su resultado con el de la API del navegador
    // pensada exactamente para esto (caretPositionFromPoint /
    // caretRangeFromPoint, que hacen su propio hit-test sobre el DOM ya
    // renderizado) y se corrige si no coinciden. Se hace en "click" (tras
    // el mouseup, no en el propio mousedown) y solo si la selección sigue
    // colapsada -- si hubo arrastre para seleccionar texto, se deja tal
    // cual, para no romper esa selección.
    click(event, view) {
      // El clic que el navegador sintetiza al levantar el dedo tras una
      // pulsación larga llegararía al `document` y cerraría el menú que acaba
      // de abrirse. Se traga ese clic y solo ese: el del asa, dentro de la
      // ventana de la pulsación.
      if (
        Date.now() < suppressTapUntil &&
        event.target instanceof HTMLElement &&
        event.target.closest(".cm-mdm-block-handle")
      ) {
        event.preventDefault();
        event.stopPropagation();
        return true;
      }
      // Un clic en el asa ya lo ha resuelto el mousedown: esto solo corrige el
      // caret del texto, y aplicarlo encima de una selección de bloques la
      // borraría (cualquier cambio de selección que no sea nuestro vacía
      // `selected`, ver blockField).
      if (event.target instanceof HTMLElement && event.target.closest(".cm-mdm-block-handle-wrap")) {
        return false;
      }
      if (!view.state.selection.main.empty) return false;
      let domPos = null;
      if (document.caretPositionFromPoint) {
        const r = document.caretPositionFromPoint(event.clientX, event.clientY);
        if (r) domPos = view.posAtDOM(r.offsetNode, r.offset);
      } else if (document.caretRangeFromPoint) {
        const r = document.caretRangeFromPoint(event.clientX, event.clientY);
        if (r) domPos = view.posAtDOM(r.startContainer, r.startOffset);
      }
      if (domPos === null || domPos === undefined || Number.isNaN(domPos)) return false;
      if (domPos === view.state.selection.main.head) return false; // ya estaba bien
      view.dispatch({ selection: { anchor: domPos } });
      return false;
    },
    contextmenu(event, view) {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return false;
      const handle = target.closest(".cm-mdm-block-handle");
      if (!handle) return false;
      event.preventDefault();
      // Si el navegador (Android) sintetiza un `contextmenu` en la misma
      // pulsación larga que ya abrió el menú, el temporizador sobra.
      cancelLongPress();

      const idx = Number(handle.dataset.blockIndex);
      const { selected } = view.state.field(blockField);
      // El clic derecho actúa sobre lo que ya estuviera marcado (con dos asas
      // marcadas no se reduce a una); con un solo bloque, sobre ese.
      const indices = selected.has(idx) ? selected : new Set([idx]);
      if (indices !== selected) selectBlocks(view, indices);
      openBlockMenu(view, indices, event.clientX, event.clientY, onBlockContextMenu);
      return true;
    },
  });
}

// ---- puerta única del menú de bloque ----
// El clic derecho del escritorio y la pulsación larga del móvil abren
// exactamente lo mismo. `indices` es lo que el menú va a tocar.
const LONG_PRESS_MS = 500;
const LONG_PRESS_SLOP = 10;
let longPressTimer = null;
let longPressOrigin = null;
let suppressTapUntil = 0;

function cancelLongPress() {
  if (longPressTimer !== null) {
    clearTimeout(longPressTimer);
    longPressTimer = null;
  }
  longPressOrigin = null;
}

// El dedo sigue apoyado cuando el menú se abre, y el menú se recoloca para
// caber en la pantalla -- puede quedar justo debajo del dedo. Al soltar, el
// navegador manda un clic sobre lo que haya ahí, que activaría una opción del
// menú sin querer (convertir el bloque a párrafo, por ejemplo). Se traga ese
// clic: el que cierra la pulsación que abrió el menú. El siguiente toque (su
// `pointerdown`) quita la valla, así que elegir una opción con un toque nuevo
// funciona igual. Con ratón no se instala nada.
function swallowTapAfterMenu() {
  let timer = null;
  const drop = () => {
    window.removeEventListener("click", onTap, true);
    window.removeEventListener("pointerdown", drop, true);
    window.removeEventListener("touchstart", drop, true);
    window.removeEventListener("keydown", drop, true);
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };
  const onTap = (event) => {
    event.preventDefault();
    event.stopPropagation();
    drop();
  };
  window.addEventListener("click", onTap, true);
  window.addEventListener("pointerdown", drop, true);
  window.addEventListener("touchstart", drop, true);
  // Teclado (Escape, Tab, Enter) también quita la valla: con teclado no hay
  // clic sintético que tragarse.
  window.addEventListener("keydown", drop, true);
  timer = window.setTimeout(drop, 5000);
}

function openBlockMenu(view, indices, x, y, onBlockContextMenu) {
  if (!onBlockContextMenu) return;
  const { blocks } = view.state.field(blockField);
  // Si es un único bloque, se puede decir de qué tipo es ya mismo; con varios
  // seleccionados (posiblemente de tipos distintos) no.
  const currentTypeLabel =
    indices.size === 1 ? BLOCK_TYPE_LABEL[blocks[[...indices][0]].type] ?? null : null;

  onBlockContextMenu({
    x,
    y,
    count: indices.size,
    currentTypeLabel,
    types: BLOCK_TYPES,
    convertTo: (type) => convertBlocks(view, indices, type),
    duplicateBlocks: () => duplicateBlocks(view, indices),
    deleteBlocks: () => deleteBlocks(view, indices),
  });
}

function selectBlocks(view, indices) {
  const { blocks } = view.state.field(blockField);
  const ordered = [...indices].sort((a, b) => a - b);
  const ranges = ordered.map((i) => EditorSelection.range(blocks[i].from, blocks[i].to));
  view.dispatch({
    effects: setSelectedBlocks.of(indices),
    selection: ranges.length ? EditorSelection.create(ranges, ranges.length - 1) : undefined,
    // Marcar un bloque no es escribir: si esto entrara en el historial, cada
    // ⌘⇧Z tendría que gastar una pulsación en la selección de por medio antes
    // de llegar al borrado que de verdad se quiere rehacer.
    annotations: Transaction.addToHistory.of(false),
  });
}

// Botón "+" del asa: añade un bloque de párrafo vacío justo después del
// bloque indicado (no necesariamente donde esté el cursor ahora mismo --
// el usuario puede clicar el "+" de cualquier bloque). Mismo mecanismo que
// Mayús+Enter (insertar "\n\n"): el hueco en blanco resultante se ve
// colapsado por blankLinePlugin hasta que el cursor entra en él.
function insertBlockAfter(view, index) {
  const { blocks } = view.state.field(blockField);
  const b = blocks[index];
  if (!b) return;
  const pos = b.to;
  view.dispatch({
    changes: { from: pos, to: pos, insert: "\n\n" },
    selection: { anchor: pos + 2 },
    effects: [setSelectedBlocks.of(new Set()), ...emptyGapEffect(pos)],
  });
  view.focus();
}

// Borrar bloque = quitar el bloque Y su separador, para que el `.md` no gane
// líneas en blanco (D1) ni una lista se parta en dos al perder un ítem (D2).
// Con varios bloques marcados se borran TODOS de una vez: la selección
// múltiple se pinta, así que tiene que actuar (D3) -- y es el mismo camino
// para el asa+Supr, para Retroceso y para el menú «Eliminar bloque».
// Qué trozo exacto desaparece se decide en block-delete.js (lógica pura, con
// sus pruebas en node --test); aquí solo se aplica.
function deleteBlocks(view, indices) {
  const { blocks } = view.state.field(blockField);
  const text = view.state.doc.toString();
  const edit = blockDeleteEdits({ text, blocks, indices });
  if (!edit) return false;
  const changes = [...edit.changes, ...orderedRenumberEdits({ text, blocks, indices })];
  const set = ChangeSet.of(changes, text.length);
  view.dispatch({
    changes: set,
    selection: { anchor: set.mapPos(edit.cursor, 1) },
    effects: setSelectedBlocks.of(new Set()),
    userEvent: "delete.block",
    scrollIntoView: true,
  });
  view.focus();
  return true;
}

// «Duplicar bloque» del menú: una copia de lo marcado justo debajo. La copia
// se inserta y, si cae en una lista numerada, la lista se reenumera -- todo en
// UNA transacción, para que un ⌘Z deshaga la duplicación entera y no medio
// cambio (los cambios de número se componen sobre el de inserción).
function duplicateBlocks(view, indices) {
  const { blocks } = view.state.field(blockField);
  const text = view.state.doc.toString();
  const changes = duplicateBlockEdits({ text, blocks, indices });
  if (!changes.length) return false;

  const inserted = ChangeSet.of(changes, text.length);
  const after = view.state.update({ changes: inserted });
  const nextBlocks = after.state.field(blockField).blocks;
  const nextText = after.state.doc.toString();

  const renumber = [];
  let shift = 0;
  for (const change of changes) {
    // `copy` viene en coordenadas del texto original; cada inserción anterior
    // corre la posición lo que ocupe su propio texto.
    const at = change.copy + shift;
    shift += change.insert.length;
    const copyIndex = nextBlocks.findIndex((b) => b.from === at);
    if (copyIndex !== -1) {
      renumber.push(...renumberOrderedRun({ text: nextText, blocks: nextBlocks, index: copyIndex }));
    }
  }

  const set = renumber.length
    ? inserted.compose(ChangeSet.of(renumber, after.state.doc.length))
    : inserted;
  view.dispatch({ changes: set, userEvent: "input.duplicate", scrollIntoView: true });
  return true;
}

// Escribir con bloques marcados sustituye a TODOS los marcados (D3), no solo
// al que lleva la selección de texto: el texto entra en el sitio del primer
// bloque marcado, los separadores de alrededor se conservan y el resto de
// bloques marcados desaparece entero (con su separador, como deleteBlocks).
function replaceBlocks(view, indices, inserted) {
  const { blocks } = view.state.field(blockField);
  const text = view.state.doc.toString();
  const edit = blockDeleteEdits({ text, blocks, indices, insert: inserted });
  if (!edit) return false;
  // Si el texto hereda el marcador del ítem (`2. X`) ese bloque no ha
  // desaparecido: la renumeración no puede contarlo como borrado.
  const changes = [
    ...edit.changes,
    ...orderedRenumberEdits({ text, blocks, indices, keepFirst: edit.keepsFirstItem }),
  ];
  const set = ChangeSet.of(changes, text.length);
  view.dispatch({
    changes: set,
    selection: { anchor: set.mapPos(edit.cursor, 1) },
    effects: setSelectedBlocks.of(new Set()),
    userEvent: "input.type",
    scrollIntoView: true,
  });
  return true;
}

/** Supr/Retroceso con bloques marcados. `false` si no hay nada marcado: la
 * tecla sigue su curso normal dentro del texto. */
function deleteSelectedBlocks(view) {
  const { selected } = view.state.field(blockField);
  if (!selected.size) return false;
  return deleteBlocks(view, [...selected]);
}

/**
 * El texto que se teclea (o se pega) pasa por aquí antes que CodeMirror: con
 * bloques marcados se aplica a todos a la vez. Devuelve `false` cuando no hay
 * bloques marcados, para no tocar la escritura normal.
 */
function typingOverBlocks() {
  return EditorView.inputHandler.of((view, from, to, inserted) => {
    const { selected } = view.state.field(blockField);
    if (!selected.size || !inserted) return false;
    return replaceBlocks(view, [...selected], inserted);
  });
}

/**
 * ⌫ al principio de un ítem de lista (D9): fusiona con el ítem de arriba
 * dejando un espacio (`- uno dos`), nunca `- uno- dos`. Si el ítem es el
 * primero de la lista, el marcador desaparece y el texto sube a párrafo.
 * Va antes del Backspace de Markdown, que si no llega a tiempo pega las dos
 * frases; si esta regla no aplica devuelve `false` y decide aquel.
 *
 * Antes de fusionar nada, la valla manda: dentro de un bloque de código, ⌫ en
 * la primera línea de contenido o en la de cierre (y en la de apertura cuando
 * arriba hay texto) se consume sin tocar el texto (`return true`, sin
 * transacción). Si no, el Backspace por defecto unía esa fila con la de arriba
 * y el `.md` perdía el bloque de código.
 *
 * Se exporta para poder probar el comando del keymap tal cual (ver
 * test/backspace-fence.test.js): el verde de la función pura no cazaba esto,
 * porque quien rompía la valla era el Backspace de después.
 */
export function runBackspaceJoin(view) {
  const range = view.state.selection.main;
  if (!range.empty) return false;
  const text = view.state.doc.toString();
  const blocks = view.state.field(blockField).blocks;
  if (backspaceWouldBreakFence({ text, pos: range.head, blocks })) return true;
  const edit = backspaceJoinEdit({ text, pos: range.head, blocks });
  if (!edit) return false;
  view.dispatch({
    changes: { from: edit.from, to: edit.to, insert: edit.insert },
    selection: { anchor: edit.cursor },
    userEvent: "delete.backward",
    scrollIntoView: true,
  });
  return true;
}

function convertBlocks(view, indices, targetType) {
  const { blocks } = view.state.field(blockField);
  const text = view.state.doc.toString();
  // La misma regla de frontera que al borrar (ver block-delete.js): lo
  // convertido queda como bloque propio, con su línea en blanco si deja de ser
  // un ítem de lista -- si no, Markdown leería la primera línea como
  // continuación del ítem de arriba.
  const changes = convertBlockEdits({
    text,
    blocks,
    indices,
    convert: (raw, block) => applyBlockType(stripBlockPrefix(raw, block.type), targetType),
  });
  if (!changes.length) return;
  view.dispatch({ changes, effects: setSelectedBlocks.of(new Set()) });
  view.focus();
}

function blockLineDecorations(view) {
  const { blocks } = view.state.field(blockField);
  const ranges = [];
  const doc = view.state.doc;
  for (const b of blocks) {
    if (b.type !== "code" && b.type !== "bullet_list_item" && b.type !== "ordered_list_item") continue;
    const cls = b.type === "code" ? "cm-mdm-line-code" : "cm-mdm-line-list";
    let pos = b.from;
    while (pos <= b.to) {
      const line = doc.lineAt(pos);
      ranges.push(Decoration.line({ class: cls }).range(line.from));
      if (line.to >= b.to) break;
      pos = line.to + 1;
    }
  }
  return Decoration.set(ranges, true);
}

function blockLinesPlugin() {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = blockLineDecorations(view);
      }
      update(update) {
        if (update.docChanged || update.state.field(blockField) !== update.startState.field(blockField)) {
          this.decorations = blockLineDecorations(update.view);
        }
      }
    },
    { decorations: (v) => v.decorations }
  );
}

// ------------------------------------------------------------------ citas
//
// Una cita ("> texto", con niveles anidados vía "> > texto") es UN bloque
// (ver computeBlocks) con independencia de su profundidad interna -- la
// profundidad se calcula aquí, línea a línea, contando los "> " de cada
// una. Mismo patrón cursor-aware que headers/listas/hr: mientras se edita
// el bloque, el Markdown va en crudo (el ">" se ve tenue, vía
// syntaxMarkDecorations); en cuanto se deja de editar, aparece la barra
// (color de acento) y el fondo gris con bordes redondeados -- una barra
// por nivel de anidación, así que una cita dentro de otra se nota.
class QuoteBarsWidget extends WidgetType {
  constructor(depth) {
    super();
    this.depth = depth;
  }
  eq(other) {
    return other.depth === this.depth;
  }
  toDOM() {
    const wrap = document.createElement("span");
    wrap.className = "cm-mdm-quote-bars";
    for (let i = 0; i < this.depth; i++) {
      const bar = document.createElement("span");
      bar.className = "cm-mdm-quote-bar";
      wrap.appendChild(bar);
    }
    return wrap;
  }
  ignoreEvent() {
    return true; // puramente decorativo -- que los clics le lleguen al texto/línea, no al widget
  }
}

function quoteDepthOf(lineText) {
  const m = lineText.match(/^(?:[ \t]*>)+/);
  if (!m) return 0;
  return (m[0].match(/>/g) || []).length;
}

function quoteDecorations(state) {
  const { blocks } = state.field(blockField);
  const ranges = [];
  const doc = state.doc;
  for (const b of blocks) {
    if (b.type !== "quote") continue;
    if (selectionIntersects(state, b.from, b.to)) continue; // editando: Markdown crudo, sin barras ni fondo

    let pos = b.from;
    while (pos <= b.to) {
      const line = doc.lineAt(pos);
      const depth = Math.max(1, quoteDepthOf(line.text));
      const isFirst = line.from <= b.from;
      const isLast = line.to >= b.to;
      let cls = "cm-mdm-line-quote";
      if (isFirst) cls += " cm-mdm-line-quote-first";
      if (isLast) cls += " cm-mdm-line-quote-last";
      ranges.push(Decoration.line({ class: cls }).range(line.from));
      ranges.push(Decoration.widget({ widget: new QuoteBarsWidget(depth), side: -1 }).range(line.from));
      if (line.to >= b.to) break;
      pos = line.to + 1;
    }
  }
  return Decoration.set(ranges, true);
}

function quotePlugin() {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = quoteDecorations(view.state);
      }
      update(update) {
        if (
          update.docChanged ||
          update.selectionSet ||
          update.state.field(blockField) !== update.startState.field(blockField)
        ) {
          this.decorations = quoteDecorations(update.state);
        }
      }
    },
    { decorations: (v) => v.decorations }
  );
}

// Separación visual real entre bloques (al estilo Notion: un H1 deja más
// aire por encima que un párrafo suelto), no solo "que no se toquen". Se
// aplica como padding-top a la PRIMERA línea de cada bloque (según su
// tipo) -- nunca al primero de la nota (ya tiene el padding de
// .cm-content) ni entre ítems consecutivos de una misma lista (deben
// quedar juntos, es una lista). Es viable sin miedo a romper las flechas
// arriba/abajo porque ya no dependen de la altura en píxeles de cada línea
// (ver verticalMoveExtension) -- antes esto habría empeorado ese bug.
const LIST_BLOCK_TYPES = new Set(["bullet_list_item", "ordered_list_item"]);
const BLOCK_SPACING_CLASS = {
  h1: "cm-mdm-space-h1",
  h2: "cm-mdm-space-h2",
  h3: "cm-mdm-space-h3",
  h4: "cm-mdm-space-h4",
  h5: "cm-mdm-space-h5",
};

function blockSpacingDecorations(state) {
  const { blocks } = state.field(blockField);
  const ranges = [];
  blocks.forEach((b, i) => {
    if (i === 0) return;
    const prev = blocks[i - 1];
    if (LIST_BLOCK_TYPES.has(b.type) && LIST_BLOCK_TYPES.has(prev.type)) return;
    const cls = BLOCK_SPACING_CLASS[b.type] || "cm-mdm-space-normal";
    ranges.push(Decoration.line({ class: cls }).range(b.from));
  });
  return Decoration.set(ranges, true);
}

function blockSpacingPlugin() {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = blockSpacingDecorations(view.state);
      }
      update(update) {
        if (update.docChanged || update.state.field(blockField) !== update.startState.field(blockField)) {
          this.decorations = blockSpacingDecorations(update.state);
        }
      }
    },
    { decorations: (v) => v.decorations }
  );
}

// ------------------------------------------------ marcadores de sintaxis
//
// Al estilo Obsidian/Typora: el bloque que se está editando (el cursor está
// dentro de su rango, según blockField) muestra el Markdown crudo con sus
// marcadores tenues -- "# ", "**", "`", "1. "... Cualquier otro bloque los
// esconde del todo (no solo atenuados): "**Tests**" pasa a verse "Tests" en
// negrita, sin asteriscos. La única excepción es ListMark ("1.", "-"): no
// se esconde -- sin él no quedaría ninguna pista visual de que es una lista
// -- pero pasa de tenue a color de texto normal en cuanto se deja de editar
// ese bloque, para que no parezca "syntax" sino contenido real.
//
// Antes esto lo resolvía una regla fija en highlightStyle (todo marcador
// siempre tenue); ahora depende de qué bloque está activo, así que hace
// falta recorrer el árbol de sintaxis por bloque en cada actualización en
// vez de una regla estática.

const MUTED_MARK_TYPES = new Set(["HeaderMark", "EmphasisMark", "CodeMark", "ListMark", "QuoteMark"]);
const HIDE_WHEN_INACTIVE = new Set(["HeaderMark", "EmphasisMark", "CodeMark", "QuoteMark"]);

function syntaxMarkDecorations(state) {
  const { blocks } = state.field(blockField);
  const ranges = [];
  const tree = syntaxTree(state);
  for (const b of blocks) {
    const active = selectionIntersects(state, b.from, b.to);
    tree.iterate({
      from: b.from,
      to: b.to,
      enter: (node) => {
        if (!MUTED_MARK_TYPES.has(node.name)) return;
        if (active) {
          ranges.push(Decoration.mark({ class: "cm-mdm-mark" }).range(node.from, node.to));
          return;
        }
        if (!HIDE_WHEN_INACTIVE.has(node.name)) return; // ListMark inactivo: sin decoración -> color normal
        let end = node.to;
        if (node.name === "HeaderMark" || node.name === "QuoteMark") {
          // consumir el espacio tras "#"/"##"... o ">"/">>"... -- si no,
          // queda un hueco donde estaban. Un QuoteMark nunca cruza una
          // línea (Lezer da uno por nivel de anidación en cada línea), así
          // que este Decoration.replace() nunca se topa con un "\n".
          while (end < b.to && /[ \t]/.test(state.doc.sliceString(end, end + 1))) end++;
        }
        ranges.push(Decoration.replace({}).range(node.from, end));
      },
    });
  }
  return Decoration.set(ranges, true);
}

function syntaxMarkPlugin() {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = syntaxMarkDecorations(view.state);
      }
      update(update) {
        if (
          update.docChanged ||
          update.selectionSet ||
          update.state.field(blockField) !== update.startState.field(blockField)
        ) {
          this.decorations = syntaxMarkDecorations(update.state);
        }
      }
    },
    { decorations: (v) => v.decorations }
  );
}

// Un párrafo necesita una línea en blanco detrás para separarse del
// siguiente en Markdown -- pero verla como un hueco real (una fila entera
// vacía) hace que los bloques parezcan tener aire de más entre ellos, cosa
// que no pasa en un editor de bloques al estilo Notion. Se colapsa su
// altura a casi nada mientras el cursor no esté ahí (mismo patrón
// cursor-aware que wikilinks/flechas/línea horizontal); en cuanto el
// cursor entra -- tras Mayús+Enter, o al clicar ahí -- vuelve a su altura
// normal para poder escribir con comodidad. Dentro de un bloque de código
// no se toca: ahí una línea en blanco es contenido de verdad, no un
// separador. Nota: es una decoración de línea (Decoration.line), no de
// bloque -- a diferencia de hrField, esas sí puede darlas un ViewPlugin.
function blankLineDecorations(view) {
  const ranges = [];
  const { state } = view;
  const doc = state.doc;
  for (const { from, to } of view.visibleRanges) {
    const firstLine = doc.lineAt(from).number;
    const lastLine = doc.lineAt(to).number;
    for (let n = firstLine; n <= lastLine; n++) {
      const line = doc.line(n);
      if (line.length !== 0) continue;
      if (selectionIntersects(state, line.from, line.to)) continue;
      if (isInsideCodeBlock(state, line.from)) continue;
      ranges.push(Decoration.line({ class: "cm-mdm-blank-collapsed" }).range(line.from));
    }
  }
  return Decoration.set(ranges, true);
}

function blankLinePlugin() {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = blankLineDecorations(view);
      }
      update(update) {
        if (update.docChanged || update.viewportChanged || update.selectionSet) {
          this.decorations = blankLineDecorations(update.view);
        }
      }
    },
    { decorations: (v) => v.decorations }
  );
}

// Línea horizontal ("---"): igual que wikilinks/flechas, se ve como una
// línea de verdad (widget) salvo mientras se está editando ese bloque en
// concreto -- si no, no habría forma de borrarla con Backspace con comodidad.
class HRWidget extends WidgetType {
  eq(other) {
    return other instanceof HRWidget;
  }
  toDOM() {
    const el = document.createElement("div");
    el.className = "cm-mdm-hr";
    return el;
  }
}

function hrDecorations(state) {
  const { blocks } = state.field(blockField);
  const ranges = [];
  for (const b of blocks) {
    if (b.type !== "hr") continue;
    if (selectionIntersects(state, b.from, b.to)) continue;
    ranges.push(Decoration.replace({ widget: new HRWidget(), block: true }).range(b.from, b.to));
  }
  return Decoration.set(ranges, true);
}

// Nota: esto tiene que ser un StateField, no un ViewPlugin -- CodeMirror no
// admite decoraciones de bloque (block: true) declaradas desde un plugin,
// solo desde un campo de estado ("Block decorations may not be specified
// via plugins"). blockField ya está definido arriba en el archivo, así que
// puede leerse aquí sin problema (los StateField se resuelven por orden de
// aparición en `extensions`, y éste va después).
const hrField = StateField.define({
  create(state) {
    return hrDecorations(state);
  },
  update(deco, tr) {
    if (
      tr.docChanged ||
      !tr.state.selection.eq(tr.startState.selection) ||
      tr.state.field(blockField) !== tr.startState.field(blockField)
    ) {
      return hrDecorations(tr.state);
    }
    return deco;
  },
  provide: (f) => EditorView.decorations.from(f),
});

// --------------------------------------------------------- menú "/" (slash)
//
// Al escribir "/" en una línea vacía se activa un menú de tipos de bloque,
// filtrable escribiendo a continuación (por abreviatura o por palabra),
// navegable con flechas arriba/abajo, Enter para confirmar, Escape para
// cancelar. Elegir una opción borra "/consulta" y pone el prefijo Markdown
// del tipo elegido (o abre el bloque de código con el cursor dentro, o dos
// bloques nuevos tras una línea horizontal).
//
// El estado del menú vive en la instancia del ViewPlugin, no en un
// StateField -- es UI efímera, no algo que deba formar parte del documento
// ni del historial de deshacer.

function applySlashChoice(view, from, to, type) {
  let insert;
  let cursorOffset;
  switch (type) {
    case "h1":
      insert = "# ";
      cursorOffset = insert.length;
      break;
    case "h2":
      insert = "## ";
      cursorOffset = insert.length;
      break;
    case "h3":
      insert = "### ";
      cursorOffset = insert.length;
      break;
    case "h4":
      insert = "#### ";
      cursorOffset = insert.length;
      break;
    case "h5":
      insert = "##### ";
      cursorOffset = insert.length;
      break;
    case "bullet_list_item":
      insert = "- ";
      cursorOffset = insert.length;
      break;
    case "ordered_list_item":
      insert = "1. ";
      cursorOffset = insert.length;
      break;
    case "code":
      insert = "```\n\n```";
      cursorOffset = 4; // dentro de la valla, en la línea en blanco
      break;
    case "hr":
      insert = "---\n\n";
      cursorOffset = insert.length;
      break;
    case "quote":
      insert = "> ";
      cursorOffset = insert.length;
      break;
    case "paragraph":
    default:
      insert = "";
      cursorOffset = 0;
  }
  view.dispatch({ changes: { from, to, insert }, selection: { anchor: from + cursorOffset } });
  view.focus();
}

// "Página" no aplica un prefijo de Markdown como el resto de opciones -- crea
// una página de verdad (llamada a la API) y dejaría un título "Nueva página"
// repetido cada vez que se usa, con el problema de que un wikilink resuelve
// por título (ver ARCHITECTURE.md), así que dos "Nueva página" son
// ambiguas. En vez de crearla al momento, elegir "Página" pasa el menú a un
// segundo modo ("naming"): se borra "/page" y se deja el cursor listo para
// que el usuario escriba el nombre de verdad ahí mismo, como texto normal;
// Enter la crea con ese nombre y la sustituye por el wikilink, Escape
// cancela y deja el nombre ya escrito como texto normal (no se pierde).
async function createPageFromName(view, from, to, onCreatePage) {
  const title = view.state.doc.sliceString(from, to).trim();
  if (!title) {
    view.focus();
    return;
  }
  const created = await onCreatePage?.(title);
  if (!created) {
    view.focus();
    return;
  }
  const insert = `[[${created.title}]]`;
  view.dispatch({ changes: { from, to, insert }, selection: { anchor: from + insert.length } });
  view.focus();
}

// La base del comando "/base" con nombre nuevo: mismo patrón que
// `createPageFromName` -- si la app dice que no se creó (nombre ocupado, error
// de la API) no se inserta nada y el nombre escrito se queda en la nota, para
// poder cambiarlo y reintentar. Cuando sí se crea, lo que queda es el bloque
// apuntando a la base de verdad y el cursor en el párrafo de debajo.
async function createDatabaseFromName(view, from, to, onCreateDatabase) {
  const title = view.state.doc.sliceString(from, to).trim();
  if (!title) {
    view.focus();
    return;
  }
  let creada = null;
  try {
    creada = await onCreateDatabase?.(title);
  } catch {
    creada = null;
  }
  // Mientras se creaba (es una llamada a la API y el árbol se recarga) el
  // usuario ha podido seguir escribiendo: si el tramo del nombre ya no es el
  // que era, no se pisa nada.
  if (!creada || view.state.doc.sliceString(from, to) !== title) {
    view.focus();
    return;
  }
  const { text, cursorOffset } = databaseBlockInsert(creada);
  view.dispatch({ changes: { from, to, insert: text }, selection: { anchor: from + cursorOffset } });
  view.focus();
}

function slashMenuExtension({
  onMenuUpdate,
  onCreatePage,
  onListDatabases,
  onPickDatabase,
  onCreateDatabase,
}) {
  const plugin = ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.view = view;
        this.mode = null; // null | "menu" | "database" | "naming"
        this.from = -1;
        this.selectedIndex = 0;
        this.namingKind = "page"; // a qué comando pertenece el modo "naming"
        this.entries = []; // bases del vault, pedidas al abrir el selector
      }

      get query() {
        if (this.mode !== "menu" && this.mode !== "database") return "";
        const pos = this.view.state.selection.main.head;
        if (pos < this.from) return "";
        // En el menú "/" la consulta empieza detrás de la barra; en el selector
        // de bases no hay barra: lo escrito (el filtro) empieza en `from`.
        return this.view.state.doc.sliceString(this.from + (this.mode === "menu" ? 1 : 0), pos);
      }

      get filteredOptions() {
        const all = SLASH_MENU_OPTIONS;
        const q = this.query.trim().toLowerCase();
        if (!q) return all;
        return all.filter((t) => t.abbrev.startsWith(q) || t.label.toLowerCase().includes(q));
      }

      /** Selector de bases: las del vault y, al final, la de crear una. */
      get databaseOptions() {
        return databaseChoices(this.entries, this.query);
      }

      /** Las opciones que se están enseñando (menú "/" o selector de bases). */
      get activeOptions() {
        return this.mode === "database" ? this.databaseOptions : this.filteredOptions;
      }

      update(update) {
        if (!update.docChanged && !update.selectionSet) return;

        if (this.mode === "naming") {
          const sel = update.state.selection;
          const pos = sel.main.head;
          const line = update.state.doc.lineAt(Math.min(this.from, update.state.doc.length));
          if (sel.ranges.length !== 1 || !sel.main.empty || pos < this.from || pos > line.to) {
            this.mode = null;
            this.from = -1;
            this.notify();
            return;
          }
          this.notify();
          return;
        }

        // El menú "/" y el selector de bases se comportan igual: viven en
        // una línea y se cierran en cuanto el cursor se sale de ella. La única
        // diferencia es que el menú exige que la barra siga detrás.
        if (this.mode === "menu" || this.mode === "database") {
          const sel = update.state.selection;
          const pos = sel.main.head;
          const line = update.state.doc.lineAt(Math.min(this.from, update.state.doc.length));
          const slashStillThere =
            this.mode !== "menu" ||
            (this.from < update.state.doc.length &&
              update.state.doc.sliceString(this.from, this.from + 1) === "/");

          if (!slashStillThere || sel.ranges.length !== 1 || !sel.main.empty || pos < this.from || pos > line.to) {
            this.close();
            return;
          }

          this.selectedIndex = Math.min(this.selectedIndex, Math.max(0, this.activeOptions.length - 1));
          this.notify();
          return;
        }

        if (!update.docChanged) return;
        update.changes.iterChanges((_fromA, _toA, fromB, _toB, inserted) => {
          if (this.mode || inserted.toString() !== "/") return;
          const line = update.state.doc.lineAt(fromB);
          const beforeSlash = update.state.doc.sliceString(line.from, fromB);
          if (beforeSlash.trim() !== "") return; // solo dispara al principio de un bloque vacío
          this.mode = "menu";
          this.from = fromB;
          this.selectedIndex = 0;
          this.notify();
        });
      }

      notify() {
        if (!this.mode) {
          onMenuUpdate(null);
          return;
        }
        if (this.mode === "naming") {
          const from = this.from;
          const target = this.namingKind; // "page" | "database": qué se está nombrando
          setTimeout(() => {
            if (this.mode !== "naming" || this.from < 0) return; // pudo cerrarse mientras tanto
            const coords = this.view.coordsAtPos(Math.min(from, this.view.state.doc.length));
            onMenuUpdate({ kind: "naming", target, x: coords?.left ?? 0, y: coords?.bottom ?? 0 });
          });
          return;
        }
        const opts = this.activeOptions;
        const selectedIndex = Math.min(this.selectedIndex, Math.max(0, opts.length - 1));
        const options = opts.map((o, i) => ({
          ...o,
          onClick: () => {
            this.selectedIndex = i;
            this.confirmSelection();
          },
        }));
        // Un vault sin bases enseña igualmente el selector (con la opción de
        // crear): el aviso dice por qué la lista está vacía.
        const note =
          this.mode === "database" && this.entries.length === 0
            ? "Aún no hay ninguna base en el vault."
            : null;
        // coordsAtPos() lee el layout del DOM -- CodeMirror no permite
        // hacerlo dentro del propio ciclo de update() ("Reading the editor
        // layout isn't allowed during an update"), así que se difiere fuera
        // de la pila de la transacción actual. setTimeout(…, 0) en vez de
        // requestAnimationFrame: solo necesitamos salir del update() en
        // curso, no esperar a un frame de composición real (que además no
        // se dispara si la pestaña no está pintando).
        setTimeout(() => {
          if ((this.mode !== "menu" && this.mode !== "database") || this.from < 0) return; // pudo cerrarse mientras tanto
          const coords = this.view.coordsAtPos(this.from);
          onMenuUpdate({
            kind: "menu",
            x: coords?.left ?? 0,
            y: coords?.bottom ?? 0,
            selectedIndex,
            options,
            note,
          });
        });
      }

      close() {
        if (!this.mode) return;
        this.mode = null;
        this.from = -1;
        onMenuUpdate(null);
      }

      moveSelection(delta) {
        if (this.mode !== "menu" && this.mode !== "database") return false;
        const n = this.activeOptions.length;
        if (n === 0) return true;
        this.selectedIndex = (this.selectedIndex + delta + n) % n;
        this.notify();
        return true;
      }

      confirmSelection() {
        if (this.mode !== "menu" && this.mode !== "database") return false;
        const opts = this.activeOptions;
        const from = this.from;
        const to = this.view.state.selection.main.head;
        if (opts.length === 0) {
          this.close();
          return true;
        }
        const chosen = opts[Math.min(this.selectedIndex, opts.length - 1)];

        // ---- selector de bases
        if (chosen.kind === "item") {
          this.close();
          this.pickDatabase(from, to, chosen.entry);
          return true;
        }
        if (chosen.kind === "create") {
          // Lo escrito como filtro se queda y pasa a ser el nombre de la base:
          // es lo mismo que el «Crear «…»» del autocompletado de "[[".
          this.startNaming(from, to, "database");
          return true;
        }

        // ---- menú "/"
        if (chosen.type === "database") {
          this.openDatabasePicker(from, to);
          return true;
        }
        if (chosen.type === "page") {
          // Borra "/consulta" y pasa a modo "naming" -- no se cierra del
          // todo (el usuario sigue escribiendo, ahora el nombre en vez del
          // filtro), así que no se llama a this.close(). El estado se
          // actualiza ANTES del dispatch a propósito: ese dispatch dispara
          // el propio update() de este plugin de forma síncrona, y si
          // this.mode siguiera siendo "menu" en ese momento, detectaría el
          // "/page" ya borrado como si el menú debiera cerrarse.
          this.startNaming(from, to, "page");
          return true;
        }

        this.close();
        applySlashChoice(this.view, from, to, chosen.type);
        return true;
      }

      /** Abre el selector de bases: borra "/base" y lista las del vault. */
      openDatabasePicker(from, to) {
        this.mode = "database";
        this.from = from;
        this.selectedIndex = 0;
        // La lista se pide UNA vez, al abrir: filtrar mientras se escribe no
        // puede ir a la API en cada tecla. El dispatch de abajo dispara el
        // update() de este plugin, que ya en modo "database" pinta el selector.
        this.entries = normalizeDatabaseEntries(onListDatabases?.());
        this.view.dispatch({ changes: { from, to, insert: "" }, selection: { anchor: from } });
      }

      /** Escribe el bloque de la base elegida, con el cursor debajo. */
      pickDatabase(from, to, entry) {
        const esperado = this.view.state.doc.sliceString(from, to);
        // El id que va al bloque es el de la fila `databases`, y la vista con
        // la que se abre hay que preguntarla: la lista del selector solo trae
        // título y carpeta.
        const pending = Promise.resolve()
          .then(() => onPickDatabase?.(entry.id))
          .catch(() => null);
        pending.then((info) => {
          const view = this.view;
          if (!view.dom.isConnected) return; // la nota se cerró mientras tanto
          // Si lo escrito ya no está donde estaba, el usuario ha seguido
          // tecleando: no se inserta nada a ciegas.
          const actual = view.state.doc.sliceString(from, Math.min(to, view.state.doc.length));
          if (!info || actual !== esperado) {
            view.focus();
            return;
          }
          const { text, cursorOffset } = databaseBlockInsert(info);
          view.dispatch({ changes: { from, to, insert: text }, selection: { anchor: from + cursorOffset } });
          view.focus();
        });
      }

      /** Pasa del menú al aviso de nombre: una página nueva o una base nueva. */
      startNaming(from, to, kind) {
        this.mode = "naming";
        this.namingKind = kind;
        this.from = from;
        if (kind === "page") {
          // "/page" (y el filtro escrito detrás) fuera: el nombre empieza de
          // cero. El dispatch dispara este update(), ya en "naming", que pinta
          // el aviso.
          this.view.dispatch({ changes: { from, to, insert: "" }, selection: { anchor: from } });
          return;
        }
        // Al crear una base, lo escrito es el nombre que va a llevar: se queda
        // tal cual, con el cursor al final, listo para Enter.
        this.notify();
      }

      confirmNaming() {
        if (this.mode !== "naming") return false;
        const from = this.from;
        const to = this.view.state.selection.main.head;
        const kind = this.namingKind;
        this.mode = null;
        this.from = -1;
        this.namingKind = "page";
        this.notify();
        if (kind === "database") createDatabaseFromName(this.view, from, to, onCreateDatabase);
        else createPageFromName(this.view, from, to, onCreatePage);
        return true;
      }

      destroy() {
        onMenuUpdate(null);
      }
    }
  );

  const menuKeymap = keymap.of([
    { key: "ArrowDown", run: (view) => view.plugin(plugin)?.moveSelection(1) ?? false },
    { key: "ArrowUp", run: (view) => view.plugin(plugin)?.moveSelection(-1) ?? false },
    {
      key: "Enter",
      run: (view) => {
        const pv = view.plugin(plugin);
        if (!pv) return false;
        if (pv.mode === "naming") return pv.confirmNaming();
        return pv.confirmSelection();
      },
    },
    {
      key: "Escape",
      run: (view) => {
        const pv = view.plugin(plugin);
        if (pv?.mode) {
          pv.close();
          return true;
        }
        return false;
      },
    },
  ]);

  return [plugin, menuKeymap];
}

// ---------------------------------------------- ⏎ / ⇧⏎ / ⌘⏎ / ⌃⏎
//
// Hasta aquí, ⏎ era un salto de línea dentro del bloque y Mayús+Enter
// ("\n\n") era lo que creaba un bloque nuevo. El dueño lo quiso al revés:
// ⏎ crea bloque y ⇧⏎/⌘⏎/⌃⏎ saltan línea dentro del bloque. Qué cambia
// exactamente en cada tipo (lista, numerada, cita, título, código) se decide
// en block-keys.js, sin CodeMirror, para poder probarlo con `node --test`.
//
// Este keymap va DESPUÉS del de "[[", del menú "/" y del de las flechas de la
// lista de enlaces: con el desplegable de "[[" abierto tiene que ganar su
// Enter (completa el enlace y NO crea bloque -- manda sobre todo lo demás).
// Las tres combinaciones con modificador hacen lo mismo: en un Mac `⌘` es el
// modificador real de CodeMirror (Mod) y en Linux/Windows lo es Ctrl; Meta se
// escucha además aparte porque es el nombre con el que el dueño lo pide.
const blockKeymap = keymap.of([
  { key: "Enter", run: (view) => runBlockKey(view, "enter") },
  { key: "Shift-Enter", run: (view) => runBlockKey(view, "soft") },
  { key: "Mod-Enter", run: (view) => runBlockKey(view, "soft") },
  { key: "Ctrl-Enter", run: (view) => runBlockKey(view, "soft") },
  { key: "Meta-Enter", run: (view) => runBlockKey(view, "soft") },
]);

// Supr y Retroceso con bloques marcados. Van antes que el keymap general (que
// borra el texto seleccionado y deja el hueco) y que el Backspace de Markdown.
const blockDeleteKeymap = keymap.of([
  { key: "Delete", run: (view) => deleteSelectedBlocks(view) },
  { key: "Backspace", run: (view) => deleteSelectedBlocks(view) },
]);

// ⌫ que fusiona dos ítems de lista: por delante del Backspace de Markdown, que
// es el que pegaba las dos frases («- uno- dos», D9).
const backspaceJoinKeymap = keymap.of([{ key: "Backspace", run: (view) => runBackspaceJoin(view) }]);

/** Bloque (del árbol de Markdown) en el que cae `pos`, o null si el cursor no
 * está dentro de ninguno (nota vacía, fila en blanco entre dos bloques). */
function blockAt(state, pos) {
  let found = null;
  for (const b of state.field(blockField).blocks) {
    if (pos >= b.from && pos <= b.to) found = b;
  }
  return found;
}

function runBlockKey(view, kind) {
  const { state } = view;
  const range = state.selection.main;

  // Con bloques marcados, ⏎ y ⇧⏎ los sustituyen por un bloque vacío en su
  // sitio (el cursor queda dentro). Sin esto, el salto caía DENTRO del rango
  // marcado, entre los separadores que ya había, y el `.md` acababa con cinco
  // líneas en blanco. Es el mismo criterio que escribir encima: con varios
  // marcados, la tecla afecta a todos.
  const { selected } = state.field(blockField);
  if (selected.size) {
    const edit = newBlockEdit({ text: state.doc.toString(), blocks: state.field(blockField).blocks, indices: [...selected] });
    if (edit) {
      const set = ChangeSet.of(edit.changes, state.doc.length);
      view.dispatch({
        changes: set,
        selection: { anchor: set.mapPos(edit.cursor, 1) },
        // El bloque vacío es el hueco que queda entre los dos separadores (ver
        // `newBlockEdit`): el primer cambio es el par de saltos que lo forma.
        effects: [
          setSelectedBlocks.of(new Set()),
          ...emptyGapEffect(edit.changes[0].from, edit.changes[0].insert),
        ],
        userEvent: "input",
        scrollIntoView: true,
      });
      view.focus();
      return true;
    }
  }

  // Con texto seleccionado no hay bloque que partir: la selección se sustituye
  // por el separador que toque (⇧⏎/⌘⏎ un salto; ⏎ el de bloque, salvo dentro
  // de código, donde una valla no se parte nunca).
  if (!range.empty) {
    const sep = kind === "enter" && !isInsideCodeBlock(state, range.from) ? "\n\n" : "\n";
    view.dispatch({
      changes: { from: range.from, to: range.to, insert: sep },
      selection: { anchor: range.from + sep.length },
      scrollIntoView: true,
      userEvent: "input",
    });
    return true;
  }

  const edit = blockKeyEdit({
    text: state.doc.toString(),
    pos: range.head,
    block: blockAt(state, range.head),
    kind,
  });
  if (!edit) return false; // nota vacía, "---", fila en blanco...: decide CodeMirror

  view.dispatch({
    changes: { from: edit.from, to: edit.to, insert: edit.insert },
    selection: { anchor: edit.cursor },
    scrollIntoView: true,
    userEvent: "input",
    effects: emptyGapEffect(edit.from, edit.insert),
  });
  return true;
}

// --------------------------------------------- flechas arriba/abajo por línea
//
// Las flechas arriba/abajo por defecto de CodeMirror (cursorLineUp/Down)
// calculan a qué línea saltar por distancia en píxeles (una altura de línea
// "típica" hacia arriba/abajo desde la posición actual), no contando líneas
// del documento. Con varias líneas colapsadas a ~5px seguidas (separadores
// entre bloques -- ver blankLinePlugin -- y la línea horizontal como widget
// de bloque) esa distancia en píxeles abarca de sobra varias líneas
// colapsadas de golpe, y el cursor podía aterrizar varios bloques más
// arriba/abajo de lo esperado en una sola pulsación (issue #2 en GitHub).
//
// Sustituye el cálculo por líneas lógicas del documento (actual ± 1) en vez
// de por píxeles -- inmune a cualquier cosa que colapse o esconda contenido,
// ahora o en el futuro. Como la fuente del editor es monoespaciada, la
// columna en caracteres coincide con la columna visual, así que no hace
// falta convertir a/desde coordenadas de pantalla. La columna "objetivo" se
// recuerda entre pulsaciones consecutivas de ↑/↓ (para no perder la columna
// real al cruzar una línea más corta) y se olvida en cualquier otro cambio
// -- escribir, hacer clic, mover con las flechas horizontales...
function verticalMoveExtension() {
  const plugin = ViewPlugin.fromClass(
    class {
      constructor() {
        this.goalColumn = null;
        this.ownMove = false;
      }
      update(update) {
        if (this.ownMove) {
          this.ownMove = false; // la transacción que acabamos de disparar nosotros mismos
          return;
        }
        if (update.docChanged || update.selectionSet) {
          this.goalColumn = null;
        }
      }
      move(view, forward, extend) {
        const state = view.state;
        const range = state.selection.main;
        const curLine = state.doc.lineAt(range.head);

        // Primero se prueba el movimiento nativo de CodeMirror (coordsAtPos
        // + una línea de alto, en píxeles). Es fiable DENTRO de un mismo
        // párrafo envuelto en varias filas visuales -- eso es precisamente
        // lo que rompía la versión anterior de este método, que calculaba
        // siempre por línea LÓGICA sin distinguir un envoltorio visual de
        // un cambio real de línea (issue #3 en GitHub).
        const native = view.moveVertically(range, forward);
        if (state.doc.lineAt(native.head).number === curLine.number) {
          this.goalColumn = null; // recalcular en la próxima línea lógica de verdad
          this.ownMove = true;
          view.dispatch({
            selection: extend ? EditorSelection.range(range.anchor, native.head) : native,
            scrollIntoView: true,
            userEvent: "select",
          });
          return true;
        }

        // Se sale del párrafo actual -- aquí es donde el cálculo en píxeles
        // se desmadra con líneas colapsadas o con mucha diferencia de
        // altura entre bloques (separadores en blanco, huecos por tipo de
        // bloque, cajas de cita...): a moveVertically se le puede ir la
        // mano y saltarse varias líneas lógicas de golpe. Se ignora a
        // dónde había decidido saltar y se avanza exactamente UNA línea
        // lógica del documento.
        const targetLineNo = curLine.number + (forward ? 1 : -1);
        if (targetLineNo < 1 || targetLineNo > state.doc.lines) return false; // primera/última línea: que decida el keymap por defecto

        const targetLine = state.doc.line(targetLineNo);
        if (this.goalColumn === null) this.goalColumn = range.head - curLine.from;
        const newPos = Math.min(targetLine.from + this.goalColumn, targetLine.to);

        this.ownMove = true;
        view.dispatch({
          selection: extend ? EditorSelection.range(range.anchor, newPos) : EditorSelection.cursor(newPos),
          scrollIntoView: true,
          userEvent: "select",
        });
        return true;
      }
    }
  );

  const moveKeymap = keymap.of([
    { key: "ArrowUp", run: (view) => view.plugin(plugin)?.move(view, false, false) ?? false },
    { key: "ArrowDown", run: (view) => view.plugin(plugin)?.move(view, true, false) ?? false },
    { key: "Shift-ArrowUp", run: (view) => view.plugin(plugin)?.move(view, false, true) ?? false },
    { key: "Shift-ArrowDown", run: (view) => view.plugin(plugin)?.move(view, true, true) ?? false },
  ]);

  return [plugin, moveKeymap];
}

// ------------------------------------------------------------- sugerencias
// Una sugerencia de agente es un parche `old_text -> new_text` (ADR-0003).
// Aquí solo se PINTA: el texto viejo se tacha y justo detrás aparece el
// propuesto, con un ✓ y un ✗. Aceptar y rechazar son llamadas a la API, así
// que las ejecuta la app mediante los callbacks que llegan en
// setSuggestions() -- el editor no habla con la red.
//
// El conjunto de sugerencias no forma parte del documento (no se deshace ni
// se guarda en el .md): vive en un StateField que solo cambia con el efecto
// setSuggestionSet, y un ViewPlugin recalcula las decoraciones cuando cambia
// el documento o el conjunto. Con la lista vacía no se pinta ni una
// decoración: el editor se comporta exactamente igual que sin esto.

const setSuggestionSet = StateEffect.define();

const NO_SUGGESTIONS = { list: [], callbacks: {} };

const suggestionField = StateField.define({
  create: () => NO_SUGGESTIONS,
  update(value, tr) {
    for (const effect of tr.effects) {
      if (effect.is(setSuggestionSet)) return effect.value;
    }
    return value;
  },
});

/** Localiza cada sugerencia pendiente en el documento ACTUAL.
 *
 * - Se marca la PRIMERA aparición del `old_text` y no se adivina cuál de
 *   varias era: el agente pudo proponer sobre un texto repetido, y elegir por
 *   él sería peor que enseñarlo en el panel.
 * - Si el texto no está (la nota cambió, o la sugerencia nació `orphaned`) no
 *   se pinta: sale en `unplaced` para que la app lo enseñe aparte.
 * - Dos sugerencias que se pisan en el mismo rango: se pinta la primera y la
 *   otra pasa al panel (CodeMirror no admite dos marcas idénticas).
 */
function locateSuggestions(state) {
  const { list } = state.field(suggestionField);
  const placed = [];
  const unplaced = [];
  const taken = [];
  if (!list.length) return { placed, unplaced };
  const text = state.doc.toString();
  for (const suggestion of list) {
    // Solo las pendientes: una aceptada o rechazada ya no es una propuesta.
    if (!suggestion.old_text || (suggestion.status && suggestion.status !== "pending")) continue;
    const from = text.indexOf(suggestion.old_text);
    const to = from < 0 ? -1 : from + suggestion.old_text.length;
    if (from < 0 || taken.some(([a, b]) => from < b && to > a)) {
      unplaced.push(suggestion.id);
      continue;
    }
    taken.push([from, to]);
    placed.push({ suggestion, from, to });
  }
  return { placed, unplaced };
}

/** Engancha un botón del widget a su acción, por ratón y por teclado.
 *
 * `click` es la vía del ratón (y de `.click()`), pero dentro del editor no
 * basta para el teclado: CodeMirror escucha `keydown` en el contenido y su
 * keymap se queda con Enter (inserta una línea nueva) antes de que el
 * navegador active el `<button>` enfocado, así que con Tab se llegaba a ✓/✗
 * y no pasaba nada. Por eso Enter y Espacio se atienden aquí mismo, con
 * `preventDefault()` + `stopPropagation()`: la acción sale una sola vez y
 * es la misma con ratón que con teclado.
 *
 * El callback recibe `(id, { hadFocus })`: `hadFocus` es true cuando el botón
 * era el elemento activo (teclado). La app lo usa para devolver el foco al
 * editor después de decidir, porque el widget entero se destruye y el foco se
 * caía a BODY. Con ratón el botón nunca llega a enfocarse (el `mousedown` de
 * arriba se neutraliza), así que `hadFocus` es false y el foco no se toca.
 */
function wireSuggestionButton(button, callback, id) {
  const decide = () => callback?.(id, { hadFocus: document.activeElement === button });
  // El mousedown solo se neutraliza: así el clic no mueve el cursor dentro
  // del texto ni roba la selección, y el documento que se revisa no cambia.
  button.addEventListener("mousedown", (event) => {
    event.preventDefault();
    event.stopPropagation();
  });
  button.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") return;
    event.preventDefault();
    event.stopPropagation();
    decide();
  });
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    decide();
  });
}

// El texto propuesto, pegado al final del rango tachado, con los dos botones.
// Un widget (no un reemplazo) porque el texto viejo TIENE que seguir viéndose:
// la gracia es comparar el de antes con el de después en el mismo sitio.
class SuggestionWidget extends WidgetType {
  constructor(suggestion, callbacks) {
    super();
    this.suggestion = suggestion;
    this.callbacks = callbacks;
  }
  eq(other) {
    return (
      other.suggestion.id === this.suggestion.id &&
      other.suggestion.old_text === this.suggestion.old_text &&
      other.suggestion.new_text === this.suggestion.new_text &&
      other.suggestion.agent === this.suggestion.agent &&
      other.callbacks === this.callbacks
    );
  }
  toDOM() {
    const wrap = document.createElement("span");
    wrap.className = "cm-sug-widget";
    wrap.dataset.suggestionId = this.suggestion.id;

    const proposed = document.createElement("span");
    proposed.className = "cm-sug-ins";
    proposed.textContent = this.suggestion.new_text;
    if (this.suggestion.agent) proposed.title = `Propuesta de ${this.suggestion.agent}`;

    const actions = document.createElement("span");
    actions.className = "cm-sug-actions";

    const accept = document.createElement("button");
    accept.type = "button";
    accept.className = "cm-sug-btn cm-sug-accept";
    accept.textContent = "✓";
    accept.setAttribute("aria-label", "Aceptar sugerencia");
    accept.title = "Aceptar sugerencia";
    wireSuggestionButton(accept, this.callbacks.onAccept, this.suggestion.id);

    const reject = document.createElement("button");
    reject.type = "button";
    reject.className = "cm-sug-btn cm-sug-reject";
    reject.textContent = "✗";
    reject.setAttribute("aria-label", "Rechazar sugerencia");
    reject.title = "Rechazar sugerencia";
    wireSuggestionButton(reject, this.callbacks.onReject, this.suggestion.id);

    actions.append(accept, reject);
    wrap.append(proposed, actions);
    return wrap;
  }
  ignoreEvent() {
    return false; // que los clics de los botones lleguen a sus listeners
  }
}

function suggestionDecorations(state) {
  const { placed } = locateSuggestions(state);
  if (!placed.length) return Decoration.none;
  const { callbacks } = state.field(suggestionField);
  const ranges = [];
  for (const { suggestion, from, to } of placed) {
    ranges.push(Decoration.mark({ class: "cm-sug-del" }).range(from, to));
    ranges.push(
      Decoration.widget({ widget: new SuggestionWidget(suggestion, callbacks), side: 1 }).range(to)
    );
  }
  return Decoration.set(ranges, true);
}

/** El reparto actual, en el formato que consume la app: ids anclados, ids
 * que no caben en el texto y una clave para comparar dos repartos. */
function placementOf(state) {
  const { placed, unplaced } = locateSuggestions(state);
  const ids = placed.map((p) => p.suggestion.id);
  return { placed: ids, unplaced, key: `${ids.join("\u0000")}|${unplaced.join("\u0000")}` };
}

function suggestionPlugin() {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = suggestionDecorations(view.state);
        this.placement = placementOf(view.state).key;
      }
      update(update) {
        const setChanged =
          update.state.field(suggestionField) !== update.startState.field(suggestionField);
        if (!update.docChanged && !setChanged) return;
        this.decorations = suggestionDecorations(update.state);
        const placement = placementOf(update.state);
        const changed = placement.key !== this.placement;
        this.placement = placement.key;
        // Solo se avisa cuando es una EDICIÓN la que mueve el reparto:
        // setSuggestions() ya devuelve el suyo a quien la llama. Sin este
        // aviso, si el `old_text` desaparece con la nota abierta la app se
        // queda con el reparto viejo y la sugerencia no sale ni en el texto
        // ni en el panel -- un callejón sin salida hasta reabrir la nota.
        if (!update.docChanged || !changed) return;
        const { callbacks } = update.state.field(suggestionField);
        callbacks.onPlacementChange?.({ placed: placement.placed, unplaced: placement.unplaced });
      }
    },
    { decorations: (v) => v.decorations }
  );
}

// ---------------------------------------------- autocompletado de wikilinks
//
// Al escribir "[[" se abre la lista de páginas del vault y se filtra según lo
// que se va escribiendo; <flechas> mueven, Enter inserta "[[Título]]" y Esc
// cierra sin insertar nada. Si lo escrito no coincide exactamente con ninguna
// página, la PRIMERA opción es "Crear «…»": la app crea la nota (llamada a la
// API, vía onCreateLink) y aquí se inserta el enlace con el título REAL que
// devuelve el servidor -- puede haber cambiado al normalizar el nombre.
//
// Se apoya en @codemirror/autocomplete a propósito: el popup, el teclado
// (flechas/Enter/Esc), el posicionamiento y el cierre al perder el foco ya
// vienen hechos y probados; escribirlos a mano aquí sería reimplementar una
// lista con foco, scroll y aria peor que la oficial. Lo propio es solo la
// fuente de opciones (linkCompletionSource), donde vive el disparador "[[",
// el filtro sin acentos y el orden.

const setLinkTargetsEffect = StateEffect.define();

// Los destinos viven en un StateField, no en la instancia del plugin: la app
// los reemplaza enteros cuando recarga el árbol (setLinkTargets) y así el
// siguiente disparo de la lista ya ve el vault al día, sin recrear el editor.
const linkTargetsField = StateField.define({
  create: () => [],
  update(value, tr) {
    for (const effect of tr.effects) {
      if (effect.is(setLinkTargetsEffect)) return effect.value;
    }
    return value;
  },
});

/** Minúsculas y sin acentos ("Año" -> "ano"): buscar "ano" tiene que
 * encontrar "año" -- es lo que espera quien escribe rápido y sin tilde. */
function foldText(s) {
  return String(s || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

// Cualquier corchete (o un salto de línea) entre "[[" y el cursor cancela la
// lista: con "[[foo]]" ya escrito y el cursor detrás del cierre, lo que hay
// delante no es una consulta sino un enlace terminado.
const LINK_BREAK_RE = /[\[\]\n]/;

/** ¿Toca abrir la lista en `pos`? Devuelve {from, typed} -- `from` es la
 * posición del "[[", `typed` lo escrito detrás -- o null. */
function linkTrigger(state, pos) {
  const line = state.doc.lineAt(pos);
  const before = state.doc.sliceString(line.from, pos);
  const open = before.lastIndexOf(LINK_OPEN);
  if (open < 0) return null;
  const typed = before.slice(open + LINK_OPEN.length);
  if (LINK_BREAK_RE.test(typed)) return null;
  const from = line.from + open;
  // Dentro de un bloque de código no salta: "[[i]]" en un array de verdad no
  // es un enlace (mismo criterio que las flechas tipográficas, más arriba).
  if (isInsideCodeBlock(state, from)) return null;
  return { from, typed };
}

/** Sustituye lo tecleado desde "[[" por "[[título]]" y deja el cursor detrás
 * del cierre. */
function applyLink(view, from, to, title) {
  const insert = linkMarkup(title);
  view.dispatch({
    changes: { from, to, insert },
    selection: { anchor: from + insert.length },
    userEvent: "input.complete",
  });
  view.focus();
}

/** Opción "Crear «…»": la nota la crea la app (es ella la que habla con la
 * API) y el título que se escribe es el que devuelve el servidor.
 *
 * Si la creación falla -- la app ya avisó por su cuenta -- no se inserta
 * NADA: lo tecleado se queda tal cual para poder corregirlo y reintentar. */
async function applyCreateLink(view, from, to, typedText, onCreateLink) {
  closeCompletion(view);
  // `typedText` es lo tecleado TAL CUAL (espacios incluidos); el recorte se
  // hace solo para el título que viaja a la API.
  const title = createLinkTitle(typedText);
  if (!title) {
    view.focus();
    return;
  }
  let created = null;
  try {
    created = await onCreateLink?.(title);
  } catch (e) {
    created = null;
  }
  if (!created || !created.title) {
    view.focus();
    return;
  }
  // Mientras se creaba, el usuario pudo seguir escribiendo (o cambiar de
  // nota, y entonces este editor ya no pinta nada): si bajo "[[" ya no está
  // exactamente lo que se tecleó -- espacios incluidos --, no se toca el
  // documento. El fragmento se comprueba crudo porque es lo que hay en el
  // texto; compararlo con el título ya recortado dejaba la nota creada y el
  // enlace sin escribir.
  const doc = view.state.doc;
  const fragment = from >= 0 && from <= to && to <= doc.length ? doc.sliceString(from, to) : null;
  const realTitle = resolveCreatedLinkTitle(fragment, typedText, created.title);
  if (!realTitle) {
    view.focus();
    return;
  }
  applyLink(view, from, to, realTitle);
}

function linkCompletionSource(onCreateLink) {
  return (context) => {
    const { state, pos } = context;
    const trigger = linkTrigger(state, pos);
    if (!trigger) return null;
    const { from, typed } = trigger;
    const query = foldText(typed.trim());

    const matches = [];
    let exact = false;
    for (const target of state.field(linkTargetsField)) {
      const title = String(target.title || "").trim();
      if (!title) continue;
      // Un título con `]` o `|` no se puede enlazar (ver isLinkableTitle): se
      // queda fuera de la lista en vez de ofrecer lo que saldría roto.
      if (!isLinkableTitle(title)) continue;
      const folded = foldText(title);
      const at = query ? folded.indexOf(query) : 0;
      if (query && at < 0) continue;
      if (folded === query) exact = true;
      // 0 = empieza por lo escrito, 1 = solo lo contiene: es el orden que se
      // pidió (primero las que empiezan), y dentro de cada grupo, alfabético.
      matches.push({ title, type: target.type, rank: at === 0 ? 0 : 1 });
    }
    matches.sort(
      (a, b) => a.rank - b.rank || a.title.localeCompare(b.title, "es", { sensitivity: "base" })
    );

    const writable = typed.trim();
    const options = [];
    if (writable && !exact) {
      options.push({
        label: `Crear «${writable}»`,
        detail: "nota nueva",
        class: "cm-mdm-link-create",
        // Lo tecleado crudo, no `writable`: la sustitución tiene que
        // encontrar en el documento exactamente lo que hay bajo "[[".
        apply: (view, _completion, applyFrom, applyTo) =>
          applyCreateLink(view, applyFrom, applyTo, typed, onCreateLink),
      });
    }
    for (const m of matches) {
      options.push({
        label: m.title,
        detail: m.type === "database" ? "base de datos" : "nota",
        apply: (view, _completion, applyFrom, applyTo) => applyLink(view, applyFrom, applyTo, m.title),
      });
    }

    // `filter: false`: el filtro y el orden son los de arriba (sin acentos,
    // primero las que empiezan), no el difuso por defecto del paquete, que
    // reordenaría por su cuenta y no entiende de tildes.
    if (!options.length) {
      // Sin lista no hay nada que enseñar, pero tampoco una caja vacía muda:
      // una línea que dice por qué (y Enter/Esc la cierran sin insertar).
      return {
        from,
        filter: false,
        options: [
          {
            // Se cuentan los destinos ENLAZABLES, no los del campo: en un
            // vault cuyas únicas notas lleven `]` no hay nada que ofrecer y el
            // aviso correcto es «Aún no hay notas», no «Sin coincidencias».
            label: countLinkableTitles(state.field(linkTargetsField))
              ? "Sin coincidencias"
              : "Aún no hay notas",
            class: "cm-mdm-link-empty",
            apply: (view) => closeCompletion(view),
          },
        ],
      };
    }
    return { from, options, filter: false };
  };
}

// El keymap del paquete entra con Prec.highest y se queda con Enter, las
// flechas y Escape por delante de todo lo demás -- incluido el menú "/", que
// vive en su propio keymap y cerraba con Escape solo si llegaba hasta él. Un
// Escape se lo tragaba el autocompletado (closeCompletion devuelve true si
// queda alguna fuente sin cerrar), así que el menú "/" no se cerraba a la
// primera. Este keymap hace lo mismo pero se registra donde toca (después del
// menú "/", antes del keymap general y del de las flechas verticales): con
// las dos listas cerradas todos sus comandos devuelven false y no se nota.
const linkKeymap = keymap.of([
  { key: "ArrowDown", run: moveCompletionSelection(true) },
  { key: "ArrowUp", run: moveCompletionSelection(false) },
  { key: "PageDown", run: moveCompletionSelection(true, "page") },
  { key: "PageUp", run: moveCompletionSelection(false, "page") },
  { key: "Enter", run: acceptCompletion },
  { key: "Escape", run: closeCompletion },
]);

function linkCompletion(onCreateLink) {
  return [
    linkTargetsField,
    autocompletion({
      // Solo esta fuente: el autocompletado de palabras del documento no
      // aporta nada en una nota y competiría por el mismo popup.
      override: [linkCompletionSource(onCreateLink)],
      icons: false, // sin hueco de icono: el tipo de destino va en `detail`
      // El keymap lo pone linkKeymap, más abajo (ver el porqué ahí).
      defaultKeymap: false,
      // activateOnTyping (por defecto) es lo que abre la lista al teclear.
    }),
  ];
}

/**
 * @param {Object} opts
 * @param {HTMLElement} opts.parent
 * @param {string} opts.doc
 * @param {(title: string) => ({id: string}|null)} opts.resolvePage
 * @param {(pageId: string) => void} opts.onNavigate
 * @param {(text: string) => void} opts.onChange
 * @param {(payload: object) => void} [opts.onBlockContextMenu]
 * @param {(pageId: string, rect: DOMRect) => void} [opts.onWikilinkHover]
 * @param {() => void} [opts.onWikilinkHoverEnd]
 * @param {(state: object|null) => void} [opts.onSlashMenu]
 * @param {(title: string) => Promise<{id: string, title: string}|null>} [opts.onCreatePage]
 * @param {() => {id: string, title: string, folder?: string}[]} [opts.onListDatabases]
 *   Bases del vault para el comando "/base": el `id` es el de la fila
 *   `databases` (el que va en el bloque) y `folder` la carpeta, para distinguir
 *   dos que se llamen igual.
 * @param {(databaseId: string) => Promise<{id: string, title: string, view: string|null}|null>} [opts.onPickDatabase]
 *   Lo que hace falta para escribir el bloque de una base que ya existe (su id
 *   y la vista con la que se abre). `null` = no se inserta nada.
 * @param {(title: string) => Promise<{id: string, title: string, view: string|null}|null>} [opts.onCreateDatabase]
 *   Crea una base con ese nombre colgando de la página abierta y devuelve lo
 *   mismo que `onPickDatabase`. `null` = no se creó (nombre ocupado, error):
 *   el editor no inserta nada.
 * @param {(title: string) => Promise<{id: string, title: string}|null>} [opts.onCreateLink]
 * @param {(list: {id: string, title: string, type: string}[]) => void} [opts.linkTargets]
 * @param {(block: {id: string, view: string|null, title: string|null}, container: HTMLElement) => Promise<void>|void} [opts.renderDatabaseBlock]
 *   Pinta una base incrustada (bloque ```mdm-database) dentro de la nota. Sin
 *   este callback la valla se queda en crudo; con él, `container` es el hueco
 *   donde va la tabla (ver DatabaseBlockWidget).
 */
// D8 (a): los bloques vacíos de los extremos no existen. En cuanto el cursor
// sale de ese hueco (deja de ser el bloque que se está creando), las líneas en
// blanco de arriba o de abajo se van del documento: lo que se ve es lo que se
// guarda. Se hace en un microtask porque despachar una transacción desde
// dentro de un `updateListener` no está permitido, y fuera del historial
// porque deshacer un cambio del usuario no tiene que resucitar un hueco que
// nunca fue un bloque.
//
// El recorte no es una edición del usuario: no pasa por `onChange` (si no,
// abrir una nota vieja con una línea en blanco delante y tocar el texto la
// reescribiría en el vault sin que nadie haya cambiado nada). Lo que se
// guarda lo decide `getValue()`, que recorta igual.
const trimming = new WeakSet();

function dropOuterBlankLines(update) {
  if (!update.docChanged && !update.selectionSet) return;
  const view = update.view;
  if (trimming.has(view)) return;
  // Barato primero: si el documento no empieza ni acaba en salto, no hay nada
  // que recortar (esto corre en cada cambio de selección y de texto; no tiene
  // que copiar el documento entero de una nota larga para nada).
  const doc = view.state.doc;
  if (!doc.length) return;
  if (doc.sliceString(0, 1) !== "\n" && doc.sliceString(doc.length - 1) !== "\n") return;
  const text = doc.toString();
  if (canonicalBody(text) === text) return;
  if (cursorInOuterBlanks(text, view.state.selection.main.head)) return; // se está escribiendo ahí
  trimming.add(view);
  queueMicrotask(() => {
    trimming.delete(view);
    if (!view.dom.isConnected) return; // el editor ya se destruyó (navegación)
    const current = view.state.doc.toString();
    const target = canonicalBody(current);
    if (target === current) return;
    if (cursorInOuterBlanks(current, view.state.selection.main.head)) return;
    view.dispatch({
      changes: { from: 0, to: current.length, insert: target },
      annotations: Transaction.addToHistory.of(false),
      userEvent: "trim.outer",
    });
  });
}

// El otro extremo de D8 (a): el bloque vacío que se crea EN MEDIO (el «+» del
// asa, ⏎ al final de un bloque con contenido detrás) tampoco existe. Se
// colapsa en cuanto el cursor sale de él sin que se haya escrito nada, con la
// misma mecánica que `dropOuterBlankLines`: microtask (despachar una
// transacción desde un `updateListener` no está permitido) y fuera del
// historial, porque deshacer un cambio del usuario no tiene que resucitar un
// hueco que nunca fue un bloque.
//
// No pasa por `onChange`: el aire no llega al fichero porque `getValue()` lo
// quita igual (ver `collapseEmptyGaps`), así que no hay nada que guardar. Y
// solo se tocan los huecos marcados por el propio editor: una nota que no se
// edita no tiene marcas y no se reescribe nunca.
const trimmingInner = new WeakSet();

/** Tramo de saltos de línea del documento que contiene `pos`. */
function newlineRunInDoc(doc, pos) {
  let from = pos;
  while (from > 0 && doc.sliceString(from - 1, from) === "\n") from--;
  let to = pos;
  while (to < doc.length && doc.sliceString(to, to + 1) === "\n") to++;
  return from === to ? null : { from, to };
}

/** ¿Hay alguna selección dentro del bloque vacío? Mientras la haya, el hueco
 * es el bloque que se está creando: quitarlo bajo los dedos sería peor que
 * dejarlo colapsado. */
function selectionInEmptyGap(state, gaps) {
  const doc = state.doc;
  for (const gap of gaps) {
    const run = newlineRunInDoc(doc, gap.from + 1);
    if (!run) continue;
    if (state.selection.ranges.some((r) => r.from < run.to && r.to > run.from)) return true;
  }
  return false;
}

function dropInnerEmptyGap(update) {
  if (!update.docChanged && !update.selectionSet) return;
  const view = update.view;
  const gaps = view.state.field(pendingEmptyGaps);
  if (!gaps.length) return;
  if (trimmingInner.has(view)) return;
  if (selectionInEmptyGap(view.state, gaps)) return; // se está escribiendo ahí
  trimmingInner.add(view);
  queueMicrotask(() => {
    trimmingInner.delete(view);
    if (!view.dom.isConnected) return; // el editor ya se destruyó (navegación)
    const state = view.state;
    const gaps = state.field(pendingEmptyGaps);
    if (!gaps.length) return;
    if (selectionInEmptyGap(state, gaps)) return; // ha vuelto al hueco
    const text = state.doc.toString();
    const { text: target, ranges } = collapseEmptyGaps(text, gaps);
    if (!ranges.length || target === text) return;
    view.dispatch({
      changes: ranges,
      annotations: Transaction.addToHistory.of(false),
      userEvent: "trim.inner",
    });
  });
}

export function createNoteEditor({
  parent,
  doc,
  resolvePage,
  onNavigate,
  onChange,
  onBlockContextMenu,
  onWikilinkHover,
  onWikilinkHoverEnd,
  onSlashMenu,
  onCreatePage,
  onCreateLink,
  onListDatabases,
  onPickDatabase,
  onCreateDatabase,
  linkTargets,
  renderDatabaseBlock,
}) {
  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: doc || "",
      extensions: [
        // El autocompletado de "[[" va el PRIMERO, antes que cualquier otro
        // keymap: con la lista abierta sus flechas/Enter/Esc tienen que
        // ganar (si no, Enter insertaría un salto de línea y las flechas
        // moverían el cursor en vez de la selección). Cerrada la lista, sus
        // comandos devuelven false y todo cae al keymap normal.
        ...linkCompletion(onCreateLink),
        // El menú "/" y Mayús+Enter van ANTES del keymap general: sus
        // combinaciones (Enter/flechas/Escape mientras el menú está activo)
        // deben ganar; cuando no hacen nada (devuelven false) caen al
        // keymap normal sin más.
        ...slashMenuExtension({
          onMenuUpdate: onSlashMenu ?? (() => {}),
          onCreatePage,
          onListDatabases,
          onPickDatabase,
          onCreateDatabase,
        }),
        // El del autocompletado, justo detrás: primero el menú "/" (que solo
        // consume teclas mientras está abierto) y luego la lista de "[[".
        linkKeymap,
        blockKeymap,
        blockDeleteKeymap,
        backspaceJoinKeymap,
        // El Backspace «que borra el marcador» de Markdown (deleteMarkupBackward)
        // se registra aquí a mano: `markdown()` ya no trae su keymap (ver abajo),
        // porque su Enter (insertNewlineContinueMarkup) competía con el de los
        // bloques y ganaba por precedencia (va con Prec.high). Del resto de su
        // comportamiento no se toca nada.
        keymap.of([{ key: "Backspace", run: deleteMarkupBackward }]),
        ...verticalMoveExtension(),
        history(),
        keymap.of([...defaultKeymap, ...historyKeymap]),
        // Sin keymap propio: su Enter (continuar lista/cita con un salto de
        // línea) es justo el que ahora decide blockKeymap. Su Backspace se
        // conserva, registrado arriba.
        markdown({ addKeymap: false }),
        syntaxHighlighting(highlightStyle),
        blockField,
        pendingEmptyGaps,
        // Sin este facet CodeMirror colapsa una selección de varios rangos al
        // rango principal: las asas se pintaban marcadas pero solo actuaba la
        // última (D3). El resto de selecciones múltiples (varios cursores) no
        // existe por defecto: solo las crea selectBlocks, al marcar asas.
        EditorState.allowMultipleSelections.of(true),
        typingOverBlocks(),
        blockHandlesPlugin(),
        blockHandleEventHandlers(onBlockContextMenu),
        blockLinesPlugin(),
        blockSpacingPlugin(),
        quotePlugin(),
        blankLinePlugin(),
        syntaxMarkPlugin(),
        hrField,
        databaseBlockField(renderDatabaseBlock),
        suggestionField,
        suggestionPlugin(),
        wikilinkPlugin(resolvePage),
        wikilinkClickHandler(onNavigate),
        wikilinkHoverHandler(onWikilinkHover, onWikilinkHoverEnd),
        markdownLinkPlugin(),
        markdownLinkClickHandler(),
        arrowPlugin(),
        EditorView.lineWrapping,
        EditorView.updateListener.of((update) => {
          const onlyTrim =
            update.docChanged &&
            update.transactions.length > 0 &&
            update.transactions.every(
              (tr) => tr.isUserEvent("trim.outer") || tr.isUserEvent("trim.inner"),
            );
          if (update.docChanged && !onlyTrim) onChange(update.state.doc.toString());
          dropOuterBlankLines(update);
          dropInnerEmptyGap(update);
        }),
      ],
    }),
  });

  return {
    // Lo que se guarda y lo que se exporta: el cuerpo canónico, sin las líneas
    // en blanco de los extremos. Decisión D8 (a): esos bloques vacíos no
    // existen -- el `.md` ya los tiraba al escribir (`canonical_body` en
    // backend/app/vault/store.py), así que el editor no los manda siquiera.
    getValue: () => {
      const text = view.state.doc.toString();
      const gaps = view.state.field(pendingEmptyGaps);
      // Los huecos vacíos que creó el editor y el usuario ha abandonado no
      // llegan al fichero: lo que se guarda coincide con lo que se ve (el
      // aire de en medio, igual que el de los extremos). Las líneas en blanco
      // escritas a mano no llevan marca y se guardan tal cual.
      return canonicalBody(gaps.length ? collapseEmptyGaps(text, gaps).text : text);
    },
    /** Destinos posibles de un wikilink -- `[{id, title, type}]` con todas
     * las notas y bases de datos del vault. La app la llama al cargar el
     * árbol y cada vez que el árbol cambia; con la lista vacía el
     * autocompletado de "[[" ofrece solo "Crear «…»". */
    setLinkTargets(list) {
      const clean = (Array.isArray(list) ? list : [])
        .filter((t) => t && t.title)
        .map((t) => ({
          id: t.id == null ? "" : String(t.id),
          title: String(t.title),
          type: t.type || "note",
        }));
      view.dispatch({ effects: setLinkTargetsEffect.of(clean) });
    },
    setValue(text) {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text || "" } });
    },
    focus: () => view.focus(),
    destroy: () => view.destroy(),
    /** Pinta las sugerencias pendientes de esta nota, en su sitio.
     *
     * `list` es `[{id, old_text, new_text, status}]` (lo que devuelve
     * `GET /pages/{id}/suggestions`) y `callbacks` trae `onAccept(id)` y
     * `onReject(id)`, que la app conecta a la API, y opcionalmente
     * `onPlacementChange({placed, unplaced})`, por el que el editor avisa
     * cuando una edición del documento deja sugerencias sin ancla (o las
     * vuelve a anclar). Devuelve qué se pudo
     * anclar en el texto y qué no -- `{placed: [id...], unplaced: [id...]}` --
     * para que la app enseñe aparte lo que aquí no cabe (desancladas, o dos
     * propuestas sobre el mismo texto).
     */
    setSuggestions(list, callbacks = {}) {
      const clean = (Array.isArray(list) ? list : [])
        .filter((s) => s && s.id && s.old_text)
        .map((s) => ({
          id: String(s.id),
          old_text: String(s.old_text),
          new_text: s.new_text == null ? "" : String(s.new_text),
          status: s.status || "pending",
          agent: s.agent || "",
        }));
      view.dispatch({ effects: setSuggestionSet.of({ list: clean, callbacks: callbacks || {} }) });
      const { placed, unplaced } = placementOf(view.state);
      return { placed, unplaced };
    },
    clearSuggestions() {
      view.dispatch({ effects: setSuggestionSet.of(NO_SUGGESTIONS) });
    },
    // Índice de la página (H1-H5), para el desplegable flotante de app.js --
    // se apoya en el mismo blockField que ya clasifica los bloques para
    // todo lo demás (seleccionar/convertir/asa), así que un "encabezado"
    // aquí es exactamente lo mismo que en el resto del editor.
    getOutline() {
      const { blocks } = view.state.field(blockField);
      return blocks
        .filter((b) => /^h[1-5]$/.test(b.type))
        .map((b) => ({
          level: Number(b.type.slice(1)),
          text: view.state.doc.sliceString(b.from, b.to).replace(/^#{1,6}[ \t]+/, ""),
          pos: b.from,
        }));
    },
    // Lleva el cursor a una posición del documento y la centra en pantalla
    // -- lo usa el desplegable del índice al clicar un encabezado.
    jumpTo(pos) {
      view.dispatch({ selection: { anchor: pos }, scrollIntoView: true });
      view.focus();
    },
  };
}
