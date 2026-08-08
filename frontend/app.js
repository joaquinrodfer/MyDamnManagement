// MyDamnManagement — panel visual. Vanilla JS, sin build, sin dependencias
// propias -- la única excepción es el editor de notas (CodeMirror 6), que
// vendorizamos ya compilado en /vendor/editor.bundle.js (ver
// frontend/editor-src/). Habla directamente con la API del propio origen
// (/pages, /databases, /search, /status).

import { createNoteEditor } from "/vendor/editor.bundle.js";

const state = {
  pagesById: new Map(), // id -> {id,title,type,icon,children} (del árbol; para resolver [[wikilinks]])
  current: null, // {kind: 'note'|'database', id}
  editor: null, // instancia activa del editor de notas (createNoteEditor), o null
};

// ---------------------------------------------------------------- helpers

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error((data && data.detail) || `HTTP ${res.status}`);
  }
  return data;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function typeIcon(type) {
  return type === "database" ? "🗄" : "📄";
}

/** Limpia #main y destruye la instancia del editor si había una nota abierta
 * -- si no, CodeMirror sigue vivo (listeners, memoria) aunque su DOM ya no
 * exista, porque quitarle el nodo padre no lo destruye por sí solo. */
function clearMain() {
  if (state.editor) {
    state.editor.destroy();
    state.editor = null;
  }
  const main = document.getElementById("main");
  main.innerHTML = "";
  return main;
}

function showEmpty() {
  state.current = null;
  clearMain().innerHTML = '<div class="empty-state">Elige una página o crea una nueva.</div>';
}

// ---------------------------------------------------------------- estado

async function pollStatus() {
  try {
    const data = await api("/status");
    setDot("api", data.services?.api?.ok);
    setDot("db", data.services?.db?.ok);
  } catch {
    setDot("api", false);
    setDot("db", false);
  }
}

function setDot(name, ok) {
  const el = document.getElementById(`dot-${name}`);
  el.classList.toggle("ok", ok === true);
  el.classList.toggle("down", ok === false);
}

// ------------------------------------------------------------------ árbol

async function loadTree() {
  const tree = await api("/pages/tree");
  state.pagesById.clear();
  indexTree(tree);

  const notes = tree.filter((n) => n.type !== "database");
  const dbs = tree.filter((n) => n.type === "database");

  const treeContainer = document.getElementById("tree-container");
  const dbContainer = document.getElementById("db-tree-container");
  treeContainer.innerHTML = "";
  dbContainer.innerHTML = "";

  if (notes.length === 0) treeContainer.innerHTML = '<div class="search-empty">Sin páginas aún.</div>';
  notes.forEach((n) => treeContainer.appendChild(renderTreeNode(n)));

  if (dbs.length === 0) dbContainer.innerHTML = '<div class="search-empty">Sin bases de datos.</div>';
  dbs.forEach((n) => dbContainer.appendChild(renderTreeNode(n)));

  if (state.current) markActive(state.current.id);
}

function indexTree(nodes) {
  for (const n of nodes) {
    state.pagesById.set(n.id, n);
    if (n.children?.length) indexTree(n.children);
  }
}

function renderTreeNode(node) {
  const wrap = document.createElement("div");
  wrap.className = "tree-node";

  const row = document.createElement("div");
  row.className = "tree-row";
  // El id de la `page` y el id de la fila `databases` que la describe son
  // dos UUID distintos -- para navegar hace falta el segundo.
  const navId = node.type === "database" ? node.database_id : node.id;
  row.dataset.id = navId;

  const icon = document.createElement("span");
  icon.className = "type-icon";
  icon.textContent = typeIcon(node.type);

  const label = document.createElement("span");
  label.className = "tree-label";
  label.textContent = node.title || "Sin título";

  row.append(icon, label);
  row.addEventListener("click", () => {
    if (node.type === "database") selectDatabase(node.database_id);
    else selectNote(node.id);
  });

  wrap.appendChild(row);

  if (node.children && node.children.length) {
    const childrenWrap = document.createElement("div");
    childrenWrap.className = "tree-children";
    node.children.forEach((c) => childrenWrap.appendChild(renderTreeNode(c)));
    wrap.appendChild(childrenWrap);
  }

  return wrap;
}

