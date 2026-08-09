// Fuente del editor de notas (CodeMirror 6). Se compila a un único archivo
// con `npm run build` (esbuild) -> ../vendor/editor.bundle.js, que es lo
// que carga la app de verdad. Este archivo nunca se sirve tal cual.
//
// Deliberadamente no fija ningún color aquí -- solo asigna clases CSS.
// El aspecto (colores claro/oscuro, tamaños) vive en frontend/index.html
// junto al resto del sistema de diseño, para no duplicar el tema en dos
// sitios.

import { EditorState, EditorSelection, StateField, StateEffect } from "@codemirror/state";
import { EditorView, keymap, Decoration, ViewPlugin, WidgetType } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { HighlightStyle, syntaxHighlighting, syntaxTree } from "@codemirror/language";
import { markdown } from "@codemirror/lang-markdown";
import { tags } from "@lezer/highlight";

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
  // HeaderMark / EmphasisMark / LinkMark / CodeMark / ListMark: el "#", "**", "*", "-"...
  { tag: tags.processingInstruction, class: "cm-mdm-mark" },
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

      if (resolved && !selectionIntersects(view.state, start, end)) {
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
];

const BLOCK_TYPE_LABEL = Object.fromEntries(BLOCK_TYPES.map((t) => [t.type, t.label]));

// Comandos del menú "/" que NO son un tipo de bloque convertible -- no
// deben aparecer en el menú "Convertir a" del clic derecho (convertir un
// párrafo ya escrito "a página" no tiene sentido: crear una página es una
// acción con efecto secundario -- una llamada a la API -- no una
// transformación de texto). Por eso viven en una lista aparte y solo se
// añaden a las opciones del menú "/" (ver slashMenuExtension), nunca a
// BLOCK_TYPES.
const SLASH_EXTRA_COMMANDS = [{ type: "page", label: "Página", abbrev: "page" }];
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
      default:
        // Blockquote, HTMLBlock, Table, LinkReference... siguen siendo
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
    contextmenu(event, view) {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return false;
      const handle = target.closest(".cm-mdm-block-handle");
      if (!handle) return false;
      event.preventDefault();

      const idx = Number(handle.dataset.blockIndex);
      const { blocks, selected } = view.state.field(blockField);
      let indices = selected.has(idx) ? selected : new Set([idx]);
      if (indices !== selected) selectBlocks(view, indices);

      if (onBlockContextMenu) {
        // Si es un único bloque, se puede decir de qué tipo es ya mismo;
        // con varios seleccionados (posiblemente de tipos distintos) no.
        const currentTypeLabel =
          indices.size === 1 ? BLOCK_TYPE_LABEL[blocks[[...indices][0]].type] ?? null : null;

        onBlockContextMenu({
          x: event.clientX,
          y: event.clientY,
          count: indices.size,
          currentTypeLabel,
          types: BLOCK_TYPES,
          convertTo: (type) => convertBlocks(view, indices, type),
          deleteBlocks: () => deleteBlocks(view, indices),
        });
      }
      return true;
    },
  });
}

function selectBlocks(view, indices) {
  const { blocks } = view.state.field(blockField);
  const ordered = [...indices].sort((a, b) => a - b);
  const ranges = ordered.map((i) => EditorSelection.range(blocks[i].from, blocks[i].to));
  view.dispatch({
    effects: setSelectedBlocks.of(indices),
    selection: ranges.length ? EditorSelection.create(ranges, ranges.length - 1) : undefined,
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
    effects: setSelectedBlocks.of(new Set()),
  });
  view.focus();
}

function deleteBlocks(view, indices) {
  const { blocks } = view.state.field(blockField);
  const doc = view.state.doc;
  const changes = [...indices].map((i) => {
    const b = blocks[i];
    let to = b.to;
    if (to < doc.length && doc.sliceString(to, to + 1) === "\n") to += 1;
    return { from: b.from, to, insert: "" };
  });
  view.dispatch({ changes, effects: setSelectedBlocks.of(new Set()) });
  view.focus();
}

