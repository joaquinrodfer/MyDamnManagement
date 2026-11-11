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
import { HighlightStyle, ensureSyntaxTree, syntaxHighlighting, syntaxTree } from "@codemirror/language";
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
import { formatEdit, underlineSpans } from "./format.js";
import { blocksFromTree } from "./block-ranges.js";
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
import { insertBlockEdit, moveBlockEdits } from "./block-move.js";
import {
  canonicalBody,
  collapseEmptyGaps,
  cursorInOuterBlanks,
  outerBlankRange,
  shiftEmptyGap,
  withoutRange,
} from "./body.js";
import { databaseErrorText, databaseNotFoundText, scanDatabaseBlocks } from "./database-embed.js";
import {
  databaseBlockInsert,
  databaseChoices,
  databaseFilterClear,
  databaseFilterRange,
  normalizeDatabaseEntries,
} from "./database-choice.js";
import {
  capPageChoices,
  normalizePageEntries,
  pageChoices,
  pageFilterClear,
  pageFilterRange,
} from "./page-choice.js";

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
// mecanismo cursor-aware que los wikilinks). No se tocan dentro de código
// -- "ptr->campo" en una nota técnica no debería convertirse, ni dentro de
// un `código` en línea, donde también es literal (ver `codeRangesOf`).

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

// --------------------------------------------------------- zonas de código
// UNA sola definición de "esto es código" para todo el pintado: lo que el
// árbol de Markdown marca como código es literal y ahí no se sustituye nada.
// Antes esto miraba solo los bloques de blockField (`type === "code"`), que
// cubre vallas y código sangrado pero NO el `código` en línea (nodo
// InlineCode): por eso "`a -> b`" salía con flecha y "`<u>x</u>`" con las
// etiquetas ocultas y el texto subrayado.
const CODE_NODE_TYPES = new Set(["FencedCode", "CodeBlock", "InlineCode"]);

// Presupuesto de parseo síncrono al pintar, en milisegundos. CodeMirror parsea
// a trozos de ~100 ms en su worker; gastar lo mismo aquí, una vez por documento
// y posición (después el árbol ya está y `ensureSyntaxTree` no trabaja), no se
// nota. Poner más -- o forzarlo en cada repintado -- sí: bloquearía el hilo al
// escribir en una nota enorme.
const CODE_PARSE_BUDGET_MS = 100;

// El árbol de sintaxis es inmutable, así que sus zonas de código también lo
// son: se cachean por árbol. Los cuatro plugins que pintan piden las mismas
// zonas en el mismo repintado; así el árbol se recorre una vez, no cuatro.
const codeRangesCache = new WeakMap();

/**
 * Zonas de código del documento: vallas, código sangrado y `código` en línea.
 * Se calculan una vez por árbol y se consultan con `isInsideCode` (recorrer el
 * árbol por cada coincidencia sería cuadrático).
 *
 * `upto` (opcional): posición hasta la que el árbol TIENE que llegar. En notas
 * grandes `syntaxTree(state)` puede ser PARCIAL -- el parser trabaja con
 * presupuesto de tiempo y deja sin parsear lo que no está a la vista --, y una
 * zona de código sin parsear no existe: al final de una nota larga el `código`
 * en línea dejaba de respetarse y volvían la flecha `->` y el `<u>`. Con
 * `upto` se fuerza el parseo hasta ahí (`ensureSyntaxTree`) antes de leer el
 * árbol, que es lo que se está pintando en ese instante. Si no llega en el
 * presupuesto (documento gigante), se devuelve lo que haya; el repintado por
 * avance del parse (ver `parseAdvanced`) lo corrige en cuanto el worker llega.
 */
function codeRangesOf(state, upto) {
  const forced = upto == null ? null : ensureSyntaxTree(state, upto, CODE_PARSE_BUDGET_MS);
  const tree = forced || syntaxTree(state);
  const cached = codeRangesCache.get(tree);
  if (cached) return cached;
  const ranges = [];
  tree.iterate({
    enter: (node) => {
      if (!CODE_NODE_TYPES.has(node.name)) return;
      ranges.push({ from: node.from, to: node.to });
      return false;
    },
  });
  codeRangesCache.set(tree, ranges);
  return ranges;
}

/** Fin del tramo visible: hasta dónde tiene que llegar el árbol para pintarlo. */
function visibleEnd(visibleRanges) {
  let end = 0;
  for (const { to } of visibleRanges) if (to > end) end = to;
  return end;
}

/** ¿El parse ha avanzado desde el último repintado? El worker de CodeMirror
 * sigue parseando en segundo plano y avisa con una transacción sin cambios de
 * documento; sin esto, las decoraciones se quedaban con las del árbol parcial
 * para siempre (esperar no arreglaba nada). */
function parseAdvanced(update) {
  return syntaxTree(update.state) !== syntaxTree(update.startState);
}

/** ¿Cae `pos` dentro de alguna de esas zonas? Los rangos del árbol son
 * semiabiertos (`to` excluido): el `to` de un `InlineCode` es la posición justo
 * detrás del backtick de cierre, y ahí puede empezar otra construcción
 * (`` `x`->y ``, `` `x`<u>y</u> ``), que sí hay que pintar. */
function isInsideCode(codeRanges, pos) {
  return codeRanges.some((r) => pos >= r.from && pos < r.to);
}