function markActive(id) {
  document.querySelectorAll(".tree-row").forEach((el) => {
    el.classList.toggle("active", el.dataset.id === id);
  });
}

// ------------------------------------------------------------------ notas

async function selectNote(id) {
  state.current = { kind: "note", id };
  markActive(id);
  const [page, backlinks] = await Promise.all([api(`/pages/${id}`), api(`/pages/${id}/backlinks`)]);
  renderNote(page, backlinks);
}

function renderNote(page, backlinks) {
  const main = clearMain();

  const titleInput = document.createElement("input");
  titleInput.className = "note-title";
  titleInput.value = page.title;

  const meta = document.createElement("div");
  meta.className = "note-meta";
  meta.textContent = `Actualizado ${new Date(page.updated_at).toLocaleString("es-ES")}`;

  // El cuadro de texto y la vista previa son el mismo sitio: CodeMirror
  // aplica el formato en vivo sobre el propio Markdown (frontend/editor-src),
  // no hay un panel de "preview" aparte que mantener sincronizado.
  const bodyMount = document.createElement("div");
  bodyMount.id = "note-body";

  let dirty = false;

  const actions = document.createElement("div");
  actions.className = "btn-row";
  actions.style.marginTop = "10px";
  actions.style.alignItems = "center";

  const saveBtn = document.createElement("button");
  saveBtn.className = "btn btn-primary";
  saveBtn.textContent = "Guardar";

  const dirtyFlag = document.createElement("span");
  dirtyFlag.className = "note-meta";
  dirtyFlag.textContent = "cambios sin guardar";
  dirtyFlag.hidden = true;

  async function save() {
    saveBtn.disabled = true;
    saveBtn.textContent = "Guardando…";
    try {
      await api(`/pages/${page.id}`, {
        method: "PATCH",
        body: JSON.stringify({ title: titleInput.value, body_markdown: state.editor.getValue() }),
      });
      dirty = false;
      dirtyFlag.hidden = true;
      await loadTree();
      markActive(page.id);
    } catch (e) {
      alert("Error al guardar: " + e.message);
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = "Guardar";
    }
  }
  saveBtn.addEventListener("click", save);

  const deleteBtn = document.createElement("button");
  deleteBtn.className = "btn btn-danger";
  deleteBtn.textContent = "Borrar página";
  deleteBtn.addEventListener("click", async () => {
    if (!confirm(`¿Borrar "${page.title}"? Esto no se puede deshacer.`)) return;
    await api(`/pages/${page.id}`, { method: "DELETE" });
    await loadTree();
    showEmpty();
  });

  actions.append(saveBtn, deleteBtn, dirtyFlag);

  const backHead = document.createElement("div");
  backHead.className = "panel-head";
  backHead.textContent = `Backlinks (${backlinks.length})`;

  const backList = document.createElement("div");
  backList.className = "backlinks-list";
  if (backlinks.length === 0) {
    backList.innerHTML = '<div class="search-empty">Ninguna página enlaza aquí todavía.</div>';
  } else {
    backlinks.forEach((b) => {
      const item = document.createElement("div");
      item.className = "backlink-item";
      item.textContent = b.title;
      item.addEventListener("click", () => selectNote(b.id));
      backList.appendChild(item);
    });
  }

  main.append(titleInput, meta, bodyMount, actions, backHead, backList);

  // Ctrl/Cmd+S guarda sin pasar por el navegador (evita el diálogo de "Guardar página")
  titleInput.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") {
      e.preventDefault();
      save();
    }
  });

  state.editor = createNoteEditor({
    parent: bodyMount,
    doc: page.body_markdown || "",
    resolvePage: (title) => findPageByTitle(title),
    onNavigate: (pageId) => selectNote(pageId),
    onChange: () => {
      dirty = true;
      dirtyFlag.hidden = false;
    },
  });

  bodyMount.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") {
      e.preventDefault();
      save();
    }
  });
}

function findPageByTitle(title) {
  const lower = title.toLowerCase();
  for (const p of state.pagesById.values()) {
    if ((p.title || "").toLowerCase() === lower) return p;
  }
  return null;
}

// -------------------------------------------------------------- databases

