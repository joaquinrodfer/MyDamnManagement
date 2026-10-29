// MyDamnManagement — panel visual. Vanilla JS, sin build, sin dependencias
// propias -- la única excepción es el editor de notas (CodeMirror 6), que
// vendorizamos ya compilado en /vendor/editor.bundle.js (ver
// frontend/editor-src/). Habla directamente con la API del propio origen
// (/pages, /databases, /search, /status).

import { createNoteEditor } from "/vendor/editor.bundle.js";

const state = {
  pagesById: new Map(), // id -> {id,title,type,icon,parent_id,children} (del árbol; para resolver [[wikilinks]])
  current: null, // {kind: 'note'|'database', id}
  editor: null, // instancia activa del editor de notas (createNoteEditor), o null
  flushPendingSave: null, // fuerza el autoguardado en curso antes de navegar a otra página
  flushSaveBeacon: null, // guarda ya (con keepalive) la nota abierta, al cerrar u ocultar la pestaña
  selectedPages: new Map(), // id de página (nodo del árbol) -> node; selección múltiple en la barra lateral
  lastClickedRowId: null, // para el rango de Mayús+clic
  history: [], // [{kind, id}, ...] -- pila de navegación para los botones atrás/adelante
  historyPos: -1, // índice de "dónde estamos" dentro de state.history
};

// ---------------------------------------------------------------- helpers

async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
  } catch (e) {
    // `fetch` solo lanza por red/DNS/servidor inalcanzable -- un 4xx/5xx llega
    // como respuesta y se trata más abajo. Se marca el error para que el aviso
    // sepa que es de red y no lo confunda con cualquier otro TypeError.
    const err = new Error((e && e.message) || "Failed to fetch");
    err.network = true;
    throw err;
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error((data && data.detail) || `HTTP ${res.status}`);
  }
  return data;
}

// ------------------------------------------------- avisos de error (toasts)
// Un único sitio donde decirle al usuario que algo ha fallado -- antes no
// había ninguno y un 500 se quedaba en una pantalla en blanco sin
// explicación, o en el console.error que solo ve quien abre las herramientas
// de desarrollo.

/** Mensaje presentable a partir de lo que puede lanzar api(): el `detail`
 * del backend en un error HTTP, o -- camino distinto -- un fallo de red, que
 * `api()` marca con `.network` y que si no se traduce aquí saldría como el
 * texto crudo en inglés del navegador ("Failed to fetch", "NetworkError",
 * "Load failed"). */
function describeError(e) {
  const raw = (e && e.message) || "";
  if (e?.network || /failed to fetch|networkerror|load failed|network error/i.test(raw)) {
    return "Sin conexión con la API";
  }
  // Cualquier otro error (un fallo real del código incluido) se enseña tal
  // cual: sigue yendo a console.error vía reportError(), no se disfraza.
  return raw || "Error inesperado";
}

const ERROR_TOAST_MS = 6000;
// Tope de avisos a la vez: en el móvil ocupan el ancho completo y tres ya
// tapan bastante la parte baja del editor.
const MAX_ERROR_TOASTS = 3;

/** Aviso discreto abajo a la derecha, descartable con su aspa, que se apila
 * si llegan varios y se va solo a los ~6s. El nodo se crea desde aquí (el
 * HTML no lleva nada) y cuelga de body, no de #main, para seguir visible
 * mientras se navega. */
function showError(message) {
  let stack = document.getElementById("error-toasts");
  if (!stack) {
    stack = document.createElement("div");
    stack.id = "error-toasts";
    stack.className = "error-toasts";
    document.body.appendChild(stack);
  }

  const messageText = String(message);

  // Un aviso repetido no se apila otra vez: sin esto, seis fallos seguidos
  // dejaban seis copias idénticas del mismo mensaje en pantalla.
  const already = Array.from(stack.children).find(
    (t) => t.querySelector(".error-toast-text")?.textContent === messageText,
  );
  if (already) return already;

  // Y nunca más de MAX_ERROR_TOASTS a la vez: con el cupo lleno se retira el
  // más viejo, que es el que menos contexto aporta ya.
  while (stack.childElementCount >= MAX_ERROR_TOASTS) {
    const oldest = stack.firstElementChild;
    if (oldest.retireToast) oldest.retireToast();
    else oldest.remove();
  }

  const toast = document.createElement("div");
  toast.className = "error-toast";
  toast.setAttribute("role", "status"); // los lectores de pantalla lo anuncian sin robar el foco

  const text = document.createElement("span");
  text.className = "error-toast-text";
  // textContent y nunca innerHTML: el mensaje puede venir del backend y un
  // detail con HTML sería una inyección -- aquí solo se pinta texto.
  text.textContent = messageText;

  const close = document.createElement("button");
  close.type = "button";
  close.className = "error-toast-close";
  close.title = "Descartar aviso";
  close.setAttribute("aria-label", "Descartar aviso");
  close.textContent = "×";

  let timer = setTimeout(dismiss, ERROR_TOAST_MS);
  function dismiss() {
    clearTimeout(timer);
    toast.remove();
    // Sin toasts no hay nada que apilar: se va también el contenedor, para no
    // ir dejando nodos vacíos colgando de body.
    if (stack.childElementCount === 0) stack.remove();
  }
  // Retirar sin tocar el contenedor: lo usa el tope de apilado, que va a
  // añadir otro aviso justo después (dismiss() borraría el contenedor vacío
  // y el aviso nuevo se quedaría colgando de un nodo suelto).
  toast.retireToast = () => {
    clearTimeout(timer);
    toast.remove();
  };
  close.addEventListener("click", dismiss);

  toast.append(text, close);
  stack.appendChild(toast);
  return toast;
}

/** Deja el detalle técnico en la consola (para depurar) y avisa al usuario
 * con una frase corta. Un solo camino: no hay cuatro copias del aviso. */