export function buildArrowDecorations(state, visibleRanges) {
  const code = codeRangesOf(state, visibleEnd(visibleRanges));
  const ranges = [];
  for (const { from, to } of visibleRanges) {
    const text = state.doc.sliceString(from, to);
    ARROW_RE.lastIndex = 0;
    let m;
    while ((m = ARROW_RE.exec(text))) {
      const start = from + m.index;
      const end = start + m[0].length;
      if (selectionIntersects(state, start, end)) continue;
      if (isInsideCode(code, start)) continue;
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
        this.decorations = buildArrowDecorations(view.state, view.visibleRanges);
      }
      update(update) {
        if (update.docChanged || update.viewportChanged || update.selectionSet || parseAdvanced(update)) {
          this.decorations = buildArrowDecorations(update.view.state, update.view.visibleRanges);
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
  constructor(block, render, epoch = 0, index = -1, selected = false) {
    super();
    this.from = block.from;
    this.parse = block.parse;
    this.render = render;
    this.epoch = epoch;
    // Índice del bloque en `blockField` (el mismo que llevan las asas de las
    // líneas) y si está marcado. El asa de ESTE bloque vive dentro del widget
    // -- ver `blockHandleDom` --, así que la decoración tiene que pasarlos.
    this.index = index;
    this.selected = selected;
  }
  eq(other) {
    // Mismo bloque, mismo sitio y misma app: CodeMirror reutiliza el DOM (y no
    // se vuelve a pedir nada a la API) en cada repintado por mover el cursor.
    // `epoch` cambia solo cuando la app avisa de que la base cambió (M2): ahí
    // SÍ interesa un widget nuevo, que vuelva a leerla.
    return (
      other.render === this.render &&
      other.epoch === this.epoch &&
      other.from === this.from &&
      other.index === this.index &&
      other.selected === this.selected &&
      other.parse.ok === this.parse.ok &&
      other.parse.id === this.parse.id &&
      other.parse.view === this.parse.view &&
      other.parse.title === this.parse.title &&
      other.parse.motivo === this.parse.motivo
    );
  }
  ignoreEvent(event) {
    // Los clics del bloque (encabezado, celdas, «abrir») son nuestros: CodeMirror
    // no los toca. El ASA no: su mousedown/contextmenu/pulsación larga tienen
    // que llegar a los handlers del editor (seleccionar, arrastrar, menú), y
    // CodeMirror descarta los eventos de un widget cuyo `ignoreEvent` dice que
    // sí (eventBelongsToEditor).
    return !(
      event.target instanceof Element && event.target.closest(".cm-mdm-block-handle-wrap")
    );
  }
  toDOM(view) {
    const wrap = document.createElement("div");
    wrap.className = "cm-mdm-dblock";
    wrap.dataset.databaseBlock = "1";
    // Aquí no se edita nada: para cambiar la vista o los datos se abre la
    // base entera. Ni una celda con contenteditable dentro del bloque.
    wrap.contentEditable = "false";

    // El asa del bloque, DENTRO del widget: este reemplazo tapa la primera
    // línea de la valla, así que el asa que la decoración de línea pinta en su
    // sitio no se llega a ver (ver `blockHandleDom`). Va la primera para que
    // también la tenga una valla rota: su tarjeta de error es igual de movible.
    if (this.index >= 0) wrap.appendChild(blockHandleDom(this.index, this.selected));

    // Un clic en el bloque (menos en lo que ya es un botón, como el
    // encabezado: eso lleva a la base) pone el cursor en la valla, que
    // reaparece en crudo. Con ignoreEvent() este clic no lo ve CodeMirror.
    // Vale también para la tarjeta de error: una valla rota se arregla con el
    // ratón, no solo con las flechas.
    wrap.addEventListener("mousedown", (event) => {
      // El asa y los controles de la tabla tienen su propio camino (el asa, en
      // `ignoreEvent`; los controles, en `data-db-action`).
      if (event.target.closest("[data-db-action], .cm-mdm-block-handle-wrap")) return;
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

// M2: «la base ha cambiado en otra ventana, vuelve a pintar los bloques».
// Un efecto (y no un cambio de texto) porque el documento no se toca: la
// valla sigue intacta, el cursor no se mueve y no hay autoguardado.
const refreshDatabaseBlocks = StateEffect.define();

function databaseBlockDecorations(state, render, epoch = 0) {
  const ranges = [];
  if (render) {
    const doc = state.doc;
    // El asa de cada bloque de base se pinta DENTRO de su widget (ver
    // `blockHandleDom`), así que el widget necesita lo mismo que el asa de una
    // línea: el índice del bloque en `blockField` y si está marcado. `blockField`
    // se crea antes que este campo (ver la lista de extensiones), así que aquí
    // ya está.
    const { blocks, selected } = state.field(blockField);
    for (const block of scanDatabaseBlocks(doc.toString())) {
      const index = blocks.findIndex((b) => block.from >= b.from && block.from <= b.to);
      // Con el cursor DENTRO, la valla se ve en crudo y editable (es como se
      // cambia la vista o se arregla el YAML). Pero un bloque MARCADO con el
      // asa no son ganas de editar la valla: es el bloque elegido para moverlo
      // o borrarlo, y desmontar la tabla en ese momento -- justo al empezar a
      // arrastrarlo -- hacía que la nota se encogiera bajo el puntero y que la
      // línea de inserción bailara. Marcado con el asa, la tabla se queda.
      const marked = index >= 0 && selected.has(index);
      if (!marked && selectionIntersects(state, block.from, block.to)) continue;
      const widget = new DatabaseBlockWidget(block, render, epoch, index, marked);
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
      return { decos: databaseBlockDecorations(state, render, 0), epoch: 0 };
    },
    update(value, tr) {
      // `epoch` sube con cada aviso de base: los widgets nuevos no son `eq` de
      // los viejos, así que CodeMirror rehace su DOM y vuelven a pedir los
      // datos. Sin el aviso, mover el cursor reutiliza el DOM (que es lo que
      // se quiere) y no se pide nada a la API.
      const epoch = value.epoch + (tr.effects.some((e) => e.is(refreshDatabaseBlocks)) ? 1 : 0);
      if (epoch !== value.epoch || tr.docChanged || !tr.state.selection.eq(tr.startState.selection)) {
        return { decos: databaseBlockDecorations(tr.state, render, epoch), epoch };
      }
      return value;
    },
    provide: (f) => EditorView.decorations.from(f, (value) => value.decos),
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

// Los bloques del documento. El cálculo vive en block-ranges.js para que
// los tests de `node --test` lo usen sobre un árbol de verdad (el mismo
// `markdownLanguage.parser`) en vez de sobre párrafos inventados.
function computeBlocks(state) {
  // Con el documento: el rango de un bloque empieza en su línea, con la sangría
  // dentro (ver `lineStartOf` en block-ranges.js).
  return blocksFromTree(syntaxTree(state).topNode, state.doc);
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
      const from = Number(effect.value?.from);
      const to = Number(effect.value?.to);
      if (!Number.isInteger(from) || !Number.isInteger(to) || to - from < 2) continue;
      // Al final del documento no hay bloque vacío que quitar: ese hueco es
      // de los extremos y de esos se encarga `dropOuterBlankLines`.
      if (to >= tr.newDoc.length) continue;
      // El hueco es un tramo de saltos: dos cuando la frontera ya tenía su
      // línea en blanco, más cuando ha habido que crearla (insertar encima o
      // debajo de un ítem de lista, donde el separador es un solo salto).
      if (/[^\n]/.test(tr.newDoc.sliceString(from, to))) continue;
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
 * que inserta es un tramo de saltos (el hueco del bloque nuevo). Puede ser
 * más largo que dos saltos cuando la frontera no traía su línea en blanco.
 * Partir un párrafo por la mitad no deja marca: ahí los saltos separan texto
 * que ya existía, y quitarlos al salir pegaría las dos mitades otra vez. */
function emptyGapEffect(from, insert = "\n\n") {
  return /^\n{2,}$/.test(insert) ? [addEmptyGap.of({ from, to: from + insert.length })] : [];
}

/**
 * El DOM del asa de un bloque: el «+» y el «⋮⋮», con el índice del bloque en
 * `data-block-index` (de ahí lo leen los handlers de `blockHandleEventHandlers`).
 *
 * Lo comparten la decoración de línea (`BlockHandleWidget`) y el widget de una
 * base incrustada. La base se pinta sustituyendo la valla ENTERA -- primera
 * línea incluida --, y una decoración que cae DENTRO de un reemplazo no se
 * pinta nunca (CodeMirror: «Create a replace decoration which replaces the
 * given range with a widget, or simply hides it»). Por eso el asa de una base
 * no existía -- ni asa, ni arrastre, ni «+», ni Alt+↑/↓ con un punto de entrada
 * claro -- y el widget tiene que llevar el asa dentro. Tarjeta t_45697398.
 */
function blockHandleDom(index, isSelected) {
  const wrap = document.createElement("span");
  wrap.className = "cm-mdm-block-handle-wrap";

  const addBtn = document.createElement("span");
  addBtn.className = "cm-mdm-block-add";
  addBtn.textContent = "+";
  addBtn.dataset.blockIndex = String(index);
  addBtn.title = "Añadir un bloque después de este · Alt+clic: antes";

  const handle = document.createElement("span");
  handle.className = "cm-mdm-block-handle" + (isSelected ? " cm-mdm-block-handle-selected" : "");
  handle.textContent = "⋮⋮";
  handle.dataset.blockIndex = String(index);
  handle.title =
    "Clic: seleccionar bloque · Mayús/Ctrl+clic: varios · clic derecho: acciones · arrastrar: mover";

  wrap.append(addBtn, handle);
  return wrap;
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
    return blockHandleDom(this.index, this.isSelected);
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
      lastPointerType = event.pointerType;
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
        const dot = target.closest(".cm-mdm-block-dot");
        if (dot) {
          event.preventDefault();
          event.stopPropagation();
          // Sin esto el punto seguiría puesto en el bloque de antes y el
          // usuario acabaría con dos bloques nuevos si vuelve a pulsar.
          view.dispatch({ effects: setDotBlock.of(-1) });
          insertBlockAfter(view, Number(dot.dataset.blockIndex));
          return true;
        }

        const addBtn = target.closest(".cm-mdm-block-add");
        if (addBtn) {
          event.preventDefault();
          // Alt+clic: el bloque nuevo va ENCIMA del actual. Sin Alt (lo de
          // siempre): debajo. El atajo es el mismo que «Insertar bloque
          // arriba» del menú del asa, que es lo que hay en móvil.
          const idx = Number(addBtn.dataset.blockIndex);
          if (event.altKey) insertBlockBefore(view, idx);
          else insertBlockAfter(view, idx);
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
          // Y con el ratón, el mismo mousedown arranca el posible arrastre:
          // mientras no se mueva más de unos píxeles no es arrastre (es clic,
          // que ya ha seleccionado). En táctil no: ahí el gesto es del menú
          // de pulsación larga (ver `beginBlockDrag`).
          if (event.button === 0 && lastPointerType !== "touch") beginBlockDrag(view, event, idx);
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
  const ordered = [...indices].sort((a, b) => a - b);
  const first = ordered[0];
  const last = ordered[ordered.length - 1];
  // Si es un único bloque, se puede decir de qué tipo es ya mismo; con varios
  // seleccionados (posiblemente de tipos distintos) no.
  const currentTypeLabel = indices.size === 1 ? BLOCK_TYPE_LABEL[blocks[first].type] ?? null : null;

  onBlockContextMenu({
    x,
    y,
    count: indices.size,
    currentTypeLabel,
    types: BLOCK_TYPES,
    convertTo: (type) => convertBlocks(view, indices, type),
    duplicateBlocks: () => duplicateBlocks(view, indices),
    deleteBlocks: () => deleteBlocks(view, indices),
    // Insertar arriba es la versión sin ratón del Alt+clic en el «+» (lo que
    // hace falta en móvil): el bloque nuevo va antes del primero marcado.
    insertAbove: () => insertBlockBefore(view, first),
    // Mover con el menú: la vía táctil del reordenado (en táctil no hay
    // arrastre). Solo tiene sentido con un bloque; con varios marcados la app
    // no ofrece estas dos opciones.
    canMoveUp: first > 0,
    canMoveDown: last < blocks.length - 1,
    moveUp: () => moveBlockTo(view, first, first - 1),
    moveDown: () => moveBlockTo(view, last, last + 2),
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

// Botón "+" del asa: añade un bloque de párrafo vacío justo DEBAJO del bloque
// indicado (clic normal) o ENCIMA con Alt+clic (pedido del dueño: «no puedo
// insertar un bloque antes de otro»). No necesariamente donde esté el cursor
// ahora mismo -- el usuario puede clicar el "+" de cualquier bloque. Mismo
// mecanismo que ⏎: el hueco en blanco resultante se ve colapsado por
// blankLinePlugin hasta que el cursor entra en él, y si se abandona sin
// escribir no queda aire en el `.md`. Qué par de saltos se inserta y dónde lo
// decide block-move.js (`insertBlockEdit`), con sus pruebas en node --test.
function insertBlockBeside(view, index, where) {
  const { blocks } = view.state.field(blockField);
  const edit = insertBlockEdit({ text: view.state.doc.toString(), blocks, index, where });
  if (!edit) return false;
  view.dispatch({
    changes: edit.changes,
    selection: { anchor: edit.cursor },
    effects: [setSelectedBlocks.of(new Set()), ...emptyGapEffect(edit.gapFrom, edit.changes[0].insert)],
  });
  view.focus();
  return true;
}

function insertBlockAfter(view, index) {
  return insertBlockBeside(view, index, "below");
}

function insertBlockBefore(view, index) {
  return insertBlockBeside(view, index, "above");
}

/**
 * Mover el bloque `index` a la posición `slot` (0 = antes del primero,
 * `blocks.length` = después del último) en UNA transacción: el `.md` cambia
 * una sola vez y un solo ⌘Z deshace el movimiento entero.
 *
 * Qué trozo de texto cambia de sitio -- y la reenumeración de la lista, si es
 * numerada -- se decide en block-move.js. `cursor` conserva el punto dentro
 * del bloque movido, para que Alt+↑ repetido siga moviendo el mismo bloque.
 */
export function moveBlockTo(view, index, slot, cursor = null) {
  const { blocks } = view.state.field(blockField);
  const text = view.state.doc.toString();
  const edit = moveBlockEdits({ text, blocks, index, slot, cursor });
  if (!edit) return false;
  view.dispatch({
    changes: edit.changes,
    selection: { anchor: edit.cursor },
    effects: setSelectedBlocks.of(new Set()),
    userEvent: "move.block",
    scrollIntoView: true,
  });
  view.focus();
  return true;
}

// ------------------------------------------------------------- arrastrar el asa
//
// Arrastrar el asa (⋮⋮) reordena: mientras se arrastra, una línea horizontal
// marca el hueco entre bloques donde va a caer, y al soltar el bloque se mueve
// (una sola transacción, igual que con el teclado).
//
// En táctil NO se arrastra: la pulsación larga ya abre el menú de bloque, y un
// gesto de arrastre competiría con el desplazamiento de la nota. Ahí el
// movimiento va por «Mover arriba» / «Mover abajo» del menú, que es
// determinista y no necesita apuntar a un hueco.
const DRAG_SLOP = 4;
const DRAG_EDGE = 44;
let blockDrag = null;
let lastPointerType = "mouse";

/** Hueco (slot) más cercano a la altura `y`: entre los bloques cuyo punto
 * medio queda por debajo del puntero. */
function dropSlotAt(view, y) {
  const { blocks } = view.state.field(blockField);
  for (let i = 0; i < blocks.length; i++) {
    const top = view.coordsAtPos(blocks[i].from, -1);
    const bottom = view.coordsAtPos(blocks[i].to, 1) || top;
    if (!top) continue;
    const mid = (top.top + (bottom ? bottom.bottom : top.bottom)) / 2;
    if (y < mid) return i;
  }
  return blocks.length;
}

function placeDropLine(view, slot) {
  if (!blockDrag) return;
  if (!blockDrag.line) {
    blockDrag.line = document.createElement("div");
    blockDrag.line.className = "cm-mdm-drop-line";
    document.body.appendChild(blockDrag.line);
  }
  const { blocks } = view.state.field(blockField);
  const content = view.contentDOM.getBoundingClientRect();
  let top = content.top;
  if (slot <= 0 && blocks.length) {
    const c = view.coordsAtPos(blocks[0].from, -1);
    if (c) top = c.top;
  } else if (slot >= blocks.length && blocks.length) {
    const last = blocks[blocks.length - 1];
    const c = view.coordsAtPos(last.to, 1) || view.coordsAtPos(last.from, -1);
    if (c) top = c.bottom;
  } else if (blocks.length) {
    const c = view.coordsAtPos(blocks[slot].from, -1);
    if (c) top = c.top;
  }
  const line = blockDrag.line;
  line.style.top = `${Math.round(top) - 1}px`;
  line.style.left = `${Math.round(content.left)}px`;
  line.style.width = `${Math.round(content.width)}px`;
}

/** El documento se desplaza solo cuando el puntero se acerca al borde de la
 * zona visible: sin esto no se puede arrastrar más allá de una pantalla. */
function autoScrollWhileDragging(view, y) {
  const rect = view.scrollDOM.getBoundingClientRect();
  if (y < rect.top + DRAG_EDGE) view.scrollDOM.scrollTop -= 14;
  else if (y > rect.bottom - DRAG_EDGE) view.scrollDOM.scrollTop += 14;
}

function onBlockDragMove(event) {
  const drag = blockDrag;
  if (!drag) return;
  // El botón ya no está pulsado: el `mouseup` se perdió (se soltó fuera de la
  // ventana, o la pestaña se quedó sin foco). Sin esto, el arrastre seguía
  // vivo, la línea de inserción se quedaba pintada y el siguiente clic movía
  // un bloque que nadie había vuelto a arrastrar.
  if (event.buttons === 0) {
    endBlockDrag();
    return;
  }
  if (!drag.active) {
    if (Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < DRAG_SLOP) return;
    drag.active = true;
    // El cursor de "agarrando" durante todo el arrastre (también fuera del asa)
    // y el texto del bloque no se selecciona sin querer.
    document.body.classList.add("cm-mdm-dragging");
  }
  event.preventDefault();
  autoScrollWhileDragging(drag.view, event.clientY);
  drag.slot = dropSlotAt(drag.view, event.clientY);
  placeDropLine(drag.view, drag.slot);
}

function endBlockDrag() {
  window.removeEventListener("mousemove", onBlockDragMove, true);
  window.removeEventListener("mouseup", onBlockDragEnd, true);
  window.removeEventListener("blur", endBlockDrag, true);
  const drag = blockDrag;
  blockDrag = null;
  if (drag?.line) drag.line.remove();
  document.body.classList.remove("cm-mdm-dragging");
  return drag;
}

function onBlockDragEnd(event) {
  const drag = blockDrag;
  if (!drag) return;
  const wasActive = drag.active;
  // El último hueco se recalcula con la altura de la suelta: puede no haber
  // habido un mousemove justo antes del mouseup.
  const slot = wasActive ? dropSlotAt(drag.view, event.clientY) : null;
  const view = drag.view;
  const index = drag.index;
  endBlockDrag();
  if (wasActive) moveBlockTo(view, index, slot, null);
}

function beginBlockDrag(view, event, index) {
  endBlockDrag(); // un arrastre anterior sin `mouseup` no puede sobrevivir a otro
  blockDrag = { view, index, startX: event.clientX, startY: event.clientY, active: false, slot: null, line: null };
  window.addEventListener("mousemove", onBlockDragMove, true);
  window.addEventListener("mouseup", onBlockDragEnd, true);
  window.addEventListener("blur", endBlockDrag, true);
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

/** Líneas en blanco entre el bloque anterior y la primera línea de `block`:
 * el aire que el autor ya escribió. Se cuentan hacia arriba desde la línea del
 * bloque y se para en la primera con contenido o al entrar en el bloque de
 * arriba (una línea vacía DENTRO de una valla de código es de ese bloque, no
 * del hueco). */
export function blankLinesBefore(doc, block, prev) {
  let blanks = 0;
  for (let number = doc.lineAt(block.from).number - 1; number >= 1; number--) {
    const line = doc.line(number);
    if (line.length !== 0) break;
    if (line.from < prev.to) break;
    blanks++;
  }
  return blanks;
}

function blockSpacingDecorations(state) {
  const { blocks } = state.field(blockField);
  const ranges = [];
  blocks.forEach((b, i) => {
    if (i === 0) return;
    const prev = blocks[i - 1];
    if (LIST_BLOCK_TYPES.has(b.type) && LIST_BLOCK_TYPES.has(prev.type)) return;
    const cls = BLOCK_SPACING_CLASS[b.type] || "cm-mdm-space-normal";
    // El aire de arriba lo pone el tipo del bloque... salvo que tenga líneas en
    // blanco delante: entonces lo ponen ELLAS (cada una mide lo que le toca por
    // el bloque de arriba, ver blankLinePlugin) y el bloque no suma su margen.
    // `--mdm-flush: 1` anula el padding-top en CSS. Antes se restaba
    // `--mdm-line` por línea y quedaba un max(): un párrafo con una línea en
    // blanco delante se quedaba en la línea entera (24 px, el «hueco del
    // doble») y un H1 volvía a sus 28 px en vez de los 18 de la línea.
    const flush = blankLinesBefore(state.doc, b, prev) > 0;
    const line = flush
      ? Decoration.line({ class: cls, attributes: { style: "--mdm-flush: 1" } })
      : Decoration.line({ class: cls });
    ranges.push(line.range(b.from));
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

// ------------------------------------------------- subrayado (`<u>x</u>`)
//
// Markdown no tiene subrayado -- `__x__` es negrita --, así que ⌘U escribe
// `<u>x</u>`, que es lo que renderizan Obsidian y GitHub. Aquí se PINTA: las
// etiquetas se esconden como el resto de marcas (o se ven tenues, con
// `cm-mdm-mark`, mientras se edita ese bloque) y el texto de dentro se
// subraya (clase `cm-mdm-underline`, en frontend/index.html).
//
// El parser entrega `<u>` y `</u>` como dos nodos HTMLTag SUELTOS -- no
// envuelven el texto --, así que emparejarlos es cosa de `underlineSpans`
// (format.js), que escanea el texto visible. Del resto de HTML no se toca
// nada: hoy no se pinta, y sigue sin pintarse.
export function underlineDecorations(state, visibleRanges) {
  const code = codeRangesOf(state, visibleEnd(visibleRanges));
  const ranges = [];
  for (const { from, to } of visibleRanges) {
    const spans = underlineSpans(state.doc.sliceString(from, to), from);
    for (const s of spans) {
      // Dentro de código no hay subrayado que pintar: son caracteres más
      // (aquí entra también el `código` en línea, ver `codeRangesOf`).
      // Se miran las DOS etiquetas: un par que abre fuera y cierra dentro
      // cruzaría el borde y escondería un `</u>` que ahí es literal (y el
      // subrayado entraría en los backticks). En ese caso se descarta el par
      // entero, en vez de recortarlo al borde: con el `</u>` a la vista, un
      // subrayado que se corta en el backtick se lee peor que las etiquetas
      // tal cual.
      if (isInsideCode(code, s.openFrom) || isInsideCode(code, s.closeFrom)) continue;
      const active = selectionIntersects(state, s.openFrom, s.closeTo);
      if (active) {
        ranges.push(Decoration.mark({ class: "cm-mdm-mark" }).range(s.openFrom, s.openTo));
        ranges.push(Decoration.mark({ class: "cm-mdm-mark" }).range(s.closeFrom, s.closeTo));
      } else {
        ranges.push(Decoration.replace({}).range(s.openFrom, s.openTo));
        ranges.push(Decoration.replace({}).range(s.closeFrom, s.closeTo));
      }
      if (s.innerTo > s.innerFrom) {
        ranges.push(Decoration.mark({ class: "cm-mdm-underline" }).range(s.innerFrom, s.innerTo));
      }
    }
  }
  return Decoration.set(ranges, true);
}

function underlinePlugin() {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = underlineDecorations(view.state, view.visibleRanges);
      }
      update(update) {
        if (update.docChanged || update.viewportChanged || update.selectionSet || parseAdvanced(update)) {
          this.decorations = underlineDecorations(update.view.state, update.view.visibleRanges);
        }
      }
    },
    { decorations: (v) => v.decorations }
  );
}

// Una línea en blanco es una línea del editor como cualquier otra: ocupa su
// altura, y así el hueco entre dos bloques delata cuántas hay -- una deja el
// aire de UNA línea, dos el doble (pedido del dueño: «que los bloques vacíos
// dejen espacio, aunque estén vacíos»). Lo que cambió después (t_11752efb) es
// CUÁNTO mide esa línea: un valor único (24 px, la línea entera) se veía como
// un bloque vacío de más. Ahora mide lo que pidió el dueño: depende del bloque
// que tiene ENCIMA -- un H1 deja más aire que un párrafo, un ítem de lista
// menos que un párrafo -- y N líneas en blanco seguidas miden N veces ese alto.
// El número viaja a CSS en `--mdm-blank-unit`: el orden H1 > H2 > texto > lista
// se cambia en un solo sitio (`BLANK_UNIT_PX`, con su test).
//
// Esto solo MARCA líneas: no cambia el documento, ni su texto, ni su longitud.
//
// - `cm-mdm-blank` en toda línea vacía fuera de un bloque de código (ahí una
//   línea en blanco es contenido de verdad, no un separador).
// - `cm-mdm-blank-after-*` con la familia del bloque de arriba: de ahí sale su
//   alto (`--mdm-blank-unit`).
// - `cm-mdm-last-line` si es la última del documento, para que la línea
//   clicable del final (el `::after` de .cm-content) no pinte una de más
//   cuando la del final ya es una línea vacía de verdad.
//
// El punto que dice «aquí se escribe» NO vive aquí: es del bloque que tiene el
// ratón encima (ver blockDotPlugin), para que no haya puntos sueltos en los
// huecos ni en una nota vacía.
const BLANK_UNIT_PX = { h1: 18, md: 14, p: 12, li: 8 };

/** Familia de alto del bloque de encima: el H1 tiene la suya; H2-H5, código,
 * cita, línea horizontal y tabla la «media»; el texto la corta; la lista, la
 * mínima. */
const BLANK_UNIT_FAMILY = {
  h1: "h1",
  h2: "md",
  h3: "md",
  h4: "md",
  h5: "md",
  code: "md",
  quote: "md",
  hr: "md",
  other: "md",
  paragraph: "p",
  bullet_list_item: "li",
  ordered_list_item: "li",
};

/** Alto (px) de UNA línea en blanco que tiene encima un bloque de este tipo. */
export function blankUnitPx(prevType) {
  return BLANK_UNIT_PX[BLANK_UNIT_FAMILY[prevType]] ?? BLANK_UNIT_PX.p;
}

/** Bloque que queda ENCIMA de `pos` (el último que acaba antes), o `null` si
 * arriba no hay ningún bloque. */
function blockAbove(blocks, pos) {
  let found = null;
  for (const b of blocks) {
    if (b.to <= pos) found = b;
    else break;
  }
  return found;
}

/** Clases de una línea en blanco. Puro (sin `view`): los tests de `node --test`
 * fijan con esto el alto relativo a cada bloque y que toda una tirada de líneas
 * en blanco comparte familia -- de ahí sale el «N veces» del CSS. */
export function blankLineClasses(doc, blocks, line) {
  const prev = blockAbove(blocks, line.from);
  const classes = ["cm-mdm-blank", `cm-mdm-blank-after-${BLANK_UNIT_FAMILY[prev?.type] ?? "p"}`];
  if (line.number === doc.lines) classes.push("cm-mdm-last-line");
  if (doc.length === 0) classes.push("cm-mdm-empty-line");
  return classes;
}

function blankLineDecorations(view) {
  const ranges = [];
  const { state } = view;
  const doc = state.doc;
  const { blocks } = state.field(blockField);
  // Al crear la vista (nota recién abierta) los rangos visibles todavía están
  // vacíos: sin este respaldo, la única línea de una nota nueva se quedaba sin
  // `cm-mdm-last-line` y el hueco del final (el `::after` de .cm-content) se
  // pintaba como una SEGUNDA línea, con su punto. Se decora el documento
  // entero solo en ese caso.
  const visible = view.visibleRanges.length ? view.visibleRanges : [{ from: 0, to: doc.length }];
  const code = codeRangesOf(state, visible[visible.length - 1].to);
  for (const { from, to } of visible) {
    const firstLine = doc.lineAt(from).number;
    const lastLine = doc.lineAt(to).number;
    for (let n = firstLine; n <= lastLine; n++) {
      const line = doc.line(n);
      if (line.length !== 0) continue;
      if (isInsideCode(code, line.from)) continue;
      const prev = blockAbove(blocks, line.from);
      ranges.push(
        Decoration.line({
          class: blankLineClasses(doc, blocks, line).join(" "),
          attributes: { style: `--mdm-blank-unit: ${blankUnitPx(prev?.type)}px` },
        }).range(line.from)
      );
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
        if (update.docChanged || update.viewportChanged || update.selectionSet || parseAdvanced(update)) {
          this.decorations = blankLineDecorations(update.view);
        }
      }
    },
    { decorations: (v) => v.decorations }
  );
}

// ------------------------------------------------- el punto del bloque
//
// El indicador de «aquí se escribe» es UN punto, y es del bloque que tiene el
// ratón encima: se pinta en su PRIMERA línea, en el margen izquierdo, y no hay
// ninguno más (ni en las líneas en blanco de en medio, ni suelto debajo del
// último bloque de una nota vacía -- eran los puntos permanentes que veía el
// dueño, porque antes el criterio era «la línea donde está el cursor»).
// Clicarlo deja el cursor en una línea nueva justo debajo del bloque, lista
// para escribir: al final de la nota, es la forma de seguir escribiendo sin
// buscar el hueco.
//
// Va como widget EN LÍNEA con ancho 0 (`vertical-align: middle`), no como
// decoración de línea con `::after`: así queda a la altura del texto de su
// primera línea sin una regla por tipo de bloque (un H1 no mide lo mismo que un
// párrafo, y su línea lleva padding-top propio). El desplazamiento al margen lo
// hace `left` negativo en CSS (index.html).
const setDotBlock = StateEffect.define();

const dotField = StateField.define({
  create: () => -1,
  update(value, tr) {
    for (const e of tr.effects) if (e.is(setDotBlock)) value = e.value;
    if (value < 0) return -1;
    // El índice tiene que seguir existiendo: borrar bloques con el punto puesto
    // dejaría un widget colgando del bloque equivocado.
    return value < tr.state.field(blockField).blocks.length ? value : -1;
  },
});

class BlockDotWidget extends WidgetType {
  constructor(index) {
    super();
    this.index = index;
  }
  eq(other) {
    return other.index === this.index;
  }
  toDOM() {
    const wrap = document.createElement("span");
    wrap.className = "cm-mdm-block-dot-wrap";
    const dot = document.createElement("span");
    dot.className = "cm-mdm-block-dot";
    dot.dataset.blockIndex = String(this.index);
    dot.title = "Escribir un bloque debajo";
    wrap.append(dot);
    return wrap;
  }
  ignoreEvent() {
    return false; // el clic lo maneja blockHandleEventHandlers
  }
}

/** ¿Manda el foco y no el ratón? En táctil no hay `:hover`: allí el punto va en
 * el bloque donde está el cursor (el del final, al abrir la nota). */
function coarsePointer() {
  return typeof window !== "undefined" && !!window.matchMedia?.("(pointer: coarse)").matches;
}

/** Índice del bloque al que pertenece `pos`, o -1 si cae en un hueco (una línea
 * en blanco entre dos bloques no es de nadie: ahí no hay punto). */
function blockIndexAtPos(state, pos) {
  if (pos == null) return -1;
  const { blocks } = state.field(blockField);
  return blocks.findIndex((b) => pos >= b.from && pos <= b.to);
}

/** ¿El puntero está DENTRO del bloque (4 px de gracia arriba y abajo)? Hace
 * falta porque `posAtCoords` pega el hueco de debajo del último bloque al final
 * del texto: sin esto, el punto aparecía también con el ratón en la banda
 * vacía del final, que es justo el «punto suelto donde no hay bloque». */
function pointerInsideBlock(view, index, y) {
  const b = view.state.field(blockField).blocks[index];
  if (!b) return false;
  const top = view.coordsAtPos(b.from, -1);
  const bottom = view.coordsAtPos(b.to, 1) || top;
  if (!top || !bottom) return false;
  return y >= top.top - 4 && y <= bottom.bottom + 4;
}

function dotDecorations(state) {
  const index = coarsePointer() ? blockIndexAtPos(state, state.selection.main.head) : state.field(dotField);
  if (index < 0) return Decoration.none;
  const b = state.field(blockField).blocks[index];
  if (!b) return Decoration.none;
  return Decoration.set([Decoration.widget({ widget: new BlockDotWidget(index), side: -1 }).range(b.from)], true);
}

function blockDotPlugin() {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = dotDecorations(view.state);
      }
      update(update) {
        if (
          update.docChanged ||
          update.selectionSet ||
          update.state.field(dotField) !== update.startState.field(dotField) ||
          update.state.field(blockField) !== update.startState.field(blockField)
        ) {
          this.decorations = dotDecorations(update.state);
        }
      }
    },
    { decorations: (v) => v.decorations }
  );
}

function blockDotEventHandlers() {
  return EditorView.domEventHandlers({
    mousemove(event, view) {
      let index = -1;
      if (!coarsePointer()) {
        const pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
        index = blockIndexAtPos(view.state, pos);
        if (index >= 0 && !pointerInsideBlock(view, index, event.clientY)) index = -1;
      }
      if (index !== view.state.field(dotField)) view.dispatch({ effects: setDotBlock.of(index) });
      return false;
    },
    // El puntero se va del cuerpo: sin ratón encima, ningún punto.
    mouseleave(event, view) {
      if (view.state.field(dotField) !== -1) view.dispatch({ effects: setDotBlock.of(-1) });
      return false;
    },
  });
}

// Nota vacía: una sola línea con un texto de ayuda. El placeholder es SOLO
// visual (el `::before` de `.cm-mdm-empty-line`, en index.html): el `.md` no
// gana ni un carácter al abrir la nota. La marca la pone blankLineDecorations
// en la única línea del documento vacío, no una clase del editor -- así no hay
// que repintar nada al crear la vista.
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
// por título (ver ARCHITECTURE.md), así que dos "Nueva página" son ambiguas.
//
// Elegir "Página" abre el selector de páginas (modo "page"): lo que se escribe
// detrás de "/page" filtra las del vault y, al elegir una, se inserta
// `[[Título]]` sin crear nada ni navegar. Lo escrito que no coincide con
// ninguna es el nombre de la nueva (modo "naming", Enter la crea): la app la
// crea hija de esta nota, el editor quita el nombre del cuerpo y la app
// guarda lo pendiente y NAVEGA a ella -- antes se quedaba en la nota original
// y el dueño lo pidió al revés. Si el nombre es el de la nota actual o el de
// una hija, el selector ofrece renombrarla en vez de crear un duplicado.
// Escape cancela y deja el nombre ya escrito como texto normal (no se pierde).
async function createPageFromName(view, from, to, onCreatePage, onOpenPage) {
  const escrito = view.state.doc.sliceString(from, to);
  const title = escrito.trim();
  if (!title) {
    view.focus();
    return;
  }
  let created = null;
  try {
    created = await onCreatePage?.(title);
  } catch {
    created = null; // la app ya avisó: no hay página y lo escrito se queda
  }
  if (!created || !created.id) {
    view.focus();
    return;
  }
  // Nos vamos a la página nueva, así que el nombre tecleado aquí no puede
  // quedarse de párrafo suelto en la nota que se deja. Si mientras se creaba el
  // usuario siguió escribiendo en ese tramo, no se pisa: se navega igual y su
  // texto se queda (el guardado de al lado lo escribe tal cual).
  const doc = view.state.doc;
  const fin = Math.min(to, doc.length);
  if (fin >= from && doc.sliceString(from, fin) === escrito) {
    view.dispatch({ changes: { from, to: fin, insert: "" } });
  }
  // Espera al guardado de la nota anterior ANTES de navegar: es la promesa que
  // hace que «/page no pierde ni una letra» sea verdad y no una carrera con el
  // autoguardado de 2 s.
  await onOpenPage?.(created.id);
}

// «Renombrar» del selector de /page: la app renombra el `.md` (PATCH del
// título) y aquí se deja el enlace nuevo. En la nota que se está mirando no se
// enlaza: un `[[ella misma]]` no aporta nada, así que solo se quita el nombre
// tecleado.
async function renamePageFromName(view, from, to, entry, onRenamePage, currentPageId) {
  const escrito = view.state.doc.sliceString(from, to);
  const title = escrito.trim();
  if (!entry?.id || !title) {
    view.focus();
    return;
  }
  let renamed = null;
  try {
    renamed = await onRenamePage?.(entry.id, title);
  } catch {
    renamed = null;
  }
  if (!renamed || !renamed.id) {
    view.focus();
    return;
  }
  const doc = view.state.doc;
  const fin = Math.min(to, doc.length);
  if (fin < from || doc.sliceString(from, fin) !== escrito) {
    view.focus(); // el usuario siguió escribiendo: no se pisa
    return;
  }
  const esLaNotaAbierta =
    currentPageId != null && String(currentPageId) === String(renamed.id);
  const insert = esLaNotaAbierta ? "" : linkMarkup(renamed.title);
  view.dispatch({ changes: { from, to: fin, insert }, selection: { anchor: from + insert.length } });
  view.focus();
}

// La base del comando "/base" con nombre nuevo: mismo patrón que
// `createPageFromName` -- si la app dice que no se creó (nombre ocupado, error
// de la API) no se inserta nada y el nombre escrito se queda en la nota, para
// poder cambiarlo y reintentar. Cuando sí se crea, lo que queda es el bloque
// apuntando a la base de verdad y el cursor en el párrafo de debajo.
async function createDatabaseFromName(view, from, to, onCreateDatabase) {
  const escrito = view.state.doc.sliceString(from, to);
  const title = escrito.trim();
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
  // que era, no se pisa nada. Se compara con lo escrito TAL CUAL: el nombre va
  // sin espacios de sobra, pero los del texto siguen ahí y también hay que
  // sustituirlos (comparando con el nombre recortado, un nombre tecleado con
  // un espacio detrás creaba la base y dejaba el nombre entero en la nota).
  const doc = view.state.doc;
  if (!creada || doc.sliceString(from, to) !== escrito) {
    view.focus();
    return;
  }
  // Misma separación que al elegir una base existente (ver `pickDatabase`):
  // una línea en blanco a cada lado, sin pegar el bloque al párrafo de arriba
  // ni dejar saltos de más detrás.
  const { text, cursorOffset, dropAfter } = databaseBlockInsert(creada, {
    before: doc.sliceString(0, from),
    after: doc.sliceString(to),
  });
  view.dispatch({
    changes: { from, to: to + dropAfter, insert: text },
    selection: { anchor: from + cursorOffset },
  });
  view.focus();
}

function slashMenuExtension({
  onMenuUpdate,
  onCreatePage,
  onOpenPage,
  onRenamePage,
  onListPages,
  onListDatabases,
  onPickDatabase,
  onCreateDatabase,
  currentPageId,
}) {
  // La instancia viva, para que la app pueda cerrar el selector desde fuera
  // (`discardTransientText`, más abajo): el plugin es la única pieza que sabe
  // qué hay escrito en el documento por culpa del menú.
  let instancia = null;

  const plugin = ViewPlugin.fromClass(
    class {
      constructor(view) {
        instancia = this;
        this.view = view;
        this.mode = null; // null | "menu" | "page" | "database" | "naming"
        this.from = -1;
        this.selectedIndex = 0;
        this.namingKind = "page"; // "page" | "database" | "rename"
        this.renameTarget = null; // página que se está renombrando (modo "naming")
        this.entries = []; // bases del vault, pedidas al abrir el selector
        this.pages = []; // páginas del vault, pedidas al abrir el selector
        this.pageCache = null; // lista de /page ya recortada (ver pageView)
        this.pageCacheKey = null; // consulta con la que se calculó
        this.pageCachePages = null; // `pages` con la que se calculó
      }

      get query() {
        if (this.mode !== "menu" && this.mode !== "page" && this.mode !== "database") return "";
        const pos = this.view.state.selection.main.head;
        if (pos < this.from) return "";
        // En el menú "/" la consulta empieza detrás de la barra y acaba en el
        // caret, como en cualquier autocompletado. En el selector de bases no
        // hay barra y manda la línea entera: el filtro es lo que se ve escrito
        // en el documento, no lo que queda a la izquierda del cursor (ver
        // `filterRange`).
        if (this.mode === "database" || this.mode === "page") {
          return this.view.state.doc.sliceString(this.from, this.filterRange().to);
        }
        return this.view.state.doc.sliceString(this.from + 1, pos);
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

      /**
       * Selector de páginas: las del vault (recortadas al tope de filas), la de
       * crear y la de renombrar. Devuelve `{options, hidden}`; `hidden` es
       * cuántas páginas no se pintan (ver capPageChoices en page-choice.js).
       *
       * Se cachea por consulta porque `filterPages` recorre y ordena el vault
       * entero y esto se llama más de una vez por tecla -- el clamp de
       * `update()` y el `notify()` que pinta --: sin caché, el trabajo de cada
       * pulsación se duplicaría justo en el caso que el tope viene a arreglar.
       */
      get pageView() {
        if (this.mode !== "page") return { options: [], hidden: 0 };
        const key = this.query;
        if (this.pageCache && this.pageCacheKey === key && this.pageCachePages === this.pages) {
          return this.pageCache;
        }
        this.pageCache = capPageChoices(
          pageChoices(this.pages, key, { currentId: currentPageId }),
        );
        this.pageCacheKey = key;
        this.pageCachePages = this.pages;
        return this.pageCache;
      }

      /** Las opciones que se están enseñando (menú "/" o un selector). */
      get activeOptions() {
        if (this.mode === "database") return this.databaseOptions;
        if (this.mode === "page") return this.pageView.options;
        return this.filteredOptions;
      }

      /**
       * Tramo que ocupa lo tecleado en el selector de bases: `from` (donde
       * empezó el filtro) hasta el FINAL DE LA LÍNEA, no hasta el caret.
       *
       * El filtro -- y, al crear, el nombre de la base -- se escribe dentro del
       * documento, así que lo que hay que sustituir (o borrar al cancelar) es
       * todo lo escrito en esa línea. Tomando el caret como final, con el
       * cursor a mitad del texto el nombre se creaba cortado y el resto del
       * filtro se quedaba de párrafo suelto detrás del bloque.
       */
      filterRange() {
        return this.mode === "page"
          ? pageFilterRange(this.view.state.doc, this.from)
          : databaseFilterRange(this.view.state.doc, this.from);
      }

      /** Tramo que ocupa el filtro del selector, o `null` si no hay selector
       * abierto o el filtro está vacío. Es lo escrito en el documento que NO
       * es texto de la nota (ver `getValue`, más abajo). */
      transientRange() {
        if (this.mode === "page") return pageFilterClear(this.view.state.doc, this.from);
        if (this.mode !== "database") return null;
        return databaseFilterClear(this.view.state.doc, this.from);
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
        if (this.mode === "menu" || this.mode === "page" || this.mode === "database") {
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
          const target = this.namingKind; // "page" | "database" | "rename"
          const renameTitle = this.renameTarget?.title || "";
          setTimeout(() => {
            if (this.mode !== "naming" || this.from < 0) return; // pudo cerrarse mientras tanto
            const coords = this.view.coordsAtPos(Math.min(from, this.view.state.doc.length));
            onMenuUpdate({
              kind: "naming",
              target,
              renameTitle,
              x: coords?.left ?? 0,
              y: coords?.bottom ?? 0,
            });
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
        let note = null;
        if (this.mode === "database" && this.entries.length === 0) {
          note = "Aún no hay ninguna base en el vault.";
        } else if (this.mode === "page" && this.pages.length === 0) {
          note = "Aún no hay ninguna página en el vault. Escribe un nombre para crear la primera.";
        }
        // coordsAtPos() lee el layout del DOM -- CodeMirror no permite
        // hacerlo dentro del propio ciclo de update() ("Reading the editor
        // layout isn't allowed during an update"), así que se difiere fuera
        // de la pila de la transacción actual. setTimeout(…, 0) en vez de
        // requestAnimationFrame: solo necesitamos salir del update() en
        // curso, no esperar a un frame de composición real (que además no
        // se dispara si la pestaña no está pintando).
        setTimeout(() => {
          if ((this.mode !== "menu" && this.mode !== "page" && this.mode !== "database") || this.from < 0) {
            return; // pudo cerrarse mientras tanto
          }
          const coords = this.view.coordsAtPos(this.from);
          onMenuUpdate({
            kind: "menu",
            x: coords?.left ?? 0,
            y: coords?.bottom ?? 0,
            selectedIndex,
            options,
            note,
            // Cuántas páginas deja fuera el tope de filas del selector de
            // /page: solo la app sabe pintarlo («… y N más»), y es 0 en el
            // menú "/" y en el selector de bases.
            overflow: this.mode === "page" ? this.pageView.hidden : 0,
          });
        });
      }

      /** Cierra el menú sin tocar el documento, y limpia su estado. */
      dismiss() {
        if (!this.mode) return;
        this.mode = null;
        this.from = -1;
        this.namingKind = "page";
        this.renameTarget = null;
        onMenuUpdate(null);
      }

      close() {
        if (!this.mode) return;
        // Cancelar el selector de bases se lleva por delante el filtro
        // tecleado: se escribió dentro del documento como UI efímera, no como
        // texto de la nota (si no, «zzz» se queda de párrafo). En el menú "/"
        // no se borra nada: ahí lo escrito es texto de la nota y el menú se
        // cierra por cualquier motivo.
        const filtro =
          this.mode === "database" || this.mode === "page" ? this.filterRange() : null;
        this.dismiss();
        if (filtro) this.dropFilter(filtro);
      }

      /**
       * Borra del documento el filtro de un selector ya cerrado.
       *
       * Va en un microtask y no en el acto porque `close()` también se llama
       * desde `update()` (cuando el caret sale de la línea) y ahí despachar una
       * transacción no está permitido -- "Calls to EditorView.update are not
       * allowed while an update is in progress" --, la misma mecánica que
       * `dropOuterBlankLines`. Si mientras tanto ese tramo ha cambiado, no se
       * borra a ciegas.
       */
      dropFilter({ from, to }) {
        const view = this.view;
        const escrito = view.state.doc.sliceString(from, Math.min(to, view.state.doc.length));
        queueMicrotask(() => {
          if (!view.dom.isConnected) return; // la nota se cerró mientras tanto
          const doc = view.state.doc;
          const fin = Math.min(to, doc.length);
          // Cancelar con el filtro vacío no despacha nada: no hay nada que
          // borrar (y una transacción vacía solo estorbaría en el historial).
          if (fin <= from || doc.sliceString(from, fin) !== escrito) return;
          view.dispatch({ changes: { from, to: fin, insert: "" } });
        });
      }

      /**
       * Cierra el menú que hubiera abierto y borra lo que haya escrito en el
       * documento lo que NO es texto de la nota -- hoy, solo el filtro del
       * selector de bases.
       *
       * Despacha en el acto (no en un microtask como `dropFilter`): la app lo
       * llama justo ANTES del guardado pendiente, al dejar la nota, y el `.md`
       * que se escribe tiene que salir ya sin el filtro. Ahí no hay ninguna
       * actualización en curso, así que el dispatch es seguro; `close()` sí
       * puede caer dentro de `update()` y de ahí el microtask.
       *
       * Lo tecleado en el menú "/" es texto de la nota y no se toca. En el modo
       * "nombre" el nombre se queda, igual que con /page (decisión de la
       * revisión de P17.3-fix).
       */
      discardTransientText() {
        if (this.mode !== "database" && this.mode !== "page") return false;
        const filtro =
          this.mode === "page"
            ? pageFilterClear(this.view.state.doc, this.from)
            : databaseFilterClear(this.view.state.doc, this.from);
        this.dismiss();
        if (!filtro) return false;
        this.view.dispatch({ changes: { from: filtro.from, to: filtro.to, insert: "" } });
        return true;
      }

      moveSelection(delta) {
        if (this.mode !== "menu" && this.mode !== "page" && this.mode !== "database") return false;
        const n = this.activeOptions.length;
        if (n === 0) return true;
        this.selectedIndex = (this.selectedIndex + delta + n) % n;
        this.notify();
        return true;
      }

      confirmSelection() {
        if (this.mode !== "menu" && this.mode !== "page" && this.mode !== "database") return false;
        const opts = this.activeOptions;
        const from = this.from;
        // En el selector de bases el tramo es todo lo escrito en la línea (ver
        // `filterRange`); en el menú "/" lo escrito es lo que hay a la
        // izquierda del caret, como en cualquier completado.
        const to =
          this.mode === "database" || this.mode === "page"
            ? this.filterRange().to
            : this.view.state.selection.main.head;
        if (opts.length === 0) {
          this.close();
          return true;
        }
        const chosen = opts[Math.min(this.selectedIndex, opts.length - 1)];

        // ---- selector de páginas
        if (this.mode === "page") {
          if (chosen.kind === "link") {
            // Enlazar con una página que ya existe: no se crea nada y no se
            // navega. El tramo (lo tecleado como filtro) pasa a ser `[[Título]]`.
            this.dismiss();
            applyLink(this.view, from, to, chosen.entry.title);
            return true;
          }
          if (chosen.kind === "rename") {
            this.startNaming(from, to, "rename", chosen.entry);
            return true;
          }
          if (chosen.kind === "create") {
            // Lo escrito se queda como nombre: Enter otra vez crea y abre.
            this.startNaming(from, to, "page");
            return true;
          }
          this.close();
          return true;
        }

        // ---- selector de bases
        if (chosen.kind === "item") {
          // `dismiss` y no `close`: el tramo no se borra antes de tiempo, lo
          // sustituye `pickDatabase` por el bloque cuando llega la API.
          this.dismiss();
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
          this.openDatabasePicker(from);
          return true;
        }
        if (chosen.type === "page") {
          // Borra "/page" y abre el selector de páginas: lo que se escriba
          // detrás filtra las que hay, con la opción de enlazar o de crear
          // -- el mismo gesto para las dos cosas. El estado se actualiza
          // ANTES del dispatch a propósito: ese dispatch dispara el propio
          // update() de este plugin de forma síncrona, y si this.mode
          // siguiera siendo "menu" en ese momento, detectaría el "/page" ya
          // borrado como si el menú debiera cerrarse.
          this.openPagePicker(from);
          return true;
        }

        this.close();
        applySlashChoice(this.view, from, to, chosen.type);
        return true;
      }

      /** Abre el selector de bases: borra "/base" y lista las del vault. */
      openDatabasePicker(from) {
        this.mode = "database";
        this.from = from;
        this.selectedIndex = 0;
        // La lista se pide UNA vez, al abrir: filtrar mientras se escribe no
        // puede ir a la API en cada tecla. El dispatch de abajo dispara el
        // update() de este plugin, que ya en modo "database" pinta el selector.
        this.entries = normalizeDatabaseEntries(onListDatabases?.());
        // Lo escrito detrás de "/base" es el filtro: se borra la línea entera
        // -- no solo hasta el caret --, el mismo tramo que sustituirá el bloque
        // al elegir. Con el caret a mitad, lo que quedara detrás se colaría
        // como filtro del selector recién abierto.
        this.view.dispatch({
          changes: { from, to: this.filterRange().to, insert: "" },
          selection: { anchor: from },
        });
      }

      /** Abre el selector de páginas: borra "/page" y lista las del vault. */
      openPagePicker(from) {
        this.mode = "page";
        this.from = from;
        this.selectedIndex = 0;
        this.renameTarget = null;
        // La lista se pide UNA vez, al abrir (igual que en /base): filtrar
        // mientras se escribe no puede recorrer el árbol en cada tecla.
        this.pages = normalizePageEntries(onListPages?.());
        // Lo escrito detrás de "/page" es el filtro: se borra la línea entera
        // -- no solo hasta el caret --, el mismo tramo que sustituirá el
        // enlace al elegir (o el nombre al crear).
        this.view.dispatch({
          changes: { from, to: this.filterRange().to, insert: "" },
          selection: { anchor: from },
        });
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
          const doc = view.state.doc;
          const actual = doc.sliceString(from, Math.min(to, doc.length));
          if (!info || actual !== esperado) {
            view.focus();
            return;
          }
          // El bloque tiene que quedar separado por UNA línea en blanco a cada
          // lado (P17-M1/C2): el texto de alrededor decide cuántos saltos hacen
          // falta delante y cuáles se lleva por delante el cambio detrás. Sin
          // esto, insertar al principio de la línea que sigue a un párrafo
          // dejaba la valla pegada arriba y dos o tres líneas en blanco debajo.
          const { text, cursorOffset, dropAfter } = databaseBlockInsert(info, {
            before: doc.sliceString(0, from),
            after: doc.sliceString(to),
          });
          view.dispatch({
            changes: { from, to: to + dropAfter, insert: text },
            selection: { anchor: from + cursorOffset },
          });
          view.focus();
        });
      }

      /** Pasa del menú al aviso de nombre: página nueva, base nueva o nombre
       * nuevo para una página que se renombra. */
      startNaming(from, to, kind, entry = null) {
        this.mode = "naming";
        this.namingKind = kind;
        this.renameTarget = kind === "rename" ? entry : null;
        this.from = from;
        if (kind === "rename") {
          // El nombre nuevo hay que escribirlo: lo que había era el de la
          // página que se renombra. Fuera el tramo y el cursor ahí.
          this.view.dispatch({ changes: { from, to, insert: "" }, selection: { anchor: from } });
          return;
        }
        // Página nueva y base nueva: lo escrito es el nombre que va a llevar,
        // así que se queda tal cual, con el cursor al final, listo para Enter.
        // (Antes "/page" venía directo del menú y lo escrito detrás era el
        // filtro, no el nombre -- por eso se borraba.)
        this.notify();
      }

      confirmNaming() {
        if (this.mode !== "naming") return false;
        const from = this.from;
        const kind = this.namingKind;
        const target = this.renameTarget;
        // El nombre es todo lo escrito en la línea (el mismo tramo que era el
        // filtro del selector, ver `filterRange`): con el cursor a mitad, lo
        // que quedara detrás no puede colarse como parte del nombre ni
        // quedarse de párrafo suelto en la nota.
        const to = this.filterRange().to;
        this.mode = null;
        this.from = -1;
        this.namingKind = "page";
        this.renameTarget = null;
        this.notify();
        if (kind === "database") createDatabaseFromName(this.view, from, to, onCreateDatabase);
        else if (kind === "rename") {
          renamePageFromName(this.view, from, to, target, onRenamePage, currentPageId);
        } else createPageFromName(this.view, from, to, onCreatePage, onOpenPage);
        return true;
      }

      destroy() {
        if (instancia === this) instancia = null;
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

  return {
    extensions: [plugin, menuKeymap],
    /**
     * Cierra desde fuera el menú abierto y tira lo que escribió en el
     * documento (el filtro de /base). Lo llama la app al dejar la nota,
     * antes de guardarla.
     *
     * No se hace desde `destroy()` del plugin: ahí ya se está desmontando la
     * vista y despachar una transacción no es seguro (y el documento ya no es
     * de nadie). Quien navega sabe cuándo hay que limpiar; el plugin, qué.
     */
    discardTransientText() {
      return instancia ? instancia.discardTransientText() : false;
    },
    /** Tramo del filtro del selector abierto (`{from, to}`), o `null`. Lo usa
     * `getValue` para dejar ese texto fuera de lo que se guarda, sin tocar el
     * documento. */
    transientRange() {
      return instancia ? instancia.transientRange() : null;
    },
  };
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

// Alt+↑ / Alt+↓: mover el bloque del cursor un puesto. Va con el resto de
// nuestros keymaps (antes del general) para que la flecha no se lleve el caret
// a la línea de al lado en vez de reordenar.
const moveBlockKeymap = keymap.of([
  { key: "Alt-ArrowUp", run: (view) => runMoveBlockKey(view, -1) },
  { key: "Alt-ArrowDown", run: (view) => runMoveBlockKey(view, 1) },
]);

// ------------------------------------------------- ⌘B / ⌘I / ⌘U (formato)
//
// Qué cambia en el documento lo decide format.js (puro, con sus tests). Aquí
// solo el pegamento: zonas de código, UNA transacción y dónde queda la
// selección.
//
// Va DESPUÉS de todos los keymaps que ya había ("[[", menú "/", flechas de la
// lista, ⏎ de bloques, Supr/⌫, Alt+↑/↓): ninguno usa Mod, así que no puede
// robarle nada a lo ya probado. Y devuelve siempre true -- también dentro de
// código, donde no hace nada -- para que la tecla NO llegue al navegador: en
// un contenteditable, Ctrl+B es su propio "negrita", que no sabe de Markdown.
const formatKeymap = keymap.of([
  { key: "Mod-b", run: (view) => runFormat(view, "bold") },
  { key: "Mod-i", run: (view) => runFormat(view, "italic") },
  { key: "Mod-u", run: (view) => runFormat(view, "underline") },
]);

/**
 * ⌘/Ctrl + B / I / U: pone o quita negrita, cursiva o subrayado. Todo el
 * cambio va en UNA transacción, así que un solo ⌘Z lo deshace (también
 * cuando la selección son varias líneas y se envuelven una por una).
 */
export function runFormat(view, kind) {
  const { state } = view;
  const ranges = state.selection.ranges.map((r) => ({ from: r.from, to: r.to }));
  const edit = formatEdit({
    text: state.doc.toString(),
    ranges,
    kind,
    codeRanges: codeRangesOf(state, ranges.reduce((end, r) => Math.max(end, r.to), 0)),
  });
  if (!edit) return true; // dentro de código: se consume la tecla y nada más
  const changes = ChangeSet.of(edit.changes, state.doc.length);
  // Si algún rango de la selección se quedó sin tocar (estaba dentro de
  // código), hay menos rangos nuevos que viejos: el principal se acota para
  // que `EditorSelection.create` no se queje.
  const mainIndex = Math.min(state.selection.mainIndex, edit.selection.length - 1);
  view.dispatch({
    changes,
    selection: EditorSelection.create(
      edit.selection.map((s) => EditorSelection.range(s.from, s.to)),
      mainIndex
    ),
    userEvent: "input.format",
    scrollIntoView: true,
  });
  view.focus();
  return true;
}

/** Bloque (del árbol de Markdown) en el que cae `pos`, o null si el cursor no
 * está dentro de ninguno (nota vacía, fila en blanco entre dos bloques). */
function blockAt(state, pos) {
  let found = null;
  for (const b of state.field(blockField).blocks) {
    if (pos >= b.from && pos <= b.to) found = b;
  }
  return found;
}

/**
 * Alt+↑ / Alt+↓: mueve el bloque del cursor (o el marcado con el asa) un
 * puesto arriba/abajo. El cursor se queda DENTRO del bloque movido, a la misma
 * altura: pulsar dos veces seguidas sigue moviendo el mismo bloque.
 *
 * - Con varios bloques marcados no aplica (`moveBlockTo` mueve uno), pero la
 *   tecla se CONSUME igual: si se dejara caer al keymap de CodeMirror, su
 *   Alt+↓ por defecto mueve UNA LÍNEA (`moveLineDown`) y eso puede pegar dos
 *   bloques al revés de lo que se está pidiendo.
 * - Con el cursor en un HUECO (entre dos bloques, nota vacía) no hay bloque que
 *   mover y la tecla también se consume sin tocar el `.md`: `moveLineDown`
 *   allí mueve la línea en blanco y se lleva el bloque de al lado, que es el
 *   mismo destrozo por otra puerta (ver `blockAt`).
 * - En el borde se consume sin tocar nada: no hay bloque al que cambiar.
 * - Si el movimiento no cabe sin cambiar las líneas del `.md` (ver
 *   block-move.js), también se consume: no se mueve, pero la tecla no acaba en
 *   `moveLineDown`, que es lo que fusionaba `> cita uno\n---\n> cita uno`.
 * - Todo el reordenado es UNA transacción (ver `moveBlockTo`), así que un solo
 *   ⌘Z lo deshace.
 */
export function runMoveBlockKey(view, dir) {
  const { state } = view;
  const range = state.selection.main;
  const { blocks, selected } = state.field(blockField);
  let index = -1;
  if (selected.size === 1) {
    index = [...selected][0];
  } else if (selected.size > 1) {
    // Mover es de uno: la tecla no hace nada, pero se queda aquí (ver arriba).
    return true;
  } else {
    const block = blockAt(state, range.head);
    // Sin bloque no hay nada que mover, pero la tecla es nuestra igual: en un
    // hueco (clic en la línea en blanco entre dos bloques, nota vacía) devolver
    // `false` la dejaba en el keymap de CodeMirror, que la usa para
    // `moveLineDown`: mueve UNA LÍNEA y escribe el `.md` sin que nadie haya
    // pedido mover nada -- «parrafo 0\n\n    codigo 1» acababa en
    // «parrafo 0\n    codigo 1\n», con el bloque de código dentro del párrafo
    // (2 bloques -> 1). En un hueco no se mueve ningún bloque: cuál de los dos
    // de al lado sería no lo dice el cursor.
    if (!block) return true;
    index = blocks.indexOf(block);
  }
  // Índice fuera de la lista (marcas viejas tras un cambio): la tecla sigue
  // siendo nuestra.
  if (index < 0 || index >= blocks.length) return true;
  const slot = dir < 0 ? index - 1 : index + 2;
  if (slot < 0 || slot > blocks.length) return true; // ya está en un extremo
  // Se mueva o no (si no cabe, `moveBlockTo` no toca el documento), la tecla es
  // nuestra: que no la recoja el `moveLineDown` de por defecto.
  moveBlockTo(view, index, slot, range.head);
  return true;
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
    const insideCode = isInsideCode(codeRangesOf(state, range.to), range.from);
    const sep = kind === "enter" && !insideCode ? "\n\n" : "\n";
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
  // Dentro de código no salta: "[[i]]" en un array de verdad no es un
  // enlace (mismo criterio que las flechas tipográficas, más arriba), y lo
  // mismo en un `código` en línea.
  if (isInsideCode(codeRangesOf(state, from + LINK_OPEN.length), from)) return null;
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
 *   Crea la página hija de la nota abierta con ese nombre. `null` = no se creó
 *   (la app ya avisó): el editor no toca el documento ni navega.
 * @param {(pageId: string) => Promise<void>|void} [opts.onOpenPage]
 *   Guarda de verdad lo pendiente de la nota actual y navega a la página
 *   recién creada (`/page` la abre: pedido del dueño). El editor espera a que
 *   termine antes de darse por hecho.
 * @param {(pageId: string, title: string) => Promise<{id: string, title: string}|null>} [opts.onRenamePage]
 *   Renombra una página existente (el `.md` y su título) y devuelve cómo ha
 *   quedado. `null` = no se renombró.
 * @param {() => {id: string, title: string, type?: string, parent_id?: string, folder?: string}[]} [opts.onListPages]
 *   Páginas del vault para el selector de "/page".
 * @param {string} [opts.currentPageId]
 *   La nota que se está editando: sirve para no insertar un enlace a sí misma
 *   y para ofrecer renombrar (en vez de crear un duplicado) cuando el nombre
 *   escrito es el suyo o el de una hija suya.
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
    const { lead, end } = outerBlankRange(current);
    if (lead === 0 && end === current.length) return;
    if (cursorInOuterBlanks(current, view.state.selection.main.head)) return;
    // El recorte va por los EXTREMOS, nunca como un reemplazo del documento
    // entero: al mapear la selección a través de un cambio que cubre todo el
    // texto, un cursor dentro del tramo reemplazado cae al principio -- era el
    // «escribo dos letras y la vista salta a la primera línea» que reportó el
    // dueño al empezar a escribir en el bloque nuevo del final.
    const changes = [];
    if (lead > 0) changes.push({ from: 0, to: lead });
    if (end < current.length) changes.push({ from: end, to: current.length });
    view.dispatch({
      changes,
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
  onOpenPage,
  onRenamePage,
  onListPages,
  currentPageId,
  onCreateLink,
  onListDatabases,
  onPickDatabase,
  onCreateDatabase,
  linkTargets,
  renderDatabaseBlock,
}) {
  // El menú "/" y el selector de bases van en una variable y no sueltos en la
  // lista de extensiones: la app tiene que poder cerrarlos desde fuera al
  // dejar la nota (ver `discardTransientText` en el objeto que se devuelve).
  const slashMenu = slashMenuExtension({
    onMenuUpdate: onSlashMenu ?? (() => {}),
    onCreatePage,
    onOpenPage,
    onRenamePage,
    onListPages,
    onListDatabases,
    onPickDatabase,
    onCreateDatabase,
    currentPageId,
  });

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
        ...slashMenu.extensions,
        // El del autocompletado, justo detrás: primero el menú "/" (que solo
        // consume teclas mientras está abierto) y luego la lista de "[[".
        linkKeymap,
        blockKeymap,
        blockDeleteKeymap,
        backspaceJoinKeymap,
        moveBlockKeymap,
        formatKeymap,
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
        dotField,
        blockDotPlugin(),
        blockDotEventHandlers(),
        blockHandleEventHandlers(onBlockContextMenu),
        blockLinesPlugin(),
        blockSpacingPlugin(),
        quotePlugin(),
        blankLinePlugin(),
        syntaxMarkPlugin(),
        underlinePlugin(),
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

  /** Documento tal como se GUARDA: sin el filtro del selector de bases, que
   * se escribe dentro del documento para poder filtrar pero no es texto de la
   * nota.
   *
   * Se recorta aquí, en el texto que sale, y no solo al cerrar el selector
   * (`discardTransientText`) porque hay guardados que no pasan por ese cierre:
   * el autoguardado de 2 s y el de emergencia al cerrar, recargar u ocultar la
   * pestaña (`flushSaveBeacon` en app.js) -- este último sin pasar por
   * `clearMain()`, así que el filtro se quedaba escrito en el `.md`.
   *
   * Y se recorta SIN despachar ni tocar la vista (ver `withoutRange`): en
   * `visibilitychange` la pestaña sigue viva y lo que hay en pantalla -- el
   * selector con su filtro -- no puede cambiar bajo los dedos. */
  function durableDoc() {
    const text = view.state.doc.toString();
    const gaps = view.state.field(pendingEmptyGaps);
    const filtro = slashMenu.transientRange();
    return filtro ? withoutRange(text, gaps, filtro) : { text, gaps };
  }

  return {
    // Lo que se guarda y lo que se exporta: el cuerpo canónico, sin las líneas
    // en blanco de los extremos ni el filtro del selector de bases. Decisión
    // D8 (a): esos bloques vacíos no existen -- el `.md` ya los tiraba al
    // escribir (`canonical_body` en backend/app/vault/store.py), así que el
    // editor no los manda siquiera.
    getValue: () => {
      const { text, gaps } = durableDoc();
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
    /** Vuelve a pintar las bases incrustadas (bloques ```mdm-database).
     *
     * La app lo llama cuando `/events` avisa de que una base de la nota
     * abierta ha cambiado en otra ventana (M2). No es una edición: no toca el
     * documento, no mueve el cursor y no pasa por `onChange`, así que no hay
     * autoguardado. Cada bloque pide otra vez sus datos, y si la base ya no
     * existe el widget pinta su tarjeta de «Base no encontrada» con la valla
     * intacta en el texto.
     */
    refreshDatabaseBlocks() {
      view.dispatch({ effects: refreshDatabaseBlocks.of(null) });
    },
    focus: () => view.focus(),
    /** Cierra el selector de bases si está abierto y borra del documento el
     * filtro que llevara escrito. La app lo llama al dejar la nota, ANTES del
     * guardado pendiente: ese texto no es contenido de la nota y no puede
     * acabar en el `.md`. Devuelve si borró algo. */
    discardTransientText: () => slashMenu.discardTransientText(),
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