function convertBlocks(view, indices, targetType) {
  const { blocks } = view.state.field(blockField);
  const doc = view.state.doc;
  const changes = [...indices].map((i) => {
    const b = blocks[i];
    const raw = doc.sliceString(b.from, b.to);
    const plain = stripBlockPrefix(raw, b.type);
    return { from: b.from, to: b.to, insert: applyBlockType(plain, targetType) };
  });
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

function slashMenuExtension(onMenuUpdate, onCreatePage) {
  const plugin = ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.view = view;
        this.mode = null; // null | "menu" | "naming"
        this.from = -1;
        this.selectedIndex = 0;
      }

      get query() {
        if (this.mode !== "menu") return "";
        const pos = this.view.state.selection.main.head;
        if (pos < this.from) return "";
        return this.view.state.doc.sliceString(this.from + 1, pos);
      }

      get filteredOptions() {
        const all = SLASH_MENU_OPTIONS;
        const q = this.query.trim().toLowerCase();
        if (!q) return all;
        return all.filter((t) => t.abbrev.startsWith(q) || t.label.toLowerCase().includes(q));
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

        if (this.mode === "menu") {
          const sel = update.state.selection;
          const pos = sel.main.head;
          const line = update.state.doc.lineAt(Math.min(this.from, update.state.doc.length));
          const slashStillThere =
            this.from < update.state.doc.length &&
            update.state.doc.sliceString(this.from, this.from + 1) === "/";

          if (!slashStillThere || sel.ranges.length !== 1 || !sel.main.empty || pos < this.from || pos > line.to) {
            this.close();
            return;
          }

          this.selectedIndex = Math.min(this.selectedIndex, Math.max(0, this.filteredOptions.length - 1));
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
          setTimeout(() => {
            if (this.mode !== "naming" || this.from < 0) return; // pudo cerrarse mientras tanto
            const coords = this.view.coordsAtPos(Math.min(from, this.view.state.doc.length));
            onMenuUpdate({ kind: "naming", x: coords?.left ?? 0, y: coords?.bottom ?? 0 });
          });
          return;
        }
        const opts = this.filteredOptions;
        const selectedIndex = Math.min(this.selectedIndex, Math.max(0, opts.length - 1));
        const options = opts.map((o, i) => ({
          ...o,
          onClick: () => {
            this.selectedIndex = i;
            this.confirmSelection();
          },
        }));
        // coordsAtPos() lee el layout del DOM -- CodeMirror no permite
        // hacerlo dentro del propio ciclo de update() ("Reading the editor
        // layout isn't allowed during an update"), así que se difiere fuera
        // de la pila de la transacción actual. setTimeout(…, 0) en vez de
        // requestAnimationFrame: solo necesitamos salir del update() en
        // curso, no esperar a un frame de composición real (que además no
        // se dispara si la pestaña no está pintando).
        setTimeout(() => {
          if (this.mode !== "menu" || this.from < 0) return; // pudo cerrarse mientras tanto
          const coords = this.view.coordsAtPos(this.from);
          onMenuUpdate({ kind: "menu", x: coords?.left ?? 0, y: coords?.bottom ?? 0, selectedIndex, options });
        });
      }

      close() {
        if (!this.mode) return;
        this.mode = null;
        this.from = -1;
        onMenuUpdate(null);
      }

      moveSelection(delta) {
        if (this.mode !== "menu") return false;
        const n = this.filteredOptions.length;
        if (n === 0) return true;
        this.selectedIndex = (this.selectedIndex + delta + n) % n;
        this.notify();
        return true;
      }

      confirmSelection() {
        if (this.mode !== "menu") return false;
        const opts = this.filteredOptions;
        const from = this.from;
        const to = this.view.state.selection.main.head;
        if (opts.length === 0) {
          this.close();
          return true;
        }
        const chosen = opts[Math.min(this.selectedIndex, opts.length - 1)];

        if (chosen.type === "page") {
          // Borra "/consulta" y pasa a modo "naming" -- no se cierra del
          // todo (el usuario sigue escribiendo, ahora el nombre en vez del
          // filtro), así que no se llama a this.close(). El estado se
          // actualiza ANTES del dispatch a propósito: ese dispatch dispara
          // el propio update() de este plugin de forma síncrona, y si
          // this.mode siguiera siendo "menu" en ese momento, detectaría el
          // "/page" ya borrado como si el menú debiera cerrarse.
          this.mode = "naming";
          this.from = from;
          this.view.dispatch({ changes: { from, to, insert: "" }, selection: { anchor: from } });
          return true;
        }

        this.close();
        applySlashChoice(this.view, from, to, chosen.type);
        return true;
      }

      confirmNaming() {
        if (this.mode !== "naming") return false;
        const from = this.from;
        const to = this.view.state.selection.main.head;
        this.mode = null;
        this.from = -1;
        this.notify();
        createPageFromName(this.view, from, to, onCreatePage);
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

const shiftEnterKeymap = keymap.of([
  {
    key: "Shift-Enter",
    run: (view) => {
      view.dispatch(view.state.replaceSelection("\n\n"));
      return true;
    },
  },
]);

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
 */
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
}) {
  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: doc || "",
      extensions: [
        // El menú "/" y Mayús+Enter van ANTES del keymap general: sus
        // combinaciones (Enter/flechas/Escape mientras el menú está activo)
        // deben ganar; cuando no hacen nada (devuelven false) caen al
        // keymap normal sin más.
        ...slashMenuExtension(onSlashMenu ?? (() => {}), onCreatePage),
        shiftEnterKeymap,
        history(),
        keymap.of([...defaultKeymap, ...historyKeymap]),
        markdown(),
        syntaxHighlighting(highlightStyle),
        blockField,
        blockHandlesPlugin(),
        blockHandleEventHandlers(onBlockContextMenu),
        blockLinesPlugin(),
        blankLinePlugin(),
        hrField,
        wikilinkPlugin(resolvePage),
        wikilinkClickHandler(onNavigate),
        wikilinkHoverHandler(onWikilinkHover, onWikilinkHoverEnd),
        arrowPlugin(),
        EditorView.lineWrapping,
        EditorView.updateListener.of((update) => {
          if (update.docChanged) onChange(update.state.doc.toString());
        }),
      ],
    }),
  });

  return {
    getValue: () => view.state.doc.toString(),
    setValue(text) {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text || "" } });
    },
    focus: () => view.focus(),
    destroy: () => view.destroy(),
  };
}