async function selectDatabase(id) {
  state.current = { kind: "database", id };
  markActive(id);
  const [database, views] = await Promise.all([api(`/databases/${id}`), api(`/databases/${id}/views`)]);
  await renderDatabase(database, views, views[0]?.id ?? null);
}

async function renderDatabase(database, views, activeViewId) {
  const main = clearMain();

  const header = document.createElement("div");
  header.className = "db-header";

  const title = document.createElement("input");
  title.className = "db-title";
  title.value = database.title;
  title.addEventListener("blur", async () => {
    const newTitle = title.value.trim() || "Sin título";
    if (newTitle === database.title) return;
    try {
      await api(`/databases/${database.id}`, { method: "PATCH", body: JSON.stringify({ title: newTitle }) });
      database.title = newTitle;
      await loadTree();
      markActive(database.id);
    } catch (e) {
      alert("Error al renombrar: " + e.message);
      title.value = database.title;
    }
  });

  const headerActions = document.createElement("div");
  headerActions.className = "btn-row";

  const newViewBtn = document.createElement("button");
  newViewBtn.className = "btn";
  newViewBtn.textContent = "+ Vista";
  newViewBtn.addEventListener("click", () => openViewDialog(database));

  const deleteDbBtn = document.createElement("button");
  deleteDbBtn.className = "btn btn-danger";
  deleteDbBtn.textContent = "Borrar base de datos";
  deleteDbBtn.addEventListener("click", async () => {
    if (!confirm(`¿Borrar "${database.title}" y todas sus filas? Esto no se puede deshacer.`)) return;
    await api(`/databases/${database.id}`, { method: "DELETE" });
    await loadTree();
    showEmpty();
  });

  headerActions.append(newViewBtn, deleteDbBtn);
  header.append(title, headerActions);

  const hint = document.createElement("div");
  hint.className = "db-schema-hint";
  hint.textContent = database.schema_def.map((p) => `${p.name} (${p.type})`).join(" · ") || "Sin propiedades definidas.";

  const tabs = document.createElement("div");
  tabs.className = "view-tabs";

  const defaultTab = document.createElement("button");
  defaultTab.className = "view-tab" + (activeViewId === null ? " active" : "");
  defaultTab.textContent = "Todas (sin vista)";
  defaultTab.addEventListener("click", () => renderDatabase(database, views, null));
  tabs.appendChild(defaultTab);

  views.forEach((v) => {
    const tab = document.createElement("button");
    tab.className = "view-tab" + (v.id === activeViewId ? " active" : "");
    tab.textContent = `${v.name} (${v.type})`;
    tab.addEventListener("click", () => renderDatabase(database, views, v.id));
    tabs.appendChild(tab);
  });

  const content = document.createElement("div");
  main.append(header, hint, tabs, content);

  const activeView = views.find((v) => v.id === activeViewId) || null;
  const rows = await api(`/databases/${database.id}/rows` + (activeView ? `?view=${activeView.id}` : ""));

  if (activeView && activeView.type === "board" && activeView.config?.group_by) {
    content.appendChild(renderBoard(database, rows, activeView));
  } else {
    content.appendChild(renderTable(database, rows));
  }

  content.appendChild(renderAddRowForm(database));
}