function reportError(context, e) {
  console.error(`${context}:`, e);
  showError(`${context}. ${describeError(e)}`);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function typeIcon(type) {
  return type === "database" ? "🗄" : "📄";
}

/** Limpia #main: fuerza cualquier autoguardado pendiente (para no perder los
 * últimos <2s de cambios al navegar), y destruye la instancia del editor si
 * había una nota abierta -- si no, CodeMirror sigue vivo (listeners,
 * memoria) aunque su DOM ya no exista, porque quitarle el nodo padre no lo
 * destruye por sí solo. */
function clearMain() {
  if (state.flushPendingSave) {
    state.flushPendingSave();
    state.flushPendingSave = null;
  }
  // El guardado de emergencia es de la nota que se está dejando atrás: en
  // cuanto se navega, ya no queda nada suyo que pueda estar pendiente.
  state.flushSaveBeacon = null;
  if (state.editor) {
    state.editor.destroy();
    state.editor = null;
  }
  // La tarjeta de vista previa al hover (si había una abierta) cuelga de
  // document.body, no de #main -- sin esto, navegar mientras estaba visible
  // la dejaba pegada en pantalla apuntando a la página anterior.
  stopLinkPreview();
  closeSlashMenu();
  closeOutline();
  const main = document.getElementById("main");
  main.innerHTML = "";
  return main;
}

async function showEmpty() {
  state.current = null;
  const main = clearMain();
  updateNavButtons();
  await renderDashboard(main);
}

// ------------------------------------------------------------- landing/dashboard
// Lo que se ve cuando no hay ninguna página abierta (al arrancar, o tras
// borrar la que estaba abierta): páginas con cambios recientes, y filas de
// cualquier database con una fecha cercana a hoy (si las hay). Una sola
// llamada -- el cálculo (qué cuenta como "cercana", cruzar schema_def con
// las filas de cada database) vive en el backend, ver GET /dashboard.

function formatRelativeTime(isoString) {
  const diffMs = Date.now() - new Date(isoString).getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return "ahora mismo";
  if (mins < 60) return `hace ${mins} min`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `hace ${hours} h`;
  const days = Math.round(hours / 24);
  if (days < 30) return `hace ${days} d`;
  const months = Math.round(days / 30);
  return `hace ${months} mes${months > 1 ? "es" : ""}`;
}

function formatDueLabel(days) {
  if (days === 0) return "hoy";
  if (days === 1) return "mañana";
  if (days === -1) return "ayer";
  if (days > 0) return `en ${days} días`;
  return `hace ${Math.abs(days)} días`;
}

function dashboardItem({ icon, title, sub, meta, metaClass, onClick }) {
  const item = document.createElement("div");
  item.className = "dashboard-item";

  const iconEl = document.createElement("span");
  iconEl.className = "dashboard-item-icon";
  iconEl.textContent = icon;

  const titleEl = document.createElement("span");
  titleEl.className = "dashboard-item-title";
  titleEl.textContent = title;
  if (sub) {
    const subEl = document.createElement("span");
    subEl.className = "dashboard-item-sub";
    subEl.textContent = ` · ${sub}`;
    titleEl.appendChild(subEl);
  }

  const metaEl = document.createElement("span");
  metaEl.className = "dashboard-item-meta" + (metaClass ? ` ${metaClass}` : "");
  metaEl.textContent = meta;

  item.append(iconEl, titleEl, metaEl);
  item.addEventListener("click", onClick);
  return item;
}

async function renderDashboard(main) {
  let data;
  try {
    data = await api("/dashboard");
  } catch (e) {
    main.innerHTML = '<div class="empty-state">Elige una página o crea una nueva.</div>';
    return;
  }
  // Si mientras cargaba el usuario ya navegó a otra página, no pisarla.
  if (state.current) return;

  main.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "dashboard";

  const recentSection = document.createElement("div");
  recentSection.className = "dashboard-section";
  const recentHead = document.createElement("div");
  recentHead.className = "dashboard-section-head";
  recentHead.textContent = "Páginas recientes";
  recentSection.appendChild(recentHead);

  if (data.recent_pages.length === 0) {
    recentSection.innerHTML += '<div class="search-empty">Elige una página o crea una nueva.</div>';
  } else {
    data.recent_pages.forEach((p) => {
      recentSection.appendChild(
        dashboardItem({
          icon: p.icon || typeIcon(p.type),
          title: p.title || "Sin título",
          meta: formatRelativeTime(p.updated_at),
          onClick: () => (p.type === "database" ? goTo("database", p.database_id) : goTo("note", p.id)),
        })
      );
    });
  }
  wrap.appendChild(recentSection);

  if (data.upcoming_dates.length > 0) {
    const dueSection = document.createElement("div");
    dueSection.className = "dashboard-section";
    const dueHead = document.createElement("div");
    dueHead.className = "dashboard-section-head";
    dueHead.textContent = "Fechas cercanas";
    dueSection.appendChild(dueHead);

    data.upcoming_dates.forEach((u) => {
      dueSection.appendChild(
        dashboardItem({
          icon: "📅",
          title: u.row_title || "Sin título",
          sub: `${u.database_title} · ${u.property_name}`,
          meta: formatDueLabel(u.days_from_today),
          metaClass: u.days_from_today < 0 ? "overdue" : "",
          onClick: () => goTo("database", u.database_id),
        })
      );
    });
    wrap.appendChild(dueSection);
  }

  main.appendChild(wrap);
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
  try {
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
    applyPageSelectionClasses();
    updateNavButtons(); // "subir un nivel" depende de parent_id, que solo se sabe tras recargar el árbol
  } catch (e) {
    // El árbol anterior (si lo había) se queda tal cual: solo se vacía
    // después de que la petición haya ido bien.
    reportError("No se pudo cargar el árbol de páginas", e);
  }
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
  // dos UUID distintos -- para navegar hace falta el segundo. data-node-id
  // es siempre el id de página de verdad, el que se usa para selección
  // múltiple y borrado (con databases, DELETE va por database_id, no por
  // este -- ver bulkDeletePages).
  const navId = node.type === "database" ? node.database_id : node.id;
  row.dataset.id = navId;
  row.dataset.nodeId = node.id;

  const icon = document.createElement("span");
  icon.className = "type-icon";
  icon.textContent = node.icon || typeIcon(node.type);

  const label = document.createElement("span");
  label.className = "tree-label";
  label.textContent = node.title || "Sin título";

  row.append(icon, label);

  row.addEventListener("click", (e) => {
    if (e.shiftKey) {
      e.preventDefault();
      selectPageRangeTo(node);
      return;
    }
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      togglePageSelection(node);
      state.lastClickedRowId = node.id;
      return;
    }
    clearPageSelection();
    state.lastClickedRowId = node.id;
    if (node.type === "database") goTo("database", node.database_id);
    else goTo("note", node.id);
  });

  row.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    let items;
    if (state.selectedPages.has(node.id)) {
      items = [...state.selectedPages.values()];
    } else {
      items = [node];
      state.selectedPages = new Map([[node.id, node]]);
      applyPageSelectionClasses();
    }
    const sections = [{ label: `${items.length} página${items.length > 1 ? "s" : ""}`, items: [] }];
    // "Nueva subpágina" solo tiene sentido con un único destino claro -- con
    // varias páginas seleccionadas a la vez sería ambiguo bajo cuál crearla.
    if (items.length === 1) {
      sections.push({
        items: [{ label: "Nueva subpágina", onClick: () => createSubpage(items[0]) }],
      });
    }
    sections.push({
      items: [
        {
          label: `Eliminar ${items.length > 1 ? "páginas" : "página"}`,
          danger: true,
          onClick: () => bulkDeletePages(items),
        },
      ],
    });
    showContextMenu(e.clientX, e.clientY, sections);
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

// -------------------------------------------------- selección múltiple de páginas

function togglePageSelection(node) {
  if (state.selectedPages.has(node.id)) state.selectedPages.delete(node.id);
  else state.selectedPages.set(node.id, node);
  applyPageSelectionClasses();
}

function clearPageSelection() {
  if (state.selectedPages.size === 0) return;
  state.selectedPages.clear();
  applyPageSelectionClasses();
}

function applyPageSelectionClasses() {
  document.querySelectorAll(".tree-row").forEach((el) => {
    el.classList.toggle("tree-row-selected", state.selectedPages.has(el.dataset.nodeId));
  });
}

function selectPageRangeTo(node) {
  const rows = [...document.querySelectorAll(".tree-row")];
  const ids = rows.map((r) => r.dataset.nodeId);
  const lastIdx = state.lastClickedRowId ? ids.indexOf(state.lastClickedRowId) : -1;
  const curIdx = ids.indexOf(node.id);

  if (lastIdx === -1 || curIdx === -1) {
    togglePageSelection(node);
    return;
  }
  const [lo, hi] = lastIdx < curIdx ? [lastIdx, curIdx] : [curIdx, lastIdx];
  for (let i = lo; i <= hi; i++) {
    const n = state.pagesById.get(ids[i]);
    if (n) state.selectedPages.set(ids[i], n);
  }
  applyPageSelectionClasses();
}

async function bulkDeletePages(items) {
  const label = items.length > 1 ? `estas ${items.length} páginas` : `"${items[0].title}"`;
  if (!confirm(`¿Eliminar ${label}? Esto no se puede deshacer.`)) return;

  const currentId = state.current?.id;
  let currentWasDeleted = false;

  for (const item of items) {
    try {
      if (item.type === "database") {
        await api(`/databases/${item.database_id}`, { method: "DELETE" });
        if (item.database_id === currentId) currentWasDeleted = true;
      } else {
        await api(`/pages/${item.id}`, { method: "DELETE" });
        if (item.id === currentId) currentWasDeleted = true;
      }
    } catch (e) {
      reportError(`No se pudo borrar "${item.title}"`, e);
    }
  }

  clearPageSelection();
  await loadTree();
  if (currentWasDeleted) showEmpty();
}

// -------------------------------------------------------------- selector de emojis
// Sin dependencias: no existe un <input type="emoji">, así que es un panel
// propio con búsqueda por palabra clave sobre una lista curada (no las ~3700
// del estándar Unicode completo -- las de uso habitual son de sobra aquí).

const EMOJI_DATA = [
  ["😀", "cara sonrisa feliz happy"], ["😄", "cara sonrisa feliz happy"], ["😁", "cara sonrisa feliz"],
  ["😂", "risa lagrimas llorar"], ["🙂", "sonrisa leve"], ["😉", "guiño wink"], ["😊", "sonrisa timida"],
  ["😍", "corazones amor love"], ["🤩", "estrellas asombro"], ["😘", "beso kiss"], ["😎", "gafas cool guay"],
  ["🤔", "pensando duda"], ["🤨", "ceja duda sospecha"], ["😐", "neutral serio"], ["😴", "dormir sueño"],
  ["😭", "llorar triste"], ["😡", "enfado rabia"], ["🥳", "fiesta celebracion"], ["🤯", "explosion mente"],
  ["😱", "susto grito"], ["🙃", "boca abajo"], ["😇", "angel santo"], ["🤗", "abrazo hug"], ["🥺", "suplica"],
  ["👍", "bien like ok"], ["👎", "mal dislike"], ["👏", "aplauso bravo"], ["🙌", "manos arriba celebrar"],
  ["🙏", "gracias por favor rezar"], ["💪", "fuerza musculo"], ["✌️", "paz victoria"], ["🤝", "trato acuerdo"],
  ["👋", "hola saludo adios"], ["✍️", "escribir mano"], ["👀", "ojos mirar"], ["🧠", "cerebro mente idea"],
  ["❤️", "corazon amor"], ["🔥", "fuego caliente tendencia"], ["✨", "brillo destello magia"],
  ["⭐", "estrella favorito"], ["🌟", "estrella brillante"], ["💡", "idea bombilla"], ["💯", "cien perfecto"],
  ["✅", "hecho correcto check"], ["❌", "no incorrecto x"], ["⚠️", "aviso alerta cuidado"],
  ["❓", "pregunta duda"], ["❗", "importante exclamacion"], ["🔴", "rojo circulo"], ["🟢", "verde circulo"],
  ["🟡", "amarillo circulo"], ["🔵", "azul circulo"], ["⚪", "blanco circulo"], ["⚫", "negro circulo"],
  ["📄", "pagina documento nota"], ["📝", "nota apuntes escribir"], ["📋", "portapapeles lista tareas"],
  ["📌", "chincheta fijado importante"], ["📍", "ubicacion pin"], ["📎", "clip adjunto"], ["🔖", "marcador"],
  ["📁", "carpeta folder"], ["📂", "carpeta abierta"], ["🗂️", "archivador organizador"], ["🗃️", "archivo caja"],
  ["📆", "calendario fecha"], ["📅", "calendario evento"], ["⏰", "alarma reloj"], ["⏳", "tiempo espera"],
  ["⌛", "arena tiempo"], ["🕐", "reloj hora"], ["📈", "grafico subida progreso"], ["📉", "grafico bajada"],
  ["📊", "grafico barras estadisticas"], ["💰", "dinero saco"], ["💵", "dinero billete"], ["💳", "tarjeta pago"],
  ["🧾", "recibo factura"], ["🏦", "banco"], ["💼", "maletin trabajo negocio"], ["📦", "caja paquete envio"],
  ["🚀", "cohete lanzamiento rapido"], ["🎯", "objetivo meta diana"], ["🏆", "trofeo logro"], ["🥇", "medalla oro"],
  ["🎓", "graduacion estudios"], ["🏫", "escuela universidad"], ["📚", "libros lectura estudio"],
  ["📖", "libro abierto leer"], ["✏️", "lapiz escribir"], ["🖊️", "boligrafo"], ["🖌️", "pincel arte"],
  ["🎨", "arte pintura diseño"], ["🔬", "microscopio ciencia"], ["🔭", "telescopio astronomia"],
  ["🧪", "tubo ensayo experimento"], ["🧬", "adn genetica"], ["⚙️", "engranaje ajustes configuracion"],
  ["🛠️", "herramientas reparar"], ["🔧", "llave inglesa"], ["🔨", "martillo"], ["🪛", "destornillador"],
  ["💻", "portatil ordenador"], ["🖥️", "ordenador escritorio"], ["⌨️", "teclado"], ["🖱️", "raton mouse"],
  ["📱", "movil telefono"], ["☎️", "telefono llamada"], ["🔌", "enchufe electricidad"], ["🔋", "bateria"],
  ["💾", "guardar disquete"], ["💿", "cd disco"], ["🌐", "internet web mundo"], ["🔗", "enlace link"],
  ["🔒", "candado bloqueado seguro"], ["🔓", "candado abierto"], ["🔑", "llave acceso"], ["🛡️", "escudo proteccion"],
  ["🐛", "bicho bug error"], ["🕷️", "araña"], ["🐍", "serpiente python"], ["🐙", "pulpo"], ["🦄", "unicornio"],
  ["🐱", "gato cat"], ["🐶", "perro dog"], ["🦊", "zorro"], ["🐼", "panda"], ["🐨", "koala"], ["🐸", "rana"],
  ["🦁", "leon"], ["🐯", "tigre"], ["🐮", "vaca"], ["🐷", "cerdo"], ["🐵", "mono"], ["🦉", "buho"], ["🐧", "pingüino"],
  ["☕", "cafe"], ["🍵", "te"], ["🍺", "cerveza"], ["🍷", "vino"], ["🍕", "pizza"], ["🍔", "hamburguesa"],
  ["🍎", "manzana fruta"], ["🍌", "platano"], ["🥑", "aguacate"], ["🍰", "tarta pastel"], ["🍪", "galleta"],
  ["🌍", "mundo tierra global"], ["🗺️", "mapa"], ["🧭", "brujula direccion"], ["✈️", "avion viaje"],
  ["🚗", "coche"], ["🚲", "bicicleta"], ["🏠", "casa hogar"], ["🏢", "edificio oficina"], ["🏥", "hospital"],
  ["⛰️", "montaña"], ["🏖️", "playa"], ["🌳", "arbol naturaleza"], ["🌱", "planta semilla crecer"],
  ["☀️", "sol dia"], ["🌙", "luna noche"], ["☁️", "nube"], ["🌧️", "lluvia"], ["⛈️", "tormenta"], ["❄️", "nieve frio"],
  ["🎉", "confeti fiesta celebracion"], ["🎂", "cumpleaños tarta"], ["🎁", "regalo"], ["🎮", "videojuego mando"],
  ["🎵", "musica nota"], ["🎧", "auriculares"], ["📷", "camara foto"], ["🎬", "cine pelicula"], ["🗓️", "calendario planificacion"],
  ["👥", "personas grupo equipo"], ["🧑‍💻", "programador desarrollador"], ["🧑‍🎓", "estudiante"],
  ["🤖", "robot ia bot"], ["👾", "alien juego"], ["🧩", "puzzle piezas encaje"], ["🎲", "dado azar"],
  ["🧭", "brujula guia"], ["📣", "megafono anuncio"], ["🔔", "campana notificacion"], ["🔕", "silencio sin notificaciones"],
];

let emojiPickerEl = null;

function closeEmojiPicker() {
  if (emojiPickerEl) {
    emojiPickerEl.remove();
    emojiPickerEl = null;
  }
  document.removeEventListener("keydown", onEmojiPickerKeydown);
}

function onEmojiPickerKeydown(e) {
  if (e.key === "Escape") closeEmojiPicker();
}

function openEmojiPicker(anchorEl, onSelect) {
  closeEmojiPicker();
  closeContextMenu();

  const panel = document.createElement("div");
  panel.className = "mdm-emoji-picker";

  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "Buscar emoji…";
  search.className = "mdm-emoji-search";

  const grid = document.createElement("div");
  grid.className = "mdm-emoji-grid";

  function renderGrid(filter) {
    grid.innerHTML = "";
    const q = filter.trim().toLowerCase();
    const matches = q ? EMOJI_DATA.filter(([, kw]) => kw.includes(q)) : EMOJI_DATA;
    if (matches.length === 0) {
      grid.innerHTML = '<div class="search-empty">Sin resultados.</div>';
      return;
    }
    matches.forEach(([emoji]) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mdm-emoji-option";
      btn.textContent = emoji;
      btn.addEventListener("click", () => {
        onSelect(emoji);
        closeEmojiPicker();
      });
      grid.appendChild(btn);
    });
  }
  renderGrid("");
  search.addEventListener("input", () => renderGrid(search.value));

  panel.append(search, grid);
  document.body.appendChild(panel);

  const anchorRect = anchorEl.getBoundingClientRect();
  panel.style.left = `${Math.min(anchorRect.left, window.innerWidth - 300)}px`;
  panel.style.top = `${anchorRect.bottom + 6}px`;

  emojiPickerEl = panel;
  search.focus();

  setTimeout(() => {
    document.addEventListener(
      "click",
      (e) => {
        if (!panel.contains(e.target)) closeEmojiPicker();
      },
      { once: true }
    );
    document.addEventListener("keydown", onEmojiPickerKeydown);
  }, 0);
}

