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
  { tag: tags.strong, class: "cm-mdm-strong" },
  { tag: tags.emphasis, class: "cm-mdm-em" },
  { tag: tags.monospace, class: "cm-mdm-code" },
  // HeaderMark / EmphasisMark / LinkMark / CodeMark / ListMark: el "#", "**", "*", "-"...
  { tag: tags.processingInstruction, class: "cm-mdm-mark" },
]);

const WIKILINK_RE = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g;

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
      const resolved = resolvePage(title);

      // Clase propia (cm-mdm-wikimark, no cm-mdm-mark): el resaltado de
      // Markdown ya intenta leer "[[" como el inicio de un link normal y
      // añade su propia decoración ahí -- si usáramos la misma clase que
      // los marcadores nativos (#, **, *) quedarían dos <span> anidados
      // pisándose. Visualmente es igual (mismo CSS), pero sin el choque.
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
        if (update.docChanged || update.viewportChanged) {
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
      const link = target.closest(".cm-mdm-wikilink");
      if (!link) return false;
      const pageId = link.getAttribute("data-page-id");
      if (!pageId) return false;
      event.preventDefault();
      onNavigate(pageId);
      return true;
    },
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
  { type: "paragraph", label: "Párrafo" },
  { type: "h1", label: "Título 1" },
  { type: "h2", label: "Título 2" },
  { type: "h3", label: "Título 3" },
  { type: "h4", label: "Título 4" },
  { type: "bullet_list_item", label: "Lista" },
  { type: "ordered_list_item", label: "Lista numerada" },
  { type: "code", label: "Código" },
];

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
      case "ATXHeading5":
      case "ATXHeading6":
        blocks.push({ from: node.from, to: node.to, type: "h4" });
        break;
      case "FencedCode":
      case "CodeBlock":
        blocks.push({ from: node.from, to: node.to, type: "code" });
        break;
      case "Paragraph":
        blocks.push({ from: node.from, to: node.to, type: "paragraph" });
        break;
      default:
        // Blockquote, HorizontalRule, HTMLBlock, Table, LinkReference...
        // siguen siendo seleccionables/borrables, solo no convertibles.
        blocks.push({ from: node.from, to: node.to, type: "other" });
    }
  }
  blocks.sort((a, b) => a.from - b.from);
  return blocks;
}

function stripBlockPrefix(text, type) {
  if (type === "h1" || type === "h2" || type === "h3" || type === "h4") return text.replace(/^#{1,6}[ \t]+/, "");
  if (type === "bullet_list_item") return text.replace(/^[-*+][ \t]+/, "");
  if (type === "ordered_list_item") return text.replace(/^\d+[.)][ \t]+/, "");
  if (type === "code") return text.replace(/^```[^\n]*\n?/, "").replace(/\n?```[ \t]*$/, "");
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
    case "bullet_list_item":
      return "- " + plain;
    case "ordered_list_item":
      return "1. " + plain;
    case "code":
      return "```\n" + plain + "\n```";
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
    const el = document.createElement("span");
    el.className = "cm-mdm-block-handle" + (this.isSelected ? " cm-mdm-block-handle-selected" : "");
    el.textContent = "⋮⋮";
    el.dataset.blockIndex = String(this.index);
    el.title = "Clic: seleccionar bloque · Mayús/Ctrl+clic: varios · clic derecho: acciones";
    return el;
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
      if (!(target instanceof HTMLElement)) return false;
      const handle = target.closest(".cm-mdm-block-handle");
      if (!handle) return false;
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
    },
    contextmenu(event, view) {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return false;
      const handle = target.closest(".cm-mdm-block-handle");
      if (!handle) return false;
      event.preventDefault();

      const idx = Number(handle.dataset.blockIndex);
      const { selected } = view.state.field(blockField);
      let indices = selected.has(idx) ? selected : new Set([idx]);
      if (indices !== selected) selectBlocks(view, indices);

      if (onBlockContextMenu) {
        onBlockContextMenu({
          x: event.clientX,
          y: event.clientY,
          count: indices.size,
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

/**
 * @param {Object} opts
 * @param {HTMLElement} opts.parent
 * @param {string} opts.doc
 * @param {(title: string) => ({id: string}|null)} opts.resolvePage
 * @param {(pageId: string) => void} opts.onNavigate
 * @param {(text: string) => void} opts.onChange
 * @param {(payload: object) => void} [opts.onBlockContextMenu]
 */
export function createNoteEditor({ parent, doc, resolvePage, onNavigate, onChange, onBlockContextMenu }) {
  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: doc || "",
      extensions: [
        history(),
        keymap.of([...defaultKeymap, ...historyKeymap]),
        markdown(),
        syntaxHighlighting(highlightStyle),
        blockField,
        blockHandlesPlugin(),
        blockHandleEventHandlers(onBlockContextMenu),
        blockLinesPlugin(),
        wikilinkPlugin(resolvePage),
        wikilinkClickHandler(onNavigate),
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