function formatPropValue(value, prop) {
  if (value === undefined || value === null || value === "") return "—";
  if (prop.type === "checkbox") return value ? "✓" : "—";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function renderTable(database, rows) {
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";

  const table = document.createElement("table");
  table.className = "rows-table";

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  headRow.innerHTML = "<th>Título</th>" + database.schema_def.map((p) => `<th>${escapeHtml(p.name)}</th>`).join("") + "<th></th>";
  thead.appendChild(headRow);

  const tbody = document.createElement("tbody");
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = database.schema_def.length + 2;
    td.className = "search-empty";
    td.textContent = "Sin filas todavía.";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  rows.forEach((row) => {
    const tr = document.createElement("tr");

    const titleTd = document.createElement("td");
    titleTd.textContent = row.title;
    tr.appendChild(titleTd);

    database.schema_def.forEach((p) => {
      const td = document.createElement("td");
      td.textContent = formatPropValue(row.properties?.[p.key], p);
      tr.appendChild(td);
    });

    const delTd = document.createElement("td");
    const delBtn = document.createElement("button");
    delBtn.className = "row-del";
    delBtn.textContent = "🗑";
    delBtn.title = "Borrar fila";
    delBtn.addEventListener("click", async () => {
      if (!confirm(`¿Borrar "${row.title}"?`)) return;
      await api(`/databases/${database.id}/rows/${row.id}`, { method: "DELETE" });
      selectDatabase(database.id);
    });
    delTd.appendChild(delBtn);
    tr.appendChild(delTd);

    tbody.appendChild(tr);
  });

  table.append(thead, tbody);
  wrap.appendChild(table);
  return wrap;
}

function renderBoard(database, rows, view) {
  const groupKey = view.config.group_by;
  const prop = database.schema_def.find((p) => p.key === groupKey);
  const columns = prop?.options?.length ? prop.options : uniqueValues(rows, groupKey);

  const board = document.createElement("div");
  board.className = "board";

  const grouped = new Map(columns.map((c) => [c, []]));
  const other = [];
  rows.forEach((r) => {
    const v = r.properties?.[groupKey];
    if (grouped.has(v)) grouped.get(v).push(r);
    else other.push(r);
  });

  columns.forEach((col) => board.appendChild(renderBoardColumn(database, col, grouped.get(col) || [])));
  if (other.length) board.appendChild(renderBoardColumn(database, "Sin valor", other));

  return board;
}

function uniqueValues(rows, key) {
  const seen = [];
  rows.forEach((r) => {
    const v = r.properties?.[key];
    if (v !== undefined && v !== null && !seen.includes(v)) seen.push(v);
  });
  return seen;
}

function renderBoardColumn(database, label, rows) {
  const col = document.createElement("div");
  col.className = "board-col";

  const head = document.createElement("div");
  head.className = "board-col-head";
  const labelSpan = document.createElement("span");
  labelSpan.textContent = String(label);
  const countSpan = document.createElement("span");
  countSpan.textContent = String(rows.length);
  head.append(labelSpan, countSpan);
  col.appendChild(head);

  rows.forEach((row) => {
    const card = document.createElement("div");
    card.className = "board-card";

    const t = document.createElement("div");
    t.className = "board-card-title";
    t.textContent = row.title;
    card.appendChild(t);

    Object.entries(row.properties || {}).forEach(([k, v]) => {
      const propDef = database.schema_def.find((p) => p.key === k);
      if (!propDef || v === null || v === "" || v === undefined) return;
      const line = document.createElement("div");
      line.className = "board-card-prop";
      line.textContent = `${propDef.name}: ${formatPropValue(v, propDef)}`;
      card.appendChild(line);
    });

    col.appendChild(card);
  });

  return col;
}

function renderAddRowForm(database) {
  const wrap = document.createElement("div");
  wrap.className = "inline-form";

  const head = document.createElement("div");
  head.className = "panel-head";
  head.style.margin = "0 0 10px";
  head.textContent = "+ Nueva fila";
  wrap.appendChild(head);

  const titleField = document.createElement("div");
  titleField.className = "field";
  const titleLabel = document.createElement("label");
  titleLabel.textContent = "Título";
  const titleInput = document.createElement("input");
  titleInput.type = "text";
  titleField.append(titleLabel, titleInput);
  wrap.appendChild(titleField);

  const propInputs = {};
  database.schema_def.forEach((p) => {
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.textContent = `${p.name} (${p.type})`;
    field.appendChild(label);

    let input;
    if (p.type === "select") {
      input = document.createElement("select");
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "—";
      input.appendChild(blank);
      (p.options || []).forEach((o) => {
        const opt = document.createElement("option");
        opt.value = o;
        opt.textContent = o;
        input.appendChild(opt);
      });
    } else if (p.type === "checkbox") {
      input = document.createElement("input");
      input.type = "checkbox";
    } else if (p.type === "number") {
      input = document.createElement("input");
      input.type = "number";
      input.step = "any";
    } else if (p.type === "date") {
      input = document.createElement("input");
      input.type = "date";
    } else {
      input = document.createElement("input");
      input.type = "text";
      if (p.type === "multiselect") input.placeholder = "valores separados por coma";
    }
    field.appendChild(input);
    wrap.appendChild(field);
    propInputs[p.key] = { input, type: p.type };
  });

  const submitBtn = document.createElement("button");
  submitBtn.className = "btn btn-primary";
  submitBtn.textContent = "Añadir fila";
  submitBtn.addEventListener("click", async () => {
    const properties = {};
    for (const [key, { input, type }] of Object.entries(propInputs)) {
      if (type === "checkbox") {
        properties[key] = input.checked;
      } else if (type === "multiselect") {
        const raw = input.value.trim();
        if (raw) properties[key] = raw.split(",").map((s) => s.trim()).filter(Boolean);
      } else if (input.value !== "") {
        properties[key] = type === "number" ? Number(input.value) : input.value;
      }
    }
    try {
      await api(`/databases/${database.id}/rows`, {
        method: "POST",
        body: JSON.stringify({ title: titleInput.value || "Sin título", properties }),
      });
      selectDatabase(database.id);
    } catch (e) {
      alert("Error al crear la fila: " + e.message);
    }
  });
  wrap.appendChild(submitBtn);

  return wrap;
}

// -------------------------------------------------- diálogo: nueva database

const dbDialog = document.getElementById("dialog-database");
const propRowsContainer = document.getElementById("prop-rows");

function addPropertyRow() {
  const wrap = document.createElement("div");
  wrap.className = "prop-row-wrap";

  const row = document.createElement("div");
  row.className = "prop-row";

  const keyInput = document.createElement("input");
  keyInput.placeholder = "clave (snake_case)";
  keyInput.className = "prop-key";

  const nameInput = document.createElement("input");
  nameInput.placeholder = "Nombre visible";
  nameInput.className = "prop-name";

  const typeSelect = document.createElement("select");
  typeSelect.className = "prop-type";
  ["text", "number", "select", "multiselect", "date", "checkbox", "url", "relation"].forEach((t) => {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    typeSelect.appendChild(opt);
  });

  const removeBtn = document.createElement("button");
  removeBtn.type = "button";
  removeBtn.textContent = "×";
  removeBtn.className = "icon-btn";
  removeBtn.addEventListener("click", () => wrap.remove());

  row.append(keyInput, nameInput, typeSelect, removeBtn);

  const optionsInput = document.createElement("input");
  optionsInput.placeholder = "opciones separadas por coma (select / multiselect)";
  optionsInput.className = "prop-options-input";
  optionsInput.hidden = true;

  typeSelect.addEventListener("change", () => {
    optionsInput.hidden = !["select", "multiselect"].includes(typeSelect.value);
  });

  wrap.append(row, optionsInput);
  propRowsContainer.appendChild(wrap);
}

document.getElementById("btn-new-database").addEventListener("click", () => {
  document.getElementById("db-name").value = "";
  propRowsContainer.innerHTML = "";
  addPropertyRow();
  dbDialog.showModal();
});
document.getElementById("btn-add-prop").addEventListener("click", addPropertyRow);
document.getElementById("btn-cancel-database").addEventListener("click", () => dbDialog.close());

// -------------------------------------------------- plantillas (Fase 3)
// Un clic: crea la database ya con schema_def + vistas por defecto.
// No hay diálogo — si el nombre no convence, el título es editable luego.

document.getElementById("btn-template-crm").addEventListener("click", () => createFromTemplate("crm"));
document.getElementById("btn-template-tasks").addEventListener("click", () => createFromTemplate("tasks"));

async function createFromTemplate(templateKey) {
  try {
    const database = await api("/databases/from-template", { method: "POST", body: JSON.stringify({ template: templateKey }) });
    await loadTree();
    await selectDatabase(database.id);
  } catch (e) {
    alert("Error al crear desde plantilla: " + e.message);
  }
}

document.getElementById("btn-create-database").addEventListener("click", async () => {
  const title = document.getElementById("db-name").value.trim() || "Sin título";
  const schema_def = [];
  propRowsContainer.querySelectorAll(".prop-row-wrap").forEach((wrap) => {
    const key = wrap.querySelector(".prop-key").value.trim();
    const name = wrap.querySelector(".prop-name").value.trim();
    const type = wrap.querySelector(".prop-type").value;
    const optionsRaw = wrap.querySelector(".prop-options-input").value.trim();
    if (!key || !name) return;
    const prop = { key, name, type };
    if (["select", "multiselect"].includes(type) && optionsRaw) {
      prop.options = optionsRaw.split(",").map((s) => s.trim()).filter(Boolean);
    }
    schema_def.push(prop);
  });

  try {
    const database = await api("/databases", { method: "POST", body: JSON.stringify({ title, schema_def }) });
    dbDialog.close();
    await loadTree();
    await selectDatabase(database.id);
  } catch (e) {
    alert("Error al crear la base de datos: " + e.message);
  }
});

// ------------------------------------------------------- diálogo: nueva vista

const viewDialog = document.getElementById("dialog-view");
let viewDialogDatabase = null;

function openViewDialog(database) {
  viewDialogDatabase = database;
  document.getElementById("view-name").value = "";
  document.getElementById("view-type").value = "table";
  const groupSelect = document.getElementById("view-groupby");
  groupSelect.innerHTML = "";
  database.schema_def.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p.key;
    opt.textContent = p.name;
    groupSelect.appendChild(opt);
  });
  document.getElementById("view-groupby-field").hidden = true;
  viewDialog.showModal();
}