// ------------------------------------------------------------------ notas

async function selectNote(id) {
  const previous = state.current;
  state.current = { kind: "note", id };
  markActive(id);
  try {
    const [page, backlinks] = await Promise.all([api(`/pages/${id}`), api(`/pages/${id}/backlinks`)]);
    renderNote(page, backlinks);
  } catch (e) {
    // renderNote() no llegó a ejecutarse, así que #main sigue mostrando la
    // página anterior: se devuelven state.current y el resaltado del árbol a
    // donde estaban, para que no apunten a algo que no se ha pintado.
    state.current = previous;
    markActive(previous ? previous.id : null);
    reportError("No se pudo abrir la nota", e);
  }
}

const AUTOSAVE_DELAY_MS = 2000;
// Presupuesto de la cola `keepalive` de los navegadores (~64 KB entre todas
// las peticiones en vuelo): se deja margen para no rozar el límite.
const KEEPALIVE_MAX_BYTES = 60000;

/** Frontmatter YAML + cuerpo, en un único .md descargable -- al ser todo
 * Markdown de por sí (incluidos los metadatos, como frontmatter), exportar
 * es solo juntar ambas cosas en un archivo y ofrecerlo para descargar; no
 * hace falta convertir nada ni tocar el backend. */
function yamlString(value) {
  const clean = String(value ?? "").replace(/\r?\n/g, " ").replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  return `"${clean}"`;
}