document.getElementById("view-type").addEventListener("change", (e) => {
  document.getElementById("view-groupby-field").hidden = e.target.value !== "board";
});
document.getElementById("btn-cancel-view").addEventListener("click", () => viewDialog.close());

document.getElementById("btn-create-view").addEventListener("click", async () => {
  const name = document.getElementById("view-name").value.trim() || "Vista";
  const type = document.getElementById("view-type").value;
  const config = {};
  if (type === "board") {
    const groupBy = document.getElementById("view-groupby").value;
    if (groupBy) config.group_by = groupBy;
  }
  try {
    await api(`/databases/${viewDialogDatabase.id}/views`, { method: "POST", body: JSON.stringify({ name, type, config }) });
    viewDialog.close();
    await selectDatabase(viewDialogDatabase.id);
  } catch (e) {
    alert("Error al crear la vista: " + e.message);
  }
});

// ---------------------------------------------------------------- nueva nota

document.getElementById("btn-new-note").addEventListener("click", async () => {
  const parent_id = state.current?.kind === "note" ? state.current.id : null;
  const page = await api("/pages", { method: "POST", body: JSON.stringify({ title: "Sin título", parent_id, body_markdown: "" }) });
  await loadTree();
  await selectNote(page.id);
  const titleEl = document.querySelector(".note-title");
  titleEl?.focus();
  titleEl?.select();
});