function exportPageAsMarkdown(page, bodyMarkdown) {
  const frontmatter = [
    "---",
    `title: ${yamlString(page.title)}`,
    `icon: ${yamlString(page.icon)}`,
    `created_by: ${yamlString(page.created_by)}`,
    `description: ${yamlString(page.description)}`,
    `created_at: ${yamlString(page.created_at)}`,
    `updated_at: ${yamlString(page.updated_at)}`,
    "---",
    "",
    "",
  ].join("\n");

  const blob = new Blob([frontmatter + bodyMarkdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const filename = (page.title || "Sin título").replace(/[/\\?%*:|"<>]/g, "-") + ".md";

  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// -------------------------------------------------------------------- índice
// Desplegable flotante (no una barra a todo lo ancho) arriba a la derecha,
// con los encabezados (H1-H5) de la página abierta -- solo tiene sentido
// dentro de una nota, así que cuelga de document.body (como el resto de
// flotantes) y se cierra en clearMain(). Se recalcula en cada pulsación vía
// onChange, así que no hace falta guardar para que aparezca un encabezado
// nuevo.

let outlineEl = null;

function closeOutline() {
  if (outlineEl) {
    outlineEl.remove();
    outlineEl = null;
  }
}

function renderOutline() {
  if (!outlineEl) return;
  const wasOpen = outlineEl.open;
  const headings = state.editor?.getOutline() ?? [];
  outlineEl.hidden = headings.length === 0;

  const list = outlineEl.querySelector(".note-outline-list");
  list.innerHTML = "";
  headings.forEach((h) => {
    const item = document.createElement("div");
    item.className = "note-outline-item";
    item.style.paddingLeft = `${(h.level - 1) * 12}px`;
    item.textContent = h.text.trim() || "(sin título)";
    item.addEventListener("click", () => {
      state.editor?.jumpTo(h.pos);
      outlineEl.open = false;
    });
    list.appendChild(item);
  });
  outlineEl.open = wasOpen; // reconstruir la lista no debe cerrar el desplegable si ya estaba abierto
}

function createOutlinePanel() {
  closeOutline();
  const details = document.createElement("details");
  details.className = "note-outline-dropdown";
  details.hidden = true; // hasta que renderOutline() confirme que hay encabezados

  const summary = document.createElement("summary");
  summary.textContent = "Índice";

  const list = document.createElement("div");
  list.className = "note-outline-list";

  details.append(summary, list);
  document.body.appendChild(details);
  outlineEl = details;
}

function renderNote(page, backlinks) {
  const main = clearMain();

  // ---- barra superior: estado de guardado + desplegable de backlinks ----
  const topbar = document.createElement("div");
  topbar.className = "note-topbar";

  const saveStatus = document.createElement("div");
  saveStatus.className = "note-savestatus";
  const saveIcon = document.createElement("span");
  saveIcon.className = "note-savestatus-icon";
  const saveLabel = document.createElement("span");
  saveStatus.append(saveIcon, saveLabel);

  function setSaveState(s) {
    saveIcon.innerHTML = "";
    saveStatus.classList.toggle("is-error", s === "error");
    if (s === "saving") {
      const spinner = document.createElement("span");
      spinner.className = "spinner";
      saveIcon.appendChild(spinner);
      saveLabel.textContent = "Guardando…";
    } else if (s === "dirty") {
      saveIcon.textContent = "•";
      saveLabel.textContent = "Cambios sin guardar…";
    } else if (s === "error") {
      saveIcon.textContent = "!";
      saveLabel.textContent = "Error al guardar";
    } else {
      saveIcon.textContent = "✓";
      saveLabel.textContent = "Guardado";
    }
  }
  setSaveState("saved");

  const backDetails = document.createElement("details");
  backDetails.className = "note-backlinks-dropdown";
  const backSummary = document.createElement("summary");
  backSummary.textContent = `Backlinks (${backlinks.length})`;
  const backList = document.createElement("div");
  backList.className = "backlinks-list";
  if (backlinks.length === 0) {
    backList.innerHTML = '<div class="search-empty">Ninguna página enlaza aquí todavía.</div>';
  } else {
    backlinks.forEach((b) => {
      const item = document.createElement("div");
      item.className = "backlink-item";
      item.textContent = b.title;
      item.addEventListener("click", () => goTo("note", b.id));
      backList.appendChild(item);
    });
  }
  backDetails.append(backSummary, backList);

  const exportBtn = document.createElement("button");
  exportBtn.className = "icon-btn";
  exportBtn.textContent = "⇩";
  exportBtn.title = "Exportar como Markdown (.md)";
  exportBtn.addEventListener("click", () => exportPageAsMarkdown(page, state.editor.getValue()));

  const topbarRight = document.createElement("div");
  topbarRight.className = "note-topbar-right";
  topbarRight.append(exportBtn, backDetails);

  topbar.append(saveStatus, topbarRight);

  // ---- imagen de cabecera (opcional) ----
  const headerWrap = document.createElement("div");
  headerWrap.className = "note-header-image-wrap";

  const fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.accept = "image/*";
  fileInput.hidden = true;

  function renderHeaderImage() {
    headerWrap.innerHTML = "";
    if (page.header_image_path) {
      const img = document.createElement("img");
      img.className = "note-header-image";
      // ?t= rompe caché aparte del Cache-Control: no-cache del backend --
      // el nombre de archivo es fijo por página (header.<ext>), así que sin
      // esto quitar una cabecera y subir otra en el momento podía seguir
      // enseñando la vieja hasta recargar.
      img.src = `${page.header_image_path}?t=${Date.now()}`;
      img.alt = "";

      const removeBtn = document.createElement("button");
      removeBtn.className = "note-header-remove";
      removeBtn.textContent = "× Quitar cabecera";
      removeBtn.addEventListener("click", async () => {
        const updated = await api(`/pages/${page.id}/header-image`, { method: "DELETE" });
        page.header_image_path = updated.header_image_path;
        renderHeaderImage();
      });

      headerWrap.append(img, removeBtn);
    } else {
      const addBtn = document.createElement("button");
      addBtn.className = "btn";
      addBtn.textContent = "+ Imagen de cabecera";
      addBtn.addEventListener("click", () => fileInput.click());
      headerWrap.appendChild(addBtn);
    }
  }

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    fileInput.value = "";
    if (!file) return;
    const formData = new FormData();
    formData.append("file", file);
    try {
      const res = await fetch(`/pages/${page.id}/header-image`, { method: "POST", body: formData });
      const data = await res.json().catch(() => null);
      if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
      page.header_image_path = data.header_image_path;
      renderHeaderImage();
    } catch (e) {
      alert("Error al subir la imagen: " + e.message);
    }
  });

  renderHeaderImage();

  // ---- icono + título ----
  const titleRow = document.createElement("div");
  titleRow.className = "note-title-row";

  const iconBtn = document.createElement("button");
  iconBtn.type = "button";
  iconBtn.className = "note-icon-btn";
  iconBtn.textContent = page.icon || "📄";
  iconBtn.title = "Cambiar icono";
  iconBtn.addEventListener("click", () => {
    openEmojiPicker(iconBtn, (emoji) => {
      page.icon = emoji;
      iconBtn.textContent = emoji;
      scheduleSave();
    });
  });

  const titleInput = document.createElement("input");
  titleInput.className = "note-title";
  titleInput.value = page.title;

  titleRow.append(iconBtn, titleInput);

  // ---- meta (creador, obligatorio y de solo lectura -- se fija al crear) ----
  const meta = document.createElement("div");
  meta.className = "note-meta";
  meta.textContent = `Creado por ${page.created_by || "—"}`;

  // ---- descripción (opcional) ----
  const descInput = document.createElement("textarea");
  descInput.className = "note-description";
  descInput.placeholder = "Añade una descripción…";
  descInput.value = page.description || "";
  descInput.rows = 1;
  function autoResizeDesc() {
    descInput.style.height = "auto";
    descInput.style.height = descInput.scrollHeight + "px";
  }

  // ---- cuerpo: el cuadro de texto ES el cuerpo de la página (estilo
  // Notion) -- no hay panel de "vista previa" aparte, ni caja/borde propios
  // que lo distingan del resto de la página ----
  const bodyMount = document.createElement("div");
  bodyMount.id = "note-body";

  // ---- autoguardado (2s tras el último cambio; sin botón "Guardar") ----
  let saveTimer = null;
  let saving = false;
  let saveAgain = false;
  let dirty = false; // hay cambios que el servidor todavía no ha confirmado
  let changeSeq = 0; // sube con cada cambio: distingue "guardado" de "cambiado mientras guardaba"
  let lastSentBody = null; // último cuerpo ACEPTADO por el servidor
  let savingBody = null; // cuerpo del PATCH normal que va de camino (null: ninguno)
  let beaconInFlight = null; // cuerpo del guardado de emergencia que va de camino
  let beaconQueued = false; // se pidió guardar otro contenido mientras había un beacon en vuelo

  /** Cuerpo del PATCH, compartido por el guardado normal y el de emergencia:
   * uno solo, para que no puedan acabar mandando cosas distintas. */
  function buildSaveBody() {
    // El icono es "obligatorio": si lo han vaciado, se restaura un valor por
    // defecto en vez de guardar un icono vacío.
    if (!page.icon) page.icon = "📄";
    return JSON.stringify({
      title: titleInput.value,
      icon: page.icon,
      description: descInput.value,
      body_markdown: state.editor.getValue(),
    });
  }

  function scheduleSave() {
    dirty = true;
    changeSeq += 1;
    setSaveState("dirty");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(doSave, AUTOSAVE_DELAY_MS);
    state.flushPendingSave = () => {
      clearTimeout(saveTimer);
      doSave();
    };
  }

  /** Da el contenido por guardado: no queda nada pendiente ni hay que mandar
   * nada al salir. Solo se llama cuando el servidor lo ha aceptado (o cuando
   * no hay vuelta atrás posible, en el camino de `beforeunload`). */
  function markSaved(body) {
    dirty = false;
    lastSentBody = body; // si hubiera que salir justo después, ya está guardado
    clearTimeout(saveTimer);
    state.flushPendingSave = null;
  }

  /** Lo contrario: el intento no se confirmó. Los cambios vuelven a estar
   * pendientes y el autoguardado normal reintenta a los 2 s. Solo toca el
   * estado compartido si esta nota sigue abierta -- si ya se navegó,
   * `flushPendingSave` y `setSaveState` son de la página nueva. */
  function markUnsaved() {
    dirty = true;
    lastSentBody = null;
    if (state.flushSaveBeacon !== flushSaveBeacon) return;
    setSaveState("dirty");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(doSave, AUTOSAVE_DELAY_MS);
    state.flushPendingSave = () => {
      clearTimeout(saveTimer);
      doSave();
    };
  }

  /** PATCH del guardado de emergencia. A partir de ~64 KB el navegador
   * rechaza en el acto las peticiones con keepalive (límite de una cola
   * compartida); una nota así se manda sin él -- puede que el navegador la
   * corte al irse, pero es lo mejor que hay. */
  function sendBeacon(body) {
    return fetch(`/pages/${page.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: new TextEncoder().encode(body).length <= KEEPALIVE_MAX_BYTES,
    });
  }

  /** Guardado de emergencia al cerrar o esconder la pestaña: un `fetch`
   * normal lo cancela el navegador a medio camino al descargar la página, así
   * que este va con `keepalive` (la petición sigue viva aunque la pestaña no).
   *
   * Dos caminos, porque no es lo mismo irse que quedarse:
   * - `alive: false` (beforeunload): optimista y silencioso. No queda interfaz
   *   ni tiempo donde reintentar, así que se da por enviado en cuanto sale.
   * - `alive: true` (visibilitychange): la pestaña sigue viva, así que solo se
   *   da por guardado si el servidor contesta 2xx. Un 500/413 llega resuelto
   *   (no pasa por el `catch` del fetch) y un corte de red lo rechaza; en los
   *   dos casos los cambios se quedan pendientes y el autoguardado de 2 s los
   *   reintenta. "Enviado" no es "aceptado por el servidor".
   *
   * Y con la pestaña viva va UNA escritura en vuelo a la vez: dos PATCH
   * solapados con contenidos distintos pueden aplicarse en el servidor al
   * revés (el viejo el último) y dejar guardado el texto anterior mientras la
   * pantalla muestra el nuevo. Mientras haya uno en vuelo, el contenido nuevo
   * espera y se reenvía en cuanto contesta (ver `beaconQueued`).
   */
  function flushSaveBeacon({ alive = false } = {}) {
    if (!dirty) return false; // nada pendiente: ni una petición

    // Se construye antes de tocar el estado: si esto lanzara, los cambios
    // siguen marcados como pendientes.
    const body = buildSaveBody();

    // Ese contenido ya va de camino: un segundo PATCH idéntico no aporta nada.
    if (beaconInFlight === body) return false;

    // Hay un PATCH normal en vuelo con OTRO cuerpo que el que hay ahora en
    // pantalla: puede aplicarse después de este momento y dejar en el servidor
    // el texto abandonado. Es la misma trampa que `beaconInFlight`, pero por el
    // camino del autoguardado de 2 s (p. ej. se escribe A, el autoguardado sale
    // y tarda, el usuario deshace y vuelve al texto ya confirmado, y oculta).
    const saveInFlight = saving && savingBody !== null && savingBody !== body;

    // Mismo contenido que el último guardado CONFIRMADO y nada en vuelo
    // (visibilitychange y acto seguido beforeunload, por ejemplo): no se manda
    // dos veces, y se descarta el guardado normal pendiente, que mandaría lo
    // mismo. Con otro cuerpo todavía en vuelo NO vale darlo por guardado: ese
    // PATCH puede aplicarse después y dejar en el servidor su texto, así que se
    // trata como pendiente y se reenvía al contestar (ver `beaconQueued`). Con
    // un guardado normal en vuelo, igual: se deja `dirty` puesto y se reenvía
    // al contestar (ver la rama de `saving` más abajo).
    if (body === lastSentBody && !beaconInFlight && !saveInFlight) {
      markSaved(body);
      if (alive) setSaveState("saved"); // no queda nada por guardar: que no diga lo contrario
      return false;
    }

    if (beaconInFlight) {
      // Hay OTRO contenido en vuelo. Se espera a que conteste y se reenvía
      // entonces lo último escrito: dos PATCH solapados con cuerpos distintos
      // pueden aplicarse al revés y dejar en el servidor el texto viejo.
      beaconQueued = true;
      if (alive) return false;
      // La pestaña se está cerrando: no habrá un "después" donde reintentar,
      // así que se manda lo nuevo igualmente (perderlo del todo es peor que
      // el riesgo de orden) y NO se da por guardado -- si el cierre no llega
      // a ocurrir, el siguiente guardado reenvía lo que hay en pantalla.
      try {
        sendBeacon(body).catch(() => {});
      } catch {
        // sin red o página ya descargándose
      }
      return true;
    }

    if (alive && saving) {
      // Hay un PATCH normal en vuelo: no se solapan dos escrituras. Si lleva
      // otro cuerpo, doSave() deja marcado `saveAgain` y repite en cuanto
      // termine; si lleva justo lo que hay en pantalla, no hay nada que
      // reenviar (y un segundo PATCH idéntico solo sería ruido).
      if (savingBody !== body) doSave();
      return false;
    }

    if (!alive && saveInFlight) {
      // La pestaña se está cerrando y hay un PATCH normal en vuelo con otro
      // contenido: se manda lo de la pantalla igualmente (perderlo del todo es
      // peor que el riesgo de orden, y este va con keepalive), pero NO se da
      // por guardado. Si el cierre no llega a ocurrir y la pestaña sobrevive,
      // el autoguardado de 2 s reenvía el contenido del editor en cuanto
      // termine el PATCH en vuelo; darlo por guardado aquí dejaba el texto
      // viejo en el servidor sin nada que lo corrigiera.
      try {
        sendBeacon(body).catch(() => {}); // la pestaña se está yendo: no hay a quién avisar
      } catch {
        // sin red o página ya descargándose
      }
      return true;
    }

    if (!alive) {
      // Nadie va a leer la respuesta: se da por enviado (así un beforeunload y
      // un visibilitychange seguidos no mandan lo mismo dos veces). El gancho
      // de navegación se deja puesto a propósito: en un cierre real no lo
      // consume nadie, y si el evento no llega a descargar la página, navegar
      // a otra nota reintenta lo último en vez de darlo por perdido.
      dirty = false;
      lastSentBody = body;
      clearTimeout(saveTimer);
      try {
        sendBeacon(body).catch(() => {}); // la pestaña se está yendo: no hay a quién avisar
      } catch {
        // sin red o página ya descargándose
      }
      return true;
    }

    beaconInFlight = body;
    beaconQueued = false;
    try {
      sendBeacon(body)
        .then((res) => {
          // Un 4xx/5xx llega aquí como promesa resuelta: solo vale un 2xx.
          if (!res || !res.ok) throw new Error(`HTTP ${res ? res.status : "sin respuesta"}`);
          if (state.flushSaveBeacon !== flushSaveBeacon) return; // ya no es la nota abierta
          if (buildSaveBody() === body) {
            // El servidor tiene exactamente lo que hay en el editor.
            markSaved(body);
            setSaveState("saved");
            return;
          }
          // Esta confirmación es de un contenido que ya no es el del editor:
          // o se escribió más mientras iba de camino, o esta respuesta vieja
          // llegó detrás de una escritura más nueva y el servidor puede
          // quedarse con el texto anterior. Darlo por guardado dejaba el texto
          // de la pantalla sin guardar y el indicador mintiendo: `dirty` sigue
          // puesto y se reenvía lo último en cuanto esto termine. Pero el
          // servidor SÍ ha aplicado `body`, así que `lastSentBody` pasa a ser
          // ese cuerpo: si no, y el usuario ha vuelto justo al texto confirmado
          // antes de este PATCH, el reenvío se creería ya guardado y no
          // mandaría nada (el servidor se quedaría con el texto abandonado).
          lastSentBody = body;
          beaconQueued = true;
        })
        .catch((e) => {
          // No se confirma nada: los cambios vuelven a la cola del autoguardado.
          console.error("El guardado de emergencia no se confirmó; se reintenta:", e);
          beaconQueued = false; // el reintento de 2 s (markUnsaved) es el camino
          markUnsaved();
        })
        .finally(() => {
          if (beaconInFlight === body) beaconInFlight = null;
          const pending = beaconQueued;
          beaconQueued = false;
          // Ya no hay nada en vuelo: se manda lo último que hay en el editor.
          if (pending && dirty && state.flushSaveBeacon === flushSaveBeacon) {
            flushSaveBeacon({ alive: true });
          }
        });
    } catch {
      beaconInFlight = null;
      beaconQueued = false;
      markUnsaved();
    }
    return true;
  }
  state.flushSaveBeacon = flushSaveBeacon;

  async function doSave() {
    clearTimeout(saveTimer);
    if (saving) {
      saveAgain = true;
      return;
    }
    saving = true;
    setSaveState("saving");

    const seqAtSave = changeSeq;
    const body = buildSaveBody();
    savingBody = body; // para que el guardado de emergencia sepa qué va de camino

    try {
      const updated = await api(`/pages/${page.id}`, { method: "PATCH", body });
      page.updated_at = updated.updated_at;
      page.title = updated.title;
      // Solo queda "nada pendiente" si no se ha escrito nada mientras se
      // guardaba: si se escribió, `dirty` sigue puesto y el guardado de
      // emergencia mandará el contenido nuevo en vez de darlo por guardado.
      // Tampoco basta con que este PATCH se haya aplicado si sigue en vuelo un
      // guardado de emergencia con otro contenido: puede llegar al servidor
      // después y dejar su texto encima. En ese caso no se cierra la cuenta;
      // al contestar ese beacon se reenvía lo que hay en pantalla.
      if (changeSeq === seqAtSave && (!beaconInFlight || beaconInFlight === body)) {
        dirty = false;
        lastSentBody = body; // si hubiera que salir justo después, ya está guardado
        state.flushPendingSave = null;
      }
      // Si sigue habiendo algo pendiente (se escribió durante el guardado, o va
      // otro PATCH de camino), el indicador no puede decir "Guardado": lo
      // manda el autoguardado de 2 s o el reenvío del beacon.
      setSaveState(dirty ? "dirty" : "saved");
      await loadTree();
      markActive(page.id);
    } catch (e) {
      setSaveState("error");
      console.error(e);
    } finally {
      saving = false;
      savingBody = null;
      if (saveAgain) {
        saveAgain = false;
        doSave();
      }
    }
  }

  titleInput.addEventListener("input", scheduleSave);
  descInput.addEventListener("input", () => {
    autoResizeDesc();
    scheduleSave();
  });

  main.append(topbar, headerWrap, fileInput, titleRow, meta, descInput, bodyMount);
  autoResizeDesc();

  // Ctrl/Cmd+S fuerza el guardado ya, sin esperar el debounce (y sin pasar
  // por el diálogo del navegador). Delegado en #main: pulsarlo desde
  // cualquier campo de la nota funciona.
  main.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") {
      e.preventDefault();
      doSave();
    }
  });

  // Comando "/page" del editor: crea una página hija de ÉSTA (page.id, la
  // que está abierta ahora mismo) con el título que el usuario ya escribió
  // en el propio editor (entry.js pide primero el nombre -- ver "naming" en
  // slashMenuExtension -- así se evita crear siempre "Nueva página", que
  // con varias iguales sería ambiguo para resolver el wikilink por título)
  // y devuelve {id, title} para que entry.js inserte el enlace.
  async function createChildPage(title) {
    try {
      const created = await api("/pages", {
        method: "POST",
        body: JSON.stringify({ title, parent_id: page.id, body_markdown: "" }),
      });
      await loadTree();
      return { id: created.id, title: created.title };
    } catch (e) {
      alert("Error al crear la página: " + e.message);
      return null;
    }
  }

  createOutlinePanel();

  state.editor = createNoteEditor({
    parent: bodyMount,
    doc: page.body_markdown || "",
    resolvePage: (title) => findPageByTitle(title),
    onNavigate: (pageId) => navigateToPage(pageId),
    onChange: () => {
      scheduleSave();
      renderOutline();
    },
    onCreatePage: createChildPage,
    onBlockContextMenu: (payload) => {
      const n = payload.count;
      const header = payload.currentTypeLabel || `${n} bloques seleccionados`;
      showContextMenu(payload.x, payload.y, [
        { label: header, items: [] },
        {
          label: "Convertir a",
          items: payload.types.map((t) => ({
            label: t.label,
            onClick: () => {
              payload.convertTo(t.type);
              scheduleSave();
            },
          })),
        },
        {
          items: [
            {
              label: `Eliminar ${n > 1 ? "bloques" : "bloque"}`,
              danger: true,
              onClick: () => {
                payload.deleteBlocks();
                scheduleSave();
              },
            },
          ],
        },
      ]);
    },
    onWikilinkHover: (pageId, rect) => startLinkPreview(pageId, rect),
    onWikilinkHoverEnd: () => stopLinkPreview(),
    onSlashMenu: (menuState) => renderSlashMenu(menuState),
  });

  renderOutline(); // población inicial -- onChange solo dispara con ediciones, no al montar
}

function findPageByTitle(title) {
  const lower = title.toLowerCase();
  for (const p of state.pagesById.values()) {
    if ((p.title || "").toLowerCase() === lower) return p;
  }
  return null;
}

/** Un wikilink puede resolver a una nota o a una database -- son la misma
 * tabla `pages` pero navegan distinto (el id de página de una database no
 * es el id que espera /databases/{id}; ver database_id en el árbol). */
function navigateToPage(pageId) {
  const node = state.pagesById.get(pageId);
  if (node?.type === "database") goTo("database", node.database_id);
  else goTo("note", pageId);
}

// -------------------------------------------------- navegación (atrás/adelante/subir)
// Historial propio, al estilo navegador -- state.current por sí solo no basta
// para "atrás", porque no hay pila. Todo lo que sea "ir a otra página de
// verdad" pasa por goTo(); lo que es solo refrescar la página ya abierta
// (borrar una fila, crear una vista nueva...) sigue llamando a selectNote/
// selectDatabase directamente, para no llenar el historial de duplicados.

async function goTo(kind, id, { fromHistory = false } = {}) {
  if (!fromHistory) {
    // Si veníamos de "atrás", navegar a algo nuevo descarta el "adelante"
    // pendiente -- mismo comportamiento que el historial de un navegador.
    state.history = state.history.slice(0, state.historyPos + 1);
    state.history.push({ kind, id });
    state.historyPos = state.history.length - 1;
  }
  if (kind === "database") await selectDatabase(id);
  else if (kind === "home") await showEmpty();
  else await selectNote(id);
  updateNavButtons();
}

function goHome() {
  goTo("home");
}

function goBack() {
  if (state.historyPos <= 0) return;
  state.historyPos -= 1;
  const entry = state.history[state.historyPos];
  goTo(entry.kind, entry.id, { fromHistory: true });
}

function goForward() {
  if (state.historyPos >= state.history.length - 1) return;
  state.historyPos += 1;
  const entry = state.history[state.historyPos];
  goTo(entry.kind, entry.id, { fromHistory: true });
}

/** El id de página de una database (Page.id) no es el id con el que se
 * navega a ella (DatabaseDef.id, ver database_id) -- para "subir un nivel"
 * desde una database hay que encontrar primero el nodo del árbol que la
 * describe, buscando por database_id en vez de por id directo. */
function getCurrentParentId() {
  if (!state.current) return null;
  if (state.current.kind === "database") {
    const node = [...state.pagesById.values()].find((n) => n.database_id === state.current.id);
    return node?.parent_id ?? null;
  }
  return state.pagesById.get(state.current.id)?.parent_id ?? null;
}

function goUp() {
  const parentId = getCurrentParentId();
  if (parentId) navigateToPage(parentId);
}

function updateNavButtons() {
  const backBtn = document.getElementById("btn-nav-back");
  const fwdBtn = document.getElementById("btn-nav-forward");
  const upBtn = document.getElementById("btn-nav-up");
  if (backBtn) backBtn.disabled = state.historyPos <= 0;
  if (fwdBtn) fwdBtn.disabled = state.historyPos >= state.history.length - 1;
  if (upBtn) upBtn.disabled = !getCurrentParentId();
}

// -------------------------------------------------- vista previa al pasar el ratón

const pagePreviewCache = new Map();
let linkPreviewEl = null;
let hoverToken = 0;

function stopLinkPreview() {
  hoverToken++; // invalida cualquier fetch en curso de un hover ya terminado
  if (linkPreviewEl) {
    linkPreviewEl.remove();
    linkPreviewEl = null;
  }
}

async function startLinkPreview(pageId, rect) {
  const token = ++hoverToken;

  let data = pagePreviewCache.get(pageId);
  if (!data) {
    try {
      data = await api(`/pages/${pageId}`);
      pagePreviewCache.set(pageId, data);
    } catch {
      return;
    }
  }
  if (token !== hoverToken) return; // el ratón ya se fue de ahí mientras se cargaba

  // Nunca puede haber dos tarjetas vivas a la vez: un clic sin Ctrl sobre un
  // wikilink resuelto lo sustituye por su marcado en crudo bajo el propio
  // ratón (ver WikilinkWidget/buildWikilinkDecorations en entry.js), lo que
  // dispara un mouseover nuevo del navegador sin que el hover anterior se
  // haya cerrado -- sin este remove(), la tarjeta vieja queda huérfana en
  // el DOM para siempre (issue #1 en GitHub).
  linkPreviewEl?.remove();

  const card = document.createElement("div");
  card.className = "mdm-link-preview";

  const title = document.createElement("div");
  title.className = "mdm-link-preview-title";
  title.textContent = `${data.icon || "📄"} ${data.title}`;

  const body = document.createElement("div");
  body.className = "mdm-link-preview-body";
  const snippet = (data.body_markdown || "").trim();
  body.textContent = snippet ? snippet.slice(0, 220) : "Sin contenido.";

  card.append(title, body);
  document.body.appendChild(card);

  let left = rect.left;
  let top = rect.bottom + 6;
  const cardRect = card.getBoundingClientRect();
  if (left + cardRect.width > window.innerWidth) left = window.innerWidth - cardRect.width - 8;
  if (top + cardRect.height > window.innerHeight) top = rect.top - cardRect.height - 6;
  card.style.left = `${left}px`;
  card.style.top = `${top}px`;

  linkPreviewEl = card;
}

// -------------------------------------------------------------------- menú "/"
// El estado (qué opciones, cuál va resaltada) lo decide entry.js -- aquí solo
// se pinta. Cada opción trae su propio onClick (cierra sobre la posición del
// "/" y el tipo elegido dentro del editor).

let slashMenuEl = null;

function closeSlashMenu() {
  if (slashMenuEl) {
    slashMenuEl.remove();
    slashMenuEl = null;
  }
}

/** Posiciona un panel flotante junto a (x, y) sin salirse de la ventana. */
function positionFloating(el, x, y) {
  document.body.appendChild(el);
  let left = x;
  let top = y + 4;
  const rect = el.getBoundingClientRect();
  if (left + rect.width > window.innerWidth) left = window.innerWidth - rect.width - 8;
  if (top + rect.height > window.innerHeight) top = y - rect.height - 4;
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
}

function renderSlashMenu(menuState) {
  closeSlashMenu();
  if (!menuState) return;

  // Modo "naming": el usuario eligió "Página" y ahora está escribiendo el
  // nombre directamente en el documento -- aquí solo hace falta un aviso
  // flotante, no una lista (no hay nada que elegir).
  if (menuState.kind === "naming") {
    const hint = document.createElement("div");
    hint.className = "mdm-slash-naming";
    hint.textContent = "Nombre de la página · Enter para crear · Esc para cancelar";
    positionFloating(hint, menuState.x, menuState.y);
    slashMenuEl = hint;
    return;
  }

  const menu = document.createElement("div");
  menu.className = "mdm-slash-menu";

  if (menuState.options.length === 0) {
    menu.innerHTML = '<div class="search-empty">Sin coincidencias.</div>';
  } else {
    menuState.options.forEach((opt, i) => {
      const item = document.createElement("div");
      item.className = "mdm-slash-menu-item" + (i === menuState.selectedIndex ? " active" : "");

      const label = document.createElement("span");
      label.textContent = opt.label;
      const abbrev = document.createElement("span");
      abbrev.className = "mdm-slash-menu-abbrev";
      abbrev.textContent = `/${opt.abbrev}`;
      item.append(label, abbrev);

      // mousedown (no click) + preventDefault: que el editor no pierda el
      // foco/selección antes de que onClick aplique el tipo elegido.
      item.addEventListener("mousedown", (e) => {
        e.preventDefault();
        opt.onClick();
      });
      menu.appendChild(item);
    });
  }

  positionFloating(menu, menuState.x, menuState.y);

  // Con la lista filtrada la opción resaltada por teclado (flechas) puede
  // quedar fuera del área visible del menú (max-height + overflow-y: auto)
  // -- sin esto, ArrowDown/ArrowUp seguían moviendo la selección pero no se
  // veía moverse.
  menu.querySelector(".mdm-slash-menu-item.active")?.scrollIntoView({ block: "nearest" });

  slashMenuEl = menu;
}

// ------------------------------------------------------------- menú contextual
// Genérico: lo usan tanto los bloques del editor como la selección múltiple
// de páginas en el árbol (ver más abajo).

let currentContextMenu = null;

function closeContextMenu() {
  if (currentContextMenu) {
    currentContextMenu.remove();
    currentContextMenu = null;
  }
  document.removeEventListener("keydown", onContextMenuKeydown);
}

function onContextMenuKeydown(e) {
  if (e.key === "Escape") closeContextMenu();
}

/** sections: [{ label?: string, items: [{ label, danger?, onClick }] }] */
function showContextMenu(x, y, sections) {
  closeContextMenu();

  const menu = document.createElement("div");
  menu.className = "mdm-context-menu";
  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;

  sections.forEach((section, i) => {
    if (i > 0) menu.appendChild(document.createElement("div")).className = "mdm-context-menu-sep";
    if (section.label) {
      const label = document.createElement("div");
      label.className = "mdm-context-menu-label";
      label.textContent = section.label;
      menu.appendChild(label);
    }
    section.items.forEach((item) => {
      const btn = document.createElement("button");
      btn.className = "mdm-context-menu-item" + (item.danger ? " danger" : "");
      btn.textContent = item.label;
      btn.addEventListener("click", () => {
        closeContextMenu();
        item.onClick();
      });
      menu.appendChild(btn);
    });
  });

  document.body.appendChild(menu);

  const rect = menu.getBoundingClientRect();
  if (rect.right > window.innerWidth) menu.style.left = `${Math.max(4, window.innerWidth - rect.width - 4)}px`;
  if (rect.bottom > window.innerHeight) menu.style.top = `${Math.max(4, window.innerHeight - rect.height - 4)}px`;

  currentContextMenu = menu;
  setTimeout(() => {
    document.addEventListener("click", closeContextMenu, { once: true });
    document.addEventListener("keydown", onContextMenuKeydown);
  }, 0);
}

// -------------------------------------------------------------- databases

async function selectDatabase(id) {
  const previous = state.current;
  state.current = { kind: "database", id };
  markActive(id);
  try {
    const [database, views] = await Promise.all([api(`/databases/${id}`), api(`/databases/${id}/views`)]);
    await renderDatabase(database, views, views[0]?.id ?? null);
  } catch (e) {
    // Igual que en selectNote: si falla la carga, no se ha pintado nada y el
    // estado vuelve a donde estaba.
    state.current = previous;
    markActive(previous ? previous.id : null);
    reportError("No se pudo abrir la base de datos", e);
  }
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
    try {
      await api(`/databases/${database.id}`, { method: "DELETE" });
      await loadTree();
      showEmpty();
    } catch (e) {
      // Si no se borró, la database sigue en pantalla: mejor decir por qué.
      reportError("No se pudo borrar la base de datos", e);
    }
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
    await goTo("database", database.id);
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
    await goTo("database", database.id);
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

/** Crea una nota y navega a ella, lista para renombrar. Compartida por el
 * "+" de la barra lateral (siempre en la raíz) y "Nueva subpágina" del
 * menú contextual (bajo la página sobre la que se hizo clic derecho). */
async function createNote(parent_id) {
  let page;
  try {
    page = await api("/pages", { method: "POST", body: JSON.stringify({ title: "Sin título", parent_id, body_markdown: "" }) });
  } catch (e) {
    // Antes esto dejaba una promesa rechazada sin capturar y cero avisos: el
    // "+" parecía no hacer nada.
    reportError("No se pudo crear la nota", e);
    return;
  }
  await loadTree();
  await goTo("note", page.id);
  const titleEl = document.querySelector(".note-title");
  titleEl?.focus();
  titleEl?.select();
}

/** "Nueva subpágina" del menú contextual del árbol -- a diferencia del "+"
 * de la barra lateral (siempre en la raíz), esta sí fija un padre concreto:
 * la página sobre la que se hizo clic derecho. */
function createSubpage(node) {
  return createNote(node.id);
}

document.getElementById("btn-new-note").addEventListener("click", () => createNote(null));

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
  try {
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
        navigateToPage(r.id);
      });
      searchResults.appendChild(el);
    });
  } catch (e) {
    // La lista anterior se queda como estaba: el aviso explica que la
    // búsqueda no llegó a hacerse, sin vaciar la barra lateral.
    reportError("No se pudo completar la búsqueda", e);
  }
}

// Clic en zona vacía de la barra lateral (no sobre una fila) -> limpia la
// selección múltiple de páginas.
document.getElementById("nav-sections").addEventListener("click", (e) => {
  if (!e.target.closest(".tree-row")) clearPageSelection();
});

document.getElementById("btn-nav-home").addEventListener("click", goHome);
document.getElementById("btn-nav-back").addEventListener("click", goBack);
document.getElementById("btn-nav-forward").addEventListener("click", goForward);
document.getElementById("btn-nav-up").addEventListener("click", goUp);

// -------------------------------------------------- colapsar la barra lateral
// Se recuerda en localStorage (no en el servidor -- es preferencia de
// pantalla, no dato del workspace) para que no vuelva a aparecer sola cada
// vez que se recarga.
const SIDEBAR_COLLAPSED_KEY = "mdm_sidebar_collapsed";
const layoutEl = document.querySelector(".layout");

function setSidebarCollapsed(collapsed) {
  layoutEl.classList.toggle("sidebar-collapsed", collapsed);
  localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
}

document.getElementById("btn-toggle-sidebar").addEventListener("click", () => {
  setSidebarCollapsed(!layoutEl.classList.contains("sidebar-collapsed"));
});

setSidebarCollapsed(localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1");

// -------------------------------------------------------------------- arranque

loadTree();
showEmpty(); // landing con páginas recientes + fechas cercanas, en vez del hueco estático de index.html
pollStatus();
setInterval(pollStatus, 4000);

// Red de seguridad: si cierras/recargas la pestaña con cambios pendientes
// del autoguardado (< 2s desde la última pulsación), los manda ya -- con
// keepalive, porque un fetch normal se cancela al descargar la página.
// `visibilitychange` cubre lo que `beforeunload` no ve: cambiar de app en el
// móvil, minimizar, bloquear la pantalla -- pero ahí la pestaña sigue viva, así
// que ese camino espera la respuesta y, si el servidor no la acepta, devuelve
// los cambios al autoguardado (ver flushSaveBeacon). Con los dos disparados no
// se guarda dos veces: el mismo contenido que ya va de camino no se repite.
function flushOnExit(alive) {
  try {
    state.flushSaveBeacon?.({ alive });
  } catch {
    // la pestaña se está yendo: ni aviso ni reintento posibles
  }
}
window.addEventListener("beforeunload", () => flushOnExit(false));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") flushOnExit(true);
});