// -------------------------------------------------------------------- buscar

const searchInput = document.getElementById("search-input");
const searchResults = document.getElementById("search-results");
const navSections = document.getElementById("nav-sections");
let searchDebounce;

searchInput.addEventListener("input", () => {
  clearTimeout(searchDebounce);
  const q = searchInput.value.trim();
  if (!q) {
    searchResults.hidden = true;
    navSections.hidden = false;
    return;
  }
  searchDebounce = setTimeout(() => runSearch(q), 250);
});

async function runSearch(q) {
  const results = await api(`/search?q=${encodeURIComponent(q)}`);
  navSections.hidden = true;
  searchResults.hidden = false;
  searchResults.innerHTML = "";
  if (results.length === 0) {
    searchResults.innerHTML = '<div class="search-empty">Sin resultados.</div>';
    return;
  }
  results.forEach((r) => {
    const el = document.createElement("div");
    el.className = "search-hit";
    el.textContent = `${typeIcon(r.type)} ${r.title}`;
    el.addEventListener("click", () => {
      searchInput.value = "";
      searchResults.hidden = true;
      navSections.hidden = false;
      if (r.type === "database") {
        // /search devuelve el id de página (mismo motivo que en el árbol);
        // resolvemos el id real de la database contra el árbol ya cargado.
        const node = state.pagesById.get(r.id);
        selectDatabase(node?.database_id || r.id);
      } else {
        selectNote(r.id);
      }
    });
    searchResults.appendChild(el);
  });
}

// -------------------------------------------------------------------- arranque

loadTree();
pollStatus();
setInterval(pollStatus, 4000);
