// Noteping — panel visual. Vanilla JS, sin build, sin dependencias
// propias -- la única excepción es el editor de notas (CodeMirror 6), que
// vendorizamos ya compilado en /vendor/editor.bundle.js (ver
// frontend/editor-src/). Habla directamente con la API del propio origen
// (/pages, /databases, /search, /status).

import { createNoteEditor } from "/vendor/editor.bundle.js";
import { openGraph } from "/graph.js";

const state = {
  pagesById: new Map(), // id -> {id,title,type,icon,parent_id,children} (del árbol; para resolver [[wikilinks]])
  current: null, // {kind: 'note'|'database', id}
  editor: null, // instancia activa del editor de notas (createNoteEditor), o null
  flushPendingSave: null, // fuerza el autoguardado en curso antes de navegar a otra página
  flushSaveBeacon: null, // guarda ya (con keepalive) la nota abierta, al cerrar u ocultar la pestaña
  selectedPages: new Map(), // id de página (nodo del árbol) -> node; selección múltiple en la barra lateral
  lastClickedRowId: null, // para el rango de Mayús+clic
  // La cuenta de por dónde va la app en la pila del NAVEGADOR (P18): la
  // navegación de verdad es `history.pushState`/`popstate`, y esto es lo que
  // necesitan los botones de la cabecera para saber si hay atrás/adelante.
  history: [], // [{kind, id}, ...]
  historyPos: -1, // índice de "dónde estamos" dentro de state.history
  // ¿La pila guardada explica la entrada del navegador en la que estamos? Es
  // `false` cuando el documento se cargó por una navegación que no fue de la
  // app (pegar una URL, un marcador): entonces la cuenta no sabe en qué punto
  // de la pila del navegador estamos y los botones se lo preguntan a él (ver
  // `puedeRetroceder`). Lo decide `abrirAlArrancar`.
  pilaAnclada: true,
  noteSession: null, // nota abierta: qué queda sin guardar y cómo refrescarla (P8, /events)
  repaintAuthor: null, // repinta «Creado por …» de la nota abierta cuando llega /auth/me (P11b)
  refitNoteText: null, // remide el alto de título/descripción de la nota abierta (P14, cambio de ancho)
  noteWidthObserver: null, // ResizeObserver del ancho de la nota abierta (P14); se corta al navegar
  ownRemoteEvents: new Map(), // page_id -> ms: cambio en disco que ha hecho esta app (no es «de fuera»)
  inbox: { dir: "Bandeja", count: 0, pages: [] }, // bandeja de capturas (P13), de GET /inbox
  inboxIds: new Set(), // ids de esa bandeja: el árbol no las repite
  myTasks: { count: 0, today: null, counts: { overdue: 0, today: 0 }, tasks: [], closed_values: [] }, // «Mis tareas», de GET /my-tasks
};

// ---------------------------------------------------------------- helpers

/** La puerta contestó 401 (P11a): la sesión no vale o no hay.
 *
 * Lo único que hace el panel es volver al login; no borra nada ni intenta
 * reintentar, porque sin sesión no hay nada que enseñar. Una sola navegación
 * aunque fallen varias llamadas a la vez (árbol, estado y `/events` se piden
 * en paralelo al arrancar) y se recuerda a dónde se iba.
 */
let redirigiendoAlLogin = false;
function irAlLogin() {
  if (redirigiendoAlLogin) return;
  redirigiendoAlLogin = true;
  const next = location.pathname + location.search;
  if (next && next !== "/") {
    location.replace(`/login?next=${encodeURIComponent(next)}`);
  } else {
    location.replace("/login");
  }
}

/** ¿Es un 401? Si lo es, manda al login y dice que sí a quien llama. */
function sesionExpirada(res) {
  if (!res || res.status !== 401) return false;
  irAlLogin();
  return true;
}

// 3.0, hito 2: la última `version` conocida de cada página (lo que va en
// `If-Match` al guardar). La alimenta `api()` con cada respuesta buena que trae
// `{id, version}`, así que un PATCH hecho desde otro sitio de ESTA pestaña
// (renombrar desde el árbol, cambiar el icono) no hace saltar un conflicto
// falso en el siguiente autoguardado del editor.
const pageVersions = new Map();

function recordVersion(data) {
  if (data && data.id && Number.isInteger(data.version)) {
    pageVersions.set(String(data.id), data.version);
  }
}

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
  // Antes de leer el cuerpo: sin sesión el `detail` es "no autenticado" y lo
  // que hace falta no es enseñarlo, es ir a la pantalla de entrada.
  if (sesionExpirada(res)) {
    const err = new Error("no autenticado");
    err.status = 401;
    throw err;
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    // `status` y el cuerpo viajan dentro del error: quien llama puede
    // distinguir un 409 de un 500 sin mirar el texto del mensaje (las
    // sugerencias usan el 409 para "desanclada" y "ya resuelta").
    const err = new Error((data && data.detail) || (data && data.error) || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  recordVersion(data);
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
 * mientras se navega.
 *
 * `options.sticky` lo deja hasta que se cierre (para un aviso que hay que
 * poder leer con calma, como el de las tareas vencidas) y
 * `options.onDismiss` se llama al cerrarlo, venga del aspa o del temporizador:
 * quien lo pinta puede apuntar que ya se ha visto. */
function showError(message, kind = "error", options = {}) {
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
  // `is-info` es el mismo aviso con el filo de otro color: no es un fallo, es
  // algo que el usuario tiene que saber (por ejemplo, que su arrastre ha
  // cambiado el modo de una propiedad).
  toast.className = kind === "info" ? "error-toast is-info" : "error-toast";
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
  close.innerHTML = svgIcon("i-x");

  let timer = options.sticky ? null : setTimeout(dismiss, ERROR_TOAST_MS);
  function dismiss() {
    if (timer !== null) clearTimeout(timer);
    toast.remove();
    // Sin toasts no hay nada que apilar: se va también el contenedor, para no
    // ir dejando nodos vacíos colgando de body.
    if (stack.childElementCount === 0) stack.remove();
    if (typeof options.onDismiss === "function") options.onDismiss();
  }
  // Retirar sin tocar el contenedor: lo usa el tope de apilado, que va a
  // añadir otro aviso justo después (dismiss() borraría el contenedor vacío
  // y el aviso nuevo se quedaría colgando de un nodo suelto).
  toast.retireToast = () => {
    if (timer !== null) clearTimeout(timer);
    toast.remove();
    if (typeof options.onDismiss === "function") options.onDismiss();
  };
  close.addEventListener("click", dismiss);

  toast.append(text, close);
  stack.appendChild(toast);
  return toast;
}

/** Un aviso informativo (no es un fallo): mismo sitio y misma pila que los
 * errores, con el filo de color de acento. Se usa cuando algo ha salido bien
 * pero el usuario tiene que saber que ha cambiado (el modo de orden de una
 * propiedad, por ejemplo) o cuando hay algo que mirar al entrar («tienes 3
 * tareas vencidas»). */
function showNotice(message, options = {}) {
  return showError(message, "info", options);
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

/** Icono del sprite SVG incrustado en index.html (docs/diseno/iconos.svg).
 * Hereda el color del texto (currentColor) y el tamaño de .svg-icon. */
function svgIcon(id, sizeClass = "") {
  const cls = sizeClass ? `svg-icon ${sizeClass}` : "svg-icon";
  return `<svg class="${cls}" aria-hidden="true" focusable="false"><use href="#${id}"/></svg>`;
}

// Icono por defecto de una página tal como lo guarda la API
// (settings.default_icon) y de una base de datos. Son DATOS del usuario: se
// siguen guardando y enviando tal cual (ver buildSaveBody), pero al pintarlos
// en el DOM se cambian por el icono vectorial de su tipo (SPEC-B §6.1). Si el
// usuario eligió otro emoji del selector, se pinta su carácter.
const DEFAULT_ICON = "📄";
const DEFAULT_ICON_BY_TYPE = { note: DEFAULT_ICON, database: "🗄" };

function isDefaultIcon(type, icon) {
  if (!icon) return true;
  const fallback = DEFAULT_ICON_BY_TYPE[type] || DEFAULT_ICON;
  // Una base creada por la API también arranca con el icono por defecto de página.
  return icon === fallback || (type === "database" && icon === DEFAULT_ICON);
}

function typeIconId(type) {
  return type === "database" ? "i-database" : "i-file-text";
}

/** Pinta el icono de una página/base en `el`: SVG de su tipo si lleva el de
 * por defecto, el emoji del usuario si eligió otro. */
function paintPageIcon(el, type, icon, sizeClass = "") {
  if (isDefaultIcon(type, icon)) el.innerHTML = svgIcon(typeIconId(type), sizeClass);
  else el.textContent = icon;
}

/** Props para dashboardItem: icono de tipo (SVG) o el emoji del usuario. */
function pageIconProps(type, icon) {
  return isDefaultIcon(type, icon) ? { iconType: typeIconId(type) } : { icon };
}

/** Limpia #main: fuerza cualquier autoguardado pendiente (para no perder los
 * últimos <2s de cambios al navegar), y destruye la instancia del editor si
 * había una nota abierta -- si no, CodeMirror sigue vivo (listeners,
 * memoria) aunque su DOM ya no exista, porque quitarle el nodo padre no lo
 * destruye por sí solo. */
function clearMain() {
  // El selector de bases (/base) escribe su filtro DENTRO del documento
  // mientras está abierto. Al dejar la nota su ViewPlugin se destruye y nadie
  // borraba ese tramo: el filtro se quedaba de párrafo suelto y el
  // autoguardado lo escribía en el `.md`. Se tira aquí y ANTES del guardado
  // pendiente, para que el fichero de la nota que se deja salga limpio; el
  // aviso deja de estar en pantalla porque el propio editor lo cierra.
  state.editor?.discardTransientText?.();
  if (state.flushPendingSave) {
    state.flushPendingSave();
    state.flushPendingSave = null;
  }
  // El guardado de emergencia es de la nota que se está dejando atrás: en
  // cuanto se navega, ya no queda nada suyo que pueda estar pendiente.
  state.flushSaveBeacon = null;
  // La sesión de la nota (cambios pendientes, refresco en vivo) es de la nota
  // que se deja atrás: renderNote() pone la suya al terminar.
  state.noteSession = null;
  state.repaintAuthor = null;
  // El vigilante del ancho de la nota es de la nota que se deja atrás: si
  // siguiera vivo, remidiría unos <textarea> que ya no están en el documento.
  if (state.noteWidthObserver) {
    state.noteWidthObserver.disconnect();
    state.noteWidthObserver = null;
  }
  state.refitNoteText = null;
  // La sala de la nota que se deja (3.0, hito 4): se cierra cuando el servidor
  // confirma lo último que se mandó (como mucho, un par de segundos).
  if (state.collabSession) {
    state.collabSession.stop({ graceful: true });
    state.collabSession = null;
  }
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
  closeSuggestionsPanel();
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

function dashboardItem({ icon, iconType, title, sub, meta, metaClass, onClick }) {
  const item = document.createElement("div");
  item.className = "dashboard-item";

  const iconEl = document.createElement("span");
  iconEl.className = "dashboard-item-icon";
  if (iconType) iconEl.innerHTML = svgIcon(iconType, "icon-18");
  else iconEl.textContent = icon;

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
  makeKeyboardActivatable(item, "button", 0);
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
    recentSection.innerHTML +=
      '<div class="empty-hint"><p class="empty-hint-title">Aquí saldrán las últimas páginas que toques.</p>' +
      '<p class="empty-hint-note">Para empezar, crea una nota con «+» en la barra lateral, o una base desde el botón de plantilla que tiene al lado.</p></div>';
  } else {
    data.recent_pages.forEach((p) => {
      recentSection.appendChild(
        dashboardItem({
          ...pageIconProps(p.type, p.icon),
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
          iconType: "i-calendar",
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
  // Los puntos api/db ya no se enseñan (se quitaron de la cabecera); el sondeo
  // sigue porque un 401 en él es lo que manda al login con la sesión caducada.
  const el = document.getElementById(`dot-${name}`);
  if (!el) return;
  el.classList.toggle("ok", ok === true);
  el.classList.toggle("down", ok === false);
}

// ------------------------------------------------- accesibilidad de teclado
// Un <div> con clic no es alcanzable con Tab ni se activa con el teclado: se
// le da el role que le toca y un keydown que dispara el mismo clic. No se
// pone role="treeitem" ni tabIndex=0 a lo loco: un treeitem huérfano (sin
// contenedor role="tree") o una parada de Tab por cada fila dejan la
// navegación peor de lo que estaba (SPEC-B §5, hallazgo #1).
function makeKeyboardActivatable(el, role = "button", tabIndex = 0) {
  el.tabIndex = tabIndex;
  if (role) el.setAttribute("role", role);
  el.addEventListener("keydown", (e) => {
    // Enter/Espacio dentro de un <input>/<select> hijo (celda en edición) no
    // son cosa nuestra.
    if (e.target !== el) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      el.click();
      return;
    }
    // Menú contextual por teclado (Mayús+F10 o la tecla Menú): sin esto, borrar
    // una página o crear una subpágina solo se podía hacer con el botón derecho.
    if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      el.dispatchEvent(
        new MouseEvent("contextmenu", {
          bubbles: true,
          cancelable: true,
          clientX: rect.left + 12,
          clientY: rect.bottom,
        })
      );
    }
  });
  return el;
}

/** Un contenedor role="tree" expone UNA sola parada de Tab (roving tabindex):
 * la fila activa, o la primera si no hay ninguna. El resto se alcanza con
 * ArrowUp/ArrowDown/Home/End, que además mueven el tabIndex=0 con el foco. */
function initTreeRovingTabIndex(container) {
  if (!container) return;
  const rows = [...container.querySelectorAll(".tree-row")];
  if (!rows.length) return;
  const active = rows.find((r) => r.classList.contains("active")) || rows[0];
  rows.forEach((r) => {
    r.tabIndex = r === active ? 0 : -1;
    r.setAttribute("aria-selected", r.classList.contains("active") ? "true" : "false");
  });
  if (container.dataset.rovingBound) return;
  container.dataset.rovingBound = "1";
  container.addEventListener("keydown", (e) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    const currentRows = [...container.querySelectorAll(".tree-row")];
    const idx = currentRows.indexOf(document.activeElement);
    if (idx === -1) return; // el foco está en un hijo de la fila, no en la fila
    e.preventDefault();
    let nextIdx = idx;
    if (e.key === "ArrowDown") nextIdx = Math.min(currentRows.length - 1, idx + 1);
    else if (e.key === "ArrowUp") nextIdx = Math.max(0, idx - 1);
    else if (e.key === "Home") nextIdx = 0;
    else if (e.key === "End") nextIdx = currentRows.length - 1;
    currentRows.forEach((r, i) => { r.tabIndex = i === nextIdx ? 0 : -1; });
    currentRows[nextIdx].focus();
  });
}


/** `id` de un control a partir de la clave de su propiedad, con el índice de
 * la propiedad delante: `nueva-fila-0-empresa`. El índice mantiene el id
 * estable y único aunque dos propiedades se llamen igual, y el prefijo separa
 * los del alta inline de los del diálogo de edición. */
function idDeCampo(prefijo, indice, clave) {
  const limpio = String(clave ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `${prefijo}-${indice}-${limpio || "campo"}`;
}

// ----------------------------------------------- focus trap de los <dialog>
// `showModal()` deja inerte lo de detrás, pero NO encierra el Tab: al pasar del
// último control el foco se queda en el <body>, una parada sin indicador
// visible (hallazgo M6), y desde ahí sigue al primer control del diálogo. Un
// solo keydown en captura lo arregla para todos los <dialog>, también para los
// que se rellenan después de abrirse (tokens, usuarios).
const TABBABLES = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

/** Las paradas de Tab reales dentro de un diálogo: ni ocultas con `hidden` (el
 * bloque del token recién creado), ni plegadas, ni con tabindex negativo. */
function focusablesDe(dialog) {
  return [...dialog.querySelectorAll(TABBABLES)].filter(
    (el) => el.tabIndex >= 0 && !el.closest("[hidden]") && el.getClientRects().length > 0,
  );
}

document.addEventListener(
  "keydown",
  (event) => {
    if (event.key !== "Tab" || event.ctrlKey || event.altKey || event.metaKey) return;
    const abiertos = document.querySelectorAll("dialog[open]");
    if (!abiertos.length) return;
    const dialog = abiertos[abiertos.length - 1]; // el de más arriba, si hubiera dos
    const controles = focusablesDe(dialog);
    if (!controles.length) {
      // Un diálogo sin controles enfocables no debe soltar el foco al body.
      event.preventDefault();
      dialog.focus();
      return;
    }
    const primero = controles[0];
    const ultimo = controles[controles.length - 1];
    const activo = document.activeElement;
    if (!dialog.contains(activo)) {
      // El foco se ha ido fuera (p. ej. al body): vuelve al extremo que toca.
      event.preventDefault();
      (event.shiftKey ? ultimo : primero).focus();
      return;
    }
    if (!controles.includes(activo)) {
      // Foco en el propio <dialog> o en algo que no es parada de Tab: el
      // siguiente Tab entra por el principio (o por el final, hacia atrás).
      event.preventDefault();
      (event.shiftKey ? ultimo : primero).focus();
      return;
    }
    if (event.shiftKey && activo === primero) {
      event.preventDefault();
      ultimo.focus();
    } else if (!event.shiftKey && activo === ultimo) {
      event.preventDefault();
      primero.focus();
    }
  },
  true,
);

// --------------------------------------------------------- quién eres (P11b)
// El backend guarda `created_by` como el `display_name` del usuario (texto, no
// un id), así que para pintar el punto de su color hay que saber de quién se
// trata. `/auth/me` da el usuario de la sesión y aquí se recuerdan los que este
// navegador ha visto —el propio y los que cree un admin— en localStorage, para
// que el color siga ahí tras una recarga.
//
// Los usuarios viven en el índice, no en el vault. El panel de administración
// los pide a `GET /auth/users`; `knownUsers` queda como respaldo (el usuario de
// la sesión y los que este navegador haya visto) para cuando esa llamada falla y
// para no volver a preguntar por cada color.
const KNOWN_USERS_KEY = "mdm_known_users";

const sessionUser = { me: null }; // el de /auth/me

// ---------------------------------------------------------------- avatares
// Las imágenes de usuario de la app (bloque B): las mismas que `auth.AVATARS`
// en el servidor, en `/avatars/<id>.svg`. Van donde antes iba el punto de
// color de cada persona. El color sigue para el cursor (y ahí lo reparte la
// sala, uno distinto por persona en cada nota).
const AVATARES = Array.from({ length: 16 }, (_, i) => `persona${String(i + 1).padStart(2, "0")}`);
function nombreDeAvatar(id) {
  return `Imagen ${Number(String(id).replace(/\D/g, "")) || ""}`.trim();
}

/** La imagen de un avatar (o `null` si no es uno de la app). Decorativa: el
 * nombre de la persona va siempre al lado. */
function avatarImg(avatar, clase = "avatar-img") {
  if (!AVATARES.includes(avatar)) return null;
  const img = document.createElement("img");
  img.className = clase;
  img.src = `/avatars/${avatar}.svg`;
  img.alt = "";
  img.setAttribute("aria-hidden", "true");
  img.decoding = "async";
  img.draggable = false;
  return img;
}

function avatarHtml(avatar, clase = "avatar-img") {
  return AVATARES.includes(avatar)
    ? `<img class="${clase}" src="/avatars/${avatar}.svg" alt="" aria-hidden="true" draggable="false">`
    : "";
}

/** El selector de imagen: una rejilla de 16, como un grupo de radios (flechas
 * para moverse, Espacio/Intro o clic para elegir). `alElegir(id)` se llama
 * con la elegida; devuelve `{valor, fijar(id)}`. */
function montarSelectorDeAvatar(caja, actual, alElegir) {
  let valor = AVATARES.includes(actual) ? actual : null;
  caja.textContent = "";
  const botones = AVATARES.map((id) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "avatar-option";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-label", nombreDeAvatar(id));
    b.title = nombreDeAvatar(id);
    b.dataset.avatar = id;
    b.appendChild(avatarImg(id, ""));
    b.addEventListener("click", () => elegir(id, true));
    caja.appendChild(b);
    return b;
  });
  function pintar() {
    botones.forEach((b, i) => {
      const marcado = b.dataset.avatar === valor;
      b.setAttribute("aria-checked", marcado ? "true" : "false");
      // Un solo botón en el orden del Tab: el elegido (o el primero).
      b.tabIndex = marcado || (!valor && i === 0) ? 0 : -1;
    });
  }
  function elegir(id, avisar) {
    valor = id;
    pintar();
    if (avisar) alElegir?.(id);
  }
  caja.addEventListener("keydown", (e) => {
    const i = botones.indexOf(document.activeElement);
    if (i < 0) return;
    const paso = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (!paso) return;
    e.preventDefault();
    const j = (i + paso + botones.length) % botones.length;
    botones[j].focus();
    elegir(botones[j].dataset.avatar, true);
  });
  pintar();
  return {
    get valor() {
      return valor;
    },
    fijar(id) {
      elegir(AVATARES.includes(id) ? id : null, false);
    },
  };
}
const knownUsers = new Map(); // display_name -> {username, display_name, color, is_admin}

function loadKnownUsers() {
  try {
    const stored = JSON.parse(localStorage.getItem(KNOWN_USERS_KEY) || "[]");
    if (Array.isArray(stored)) stored.forEach((u) => rememberUser(u, { persist: false }));
  } catch {
    // localStorage con basura: no es un dato crítico, se empieza de cero
  }
}

function rememberUser(user, { persist = true } = {}) {
  if (!user || !user.display_name) return;
  knownUsers.set(user.display_name, {
    username: user.username || "",
    display_name: user.display_name,
    color: user.color || null,
    avatar: user.avatar || null,
    is_admin: Boolean(user.is_admin),
  });
  if (!persist) return;
  try {
    localStorage.setItem(KNOWN_USERS_KEY, JSON.stringify([...knownUsers.values()]));
  } catch {
    // sin sitio en localStorage: el color se pierde al recargar, nada más
  }
}

/** Avatar del usuario que firma `displayName`, o null si no se conoce. */
function avatarForAuthor(displayName) {
  const name = (displayName || "").trim();
  return name ? knownUsers.get(name)?.avatar || null : null;
}

/** Color del usuario que firma `displayName`, o null si no se conoce. */
function colorForAuthor(displayName) {
  const name = (displayName || "").trim();
  return name ? knownUsers.get(name)?.color || null : null;
}

/** «Creado por <nombre>», con el punto del color de quien la creó.
 *
 * El color llega resuelto del servidor (`created_by_color`, P11b): así la nota
 * de otro usuario sale con SU color aunque este navegador no lo haya visto
 * nunca ni sea admin. `knownUsers` queda como respaldo (respuestas antiguas o
 * un autor cambiado a mano).
 *
 * Sin color (notas viejas con «Tú», o un `created_by` que no es de nadie) no
 * hay punto y el texto se queda en el gris de .note-meta: inventar un color
 * para un nombre que no es de nadie sería mentir con una bonita paleta.
 */
function paintCreatedBy(el, createdBy, createdByColor, createdByAvatar = null) {
  el.textContent = "";
  const name = (createdBy || "").trim();
  if (!name) {
    el.textContent = "Creado por —";
    return;
  }
  // Su imagen (bloque B); sin imagen conocida, el punto de color de antes.
  const img = avatarImg(createdByAvatar || avatarForAuthor(name));
  const color = createdByColor || colorForAuthor(name);
  if (img) {
    el.appendChild(img);
  } else if (color) {
    const dot = document.createElement("span");
    dot.className = "user-dot";
    dot.style.background = color;
    dot.setAttribute("aria-hidden", "true");
    el.appendChild(dot);
  }
  el.appendChild(document.createTextNode(`Creado por ${name}`));
}

// ---------------------------------------------------------------- el chip

// ------------------------------------------------------------- marca: Noteping

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * El símbolo de Noteping (B2), en línea para que use los tokens `--np-*`.
 *
 * - `variante: "color"` (por defecto): tile, N y el punto coral.
 * - `variante: "mono"`: una sola tinta, `currentColor` (lo pinta el `color:`
 *   de donde va: impresión, deshabilitado, un icono que hereda).
 * - `sinLeer: true`: el sonar N1 sobre el punto (`data-unread`); se enciende
 *   y apaga después con `fijarMarcaSinLeer`.
 *
 * Es decorativo (`aria-hidden`): el nombre va escrito al lado, y el «sin
 * leer» se dice con texto (ver `fijarMarcaSinLeer`).
 */
function marcaNoteping({ variante = "color", tamano = 28, sinLeer = false } = {}) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 64 64");
  svg.setAttribute("width", String(tamano));
  svg.setAttribute("height", String(tamano));
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.classList.add("np-mark");
  if (variante === "mono") svg.classList.add("np-mark--mono");
  svg.dataset.unread = sinLeer ? "true" : "false";
  const pieza = (tag, attrs) => {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    svg.appendChild(el);
  };
  pieza("rect", { class: "np-tile", x: ".5", y: ".5", width: "63", height: "63", rx: "15" });
  pieza("path", { class: "np-n", d: "M20.5 46.5V18.5L43.5 46.5V31" });
  pieza("circle", { class: "np-ring np-ring--a", cx: "43.5", cy: "19", r: "4.6" });
  pieza("circle", { class: "np-ring np-ring--b", cx: "43.5", cy: "19", r: "4.6" });
  pieza("circle", { class: "np-dot", cx: "43.5", cy: "19", r: "4.6" });
  return svg;
}

/** La marca de la cabecera de la barra lateral, a 28 px. */
function montarMarca() {
  const hueco = document.getElementById("brand-mark");
  if (hueco) hueco.replaceWith(marcaNoteping({ tamano: 28 }));
}

/**
 * Enciende o apaga el sonar de la marca de la cabecera. `n` es cuántas cosas
 * hay sin leer (0 lo apaga). Lo llamará el panel de notificaciones cuando
 * exista; hasta entonces nadie lo enciende.
 */
function fijarMarcaSinLeer(n) {
  const marca = document.querySelector("#sidebar-brand .np-mark");
  const aviso = document.getElementById("brand-unread");
  const hay = Number(n) > 0;
  if (marca) marca.dataset.unread = hay ? "true" : "false";
  if (aviso) aviso.textContent = hay ? (n === 1 ? "1 aviso sin leer" : `${n} avisos sin leer`) : "";
}

function paintUserChip() {
  const wrap = document.getElementById("user-chip-wrap");
  const chip = document.getElementById("user-chip");
  const menu = document.getElementById("user-menu");
  const me = sessionUser.me;
  if (!wrap || !chip || !menu) return;
  if (!me) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  // La inicial sobre tu color, como el cuadrado del espacio en Notion.
  const avatar = chip.querySelector(".user-avatar");
  const img = avatarImg(me.avatar, "");
  avatar.textContent = "";
  avatar.classList.toggle("has-img", Boolean(img));
  if (img) {
    avatar.style.background = "";
    avatar.appendChild(img);
  } else {
    avatar.style.background = me.color || "var(--ink-muted)";
    avatar.textContent = (me.display_name || me.username || "?").trim().charAt(0).toUpperCase();
  }
  chip.querySelector(".user-chip-name").textContent = me.display_name;
  chip.title = `${me.display_name} (@${me.username})${me.is_admin ? " · administrador" : ""}`;
  chip.setAttribute("aria-label", `Tu cuenta: ${me.display_name}. Abrir el menú`);
  // «Administración» solo existe para un admin: a los demás no se les enseña
  // un apartado que solo puede dar 403.
  const pestanaAdmin = document.querySelector('.settings-tab[data-section="admin"]');
  if (pestanaAdmin) pestanaAdmin.hidden = !me.is_admin;
}

function initUserChip() {
  const wrap = document.getElementById("user-chip-wrap");
  const chip = document.getElementById("user-chip");
  const menu = document.getElementById("user-menu");
  if (!wrap || !chip || !menu) return;

  const closeMenu = () => {
    menu.hidden = true;
    chip.setAttribute("aria-expanded", "false");
  };
  const openMenu = () => {
    menu.hidden = false;
    chip.setAttribute("aria-expanded", "true");
  };

  chip.addEventListener("click", (event) => {
    event.stopPropagation();
    if (menu.hidden) openMenu();
    else closeMenu();
  });
  // Clic en cualquier otro sitio o Escape: se cierra. Sin esto el menú se
  // quedaba abierto encima del contenido al navegar.
  document.addEventListener("click", (event) => {
    if (!menu.hidden && !wrap.contains(event.target)) closeMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      closeMenu();
      chip.focus();
    }
  });

  menu.querySelector('[data-action="settings"]').addEventListener("click", () => {
    closeMenu();
    openSettings();
  });
  menu.querySelector('[data-action="logout"]').addEventListener("click", () => {
    closeMenu();
    logOut();
  });
}

/** Pide /auth/me. Un 401 ya lo gestiona api(): manda al login y lanza. */
async function loadCurrentUser() {
  try {
    const me = await api("/auth/me");
    sessionUser.me = me;
    rememberUser(me);
    paintUserChip();
    // La nota abierta pudo pintarse antes de saber quién eres: se repinta su
    // autoría para que el color no falte por una carrera de milisegundos.
    state.repaintAuthor?.();
  } catch (e) {
    if (e?.status === 401) return; // ya se está yendo al login
    reportError("No se pudo saber qué usuario tienes", e);
  }
}

/** Cerrar la sesión: la cookie deja de valer en el servidor y se va al login.
 * Si la petición no llega, NO se navega: con la sesión todavía viva, /login
 * devolvería al panel y el botón parecería roto. */
async function logOut() {
  try {
    await api("/auth/logout", { method: "POST" });
  } catch (e) {
    if (e?.status === 401) return;
    reportError("No se pudo cerrar la sesión", e);
    return;
  }
  location.replace("/login");
}

// ------------------------------------------------------- diálogo: contraseña

const passwordForm = document.getElementById("form-password");
const passwordNote = document.getElementById("pw-note");
const PASSWORD_NOTE = passwordNote?.textContent || "";

/** Aviso dentro de un diálogo: el mismo hueco para «cambiando…», el error del
 * backend y el «hecho». */
function setFormNote(el, text, kind = "") {
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("form-error", kind === "error");
  el.classList.toggle("form-ok", kind === "ok");
}

/** Deja el formulario de la contraseña como nuevo (al entrar en «Usuario»). */
function prepararContrasena() {
  if (!passwordForm) return;
  passwordForm.reset();
  setFormNote(passwordNote, PASSWORD_NOTE);
  document.getElementById("btn-save-password").disabled = false;
}

passwordForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const current = document.getElementById("pw-current").value;
  const next = document.getElementById("pw-new").value;
  const repeat = document.getElementById("pw-repeat").value;
  if (!current) {
    return setFormNote(passwordNote, "Escribe tu contraseña actual.", "error");
  }
  if (next.length < 8) {
    return setFormNote(passwordNote, "La contraseña nueva necesita al menos 8 caracteres.", "error");
  }
  if (next !== repeat) {
    return setFormNote(passwordNote, "Las dos contraseñas nuevas no coinciden.", "error");
  }

  const save = document.getElementById("btn-save-password");
  save.disabled = true;
  setFormNote(passwordNote, "Cambiando…");
  try {
    const me = await api("/auth/password", {
      method: "POST",
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
    // El backend cierra las demás sesiones y deja esta abierta con una cookie
    // nueva (viene puesta en la respuesta); aquí solo queda reflejarlo.
    if (me) {
      sessionUser.me = me;
      rememberUser(me);
      paintUserChip();
    }
    passwordForm.reset();
    setFormNote(
      passwordNote,
      "Contraseña cambiada. Las demás sesiones abiertas se han cerrado.",
      "ok",
    );
  } catch (e) {
    if (e?.status === 401) return;
    setFormNote(passwordNote, describeError(e), "error");
  } finally {
    save.disabled = false;
  }
});


// --------------------------------------------------------- diálogo: usuarios

const usersNote = document.getElementById("users-note");
const usersList = document.getElementById("users-list");
const newUserForm = document.getElementById("form-new-user");
const newUserNote = document.getElementById("nu-note");
const NEW_USER_NOTE = newUserNote?.textContent || "";

// El aviso de «no se pudo cargar el listado» lleva el número de usuarios que se
// están enseñando: si se queda escrito al abrir, crear uno nuevo lo dejaba
// mintiendo («(1)» con dos filas). Por eso se repinta con cada lista. Con
// `GET /auth/users` en la API el aviso solo sale si esa llamada falla.
let usersListUnavailable = false;

function paintUsersNote() {
  if (!usersListUnavailable) return;
  const count = knownUsers.size;
  setFormNote(
    usersNote,
    "No se pudo cargar el listado de usuarios: aquí solo salen los que este " +
      `navegador conoce (${count} ${count === 1 ? "usuario" : "usuarios"}).`,
  );
}

/** Filas de usuario (punto, nombre, @usuario y admin), de la lista que llegue. */
function renderUsers(users) {
  if (!usersList) return 0;
  usersList.textContent = "";
  const sorted = [...users].sort((a, b) =>
    (a.display_name || "").localeCompare(b.display_name || "", "es"),
  );
  sorted.forEach((u) => {
    const row = document.createElement("div");
    row.className = "user-row";
    let dot = avatarImg(u.avatar);
    if (!dot) {
      dot = document.createElement("span");
      dot.className = "user-dot";
      dot.style.background = u.color || "var(--ink-muted)";
      dot.setAttribute("aria-hidden", "true");
    }
    const name = document.createElement("span");
    name.className = "user-name";
    name.textContent = u.display_name;
    const handle = document.createElement("span");
    handle.className = "user-handle";
    handle.textContent = `@${u.username}`;
    row.append(dot, name, handle);
    if (u.is_admin) {
      const badge = document.createElement("span");
      badge.className = "user-admin-badge";
      badge.textContent = "admin";
      row.appendChild(badge);
    }
    usersList.appendChild(row);
  });
  paintUsersNote();
  return sorted.length;
}

/** Respaldo: los que conoce este navegador (si `GET /auth/users` falla). */
function renderKnownUsers() {
  return renderUsers([...knownUsers.values()]);
}

/** El apartado «Administración»: el formulario limpio y la lista del servidor. */
async function cargarUsuariosDeAjustes() {
  if (!usersList || !newUserForm) return;
  newUserForm.reset();
  selectorDeAlta.fijar(null);
  setFormNote(newUserNote, NEW_USER_NOTE);
  // Se vuelve a preguntar en cada apertura: puede que la API ya lo tenga.
  usersListUnavailable = false;
  renderKnownUsers();
  setFormNote(usersNote, "Cargando usuarios…");
  try {
    // El listado es del servidor (`GET /auth/users`, solo admin): un segundo
    // admin en un navegador limpio ve a todos, no solo a sí mismo.
    const fetched = await api("/auth/users");
    if (Array.isArray(fetched)) {
      fetched.forEach((u) => rememberUser(u));
      usersListUnavailable = false;
      renderUsers(fetched);
    }
    setFormNote(usersNote, "");
  } catch (e) {
    if (e?.status === 401) return;
    usersListUnavailable = true;
    renderKnownUsers(); // pinta la lista conocida y el aviso con su cuenta
  }
}

// ----------------------------------------------- copias de seguridad (plan H0)

const copiasNota = document.getElementById("backup-note");

function fmtTamano(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function pintarCopias(estado) {
  const programacion = document.getElementById("backup-schedule");
  if (programacion) programacion.hidden = !estado.configured;
  if (!estado.configured) {
    setFormNote(
      copiasNota,
      "Las copias automáticas están desactivadas: el servidor no tiene BACKUP_DIR (una carpeta donde guardarlas).",
    );
    return;
  }
  document.getElementById("backup-frequency").value = estado.frequency;
  document.getElementById("backup-keep").value = estado.keep;
  const lista = document.getElementById("backup-files");
  lista.replaceChildren(
    ...estado.files.map((f) => {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = `/backups/files/${encodeURIComponent(f.name)}`;
      a.textContent = f.name;
      const extra = document.createElement("span");
      extra.textContent = `${fmtFecha(f.created_at)} · ${fmtTamano(f.size)}`;
      li.append(a, extra);
      return li;
    }),
  );
  setFormNote(
    copiasNota,
    estado.last ? `Última copia automática: ${fmtFecha(estado.last)}.` : "Todavía no se ha hecho ninguna copia automática.",
  );
}

async function cargarCopias() {
  setFormNote(copiasNota, "");
  try {
    pintarCopias(await api("/backups"));
  } catch (e) {
    if (e?.status === 401) return;
    setFormNote(copiasNota, e?.message || "No se pudo leer el estado de las copias.", "error");
  }
}

document.getElementById("btn-backup-download")?.addEventListener("click", async (ev) => {
  const boton = ev.currentTarget;
  boton.disabled = true;
  setFormNote(copiasNota, "Preparando la copia…");
  try {
    const res = await fetch("/backups/download", { method: "POST" });
    if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || `HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = /filename="?([^";]+)/.exec(res.headers.get("content-disposition") || "")?.[1] || "noteping-copia.tar.gz";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    setFormNote(copiasNota, `Copia descargada (${fmtTamano(blob.size)}).`, "ok");
  } catch (e) {
    setFormNote(copiasNota, e?.message || "No se pudo hacer la copia.", "error");
  } finally {
    boton.disabled = false;
  }
});

document.getElementById("btn-backup-save")?.addEventListener("click", async () => {
  try {
    const keep = Math.min(90, Math.max(1, parseInt(document.getElementById("backup-keep").value, 10) || 7));
    const estado = await api("/backups/settings", {
      method: "PUT",
      body: JSON.stringify({ frequency: document.getElementById("backup-frequency").value, keep }),
    });
    pintarCopias(estado);
    setFormNote(copiasNota, "Guardado.", "ok");
  } catch (e) {
    setFormNote(copiasNota, e?.message || "No se pudo guardar.", "error");
  }
});

document.getElementById("btn-backup-run")?.addEventListener("click", async (ev) => {
  const boton = ev.currentTarget;
  boton.disabled = true;
  setFormNote(copiasNota, "Haciendo la copia…");
  try {
    const hecho = await api("/backups/run", { method: "POST" });
    pintarCopias(hecho);
    setFormNote(copiasNota, `Copia hecha: ${hecho.made.name}.`, "ok");
  } catch (e) {
    setFormNote(copiasNota, e?.message || "No se pudo hacer la copia.", "error");
  } finally {
    boton.disabled = false;
  }
});

// La imagen del usuario nuevo: se elige aquí y viaja con el alta.
const selectorDeAlta = montarSelectorDeAvatar(
  document.getElementById("nu-avatar") || document.createElement("div"),
  null,
  null,
);

newUserForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = document.getElementById("nu-username").value.trim().toLowerCase();
  const displayName = document.getElementById("nu-display").value.trim();
  const password = document.getElementById("nu-password").value;
  const isAdmin = document.getElementById("nu-admin").checked;

  if (!username) {
    return setFormNote(newUserNote, "El usuario no puede estar vacío.", "error");
  }
  if (!displayName) {
    return setFormNote(newUserNote, "El nombre visible no puede estar vacío.", "error");
  }
  if (password.length < 8) {
    return setFormNote(
      newUserNote,
      "La contraseña del usuario nuevo necesita al menos 8 caracteres.",
      "error",
    );
  }

  const create = document.getElementById("btn-create-user");
  create.disabled = true;
  setFormNote(newUserNote, "Creando…");
  try {
    const created = await api("/auth/users", {
      method: "POST",
      body: JSON.stringify({
        username,
        display_name: displayName,
        password,
        is_admin: isAdmin,
        ...(selectorDeAlta.valor ? { avatar: selectorDeAlta.valor } : {}),
      }),
    });
    rememberUser(created);
    newUserForm.reset();
    selectorDeAlta.fijar(null);
    renderKnownUsers();
    setFormNote(newUserNote, `Usuario «${created.display_name}» creado.`, "ok");
  } catch (e) {
    if (e?.status === 401) return;
    setFormNote(newUserNote, describeError(e), "error");
  } finally {
    create.disabled = false;
  }
});


// ------------------------------------------------- bandeja de capturas (P13)
//
// Lo que entra por un atajo del sistema (`POST /capture`, ver tools/capture/)
// cae en `Bandeja/` y no es todavía una página del wiki. La sección de arriba
// de la barra lateral lo enseña solo si hay algo —«Bandeja (0)» sería ruido— y
// cada nota tiene su «Procesar»: elegir carpeta (o crear una) y moverla, que es
// el PATCH de `parent_id` de siempre. El árbol no repite estas notas.

/** Pide la bandeja y la pinta. La llama `loadTree()`, así que un evento del
 * vigilante (una captura desde fuera) la refresca sola. */
async function loadInbox() {
  let bandeja;
  try {
    bandeja = await api("/inbox");
  } catch (e) {
    if (e?.status === 401) return; // ya se está yendo al login
    reportError("No se pudo cargar la bandeja", e);
    return;
  }
  state.inbox = bandeja;
  state.inboxIds = new Set((bandeja.pages || []).map((p) => p.id));
  paintInbox();
}

function paintInbox() {
  const section = document.getElementById("inbox-section");
  const list = document.getElementById("inbox-list");
  const label = document.getElementById("inbox-label");
  if (!section || !list || !label) return;

  const { count = 0, pages = [] } = state.inbox || {};
  // Vacía: la sección no existe (ni un «Bandeja (0)» que no dice nada) y la
  // etiqueta se queda sin número. Antes se ocultaba la sección pero el
  // «Bandeja (N)» de la vez anterior seguía en el DOM, así que el contador
  // decía que había algo pendiente cuando ya no había nada (C1).
  section.hidden = count === 0;
  if (count === 0) {
    label.textContent = "Bandeja";
    list.textContent = "";
    list.hidden = true;
    return;
  }

  label.textContent = `Bandeja (${count})`;
  list.textContent = "";
  pages.forEach((page) => list.appendChild(inboxRow(page)));
  list.hidden = !inboxAbierta;
}

let inboxAbierta = false;

function inboxRow(page) {
  const row = document.createElement("div");
  row.className = "inbox-item";
  row.setAttribute("role", "listitem");

  const open = document.createElement("button");
  open.type = "button";
  open.className = "inbox-open";
  open.title = `Abrir «${page.title}»`;
  const icon = document.createElement("span");
  icon.className = "type-icon";
  paintPageIcon(icon, "note", page.icon);
  const title = document.createElement("span");
  title.className = "inbox-title";
  title.textContent = page.title || "Sin título";
  open.append(icon, title);
  open.addEventListener("click", () => goTo("note", page.id));

  const process = document.createElement("button");
  process.type = "button";
  process.className = "inbox-process";
  process.textContent = "Procesar";
  process.setAttribute("aria-label", `Procesar «${page.title}»`);
  process.addEventListener("click", () => openProcesarDialog(page));

  // Borrar una captura es borrar una nota: el mismo icono de basura del árbol,
  // con el `.icon-btn` que ya existe (nada nuevo que estilar).
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "icon-btn";
  remove.title = `Borrar «${page.title}»`;
  remove.setAttribute("aria-label", `Borrar «${page.title}»`);
  remove.innerHTML = svgIcon("i-trash");
  remove.addEventListener("click", () => borrarCaptura(page));

  row.append(open, process, remove);
  return row;
}

/** Borra una captura por el camino de siempre: `bulkDeletePages` —con su
 * confirmación— y `DELETE /pages/{id}`, que manda el `.md` a
 * `.trash/<fecha>/Bandeja/`, nunca un borrado directo. Refresca el árbol y con
 * él la bandeja: la fila desaparece, el contador se limpia y, si era la
 * última, la sección se va. */
function borrarCaptura(page) {
  bulkDeletePages([page]);
}

function initInbox() {
  const toggle = document.getElementById("btn-inbox");
  const list = document.getElementById("inbox-list");
  if (!toggle || !list) return;
  toggle.addEventListener("click", () => {
    inboxAbierta = !inboxAbierta;
    list.hidden = !inboxAbierta;
    toggle.setAttribute("aria-expanded", inboxAbierta ? "true" : "false");
  });
  // Elegir una página o escribir una nueva son excluyentes: lo último que se
  // toca es lo que manda, y el otro campo se vacía para que no parezca que se
  // van a hacer las dos cosas.
  procesarNew?.addEventListener("input", () => {
    // Otro nombre, otra comprobación: lo que dijo el servidor era de este.
    coincidenciasDelServidor = null;
    if (procesarNew.value.trim()) procesarSelect.value = "";
    paintCoincidencias();
  });
  procesarSelect?.addEventListener("change", () => {
    if (!procesarSelect.value) return;
    // Elegir una página descarta lo que dijera el servidor: la coincidencia
    // era del nombre que ya no se va a crear. Sin esto el bloque «ya existe»
    // se quedaba pintado sobre ese nombre viejo y «Procesar» seguía
    // rechazando la página recién elegida, sin más salida que reabrir el
    // diálogo o pulsar «Usar esta» (que pisa la elección hecha).
    const pendiente = Boolean(coincidenciasDelServidor?.length);
    procesarNew.value = "";
    coincidenciasDelServidor = null;
    paintCoincidencias();
    // El aviso de la colisión también era del nombre viejo: la nota vuelve a
    // su texto de siempre en vez de seguir anunciando «ya existe» ahí abajo.
    if (pendiente) setFormNote(procesarNote, PROCESAR_NOTE);
  });
}

// ------------------------------------------------------- diálogo: procesar

const procesarDialog = document.getElementById("dialog-procesar");
const procesarTitle = document.getElementById("proc-title");
const procesarSelect = document.getElementById("proc-parent");
const procesarNew = document.getElementById("proc-new");
const procesarNote = document.getElementById("proc-note");
const PROCESAR_NOTE = procesarNote?.textContent || "";
let procesarPage = null;

/** ¿`id` cuelga (a cualquier profundidad) de `ancestro`? */
function esDescendiente(id, ancestro) {
  let node = state.pagesById.get(id);
  const seen = new Set();
  while (node?.parent_id && !seen.has(node.id)) {
    if (node.parent_id === ancestro) return true;
    seen.add(node.id);
    node = state.pagesById.get(node.parent_id);
  }
  return false;
}

/** Las páginas que pueden hacer de carpeta: notas (no bases ni filas), sin la
 * que se mueve, sin sus descendientes —el backend rechaza el ciclo— y sin las
 * otras notas de la bandeja, que tampoco están clasificadas todavía. Por
 * título, que es como se busca en la lista. */
function carpetasPosibles(pageId) {
  return [...state.pagesById.values()]
    .filter(
      (n) =>
        n.type === "note" &&
        n.id !== pageId &&
        !state.inboxIds.has(n.id) &&
        !esDescendiente(n.id, pageId),
    )
    .sort((a, b) => (a.title || "").localeCompare(b.title || "", "es"));
}

/** Nombre comparable: sin espacios de sobra, sin mayúsculas y en NFC, para
 * que «Hitos», «hitos » y «HITOS» cuenten como el mismo nombre. */
function nombreNormalizado(titulo) {
  return (titulo || "")
    .normalize("NFC")
    .trim()
    .replace(/\s+/g, " ")
    .toLocaleLowerCase("es");
}

/** Dónde vive la página en el árbol («Páginas / Proyectos / Hitos»): es lo
 * que distingue dos páginas con el mismo título. La API no da la ruta, así
 * que se sube por los `parent_id`. Una captura no está en «Páginas» —vive en
 * `Bandeja/`— y no se le inventa una ruta que no tiene. */
function rutaDePagina(id) {
  const nodo = state.pagesById.get(id);
  if (!nodo) return "Páginas";
  if (state.inboxIds.has(id)) return ["Bandeja", nodo.title || "Sin título"].join(" / ");
  const nombres = [];
  const visto = new Set();
  let node = nodo;
  while (node && !visto.has(node.id)) {
    visto.add(node.id);
    nombres.unshift(node.title || "Sin título");
    node = node.parent_id ? state.pagesById.get(node.parent_id) : null;
  }
  return ["Páginas", ...nombres].join(" / ");
}

/** Los títulos que enlaza un cuerpo, normalizados (sin alias: el `|` de
 * `[[Título|Alias]]` no es parte del título que resuelve). */
function titulosEnlazados(cuerpo) {
  const vistos = new Set();
  for (const m of (cuerpo || "").matchAll(/\[\[([^\]|]+)(?:\|[^\]]+)?\]\]/g)) {
    vistos.add(nombreNormalizado(m[1]));
  }
  return vistos;
}

/** El cuerpo con `[[titulo]]` añadido al final, separado del texto por una
 * línea en blanco; `null` si el título ya estaba enlazado (no se duplica).
 * Los títulos del vault no llevan `[`, `]` ni `|` —`names.sanitize` los cambia
 * por paréntesis o espacio—, así que el enlace no puede salir partido. */
function cuerpoConEnlace(cuerpo, titulo) {
  const actual = cuerpo || "";
  if (titulosEnlazados(actual).has(nombreNormalizado(titulo))) return null;
  const texto = actual.replace(/\s+$/, "");
  return texto ? `${texto}\n\n[[${titulo}]]\n` : `[[${titulo}]]\n`;
}

/** Las notas que ocupan un nombre en la raíz de `Notas/`, que es donde nace
 * una página nueva (`POST /pages` sin padre). Son las únicas con las que
 * colisiona ese nombre: las de una subcarpeta viven en la suya, y las
 * capturas de la bandeja están en `Bandeja/` (no son páginas del wiki y no
 * hacen de carpeta). */
function notasEnLaRaiz() {
  return [...state.pagesById.values()].filter(
    (n) => n.type === "note" && !n.parent_id && !state.inboxIds.has(n.id),
  );
}

/** Las notas de la raíz que ya se llaman así, con su ruta. La que se está
 * procesando no cuenta. */
function notasConEseNombre(titulo, pageId) {
  const buscado = nombreNormalizado(titulo);
  if (!buscado) return [];
  return notasEnLaRaiz()
    .filter((n) => n.id !== pageId && nombreNormalizado(n.title) === buscado)
    .map((n) => ({ nodo: n, ruta: rutaDePagina(n.id) }))
    .sort((a, b) => a.ruta.localeCompare(b.ruta, "es"));
}

/** Las notas de la raíz indexadas por nombre comparable: el estado de antes
 * de crear, contra el que se comprueba el título que guarde el servidor. */
function indiceDeLaRaiz(pageId) {
  const mapa = new Map();
  for (const n of notasEnLaRaiz()) {
    if (n.id === pageId) continue;
    mapa.set(nombreNormalizado(n.title), { nodo: n, ruta: rutaDePagina(n.id) });
  }
  return mapa;
}

/** ¿El servidor ha guardado el nombre como otro que ya existía?
 *
 * El saneado del backend no se copia aquí (`< > : " / \ | ? *`, corchetes,
 * puntos y espacios finales…): manda lo que ha quedado en el fichero. Si el
 * título que vuelve no es el tecleado pero coincide con una entrada que ya
 * estaba —o con la base de un « (2)» que el backend pone al chocar—, es que
 * el nombre estaba ocupado.
 *
 * @param {string} devuelto título final que ha guardado el servidor
 * @param {string} tecleado título que escribió el usuario
 * @param {Map<string,{nodo:object,ruta:string}>} antes entradas de la carpeta antes de escribir
 * @returns {{nodo:object,ruta:string}|null} la entrada que ocupa el nombre, o `null`
 */
function ocupadaPorElServidor(devuelto, tecleado, antes) {
  if (!devuelto) return null;
  if (nombreNormalizado(devuelto) === nombreNormalizado(tecleado)) return null;
  const talCual = antes.get(nombreNormalizado(devuelto));
  if (talCual) return talCual;
  const sufijo = /^(.*?) \((\d+)\)$/u.exec(devuelto);
  if (sufijo && Number(sufijo[2]) >= 2) return antes.get(nombreNormalizado(sufijo[1])) || null;
  return null;
}

/** El aviso de «ya existe» de Procesar, para no escribirlo dos veces. */
function avisoDeCoincidencia({ nodo, ruta }) {
  return `Ya existe «${nodo.title}» (${ruta}): no se ha creado nada. Usa esa página o marca «crear una nueva igualmente».`;
}

/** Las coincidencias que toca enseñar: las del nombre tecleado o, si el
 * servidor acaba de toparse con una, esa (lo tecleado puede no ser el nombre
 * que chocó: «Proyectos.» se guarda como «Proyectos»). */
let coincidenciasDelServidor = null;
function coincidenciasActuales() {
  const nueva = procesarNew.value.trim();
  // Lo del servidor vale mientras se siga creando ese nombre: si el campo
  // está vacío no se crea nada nuevo y no hay colisión que enseñar.
  if (coincidenciasDelServidor?.length && nueva) return coincidenciasDelServidor;
  if (!nueva || !procesarPage) return [];
  return notasConEseNombre(nueva, procesarPage.id);
}

/** Pinta (o esconde) el bloque de «ya existe» del diálogo Procesar. Todas las
 * que salen ocupan un nombre en la raíz de `Notas/`, así que todas pueden
 * hacer de carpeta: ni botones deshabilitados ni rutas inventadas. */
function paintCoincidencias() {
  const caja = document.getElementById("proc-existing");
  const lista = document.getElementById("proc-existing-list");
  const cabecera = document.getElementById("proc-existing-note");
  if (!caja || !lista || !cabecera) return;
  const iguales = coincidenciasActuales();
  caja.hidden = iguales.length === 0;
  lista.textContent = "";
  if (!iguales.length) {
    // Sin coincidencias no hay nada que forzar: si se marcó para otro
    // nombre, la casilla no puede quedarse marcada.
    const forzar = document.getElementById("proc-force");
    if (forzar) forzar.checked = false;
    return;
  }
  cabecera.textContent =
    iguales.length === 1
      ? "Ya existe una página con ese nombre. Puedes usar esa o crear otra igualmente."
      : `Ya hay ${iguales.length} páginas con ese nombre. Elige cuál usar, o crea otra igualmente.`;
  iguales.forEach(({ nodo, ruta }) => {
    const fila = document.createElement("div");
    fila.className = "proc-existing-item";
    const rutaEl = document.createElement("span");
    rutaEl.className = "proc-existing-path";
    rutaEl.textContent = ruta;
    const usar = document.createElement("button");
    usar.type = "button";
    usar.className = "btn";
    usar.textContent = "Usar esta";
    usar.setAttribute("aria-label", `Usar «${nodo.title}» (${ruta})`);
    usar.addEventListener("click", () => {
      // Elegir la existente es lo mismo que elegirla en la lista de arriba:
      // la nota cuelga de ella y no se crea nada.
      coincidenciasDelServidor = null;
      procesarSelect.value = nodo.id;
      procesarNew.value = "";
      paintCoincidencias();
      setFormNote(procesarNote, `La nota pasará a colgar de «${nodo.title}» (${ruta}).`, "ok");
    });
    fila.append(rutaEl, usar);
    lista.appendChild(fila);
  });
}

/** Deja el diálogo como recién abierto: sin coincidencias y sin permiso
 * para duplicar. */
function reiniciarCoincidencias() {
  coincidenciasDelServidor = null;
  const caja = document.getElementById("proc-existing");
  if (caja) caja.hidden = true;
  const forzar = document.getElementById("proc-force");
  if (forzar) forzar.checked = false;
}

function openProcesarDialog(page) {
  if (!procesarDialog) return;
  procesarPage = page;
  procesarTitle.textContent = page.title || "Sin título";
  procesarSelect.textContent = "";
  const empezar = document.createElement("option");
  empezar.value = "";
  empezar.textContent = "— elige una página —";
  procesarSelect.appendChild(empezar);
  carpetasPosibles(page.id).forEach((node) => {
    const option = document.createElement("option");
    option.value = node.id;
    option.textContent = node.title || "Sin título";
    procesarSelect.appendChild(option);
  });
  procesarNew.value = "";
  reiniciarCoincidencias();
  setFormNote(procesarNote, PROCESAR_NOTE);
  const boton = document.getElementById("btn-do-procesar");
  if (boton) boton.disabled = false;
  procesarDialog.showModal();
  procesarSelect.focus();
}

async function procesarNota() {
  const page = procesarPage;
  if (!page) return;
  const nueva = procesarNew.value.trim();
  let parentId = procesarSelect.value || null;
  if (!nueva && !parentId) {
    return setFormNote(
      procesarNote,
      "Elige una página o escribe el nombre de una nueva.",
      "error",
    );
  }

  // Un nombre que ya ocupa una nota de la raíz de `Notas/` —donde nace la
  // página nueva— no crea otra a la callada: el backend le pondría un sufijo
  // («Hitos (2)») y el árbol acabaría con dos «Hitos» (M1). O se usa la que
  // hay, o se marca la casilla. Las capturas de la bandeja y las notas de
  // subcarpetas no comparten carpeta con la página nueva y no bloquean.
  const forzar = document.getElementById("proc-force");
  const permitirOtra = Boolean(forzar?.checked);
  // La coincidencia del servidor es de un nombre que se intentó crear; con el
  // campo vacío (se ha elegido una página en «Mover a») no hay nada que
  // choque y no puede bloquear el movimiento.
  const iguales = nueva
    ? coincidenciasDelServidor?.length
      ? coincidenciasDelServidor
      : notasConEseNombre(nueva, page.id)
    : [];
  if (iguales.length && !permitirOtra) {
    paintCoincidencias();
    return setFormNote(
      procesarNote,
      iguales.length === 1
        ? avisoDeCoincidencia(iguales[0])
        : `Ya hay ${iguales.length} páginas llamadas «${nueva}»: usa una de ellas o marca «crear una nueva igualmente».`,
      "error",
    );
  }

  const boton = document.getElementById("btn-do-procesar");
  if (boton) boton.disabled = true;
  setFormNote(procesarNote, "Moviendo…");
  try {
    if (nueva) {
      // Estado de la raíz antes de crear: contra él se comprueba el título
      // que el servidor guarde de verdad (su saneado no se copia aquí).
      const antes = permitirOtra ? new Map() : indiceDeLaRaiz(page.id);
      const creada = await api("/pages", {
        method: "POST",
        body: JSON.stringify({ title: nueva }),
      });
      const ocupada = permitirOtra ? null : ocupadaPorElServidor(creada.title, nueva, antes);
      if (ocupada) {
        // El nombre saneado sí estaba ocupado y el servidor ha creado
        // «Hitos (2)»: se deshace la página vacía antes de mover la nota.
        let deshecho = true;
        try {
          await api(`/pages/${creada.id}`, { method: "DELETE" });
        } catch (_) {
          deshecho = false;
        }
        if (!deshecho) {
          return setFormNote(
            procesarNote,
            `El servidor ha creado «${creada.title}» y no se ha podido deshacer. Revísala en el árbol antes de seguir.`,
            "error",
          );
        }
        coincidenciasDelServidor = [ocupada];
        await loadTree();
        paintCoincidencias();
        return setFormNote(procesarNote, avisoDeCoincidencia(ocupada), "error");
      }
      parentId = creada.id;
    }
    // Al procesar, la página destino tiene que mencionar la captura: sin
    // `[[Título]]` no hay arista en el grafo ni backlink desde la captura a su
    // contenedor (decisión del dueño, 9 nov).
    //
    // El enlace se escribe ANTES de mover, y no después: el fallo que importa
    // —el PATCH del cuerpo— deja entonces la captura donde estaba, sin nada a
    // medias; y si el que falla es el movimiento, el cuerpo del destino se
    // restaura con el texto que se acaba de leer. Al revés no habría vuelta:
    // una captura no cuelga de ninguna página (vive en `Bandeja/`, sin
    // `parent_id`), así que no existe un PATCH que la devuelva a la bandeja y
    // deshacer el movimiento sería imposible.
    const titulo = page.title || "Sin título";
    const destino = await api(`/pages/${parentId}`);
    // Una base de datos no enlaza: sus filas no son páginas del árbol y una
    // nota tampoco cuelga de ella (el vault lo rechaza). Ahí solo se mueve.
    const cuerpoAntes = destino.body_markdown || "";
    const conEnlace =
      destino.type === "database" ? null : cuerpoConEnlace(cuerpoAntes, titulo);
    if (conEnlace !== null) {
      await api(`/pages/${parentId}`, {
        method: "PATCH",
        body: JSON.stringify({ body_markdown: conEnlace }),
      });
    }

    let movida;
    try {
      movida = await api(`/pages/${page.id}`, {
        method: "PATCH",
        body: JSON.stringify({ parent_id: parentId }),
      });
    } catch (e) {
      // No se ha movido nada: se retira el enlace para no dejar la página
      // destino mencionando una captura que sigue en la bandeja.
      if (conEnlace !== null) {
        try {
          await api(`/pages/${parentId}`, {
            method: "PATCH",
            body: JSON.stringify({ body_markdown: cuerpoAntes }),
          });
        } catch (_) {
          setFormNote(
            procesarNote,
            "No se ha podido procesar la captura y el enlace añadido a la página destino se ha quedado ahí. Revísala antes de reintentar.",
            "error",
          );
          return;
        }
      }
      throw e;
    }

    // Si el vault resolvió una colisión de nombre al moverla («Ideas» →
    // «Ideas (2)»), el enlace vale el título REAL que devuelve el servidor: el
    // que se escribió antes ya no resuelve. El cuerpo se rehace desde el texto
    // original, así que no se duplica nada.
    if (movida.title !== page.title) {
      const conTituloReal = cuerpoConEnlace(cuerpoAntes, movida.title);
      if (conTituloReal !== null) {
        try {
          await api(`/pages/${parentId}`, {
            method: "PATCH",
            body: JSON.stringify({ body_markdown: conTituloReal }),
          });
        } catch (_) {
          setFormNote(
            procesarNote,
            `La captura se ha movido como «${movida.title}», pero no se ha podido corregir el enlace de la página destino, que sigue apuntando a «${titulo}». Corrígelo a mano.`,
            "error",
          );
          return;
        }
      }
    }

    procesarDialog.close();
    // El árbol refrescado (incluida la bandeja, que pierde esta nota) y
    // después la nota abierta, ya en su sitio.
    await loadTree();
    goTo("note", page.id);
  } catch (e) {
    if (e?.status === 401) return;
    setFormNote(procesarNote, describeError(e), "error");
  } finally {
    if (boton) boton.disabled = false;
  }
}

// --------------------------------------------------- diálogo: tokens (P13)

const tokensNote = document.getElementById("tokens-note");
const tokensList = document.getElementById("tokens-list");
const newTokenForm = document.getElementById("form-new-token");
const newTokenName = document.getElementById("nt-name");
const newTokenNote = document.getElementById("nt-note");
const NEW_TOKEN_NOTE = newTokenNote?.textContent || "";
const tokenReveal = document.getElementById("token-reveal");
const TOKENS_NOTE = tokensNote?.textContent || "";

/** Fecha legible de un `last_used_at`, o «nunca usado». */
function formatWhen(iso) {
  if (!iso) return "nunca usado";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return iso;
  return when.toLocaleString("es-ES", { dateStyle: "short", timeStyle: "short" });
}

function renderTokens(tokens) {
  if (!tokensList) return;
  tokensList.textContent = "";
  if (!tokens.length) {
    const vacio = document.createElement("div");
    vacio.className = "search-empty";
    vacio.textContent = "Todavía no hay ningún token de captura.";
    tokensList.appendChild(vacio);
    return;
  }
  tokens.forEach((token) => {
    const row = document.createElement("div");
    row.className = "token-row";

    const name = document.createElement("span");
    name.className = "token-name";
    name.textContent = token.name || "Sin nombre";

    const meta = document.createElement("span");
    meta.className = "token-meta";
    meta.textContent = `Último uso: ${formatWhen(token.last_used_at)}`;
    meta.title = `Creado el ${formatWhen(token.created_at)}`;

    const revoke = document.createElement("button");
    revoke.type = "button";
    revoke.className = "btn token-revoke";
    revoke.textContent = "Revocar";
    revoke.setAttribute("aria-label", `Revocar el token «${token.name || "sin nombre"}»`);
    revoke.addEventListener("click", () => revocarToken(token, revoke));

    row.append(name, meta, revoke);
    tokensList.appendChild(row);
  });
}

async function loadTokens() {
  try {
    renderTokens(await api("/auth/tokens"));
  } catch (e) {
    if (e?.status === 401) return;
    setFormNote(tokensNote, describeError(e), "error");
  }
}

/** Los tokens de captura, al entrar en «Usuario»: sin el secreto de antes. */
async function cargarTokensDeAjustes() {
  if (!tokensList) return;
  if (tokenReveal) tokenReveal.hidden = true;
  if (newTokenForm) newTokenForm.reset();
  setFormNote(tokensNote, TOKENS_NOTE);
  setFormNote(newTokenNote, NEW_TOKEN_NOTE);
  await loadTokens();
}

async function revocarToken(token, boton) {
  boton.disabled = true;
  try {
    await api(`/auth/tokens/${token.id}`, { method: "DELETE" });
    // El secreto que había en pantalla ya no vale para nada: fuera.
    if (tokenReveal) {
      tokenReveal.hidden = true;
      const input = document.getElementById("token-value");
      if (input) input.value = "";
    }
    setFormNote(
      tokensNote,
      `Token «${token.name || "sin nombre"}» revocado: el atajo que lo usaba deja de funcionar.`,
      "ok",
    );
    await loadTokens();
  } catch (e) {
    if (e?.status === 401) return;
    setFormNote(tokensNote, describeError(e), "error");
    boton.disabled = false;
  }
}

newTokenForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const boton = document.getElementById("btn-create-token");
  if (boton) boton.disabled = true;
  setFormNote(newTokenNote, "Creando…");
  try {
    const creado = await api("/auth/tokens", {
      method: "POST",
      body: JSON.stringify({ name: newTokenName.value.trim() || null }),
    });
    // El secreto se enseña UNA vez: en la base solo queda su SHA-256, así que
    // esta pantalla es la única ocasión de copiarlo.
    if (tokenReveal) {
      const input = document.getElementById("token-value");
      if (input) input.value = creado.token;
      tokenReveal.hidden = false;
      input?.select();
    }
    newTokenForm.reset();
    setFormNote(newTokenNote, NEW_TOKEN_NOTE);
    await loadTokens();
  } catch (e) {
    if (e?.status === 401) return;
    setFormNote(newTokenNote, describeError(e), "error");
  } finally {
    if (boton) boton.disabled = false;
  }
});

document.getElementById("btn-copy-token")?.addEventListener("click", async (event) => {
  const input = document.getElementById("token-value");
  const boton = event.currentTarget;
  if (!input) return;
  try {
    await navigator.clipboard.writeText(input.value);
  } catch {
    // Sin permiso de portapapeles (o sin contexto seguro): se selecciona para
    // que un ⌘C/Ctrl+C a mano siga sirviendo.
    input.select();
    document.execCommand?.("copy");
  }
  boton.textContent = "Copiado";
  setTimeout(() => {
    boton.textContent = "Copiar";
  }, 1500);
});

// ------------------------------------------------------------------ ajustes
// Una ventana con apartados (como en Notion): «Aplicación» (tema), «Usuario»
// (nombre visible, contraseña y tokens de captura) y, solo para un admin,
// «Administración» (usuarios). Se abre desde el menú del usuario, abajo en la
// barra lateral. Cada apartado se prepara al entrar en él: los formularios
// limpios y las listas pedidas otra vez al servidor.

const settingsDialog = document.getElementById("dialog-settings");
const profileForm = document.getElementById("form-profile");
const profileNote = document.getElementById("profile-note");
const PROFILE_NOTE_HTML = profileNote?.innerHTML || "";

// ------------------------------------------- Ajustes > Información
// La versión que corre (la pone el CI en la imagen y se actualiza sola en cada
// despliegue), el estado de la API y la base y, para los administradores, las
// cifras de la instalación. Todo sale de `GET /about`; mientras el apartado está
// abierto se refresca cada 15 s. Si la versión del servidor ya no es la que
// cargó esta pestaña, se ofrece recargar.

let versionAlCargar = null;
let infoTimer = null;

function pararInformacion() {
  if (infoTimer) clearInterval(infoTimer);
  infoTimer = null;
}

async function cargarInformacion() {
  pararInformacion();
  await pintarInformacion();
  infoTimer = setInterval(() => {
    if (!settingsDialog?.open) return pararInformacion();
    pintarInformacion();
  }, 15000);
}

const fmtNum = (n) => (n ?? 0).toLocaleString("es-ES");

function fmtBytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
  return `${(b / 1024 ** i).toLocaleString("es-ES", { maximumFractionDigits: i ? 1 : 0 })} ${u[i]}`;
}

function fmtDuracion(s) {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${m} min`;
  return `${m} min`;
}

function fmtFecha(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString("es-ES", { dateStyle: "medium", timeStyle: "short" });
}

function infoEl(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function infoGroup(titulo, ...hijos) {
  const g = infoEl("div", "settings-group");
  g.append(infoEl("h5", null, titulo), ...hijos);
  return g;
}

function infoCard(label, value, note) {
  const c = infoEl("div", "info-card");
  c.append(infoEl("div", "info-card-label", label), infoEl("div", "info-card-value", value));
  if (note) c.append(infoEl("div", "info-card-note", note));
  return c;
}

function infoRows(pares) {
  const dl = infoEl("dl", "info-rows");
  for (const [k, v] of pares) {
    const dd = infoEl("dd");
    if (v instanceof Node) dd.append(v);
    else dd.textContent = v;
    dl.append(infoEl("dt", null, k), dd);
  }
  return dl;
}

function infoEstado(ok, texto) {
  const s = infoEl("span");
  const dot = infoEl("span", "info-dot" + (ok ? "" : " is-bad"));
  s.append(dot, texto);
  return s;
}

const COLORES_TIPO = { note: "var(--accent)", database: "#f59e0b", database_row: "#10b981" };

function grafMeses(meses) {
  const caja = infoEl("div");
  const max = Math.max(1, ...meses.map((m) => m.note + m.database + m.database_row));
  const fila = infoEl("div", "info-months");
  fila.setAttribute("role", "img");
  fila.setAttribute(
    "aria-label",
    meses.map((m) => `${m.month}: ${m.note + m.database + m.database_row}`).join(", "),
  );
  for (const m of meses) {
    const col = infoEl("div", "info-month");
    const total = m.note + m.database + m.database_row;
    const barra = infoEl("div", "info-month-bar");
    barra.style.height = `${Math.max(1, Math.round((total / max) * 100))}%`;
    barra.title = `${m.month}: ${total} (${m.note} notas, ${m.database} bases, ${m.database_row} filas)`;
    for (const tipo of ["note", "database", "database_row"]) {
      if (!m[tipo]) continue;
      const tramo = infoEl("span");
      tramo.style.flex = `${m[tipo]} 0 0`;
      tramo.style.background = COLORES_TIPO[tipo];
      barra.append(tramo);
    }
    const [y, mm] = m.month.split("-");
    const nombre = new Date(Number(y), Number(mm) - 1, 1).toLocaleString("es-ES", { month: "short" });
    col.append(barra, infoEl("div", "info-month-label", nombre));
    fila.append(col);
  }
  const leyenda = infoEl("div", "info-legend");
  for (const [tipo, texto] of [["note", "Notas"], ["database", "Bases"], ["database_row", "Filas"]]) {
    const i = infoEl("span");
    const muestra = infoEl("i");
    muestra.style.background = COLORES_TIPO[tipo];
    i.append(muestra, texto);
    leyenda.append(i);
  }
  caja.append(fila, leyenda);
  return caja;
}

async function pintarInformacion() {
  const caja = document.getElementById("info-content");
  if (!caja) return;
  let info;
  try {
    info = await api("/about");
  } catch (e) {
    caja.replaceChildren(
      infoGroup("Estado", infoRows([["API", infoEstado(false, `Sin respuesta (${describeError(e)})`)]])),
    );
    return;
  }
  const v = info.version || {};
  if (versionAlCargar === null) versionAlCargar = v.revision || v.version;
  const partes = [];

  // Versión
  const cab = infoEl("div", "info-version");
  cab.append(infoEl("span", "info-version-name", v.version === "desarrollo" ? "Desarrollo" : v.version));
  if (v.revision_short) {
    const rev = infoEl("span", "info-version-rev", v.revision_short);
    rev.title = v.revision;
    cab.append(rev);
  }
  const grupoVersion = infoGroup(
    "Versión",
    cab,
    infoRows([
      ["Publicada", fmtFecha(v.built_at)],
      ["Esquema de la base", `v${v.schema_version}`],
      ["Python · SQLite", `${v.python} · ${v.sqlite}`],
    ]),
  );
  const actual = v.revision || v.version;
  if (versionAlCargar && actual && actual !== versionAlCargar) {
    const aviso = infoEl("div", "info-update");
    const btn = infoEl("button", "btn", "Recargar");
    btn.type = "button";
    btn.addEventListener("click", () => location.reload());
    aviso.append(infoEl("span", null, "Hay una versión nueva desplegada. Recarga para usarla."), btn);
    grupoVersion.append(aviso);
  }
  partes.push(grupoVersion);

  // Estado
  const db = info.db || {};
  partes.push(
    infoGroup(
      "Estado",
      infoRows([
        ["API", infoEstado(info.api?.ok, "Funcionando")],
        ["Base de datos", infoEstado(db.ok, db.ok ? `Funcionando · ${db.latency_ms} ms · ${String(db.journal_mode).toUpperCase()}` : db.error || "Con errores")],
        ["Arranque", infoEstado(info.server?.startup === "ok", info.server?.startup === "ok" ? "Índice listo" : info.server?.startup)],
        ["En marcha desde", `${fmtFecha(info.server?.started_at)} (${fmtDuracion(info.server?.uptime_s || 0)})`],
      ]),
    ),
  );

  const a = info.admin;
  if (a) {
    const c = a.counts;
    const grid = infoEl("div", "info-grid");
    grid.append(
      infoCard("Notas", fmtNum(c.notes)),
      infoCard("Bases de datos", fmtNum(c.databases), `${fmtNum(c.views)} vistas`),
      infoCard("Filas", fmtNum(c.rows)),
      infoCard("Enlaces", fmtNum(c.links)),
      infoCard("Usuarios", fmtNum(c.users), `${fmtNum(c.admins)} admin.`),
      infoCard("Sesiones activas", fmtNum(c.sessions_active)),
      infoCard("En la papelera", fmtNum(c.trash)),
    );
    partes.push(infoGroup("Contenido", grid));

    const act = a.activity;
    const gridAct = infoEl("div", "info-grid");
    gridAct.append(
      infoCard("Creadas (7 días)", fmtNum(act.created_7d)),
      infoCard("Creadas (30 días)", fmtNum(act.created_30d)),
      infoCard("Editadas (24 h)", fmtNum(act.edited_24h)),
      infoCard("Editadas (7 días)", fmtNum(act.edited_7d)),
    );
    partes.push(infoGroup("Actividad", gridAct));
    if (a.by_month?.length) partes.push(infoGroup("Altas por mes", grafMeses(a.by_month)));

    if (a.top_creators?.length) {
      const ul = infoEl("ul", "info-list");
      for (const t of a.top_creators) {
        const li = infoEl("li");
        li.append(infoEl("span", null, t.name), infoEl("span", null, `${fmtNum(t.count)} páginas`));
        ul.append(li);
      }
      partes.push(infoGroup("Quién crea más", ul));
    }

    const s = a.storage;
    const co = a.collab;
    partes.push(
      infoGroup(
        "Almacenamiento y uso",
        infoRows([
          ["Base de datos", fmtBytes(s.index_bytes)],
          ["Texto de las notas", fmtBytes(s.documents_bytes)],
          ["Copia en disco (.md)", s.projection ? "Activada" : "Desactivada (se exporta a petición)"],
          ["Páginas de la raíz", `${fmtNum(a.sharing.shared)} compartidas · ${fmtNum(a.sharing.private)} privadas`],
          ["Edición en vivo", `${fmtNum(co.rooms)} notas abiertas · ${fmtNum(co.peers)} personas`],
          ["Guardado en vivo", co.persist_ms_p50 == null ? "—" : `${co.persist_ms_p50} ms (p95 ${co.persist_ms_p95} ms)`],
          ["Usuarios desde", fmtFecha(a.users_since)],
        ]),
      ),
    );
  }
  caja.replaceChildren(...partes);
}

function mostrarApartadoDeAjustes(nombre) {
  if (!settingsDialog) return;
  const pestana = settingsDialog.querySelector(`.settings-tab[data-section="${nombre}"]`);
  // Un apartado que no toca (Administración sin ser admin) no se abre.
  if (!pestana || pestana.hidden) nombre = "info";
  settingsDialog.querySelectorAll(".settings-tab").forEach((tab) => {
    const activa = tab.dataset.section === nombre;
    tab.classList.toggle("active", activa);
    tab.setAttribute("aria-current", activa ? "page" : "false");
  });
  settingsDialog.querySelectorAll(".settings-section").forEach((sec) => {
    sec.hidden = sec.dataset.section !== nombre;
  });
  settingsDialog.querySelector(".settings-body")?.scrollTo?.(0, 0);
  if (nombre === "app") sincronizarTemaEnAjustes();
  if (nombre === "info") cargarInformacion();
  else pararInformacion();
  if (nombre === "user") {
    prepararPerfil();
    prepararContrasena();
    cargarTokensDeAjustes();
  }
  if (nombre === "admin") {
    cargarUsuariosDeAjustes();
    cargarCopias();
  }
}

function openSettings(apartado = "info") {
  if (!settingsDialog) return;
  if (!settingsDialog.open) settingsDialog.showModal();
  mostrarApartadoDeAjustes(apartado);
  settingsDialog.querySelector(".settings-tab.active")?.focus();
}

settingsDialog?.querySelectorAll(".settings-tab").forEach((tab) => {
  tab.addEventListener("click", () => mostrarApartadoDeAjustes(tab.dataset.section));
});
document.getElementById("btn-close-settings")?.addEventListener("click", () => {
  settingsDialog.close();
});
// Clic en el fondo (fuera de la ventana): se cierra, como cualquier panel.
settingsDialog?.addEventListener("click", (e) => {
  if (e.target === settingsDialog) settingsDialog.close();
});

/** Las opciones de tema, marcadas con el que hay puesto. */
function sincronizarTemaEnAjustes() {
  const actual = document.documentElement.dataset.theme === "light" ? "light" : "dark";
  document.querySelectorAll('input[name="settings-theme"]').forEach((input) => {
    input.checked = input.value === actual;
  });
}
document.querySelectorAll('input[name="settings-theme"]').forEach((input) => {
  input.addEventListener("change", () => {
    if (input.checked) applyTheme(input.value);
  });
});

function prepararPerfil() {
  if (!profileForm) return;
  const me = sessionUser.me;
  document.getElementById("profile-name").value = me?.display_name || "";
  if (profileNote) {
    profileNote.innerHTML = PROFILE_NOTE_HTML;
    profileNote.classList.remove("form-error", "form-ok");
  }
  const usuario = document.getElementById("profile-username");
  if (usuario) usuario.textContent = me ? `@${me.username}` : "tu usuario";
  document.getElementById("btn-save-profile").disabled = false;
  selectorDePerfil.fijar(me?.avatar);
  setFormNote(profileAvatarNote, PROFILE_AVATAR_NOTE);
}

// Tu imagen: se guarda al elegirla.
const profileAvatarNote = document.getElementById("profile-avatar-note");
const PROFILE_AVATAR_NOTE = profileAvatarNote?.textContent || "";
const selectorDePerfil = montarSelectorDeAvatar(
  document.getElementById("profile-avatar") || document.createElement("div"),
  null,
  async (avatar) => {
    if (avatar === sessionUser.me?.avatar) return;
    setFormNote(profileAvatarNote, "Guardando…");
    try {
      const me = await api("/auth/me", { method: "PATCH", body: JSON.stringify({ avatar }) });
      sessionUser.me = me;
      rememberUser(me);
      paintUserChip();
      setFormNote(profileAvatarNote, "Imagen guardada.", "ok");
    } catch (e) {
      if (e?.status === 401) return;
      selectorDePerfil.fijar(sessionUser.me?.avatar);
      setFormNote(profileAvatarNote, describeError(e), "error");
    }
  },
);

profileForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const nombre = document.getElementById("profile-name").value.trim();
  if (!nombre) {
    return setFormNote(profileNote, "El nombre visible no puede estar vacío.", "error");
  }
  if (nombre === sessionUser.me?.display_name) {
    return setFormNote(profileNote, "Ese ya es tu nombre visible.", "ok");
  }
  const guardar = document.getElementById("btn-save-profile");
  guardar.disabled = true;
  setFormNote(profileNote, "Guardando…");
  try {
    const me = await api("/auth/me", {
      method: "PATCH",
      body: JSON.stringify({ display_name: nombre }),
    });
    sessionUser.me = me;
    rememberUser(me);
    paintUserChip();
    state.repaintAuthor?.();
    setFormNote(profileNote, `Ahora te llamas «${me.display_name}».`, "ok");
  } catch (e) {
    if (e?.status === 401) return;
    setFormNote(profileNote, describeError(e), "error");
  } finally {
    guardar.disabled = false;
  }
});

document.getElementById("btn-cancel-procesar")?.addEventListener("click", () => {
  procesarDialog.close();
});

document.getElementById("btn-do-procesar")?.addEventListener("click", procesarNota);

initInbox();

// ------------------------------------------------- mis tareas (todas las bases)
//
// La pregunta «¿qué me toca hoy?», cruzando TODAS las bases: las filas de
// cualquier base cuya propiedad de tipo `person` («Responsable») te contenga.
// El cruce lo hace el servidor de una vez (`GET /my-tasks`: una consulta para
// los schemas y otra para las filas de las bases que tienen responsables), así
// que el panel no entra base por base ni pide cuarenta listas en serie.
//
// La sección de la barra lateral es un botón que abre la vista y solo existe
// si hay algo, como la bandeja: quien no usa responsables no ve una sección
// vacía. A su lado, un contador con lo que **ya se pasó** y lo que **se pasa
// hoy**, que es la única parte de la vista que hay que ver sin abrirla. La
// vista agrupa por fecha (vencidas / hoy / esta semana / resto) y cada fila se
// abre como página, que es el mecanismo que ya hay.
//
// **Qué está vencido lo dice el servidor.** La respuesta trae `today` (el día
// con el que comparó), `counts` (los dos números de la barra) y, por tarea, su
// bloque (`due`) y los días que le quedan (`days`). Aquí no se mira el reloj ni
// se repite la resta: con dos portátiles en husos distintos el contador y la
// lista tendrían dos «hoy» y se contradirían. Lo mismo hace la tabla con
// `overdue_keys` (ver `paintCell`).
//
// Al entrar, si hay algo vencido se avisa una vez por sesión con un aviso
// discreto y descartable (`avisarDeVencidas`): ni modal ni nada que bloquee, y
// nada fuera de la app —sin correo, sin push, sin nada en segundo plano—.
//
// Las filas **cerradas** no salen. Manda la regla de cada base (`closed:
// {property, values}` en su `.md`, la misma que cuenta las subtareas); una base
// sin regla no tiene estado y entra entera. Los valores que las bases declaran
// llegan en `closed_values` y la vista los dice en una nota: el criterio se lee,
// no se adivina.

const BLOQUES_DE_MIS_TAREAS = [
  { key: "vencidas", title: "Vencidas" },
  { key: "hoy", title: "Hoy" },
  { key: "semana", title: "Esta semana" },
  { key: "resto", title: "Resto" },
];

/** Refresco agrupado de «Mis tareas»: varios avisos seguidos (guardar tres
 * celdas, una importación) se convierten en una sola consulta. */
let myTasksRefreshTimer = null;
function scheduleMyTasksRefresh() {
  if (myTasksRefreshTimer !== null) return;
  myTasksRefreshTimer = setTimeout(() => {
    myTasksRefreshTimer = null;
    loadMyTasks();
  }, 200);
}

/** Pide «Mis tareas» y repinta la sección. La llama `loadTree()`, así que un
 * evento del vigilante (o un guardado) la refresca sola. */
async function loadMyTasks() {
  let datos;
  try {
    datos = await api("/my-tasks");
  } catch (e) {
    if (e?.status === 401) return; // ya se está yendo al login
    reportError("No se pudieron cargar tus tareas", e);
    return;
  }
  state.myTasks = datos;
  paintMyTasks();
  avisarDeVencidas(datos);
  // La vista abierta se repinta con lo que acaba de llegar: quitarse de una
  // tarea o marcarla «Hecho» tiene que verse sin recargar la página.
  if (state.current?.kind === "mytasks") {
    renderMisTareas(clearMain());
  }
}

/** La clave con la que se recuerda, **en esta sesión**, que el aviso de
 * vencidas ya salió. `sessionStorage` y no `localStorage`: cerrar la pestaña
 * vuelve a empezar, y mañana el aviso tiene que volver a decir la verdad de
 * mañana. */
const AVISO_VENCIDAS_KEY = "mdm_aviso_vencidas";

/** Aviso de «tienes N tareas vencidas» al entrar, una vez por sesión.
 *
 * Discreto y descartable (el aviso informativo de siempre): no es un modal ni
 * bloquea nada, y si se cierra no vuelve a salir hasta la próxima sesión. La
 * cuenta la trae el servidor (`counts.overdue`), así que lo que dice el aviso
 * es exactamente lo que cuenta la barra lateral. Sin nada vencido no se pinta.
 *
 * `sticky` porque no se va solo: un aviso que hay que poder leer con calma no
 * debería desaparecer a los seis segundos. La marca de «ya salió» se pone al
 * pintarlo —no al cerrarlo— para que un refresco del árbol (que llega cada vez
 * que cambia algo en el vault) no lo repita tres veces seguidas. */
function avisarDeVencidas(datos) {
  const vencidas = Number(datos?.counts?.overdue) || 0;
  if (vencidas <= 0) return;
  try {
    if (sessionStorage.getItem(AVISO_VENCIDAS_KEY)) return;
    sessionStorage.setItem(AVISO_VENCIDAS_KEY, "1");
  } catch {
    // Sin almacenamiento (modo privado estricto) el aviso sale igual: lo que
    // se pierde es que no se repita, no el aviso.
  }
  showNotice(
    vencidas === 1 ? "Tienes 1 tarea vencida" : `Tienes ${vencidas} tareas vencidas`,
    { sticky: true },
  );
}

function paintMyTasks() {
  const section = document.getElementById("mytasks-section");
  const label = document.getElementById("mytasks-label");
  const boton = document.getElementById("btn-mytasks");
  const badges = document.getElementById("mytasks-badges");
  if (!section || !label) return;

  const count = state.myTasks?.count || 0;
  const vencidas = Number(state.myTasks?.counts?.overdue) || 0;
  const paraHoy = Number(state.myTasks?.counts?.today) || 0;
  // Sin nada asignado la sección no existe (ni un «Mis tareas (0)» que no dice
  // nada): el mismo criterio que la bandeja.
  section.hidden = count === 0;
  label.textContent = "Mis tareas";
  // El total no va en la etiqueta: lo urgente es lo que se ve, y el total está
  // al abrir la vista. Va en el `title` y en el `aria-label`, para quien no ve
  // los números o pasa el ratón.
  const total = count === 1 ? "1 tarea abierta" : `${count} tareas abiertas`;
  if (badges) {
    badges.replaceChildren();
    const piezas = [];
    if (vencidas > 0) {
      const texto = vencidas === 1 ? "1 vencida" : `${vencidas} vencidas`;
      piezas.push(texto);
      badges.appendChild(
        badgeDeMisTareas(
          "is-overdue",
          texto,
          vencidas === 1
            ? "1 tarea vencida (fecha anterior a hoy)"
            : `${vencidas} tareas vencidas (fecha anterior a hoy)`,
        ),
      );
    }
    if (paraHoy > 0) {
      const texto = paraHoy === 1 ? "1 hoy" : `${paraHoy} hoy`;
      piezas.push(texto);
      badges.appendChild(
        badgeDeMisTareas(
          "is-today",
          texto,
          paraHoy === 1 ? "1 tarea vence hoy" : `${paraHoy} tareas vencen hoy`,
        ),
      );
    }
    badges.hidden = piezas.length === 0;
    if (boton) {
      boton.title = piezas.length
        ? `Mis tareas: ${total} · ${piezas.join(" · ")}`
        : `Mis tareas: ${total}`;
      boton.setAttribute(
        "aria-label",
        piezas.length ? `Mis tareas, ${piezas.join(" y ")}` : "Mis tareas",
      );
    }
  }
  if (boton) {
    boton.setAttribute("aria-current", state.current?.kind === "mytasks" ? "page" : "false");
  }
}

/** Una insignia del contador de la barra lateral. El texto visible es corto
 * («3 vencidas», «1 hoy») y el `title` dice lo mismo con todas las palabras. */
function badgeDeMisTareas(clase, texto, descripcion) {
  const badge = document.createElement("span");
  badge.className = `mytasks-badge ${clase}`;
  badge.textContent = texto;
  badge.title = descripcion;
  return badge;
}

/** «2026-11-16» -> `Date` local a medianoche, o `null` si no es una fecha.
 *
 * Solo para pintar el texto de la fecha (el `toLocaleDateString` de un día
 * lejano) y para ordenar dentro del bloque: qué bloque es cada tarea y cuántos
 * días le quedan lo dice el servidor (`due` / `days`), no esta resta. */
function fechaLocalDe(iso) {
  const partes = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || "").trim());
  if (!partes) return null;
  const fecha = new Date(Number(partes[1]), Number(partes[2]) - 1, Number(partes[3]));
  return Number.isNaN(fecha.getTime()) ? null : fecha;
}

/** El bloque de la vista para cada bloque del servidor (`due` en la respuesta).
 * El servidor dice `overdue`/`today`/`week`/`later` comparando contra su día;
 * aquí solo se traduce al nombre que se pinta, y `null` (sin fecha legible) cae
 * en el «Resto», que es lo que es: una tarea sin fecha no está ni vencida ni
 * para hoy. */
const BLOQUE_DEL_SERVIDOR = {
  overdue: "vencidas",
  today: "hoy",
  week: "semana",
  later: "resto",
};

/** Reparte las tareas en los cuatro bloques, ordenadas por fecha ascendente
 * (las que no tienen fecha, al final de «Resto»).
 *
 * El reparto NO vuelve a restar fechas: usa `due` (el bloque) y `days` (los
 * días) que calculó el servidor contra su propio día. Con un portátil en otro
 * huso, la lista y el contador de la barra lateral seguirían diciendo lo mismo,
 * porque los dos leen la misma resta; si el cliente la repitiera con su reloj,
 * un contador de «2 vencidas» podría abrir una lista de «Vencidas: 0». */
function bloquesDeMisTareas(tasks) {
  const bloques = new Map(BLOQUES_DE_MIS_TAREAS.map((bloque) => [bloque.key, []]));

  for (const tarea of tasks || []) {
    const key = BLOQUE_DEL_SERVIDOR[tarea.due] || "resto";
    const fecha = fechaLocalDe(tarea.date?.value);
    const dias = Number.isInteger(tarea.days) ? tarea.days : null;
    bloques.get(key).push({ tarea, fecha, dias });
  }

  // `sort` es estable: lo que empata (misma fecha) conserva el orden del
  // servidor, que es por base y título.
  for (const lista of bloques.values()) {
    lista.sort((a, b) => {
      if (a.fecha && b.fecha) return a.fecha - b.fecha;
      if (a.fecha) return -1;
      if (b.fecha) return 1;
      return 0;
    });
  }
  return bloques;
}

/** «hoy», «mañana», «hace 3 días»… y la fecha a partir de una semana. */
function etiquetaDeFecha({ fecha, dias }) {
  if (!fecha) return "";
  if (Math.abs(dias) <= 7) return formatDueLabel(dias);
  return fecha.toLocaleDateString("es-ES", { day: "numeric", month: "short", year: "numeric" });
}

/** La nota que explica el filtro, con los valores que declaran las bases.
 *
 * La lista la arma el servidor leyendo el `.md` de cada base: aquí no hay una
 * copia del criterio que pueda contradecirlo. Si ninguna declara estado, la
 * nota lo dice en vez de explicar una lista que no existe. */
function notaDeTareasAbiertas(closed) {
  const valores = (closed || []).filter(Boolean);
  if (!valores.length) {
    return "Solo tareas abiertas. Ninguna base declara qué cuenta como cerrada: no se esconde nada.";
  }
  return `Solo tareas abiertas. Cada base manda: no salen las filas cuyo estado sea ${valores.join(", ")}. Una base sin regla entra entera.`;
}

function itemDeMiTarea({ tarea, fecha, dias }, abrir) {
  const item = document.createElement("div");
  item.className = "dashboard-item";
  item.title = `Abrir «${tarea.title || "Sin título"}» de «${tarea.database_title}»`;

  const icono = document.createElement("span");
  icono.className = "dashboard-item-icon";
  paintPageIcon(icono, "note", tarea.icon);

  const titulo = document.createElement("span");
  titulo.className = "dashboard-item-title";
  titulo.textContent = tarea.title || "Sin título";

  // De qué base viene: la lista es mezclada y sin esto no se sabe qué se está
  // mirando ni dónde está la fila.
  const base = document.createElement("span");
  base.className = "my-task-base";
  base.textContent = tarea.database_title;

  item.append(icono, titulo, base);

  // El estado, solo si la base tiene una propiedad de selección que haga de
  // estado (lo decide el servidor, que es quien lee el schema).
  if (tarea.status?.value) {
    const estado = document.createElement("span");
    estado.className = "my-task-status";
    estado.textContent = tarea.status.value;
    estado.title = `${tarea.status.name}: ${tarea.status.value}`;
    item.append(estado);
  }

  const meta = document.createElement("span");
  // El tono de vencida lo dice el servidor (`due`), no se deduce de `dias`.
  meta.className = "dashboard-item-meta" + (tarea.due === "overdue" ? " overdue" : "");
  meta.textContent = etiquetaDeFecha({ fecha, dias });
  item.append(meta);

  item.addEventListener("click", () => abrir(item));
  makeKeyboardActivatable(item, "button", 0);
  return item;
}

/** Abre una tarea: es una fila, y una fila es una página más. */
async function abrirMiTarea(tarea) {
  if (!tarea?.row_id) return;
  await goTo("note", tarea.row_id);
}

function renderMisTareas(main) {
  const datos = state.myTasks || {};
  const wrap = document.createElement("div");
  wrap.className = "dashboard";

  const cabeza = document.createElement("div");
  cabeza.className = "my-tasks-head";
  const titulo = document.createElement("h1");
  titulo.className = "my-tasks-title";
  titulo.textContent = datos.count ? `Mis tareas (${datos.count})` : "Mis tareas";
  cabeza.appendChild(titulo);
  const nota = notaDeTareasAbiertas(datos.closed_values);
  if (nota) {
    const parrafo = document.createElement("p");
    parrafo.className = "my-tasks-note";
    parrafo.textContent = nota;
    cabeza.appendChild(parrafo);
  }
  wrap.appendChild(cabeza);

  if (!datos.count) {
    const vacio = document.createElement("div");
    vacio.className = "my-tasks-empty";
    vacio.textContent =
      "No tienes ninguna tarea asignada. Ponte como responsable en una fila de cualquier base y aparecerá aquí.";
    wrap.appendChild(vacio);
    main.appendChild(wrap);
    return;
  }

  const bloques = bloquesDeMisTareas(datos.tasks);
  for (const bloque of BLOQUES_DE_MIS_TAREAS) {
    const lista = bloques.get(bloque.key) || [];
    if (!lista.length) continue;
    const seccion = document.createElement("section");
    seccion.className = "dashboard-section";
    const head = document.createElement("div");
    head.className = "dashboard-section-head";
    head.textContent = `${bloque.title} (${lista.length})`;
    seccion.appendChild(head);
    for (const entrada of lista) {
      seccion.appendChild(
        itemDeMiTarea(entrada, (origen) => abrirMiTarea(entrada.tarea, origen)),
      );
    }
    wrap.appendChild(seccion);
  }

  main.appendChild(wrap);
}

async function showMisTareas() {
  state.current = { kind: "mytasks", id: null };
  markActive(null);
  const main = clearMain();
  updateNavButtons();
  paintMyTasks();
  renderMisTareas(main);
}

function initMyTasks() {
  const boton = document.getElementById("btn-mytasks");
  if (!boton) return;
  boton.addEventListener("click", () => goTo("mytasks"));
}

initMyTasks();

// ------------------------------------------------------------------ árbol

async function loadTree() {
  try {
    // La bandeja (P13) se refresca con el árbol: es el mismo momento en el que
    // puede haber cambiado (un `.md` nuevo, una captura desde un atajo). «Mis
    // tareas» también: asignar o quitar un responsable, o cerrar una fila,
    // llega por aquí y el contador de la barra tiene que decir la verdad.
    await loadInbox();
    await loadMyTasks();
    const tree = await api("/pages/tree");
    state.pagesById.clear();
    indexTree(tree);

    // Las notas de la bandeja no se repiten aquí: ya están en su sección. Una
    // que tenga hijas se queda en el árbol —esconderla se llevaría por delante
    // su subárbol— y entonces sale en los dos sitios, que es lo menos malo.
    //
    // Las bases de datos son páginas del árbol como cualquier otra (P17.6):
    // con su icono propio y su clic, que las abre en su vista completa. Antes
    // vivían en una sección aparte del panel y esa era su única entrada; ahora
    // una base sin ninguna nota que la incruste sigue estando aquí.
    const notes = tree.filter(
      (n) => !(state.inboxIds.has(n.id) && !n.children?.length),
    );

    const treeContainer = document.getElementById("tree-container");
    treeContainer.innerHTML = "";

    if (notes.length === 0) {
      treeContainer.innerHTML =
        '<div class="empty-hint empty-hint--side"><p class="empty-hint-title">Todavía no hay páginas.</p>' +
        '<p class="empty-hint-note">Pulsa «+» aquí arriba para escribir la primera nota.</p></div>';
    }
    notes.forEach((n) => treeContainer.appendChild(renderTreeNode(n)));

    if (state.current) markActive(state.current.id);
    applyPageSelectionClasses();
    updateNavButtons(); // "subir un nivel" depende de parent_id, que solo se sabe tras recargar el árbol
    // Los contenedores ya tienen role="tree" (index.html): se les aplica el
    // roving tabindex después de poblarlos.
    document.querySelectorAll('[role="tree"]').forEach(initTreeRovingTabIndex);
    // El árbol acaba de cambiar (arranque, crear, borrar, evento del
    // vigilante...): los destinos que ofrece el "[[" del editor son estos.
    syncLinkTargets();
    // El arranque lo necesita para saber si una dirección de la URL se puede
    // resolver (`abrirAlArrancar`): sin árbol no se puede distinguir «esa nota
    // no existe» de «la API todavía no contesta».
    return true;
  } catch (e) {
    // El árbol anterior (si lo había) se queda tal cual: solo se vacía
    // después de que la petición haya ido bien.
    reportError("No se pudo cargar el árbol de páginas", e);
    return false;
  }
}

function indexTree(nodes) {
  for (const n of nodes) {
    state.pagesById.set(n.id, n);
    if (n.children?.length) indexTree(n.children);
  }
}

function renderTreeNode(node, depth = 1) {
  const wrap = document.createElement("div");
  wrap.className = "tree-node";
  // El envoltorio no aporta nada al árbol de accesibilidad: con role="none"
  // sus hijos (treeitem y group) cuelgan directamente del role="tree".
  wrap.setAttribute("role", "none");

  const row = document.createElement("div");
  row.className = "tree-row";
  makeKeyboardActivatable(row, "treeitem", -1);
  row.setAttribute("aria-level", String(depth));
  if (node.children?.length) row.setAttribute("aria-expanded", "true");
  // El id de la `page` y el id de la fila `databases` que la describe son
  // dos UUID distintos -- para navegar hace falta el segundo. data-node-id
  // es siempre el id de página de verdad, el que se usa para selección
  // múltiple y borrado (con databases, DELETE va por database_id, no por
  // este -- ver bulkDeletePages).
  const navId = node.type === "database" ? node.database_id : node.id;
  row.dataset.id = navId;
  row.dataset.nodeId = node.id;
  if (!node.title) row.setAttribute("aria-label", "Sin título");

  const icon = document.createElement("span");
  icon.className = "type-icon";
  paintPageIcon(icon, node.type, node.icon);

  const label = document.createElement("span");
  label.className = "tree-label";
  label.textContent = node.title || "Sin título";

  row.append(icon, label);
  // 3.0, hito 3: candado en las páginas que nadie más ve (privadas).
  if (node.shared === false && node.type !== "database_row") {
    const lock = document.createElement("span");
    lock.className = "tree-lock";
    lock.title = "Privada: solo la ven sus propietarios";
    lock.setAttribute("aria-label", "Privada");
    lock.innerHTML = svgIcon("i-lock");
    row.append(lock);
  }

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
    childrenWrap.setAttribute("role", "group");
    node.children.forEach((c) => childrenWrap.appendChild(renderTreeNode(c, depth + 1)));
    wrap.appendChild(childrenWrap);
  }

  return wrap;
}

function markActive(id) {
  document.querySelectorAll(".tree-row").forEach((el) => {
    el.classList.toggle("active", el.dataset.id === id);
  });
  // El roving tabindex sigue a la fila activa también cuando markActive se
  // llama fuera de loadTree() (p.ej. al navegar a una nota).
  document.querySelectorAll('[role="tree"]').forEach(initTreeRovingTabIndex);
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
  if (currentWasDeleted) volverAlInicio();
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

/** Mientras llega una página: una línea fina bajo la cabecera (`.is-loading`
 * en el CSS, que espera 150 ms antes de enseñarla: una carga rápida no
 * parpadea). Se cuenta, porque dos navegaciones pueden solaparse. */
let cargasEnCurso = 0;
function marcarCarga(empieza) {
  cargasEnCurso = Math.max(0, cargasEnCurso + (empieza ? 1 : -1));
  document.body.classList.toggle("is-loading", cargasEnCurso > 0);
}

async function selectNote(id) {
  const previous = state.current;
  state.current = { kind: "note", id };
  markActive(id);
  marcarCarga(true);
  try {
    const [page, backlinks] = await Promise.all([api(`/pages/${id}`), api(`/pages/${id}/backlinks`)]);
    renderNote(page, backlinks);
    return true;
  } catch (e) {
    // renderNote() no llegó a ejecutarse, así que #main sigue mostrando la
    // página anterior: se devuelven state.current y el resaltado del árbol a
    // donde estaban, para que no apunten a algo que no se ha pintado.
    state.current = previous;
    markActive(previous ? previous.id : null);
    // Un enlace viejo (o a una fila que ya no ves): se dice, sin el error
    // técnico. 404 es también «no tienes permiso» (3.0, hito 3).
    if (e?.status === 404) showNotice("Esa página ya no existe (o no tienes acceso).");
    else reportError("No se pudo abrir la nota", e);
    return false;
  } finally {
    marcarCarga(false);
  }
}

const AUTOSAVE_DELAY_MS = 2000;
// Con la nota en una sala (bloque C), el título, la descripción y el icono se
// ven en vivo en las demás pestañas: se mandan casi al momento.
const LIVE_META_DELAY_MS = 300;
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
    makeKeyboardActivatable(item, "button", 0);
    list.appendChild(item);
  });
  outlineEl.open = wasOpen; // reconstruir la lista no debe cerrar el desplegable si ya estaba abierto
}

function createOutlinePanel(mountEl = document.body) {
  closeOutline();
  const details = document.createElement("details");
  details.className = "note-outline-dropdown";
  details.hidden = true; // hasta que renderOutline() confirme que hay encabezados

  const summary = document.createElement("summary");
  summary.innerHTML = `${svgIcon("i-list-tree")}Índice`;

  const list = document.createElement("div");
  list.className = "note-outline-list";

  details.append(summary, list);
  // El índice vive en la barra de la nota, junto a Exportar y Backlinks: con
  // position:fixed tapaba los dos a 1280 y a 1024 px (SPEC-B §5, hallazgo #2).
  mountEl.appendChild(details);
  outlineEl = details;
}

// ------------------------------------------- sugerencias de agentes (P10c)
// El parche se pinta DENTRO del editor (frontend/editor-src, setSuggestions):
// el texto viejo tachado y el propuesto al lado, con ✓ y ✗. Aquí se piden, se
// deciden y se conversa. El panel lateral es solo para lo que el editor no
// puede poner en su sitio -- las que ya no encajan en el texto y las ya
// decididas -- más el hilo de respuestas (el canal de vuelta al agente).

const SUGGESTION_STATUS_LABEL = {
  pending: "pendiente",
  accepted: "aceptada",
  rejected: "rechazada",
  orphaned: "desanclada",
};

let suggestionsPanelEl = null;

/** El panel es una columna de .layout, fuera de #main: al cambiar de página
 * hay que quitarlo a mano (si no, seguiría ahí, apuntando a la nota
 * anterior). */
function closeSuggestionsPanel() {
  if (suggestionsPanelEl) {
    suggestionsPanelEl.remove();
    suggestionsPanelEl = null;
  }
}

/** El menú contextual de un bloque (el asa): convertir, insertar, duplicar,
 * mover y eliminar. Es el MISMO en una nota y en una fila abierta como
 * página; lo único que cambia es qué hay que hacer después de editar el
 * documento, y eso llega en `alEditar` (guardar la nota, guardar la fila). */
function mostrarMenuDeBloque(payload, alEditar) {
  const n = payload.count;
  const header = payload.currentTypeLabel || `${n} bloques seleccionados`;
  const mover = [];
  // Mover es de UN bloque: con varios marcados no se ofrece (no está claro a
  // dónde va el grupo). En móvil es la vía del reordenado -- el arrastre con el
  // asa es cosa del ratón.
  if (n === 1) {
    if (payload.canMoveUp) {
      mover.push({
        label: "Mover arriba",
        onClick: () => {
          payload.moveUp();
          alEditar();
        },
      });
    }
    if (payload.canMoveDown) {
      mover.push({
        label: "Mover abajo",
        onClick: () => {
          payload.moveDown();
          alEditar();
        },
      });
    }
  }
  showContextMenu(payload.x, payload.y, [
    { label: header, items: [] },
    {
      label: "Convertir a",
      items: payload.types.map((t) => ({
        label: t.label,
        onClick: () => {
          payload.convertTo(t.type);
          alEditar();
        },
      })),
    },
    {
      items: [
        {
          label: "Insertar bloque arriba",
          onClick: () => {
            payload.insertAbove();
            alEditar();
          },
        },
        {
          label: `Duplicar ${n > 1 ? "bloques" : "bloque"}`,
          onClick: () => {
            payload.duplicateBlocks();
            alEditar();
          },
        },
        ...mover,
      ],
    },
    {
      items: [
        {
          label: `Eliminar ${n > 1 ? "bloques" : "bloque"}`,
          danger: true,
          onClick: () => {
            payload.deleteBlocks();
            alEditar();
          },
        },
      ],
    },
  ]);
}

/** Crea una nota HERMANA de la que se está mirando (mismo `parent_id`, o la
 * raíz si no tiene): es lo que pide un `[[Título nuevo]]` escrito en el
 * cuerpo. Compartida por la nota y la fila abierta como página -- la fila no
 * puede ser carpeta de nada, así que la nueva nota no cuelga de ella. */
async function crearNotaHermana(parentId, title) {
  try {
    const created = await api("/pages", {
      method: "POST",
      body: JSON.stringify({ title, parent_id: parentId ?? null, body_markdown: "" }),
    });
    await loadTree();
    return { id: created.id, title: created.title };
  } catch (e) {
    if (e?.status === 401) return null; // ya se está yendo al login
    reportError(`No se pudo crear la nota «${title}»`, e);
    return null; // el editor no inserta nada si no hay título
  }
}

/** Renombra una nota del vault desde el selector de `/page` del editor: el
 * nombre tiene que estar libre entre sus hermanas (el servidor desambigua en
 * silencio con un «(2)», y ese duplicado es justo lo que no se quiere).
 * Devuelve `{id, title}` con el nombre que quedó, o `null`.
 *
 * Lo comparten la nota (que además refresca su cabecera si se ha renombrado a
 * sí misma) y la fila abierta como página. */
async function renombrarPaginaDelVault(pageId, title) {
  const nombre = String(title || "").trim();
  const node = state.pagesById.get(pageId);
  if (!nombre || !node) return null;
  // Se compara la carpeta tal cual -- `""` y `null` son la raíz --, no con
  // childTitleTaken: esa compara `String(null)` y con la raíz nunca encontraba
  // nada.
  const carpeta = (n) => String(n.parent_id ?? "");
  for (const otra of state.pagesById.values()) {
    if (String(otra.id) === String(pageId)) continue;
    if (carpeta(otra) !== carpeta(node)) continue;
    if (!sameTitle(otra.title, nombre)) continue;
    showError(`Ya hay una página llamada «${otra.title}» en esa carpeta. Elige otro nombre.`);
    return null;
  }
  let updated;
  try {
    updated = await api(`/pages/${pageId}`, {
      method: "PATCH",
      body: JSON.stringify({ title: nombre }),
    });
  } catch (e) {
    if (e?.status === 401) return null;
    reportError(`No se pudo renombrar «${node.title}»`, e);
    return null;
  }
  await loadTree();
  // El vigilante verá el `.md` renombrado y lo contará por /events como una
  // edición de FUERA; es nuestra, así que se apunta para no enseñar el aviso
  // «esta nota cambió en disco» (que además bloquearía el guardado pendiente
  // del nombre que el editor acaba de quitar del cuerpo).
  state.ownRemoteEvents.set(String(pageId), Date.now());
  if (!sameTitle(updated.title, nombre)) {
    showError(
      `La página se llama ahora «${updated.title}»: «${nombre}» no estaba libre ` +
        "o no vale como nombre de fichero.",
    );
  }
  return { id: pageId, title: updated.title };
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
    } else if (s === "readonly") {
      // 3.0 (hitos 3 y 4): la nota la puedes ver pero no editar.
      saveIcon.innerHTML = svgIcon("i-lock");
      saveLabel.textContent = "Solo lectura";
    } else if (s === "offline") {
      // La sala se cayó y se reintenta: lo escrito sigue aquí y se manda al volver.
      saveIcon.textContent = "•";
      saveLabel.textContent = "Reconectando…";
    } else {
      saveIcon.innerHTML = svgIcon("i-check");
      saveLabel.textContent = "Guardado";
    }
  }
  setSaveState("saved");

  const backDetails = document.createElement("details");
  backDetails.className = "note-backlinks-dropdown";
  const backSummary = document.createElement("summary");
  const backList = document.createElement("div");
  backList.className = "backlinks-list";
  // P8: aparte, porque un cambio en OTRA nota puede cambiar los enlaces
  // entrantes de esta y entonces solo se repinta esto, sin tocar el editor.
  function paintBacklinks(fresh) {
    backSummary.innerHTML = `${svgIcon("i-chevron-right", "back-chevron-closed")}${svgIcon(
      "i-chevron-down",
      "back-chevron-open",
    )}Backlinks (${fresh.length})`;
    backList.innerHTML = "";
    if (fresh.length === 0) {
      // Una lista vacía no explica nada: el dueño no entendía los backlinks
      // («no termino de entender demasiado lo de backlinks»), así que cuando
      // no hay ninguno se dice qué son y cómo se crean, con el título de ESTA
      // nota dentro del ejemplo -- es el que hay que escribir en la otra.
      const vacio = document.createElement("div");
      vacio.className = "backlinks-empty";
      vacio.textContent = "Ninguna nota enlaza a esta todavía.";
      const ayuda = document.createElement("div");
      ayuda.className = "backlinks-help";
      const ejemplo = document.createElement("code");
      ejemplo.textContent = `[[${page.title}]]`;
      ayuda.append("Se crean escribiendo ", ejemplo, " en el cuerpo de otra nota.");
      backList.append(vacio, ayuda);
      return;
    }
    // Con backlinks, se lee de dónde viene cada uno: la cabecera dice qué es
    // esta lista y cada fila es el título de la nota que enlaza, clicable.
    const cabecera = document.createElement("div");
    cabecera.className = "backlinks-title";
    cabecera.textContent = "Notas que enlazan aquí";
    backList.appendChild(cabecera);
    fresh.forEach((b) => {
      const item = document.createElement("div");
      item.className = "backlink-item";
      item.textContent = b.title;
      // navigateToPage y no goTo("note"): un backlink puede venir de una base
      // de datos (misma tabla `pages`), y su id no es el que abre la base.
      item.addEventListener("click", () => navigateToPage(b.id));
      makeKeyboardActivatable(item, "button", 0);
      backList.appendChild(item);
    });
  }
  paintBacklinks(backlinks);
  backDetails.append(backSummary, backList);

  const exportBtn = document.createElement("button");
  exportBtn.className = "icon-btn";
  exportBtn.innerHTML = svgIcon("i-download");
  exportBtn.title = "Exportar como Markdown (.md)";
  exportBtn.setAttribute("aria-label", "Exportar como Markdown (.md)");
  exportBtn.addEventListener("click", () => exportPageAsMarkdown(page, state.editor.getValue()));

  const topbarRight = document.createElement("div");
  topbarRight.className = "note-topbar-right";
  topbarRight.append(botonCompartir("page", page.id, () => page.title), exportBtn, backDetails);

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
      removeBtn.innerHTML = `${svgIcon("i-x")}Quitar cabecera`;
      removeBtn.addEventListener("click", async () => {
        const updated = await api(`/pages/${page.id}/header-image`, { method: "DELETE" });
        page.header_image_path = updated.header_image_path;
        renderHeaderImage();
      });

      headerWrap.append(img, removeBtn);
    } else {
      const addBtn = document.createElement("button");
      addBtn.className = "btn";
      addBtn.innerHTML = `${svgIcon("i-plus")}Imagen de cabecera`;
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
      if (sesionExpirada(res)) return; // la subida tampoco pasa sin sesión
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
  paintPageIcon(iconBtn, "note", page.icon, "icon-20");
  iconBtn.title = "Cambiar icono";
  iconBtn.setAttribute("aria-label", "Cambiar icono de la página");
  iconBtn.addEventListener("click", () => {
    openEmojiPicker(iconBtn, (emoji) => {
      page.icon = emoji;
      paintPageIcon(iconBtn, "note", emoji, "icon-20");
      scheduleSave();
    });
  });

  // El título es un <textarea> de una sola línea lógica y no un <input>
  // (P14): en un móvil de 375 px un título normal no cabe y el <input> lo
  // recorta sin más (no hay dónde leer el final). Con un textarea el título
  // ocupa las líneas que necesite y se lee entero; Enter no mete salto, quita
  // el foco (un título no tiene saltos de línea).
  const titleInput = document.createElement("textarea");
  titleInput.className = "note-title";
  titleInput.rows = 1;
  titleInput.value = page.title;
  titleInput.setAttribute("aria-label", "Título de la página");
  titleInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      titleInput.blur();
    }
  });
  function autoResizeTitle() {
    titleInput.style.height = "auto";
    titleInput.style.height = `${titleInput.scrollHeight}px`;
  }

  titleRow.append(iconBtn, titleInput);

  // ---- meta (creador, obligatorio y de solo lectura -- se fija al crear) ----
  // El punto lleva el color de quien la creó, que viene resuelto en la página
  // (`created_by_color`); sin color (notas viejas con «Tú») no hay punto (ver
  // paintCreatedBy).
  const meta = document.createElement("div");
  meta.className = "note-meta";
  paintCreatedBy(meta, page.created_by, page.created_by_color, page.created_by_avatar);
  // /auth/me puede llegar después que la nota: entonces se repinta solo esto
  // (el color del servidor no cambia; el respaldo local sí puede).
  state.repaintAuthor = () =>
    paintCreatedBy(meta, page.created_by, page.created_by_color, page.created_by_avatar);

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
  // Aviso de "esta nota cambió en disco" en pantalla (P8): mientras esté ahí,
  // el autoguardado NO manda nada -- si mandara, escribiría lo de esta pestaña
  // encima del cambio de disco y el aviso dejaría de tener sentido (el usuario
  // ya no podría elegir el fichero). Se levanta con "Descartar" o al recargar.
  let blocked = false;
  let changeSeq = 0; // sube con cada cambio: distingue "guardado" de "cambiado mientras guardaba"
  let lastSentBody = null; // último cuerpo ACEPTADO por el servidor
  let savingBody = null; // cuerpo del PATCH normal que va de camino (null: ninguno)
  let beaconInFlight = null; // cuerpo del guardado de emergencia que va de camino
  let beaconQueued = false; // se pidió guardar otro contenido mientras había un beacon en vuelo

  // El editor de ESTA nota, fijado al terminar renderNote(). `state.editor` es
  // el de la nota que se esté pintando AHORA: en cuanto se navega pasa a ser el
  // de la nota nueva, así que leerlo desde el cierre de la nota que se deja
  // atrás es leer el cuerpo de otra nota -- y ese texto acababa escrito en el
  // `.md` de esta. Tras `destroy()` el documento de `noteEditor` sigue siendo
  // el de esta nota, y es el único texto que este guardado puede mandar.
  let noteEditor = null;
  let noteBodyFallback = page.body_markdown || ""; // último texto visto, si el editor ya no está
  // 3.0, hito 4: si la nota está en una sala de edición en vivo (ver abajo,
  // `startCollab`). Mientras lo esté, el cuerpo no se guarda por `PATCH`.
  let collabLive = false;
  let session = null; // la de esta nota; la crea renderNote() al final

  /** Texto del cuerpo de ESTA nota -- nunca el de la que esté abierta ahora.
   * El editor de esta nota se destruye al navegar, pero su documento sigue
   * siendo el suyo: de ahí sale el texto de un guardado que termina tarde. */
  function bodyText() {
    if (noteEditor) {
      try {
        return noteEditor.getValue();
      } catch {
        // editor destruido sin documento legible: vale el último texto visto
      }
    }
    return noteBodyFallback;
  }

  /** ¿Esta nota (este cierre de renderNote) sigue siendo la que está abierta? */
  function noteIsOpen() {
    return session !== null && state.noteSession === session;
  }

  /** Cuerpo del PATCH, compartido por el guardado normal y el de emergencia:
   * uno solo, para que no puedan acabar mandando cosas distintas. */
  function buildSaveBody() {
    // El icono es "obligatorio": si lo han vaciado, se restaura un valor por
    // defecto en vez de guardar un icono vacío.
    if (!page.icon) page.icon = "📄";
    const cuerpo = {
      title: titleInput.value,
      icon: page.icon,
      description: descInput.value,
    };
    // En una sala (3.0, hito 4) el cuerpo no va por aquí: lo guarda el
    // servidor. Mandarlo sería escribir por un camino que la sala no ve, y la
    // reiniciaría para todos.
    if (!collabLive) cuerpo.body_markdown = bodyText();
    return JSON.stringify(cuerpo);
  }

  function scheduleSave() {
    dirty = true;
    changeSeq += 1;
    setSaveState("dirty");
    clearTimeout(saveTimer);
    if (blocked) return; // hay un cambio en disco sin decidir: no se pisa
    // En una sala, el título se ve en vivo: se manda casi al momento.
    saveTimer = setTimeout(doSave, collabLive ? LIVE_META_DELAY_MS : AUTOSAVE_DELAY_MS);
    state.flushPendingSave = () => {
      clearTimeout(saveTimer);
      doSave();
    };
  }

  /** Lo que otro ha escrito en el título, la descripción o el icono (por la
   * sala). Si aquí hay cambios sin guardar en ese mismo campo, se quedan los
   * de aquí: se van a guardar enseguida y ganan (el último que escribe). */
  function applyRemoteMeta(fields) {
    if (state.editor !== noteEditor) return;
    if (typeof fields.title === "string" && fields.title !== titleInput.value) {
      if (!dirty || document.activeElement !== titleInput) {
        const enfocado = document.activeElement === titleInput;
        const [ini, fin] = [titleInput.selectionStart, titleInput.selectionEnd];
        titleInput.value = fields.title;
        if (enfocado) titleInput.setSelectionRange(Math.min(ini, fields.title.length), Math.min(fin, fields.title.length));
        autoResizeTitle();
        page.title = fields.title;
        const nodo = state.pagesById.get(String(page.id));
        if (nodo) nodo.title = fields.title;
      }
    }
    if (typeof fields.description === "string" && fields.description !== descInput.value) {
      if (!dirty || document.activeElement !== descInput) {
        const enfocado = document.activeElement === descInput;
        const [ini, fin] = [descInput.selectionStart, descInput.selectionEnd];
        descInput.value = fields.description;
        if (enfocado) descInput.setSelectionRange(Math.min(ini, fields.description.length), Math.min(fin, fields.description.length));
        autoResizeDesc();
        page.description = fields.description;
      }
    }
    if (typeof fields.icon === "string" && fields.icon && fields.icon !== page.icon) {
      page.icon = fields.icon;
      paintPageIcon(iconBtn, "note", fields.icon, "icon-20");
    }
    if (Number.isInteger(fields.version)) {
      page.version = fields.version;
      recordVersion({ id: page.id, version: fields.version });
    }
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
          // La versión que deja este guardado (3.0): si no se apunta, el
          // siguiente autoguardado iría con la de antes y saldría un 409 falso.
          res.clone().json().then(recordVersion, () => {});
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
    // Con el aviso de cambio en disco en pantalla no se escribe nada (tampoco
    // con Ctrl+S): primero se decide recargar o quedarse con lo de aquí.
    if (blocked) return;
    clearTimeout(saveTimer);
    // Un guardado de ESTA nota solo puede aplicarse a su `.md`. Si la nota ya
    // se dejó atrás y no queda nada suyo sin confirmar, se descarta: reenviarlo
    // escribiría su texto viejo encima de lo que tenga ahora el fichero (una
    // edición de fuera, por ejemplo) sin ganar nada. El flush de navegación
    // sigue pasando -- clearMain() lo llama ANTES de anular la sesión, así que
    // en ese momento esta nota todavía es la abierta --, y también los
    // reintentos de contenido que el servidor nunca confirmó (`dirty`).
    if (!noteIsOpen() && !dirty) return;
    if (saving) {
      saveAgain = true;
      return;
    }
    saving = true;
    // El indicador es el de la nota ABIERTA: un guardado que termina tarde en
    // una nota que ya no se está mirando no puede moverlo (diría «Guardando…»
    // de una nota que no es la de la pantalla).
    if (noteIsOpen()) setSaveState("saving");

    const seqAtSave = changeSeq;
    const body = buildSaveBody();
    // Lo que se ha mandado como título, para saber si el servidor lo cambia:
    // `names.sanitize()` sustituye los corchetes y los signos que no valen en
    // un nombre de fichero, y la nota acaba llamándose de otra forma.
    const sentTitle = JSON.parse(body).title;
    savingBody = body; // para que el guardado de emergencia sepa qué va de camino

    try {
      // `If-Match` (3.0): el guardado solo vale si nadie ha escrito la nota
      // desde la versión que tenemos. Si alguien lo hizo (otra pestaña, otra
      // persona), el servidor contesta 409 y decide el usuario (abajo).
      const known = pageVersions.get(String(page.id)) ?? page.version;
      const headers = { "Content-Type": "application/json" };
      // En una sala la versión la mueve el servidor al guardar el cuerpo: el
      // título y la descripción van sin `If-Match` (el último gana, como
      // cualquier campo suelto); el conflicto del cuerpo lo resuelve la sala.
      if (Number.isInteger(known) && !collabLive) headers["If-Match"] = `"${known}"`;
      // En una sala (bloque C) el título, la descripción y el icono van por
      // ella: se guardan igual y los demás los ven al momento. Si la sala no
      // contesta (se cayó a medias), por `PATCH`, como siempre.
      let updated = null;
      const sala = collabLive ? state.collabSession : null;
      if (sala?.saveMeta && state.editor === noteEditor) {
        try {
          updated = await sala.saveMeta(JSON.parse(body));
          recordVersion({ id: page.id, version: updated.version });
        } catch (err) {
          if (err?.readonly) throw err;
          updated = null;
        }
      }
      if (!updated) updated = await api(`/pages/${page.id}`, { method: "PATCH", body, headers });
      page.updated_at = updated.updated_at;
      page.version = updated.version;
      page.title = updated.title;
      // Si el servidor ha cambiado el título, la cabecera tiene que enseñar el
      // que se ha guardado de verdad: si no, la nota se llama de una forma en
      // la cabecera y de otra en el árbol, sin nada que lo explique, y el
      // cambio se descubre al recargar. El input solo se pisa si no se ha
      // escrito nada mientras iba el PATCH (`changeSeq === seqAtSave`): si se
      // escribió, lo tecleado manda y el aviso sale en el guardado que lo
      // aplique, con el título que quede entonces. `showError` no se apila
      // consigo mismo, así que esto es un aviso por guardado, no por tecla.
      //
      // El aviso dice solo lo que se sabe: qué título ha quedado. De los
      // corchetes sí se sabe la causa (el formato de `[[…]]` no los admite),
      // pero `sanitize()` cambia el título por más motivos —vacío, espacios de
      // más, nombre ya cogido, truncado— y ahí el aviso no puede culpar a los
      // caracteres ni inventarse un porqué.
      // Desde P14 el título es un <textarea>: repintarlo cambia el número de
      // líneas, así que hay que volver a medir el alto. Y también cubre pegar
      // un título con saltos, que en disco se guarda saneado.
      if (noteIsOpen() && changeSeq === seqAtSave && titleInput.value !== updated.title) {
        titleInput.value = updated.title;
        autoResizeTitle();
        if (updated.title !== sentTitle) showError(
          /\[|\]/.test(sentTitle)
            ? `El título se ha guardado como «${updated.title}»: los corchetes rompen los enlaces [[…]].`
            : `El título se ha guardado como «${updated.title}».`,
        );
      }
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
        // `flushPendingSave` es el disparador de la nota ABIERTA: si esta ya
        // se dejó atrás, el suyo lo puso (y lo consumió) la nota nueva al
        // navegar, y anularlo aquí dejaría a esa sin flush al salir.
        if (noteIsOpen()) state.flushPendingSave = null;
      }
      // El indicador, el árbol y el resaltado son de la nota que se está
      // mirando: si esta se dejó atrás mientras el PATCH iba de camino, no se
      // tocan -- un cierre ajeno no puede repintar el estado de otra nota.
      if (noteIsOpen()) {
        // Si sigue habiendo algo pendiente (se escribió durante el guardado, o
        // va otro PATCH de camino), el indicador no puede decir "Guardado": lo
        // manda el autoguardado de 2 s o el reenvío del beacon.
        setSaveState(dirty ? "dirty" : "saved");
        await loadTree();
        markActive(page.id);
      }
    } catch (e) {
      if (e.status === 409 && e.data && Number.isInteger(e.data.version) && noteIsOpen()) {
        // Alguien guardó esta nota mientras escribíamos (3.0, `If-Match`). No
        // se pisa ni se pierde nada: lo de esta pestaña sigue en el editor sin
        // guardar, y el usuario elige. «Recargar» trae lo del otro; «Quedarme
        // con lo mío» guarda esto encima (con la versión de ahora).
        const actual = e.data.version;
        setSaveState("dirty");
        session.suspendSave();
        showRemoteBanner({
          session,
          text: "Alguien ha guardado esta nota mientras escribías. Tus cambios siguen aquí, sin guardar.",
          reloadTitle: "Traer lo que se ha guardado y perder lo tuyo",
          dismissLabel: "Quedarme con lo mío",
          dismissTitle: "Guardar lo que hay en el editor encima",
          onReload: () => reloadOpenNoteFromDisk(session),
          onDismiss: () => {
            pageVersions.set(String(page.id), actual);
            session.resumeSave();
          },
        });
        return;
      }
      // El indicador de error también es el de la nota abierta: si el PATCH
      // fallido era de una que ya se dejó atrás, el aviso sería de otra nota.
      if (noteIsOpen()) setSaveState("error");
      console.error(e);
    } finally {
      saving = false;
      savingBody = null;
      if (saveAgain) {
        saveAgain = false;
        // Solo si queda algo sin confirmar. Este reenvío se lanzaba a ciegas
        // desde una nota ya abandonada y, como el cuerpo se leía del editor
        // global, escribía el texto de la nota nueva en el `.md` de la vieja.
        if (dirty) doSave();
      }
    }
  }

  titleInput.addEventListener("input", () => {
    autoResizeTitle();
    scheduleSave();
  });
  descInput.addEventListener("input", () => {
    autoResizeDesc();
    scheduleSave();
  });

  // ---- una fila de base: sus propiedades, entre la descripción y el cuerpo ----
  // Una fila es una página normal; lo que tiene de más son las propiedades de
  // su base (ver `montarPanelDeFila`). La miga de pan dice de qué base es y
  // lleva a su tabla.
  const esFila = page.type === "database_row" && Boolean(page.database_id);
  const panelFilaEl = esFila ? document.createElement("section") : null;
  if (esFila) {
    const miga = document.createElement("nav");
    miga.className = "note-crumbs";
    miga.setAttribute("aria-label", "Dónde estás");
    const aLaBase = document.createElement("button");
    aLaBase.type = "button";
    aLaBase.className = "note-crumb";
    const nodoBase = [...state.pagesById.values()].find(
      (n) => n.type === "database" && String(n.database_id) === String(page.database_id),
    );
    aLaBase.innerHTML = `${svgIcon("i-database")}<span></span>`;
    aLaBase.querySelector("span").textContent = nodoBase?.title || "Base de datos";
    aLaBase.title = "Abrir la base de esta fila";
    aLaBase.addEventListener("click", () => goTo("database", page.database_id));
    miga.appendChild(aLaBase);
    topbar.prepend(miga);
  }

  main.append(
    topbar, headerWrap, fileInput, titleRow, meta, descInput,
    ...(panelFilaEl ? [panelFilaEl] : []),
    bodyMount,
  );
  const panelFila = panelFilaEl
    ? montarPanelDeFila(page, panelFilaEl, {
        alCargarBase: (base) => {
          const rotulo = topbar.querySelector(".note-crumb span");
          if (rotulo) rotulo.textContent = base.title || "Base de datos";
        },
      })
    : null;
  autoResizeDesc();
  autoResizeTitle();

  // El alto del título y de la descripción sale de su ancho, y ese ancho cambia
  // sin que haya `resize` de ventana: al girar el móvil, al estrechar la ventana
  // y también al plegar la barra lateral en escritorio. Sin volver a medir, el
  // <textarea> (overflow: hidden, sin asa ni scroll) se queda con el alto de
  // antes y el texto que no cabe no se puede leer de ninguna manera. Se vigila
  // el ancho del propio campo: al medir el alto cambia el alto, no el ancho, así
  // que comparar el ancho evita que el observador se dispare a sí mismo.
  let anchoTitulo = titleInput.clientWidth;
  function refitNoteText() {
    if (!titleInput.isConnected) return;
    const ancho = titleInput.clientWidth;
    if (ancho === anchoTitulo) return;
    anchoTitulo = ancho;
    autoResizeTitle();
    autoResizeDesc();
  }
  const noteWidthObserver = new ResizeObserver(refitNoteText);
  noteWidthObserver.observe(titleInput);
  state.refitNoteText = refitNoteText;
  state.noteWidthObserver = noteWidthObserver;

  // Comando "/page" del editor: crea una página hija de ÉSTA (page.id, la
  // que está abierta ahora mismo) con el nombre que el usuario escribió en el
  // selector (ver "page"/"naming" en slashMenuExtension). Devuelve {id, title}
  // y el editor la abre con `onOpenPage` -- ya no se queda el enlace en esta
  // nota, porque se navega a la nueva. El nombre es obligatorio: una página
  // sin título repetiría "Nueva página" y haría ambiguo el wikilink.
  async function createChildPage(title) {
    const nombre = String(title || "").trim();
    if (!nombre) return null;
    let created;
    try {
      created = await api("/pages", {
        method: "POST",
        body: JSON.stringify({ title: nombre, parent_id: page.id, body_markdown: "" }),
      });
    } catch (e) {
      if (e?.status === 401) return null; // ya se está yendo al login
      reportError(`No se pudo crear la página «${nombre}»`, e);
      return null;
    }
    await loadTree();
    // El servidor puede haber cambiado el nombre al sanearlo (corchetes, un
    // fichero que ya existía en el disco...). Se dice cuál ha quedado porque
    // es la página a la que se va a navegar, no la que se tecleó.
    if (!sameTitle(created.title, nombre)) {
      showError(
        `La página se creó como «${created.title}»: «${nombre}» no estaba libre ` +
          "o no vale como nombre de fichero.",
      );
    }
    return { id: created.id, title: created.title };
  }

  // /page navega a la página recién creada (pedido del dueño: «se debería de
  // acceder automáticamente a esa nueva página»). Lo que había sin guardar en
  // ESTA nota se guarda de verdad -- se espera al PATCH, no se lanza y se
  // sigue -- antes de dejar la nota: si no, el autoguardado de 2 s se quedaba
  // pendiente y lo último tecleado no llegaba al `.md`.
  async function openPageFromSlash(pageId) {
    if (!pageId) return;
    const session = state.noteSession;
    if (state.flushPendingSave) {
      const flush = state.flushPendingSave;
      state.flushPendingSave = null;
      flush();
    }
    await session?.whenQuiet?.(5000);
    await goTo("note", pageId);
    // Cursor al principio del cuerpo: el título ya está escrito y así se
    // empieza a escribir dentro sin tener que dar un clic.
    state.editor?.jumpTo?.(0);
  }

  // «Renombrar» del selector de /page. El nombre nuevo tiene que estar libre
  // entre las hermanas (el servidor desambigua en silencio con un «(2)», y ese
  // duplicado es justo lo que no se quiere). Devuelve cómo ha quedado para que
  // el editor escriba el enlace con el título real.
  async function renamePageFromSlash(pageId, title) {
    const resultado = await renombrarPaginaDelVault(pageId, title);
    if (!resultado) return null;
    // Si se renombra la nota que está abierta, su cabecera tiene que enseñar
    // el nombre nuevo: el PATCH va por el editor, no por el campo del título.
    if (String(page.id) === String(pageId)) {
      page.title = resultado.title;
      titleInput.value = resultado.title;
      autoResizeTitle();
      markActive(pageId);
    }
    return resultado;
  }

  // Comando "Crear «…»" del autocompletado de "[[" (entry.js): la nota nueva
  // es HERMANA de la que se está editando -- mismo parent_id, o la raíz si
  // ésta está en la raíz --, no su hija: escribir "[[" es enlazar con otra
  // nota del mismo nivel, y crear una subpágina ya es "/page" (que sí cuelga
  // de page.id). El árbol se refresca aquí mismo, sin esperar al evento del
  // vigilante (el SSE puede llegar después, o no llegar si está
  // desconectado). Devuelve {id, title} para que el editor escriba el enlace
  // con el título que haya quedado DE VERDAD en el servidor: el nombre se
  // normaliza al crearlo y "Lo que sea" puede acabar siendo otra cosa.
  async function createLinkedPage(title) {
    return crearNotaHermana(page.parent_id, title);
  }

  // Comando "/base" (P17.3): crear la base aquí, colgando de ESTA nota -- el
  // mismo camino que el panel (POST /databases con parent_id), que la guarda
  // en la carpeta de hijas de la nota (`Notas/Proyectos/Contactos.md`), no en
  // `Bases/`. Sin `parent_id` acabaría suelta en la raíz.
  //
  // Si en esa carpeta ya hay algo con ese nombre no se crea nada: el servidor
  // desambigua en silencio y ese duplicado es justo lo que no queremos (M1).
  async function createDatabaseInNote(title) {
    const nombre = title.trim();
    if (!nombre) return null;
    const ocupada = childTitleTaken(page.id, nombre);
    if (ocupada) {
      // El título que ya existe, no lo que se acaba de teclear: es el que el
      // usuario verá en el árbol.
      showError(
        ocupada.type === "database"
          ? `Ya hay una base llamada «${ocupada.title}» en esta carpeta.`
          : `Ya hay una página llamada «${ocupada.title}» en esta carpeta.`,
      );
      return null;
    }
    let created;
    try {
      created = await api("/databases", {
        method: "POST",
        body: JSON.stringify({ title: nombre, parent_id: page.id, schema_def: [] }),
      });
    } catch (e) {
      if (e?.status === 401) return null;
      reportError(`No se pudo crear la base «${nombre}»`, e);
      return null;
    }
    await loadTree();
    // El servidor puede haber cambiado el nombre: si en el disco ya había un
    // fichero con ese nombre (aunque el índice aún no lo conociera) le pone
    // sufijo, y si el nombre no cabe en un fichero lo sanea. Se dice lo que
    // quedó de verdad -- el bloque apunta a esa base, no a lo tecleado.
    if (!sameTitle(created.title, nombre)) {
      showError(
        `La base se creó como «${created.title}»: «${nombre}» no estaba libre ` +
          "o no vale como nombre de fichero.",
      );
    }
    return { id: created.id, title: created.title, view: null };
  }

  createOutlinePanel(topbarRight);

  state.editor = createNoteEditor({
    parent: bodyMount,
    doc: page.body_markdown || "",
    resolvePage: (title) => findPageByTitle(title),
    onNavigate: (pageId) => navigateToPage(pageId),
    onChange: (_text, info) => {
      noteBodyFallback = bodyText();
      renderOutline();
      // 3.0, hito 4: en una sala, el cuerpo lo guarda el servidor (los cambios
      // propios salen por el WebSocket; los ajenos ya vienen guardados). El
      // autoguardado de siempre queda para el título y la descripción, y para
      // cuando no hay sala (servidor viejo, proxy sin WebSocket).
      if (info?.remote || collabLive) return;
      scheduleSave();
    },
    // Una fila no es carpeta de nada (`_folder_for_parent` lo rechaza en el
    // vault): se dice en vez de dejar un comando que no hace nada.
    onCreatePage: esFila
      ? () => {
          showError("Una fila no puede tener subpáginas: crea la nota desde la barra lateral.");
          return null;
        }
      : createChildPage,
    onOpenPage: openPageFromSlash,
    onRenamePage: renamePageFromSlash,
    onListPages: listPageEntries,
    currentPageId: page.id,
    onCreateLink: createLinkedPage,
    onBlockContextMenu: (payload) =>
      mostrarMenuDeBloque(payload, () => {
        noteBodyFallback = bodyText();
        scheduleSave();
        renderOutline();
      }),
    onWikilinkHover: (pageId, rect) => startLinkPreview(pageId, rect),
    onWikilinkHoverEnd: () => stopLinkPreview(),
    onSlashMenu: (menuState) => renderSlashMenu(menuState),
    onListDatabases: listDatabaseEntries,
    onPickDatabase: databaseBlockFor,
    onCreateDatabase: esFila
      ? () => {
          showError("Una fila no puede tener una base colgando: crea la base desde una nota.");
          return null;
        }
      : createDatabaseInNote,
    renderDatabaseBlock: renderDatabaseEmbed,
  });
  noteEditor = state.editor;

  // ---- edición en vivo (3.0, hito 4) ----
  // La nota entra en su sala: lo que escribes llega a los demás al momento y
  // lo suyo a ti, con su cursor. Si no hay sala (servidor sin WebSocket), todo
  // sigue como antes: autoguardado por PATCH.
  state.collabSession = state.editor.startCollab({
    pageId: page.id,
    onStatus: (estado) => {
      if (state.editor !== noteEditor) return; // la nota ya se dejó atrás
      if (estado === "unavailable") {
        collabLive = false;
        return;
      }
      if (estado === "connecting") return;
      collabLive = estado !== "gone";
      if (estado === "gone") {
        showError("Esta nota se ha borrado.");
        return;
      }
      // El indicador habla del cuerpo cuando no hay nada más pendiente (el
      // título y la descripción siguen con su autoguardado).
      if (estado === "saving") setSaveState("saving");
      else if (estado === "offline") setSaveState("offline");
      else if (estado === "readonly") setSaveState("readonly");
      else if (!dirty) setSaveState("saved");
    },
    onRemoteReset: () =>
      showError("La nota se ha cambiado por otro camino y se ha recargado: lo último que escribiste no llegó a guardarse."),
    onMeta: (fields) => applyRemoteMeta(fields),
  });

  // El árbol ya está cargado cuando se abre una nota (lo pide el arranque y
  // cada navegación), pero setLinkTargets es un efecto sobre el estado del
  // editor: hay que dárselo a ESTA instancia, que acaba de nacer.
  syncLinkTargets();

  // ---- sugerencias de agentes (P10c) ----
  // El contador de la cabecera abre el panel; el editor pinta las pendientes
  // en su sitio y devuelve cuáles pudo anclar (setSuggestions: {placed,
  // unplaced}). Nada de esto existe para una nota sin sugerencias: el botón
  // nace oculto y el editor no recibe lista.
  const sugBtn = document.createElement("button");
  sugBtn.type = "button";
  sugBtn.className = "note-suggestions-counter";
  sugBtn.hidden = true;
  sugBtn.setAttribute("aria-expanded", "false");
  sugBtn.setAttribute("aria-controls", "note-suggestions-panel");
  topbarRight.prepend(sugBtn);

  const panel = document.createElement("aside");
  panel.id = "note-suggestions-panel";
  panel.className = "note-suggestions-panel";
  panel.hidden = true;
  panel.setAttribute("aria-label", "Sugerencias de agentes");
  // Dentro de .layout (a la derecha de #main y desplazándolo), no colgando de
  // body: así el panel reserva su sitio en vez de tapar el texto.
  (document.querySelector(".layout") || document.body).appendChild(panel);
  suggestionsPanelEl = panel;

  sugBtn.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    sugBtn.setAttribute("aria-expanded", panel.hidden ? "false" : "true");
  });

  let suggestionItems = []; // todas las de esta nota: pendientes y decididas
  let placedIds = new Set(); // las que el editor ha podido anclar en el texto

  // Un único objeto de callbacks mientras viva la nota: el editor lo compara
  // por identidad (SuggestionWidget.eq) para no reconstruir los widgets en
  // cada repintado.
  const suggestionCallbacks = {
    // `meta.hadFocus` dice si quien decidió era el propio botón (Tab/Enter o
    // Espacio); ver wireSuggestionButton en entry.js.
    onAccept: (id, meta) => acceptSuggestion(id, meta),
    onReject: (id, meta) => rejectSuggestion(id, meta),
    // El editor avisa cuando es una EDICIÓN la que desancla algo. Sin esto,
    // `placedIds` era la foto de cuando se cargó la nota: si el `old_text`
    // desaparecía del texto, la marca se iba (bien) pero el panel seguía
    // diciendo que estaba «dentro del texto» y no la listaba en ningún sitio.
    onPlacementChange: ({ placed }) => {
      if (state.noteSession !== session) return;
      const next = new Set(placed ?? []);
      const same =
        next.size === placedIds.size && [...next].every((id) => placedIds.has(id));
      if (same) return;
      placedIds = next;
      paintSuggestionsPanel();
    },
  };

  function paintSuggestions() {
    if (state.noteSession !== session) return; // ya se navegó a otra nota
    const pending = suggestionItems.filter((s) => s.status === "pending");
    const result = state.editor?.setSuggestions?.(pending, suggestionCallbacks);
    placedIds = new Set(result?.placed ?? []);
    sugBtn.hidden = suggestionItems.length === 0;
    // Sin pendientes no se enseña un «(0)», que parece un aviso habiendo
    // nada que decidir (C3). El botón sigue: es la puerta a las ya
    // decididas, y su title dice lo que hay.
    sugBtn.textContent = pending.length ? `Sugerencias (${pending.length})` : "Sugerencias";
    sugBtn.title = pending.length
      ? `${pending.length} sugerencia${pending.length > 1 ? "s" : ""} por decidir`
      : "No queda nada pendiente: aquí están las ya decididas";
    paintSuggestionsPanel();
  }

  function paintSuggestionsPanel() {
    // Lo que el dueño esté escribiendo en una respuesta no se pierde al
    // repintar (un rechazo, una respuesta enviada, una sugerencia nueva...).
    const drafts = new Map();
    panel.querySelectorAll(".sug-reply-input").forEach((input) => {
      if (input.value.trim()) drafts.set(input.dataset.suggestionId, input.value);
    });

    panel.innerHTML = "";

    const head = document.createElement("div");
    head.className = "sug-panel-head";
    const headTitle = document.createElement("span");
    headTitle.className = "sug-panel-title";
    headTitle.textContent = "Sugerencias de agentes";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "icon-btn sug-panel-close";
    closeBtn.innerHTML = svgIcon("i-x");
    closeBtn.title = "Cerrar el panel de sugerencias";
    closeBtn.setAttribute("aria-label", "Cerrar el panel de sugerencias");
    closeBtn.addEventListener("click", () => {
      panel.hidden = true;
      sugBtn.setAttribute("aria-expanded", "false");
    });
    head.append(headTitle, closeBtn);
    panel.appendChild(head);

    const pending = suggestionItems.filter((s) => s.status === "pending");
    const blocked = pending.filter((s) => !placedIds.has(s.id));
    const decided = suggestionItems.filter((s) => s.status !== "pending");
    const anchored = pending.length - blocked.length;

    const summary = document.createElement("div");
    summary.className = "sug-panel-note";
    if (suggestionItems.length === 0) {
      summary.textContent = "Ningún agente ha propuesto nada en esta nota.";
      panel.appendChild(summary);
    } else if (anchored) {
      summary.textContent = `${anchored} pendiente${anchored > 1 ? "s" : ""} dentro del texto; aquí está el resto.`;
      panel.appendChild(summary);
    } else if (blocked.length) {
      // Ninguna encaja ya en el texto: el editor no pinta nada y todo lo que
      // hay que decidir está aquí.
      summary.textContent = "Las pendientes ya no encajan en el texto; están aquí abajo.";
      panel.appendChild(summary);
    } else {
      summary.textContent = "No queda nada pendiente.";
      panel.appendChild(summary);
    }

    if (blocked.length) {
      panel.appendChild(suggestionSection("No se pueden pintar en el texto", blocked, true));
    }
    if (decided.length) {
      panel.appendChild(suggestionSection("Ya decididas", decided, false));
    }

    panel.querySelectorAll(".sug-reply-input").forEach((input) => {
      const draft = drafts.get(input.dataset.suggestionId);
      if (draft) input.value = draft;
    });
  }

  function suggestionSection(heading, items, decidable) {
    const section = document.createElement("section");
    section.className = "sug-section";
    const title = document.createElement("h3");
    title.className = "sug-section-head";
    title.textContent = `${heading} (${items.length})`;
    section.appendChild(title);
    items.forEach((s) => section.appendChild(suggestionItemEl(s, decidable)));
    return section;
  }

  /** Una sugerencia en el panel: quién, cuándo, qué cambia, por qué, su hilo
   * y el campo para responderle. */
  function suggestionItemEl(s, decidable) {
    const item = document.createElement("div");
    item.className = "sug-item";
    item.dataset.suggestionId = s.id;

    const head = document.createElement("div");
    head.className = "sug-head";
    const agent = document.createElement("span");
    agent.className = "sug-agent";
    agent.textContent = s.agent || "agente";
    const when = document.createElement("span");
    when.className = "sug-when";
    // formatted relative (además del title exacto): lo mismo que el resto de
    // fechas de la app.
    when.textContent = s.created_at ? formatRelativeTime(s.created_at) : "";
    if (s.created_at) when.title = s.created_at;
    const status = document.createElement("span");
    status.className = `sug-status sug-status-${s.status}`;
    status.textContent = SUGGESTION_STATUS_LABEL[s.status] || s.status;
    head.append(agent, when, status);

    const diff = document.createElement("div");
    diff.className = "sug-diff";
    if (s.kind === "new_page") {
      const proposedTitle = document.createElement("span");
      proposedTitle.className = "sug-new";
      proposedTitle.textContent = s.title || "(sin título)";
      const kind = document.createElement("span");
      kind.className = "sug-when";
      kind.textContent = " · nota nueva";
      diff.append(proposedTitle, kind);
    } else {
      const before = document.createElement("span");
      before.className = "sug-old";
      before.textContent = s.old_text;
      const arrow = document.createElement("span");
      arrow.className = "sug-arrow";
      arrow.textContent = "→";
      const after = document.createElement("span");
      after.className = "sug-new";
      after.textContent = s.new_text;
      diff.append(before, arrow, after);
    }
    item.append(head, diff);

    if (s.rationale) {
      const why = document.createElement("div");
      why.className = "sug-rationale";
      why.textContent = s.rationale;
      item.appendChild(why);
    }
    if (s.reason) {
      const reason = document.createElement("div");
      reason.className = "sug-reason";
      reason.textContent = `Motivo del rechazo: ${s.reason}`;
      item.appendChild(reason);
    }

    if (decidable) {
      const actions = document.createElement("div");
      actions.className = "sug-actions";
      const accept = document.createElement("button");
      accept.type = "button";
      accept.className = "btn sug-accept";
      accept.textContent = "Aceptar";
      accept.addEventListener("click", () => acceptSuggestion(s.id));
      const reject = document.createElement("button");
      reject.type = "button";
      reject.className = "btn sug-reject";
      reject.textContent = "Rechazar";
      reject.addEventListener("click", () => rejectSuggestion(s.id));
      actions.append(accept, reject);
      item.appendChild(actions);
    }

    const replies = Array.isArray(s.replies) ? s.replies : [];
    if (replies.length) {
      const thread = document.createElement("div");
      thread.className = "sug-thread";
      replies.forEach((r) => {
        const entry = document.createElement("div");
        entry.className = "sug-reply-entry";
        const who = document.createElement("span");
        who.className = "sug-reply-author";
        // Quién habla: si el autor tiene cuenta (el dueño, o cualquiera que
        // haya respondido) lleva su punto de color; si no —el agente—, su
        // nombre tal cual, que es lo que ya venía.
        const authorColor = colorForAuthor(r.author);
        const authorImg = avatarImg(avatarForAuthor(r.author));
        if (authorImg) {
          who.classList.add("is-owner");
          who.appendChild(authorImg);
        } else if (authorColor) {
          who.classList.add("is-owner");
          const dot = document.createElement("span");
          dot.className = "user-dot";
          dot.style.background = authorColor;
          dot.setAttribute("aria-hidden", "true");
          who.appendChild(dot);
        }
        who.appendChild(document.createTextNode(`${r.author || "—"}:`));
        const text = document.createElement("span");
        text.className = "sug-reply-text";
        text.textContent = r.text;
        entry.append(who, " ", text);
        thread.appendChild(entry);
      });
      item.appendChild(thread);
    }

    const replyRow = document.createElement("div");
    replyRow.className = "sug-reply-row";
    const input = document.createElement("input");
    input.type = "text";
    input.className = "sug-reply-input";
    input.dataset.suggestionId = s.id;
    input.placeholder = "Responder al agente…";
    input.maxLength = 1000;
    input.setAttribute("aria-label", "Responder al agente sobre esta sugerencia");
    const send = document.createElement("button");
    send.type = "button";
    send.className = "btn sug-reply-send";
    send.textContent = "Enviar";
    send.addEventListener("click", () => sendReply(s.id, input, send));
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      sendReply(s.id, input, send);
    });
    replyRow.append(input, send);
    item.appendChild(replyRow);

    return item;
  }

  async function loadSuggestions() {
    // Una sola petición (todas, sin `?status=pending`) y el filtro aquí: el
    // panel necesita también las decididas y las desancladas, así que pedir
    // solo las pendientes obligaría a una segunda llamada para lo mismo.
    try {
      const found = await api(`/pages/${page.id}/suggestions`);
      if (state.noteSession !== session) return; // ya se navegó
      suggestionItems = Array.isArray(found) ? found : [];
      paintSuggestions();
    } catch (e) {
      reportError("No se pudieron cargar las sugerencias", e);
    }
  }

  /** Devuelve el foco al editor cuando la decisión se tomó desde un botón
   * del texto y el widget que lo tenía se ha destruido por el camino.
   *
   * Al aceptar la nota entera se repinta (editor nuevo) y al rechazar se
   * recalculan las decoraciones: en los dos casos el ✓/✗ enfocado desaparece y
   * `document.activeElement` se caía a BODY, así que el siguiente Tab
   * aterrizaba en el contador del panel en vez de seguir en el texto. Solo se
   * devuelve si el botón tenía el foco de verdad (con ratón no lo tiene: el
   * `mousedown` del widget se neutraliza) y si nadie más se lo ha llevado
   * mientras se decidía. */
  function restoreEditorFocus(meta) {
    if (!meta?.hadFocus) return;
    const active = document.activeElement;
    if (active && active !== document.body && active.isConnected) return;
    if (state.current?.kind !== "note" || state.current.id !== page.id) return;
    state.editor?.focus?.();
  }

  /** Aceptar = el servidor aplica el parche al `.md`. Aquí no basta con
   * quitar la marca: la nota se relee del servidor y se repinta con el texto
   * nuevo. Antes se fuerza el guardado pendiente, para que el parche se
   * aplique sobre el mismo texto que se está viendo. */
  async function acceptSuggestion(id, meta) {
    if (state.noteSession !== session) return;
    if (dirty) {
      state.flushPendingSave?.();
      await session.whenQuiet();
      if (state.noteSession !== session) return;
    }
    try {
      await api(`/suggestions/${id}/accept`, { method: "POST" });
    } catch (e) {
      if (e.status === 409) {
        // 409: el ancla ya no está (o ya estaba decidida). No se ha tocado el
        // .md: se avisa y se recarga la lista, que ahora la traerá desanclada.
        showError(
          e.data?.error === "ya_resuelta"
            ? "Esa sugerencia ya estaba decidida. Se ha recargado la lista."
            : "La sugerencia ya no encaja en el texto (el ancla desapareció). Se ha recargado la lista.",
        );
        await loadSuggestions();
        restoreEditorFocus(meta);
        return;
      }
      reportError("No se pudo aceptar la sugerencia", e);
      return;
    }
    if (state.noteSession !== session) return;
    await reloadNoteFromDisk(page.id);
    restoreEditorFocus(meta);
  }

  /** Rechazar no toca el `.md`: basta con que el editor deje de pintarla y
   * el panel la mueva a "ya decididas". */
  async function rejectSuggestion(id, meta) {
    if (state.noteSession !== session) return;
    try {
      await api(`/suggestions/${id}/reject`, { method: "POST" });
    } catch (e) {
      if (e.status === 409) {
        showError("Esa sugerencia ya estaba decidida. Se ha recargado la lista.");
        await loadSuggestions();
        restoreEditorFocus(meta);
        return;
      }
      reportError("No se pudo rechazar la sugerencia", e);
      return;
    }
    await loadSuggestions();
    restoreEditorFocus(meta);
  }

  async function sendReply(id, input, button) {
    const text = input.value.trim();
    if (!text) return;
    button.disabled = true;
    try {
      const updated = await api(`/suggestions/${id}/reply`, {
        method: "POST",
        body: JSON.stringify({ text }),
      });
      const index = suggestionItems.findIndex((s) => s.id === id);
      if (index >= 0) suggestionItems[index] = updated;
      // Se vacía ANTES de repintar: el repintado conserva los borradores que
      // encuentre en el DOM, y si no, la respuesta ya enviada volvería al
      // campo como si estuviera sin mandar.
      input.value = "";
      paintSuggestions();
    } catch (e) {
      button.disabled = false;
      reportError("No se pudo responder a la sugerencia", e);
    }
  }


  renderOutline(); // población inicial -- onChange solo dispara con ediciones, no al montar

  // P8: lo que `/events` necesita saber de esta nota -- si hay algo sin
  // guardar (para no pisarlo), cómo se tira lo pendiente si el usuario pide
  // recargar, y cómo se refrescan los backlinks sin tocar el editor.
  session = {
    id: page.id,
    // Una fila: de qué base es, y cómo repintar su panel de propiedades cuando
    // otra pestaña cambia la fila o el esquema (`handleDatabaseEvent`).
    rowDatabaseId: esFila ? String(page.database_id) : null,
    refreshRowPanel: () => panelFila?.refrescar(),
    hasPendingWork: () => dirty || saving || beaconInFlight !== null,
    /** Para el autoguardado mientras el usuario decide (no pisa el disco). */
    suspendSave() {
      blocked = true;
      clearTimeout(saveTimer);
      saveTimer = null;
      // Salir de la nota (navegar) sí guarda lo de esta pestaña: perder el
      // texto en silencio sería peor. Para tirar lo pendiente está
      // discardPending(), que es lo que usa "Recargar".
      state.flushPendingSave = () => {
        blocked = false;
        doSave();
      };
    },
    /** "Descartar": se queda con lo de esta pestaña y se guarda ya. */
    resumeSave() {
      blocked = false;
      if (dirty) doSave();
    },
    discardPending() {
      clearTimeout(saveTimer);
      dirty = false;
      state.flushPendingSave = null;
      if (state.flushSaveBeacon === flushSaveBeacon) state.flushSaveBeacon = null;
    },
    /** Resuelve cuando no queda ninguna escritura en vuelo (o al agotarse). */
    whenQuiet(maxMs = 3000) {
      if (!saving && beaconInFlight === null) return Promise.resolve();
      return new Promise((resolve) => {
        const started = Date.now();
        const tick = setInterval(() => {
          if ((!saving && beaconInFlight === null) || Date.now() - started > maxMs) {
            clearInterval(tick);
            resolve();
          }
        }, 40);
      });
    },
    refreshBacklinks() {
      api(`/pages/${page.id}/backlinks`)
        .then((fresh) => {
          if (state.noteSession === session) paintBacklinks(fresh);
        })
        .catch((e) => console.error("No se pudieron refrescar los backlinks:", e));
    },
    /** Ha nacido o se ha decidido una sugerencia de esta nota (M3): se relee
     * solo su lista y se repintan el contador, el panel y las marcas del
     * texto. El documento no se toca, así que el cursor y lo que se esté
     * escribiendo siguen donde estaban, y el panel no se abre solo. */
    refreshSuggestions() {
      loadSuggestions();
    },
    /** Una base incrustada en esta nota ha cambiado en otra ventana (M2): se
     * repintan los bloques sin tocar el documento. La valla sigue intacta, el
     * cursor no se mueve y no se dispara ningún guardado. */
    refreshDatabaseEmbeds() {
      state.editor?.refreshDatabaseBlocks?.();
    },
  };
  state.noteSession = session;
  loadSuggestions(); // pendientes en el editor + panel (si no hay, no se pinta nada)
}

// ------------------------------------------------- cambios en disco (P8, SSE)
// El backend vigila VAULT_PATH y cuenta por `/events` cada `.md` que cambia
// por fuera de la aplicación, ya reindexado. Aquí se escucha ese flujo: el
// árbol se refresca siempre (un alta o un borrado puede cambiar qué cuelga de
// qué), los backlinks de la nota abierta también, y el editor solo se recarga
// si la nota que se está mirando es la que cambió y NO hay nada sin guardar.
// Con cambios pendientes no se pisa nada: sale un aviso y decide el usuario.

let vaultEvents = null;

/** Aviso discreto dentro de #main: "esta nota cambió en disco". */
function showRemoteBanner({
  deleted = false,
  session,
  onReload,
  onDismiss,
  text: customText,
  reloadTitle,
  dismissLabel,
  dismissTitle,
}) {
  const main = document.getElementById("main");
  if (!main) return;
  main.querySelector(".note-remote-banner")?.remove();

  const banner = document.createElement("div");
  banner.className = "note-remote-banner";
  banner.setAttribute("role", "status");

  const text = document.createElement("span");
  text.className = "note-remote-banner-text";
  text.textContent = customText || (deleted
    ? "Esta nota se ha borrado en disco. Tienes cambios sin guardar."
    : "Esta nota cambió en disco. Tienes cambios sin guardar.");

  const reloadBtn = document.createElement("button");
  reloadBtn.type = "button";
  reloadBtn.className = "btn";
  reloadBtn.textContent = "Recargar";
  reloadBtn.title = reloadTitle || (deleted
    ? "Cerrar la nota y perder lo que no has guardado"
    : "Recargar desde el disco y perder lo que no has guardado");
  reloadBtn.addEventListener("click", () => {
    banner.remove();
    if (state.noteSession !== session) return; // mientras tanto se navegó
    onReload();
  });

  const dismissBtn = document.createElement("button");
  dismissBtn.type = "button";
  dismissBtn.className = "btn";
  dismissBtn.textContent = dismissLabel || "Descartar";
  dismissBtn.title = dismissTitle || "Seguir con lo que hay en el editor";
  dismissBtn.addEventListener("click", () => {
    banner.remove();
    if (state.noteSession !== session) return;
    onDismiss?.();
  });

  banner.append(text, reloadBtn, dismissBtn);
  main.prepend(banner);
}

/** El usuario ha aceptado perder lo suyo: se tira lo pendiente y se relee. */
function reloadOpenNoteFromDisk(session, { deleted = false } = {}) {
  session.discardPending();
  session.whenQuiet().then(() => {
    if (state.noteSession !== session) return; // ya no es la nota abierta
    if (deleted) {
      volverAlInicio();
      return;
    }
    reloadNoteFromDisk(session.id);
  });
}

/** Relee la nota del disco y la repinta, sin tocar historial ni selección. */
async function reloadNoteFromDisk(id) {
  let page;
  let backlinks;
  try {
    [page, backlinks] = await Promise.all([
      api(`/pages/${id}`),
      api(`/pages/${id}/backlinks`),
    ]);
  } catch (e) {
    // 404: se borró entre el aviso y esta lectura.
    if (state.current?.kind === "note" && state.current.id === id) volverAlInicio();
    else console.error("No se pudo recargar la nota desde el disco:", e);
    return;
  }
  // El usuario pudo navegar mientras llegaba la respuesta.
  if (state.current?.kind !== "note" || state.current.id !== id) return;
  // Y pudo empezar a escribir en ese hueco: tampoco se pisa.
  if (state.noteSession?.hasPendingWork()) {
    const session = state.noteSession;
    session.suspendSave();
    showRemoteBanner({
      session,
      onReload: () => reloadOpenNoteFromDisk(session),
      onDismiss: () => session.resumeSave(),
    });
    return;
  }
  renderNote(page, backlinks);
  markActive(id);
}

/**
 * Refresco del árbol agrupado: un `rsync`/restore de 2.000 ficheros son 2.000
 * eventos y no 2.000 `GET /pages` por pestaña. El árbol se repinta una vez con
 * el estado final, que es el único que se llega a ver.
 */
const TREE_REFRESH_MS = 200;
let treeRefreshTimer = null;

function scheduleTreeRefresh() {
  if (treeRefreshTimer !== null) return;
  treeRefreshTimer = setTimeout(() => {
    treeRefreshTimer = null;
    loadTree();
  }, TREE_REFRESH_MS);
}

/** Un evento `page` de `/events`: `{action, page_id, path}`. */
function handleVaultEvent(data) {
  if (!data || data.type !== "page") return;

  // El árbol siempre, pero agrupado: quitar o añadir un `.md` cambia qué
  // cuelga de qué, y eso se ve en la barra lateral aunque no se toque el
  // editor.
  scheduleTreeRefresh();

  // Cambio hecho por la propia app y ya contado (renombrar la nota abierta
  // desde /page): el árbol ya se ha refrescado arriba, pero no es una edición
  // externa -- sin esto salía el aviso de «cambió en disco» con el guardado
  // pendiente bloqueado justo después de renombrar. Se consume una sola vez y
  // caduca, por si el evento no llega (el vigilante puede estar apagado).
  const propio = state.ownRemoteEvents.get(String(data.page_id));
  if (propio !== undefined) {
    state.ownRemoteEvents.delete(String(data.page_id));
    if (Date.now() - propio < 10000) {
      // Los backlinks de la nota abierta sí se refrescan: el título acaba de
      // cambiar, y un enlace entrante escrito con el nombre viejo ya no la
      // encuentra. Lo que no se hace es recargar el documento ni avisar.
      state.noteSession?.refreshBacklinks?.();
      return;
    }
  }

  const session = state.noteSession;
  if (!session || session.id !== data.page_id) {
    // Otra nota: sus wikilinks pueden ser backlinks de la que está abierta.
    session?.refreshBacklinks();
    return;
  }

  if (data.action === "deleted") {
    if (session.hasPendingWork()) {
      session.suspendSave();
      showRemoteBanner({
        deleted: true,
        session,
        onReload: () => reloadOpenNoteFromDisk(session, { deleted: true }),
        onDismiss: () => session.resumeSave(),
      });
    } else {
      volverAlInicio(); // ya no está: se vuelve al inicio, como al borrarla desde la app
    }
    return;
  }

  if (session.hasPendingWork()) {
    session.suspendSave();
    showRemoteBanner({
      session,
      onReload: () => reloadOpenNoteFromDisk(session),
      onDismiss: () => session.resumeSave(),
    });
    return;
  }
  reloadNoteFromDisk(session.id);
}

/** Un evento `suggestion` de `/events` (M3): una sugerencia ha nacido o se ha
 * decidido. Se refresca lo mínimo: el contador «Sugerencias (N)», el estado del
 * panel y las decoraciones inline de la nota abierta —y solo si el aviso es de
 * esa nota—. No se recarga el documento (el cursor y lo que se esté escribiendo
 * se quedan como están) y el panel no se abre solo: si estaba cerrado, lo único
 * que se nota es el contador. */
function handleSuggestionEvent(data) {
  if (!data || data.type !== "suggestion") return;
  const session = state.noteSession;
  if (!session || session.id !== data.page_id) return; // otra nota: aquí nada
  session.refreshSuggestions?.();
}

/** Un evento `database` de `/events` (M2): una base ha sido borrada,
 * renombrada o ha cambiado de filas en otra pestaña/ventana. Se repintan los
 * bloques incrustados de la nota abierta —y solo si el `.md` menciona esa
 * base—. No se recarga el documento: el texto de la valla no se toca, el
 * cursor se queda donde estaba y el autoguardado no se entera. */
function handleDatabaseEvent(data) {
  if (!data || data.type !== "database" || !data.database_id) return;

  // «Mis tareas» cruza TODAS las bases, así que un cambio de filas en
  // cualquiera de ellas puede cambiar lo que enseña y el contador de la barra:
  // asignarte una tarea, cerrarla o quitarte de ella desde la tabla (esta
  // pestaña) o desde otra ventana. Se refresca antes del «¿hay nota abierta?»
  // de abajo, que no tiene nada que ver con esta vista. El aviso llega una vez
  // por guardado; el retardo corto junta ráfagas (una importación, varias
  // celdas seguidas) en una sola consulta.
  if (data.action === "rows" || data.action === "updated" || data.action === "deleted") {
    scheduleMyTasksRefresh();
  }
  // Renombrar o borrar una base cambia el árbol.
  if (data.action === "updated" || data.action === "deleted") scheduleTreeRefresh();

  // La base abierta en esta pestaña (su tabla, tablero, lista o calendario):
  // se repinta con lo de ahora, salvo el eco de un cambio propio (ya pintado).
  if (state.current?.kind === "database" && String(state.current.id) === String(data.database_id)) {
    if (data.action === "deleted") {
      showNotice("Esta base se ha borrado.");
      volverAlInicio();
      return;
    }
    const eco =
      String(data.database_id) === ultimoCambioPropio.databaseId &&
      Date.now() - ultimoCambioPropio.ts < 3000;
    if (!eco) refrescarBaseAbierta();
    return;
  }

  const session = state.noteSession;
  if (!session) return;
  // La nota abierta es una fila de ESA base: su panel de propiedades se relee
  // (la fila o el esquema han cambiado). Salvo el eco de un cambio propio, que
  // el panel ya tiene pintado.
  if (session.rowDatabaseId === String(data.database_id) && data.action !== "deleted") {
    const eco =
      String(data.database_id) === ultimoCambioPropio.databaseId &&
      Date.now() - ultimoCambioPropio.ts < 3000;
    if (!eco) session.refreshRowPanel?.();
  }
  // El id va dentro de la valla; si no está en esta nota, el aviso no es suyo.
  if (!state.editor?.getValue?.().includes(data.database_id)) return;
  // Eco de una fila que se acaba de añadir o de editar desde el propio bloque:
  // su tabla ya está repintada con lo que devolvió la API (y el aviso, el foco
  // y la celda en edición están puestos), así que volver a pintarla desde cero
  // solo los perdería. El cambio hecho en otra ventana no pasa por aquí y sí
  // repinta.
  if (
    data.action === "rows" &&
    String(data.database_id) === ultimoCambioPropio.databaseId &&
    Date.now() - ultimoCambioPropio.ts < 3000
  ) {
    return;
  }
  // Con el menú de una columna abierto (añadiendo opciones, renombrando), el
  // repintado se aplaza a cuando se cierre: rehacer el bloque ahora se llevaría
  // el ancla del menú y lo cerraría a mitad.
  if (panelDeColumnaAbierto) {
    refrescoDeBasesPendiente = true;
    return;
  }
  session.refreshDatabaseEmbeds?.();
}
let refrescoDeBasesPendiente = false;

/** Repinta la base abierta (aviso de otra pestaña) sin perder el sitio: el
 * scroll de la página y el de cada tabla. Si se está escribiendo en ella (un
 * campo con el foco, un menú o un diálogo abiertos), espera a que se suelte. */
let refrescoDeBaseTimer = null;
function refrescarBaseAbierta() {
  clearTimeout(refrescoDeBaseTimer);
  refrescoDeBaseTimer = setTimeout(async () => {
    refrescoDeBaseTimer = null;
    const vista = state.databaseView;
    if (!vista || state.current?.kind !== "database" || String(state.current.id) !== String(vista.id)) return;
    const main = document.getElementById("main");
    const activo = document.activeElement;
    const ocupado =
      panelDeColumnaAbierto ||
      document.querySelector("dialog[open]") ||
      (activo && main?.contains(activo) && activo.matches("input, select, textarea, [contenteditable=true]"));
    if (ocupado) {
      refrescarBaseAbierta(); // se vuelve a mirar en un momento
      return;
    }
    const scroll = main?.scrollTop ?? 0;
    const tablas = [...(main?.querySelectorAll(".table-wrap, .board, .calendar-wrap") || [])].map(
      (el) => el.scrollLeft,
    );
    await vista.recargar();
    if (!main) return;
    main.scrollTop = scroll;
    [...main.querySelectorAll(".table-wrap, .board, .calendar-wrap")].forEach((el, i) => {
      if (tablas[i] !== undefined) el.scrollLeft = tablas[i];
    });
  }, 250);
}

function connectVaultEvents() {
  if (typeof EventSource === "undefined") return; // sin SSE, sin avisos en vivo
  try {
    vaultEvents = new EventSource("/events");
  } catch (e) {
    console.error("No se pudo abrir /events:", e);
    return;
  }
  // `event: page` llega por su propio nombre, no por `onmessage`.
  vaultEvents.addEventListener("page", (ev) => {
    let data;
    try {
      data = JSON.parse(ev.data);
    } catch {
      return; // un evento que no es JSON se ignora: el siguiente sí vale
    }
    handleVaultEvent(data);
  });
  // `event: suggestion` (M3): la lista de sugerencias de una nota cambió.
  vaultEvents.addEventListener("suggestion", (ev) => {
    let data;
    try {
      data = JSON.parse(ev.data);
    } catch {
      return;
    }
    handleSuggestionEvent(data);
  });
  // `event: tree` (bloque C): una página nueva, borrada o renombrada. Sin el
  // vigilante del disco (producción) nadie más avisaba al árbol.
  vaultEvents.addEventListener("tree", () => scheduleTreeRefresh());
  // `event: database` (M2): una base incrustada ha cambiado en otra ventana.
  vaultEvents.addEventListener("database", (ev) => {
    let data;
    try {
      data = JSON.parse(ev.data);
    } catch {
      return;
    }
    handleDatabaseEvent(data);
  });
  // El navegador reconecta solo; esto solo deja constancia de que se cayó.
  let reconnecting = false;
  vaultEvents.addEventListener("error", () => {
    if (reconnecting) return;
    reconnecting = true;
    console.warn("/events caído; el navegador reintenta solo");
  });
  vaultEvents.addEventListener("open", () => {
    reconnecting = false;
  });
}

/** Destinos posibles de un wikilink, tal como los quiere el autocompletado
 * de "[[" (`setLinkTargets`): {id, title, type} de todas las páginas y bases
 * de datos del vault. Sale del mismo `state.pagesById` que ya usa
 * resolvePage(), así que lo que ofrece la lista es exactamente lo que un
 * wikilink puede resolver después -- ni más ni menos.
 *
 * `id` es el de la PÁGINA (no el `database_id`): es el que espera
 * navigateToPage, y es el que el enlace necesita para tener vista previa. */
function linkTargets() {
  const list = [];
  for (const p of state.pagesById.values()) {
    if (!p.title) continue;
    list.push({ id: p.id, title: p.title, type: p.type || "note" });
  }
  return list;
}

/** Mantiene la lista del autocompletado al día con el árbol. La app la llama
 * desde loadTree() (una sola vez por recarga, no una por nodo). */
function syncLinkTargets() {
  state.editor?.setLinkTargets?.(linkTargets());
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
// La navegación vive en el historial del NAVEGADOR (P18). Cada nota y cada
// base tiene su dirección --`/<id>`, el id y no el título, que cambia--, así
// que recargar en ella abre esa página, se puede mandar el enlace a otra
// persona y atrás/adelante del navegador recorren lo visitado. Los botones de
// la cabecera siguen ahí, pero ya no llevan una pila propia: le piden al
// navegador que mueva SU pila y pintan lo que llega por `popstate`. Dos pilas
// serían dos posibilidades de desincronizarse.
//
// `state.history`/`state.historyPos` no son la pila: son la cuenta de por
// dónde va la app dentro de la del navegador (lo que necesitan los botones
// para saber si pueden ir atrás/adelante), y se guardan en `sessionStorage`
// para que un F5 no los apague.
//
// Lo que sea "ir a otra página de verdad" pasa por goTo(); lo que es solo
// refrescar la página ya abierta (borrar una fila, crear una vista nueva...)
// sigue llamando a selectNote/selectDatabase directamente, para no llenar el
// historial de duplicados.
//
// Las filas de una base también tienen la suya: desde que son páginas
// normales se abren con `goTo("note", id)`, como una nota, y volver a la tabla
// es el «atrás» del navegador.

const LAST_ROUTE_KEY = "mdm_last_route";
// La cuenta de la pila dentro de la pestaña: sobrevive a recargar (que es el
// caso que importa: F5 en una nota y seguir pudiendo ir atrás) y no se mezcla
// entre pestañas.
const NAV_HISTORY_KEY = "mdm_nav_history";
// La marca del documento que escribe la cuenta (P18): un valor al azar por
// carga. Viaja dentro de cada entrada del navegador y en la pila guardada, y
// sirve para una sola cosa: saber si la entrada en la que estamos la escribió
// ESTE documento. Un F5 conserva la entrada con su estado --y la pila
// guardada, con la misma marca--, así que la cuenta sigue valiendo; una
// dirección pegada, un marcador o un atrás a una entrada de otro documento
// traen otra marca (o ninguna), y entonces la cuenta no puede decir dónde
// estamos (ver `pilaAnclada`).
const NAV_GEN = Math.random().toString(36).slice(2);

/** Lo que se guarda en cada entrada del navegador: la posición en la cuenta y
 * la marca del documento que la escribió (ver `NAV_GEN`). */
function marcaDeEntrada(pos) {
  return { pos, gen: NAV_GEN };
}

/** La dirección de un destino: `/<id>` para una nota o una base, `/mis-tareas`
 * para esa vista y `/` para el inicio. */
function rutaDe(kind, id) {
  if (kind === "mytasks") return "/mis-tareas";
  if (kind === "home" || !id) return "/";
  return "/" + encodeURIComponent(id);
}

/** ¿Las dos entradas son el mismo sitio? */
function mismaRuta(a, b) {
  return Boolean(a && b) && a.kind === b.kind && (a.id ?? null) === (b.id ?? null);
}

const ES_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** El destino que corresponde a una dirección, resuelto contra el árbol ya
 * cargado (`state.pagesById`). `null` si la dirección no es de ninguna parte.
 * Un id que no está en el árbol puede ser una fila: se intenta como página.
 *
 * Una base se reconoce por su `database_id` --el id de su página es otro-- y
 * se acepta también el de la página, para que un enlace copiado desde
 * cualquier sitio acabe en la base. */
function destinoDeRuta(pathname = location.pathname) {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/") return { kind: "home", id: null };
  if (path === "/mis-tareas") return { kind: "mytasks", id: null };
  let id;
  try {
    id = decodeURIComponent(path.replace(/^\//, ""));
  } catch {
    return null;
  }
  if (!id || id.includes("/")) return null;
  const node = state.pagesById.get(id);
  if (node) {
    return node.type === "database"
      ? { kind: "database", id: node.database_id }
      : { kind: "note", id: node.id };
  }
  const base = [...state.pagesById.values()].find(
    (n) => n.type === "database" && n.database_id === id,
  );
  if (base) return { kind: "database", id: base.database_id };
  // Una fila de base no está en el árbol (vive en su tabla), pero es una
  // página con su dirección: se abre como nota y el servidor dice si existe.
  return ES_UUID.test(id) ? { kind: "note", id } : null;
}

/** «Abrir donde lo dejaste»: el último sitio abierto en ESTE navegador, en
 * `localStorage` (una preferencia de pantalla, como el tema; no es un dato del
 * vault). El inicio no se guarda: ir a Inicio es ir a Inicio. */
function recordarRuta(kind, id) {
  try {
    if (kind === "home") localStorage.removeItem(LAST_ROUTE_KEY);
    else localStorage.setItem(LAST_ROUTE_KEY, JSON.stringify({ kind, id: id ?? null }));
  } catch {
    // sin localStorage (modo privado, cuota llena) se pierde «donde lo
    // dejaste» y nada más: la app funciona igual
  }
}

function rutaRecordada() {
  try {
    const guardada = JSON.parse(localStorage.getItem(LAST_ROUTE_KEY) || "null");
    if (!guardada || typeof guardada.kind !== "string") return null;
    return { kind: guardada.kind, id: guardada.id ?? null };
  } catch {
    return null;
  }
}

/** La cuenta de la pila, a `sessionStorage`. Si el navegador no la deja
 * guardar, la app sigue: solo se pierde saber si hay «adelante» tras recargar. */
function guardarPila() {
  try {
    sessionStorage.setItem(
      NAV_HISTORY_KEY,
      JSON.stringify({ pos: state.historyPos, gen: NAV_GEN, pila: state.history }),
    );
  } catch {
    // sin sessionStorage: la cuenta se queda en memoria
  }
}

function pilaGuardada() {
  try {
    const datos = JSON.parse(sessionStorage.getItem(NAV_HISTORY_KEY) || "null");
    if (!datos || !Array.isArray(datos.pila) || typeof datos.pos !== "number") return null;
    if (datos.pila.some((e) => !e || typeof e.kind !== "string")) return null;
    if (datos.pila.length === 0) return null;
    return {
      pos: Math.max(0, Math.min(datos.pos, datos.pila.length - 1)),
      // La marca del documento que la guardó, para poder comprobar que la
      // entrada del navegador en la que estamos es la que la pila describe.
      gen: typeof datos.gen === "string" ? datos.gen : null,
      pila: datos.pila.map((e) => ({ kind: e.kind, id: e.id ?? null })),
    };
  } catch {
    return null;
  }
}

/** Pinta un destino SIN tocar la dirección. Devuelve si se pudo: si la página
 * no existe (un enlace viejo), no se pinta nada y no se mueve la barra. */
async function pintar(kind, id, { focus = null } = {}) {
  if (kind === "database") return selectDatabase(id, { focus });
  if (kind === "mytasks") {
    await showMisTareas();
    return true;
  }
  if (kind === "home" || !id) {
    await showEmpty();
    return true;
  }
  return selectNote(id);
}

async function goTo(kind, id, { replace = false } = {}) {
  // Abrir una base deja el foco en el primer campo del alta. Volver con atrás o
  // adelante no pasa por aquí (lo pinta `seguirAlNavegador` sin tocar el foco,
  // que es del usuario, no del historial).
  const pintada = await pintar(kind, id, { focus: "addRow" });
  if (!pintada) return false;
  // La sección de la barra lateral se marca (o se desmarca) como la actual con
  // la misma navegación que todo lo demás.
  paintMyTasks();
  if (replace) {
    state.history[state.historyPos] = { kind, id: id ?? null };
    history.replaceState(marcaDeEntrada(state.historyPos), "", rutaDe(kind, id));
  } else {
    // Como en un navegador: navegar desde una posición anterior descarta el
    // «adelante» pendiente. En la pila del navegador lo hace él; en nuestra
    // cuenta, este `slice`.
    state.history = state.history.slice(0, state.historyPos + 1);
    state.history.push({ kind, id: id ?? null });
    state.historyPos = state.history.length - 1;
    history.pushState(marcaDeEntrada(state.historyPos), "", rutaDe(kind, id));
  }
  guardarPila();
  recordarRuta(kind, id);
  updateNavButtons();
  return true;
}

function goHome() {
  goTo("home");
}

/** Dónde estamos dentro de la pila del navegador, si el navegador lo dice.
 *
 * La `Navigation API` (Chromium 102 y posteriores) expone el índice exacto de
 * la entrada actual y la pila entera: es la única forma de saber si hay
 * atrás/adelante sin adivinar. Sin ella devuelve `null` y se cae a la cuenta
 * de la app, que es exacta mientras sepa que las entradas son suyas.
 */
function posicionEnLaPila() {
  const nav = window.navigation;
  const actual = nav && nav.currentEntry;
  if (!actual || typeof actual.index !== "number") return null;
  return { pos: actual.index, total: nav.entries().length };
}

/** ¿Se puede ir atrás? La cabecera no puede contradecir al navegador: si él
 * tiene atrás --aunque sea a una entrada que no puso esta app, como la página
 * desde la que se abrió-- el botón tiene que estar vivo; y al revés, un ← que
 * no hace nada es una mentira peor que uno apagado.
 *
 * Primero la cuenta de la app (`pos > 0` es prueba: esas entradas existen) y,
 * cuando no puede probarlo, el navegador: `Navigation API` si está, y si no
 * `history.length` (hay más entradas que esta, que es lo único que se sabe).
 * Con la pila anclada y sin `Navigation API`, `pos 0` es la primera página de
 * la app y el botón se apaga: volver más atrás sería salirse de ella.
 */
function puedeRetroceder() {
  if (state.historyPos > 0) return true;
  const real = posicionEnLaPila();
  if (real) return real.pos > 0;
  if (state.pilaAnclada) return false;
  return history.length > 1;
}

/** ¿Se puede ir adelante?
 *
 * `pos` por debajo del final de la cuenta es prueba de que hay algo delante.
 * Que `pos` SEA el final no prueba lo contrario: al recargar en una página
 * puede quedar por delante una entrada que no está en la pila de este
 * documento (se navegó a otra nota desde otro documento y se volvió con
 * atrás). Por eso ahí se le pregunta al navegador, y sin `Navigation API` se
 * apaga: `history.length` dice cuántas entradas hay, no dónde estamos.
 */
function puedeAvanzar() {
  if (state.historyPos < state.history.length - 1) return true;
  const real = posicionEnLaPila();
  if (real) return real.pos < real.total - 1;
  return false;
}

function goBack() {
  if (!puedeRetroceder()) return;
  // No se pinta aquí: se le pide al navegador que retroceda y `popstate` hace
  // el resto. Así la cabecera y el botón del navegador son el mismo camino.
  history.back();
}

function goForward() {
  if (!puedeAvanzar()) return;
  history.forward();
}

/** Vuelve al inicio **cambiando la dirección** (la página abierta ya no
 * existe: dejarla en la barra sería dejar un enlace roto en el historial). Se
 * sustituye la entrada, no se añade una: «atrás» no debe volver a lo borrado. */
function volverAlInicio() {
  goTo("home", null, { replace: true }).catch((e) =>
    reportError("No se pudo volver al inicio", e),
  );
}

/** El navegador ha cambiado de entrada (atrás/adelante, o una entrada que no
 * es nuestra): se pinta lo que dice la dirección. No se empuja nada -- esa
 * entrada ya existe. */
async function seguirAlNavegador(event) {
  const destino = destinoDeRuta();
  if (!destino) {
    // La dirección ya no es de ninguna página (se borró mientras tanto).
    showNotice("Esa página ya no existe: te llevo al inicio.");
    await goTo("home", null, { replace: true });
    return;
  }
  // La posición la dice la propia entrada (cada pushState guarda la suya).
  const pos = event.state && typeof event.state.pos === "number" ? event.state.pos : null;
  if (pos !== null && pos >= 0 && pos < state.history.length) state.historyPos = pos;
  // Si la entrada no es de esta pestaña (viene de antes de una recarga sin
  // `sessionStorage`, o la pila se perdió), se reancla en su destino en vez de
  // inventar una posición.
  if (!mismaRuta(state.history[state.historyPos], destino)) {
    const i = state.history.findIndex((e) => mismaRuta(e, destino));
    state.historyPos = i >= 0 ? i : state.history.push(destino) - 1;
  }
  const pintada = await pintar(destino.kind, destino.id, { focus: null });
  if (!pintada) {
    await goTo("home", null, { replace: true });
    return;
  }
  // La entrada se queda con su posición y con la dirección canónica (una base
  // abierta por el id de su página pasa a su `database_id`).
  history.replaceState(
    marcaDeEntrada(state.historyPos),
    "",
    rutaDe(destino.kind, destino.id),
  );
  paintMyTasks();
  recordarRuta(destino.kind, destino.id);
  guardarPila();
  updateNavButtons();
}

window.addEventListener("popstate", (event) => {
  seguirAlNavegador(event).catch((e) => reportError("No se pudo cambiar de página", e));
});

// Volver a esta página desde la caché del navegador (atrás/adelante sin
// recargarla: `pageshow` con `persisted`) no reejecuta nada, así que los
// botones se quedarían como los dejó esta página al irse. Y la cuenta puede
// haberse quedado corta: mientras esta página dormía, otro documento empujó
// entradas delante (se navegó a otra nota y se volvió), y eso la cuenta no lo
// puede saber. Se marca sin anclar y se vuelve a preguntar.
window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  state.pilaAnclada = false;
  updateNavButtons();
});

/** El arranque: cargado el árbol, se abre lo que dice la dirección (P18).
 *
 * - En `/<id>` manda la dirección -- es lo que hace que un enlace compartido
 *   abra esa nota--; si esa página ya no existe, se entra limpio en Inicio y
 *   se dice, que un enlace muerto no puede dejar la pantalla en blanco.
 * - En `/` manda «donde lo dejaste» (`localStorage`). Si lo recordado ya no
 *   existe, se entra limpio en Inicio **sin error**: no es un fallo, es una
 *   página que se borró.
 * - La pila de la pestaña se recupera si la última entrada era esta misma
 *   dirección: recargar no apaga «atrás» ni «adelante».
 */
async function abrirAlArrancar() {
  const arbolListo = await loadTree();
  if (!arbolListo) {
    // Sin árbol no hay direcciones que resolver: se pinta el inicio y se deja
    // la dirección como está (recargar cuando la API conteste).
    await showEmpty();
    return;
  }
  let destino = destinoDeRuta();
  if (!destino) {
    showNotice("Esa página ya no existe: te llevo al inicio.");
    destino = { kind: "home", id: null };
  } else if (destino.kind === "home") {
    const recordada = rutaRecordada();
    const valida = recordada ? destinoDeRuta(rutaDe(recordada.kind, recordada.id)) : null;
    if (valida) destino = valida;
  }
  const guardada = pilaGuardada();
  // La pila guardada solo vale si la entrada en la que estamos es la que la
  // pila describe: cada entrada lleva la marca del documento que la escribió y
  // un F5 conserva las dos cosas. Si el documento se cargó por una navegación
  // que no fue nuestra --pegar una URL en la barra, abrir un marcador, volver
  // con atrás a una entrada de otro documento--, la cuenta no dice dónde
  // estamos dentro de la pila del navegador, y darlo por hecho apagaba ← y →
  // aunque el navegador sí pudiera.
  const mismaEntrada =
    guardada !== null &&
    history.state !== null &&
    history.state.gen === guardada.gen &&
    history.state.pos === guardada.pos &&
    mismaRuta(guardada.pila[guardada.pos], destino);
  if (mismaEntrada) {
    state.history = guardada.pila;
    state.historyPos = guardada.pos;
    state.pilaAnclada = true;
  } else {
    state.history = [destino];
    state.historyPos = 0;
    state.pilaAnclada = false;
  }
  // La entrada de arranque es de la app: se queda con su posición y con la
  // dirección canónica (en `/` eso pasa a `/<id>` cuando hay «donde lo
  // dejaste»; una base abierta por el id de su página, a su `database_id`).
  history.replaceState(
    marcaDeEntrada(state.historyPos),
    "",
    rutaDe(destino.kind, destino.id),
  );
  const pintada = await pintar(destino.kind, destino.id, { focus: null });
  if (!pintada) {
    // La dirección apuntaba a algo que el árbol tenía y el servidor ya no: se
    // entra limpio, sin dejar la barra en un enlace roto.
    state.history = [{ kind: "home", id: null }];
    state.historyPos = 0;
    state.pilaAnclada = false; // el navegador puede tener entradas suyas detrás
    history.replaceState(marcaDeEntrada(0), "", "/");
    await showEmpty();
  } else {
    recordarRuta(destino.kind, destino.id);
  }
  paintMyTasks();
  guardarPila();
  updateNavButtons();
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
  if (backBtn) backBtn.disabled = !puedeRetroceder();
  if (fwdBtn) fwdBtn.disabled = !puedeAvanzar();
  if (upBtn) upBtn.disabled = !getCurrentParentId();
  // «Inicio» (en «Mis cosas») se marca como «Mis tareas» cuando es donde estás:
  // el inicio es no tener nada abierto (`showEmpty`).
  const homeBtn = document.getElementById("btn-nav-home");
  if (homeBtn) homeBtn.setAttribute("aria-current", !state.current ? "page" : "false");
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
  const titleIcon = document.createElement("span");
  paintPageIcon(titleIcon, data.type || "note", data.icon);
  title.append(titleIcon, ` ${data.title}`);

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

  // Modo "naming": el usuario eligió "Página" o "Nueva base" y ahora está
  // escribiendo el nombre directamente en el documento -- aquí solo hace falta
  // un aviso flotante, no una lista (no hay nada que elegir).
  if (menuState.kind === "naming") {
    const hint = document.createElement("div");
    hint.className = "mdm-slash-naming";
    if (menuState.target === "rename") {
      // Se dice lo que va a pasar ANTES de hacerlo: se renombra esa página (su
      // título y su fichero), no se crea una nueva.
      hint.textContent = `Renombrar «${menuState.renameTitle}» → nombre nuevo · Enter renombra la página · Esc cancela`;
    } else {
      hint.textContent =
        menuState.target === "database"
          ? "Nombre de la base · Enter para crear · Esc para cancelar"
          : "Nombre de la página · Enter para crear y abrirla · Esc cancela";
    }
    positionFloating(hint, menuState.x, menuState.y);
    slashMenuEl = hint;
    return;
  }

  const menu = document.createElement("div");
  menu.className = "mdm-slash-menu";

  // Aviso corto encima de la lista: lo manda el selector de bases cuando el
  // vault no tiene ninguna (la lista sigue teniendo la opción de crear).
  if (menuState.note) {
    const note = document.createElement("div");
    note.className = "search-empty";
    note.textContent = menuState.note;
    menu.appendChild(note);
  }

  // Las opciones van en su propia caja con scroll, y la fila de «… y N más»
  // queda FUERA de ella: dentro no se vería justo cuando hace falta (habría
  // que bajar por 50 filas para descubrir que la lista sigue).
  const lista = document.createElement("div");
  lista.className = "mdm-slash-menu-list";
  menu.appendChild(lista);

  if (menuState.options.length === 0) {
    // Un vault sin páginas ya trae su explicación en `note` («Aún no hay
    // ninguna página…»): añadir «Sin coincidencias» sería decir dos veces lo
    // mismo, y lo segundo es mentira (no hay nada que buscar). Sin aviso,
    // «Sin coincidencias» es lo único que se puede decir.
    if (!menuState.note) {
      const vacio = document.createElement("div");
      vacio.className = "search-empty";
      vacio.textContent = "Sin coincidencias.";
      lista.appendChild(vacio);
    }
  } else {
    menuState.options.forEach((opt, i) => {
      const item = document.createElement("div");
      item.className = "mdm-slash-menu-item" + (i === menuState.selectedIndex ? " active" : "");

      const label = document.createElement("span");
      label.className = "mdm-slash-menu-label";
      label.textContent = opt.label;
      const right = document.createElement("span");
      // Las opciones del selector de bases llevan su carpeta en vez de la
      // abreviatura "/x": es lo único que distingue dos bases con el mismo
      // título ("Contactos" en Bases y en Clientes).
      if (opt.hint) {
        right.className = "mdm-slash-menu-hint";
        right.textContent = opt.hint;
      } else {
        right.className = "mdm-slash-menu-abbrev";
        right.textContent = `/${opt.abbrev}`;
      }
      item.append(label, right);

      // mousedown (no click) + preventDefault: que el editor no pierda el
      // foco/selección antes de que onClick aplique el tipo elegido.
      item.addEventListener("mousedown", (e) => {
        e.preventDefault();
        opt.onClick();
      });
      lista.appendChild(item);
    });
  }

  // Fila final con lo que el tope deja fuera (selector de /page en un vault
  // grande, ver capPageChoices en editor-src/page-choice.js). No es una
  // opción -- no se puede elegir ni resaltar, y el teclado no la visita --:
  // solo avisa de que la lista sigue y de que hay que escribir para acotar.
  if (menuState.overflow > 0) {
    const more = document.createElement("div");
    more.className = "mdm-slash-menu-more";
    more.textContent = `… y ${menuState.overflow} más, sigue escribiendo`;
    menu.appendChild(more);
  }

  positionFloating(menu, menuState.x, menuState.y);

  // Con la lista filtrada la opción resaltada por teclado (flechas) puede
  // quedar fuera del área visible de la lista (max-height + overflow-y: auto
  // en .mdm-slash-menu-list) -- sin esto, ArrowDown/ArrowUp seguían moviendo
  // la selección pero no se veía moverse.
  lista.querySelector(".mdm-slash-menu-item.active")?.scrollIntoView({ block: "nearest" });

  slashMenuEl = menu;
}

// --------------------------------------------------- bases del comando "/base"
// El editor pide esta lista al abrir el selector de "/base" (ver
// slashMenuExtension en entry.js). El `id` es el de la fila `databases` -- el
// que espera /databases/{id} y el que va dentro del bloque ```mdm-database --,
// no el de la página que representa la base.

/** ¿Los dos títulos son el mismo nombre para el vault (NFC, sin mayúsculas)? */
function sameTitle(a, b) {
  const norm = (s) => String(s ?? "").normalize("NFC").toLowerCase();
  return norm(a) === norm(b);
}

/** Carpeta en la que vive una base, deducida del árbol.
 *
 * El árbol no trae rutas: la jerarquía son carpetas (D4), así que la cadena de
 * notas padre es la carpeta. En la raíz, `Bases/` (donde viven las bases
 * sueltas); colgando de una nota, sus títulos -- bastante para no confundir dos
 * bases que se llamen igual.
 */
function nodeFolderLabel(node) {
  const partes = [];
  let actual = node;
  // Tope de seguridad: esto corre al abrir un menú, no puede colgarse.
  for (let i = 0; i < 32 && actual?.parent_id; i++) {
    actual = state.pagesById.get(actual.parent_id);
    if (!actual) break;
    partes.unshift(actual.title || "Sin título");
  }
  return partes.length ? partes.join(" / ") : "Bases";
}

/** Las bases del vault, por título, con su carpeta. */
function listDatabaseEntries() {
  const bases = [];
  for (const node of state.pagesById.values()) {
    if (node.type !== "database" || !node.database_id) continue;
    bases.push({
      id: node.database_id,
      title: node.title || "Sin título",
      folder: nodeFolderLabel(node),
    });
  }
  return bases.sort(
    (a, b) => a.title.localeCompare(b.title, "es") || a.folder.localeCompare(b.folder, "es"),
  );
}

/** Carpeta de una página, deducida del árbol (en la raíz: vacío). Es lo que
 * distingue dos páginas con el mismo título en el selector de /page. */
function pageFolderLabel(node) {
  const partes = [];
  let actual = node;
  for (let i = 0; i < 32 && actual?.parent_id; i++) {
    actual = state.pagesById.get(actual.parent_id);
    if (!actual) break;
    partes.unshift(actual.title || "Sin título");
  }
  return partes.join(" / ");
}

/** Las páginas del vault (notas y bases) para el selector del comando /page.
 * El `id` es el de la página -- el del árbol y /pages/{id} --, no el de la fila
 * `databases`. */
function listPageEntries() {
  const paginas = [];
  for (const node of state.pagesById.values()) {
    paginas.push({
      id: node.id,
      title: node.title || "Sin título",
      type: node.type || "note",
      parent_id: node.parent_id ?? null,
      folder: pageFolderLabel(node),
    });
  }
  return paginas;
}

/** ¿Ya hay una página (nota o base) con ese nombre en la carpeta de `parentId`?
 *
 * El servidor desambigua en silencio (`Contactos (2).md`) porque el disco no
 * admite dos ficheros con el mismo nombre; al crear desde la nota eso sería el
 * defecto M1 -- un duplicado que nadie pidió --, así que se pregunta antes y no
 * se llega a la API.
 */
function childTitleTaken(parentId, title) {
  for (const node of state.pagesById.values()) {
    if (String(node.parent_id || "") !== String(parentId)) continue;
    if (sameTitle(node.title, title)) return node;
  }
  return null;
}

/** Lo que hace falta para escribir el bloque de una base que ya existe: su id y
 * la vista con la que se abre.
 *
 * Vista: la primera, que es la que abre el bloque cuando no dice ninguna (la
 * «vista activa» no se guarda en ningún sitio: al abrir la base se pinta la
 * primera). Se escribe solo si hay más de una -- con una sola, dejarla fuera
 * hace que el .md siga abriendo la que toca aunque mañana se añada otra.
 */
async function databaseBlockFor(databaseId) {
  let database;
  let views;
  try {
    [database, views] = await Promise.all([
      api(`/databases/${databaseId}`),
      api(`/databases/${databaseId}/views`),
    ]);
  } catch (e) {
    if (e?.status !== 401) reportError("No se pudo leer la base de datos", e);
    return null;
  }
  return {
    id: database.id,
    title: database.title,
    view: views.length > 1 ? views[0].name : null,
  };
}

// ------------------------------------------------------------- menú contextual
// Genérico: lo usan tanto los bloques del editor como la selección múltiple
// de páginas en el árbol (ver más abajo).

let currentContextMenu = null;
let contextMenuOpener = null; // a quién devolver el foco al cerrar el menú

function closeContextMenu() {
  if (currentContextMenu) {
    currentContextMenu.remove();
    currentContextMenu = null;
  }
  document.removeEventListener("keydown", onContextMenuKeydown);
  // Abierto con el teclado, el foco se fue al menú: vuelve a quien lo abrió
  // (la fila del árbol) en vez de quedarse en <body>.
  if (contextMenuOpener?.isConnected) contextMenuOpener.focus();
  contextMenuOpener = null;
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
  contextMenuOpener = document.activeElement;
  setTimeout(() => {
    document.addEventListener("click", closeContextMenu, { once: true });
    document.addEventListener("keydown", onContextMenuKeydown);
    // El primer elemento queda enfocado: el menú ya se maneja con Tab,
    // Enter/Espacio y Escape (sus opciones son <button> de verdad).
    menu.querySelector(".mdm-context-menu-item")?.focus();
  }, 0);
}

// -------------------------------------------------------------- databases

// La pestaña con la que se dejó cada base (id de vista, o `null` para «Todas»),
// en `localStorage`: es la mitad de «abrir donde lo dejaste» que vive DENTRO de
// una base, y una preferencia de este navegador, no un dato del vault. Sin la
// clave en el mapa, esa base no se ha abierto nunca aquí.
const VIEWS_KEY = "mdm_last_views";

function vistaRecordada(databaseId) {
  try {
    const mapa = JSON.parse(localStorage.getItem(VIEWS_KEY) || "{}") || {};
    if (!Object.prototype.hasOwnProperty.call(mapa, databaseId)) return undefined;
    return mapa[databaseId] ?? null;
  } catch {
    return undefined;
  }
}

function recordarVista(databaseId, viewId) {
  try {
    const mapa = JSON.parse(localStorage.getItem(VIEWS_KEY) || "{}") || {};
    mapa[databaseId] = viewId ?? null;
    localStorage.setItem(VIEWS_KEY, JSON.stringify(mapa));
  } catch {
    // sin localStorage se abre siempre en la primera vista; nada más
  }
}

/** La pestaña con la que abrir una base: la que se dejó si sigue existiendo
 * («Todas» incluida), y si no la primera. */
function vistaInicial(databaseId, views) {
  const recordada = vistaRecordada(databaseId);
  if (recordada === undefined) return views[0]?.id ?? null;
  if (recordada === null) return null;
  return views.some((v) => v.id === recordada) ? recordada : (views[0]?.id ?? null);
}

async function selectDatabase(id, { focus = null } = {}) {
  const previous = state.current;
  state.current = { kind: "database", id };
  markActive(id);
  marcarCarga(true);
  try {
    const [database, views] = await Promise.all([api(`/databases/${id}`), api(`/databases/${id}/views`)]);
    // «Abrir donde lo dejaste» también dentro de una base: su pestaña (ver
    // `vistaInicial`), no siempre la primera.
    await renderDatabase(database, views, vistaInicial(id, views), { focus });
    return true;
  } catch (e) {
    // Igual que en selectNote: si falla la carga, no se ha pintado nada y el
    // estado vuelve a donde estaba.
    state.current = previous;
    markActive(previous ? previous.id : null);
    reportError("No se pudo abrir la base de datos", e);
    return false;
  } finally {
    marcarCarga(false);
  }
}

/** Las entradas que ya ocupan un nombre en la carpeta de esta base: las
 * otras bases de su mismo padre y —si cuelga de una nota— las notas
 * hermanas, que comparten carpeta (`Notas/<nota>/`). Dentro de una misma
 * carpeta es donde el backend resuelve la colisión (`unique_stem`). */
function hermanasDeLaBase(database) {
  const propia = [...state.pagesById.values()].find(
    (n) => n.type === "database" && n.database_id === database.id,
  );
  const carpeta = propia?.parent_id ?? null;
  const mapa = new Map();
  for (const n of state.pagesById.values()) {
    if (n.type === "database" && n.database_id === database.id) continue;
    if ((n.parent_id ?? null) !== carpeta) continue;
    // En la raíz las notas viven en `Notas/` y las bases en `Bases/`: solo
    // comparten carpeta cuando la base cuelga de una nota.
    if (n.type !== "database" && !carpeta) continue;
    mapa.set(nombreNormalizado(n.title), { nodo: n, ruta: rutaDePagina(n.id) });
  }
  return mapa;
}

/** El aviso de nombre ocupado al renombrar una base. */
function avisoDeNombreOcupado(ocupada, accion) {
  const que = ocupada.nodo.type === "database" ? "base de datos" : "página";
  return `Ya hay una ${que} «${ocupada.nodo.title}» en esa carpeta (${ocupada.ruta}). ${accion}`;
}

/** Cómo se llama cada tipo de propiedad en la interfaz.
 *
 * `text`, `select`... son la clave interna del `.md` y del schema; al usuario
 * se le enseña el nombre del tipo, igual que el nombre de la propiedad. No se
 * escribe «snake_case» en ninguna parte de la interfaz. */
const TIPOS_VISIBLES = {
  text: "Texto",
  number: "Número",
  date: "Fecha",
  select: "Selección",
  multiselect: "Multiselección",
  checkbox: "Casilla",
  url: "Enlace",
  relation: "Relación",
  parent_row: "Subtarea de",
  person: "Persona",
};

function tipoVisible(tipo) {
  return TIPOS_VISIBLES[tipo] || "Texto";
}

// --------------------------------------------------------------- subtareas
// Una fila puede colgar de otra de su MISMA base (propiedad `parent_row`). El
// servidor decide las reglas —madre válida, sin ciclos, qué cuenta como
// cerrada— y manda la jerarquía resuelta en cada fila: `parent_id` y
// `children {total, closed}`. Aquí solo se pinta: la indentación, el contador
// de la tarjeta y la lista de subtareas de la fila abierta. Duplicar la regla
// de «cerrada» en el cliente sería tener dos verdades que pueden discrepar.

/** La clave de la celda que guarda la fila madre, o `null` si la base no tiene
 * jerarquía. La propiedad se llama como el usuario quiera («Subtarea de»,
 * «Paso de»): lo que la identifica es su tipo. */
function claveDeJerarquia(database) {
  const prop = (database?.schema_def || []).find(
    (p) => p && p.key && p.type === "parent_row",
  );
  return prop ? String(prop.key) : null;
}

/** La regla de qué cuenta como fila cerrada en esta base, o `null`.
 *
 * La resuelve el servidor (`closed: {property, values, source}`): el bloque
 * `closed:` del `.md` o, si no lo hay, el marcador `done` viejo (`source:
 * "done"`, compatibilidad de lectura). El cliente NO vuelve a interpretarlo:
 * duplicar la regla sería tener dos verdades que pueden discrepar. */
function reglaDeCierre(database) {
  const regla = database?.closed;
  if (!regla || !regla.property) return null;
  return {
    property: String(regla.property),
    values: Array.isArray(regla.values) ? regla.values : [],
    source: regla.source === "done" ? "done" : "base",
  };
}

/** La propiedad que dice si una fila está cerrada, o `null`. Solo sirve para
 * enseñar el estado en la lista de subtareas: quien cuenta las cerradas es el
 * servidor. */
function propiedadDeCierre(database) {
  const regla = reglaDeCierre(database);
  if (!regla) return null;
  return (database?.schema_def || []).find((p) => p.key === regla.property) || null;
}

/** El nivel de filas de una vista: `roots` (solo madres) o `all`.
 *
 * Misma regla que `app.hierarchy.level`: manda lo que diga el `config` de la
 * vista (en el `.md`); sin la clave, tabla y tablero enseñan solo madres y la
 * lista y el calendario, todas. Una base sin jerarquía no tiene nivel. */
function nivelDeVista(view, database) {
  if (!claveDeJerarquia(database)) return "all";
  const guardado = String(view?.config?.parent_level || "").toLowerCase();
  if (guardado === "roots" || guardado === "all") return guardado;
  const tipo = String(view?.type || "");
  return tipo === "table" || tipo === "board" ? "roots" : "all";
}

/** `{id: profundidad}` de las filas VISIBLES: 0 es una raíz, 1 una hija, 2 una
 * nieta. Se calcula de una pasada por fila y se corta si un `.md` editado a
 * mano trae un ciclo (el servidor no deja crearlo). */
function mapaDeProfundidades(rows, clave) {
  const mapa = new Map();
  if (!clave || !Array.isArray(rows)) return mapa;
  const porId = new Map(rows.map((r) => [String(r.id), r]));
  rows.forEach((row) => {
    let prof = 0;
    const vistas = new Set([String(row.id)]);
    let actual = row.parent_id ? porId.get(String(row.parent_id)) : null;
    while (actual && !vistas.has(String(actual.id))) {
      vistas.add(String(actual.id));
      prof += 1;
      actual = actual.parent_id ? porId.get(String(actual.parent_id)) : null;
    }
    mapa.set(String(row.id), prof);
  });
  return mapa;
}

/** El «1 de 3» de una fila con subtareas, o `null` si no tiene ninguna. */
function contadorDeSubtareas(row) {
  const total = row?.children?.total || 0;
  if (!total) return null;
  return `${row.children.closed || 0} de ${total}`;
}

/** Cómo se enseña cada tipo de vista en su pestaña: icono y nombre legible. */
const TIPOS_DE_VISTA = {
  table: { icono: "i-table", nombre: "Tabla" },
  board: { icono: "i-board", nombre: "Tablero" },
  calendar: { icono: "i-calendar", nombre: "Calendario" },
  list: { icono: "i-list", nombre: "Lista" },
};

async function renderDatabase(database, views, activeViewId, { focus = null } = {}) {
  // Una fila escrita en la fantasma que aún se está guardando (se cambió de
  // vista con ella a medias): se espera, para pintar la vista ya con ella.
  if (altaEnVuelo) await altaEnVuelo;
  // El selector de una celda de persona necesita el directorio: se pide al
  // abrir una base que tenga alguna, antes de que nadie pinte una celda.
  if ((database.schema_def || []).some((p) => p.type === "person")) {
    // `forzar`: el nombre visible de una persona puede haber cambiado desde el
    // último pintado (la celda lo lee del servidor, pero los desplegables de
    // alta y de la ficha salen del directorio cacheado).
    cargarPersonas({ forzar: true }).catch(() => {});
  }
  const main = clearMain();
  // Repinta esta misma vista (con la pestaña activa) leyendo del servidor: lo
  // usan el tablero tras mover una tarjeta y el diálogo de propiedades, para no
  // enseñar nada que el `.md` no tenga ya. El fallo se avisa aquí: quien
  // repinta no espera la promesa, y una rechazada sin dueño no se ve.
  const refresh = () =>
    renderDatabase(database, views, activeViewId).catch((e) =>
      reportError("No se pudo refrescar la base de datos", e),
    );

  // Guardar el esquema reescribe en el servidor lo que depende de cada
  // propiedad: `group_by`, `date_property`, `filters` y `sort` de las vistas.
  // Repintar con el array de `views` capturado al pintar enseñaría la
  // configuración vieja sobre los datos nuevos (el tablero se quedaba en una
  // columna «sin valor» y el calendario pintaba las fechas de otra propiedad),
  // así que este repintado vuelve a pedir la base y las vistas.
  const recargarVistas = () =>
    (async () => {
      const [fresca, vistas] = await Promise.all([
        api(`/databases/${database.id}`),
        api(`/databases/${database.id}/views`),
      ]);
      // `null` es la pestaña «Todas (sin vista)», un estado legítimo: sólo se
      // cae a la primera vista si la activa era una vista y ya no viene.
      const activa =
        activeViewId === null || vistas.some((v) => v.id === activeViewId)
          ? activeViewId
          : (vistas[0]?.id ?? null);
      await renderDatabase(fresca, vistas, activa);
    })().catch((e) => reportError("No se pudo refrescar la base de datos", e));

  // Para el aviso de otra pestaña (`refrescarBaseAbierta`): repintar esta
  // misma base con lo de ahora.
  state.databaseView = { id: database.id, recargar: recargarVistas };

  const header = document.createElement("div");
  header.className = "db-header";

  const title = document.createElement("input");
  title.className = "db-title";
  title.value = database.title;
  // Enter guarda igual que salir del campo (como el título de una nota).
  // Antes solo guardaba el `blur`, así que Enter dejaba el campo con el
  // nombre nuevo y el árbol y el `.md` con el viejo, sin decir nada (M2).
  const guardarTitulo = async () => {
    const newTitle = title.value.trim() || "Sin título";
    if (newTitle === database.title) return;
    // Lo que ya ocupa un nombre en esta carpeta, antes de tocar nada: sirve
    // para la comprobación de antes y para la de después.
    const hermanas = hermanasDeLaBase(database);
    const ocupada = hermanas.get(nombreNormalizado(newTitle));
    if (ocupada) {
      // El backend resolvería la colisión con un sufijo («Clientes (2)») y
      // quedarían dos bases con el mismo nombre a la vista: se avisa y no
      // se toca nada.
      alert(avisoDeNombreOcupado(ocupada, "Elige otro nombre."));
      title.value = database.title;
      return;
    }
    try {
      const guardada = await api(`/databases/${database.id}`, {
        method: "PATCH",
        body: JSON.stringify({ title: newTitle }),
      });
      const final = guardada?.title || newTitle;
      // El saneado del backend no se copia aquí: manda el título que ha
      // quedado en el fichero. Si vuelve con otro nombre y ese nombre ya lo
      // tenía una hermana («Clientes.» -> «Clientes (2)»), estaba ocupado:
      // se deshace el renombrado para no dejar dos iguales.
      const choque = ocupadaPorElServidor(final, newTitle, hermanas);
      if (choque) {
        let deshecho = true;
        try {
          await api(`/databases/${database.id}`, {
            method: "PATCH",
            body: JSON.stringify({ title: database.title }),
          });
        } catch (_) {
          deshecho = false;
        }
        title.value = database.title;
        await loadTree();
        alert(
          avisoDeNombreOcupado(
            choque,
            deshecho
              ? `Se ha dejado «${database.title}» como estaba.`
              : `No se ha podido deshacer el renombrado: la base se llama ahora «${final}».`,
          ),
        );
        return;
      }
      database.title = final;
      title.value = final;
      await loadTree();
      markActive(database.id);
    } catch (e) {
      if (e?.status === 401) return;
      alert("Error al renombrar: " + e.message);
      title.value = database.title;
    }
  };
  title.addEventListener("blur", guardarTitulo);
  title.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    title.blur(); // un solo camino: el mismo que salir del campo
  });

  // La vista activa hace falta ya para el menú: «Todas» no es una vista, es la
  // base sin filtro, y no hay nada que filtrar, ordenar ni borrar.
  const activeView = views.find((v) => v.id === activeViewId) || null;
  // La pestaña con la que queda abierta esta base se recuerda aquí (una vez
  // pintada, no antes: si el pintado falla, la que valía sigue siendo la
  // anterior). Es lo que hace que recargar en `/<id>` de una base --o volver a
  // ella mañana-- abra la misma vista, «Todas» incluida.
  if (state.current?.kind === "database" && state.current.id === database.id) {
    recordarVista(database.id, activeViewId);
  }
  // Nunca se borra la última vista: sin ninguna, la base se quedaría sin la
  // pestaña con la que se abre. El menú lo dice en vez de esconder la opción.
  const esLaUltimaVista = Boolean(activeView) && views.length <= 1;

  const borrarVista = async () => {
    const pregunta =
      `¿Borrar la vista «${activeView.name}»?\n\n` +
      "Las filas no se tocan: los datos son de la base, no de la vista. " +
      "Se quita esta forma de mirarlos (filtro, orden y tipo).";
    if (!confirm(pregunta)) return;
    try {
      await api(`/databases/${database.id}/views/${activeView.id}`, { method: "DELETE" });
    } catch (e) {
      reportError("No se pudo borrar la vista", e);
      return;
    }
    // La vista activa pasa a una que exista -- la vecina de la izquierda, o
    // la primera si era la primera -- y el foco vuelve a la pestaña: el menú
    // ya no está, y el foco no puede quedarse en <body>.
    const indice = views.findIndex((v) => v.id === activeView.id);
    const quedan = views.filter((v) => v.id !== activeView.id);
    const siguiente = quedan[Math.min(indice, quedan.length - 1)] || null;
    await renderDatabase(database, quedan, siguiente ? siguiente.id : null, {
      focus: "viewTab",
    });
  };

  const borrarBase = async () => {
    if (!confirm(`¿Borrar "${database.title}" y todas sus filas? Esto no se puede deshacer.`)) return;
    try {
      await api(`/databases/${database.id}`, { method: "DELETE" });
      await loadTree();
      volverAlInicio();
    } catch (e) {
      // Si no se borró, la database sigue en pantalla: mejor decir por qué.
      reportError("No se pudo borrar la base de datos", e);
    }
  };

  header.append(title, botonCompartir("database", database.id, () => database.title));

  // Debajo del título ya no va la lista «Propiedad (Tipo) · …»: no decía nada
  // que la cabecera de la tabla no diga (cada columna lleva el icono de su tipo).

  // La línea de las vistas, como en Notion: las pestañas a la izquierda y, a la
  // derecha, el recuento y los tres puntos con todo lo demás (filtrar, ordenar,
  // propiedades, plantillas, borrar). Al marcar filas, la barra de la selección
  // ocupa ESTA línea (ver `.db-viewbar > .db-sel-bar` en el CSS): la tabla no se
  // mueve y, al quitar la selección, vuelven las pestañas.
  const viewbar = document.createElement("div");
  viewbar.className = "db-viewbar";

  const tabs = document.createElement("div");
  tabs.className = "view-tabs";
  // La pestaña de cada vista (null = «sin vista»). El repintado destruye estos
  // botones, así que se guardan aquí para poder devolver el foco al activo.
  const tabButtons = new Map();

  const defaultTab = document.createElement("button");
  defaultTab.className = "view-tab" + (activeViewId === null ? " active" : "");
  defaultTab.innerHTML = `${svgIcon("i-database")}<span>Todas</span>`;
  defaultTab.title = "Todas las filas, sin vista";
  defaultTab.addEventListener("click", () => renderDatabase(database, views, null, { focus: "viewTab" }));
  tabButtons.set(null, defaultTab);
  tabs.appendChild(defaultTab);

  views.forEach((v) => {
    const tab = document.createElement("button");
    tab.className = "view-tab" + (v.id === activeViewId ? " active" : "");
    // El tipo de la vista lo dice el icono (y el `title`), no un sufijo.
    const tipo = TIPOS_DE_VISTA[v.type] || TIPOS_DE_VISTA.table;
    tab.innerHTML = `${svgIcon(tipo.icono)}<span>${escapeHtml(v.name)}</span>`;
    tab.title = `${v.name} · ${tipo.nombre}`;
    tab.addEventListener("click", () => renderDatabase(database, views, v.id, { focus: "viewTab" }));
    tabButtons.set(v.id, tab);
    tabs.appendChild(tab);
  });

  // «+» tras la última pestaña: una vista nueva, como en Notion.
  const newViewTab = document.createElement("button");
  newViewTab.type = "button";
  newViewTab.className = "view-tab view-tab-add";
  newViewTab.innerHTML = svgIcon("i-plus");
  newViewTab.title = "Nueva vista";
  newViewTab.setAttribute("aria-label", "Nueva vista");
  newViewTab.addEventListener("click", () => openViewDialog(database));
  tabs.appendChild(newViewTab);

  const viewbarFin = document.createElement("div");
  viewbarFin.className = "db-viewbar-end";

  const menuBtn = document.createElement("button");
  menuBtn.type = "button";
  menuBtn.className = "icon-btn db-menu-btn";
  menuBtn.innerHTML = svgIcon("i-more");
  menuBtn.title = "Opciones de la base";
  menuBtn.setAttribute("aria-label", "Opciones de la base: filtrar, ordenar, propiedades…");
  menuBtn.setAttribute("aria-haspopup", "menu");
  menuBtn.setAttribute("aria-expanded", "false");
  menuBtn.addEventListener("click", () => {
    if (menuBtn.getAttribute("aria-expanded") === "true") {
      cerrarPanelDeColumna();
      return;
    }
    abrirMenuDeBase();
  });
  viewbarFin.appendChild(menuBtn);
  viewbar.append(tabs, viewbarFin);

  /** El menú de los tres puntos. Lo de la vista arriba (filtrar, ordenar,
   * qué filas), lo de la base debajo y lo que borra al final. Una opción que
   * no se puede usar se queda visible y, al pulsarla, dice por qué. */
  function abrirMenuDeBase() {
    const menu = document.createElement("div");
    menu.className = "db-col-menu db-base-menu";
    const avisar = (texto) => {
      const nota = notaDeMenu(menu);
      nota.textContent = texto;
      nota.hidden = false;
    };
    const elegir = (accion) => () => {
      cerrarPanelDeColumna();
      accion();
    };
    const etiqueta = (texto) => {
      const el = document.createElement("div");
      el.className = "db-col-menu-label";
      el.textContent = texto;
      return el;
    };

    menu.appendChild(etiqueta(activeView ? `Vista «${activeView.name}»` : "Todas (sin vista)"));
    const sinVista =
      "«Todas» es la base entera: el filtro, el orden de las filas y el ancho y el " +
      "orden de las columnas son de una vista. Elige una pestaña para cambiarlos.";
    const sinPropiedades = "La base no tiene propiedades: no hay nada que filtrar ni por lo que ordenar.";
    const motivoDeVista = !activeView ? sinVista : database.schema_def.length === 0 ? sinPropiedades : "";
    const nFiltros = filtros.length;
    const nOrden = clavesOrden.length;
    menu.append(
      itemDeColumna("Filtrar", {
        marca: nFiltros ? String(nFiltros) : "",
        motivo: motivoDeVista,
        alElegir: motivoDeVista
          ? () => avisar(motivoDeVista)
          : elegir(() => openFilterDialog(database, activeView, aplicarConfig)),
      }),
      itemDeColumna("Ordenar", {
        marca: nOrden ? String(nOrden) : "",
        motivo: motivoDeVista,
        alElegir: motivoDeVista
          ? () => avisar(motivoDeVista)
          : elegir(() => openSortDialog(database, activeView, aplicarConfig)),
      }),
    );
    // El nivel de filas es de la vista y solo existe si la base tiene
    // jerarquía: una base sin subtareas no enseña algo que no haría nada.
    if (activeView && claveDeJerarquia(database)) {
      const nivel = nivelDeVista(activeView, database);
      menu.appendChild(separadorDeMenu());
      menu.appendChild(etiqueta("Filas"));
      [
        ["roots", "Solo madres"],
        ["all", "Todas las filas"],
      ].forEach(([valor, texto]) =>
        menu.appendChild(
          itemDeColumna(texto, {
            radio: true,
            marca: nivel === valor ? "✓" : "",
            alElegir: elegir(() => {
              if (nivel !== valor) aplicarConfig({ parent_level: valor });
            }),
          }),
        ),
      );
    }

    menu.appendChild(separadorDeMenu());
    menu.appendChild(
      itemDeColumna("Propiedades…", {
        alElegir: elegir(() => openDatabaseDialog(database, recargarVistas)),
      }),
    );
    // Las plantillas de fila se configuran desde la base (función 29): lo que
    // se guarda es parte de su `.md`, no una preferencia de esta pestaña.
    menu.appendChild(
      itemDeColumna("Plantillas de fila…", {
        alElegir: elegir(() => openRowTemplatesDialog(database, recargarVistas)),
      }),
    );
    menu.appendChild(
      itemDeColumna("Nueva vista…", { alElegir: elegir(() => openViewDialog(database)) }),
    );

    menu.appendChild(separadorDeMenu());
    if (activeView) {
      const motivo = esLaUltimaVista
        ? `«${activeView.name}» es la última vista de esta base: no se puede borrar. ` +
          "Las filas no dependen de ella y no se tocan."
        : "";
      menu.appendChild(
        itemDeColumna("Borrar esta vista", {
          peligro: !motivo,
          motivo,
          alElegir: motivo ? () => avisar(motivo) : elegir(borrarVista),
        }),
      );
    }
    menu.appendChild(
      itemDeColumna("Borrar base de datos", { peligro: true, alElegir: elegir(borrarBase) }),
    );

    abrirPanelFlotante(menuBtn, menu, { etiqueta: "Opciones de la base" });
    // Al borde derecho del botón, que el menú sale de la esquina de la base.
    const r = menuBtn.getBoundingClientRect();
    const ancho = menu.getBoundingClientRect().width;
    menu.style.left = `${Math.round(Math.max(8, Math.min(r.right - ancho, window.innerWidth - ancho - 8)))}px`;
  }

  const content = document.createElement("div");
  // El cuerpo de la vista (tabla/tablero/lista/calendario) entra con un
  // fundido de 160 ms solo cuando se cambia de pestaña; al repintar por
  // guardar una fila, mover una tarjeta o editar el esquema no se anima nada
  // (repintar no es navegar, y el parpadeo se notaría a cada celda).
  content.className = "db-view" + (focus === "viewTab" ? " is-entering" : "");
  main.append(header, viewbar, content);
  // En pantalla estrecha las pestañas van en una línea que se desplaza de lado
  // (ver el bloque «móvil» del CSS): la activa tiene que quedar a la vista, o
  // se mira el calendario con «Tabla» en pantalla. Solo se mueve esa línea.
  const pestanaActiva = tabs.querySelector(".view-tab.active");
  if (pestanaActiva && tabs.scrollWidth > tabs.clientWidth) {
    const caja = tabs.getBoundingClientRect();
    const r = pestanaActiva.getBoundingClientRect();
    if (r.left < caja.left || r.right > caja.right) {
      tabs.scrollLeft += r.left - caja.left - (caja.width - r.width) / 2;
    }
  }

  // El filtro y el orden son de la vista y los aplica el servidor (`?view=`),
  // que es el mismo `apply_view` que exporta el `.md`: no hay dos versiones de
  // la regla. M no sale de la lista filtrada: cuando hay filtro se pide
  // también la base entera, porque «3 de 10» es lo que hace honesto el recuento.
  const filtros = filtrosDeVista(activeView);
  const clavesOrden = clavesDeOrden(activeView?.config);
  let rows;
  let total = null;
  // «Solo madres» deja fuera las hijas: el «N de M» tiene que seguir contando
  // la base entera, igual que con un filtro. Se pide también la lista completa.
  const soloMadres =
    Boolean(activeView) &&
    Boolean(claveDeJerarquia(database)) &&
    nivelDeVista(activeView, database) === "roots";
  if (!activeView) {
    rows = await api(`/databases/${database.id}/rows`);
  } else if (filtros.length || soloMadres) {
    const [deLaVista, todas] = await Promise.all([
      api(`/databases/${database.id}/rows?view=${activeView.id}`),
      api(`/databases/${database.id}/rows`),
    ]);
    rows = deLaVista;
    total = todas.length;
  } else {
    rows = await api(`/databases/${database.id}/rows?view=${activeView.id}`);
  }

  // Guardar el filtro o el orden es un PATCH a la vista y un repintado con lo
  // que el servidor acaba de escribir: la pantalla no guarda su propia copia.
  const crearVistaYAplicar = async (cambios) => {
    try {
      const nueva = await crearVistaPorDefecto(database, cambios);
      const [fresca, vistas] = await Promise.all([
        api(`/databases/${database.id}`),
        api(`/databases/${database.id}/views`),
      ]);
      await renderDatabase(fresca, vistas, nueva?.id ?? vistas[0]?.id ?? null);
    } catch (e) {
      reportError("No se pudo guardar la vista", e);
    }
  };
  const aplicarConfig = async (cambios) => {
    // El ancho y el orden de las columnas repintan la base entera (el `.md`
    // manda, la pantalla no guarda su propia copia): se apunta dónde estaba
    // mirando el usuario para devolverlo después. Sin esto, soltar el asa con
    // la tabla desplazada la mandaba a la primera columna, y el foco del
    // teclado se quedaba en `<body>` entre dos reordenaciones seguidas.
    const sonColumnas = "widths" in cambios || "order" in cambios;
    const antes = sonColumnas ? estadoDeColumnas() : null;
    try {
      await guardarConfigDeVista(database, activeView, cambios);
    } catch (e) {
      reportError("No se pudo guardar la vista", e);
      return;
    }
    await recargarVistas();
    if (antes) restaurarColumnas(antes);
  };

  if (activeView) {
    // El recuento va en la línea de las vistas, junto a los tres puntos; los
    // chips de las condiciones activas, en su propia línea encima de la tabla
    // (solo si hay alguna).
    const nivel = nivelDeVista(activeView, database);
    const cuenta = document.createElement("span");
    cuenta.className = "db-count";
    // «Solo madres» deja fuera las hijas igual que un filtro: el recuento sigue
    // diciendo cuántas hay en la base, o «1 fila» parecería que solo hay una.
    const filtraAlgo = filtros.length > 0 || nivel === "roots";
    cuenta.textContent = textoDeRecuento(rows.length, filtraAlgo ? total : null);
    viewbarFin.insertBefore(cuenta, menuBtn);
    // Un filtro o un orden activo se nota también en los tres puntos.
    menuBtn.classList.toggle("is-active", filtros.length > 0 || clavesOrden.length > 0);
    const chips = renderViewTools({ database, view: activeView, onCambio: aplicarConfig });
    if (chips) main.insertBefore(chips, content);
  }

  // La tabla de la vista y su «+ Nueva fila». La tabla se pinta a demanda
  // porque con un filtro que no deja pasar nada no se enseña (ver abajo) y
  // aun así hay que poder añadir una fila.
  const esTabla = !activeView || !["board", "calendar", "list"].includes(activeView.type) ||
    (activeView.type === "board" && !activeView.config?.group_by);
  let tablaPintada = null;
  let alPedirTabla = null;
  const alta = renderAltaEnTabla({
    database,
    vista: activeView,
    tabla: () => {
      alPedirTabla?.();
      return tablaPintada;
    },
  });
  // La cabecera de cada columna es su menú (renombrar, ocultar en ESTA vista,
  // tipo, borrar) y el «+» añade una columna: `recargarVistas` relee base y
  // vistas después, que el servidor puede haber migrado claves y la lista de
  // columnas de la vista. Y sigue ordenando al pincharla (`filtros-vista`):
  // el menú es el botón de tres puntos, el rótulo ordena.
  const pintarTabla = () =>
    (tablaPintada = renderTable(database, rows, {
      sort: clavesOrden,
      onToggleSort: activeView
        ? (key) => aplicarConfig({ sort: alternarOrden(clavesOrden, key) })
        : null,
      vacio: filtros.length ? "Ninguna fila cumple el filtro." : null,
      // El ancho y el orden de las columnas se guardan en la vista activa:
      // sin vista no hay entrada en el `.md` donde escribirlos.
      view: activeView,
      // Una base sin ninguna vista no tiene dónde guardar anchos ni orden: el
      // primer cambio crea su vista «Tabla», como la que trae toda base de
      // Notion. Con vistas, «Todas (sin vista)» se queda como está.
      onCambioColumnas: activeView ? aplicarConfig : views.length ? null : crearVistaYAplicar,
      onOrdenar: activeView || !views.length
        ? (key, dir) =>
            (activeView ? aplicarConfig : crearVistaYAplicar)({ sort: fijarOrden(clavesOrden, key, dir) })
        : null,
      onChanged: recargarVistas,
      // Tras borrar o cambiar una propiedad en bloque, la tabla se repinta
      // con lo que diga el servidor: nada de ir tocando filas a mano.
      onRepintar: refresh,
      // La barra de la selección ocupa la línea de las vistas.
      barraEn: viewbar,
      // La fila nueva ya está en su sitio: el contador de la vista y el aviso
      // (si el filtro la deja fuera, lo dice).
      onAlta: (res) => {
        if (res.visibles !== null) refrescarRecuentoDeVista(content, res.visibles, res.total);
        alta.avisar(res);
      },
    }));

  // Vacío honesto: no una tabla en blanco, sino el motivo escrito y la salida.
  if (filtros.length && rows.length === 0) {
    // Sin filas no hay tabla, pero la selección de antes sigue viva: la barra
    // se pinta igual (dice cuántas hay y que el filtro las ha dejado fuera).
    // Esconderla dejaría una selección invisible que, aun así, actúa.
    const hueco = document.createElement("div");
    hueco.className = "db-sel-shell";
    const sel = crearSeleccionDeFilas({
      database,
      rows: [],
      onRepintar: refresh,
      anfitrion: viewbar,
      marco: hueco,
    });
    hueco.append(sel.informes, avisoDeFiltroVacio(() => aplicarConfig({ filters: [] })));
    content.appendChild(hueco);
    // Sin filas no hay tabla donde escribir la nueva: «+ Nueva fila» cambia el
    // aviso por la tabla vacía y pinta ahí la fila fantasma. Vale para
    // cualquier tipo de vista: sin filas, ni el tablero ni la lista tienen
    // dónde escribirla.
    alPedirTabla = () => {
      alPedirTabla = null;
      hueco.replaceWith(pintarTabla());
    };
  } else if (activeView && activeView.type === "board" && activeView.config?.group_by) {
    content.appendChild(renderBoard(database, rows, activeView, refresh));
  } else if (activeView && activeView.type === "calendar") {
    content.appendChild(renderCalendar(database, rows, activeView, refresh));
  } else if (activeView && activeView.type === "list") {
    content.appendChild(renderList(database, rows, activeView, refresh, { barraEn: viewbar }));
  } else {
    content.appendChild(pintarTabla());
  }

  // «+ Nueva fila» bajo la tabla: la fila se escribe en la propia tabla. La
  // lista y el calendario traen su alta dentro y el tablero, una por columna.
  if (esTabla || alPedirTabla) content.appendChild(alta);
  // El foco se mueve solo cuando hay un motivo, no en cada repintado: abrir la
  // base (el primer campo del alta, que si no hay que recorrer la tabla entera
  // para llegar a «Nueva fila») o cambiar de vista (la pestaña, que acaba de
  // repintarse). `refresh` —mover una tarjeta, guardar una fila, guardar el
  // esquema— no pide nada: antes el foco acababa en el alta, al final del
  // contenido, y el siguiente Tab seguía desde ahí. `preventScroll` porque si
  // el destino cae bajo el pliegue no queremos dar un salto.
  if (focus === "addRow") {
    alta.querySelector(".db-add-row-btn")?.focus({ preventScroll: true });
  } else if (focus === "viewTab") {
    tabButtons.get(activeViewId)?.focus({ preventScroll: true });
  }
}

// ------------------------------------------------ filtros y orden de una vista
// El filtro y el orden son de LA VISTA, no de la base: viven en su `config`
// --el frontmatter de `Bases/<slug>.md`-- y los aplica el servidor sobre
// `GET /databases/{id}/rows?view=`. Aquí solo se editan y se enseñan; si se
// filtrara otra vez en el navegador habría dos versiones de la regla y la
// pantalla y el `.md` exportado podrían discrepar sin que nada lo dijera.
//
// Las condiciones se combinan con Y (una fila se enseña si las cumple todas).
// El O entre grupos no existe todavía.

const OPERADORES_TEXTO = [
  ["eq", "es"],
  ["neq", "no es"],
  ["contains", "contiene"],
  ["is_empty", "está vacío"],
  ["is_not_empty", "no está vacío"],
];

/** Los operadores que ofrece cada tipo de propiedad, en el orden en que se
 * leen. Los nombres son los que guarda el `.md` (`app/views.py`): cambiarlos
 * aquí sin cambiarlos allí rompería los filtros ya escritos. */
const OPERADORES_FILTRO = {
  text: OPERADORES_TEXTO,
  url: OPERADORES_TEXTO,
  number: [
    ["eq", "="],
    ["neq", "≠"],
    ["gt", ">"],
    ["lt", "<"],
    ["gte", "≥"],
    ["lte", "≤"],
    ["is_empty", "está vacío"],
    ["is_not_empty", "no está vacío"],
  ],
  date: [
    ["eq", "es"],
    ["before", "antes de"],
    ["after", "después de"],
    ["is_empty", "está vacío"],
    // El servidor lo admite en cualquier tipo (`views.SIN_VALOR`); sin esta
    // fila el chip de un calendario de serie enseñaba el operador en crudo
    // («Entrega is_not_empty») y el diálogo lo perdía al aplicarlo.
    ["is_not_empty", "no está vacío"],
  ],
  select: [
    ["eq", "es"],
    ["neq", "no es"],
  ],
  multiselect: [
    ["contains", "contiene"],
    ["is_empty", "está vacío"],
    ["is_not_empty", "no está vacío"],
  ],
  checkbox: [
    ["checked", "marcado"],
    ["unchecked", "desmarcado"],
  ],
  relation: [
    ["includes", "incluye"],
    ["is_empty", "está vacío"],
    ["is_not_empty", "no está vacío"],
  ],
  person: [
    ["eq", "es"],
    ["neq", "no es"],
    ["is_empty", "está vacía"],
    ["is_not_empty", "no está vacía"],
  ],
};

/** Los operadores que no llevan valor: el estado lo dice el propio operador. */
const OPS_SIN_VALOR = new Set([
  "is_empty",
  "is_not_empty",
  "checked",
  "unchecked",
  "is_closed",
  "is_open",
]);

/** Los operadores del estado de la base. No enumeran valores: la regla de la
 * base (`closed: {property, values}`) es la que sabe qué está cerrado, así que
 * el filtro no tiene que repetir la lista en cada condición. */
const OPS_DE_ESTADO = [
  ["is_closed", "está cerrada"],
  ["is_open", "está abierta"],
];

/** Si el operador es de los del estado de la base.
 *
 * Son operadores de la VISTA y no de la columna: quién está cerrado lo decide
 * la regla de la base (`closed: {property, values}`), así que la condición no
 * tiene que nombrar la columna donde vive la regla —el `.md` puede traerla sin
 * `key` y el servidor la aplica igual (`app.views.apply_view`)—. */
function esOperadorDeEstado(op) {
  return OPS_DE_ESTADO.some(([valor]) => valor === op);
}

/** Si la condición es la de la vista: un operador de estado y ninguna columna. */
function esCondicionDeEstadoSinColumna(cond) {
  return !cond?.key && esOperadorDeEstado(cond?.op);
}

// Los títulos de las filas de las bases destino de una relación, para que el
// valor de «incluye» sea un nombre y no un id. Se cachea por clave de
// propiedad; si la base destino no se puede leer, el control pide el id.
const titulosDeRelacion = new Map();

/** Los operadores de una propiedad. `regla` es la regla de cierre de la base:
 * la columna que la base declara como estado ofrece además «está cerrada» y
 * «está abierta» (que son de la vista, aunque se lean desde su columna; el
 * panel los ofrece en cualquier condición con `operadoresDeCondicion`). */
function operadoresDe(prop, regla = null) {
  const ops = [...(OPERADORES_FILTRO[prop?.type] || OPERADORES_TEXTO)];
  if (regla && prop?.key && String(regla.property) === String(prop.key)) {
    ops.push(...OPS_DE_ESTADO);
  }
  return ops;
}

function etiquetaDeOperador(prop, op, regla = null) {
  const par = operadoresDe(prop, regla).find(([valor]) => valor === op);
  if (par) return par[1];
  return (OPS_DE_ESTADO.find(([valor]) => valor === op) || [op, op])[1];
}

function operadorPorDefecto(prop, regla = null) {
  return operadoresDe(prop, regla)[0][0];
}

/** Los operadores que ofrece UNA condición del panel de filtros.
 *
 * Los de estado (`is_closed`/`is_open`) son de la vista: se ofrecen siempre,
 * también cuando la condición no nombra ninguna columna. Y una condición que ya
 * es de estado conserva su operador aunque la regla de la base se haya movido a
 * otra columna: al abrir el panel no puede convertirse en un `eq` con el valor
 * vacío (`Estado es «—»`), que es lo que pasaba antes de esto. */
function operadoresDeCondicion(prop, cond, regla) {
  const ops = operadoresDe(prop, regla);
  if (!prop?.key) return [...OPS_DE_ESTADO];
  if (esOperadorDeEstado(cond.op) && !ops.some(([valor]) => valor === cond.op)) {
    ops.push(...OPS_DE_ESTADO);
  }
  return ops;
}

/** Los filtros de una vista, normalizados (una vista vieja puede no tener).
 *
 * Una condición de estado (`is_closed`/`is_open`) vale sin `key`: la regla de la
 * base es la que sabe qué está cerrado. Descartarla aquí dejaba la vista
 * filtrada por el servidor sin chip y con el panel diciendo «sin condiciones»,
 * así que aplicar cualquier cambio en el panel borraba el filtro del `.md` sin
 * avisar. */
function filtrosDeVista(view) {
  const filtros = view?.config?.filters;
  if (!Array.isArray(filtros)) return [];
  return filtros.filter(
    (f) => f && typeof f === "object" && f.op && (f.key || esCondicionDeEstadoSinColumna(f)),
  );
}

/** Las claves de orden, aceptando la forma antigua de una sola clave
 * (`sort: {key, dir}`), que las versiones anteriores escribían. */
function clavesDeOrden(config) {
  const bruto = config?.sort;
  const lista = Array.isArray(bruto) ? bruto : bruto ? [bruto] : [];
  return lista
    .filter((clave) => clave && typeof clave === "object" && clave.key)
    .map((clave) => ({ key: clave.key, dir: clave.dir === "desc" ? "desc" : "asc" }));
}

/** «3 filas» o «3 de 10 filas»: el recuento nunca cuenta solo una parte sin
 * decir de qué es parte. `total` nulo es «no hay filtro». */
function textoDeRecuento(visibles, total) {
  const plural = (n) => (n === 1 ? "fila" : "filas");
  if (total === null || total === undefined) return `${visibles} ${plural(visibles)}`;
  return `${visibles} de ${total} ${plural(total)}`;
}

async function guardarConfigDeVista(database, view, cambios) {
  const config = { ...(view.config || {}) };
  for (const [clave, valor] of Object.entries(cambios)) {
    // Lista vacía = no hay filtro ni orden: se quita la clave en vez de
    // escribir `filters: []`, que el `.md` no necesita.
    if (
      valor === undefined ||
      valor === null ||
      (Array.isArray(valor) && valor.length === 0) ||
      // Un mapa vacío tampoco se escribe: `widths: {}` es un flow map, que el
      // parser del frontmatter no sabe leer y la clave se perdería al releer.
      (typeof valor === "object" && !Array.isArray(valor) && Object.keys(valor).length === 0)
    ) {
      delete config[clave];
    } else {
      config[clave] = valor;
    }
  }
  const guardada = await api(`/databases/${database.id}/views/${view.id}`, {
    method: "PATCH",
    body: JSON.stringify({ config }),
  });
  view.config = guardada?.config || config;
  return guardada;
}

function nombreDePropiedad(database, key) {
  // El título no es una propiedad del `schema`: su nombre vive en `title_name`
  // (y el servidor ordena por él con la clave `title`).
  if (key === "title") return database.title_name || "Título";
  return database.schema_def.find((p) => p.key === key)?.name || key;
}

/** Una condición en palabras, para el chip: «Fase es «lead»». */
function textoDeCondicion(cond, database) {
  const prop = database.schema_def.find((p) => p.key === cond.key);
  if (!prop) {
    // Una condición de la VISTA, sin columna: «Está abierta». Es lo que deja un
    // filtro sin `key`, y hay que pintarlo o el chip desaparece y el panel de
    // filtros borra la condición al aplicarse.
    const texto = `${cond.key ? cond.key + " " : ""}${etiquetaDeOperador(null, cond.op)}`;
    return texto.charAt(0).toUpperCase() + texto.slice(1);
  }
  const operador = etiquetaDeOperador(prop, cond.op, reglaDeCierre(database));
  if (OPS_SIN_VALOR.has(cond.op)) return `${prop.name} ${operador}`;
  const valor =
    prop.type === "relation"
      ? titulosDeRelacion.get(prop.key)?.get(cond.value) || String(cond.value ?? "")
      : prop.type === "person"
        ? nombreDePersona(cond.value) || String(cond.value ?? "")
        : String(cond.value ?? "");
  return `${prop.name} ${operador} «${valor}»`;
}

/** Pinta UNA vez las condiciones como chips (con su aspa) y el resumen del
 * orden. Se llama otra vez cuando llegan los títulos de las relaciones. */
function pintarChipsDeVista(chips, database, filtros, claves, onCambio) {
  chips.textContent = "";
  filtros.forEach((cond, indice) => {
    const chip = document.createElement("span");
    chip.className = "db-chip";
    const texto = document.createElement("span");
    texto.textContent = textoDeCondicion(cond, database);
    const quitar = document.createElement("button");
    quitar.type = "button";
    quitar.className = "db-chip-remove";
    quitar.innerHTML = svgIcon("i-x");
    quitar.title = "Quitar esta condición";
    quitar.setAttribute("aria-label", `Quitar el filtro «${texto.textContent}»`);
    quitar.addEventListener("click", () =>
      onCambio({ filters: filtros.filter((_, i) => i !== indice) }),
    );
    chip.append(texto, quitar);
    chips.appendChild(chip);
  });
  if (filtros.length > 1) {
    const limpiar = document.createElement("button");
    limpiar.type = "button";
    limpiar.className = "btn-link";
    limpiar.textContent = "Limpiar filtro";
    limpiar.addEventListener("click", () => onCambio({ filters: [] }));
    chips.appendChild(limpiar);
  }
  if (claves.length) {
    const resumen = document.createElement("span");
    resumen.className = "db-sort-summary";
    const orden = claves
      .map((clave) => `${nombreDePropiedad(database, clave.key)} ${clave.dir === "desc" ? "↓" : "↑"}`)
      .join(" · ");
    resumen.textContent = `Orden: ${orden}`;
    const quitarOrden = document.createElement("button");
    quitarOrden.type = "button";
    quitarOrden.className = "btn-link";
    quitarOrden.textContent = "Quitar orden";
    quitarOrden.addEventListener("click", () => onCambio({ sort: [] }));
    chips.append(resumen, quitarOrden);
  }
}

/** Los chips de las condiciones activas de la vista (filtro y orden), en su
 * línea encima de la tabla. Filtrar, ordenar y el nivel de filas están en el
 * menú de los tres puntos; el recuento, en la línea de las vistas. `null` si la
 * vista no filtra ni ordena: no se pinta una línea vacía. */
function renderViewTools({ database, view, onCambio }) {
  const filtros = filtrosDeVista(view);
  const claves = clavesDeOrden(view.config);
  if (!filtros.length && !claves.length) return null;

  const tools = document.createElement("div");
  tools.className = "db-tools";

  const chips = document.createElement("div");
  chips.className = "db-chips";
  tools.appendChild(chips);
  pintarChipsDeVista(chips, database, filtros, claves, onCambio);

  // Un filtro sobre una relación enseña un id hasta que se conoce el título de
  // la fila: con la lista de la base destino, el chip dice el nombre.
  if (filtros.some((f) => database.schema_def.find((p) => p.key === f.key)?.type === "relation")) {
    cargarTitulosDeRelacion(database).then(() => {
      if (!chips.isConnected) return;
      pintarChipsDeVista(chips, database, filtros, claves, onCambio);
    });
  }
  // Un filtro sobre una persona enseña el id hasta que llega el directorio.
  if (
    filtros.some((f) => database.schema_def.find((p) => p.key === f.key)?.type === "person")
  ) {
    cargarPersonas()
      .then(() => {
        if (!chips.isConnected) return;
        pintarChipsDeVista(chips, database, filtros, claves, onCambio);
      })
      .catch(() => {});
  }

  return tools;
}

/** Reescribe el recuento de la barra SIN repintar la vista.
 *
 * El alta en línea de la lista y del calendario repinta solo su cuerpo, y el
 * «N de M filas» vive en la línea de las vistas (`.db-viewbar`): sin esto el
 * contador se quedaba con el número de antes del alta (un contador que miente
 * es peor que no tenerlo). `total` nulo es «la vista no filtra».
 *
 * `desde` es el cuerpo de la vista (dentro de `.db-view`); la línea de las
 * vistas es hermana de `.db-view`.
 */
function refrescarRecuentoDeVista(desde, visibles, total) {
  const cuerpo = desde.closest(".db-view");
  const cuenta = cuerpo?.parentElement?.querySelector(":scope > .db-viewbar .db-count") ?? null;
  if (!cuenta) return;
  cuenta.textContent = textoDeRecuento(visibles, total);
}

/** La cabecera de una columna: un botón que ordena, con su flecha y, si hay
 * varias claves, el número de prioridad. */
function celdaDeCabecera(prop, claves, onToggleSort) {
  const th = document.createElement("th");
  th.scope = "col";
  if (!onToggleSort) {
    th.textContent = prop.name;
    return th;
  }
  const indice = claves.findIndex((clave) => clave.key === prop.key);
  const activa = indice !== -1;
  th.setAttribute(
    "aria-sort",
    activa ? (claves[indice].dir === "desc" ? "descending" : "ascending") : "none",
  );
  const boton = document.createElement("button");
  boton.type = "button";
  boton.className = "th-sort" + (activa ? " is-sorted" : "");
  boton.title = activa
    ? `Ordenado por ${prop.name}: pulsa para invertir`
    : `Ordenar por ${prop.name}`;
  boton.append(document.createTextNode(prop.name));
  if (activa) {
    const flecha = document.createElement("span");
    flecha.className = "sort-arrow";
    flecha.textContent = claves[indice].dir === "desc" ? "↓" : "↑";
    flecha.setAttribute("aria-hidden", "true");
    boton.appendChild(flecha);
    if (claves.length > 1) {
      const prioridad = document.createElement("span");
      prioridad.className = "sort-priority";
      prioridad.textContent = String(indice + 1);
      prioridad.title = `Clave ${indice + 1} de ${claves.length}`;
      boton.appendChild(prioridad);
    }
  }
  boton.addEventListener("click", () => onToggleSort(prop.key));
  th.appendChild(boton);
  return th;
}

/** «Orden ascendente/descendente» del menú de una columna: esa columna pasa a
 * mandar con ese sentido (las demás quedan de desempate); `dir` nulo la quita. */
function fijarOrden(claves, key, dir) {
  const resto = claves.filter((clave) => clave.key !== key);
  return dir ? [{ key, dir }, ...resto] : resto;
}

/** La vista «Tabla» de una base que no tiene ninguna, con `config` dentro.
 * Es donde se guardan anchos, orden y ordenación de las columnas. */
async function crearVistaPorDefecto(database, config = {}) {
  const limpia = {};
  for (const [k, v] of Object.entries(config || {})) {
    if (v === undefined || v === null) continue;
    if (Array.isArray(v) && !v.length) continue;
    if (typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length) continue;
    limpia[k] = v;
  }
  return api(`/databases/${database.id}/views`, {
    method: "POST",
    body: JSON.stringify({ name: "Tabla", type: "table", config: limpia }),
  });
}

/** Clic en la cabecera: si esa columna manda, invierte el sentido; si no,
 * pasa a mandar (ascendente) sin perder las demás, que quedan de desempate. */
function alternarOrden(claves, key) {
  const indice = claves.findIndex((clave) => clave.key === key);
  if (indice === -1) return [{ key, dir: "asc" }, ...claves];
  return claves.map((clave, i) =>
    i === indice ? { key, dir: clave.dir === "asc" ? "desc" : "asc" } : clave,
  );
}

/** El vacío con motivo: la vista no pinta una tabla en blanco, dice que el
 * filtro no deja pasar nada y ofrece quitarlo. */
function avisoDeFiltroVacio(onQuitar) {
  const box = avisoDeVista(
    "Ninguna fila cumple el filtro.",
    "Las filas siguen en la base: es el filtro de esta vista el que no deja pasar ninguna.",
  );
  const quitar = document.createElement("button");
  quitar.type = "button";
  quitar.className = "btn";
  quitar.textContent = "Quitar el filtro";
  quitar.addEventListener("click", onQuitar);
  box.appendChild(quitar);
  return box;
}

async function cargarTitulosDeRelacion(database) {
  const props = database.schema_def.filter((p) => p.type === "relation" && p.target_database);
  await Promise.all(
    props.map(async (prop) => {
      if (titulosDeRelacion.has(prop.key)) return;
      try {
        const filas = await api(`/databases/${prop.target_database}/rows`);
        titulosDeRelacion.set(
          prop.key,
          new Map(filas.map((fila) => [fila.id, fila.title || "Sin título"])),
        );
      } catch (_) {
        // La base destino no está (o no se puede leer): el id a mano sigue
        // valiendo, así que el control no se queda sin nada.
      }
    }),
  );
}

// --------------------------------------------------- panel de filtro (dialog)
const filterDialog = document.getElementById("dialog-filter");
const filterRowsEl = document.getElementById("filter-rows");
const filterStatusEl = document.getElementById("filter-status");
const sortDialog = document.getElementById("dialog-sort");
const sortRowsEl = document.getElementById("sort-rows");
const sortStatusEl = document.getElementById("sort-status");

let filterDialogCtx = null; // { database, view, condiciones, onCambio }
let sortDialogCtx = null; // { database, view, claves, onCambio }

function openFilterDialog(database, view, onCambio) {
  filterStatusEl.textContent = "";
  document.getElementById("filter-view-name").textContent = view.name;
  filterDialogCtx = {
    database,
    view,
    onCambio,
    condiciones: filtrosDeVista(view).map((filtro) => ({
      key: filtro.key,
      op: filtro.op,
      value: filtro.value === undefined || filtro.value === null ? "" : String(filtro.value),
    })),
  };
  pintarFilasDeFiltro();
  filterDialog.showModal();
  cargarTitulosDeRelacion(database).then(() => {
    if (filterDialog.open) pintarFilasDeFiltro();
  });
  if (database.schema_def.some((p) => p.type === "person")) {
    cargarPersonas()
      .then(() => {
        if (filterDialog.open) pintarFilasDeFiltro();
      })
      .catch(() => {});
  }
}

function pintarFilasDeFiltro() {
  const ctx = filterDialogCtx;
  if (!ctx) return;
  filterRowsEl.textContent = "";
  ctx.condiciones.forEach((cond, indice) => {
    filterRowsEl.appendChild(filaDeCondicion(ctx.database, cond, indice));
  });
  if (ctx.condiciones.length === 0) {
    const vacio = document.createElement("p");
    vacio.className = "filter-empty";
    vacio.textContent =
      "Sin condiciones: la vista enseña todas las filas. Añade una para filtrar.";
    filterRowsEl.appendChild(vacio);
  }
  const anadir = document.getElementById("btn-add-filter");
  anadir.disabled = ctx.database.schema_def.length === 0;
}

function filaDeCondicion(database, cond, indice) {
  const fila = document.createElement("div");
  fila.className = "filter-row";

  const propSelect = document.createElement("select");
  propSelect.className = "filter-prop";
  propSelect.setAttribute("aria-label", `Propiedad de la condición ${indice + 1}`);

  // La propiedad de la condición. Si ya no existe (un `.md` editado a mano),
  // se enseña su clave para poder quitarla: convertirla en una condición sobre
  // la primera propiedad de la base cambiaría el filtro sin decirlo.
  const prop = database.schema_def.find((p) => p.key === cond.key) || null;
  const opciones = [...database.schema_def];
  if (!prop && cond.key) {
    opciones.unshift({ key: cond.key, name: `${cond.key} (ya no existe)` });
  }
  opciones.forEach((p) => {
    const opcion = document.createElement("option");
    opcion.value = p.key;
    opcion.textContent = p.name;
    propSelect.appendChild(opcion);
  });
  const regla = reglaDeCierre(database);
  // «Estado de la base»: la condición de la vista, sin columna. Es la forma que
  // trae un `.md` escrito a mano (`filters: [{op: is_open}]`) y la que sobrevive
  // a que la regla se mueva de columna: el filtro se abre y se guarda igual.
  // Solo se ofrece si la base declara la regla de cierre: sin ella nada está
  // cerrado, así que «está cerrada» dejaría la vista en 0 filas sin explicación
  // (`app.views.apply_view` no deja pasar ninguna fila). Una condición de estado
  // que YA viene guardada se sigue abriendo aunque no haya regla, para no
  // convertirla en un `eq` con el valor vacío al aplicar.
  if (regla || esCondicionDeEstadoSinColumna(cond)) {
    const sinColumna = document.createElement("option");
    sinColumna.value = "";
    sinColumna.textContent = "Estado de la base";
    propSelect.appendChild(sinColumna);
  }
  const propParaElControl = prop || {
    key: cond.key || "",
    name: cond.key || "Estado de la base",
    type: "text",
  };
  propSelect.value = cond.key || "";
  if (propSelect.selectedIndex < 0 && prop) propSelect.value = prop.key;
  propSelect.addEventListener("change", () => {
    if (propSelect.value === "") {
      cond.key = "";
      cond.op = OPS_DE_ESTADO[0][0];
      cond.value = "";
      pintarFilasDeFiltro();
      return;
    }
    const nueva = database.schema_def.find((p) => p.key === propSelect.value);
    if (!nueva) return;
    cond.key = nueva.key;
    cond.op = operadorPorDefecto(nueva, regla);
    cond.value = "";
    pintarFilasDeFiltro();
  });

  const opSelect = document.createElement("select");
  opSelect.className = "filter-op";
  opSelect.setAttribute("aria-label", `Operador de la condición ${indice + 1}`);
  const ops = operadoresDeCondicion(propParaElControl, cond, regla);
  ops.forEach(([valor, etiqueta]) => {
    const opcion = document.createElement("option");
    opcion.value = valor;
    opcion.textContent = etiqueta;
    opSelect.appendChild(opcion);
  });
  if (!ops.some(([valor]) => valor === cond.op)) {
    cond.op = operadorPorDefecto(propParaElControl, regla);
  }
  opSelect.value = cond.op;
  opSelect.addEventListener("change", () => {
    cond.op = opSelect.value;
    // El operador decide si hay valor que pedir: el control se rehace.
    pintarFilasDeFiltro();
  });

  const valor = controlDeValor(propParaElControl, cond, indice);

  const quitar = document.createElement("button");
  quitar.type = "button";
  quitar.className = "filter-remove";
  quitar.innerHTML = svgIcon("i-x");
  quitar.title = "Quitar esta condición";
  quitar.setAttribute("aria-label", `Quitar la condición ${indice + 1}`);
  quitar.addEventListener("click", () => {
    filterDialogCtx.condiciones.splice(indice, 1);
    pintarFilasDeFiltro();
  });

  fila.append(propSelect, opSelect, valor, quitar);
  return fila;
}

function controlDeValor(prop, cond, indice) {
  const caja = document.createElement("span");
  caja.className = "filter-value";
  if (OPS_SIN_VALOR.has(cond.op)) return caja; // el operador ya lo dice todo

  let control;
  if (prop.type === "select") {
    control = document.createElement("select");
    const vacio = document.createElement("option");
    vacio.value = "";
    vacio.textContent = "—";
    control.appendChild(vacio);
    const opciones = [...(prop.options || [])];
    if (cond.value && !opciones.includes(cond.value)) opciones.push(cond.value);
    opciones.forEach((valor) => {
      const opcion = document.createElement("option");
      opcion.value = valor;
      opcion.textContent = valor;
      control.appendChild(opcion);
    });
    control.value = cond.value || "";
  } else if (prop.type === "relation") {
    control = controlDeRelacion(prop, cond);
  } else if (prop.type === "person") {
    control = controlDePersona(prop, cond);
  } else if (prop.type === "date") {
    control = document.createElement("input");
    control.type = "date";
    control.value = cond.value || "";
  } else if (prop.type === "number") {
    control = document.createElement("input");
    control.type = "number";
    control.step = "any";
    control.value = cond.value ?? "";
  } else {
    control = document.createElement("input");
    control.type = "text";
    control.placeholder = "valor";
    control.value = cond.value ?? "";
  }

  control.classList.add("filter-value-input");
  control.setAttribute("aria-label", `Valor de la condición ${indice + 1}`);
  const leer = () => {
    cond.value = control.value;
  };
  control.addEventListener("input", leer);
  control.addEventListener("change", leer);
  caja.appendChild(control);
  return caja;
}

/** El valor de un filtro sobre una persona: el desplegable del directorio.
 *
 * Se guarda el **id** (lo que dice la celda), pero se elige por nombre. Si el
 * directorio aún no está, se pide y el panel se repinta al llegar; mientras
 * tanto el desplegable sale con «sin asignar» y el valor guardado, que no se
 * pierde.
 */
function controlDePersona(prop, cond) {
  if (!personasCache.size) {
    cargarPersonas()
      .then(() => {
        if (filterDialog.open) pintarFilasDeFiltro();
      })
      .catch(() => {});
  }
  const select = document.createElement("select");
  const vacio = document.createElement("option");
  vacio.value = "";
  vacio.textContent = "— sin asignar";
  select.appendChild(vacio);
  personasCache.forEach((persona, id) => {
    const opcion = document.createElement("option");
    opcion.value = id;
    opcion.textContent = persona.display_name || id;
    select.appendChild(opcion);
  });
  if (cond.value && !personasCache.has(String(cond.value))) {
    const huerfana = document.createElement("option");
    huerfana.value = String(cond.value);
    huerfana.textContent = String(cond.value);
    select.appendChild(huerfana);
  }
  select.value = cond.value || "";
  return select;
}

function controlDeRelacion(prop, cond) {
  const titulos = titulosDeRelacion.get(prop.key);
  if (!titulos) {
    // Sin la lista del destino: se escribe el id, que es lo que guarda la
    // celda. El desplegable llega cuando carga `cargarTitulosDeRelacion`.
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "id de la fila";
    input.value = cond.value ?? "";
    return input;
  }
  const select = document.createElement("select");
  const vacio = document.createElement("option");
  vacio.value = "";
  vacio.textContent = "—";
  select.appendChild(vacio);
  titulos.forEach((titulo, id) => {
    const opcion = document.createElement("option");
    opcion.value = id;
    opcion.textContent = titulo;
    select.appendChild(opcion);
  });
  if (cond.value && !titulos.has(cond.value)) {
    const huerfana = document.createElement("option");
    huerfana.value = cond.value;
    huerfana.textContent = cond.value;
    select.appendChild(huerfana);
  }
  select.value = cond.value || "";
  return select;
}

document.getElementById("btn-add-filter").addEventListener("click", () => {
  const ctx = filterDialogCtx;
  const prop = ctx?.database.schema_def[0];
  if (!ctx || !prop) return;
  ctx.condiciones.push({
    key: prop.key,
    op: operadorPorDefecto(prop, reglaDeCierre(ctx.database)),
    value: "",
  });
  filterStatusEl.textContent = "";
  pintarFilasDeFiltro();
});

document.getElementById("btn-cancel-filter").addEventListener("click", () => {
  filterDialog.close();
});

document.getElementById("btn-clear-filter").addEventListener("click", async () => {
  const ctx = filterDialogCtx;
  if (!ctx) return;
  filterDialog.close();
  await ctx.onCambio({ filters: [] });
});

document.getElementById("btn-apply-filter").addEventListener("click", async () => {
  const ctx = filterDialogCtx;
  if (!ctx) return;
  const filtros = [];
  const sinValor = [];
  ctx.condiciones.forEach((cond, indice) => {
    if (esOperadorDeEstado(cond.op)) {
      // Operador de la vista: la regla de la base decide qué está cerrado, así
      // que se guarda sin `key` cuando la condición no nombra columna (la forma
      // que acepta `apply_view`). Con `key` también vale: una regla que se mueve
      // de columna no invalida el filtro.
      filtros.push(cond.key ? { key: cond.key, op: cond.op } : { op: cond.op });
      return;
    }
    if (!cond.key) return;
    if (OPS_SIN_VALOR.has(cond.op)) {
      filtros.push({ key: cond.key, op: cond.op });
      return;
    }
    const valor = cond.value === null || cond.value === undefined ? "" : String(cond.value);
    if (valor.trim() === "") {
      sinValor.push(indice + 1);
      return;
    }
    filtros.push({ key: cond.key, op: cond.op, value: valor.trim() });
  });
  if (sinValor.length) {
    // A medias no: una condición sin valor filtraría por «vacío», que no es lo
    // que se está pidiendo.
    filterStatusEl.textContent = `Falta el valor de ${
      sinValor.length === 1 ? "la condición" : "las condiciones"
    } ${sinValor.join(", ")}.`;
    return;
  }
  filterDialog.close();
  await ctx.onCambio({ filters: filtros });
});

// ---------------------------------------------------- panel de orden (dialog)
function openSortDialog(database, view, onCambio) {
  sortStatusEl.textContent = "";
  document.getElementById("sort-view-name").textContent = view.name;
  sortDialogCtx = { database, view, onCambio, claves: clavesDeOrden(view.config) };
  pintarFilasDeOrden();
  sortDialog.showModal();
}

function pintarFilasDeOrden() {
  const ctx = sortDialogCtx;
  if (!ctx) return;
  sortRowsEl.textContent = "";
  ctx.claves.forEach((clave, indice) => {
    sortRowsEl.appendChild(filaDeOrden(ctx.database, ctx.claves, clave, indice));
  });
  if (ctx.claves.length === 0) {
    const vacio = document.createElement("p");
    vacio.className = "filter-empty";
    vacio.textContent = "Sin claves: las filas salen en el orden de la base.";
    sortRowsEl.appendChild(vacio);
  }
  document.getElementById("btn-add-sort").disabled = ctx.database.schema_def.length === 0;
}

function filaDeOrden(database, claves, clave, indice) {
  const fila = document.createElement("div");
  fila.className = "filter-row";

  const propSelect = document.createElement("select");
  propSelect.className = "filter-prop";
  propSelect.setAttribute("aria-label", `Propiedad de la clave ${indice + 1}`);
  const usadas = new Set(claves.filter((_, i) => i !== indice).map((otra) => otra.key));
  // El título se puede ordenar como cualquier columna (texto natural): no es
  // una propiedad del `schema`, pero el servidor lo conoce con la clave
  // `title`, así que se ofrece el primero.
  [{ key: "title", name: database.title_name || "Título" }, ...database.schema_def].forEach((prop) => {
    if (usadas.has(prop.key)) return; // una propiedad, una clave
    const opcion = document.createElement("option");
    opcion.value = prop.key;
    opcion.textContent = prop.name;
    propSelect.appendChild(opcion);
  });
  if (![...propSelect.options].some((o) => o.value === clave.key)) {
    const opcion = document.createElement("option");
    opcion.value = clave.key;
    opcion.textContent = nombreDePropiedad(database, clave.key);
    propSelect.appendChild(opcion);
  }
  propSelect.value = clave.key;
  propSelect.addEventListener("change", () => {
    clave.key = propSelect.value;
  });

  const dirSelect = document.createElement("select");
  dirSelect.className = "filter-op";
  dirSelect.setAttribute("aria-label", `Dirección de la clave ${indice + 1}`);
  [
    ["asc", "ascendente ↑"],
    ["desc", "descendente ↓"],
  ].forEach(([valor, etiqueta]) => {
    const opcion = document.createElement("option");
    opcion.value = valor;
    opcion.textContent = etiqueta;
    dirSelect.appendChild(opcion);
  });
  dirSelect.value = clave.dir;
  dirSelect.addEventListener("change", () => {
    clave.dir = dirSelect.value === "desc" ? "desc" : "asc";
  });

  const mover = (texto, etiqueta, desplazamiento, desactivado) => {
    const boton = document.createElement("button");
    boton.type = "button";
    boton.className = "filter-move";
    boton.textContent = texto;
    boton.title = etiqueta;
    boton.setAttribute("aria-label", etiqueta);
    boton.disabled = desactivado;
    boton.addEventListener("click", () => {
      const destino = indice + desplazamiento;
      [claves[indice], claves[destino]] = [claves[destino], claves[indice]];
      pintarFilasDeOrden();
    });
    return boton;
  };

  const quitar = document.createElement("button");
  quitar.type = "button";
  quitar.className = "filter-remove";
  quitar.innerHTML = svgIcon("i-x");
  quitar.title = "Quitar esta clave";
  quitar.setAttribute("aria-label", `Quitar la clave ${indice + 1}`);
  quitar.addEventListener("click", () => {
    claves.splice(indice, 1);
    pintarFilasDeOrden();
  });

  fila.append(
    propSelect,
    dirSelect,
    mover("↑", "Subir la clave", -1, indice === 0),
    mover("↓", "Bajar la clave", 1, indice === claves.length - 1),
    quitar,
  );
  return fila;
}

document.getElementById("btn-add-sort").addEventListener("click", () => {
  const ctx = sortDialogCtx;
  if (!ctx) return;
  const usadas = new Set(ctx.claves.map((clave) => clave.key));
  const libres = [
    { key: "title", name: ctx.database.title_name || "Título" },
    ...ctx.database.schema_def,
  ];
  const libre = libres.find((prop) => !usadas.has(prop.key));
  if (!libre) {
    sortStatusEl.textContent = "Ya están todas las propiedades en el orden.";
    return;
  }
  sortStatusEl.textContent = "";
  ctx.claves.push({ key: libre.key, dir: "asc" });
  pintarFilasDeOrden();
});

document.getElementById("btn-cancel-sort").addEventListener("click", () => {
  sortDialog.close();
});

document.getElementById("btn-clear-sort").addEventListener("click", async () => {
  const ctx = sortDialogCtx;
  if (!ctx) return;
  sortDialog.close();
  await ctx.onCambio({ sort: [] });
});

document.getElementById("btn-apply-sort").addEventListener("click", async () => {
  const ctx = sortDialogCtx;
  if (!ctx) return;
  sortDialog.close();
  await ctx.onCambio({
    sort: ctx.claves.map((clave) => ({ key: clave.key, dir: clave.dir })),
  });
});

// ------------------------------------- bases dentro de una nota (P17.2)
/** Pinta, dentro de la nota, la base de un bloque ```mdm-database.
 *
 * La tabla es exactamente la de la base (`renderTable`), celdas editables
 * incluidas: un clic abre el control del tipo de esa celda y lo que se escribe
 * va al `.md` de ESA fila, nunca al de la nota (el bloque guarda la referencia,
 * no los datos). También el título de la fila se edita aquí: es lo que más se
 * toca, y renombrar es lo mismo que renombrar desde la base (cambia el `title`
 * del frontmatter y el fichero `<id8>.md` no se mueve).
 *
 * Lo que NO se hace desde aquí: borrar filas. La tabla va sin columna de
 * acciones (`deletable: false`) porque borrar desde dentro de una nota es
 * demasiado fácil de hacer sin querer; el encabezado (un botón) lleva a la base
 * entera. El esquema SÍ se toca desde la cabecera de cada columna (renombrar,
 * ocultar en la vista, tipo, borrar y «+»), igual que en la página de la base:
 * es el mismo `renderTable` y el mismo PATCH, y el bloque se repinta con lo que
 * devuelve el servidor (`recargar`).
 *
 * Pintar no toca el documento: la valla, el cursor y el autoguardado siguen
 * donde estaban. Al añadir una fila o al guardar una celda se repintan solo el
 * contador y la tabla de este bloque; los demás bloques de la misma base piden
 * sus filas al servidor (`refrescarOtrosEmbeds`).
 *
 * Si la base ya no está, se avisa con `notFound` para que el editor pinte
 * «Base no encontrada: <title|id>»; cualquier otro fallo se propaga con su
 * motivo. En los dos casos el texto de la valla se queda intacto en la nota.
 */
async function renderDatabaseEmbed(block, container) {
  const { id, view, title } = block;
  let database;
  let views;
  try {
    [database, views] = await Promise.all([api(`/databases/${id}`), api(`/databases/${id}/views`)]);
  } catch (e) {
    if (e && e.status === 404) {
      const err = new Error("Base no encontrada");
      err.notFound = true;
      throw err;
    }
    throw new Error(describeError(e));
  }

  // La vista se busca por nombre; si el bloque no la dice (o ya no existe), la
  // primera -- el mismo criterio que selectDatabase.
  let activeView = views.find((v) => view && v.name === view) || views[0] || null;

  // La tabla incrustada usa la vista con la que está configurada, y eso
  // incluye su filtro y su orden: los aplica el mismo `apply_view` del
  // servidor, así que no hay una segunda versión de la regla en el navegador.
  const filtros = filtrosDeVista(activeView);
  let clavesOrden = clavesDeOrden(activeView?.config);
  let rows;
  let total = null;
  try {
    if (filtros.length) {
      const [deLaVista, todas] = await Promise.all([
        api(`/databases/${database.id}/rows?view=${activeView.id}`),
        api(`/databases/${database.id}/rows`),
      ]);
      rows = deLaVista;
      total = todas.length;
    } else {
      rows = await api(
        `/databases/${database.id}/rows` + (activeView ? `?view=${activeView.id}` : ""),
      );
    }
  } catch (e) {
    throw new Error(describeError(e));
  }

  const head = document.createElement("div");
  head.className = "db-embed-head";

  // El nombre de la base es solo un título (como en Notion), no un enlace: la
  // base se abre desde el menú «⋯» del bloque. Se puede ocultar desde ese mismo
  // menú (`hide_title: true` en la valla).
  const open = document.createElement("div");
  open.className = "db-embed-title";
  open.textContent = database.title || title || "Base de datos";
  open.hidden = Boolean(block.hideTitle);

  const meta = document.createElement("span");
  meta.className = "db-embed-meta";

  // Contador y tabla se repintan juntos, con lo que devuelve el servidor: es
  // el contador DE LA VISTA (filas que enseña, no filas de la base) y la tabla
  // ya viene filtrada y ordenada por `?view=`.
  const body = document.createElement("div");
  body.className = "db-embed-body";
  // Comportamiento propio de la copia incrustada (ver `renderTable`): el
  // marcador que deja pasar el clic a través del widget del editor y los dos
  // avisos del guardado.
  const hooks = {
    embed: true,
    // ANTES del PATCH: el servidor publica `rows` en /events antes de
    // responder, así que el aviso llega a esta pestaña antes que el guardado.
    // Se apunta que el cambio es de aquí para descartar el eco: si no, el
    // editor repintaría el bloque desde cero y se llevaría por delante la
    // celda que se está editando (y su input, con el foco dentro).
    onBeforeSave: () => {
      ultimoCambioPropio = { databaseId: String(database.id), ts: Date.now() };
    },
    // Guardado de verdad: los DEMÁS bloques de esta misma base piden sus filas
    // al servidor. Sin esto se quedan con lo de antes, porque el eco del SSE se
    // descarta arriba. Este bloque no entra: su celda ya está repintada con lo
    // que devolvió el PATCH.
    onSaved: () => refrescarOtrosEmbeds(database.id, container),
  };

  // La tabla que hay pintada ahora (se rehace en cada `pintar`) y el «+ Nueva
  // fila», que no: su aviso sobrevive al repintado de las filas.
  let tablaPintada = null;
  const alta = renderAltaEnTabla({ database, vista: activeView, tabla: () => tablaPintada });

  const pintar = (filas, totalBase = null) => {
    // Si en ESTE bloque hay una celda abierta, el repintado no puede llevársela
    // por delante: un guardado en OTRO bloque de la misma base pide aquí filas
    // nuevas (`refrescarOtrosEmbeds`) y vaciar `body` destruiría el `<input>`
    // --y el foco-- de lo que el usuario está escribiendo. Se aparta fila,
    // columna y valor tecleado, y se vuelve a abrir después de pintar.
    const enEdicion = celdaEnEdicion(body);
    meta.textContent = `${activeView ? activeView.name : "Todas"} · ${textoDeRecuento(
      filas.length,
      totalBase,
    )}`;
    body.textContent = "";
    body.append(
      (tablaPintada = renderTable(database, filas, {
        deletable: false,
        hooks,
        // Con filas marcadas, la barra ocupa la cabecera del bloque.
        barraEn: head,
        sort: clavesOrden,
        vacio: filtros.length ? "Ninguna fila cumple el filtro." : null,
        view: activeView,
        onChanged: recargar,
        // Ancho, orden de columnas y ordenación también desde la nota: se
        // guardan en la vista del bloque (y si la base no tiene ninguna, se
        // crea su «Tabla», que pasa a ser la del bloque).
        onCambioColumnas: guardarVistaDelBloque,
        onOrdenar: (key, dir) => guardarVistaDelBloque({ sort: fijarOrden(clavesOrden, key, dir) }),
        // La fila nueva ya está en su sitio (o fuera, si el filtro no la
        // enseña): contador, aviso y los demás bloques de esta base. El eco
        // de este POST se descarta (ver `handleDatabaseEvent`), así que si no
        // se piden aquí se quedan con las filas de antes.
        onAlta: (res) => {
          if (res.visibles !== null) {
            meta.textContent = `${activeView ? activeView.name : "Todas"} · ${textoDeRecuento(
              res.visibles,
              // Mismo criterio que al pintar el bloque: «de M» solo con filtro.
              filtros.length ? res.total : null,
            )}`;
          }
          alta.avisar(res);
          refrescarOtrosEmbeds(database.id, container);
        },
        // Tras una acción en bloque, este bloque se repinta con lo que diga el
        // servidor y los demás bloques de la misma base se enteran igual que
        // tras guardar una celda.
        onRepintar: async () => {
          const embed = embedsDeBase.get(container);
          if (embed) {
            try {
              await embed.refresh();
            } catch (e) {
              reportError("No se pudo refrescar la tabla", e);
            }
          }
          refrescarOtrosEmbeds(database.id, container);
        },
      })),
    );
    if (enEdicion) {
      reabrirCeldaEnEdicion(
        body,
        database,
        filas,
        enEdicion,
        hooks,
        columnasDeVista(database, activeView),
      );
    }
  };

  /** Después de tocar el esquema o la vista desde la cabecera del bloque: se
   * relee todo (base, vistas, filas) porque el servidor acaba de derivar una
   * clave o de cambiar las columnas de la vista. El aviso `updated` de /events
   * también rehace el widget; si eso pasa antes de que llegue la respuesta,
   * este contenedor ya no está en el documento y no se pinta en el vacío. */
  const guardarVistaDelBloque = async (cambios) => {
    try {
      if (activeView) {
        await guardarConfigDeVista(database, activeView, cambios);
      } else {
        activeView = await crearVistaPorDefecto(database, cambios);
      }
    } catch (e) {
      reportError("No se pudo guardar la vista", e);
      return;
    }
    clavesOrden = clavesDeOrden(activeView?.config);
    // El orden de las filas depende de la vista: se vuelven a pedir.
    await recargar();
  };
  const recargar = async () => {
    try {
      const [fresca, vistas] = await Promise.all([
        api(`/databases/${database.id}`),
        api(`/databases/${database.id}/views`),
      ]);
      Object.assign(database, fresca);
      const suya = activeView ? vistas.find((v) => v.id === activeView.id) : null;
      if (suya) Object.assign(activeView, suya);
      const filas = await api(
        `/databases/${database.id}/rows` + (activeView ? `?view=${activeView.id}` : ""),
      );
      if (container.isConnected) pintar(filas);
      refrescarOtrosEmbeds(database.id, container);
    } catch (e) {
      reportError("No se pudo refrescar la tabla de la base", e);
    }
  };

  pintar(rows, total);

  // El bloque se apunta para que otro bloque de la MISMA base pueda pedirle
  // que se repinte con lo que diga el servidor, sin rehacer el widget: así se
  // queda intacto todo lo que vive aquí dentro (el aviso `role=status` y el
  // foco del «+ Nueva fila»). Lo usa `refrescarOtrosEmbeds`.
  container.dataset.databaseId = String(database.id);
  embedsDeBase.set(container, {
    databaseId: String(database.id),
    async refresh() {
      const filas = await api(
        `/databases/${database.id}/rows` + (activeView ? `?view=${activeView.id}` : ""),
      );
      let totalBase = null;
      if (filtros.length) {
        const todas = await api(`/databases/${database.id}/rows`);
        totalBase = todas.length;
      }
      // El bloque pudo irse mientras llegaba la respuesta (el editor rehace
      // los widgets con cada aviso de base): pintar en el hueco que ya no está
      // no rompe nada, pero es trabajo tirado.
      if (container.isConnected) pintar(filas, totalBase);
    },
  });

  // El menú del bloque: abrir la base, elegir su vista, ocultar el título,
  // copiar el Markdown de la valla y quitar el bloque de la página. Todo lo que
  // antes se hacía editando la valla a mano.
  const opciones = document.createElement("button");
  opciones.type = "button";
  opciones.className = "db-embed-menu-btn";
  opciones.dataset.dbAction = "opciones";
  opciones.innerHTML = svgIcon("i-more");
  opciones.setAttribute("aria-label", "Opciones de la base");
  opciones.setAttribute("aria-haspopup", "menu");
  opciones.setAttribute("aria-expanded", "false");
  opciones.title = "Opciones de la base";
  opciones.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") e.stopPropagation();
  });
  opciones.addEventListener("click", (e) => {
    e.stopPropagation();
    abrirMenuDeBloqueDeBase(opciones, { block, database, views, activeView });
  });
  head.classList.toggle("is-title-hidden", Boolean(block.hideTitle));
  head.append(open, meta, opciones);
  container.append(
    head,
    body,
    // La fila se escribe en la propia tabla (fila fantasma): sin recargar la
    // nota ni la página.
    alta,
  );
}

/** El menú «⋯» de una base incrustada en una nota. Lo que cambia la valla
 * (`view`, `hide_title`) lo escribe el editor con `block.actualizar`: la nota
 * se guarda como cualquier otra edición y el bloque se repinta solo. */
function abrirMenuDeBloqueDeBase(anchor, { block, database, views, activeView }) {
  const menu = document.createElement("div");
  menu.className = "db-col-menu db-col-menu-notion";
  const rotulo = document.createElement("div");
  rotulo.className = "db-col-menu-label";
  rotulo.textContent = database.title || "Base de datos";
  menu.appendChild(rotulo);
  menu.appendChild(
    itemDeColumna("Abrir la base de datos", {
      icono: svgIcon("i-expand"),
      alElegir: () => {
        cerrarPanelDeColumna();
        goTo("database", database.id);
      },
    }),
  );
  if (views.length > 1) {
    menu.appendChild(separadorDeMenu());
    const sub = document.createElement("div");
    sub.className = "db-col-menu-label";
    sub.textContent = "Vista";
    menu.appendChild(sub);
    views.forEach((v) => {
      const actual = activeView && v.id === activeView.id;
      menu.appendChild(
        itemDeColumna(v.name, {
          icono: svgIcon(v.type === "board" ? "i-board" : v.type === "calendar" ? "i-calendar" : v.type === "list" ? "i-list" : "i-table"),
          radio: true,
          marca: actual ? "✓" : "",
          alElegir: () => {
            cerrarPanelDeColumna();
            if (!actual) block.actualizar?.({ view: v.name });
          },
        }),
      );
    });
  }
  menu.appendChild(separadorDeMenu());
  menu.appendChild(
    itemDeColumna(block.hideTitle ? "Mostrar el título" : "Ocultar el título", {
      icono: ICONOS_DE_MENU.ocultar,
      alElegir: () => {
        cerrarPanelDeColumna();
        block.actualizar?.({ hideTitle: !block.hideTitle });
      },
    }),
  );
  menu.appendChild(
    itemDeColumna("Copiar como Markdown", {
      icono: svgIcon("i-file-text"),
      alElegir: async () => {
        const texto = block.markdown?.() || "";
        try {
          await navigator.clipboard.writeText(texto);
          const nota = notaDeMenu(menu);
          nota.textContent = "Copiado: es el bloque tal y como se guarda en el `.md` de la nota.";
          nota.hidden = false;
        } catch (e) {
          reportError("No se pudo copiar", e);
        }
      },
    }),
  );
  menu.appendChild(separadorDeMenu());
  menu.appendChild(
    itemDeColumna("Quitar de la página", {
      icono: ICONOS_DE_MENU.borrar,
      peligro: true,
      alElegir: () => {
        cerrarPanelDeColumna();
        block.quitar?.();
      },
    }),
  );
  abrirPanelFlotante(anchor, menu, { etiqueta: "Opciones de la base" });
}

/** La celda que se está editando dentro de un bloque incrustado, si hay
 * alguna: `{rowId, columna, valor, enfocada}`. `columna` es el índice entre las
 * celdas editables de la fila (0 es el título cuando la tabla lo enseña).
 *
 * La usa `pintar()` para no perder lo que se está escribiendo cuando llega un
 * repintado de fuera: un guardado en otro bloque de la misma base repinta este
 * entero (ver `refrescarOtrosEmbeds`), y sin esto el `<input>` de la celda
 * abierta se va con el `body`. */
function celdaEnEdicion(container) {
  const control = container.querySelector(
    'td[data-editable="1"] input, td[data-editable="1"] select, td[data-editable="1"] textarea',
  );
  if (!control) return null;
  // El repintado va a vaciar `body` y a destruir este control: su `blur` no
  // puede guardar nada. `filas` se pidió al servidor ANTES del repintado, así
  // que un PATCH disparado por ese blur dejaría la fila obsoleta --la celda
  // reabierta se sembraría con el valor viejo mientras el `.md` ya tiene el
  // nuevo-- y Esc ya no podría cancelar lo tecleado. `commit()` sale si el
  // control está marcado: lo tecleado vive solo en la celda que se reabre.
  control.__descartado = true;
  const td = control.closest("td");
  const tr = td?.parentElement;
  if (!tr || !tr.dataset.rowId) return null;
  return {
    rowId: String(tr.dataset.rowId),
    columna: celdasEditables(tr).indexOf(td),
    valor: control.value,
    enfocada: td.contains(document.activeElement),
  };
}

/** Vuelve a abrir, en la tabla recién pintada, la celda que estaba en edición
 * (`estado` de `celdaEnEdicion`) con el valor tecleado dentro.
 *
 * La fila se busca por `data-row-id` y la columna por índice: la vista puede
 * venir reordenada o filtrada, pero la fila y sus columnas son las mismas. Si
 * la fila ya no está en la vista no hay nada que reabrir y se descarta.
 *
 * Es un reabrir, no un guardar: `editCell` siembra el control con lo que hay en
 * el servidor y aquí se sustituye por lo tecleado, así que su `seeded` sigue
 * siendo el valor del `.md` y el próximo commit (Enter, Tab o blur) guarda lo
 * que el usuario escribió. El foco solo se recupera si lo tenía: si el
 * repintado lo provocó algo que ya se lo llevó a otro sitio, robarlo sería
 * perder lo que estaba haciendo. */
function reabrirCeldaEnEdicion(container, database, filas, estado, hooks, columnas = null) {
  const fila = filas.find((r) => String(r.id) === estado.rowId);
  if (!fila) return false;
  const tr = container.querySelector(`tr[data-row-id="${CSS.escape(estado.rowId)}"]`);
  const td = tr ? celdasEditables(tr)[estado.columna] : null;
  if (!td) return false;
  // La columna 0 es el título cuando la tabla lo enseña (`renderTable`). Las
  // columnas son las DE LA VISTA (orden y ocultas incluidas), no el schema.
  const muestraTitulo = !database.title_hidden;
  const columnasTabla = columnas || database.schema_def;
  const prop = muestraTitulo
    ? columnasTabla[estado.columna - 1] || null
    : columnasTabla[estado.columna] || null;
  editCell(td, database, fila, prop, hooks);
  const control = td.querySelector("input, select, textarea");
  if (!control) return false;
  control.value = estado.valor;
  if (estado.enfocada) {
    // El foco va DIRECTO al control, no a la celda: `td.focus()` dispara el
    // `blur` del input que acaba de crear `editCell`, y ese blur guarda y
    // repinta la celda -- se llevaría por delante la edición recién reabierta.
    // Solo se mueve el testigo del roving tabindex.
    setRovingCell(td);
    control.focus?.();
    // El cursor, al final de lo tecleado. `number`/`date` no saben de rangos.
    try {
      control.setSelectionRange?.(estado.valor.length, estado.valor.length);
    } catch {
      /* control sin selección de texto */
    }
  }
  return true;
}

// ---------------------------------------- añadir filas desde una vista
// Una vista (tabla incrustada, lista, agenda) se puede tocar sin abrir la base
// entera: «+ Nueva fila» al final crea la fila por el mismo POST que el alta de
// la base y repinta lo justo para que aparezca donde le toque, sin recargar.

/** Último cambio escrito desde esta pestaña: `{databaseId, ts}`. Sirve para no
 * repintar el bloque incrustado con el eco de nuestro propio POST o PATCH (ver
 * `handleDatabaseEvent`): crear una fila y editar una celda publican el mismo
 * evento `rows`, y repintar el bloque desde cero se llevaría por delante la
 * celda que se está editando. */
let ultimoCambioPropio = { databaseId: null, ts: 0 };

/** Crea una fila por el camino de siempre y vuelve a pedir la lista DE LA
 * VISTA ACTIVA.
 *
 * El filtro y el orden los aplica el servidor (`apply_view`), así que la única
 * verdad de «¿la enseña esta vista?» es esa segunda lectura: duplicar el filtro
 * en el navegador acabaría diciendo algo distinto de lo que enseña la tabla.
 * Devuelve `{fila, filas, total, visible}`; `filas` ya viene filtrada y
 * ordenada, y `total` (nulo si la vista no filtra) es el tamaño de la base,
 * que es lo que hace honesto el recuento «N de M» tras un alta.
 */
async function crearFilaEnVista(database, activeView, titulo, extra = {}, body = "") {
  // ANTES del POST: el servidor publica `rows` en /events (M2) antes de
  // responder, así que el aviso le llega a esta pestaña antes que la fila. Se
  // apunta que el cambio es de aquí para ignorar el eco (si no, el editor
  // repinta el bloque desde cero y se lleva por delante el aviso y el foco que
  // se ponen al volver). El cambio de OTRA ventana no pasa por aquí.
  ultimoCambioPropio = { databaseId: String(database.id), ts: Date.now() };
  const fila = await api(`/databases/${database.id}/rows`, {
    method: "POST",
    // `body_markdown` viaja cuando la fila nace de una plantilla de fila: es su
    // cuerpo inicial. Sin plantilla no se manda la clave (cuerpo vacío).
    body: JSON.stringify({
      title: titulo,
      properties: extra,
      ...(body ? { body_markdown: body } : {}),
    }),
  });
  const filas = await api(
    `/databases/${database.id}/rows` + (activeView ? `?view=${activeView.id}` : ""),
  );
  // El total de la base, para el recuento: una vista con filtro tiene que poder
  // seguir diciendo «N de M» después de un alta, y `filas` solo trae las que
  // pasan el filtro. Sin filtro, `filas` ya es la base entera y no se pide nada.
  let total = null;
  if (filtrosDeVista(activeView).length) {
    const todas = await api(`/databases/${database.id}/rows`);
    total = todas.length;
  }
  return { fila, filas, total, visible: filas.some((r) => r.id === fila.id) };
}

/** Los bloques de base pintados en la nota y cómo volver a pintarlos con lo
 * que diga el servidor: `container` -> `{databaseId, refresh()}`. En un
 * `WeakMap` (y no en el elemento) para no dejar basura: cuando el editor
 * rehace un widget o se cambia de nota, la entrada se va con el DOM.
 *
 * Solo cubre los bloques de una nota, que son los que no pueden repintarse por
 * su cuenta: la lista, la agenda y la base entera se repintan con su propia
 * recarga. */
const embedsDeBase = new WeakMap();

/** Repinta los bloques de la nota abierta que enseñan `databaseId`, menos
 * `excepto`, pidiendo a cada uno SUS filas (cada bloque tiene su `view:`, con
 * su filtro y su orden, y la verdad de qué enseña cada vista es del servidor).
 *
 * Hace falta porque en la pestaña que escribe el alta el evento `rows` de
 * `/events` llega ANTES que la respuesta del POST y se descarta como eco (ver
 * `handleDatabaseEvent`): sin esto, una nota con dos vistas de la misma base
 * --`view: Tabla` y `view: Ordenada`, el patrón normal-- dejaba el segundo
 * bloque con el contador y las filas de antes hasta el siguiente aviso.
 *
 * Repinta contador y tabla, nada más: el «+ Nueva fila» de cada bloque (con su
 * aviso y su foco) no se toca. */
function refrescarOtrosEmbeds(databaseId, excepto) {
  const id = String(databaseId);
  for (const container of document.querySelectorAll(".cm-mdm-dblock-body")) {
    if (container === excepto) continue;
    const embed = embedsDeBase.get(container);
    if (!embed || embed.databaseId !== id) continue;
    embed.refresh().catch((e) => {
      console.error("No se pudo refrescar el bloque de la base:", e);
    });
  }
}

/** El aviso de una fila que la vista activa no enseña: su filtro la deja
 * fuera. Se dice en vez de dejar el hueco (una fila fantasma): la fila está en
 * la base, en su `.md` y en «Todas». */
function avisoDeFilaOculta(titulo, nombreVista) {
  return (
    `«${titulo}» se ha añadido, pero la vista «${nombreVista}» no la enseña: ` +
    "su filtro la deja fuera. La fila está en la base (pestaña «Todas») y en su fichero."
  );
}

/** El foco, en el nombre de la fila que se acaba de crear: el `<tr>` de la
 * tabla, la línea de la lista o el chip del calendario (los tres llevan
 * `data-row-id`). Devuelve si la encontró. */
/** La fila recién creada entra con un fundido (`.is-new` en el CSS) para que se
 * vea DÓNDE ha caído: el orden de la vista puede dejarla en medio. Solo
 * presentación; la clase se quita sola. */
function marcarComoNueva(el) {
  if (!el) return;
  el.classList.add("is-new");
  setTimeout(() => el.classList.remove("is-new"), 400);
}

function enfocarFila(container, rowId) {
  const el = container.querySelector(`[data-row-id="${rowId}"]`);
  if (!el) return false;
  marcarComoNueva(el);
  if (el.tagName === "TR") {
    // La tabla no tiene filas enfocables: el nombre es la primera celda, que ya
    // es editable (también en la incrustada). Si por lo que sea no lo es, la
    // celda se enfoca igual: así el lector anuncia el título y el siguiente Tab
    // sigue desde la fila nueva.
    const celda = el.querySelector('td[data-editable="1"]') || el.querySelector("td");
    if (!celda) return false;
    if (celda.dataset.editable === "1") setRovingCell(celda);
    else celda.tabIndex = -1;
    celda.focus();
    return true;
  }
  el.focus();
  return true;
}

/** La fila fantasma que hay abierta ahora (la de una tabla o la tarjeta de un
 * tablero): solo puede haber UNA viva en toda la pantalla. Abrir otra resuelve
 * antes la que hubiera. */
let campoFantasmaVivo = null;

/** El POST de la última fila escrita en una fantasma, mientras está en vuelo.
 * Quien vaya a pedir las filas de una base lo espera antes (`renderDatabase`):
 * si no, cambiar de vista con la fila a medio guardar pintaría la vista nueva
 * sin ella. */
let altaEnVuelo = null;

/** El comportamiento del campo de una fila FANTASMA (la de la tabla y la
 * tarjeta del tablero): la fila no existe en el `.md` mientras se escribe.
 *
 * - Se crea AL RESOLVERSE, con el título completo, en un solo POST (con
 *   `extra` como propiedades). No hay un guardado por letra: el `.md` nunca
 *   tiene un título a medias.
 * - Se resuelve con Enter, Tab, Esc, al perder el foco, con un clic en
 *   cualquier otro sitio (también uno que no mueva el foco), si el repintado
 *   se lleva el campo (cambiar de vista) o al abrir otra fantasma.
 * - Resuelta con texto, se guarda; resuelta vacía, se quita y no se ha creado
 *   nada (`alAbandonar`): ni una llamada al servidor.
 *
 * `alAbandonar(foco)` y `alCrearse(fila, {foco, aLaSiguiente})`: `foco` dice si
 * el usuario sigue ahí (tecla) o ya se fue a otro sitio, y `aLaSiguiente` que
 * salió con Tab.
 */
function armarCampoFantasma(input, { database, extra = {}, alAbandonar, alCrearse }) {
  // Solo una viva: la anterior se resuelve (se guarda o se quita) antes.
  campoFantasmaVivo?.resolver();

  const caja = input.closest("tr, .board-card") || input;
  let cerrada = false;

  // El `blur` no basta: un clic que no mueve el foco (uno sintético, o sobre
  // algo que lo retiene) dejaría la fantasma viva. Cualquier clic fuera de la
  // fila la resuelve.
  const alClicFuera = (e) => {
    if (!caja.contains(e.target)) cerrar();
  };
  // Y si un repintado se lleva el campo, quitar un nodo con el foco no avisa
  // con `blur`: se vigila que siga en el documento.
  const vigia = new MutationObserver(() => {
    if (!input.isConnected) cerrar();
  });
  const yo = { resolver: () => cerrar() };

  const enganchar = () => {
    cerrada = false;
    campoFantasmaVivo = yo;
    document.addEventListener("click", alClicFuera, true);
    vigia.observe(document.body, { childList: true, subtree: true });
  };
  const soltar = () => {
    document.removeEventListener("click", alClicFuera, true);
    vigia.disconnect();
    if (campoFantasmaVivo === yo) campoFantasmaVivo = null;
  };

  async function cerrar({ foco = false, aLaSiguiente = false } = {}) {
    if (cerrada) return;
    cerrada = true;
    soltar();
    const titulo = input.value.trim();
    if (!titulo) {
      alAbandonar(foco); // vacía: no se ha creado nada y no se crea
      return;
    }
    input.readOnly = true;
    // El eco de este POST no debe repintar el bloque de la nota: mismo apunte
    // que `crearFilaEnVista`.
    ultimoCambioPropio = { databaseId: String(database.id), ts: Date.now() };
    const intento = api(`/databases/${database.id}/rows`, {
      method: "POST",
      body: JSON.stringify({ title: titulo, properties: extra }),
    });
    const enVuelo = intento.then(
      () => {},
      () => {},
    );
    altaEnVuelo = enVuelo;
    enVuelo.then(() => {
      if (altaEnVuelo === enVuelo) altaEnVuelo = null;
    });
    let row;
    try {
      row = await intento;
    } catch (e) {
      reportError("No se pudo añadir la fila", e);
      if (input.isConnected && !campoFantasmaVivo) {
        // No se pierde lo escrito: el campo sigue abierto para reintentar.
        input.readOnly = false;
        enganchar();
        input.focus();
      } else {
        alAbandonar(false);
      }
      return;
    }
    // El foco solo se lleva a la fila nueva si el usuario no se ha ido a otro
    // sitio mientras se guardaba.
    const sigueAhi =
      foco && (document.activeElement === input || document.activeElement === document.body);
    await alCrearse(row, { foco: sigueAhi, aLaSiguiente });
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === "Escape" || (e.key === "Tab" && !e.shiftKey)) {
      // Vacía: Tab sigue su camino y el blur la quita.
      if (e.key === "Tab" && !input.value.trim()) return;
      e.preventDefault();
      e.stopPropagation();
      cerrar({ foco: true, aLaSiguiente: e.key === "Tab" });
      return;
    }
    // Lo que no se pare aquí lo recibe quien haya encima: el editor de la
    // nota (la tabla incrustada) o los atajos de la tarjeta del tablero.
    e.stopPropagation();
  });
  input.addEventListener("blur", () => cerrar());
  enganchar();
}

/** El botón de alta: «+ Nueva fila» y, pegado, un desplegable discreto («▾»)
 * con «Nueva fila» arriba y una entrada por plantilla de fila de la base.
 *
 * El «▾» está SIEMPRE, en la tabla de la base, en la incrustada y en cada
 * columna del tablero, y lee las plantillas del servidor al abrirse: así no
 * depende de cuándo se pintó el botón (las plantillas se editan desde el menú
 * de la base y este control sobrevive a los repintados). Sin plantillas, el
 * menú lo dice.
 *
 * No crea nada por sí mismo: `alCrear()` pinta la fila fantasma (o la tarjeta)
 * y `alElegirPlantilla(plantilla)` crea la fila ya rellena. Una plantilla que
 * ya no encaja con la base (`error`, lo dice el servidor) sale apagada con su
 * motivo, en vez de crear una fila a medias.
 */
function botonDeAlta({ database, alCrear, alElegirPlantilla, etiqueta = "Nueva fila" }) {
  const grupo = document.createElement("span");
  grupo.className = "db-new-row has-templates";
  // Dentro de una nota: sin esto el widget del editor se queda el clic.
  grupo.dataset.dbAction = "add-row";

  // El keymap del editor consume Enter/Espacio dentro del widget: se paran
  // aquí para que el botón reciba su `click` de siempre.
  const noPropagar = (e) => {
    if (e.key === "Enter" || e.key === " ") e.stopPropagation();
  };

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn db-add-row-btn";
  btn.innerHTML = `${svgIcon("i-plus")}${escapeHtml(etiqueta)}`;
  // Con una fantasma abierta, el `mousedown` le quitaría el foco y la fila se
  // resolvería ANTES del clic: la tabla cambia de alto, el botón se mueve y el
  // clic cae fuera. Sin mover el foco, la fantasma se resuelve en el propio
  // clic (ver `armarCampoFantasma`) y el botón hace lo suyo.
  const sinRobarFoco = (e) => {
    if (campoFantasmaVivo) e.preventDefault();
  };
  btn.addEventListener("mousedown", sinRobarFoco);
  btn.addEventListener("keydown", noPropagar);
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    alCrear(btn);
  });
  grupo.appendChild(btn);

  const mas = document.createElement("button");
  mas.type = "button";
  mas.className = "btn db-new-row-more";
  mas.textContent = "▾";
  mas.title = "Nueva fila desde una plantilla";
  mas.setAttribute("aria-label", `${etiqueta}: elegir plantilla`);
  mas.setAttribute("aria-haspopup", "menu");
  mas.setAttribute("aria-expanded", "false");
  mas.addEventListener("mousedown", sinRobarFoco);
  mas.addEventListener("keydown", noPropagar);
  let abriendo = false;
  mas.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (abriendo) return;
    abriendo = true;
    // Las plantillas de AHORA: si el servidor no contesta, las que ya había.
    try {
      const fresca = await api(`/databases/${database.id}`);
      database.row_templates = fresca.row_templates || [];
    } catch (_) {
      /* se enseñan las conocidas */
    } finally {
      abriendo = false;
    }
    if (!mas.isConnected) return;
    const plantillas = database.row_templates || [];
    const menu = document.createElement("div");
    menu.className = "db-col-menu";
    menu.appendChild(
      itemDeColumna("+ Nueva fila", {
        alElegir: () => {
          cerrarPanelDeColumna();
          alCrear(btn);
        },
      }),
    );
    if (plantillas.length) {
      menu.appendChild(separadorDeMenu());
      const cabecera = document.createElement("div");
      cabecera.className = "db-col-menu-label";
      cabecera.textContent = "Desde una plantilla";
      menu.appendChild(cabecera);
    }
    plantillas.forEach((plantilla) => {
      const motivo = plantilla.error
        ? `Esta plantilla ya no encaja con la base: ${plantilla.error}`
        : "";
      menu.appendChild(
        itemDeColumna(plantilla.name, {
          motivo,
          alElegir: motivo
            ? () => {
                const nota = notaDeMenu(menu);
                nota.textContent = motivo;
                nota.hidden = false;
              }
            : () => {
                cerrarPanelDeColumna();
                alElegirPlantilla(plantilla, btn);
              },
        }),
      );
    });
    if (!plantillas.length) {
      const nota = notaDeMenu(menu);
      nota.textContent =
        "Esta base no tiene plantillas de fila. Se crean en el menú de la base, «Plantillas de fila».";
      nota.hidden = false;
    }
    abrirPanelFlotante(mas, menu, { etiqueta: "Nueva fila" });
  });
  grupo.appendChild(mas);
  return grupo;
}

/** El «+ Nueva fila» de una TABLA (la página de la base y la incrustada en una
 * nota): el botón de alta y, debajo, el aviso (`role=status`) de lo que pasó
 * con la fila --añadida, o añadida pero fuera del filtro de la vista--. La
 * fila se escribe en la propia tabla (`shell.nuevaFila`, la fila fantasma).
 *
 * `tabla()` devuelve el `shell` de `renderTable` que hay pintado AHORA: la
 * tabla se rehace con cada repintado y este control no.
 */
function renderAltaEnTabla({ database, vista, tabla }) {
  const wrap = document.createElement("div");
  wrap.className = "db-add-row";
  const status = document.createElement("p");
  status.className = "db-add-row-status";
  status.setAttribute("role", "status");
  const boton = botonDeAlta({
    database,
    alCrear: (btn) => {
      status.textContent = "";
      tabla()?.nuevaFila({ volverA: btn });
    },
    alElegirPlantilla: (plantilla, btn) => {
      status.textContent = "";
      tabla()?.filaDePlantilla(plantilla, { volverA: btn });
    },
  });
  wrap.append(boton, status);
  /** Lo llama la tabla (`onAlta`) cuando la fila ya está en su sitio. */
  wrap.avisar = ({ fila, visible }) => {
    const titulo = fila.title || "Sin título";
    status.textContent = visible
      ? `Fila «${titulo}» añadida.`
      : avisoDeFilaOculta(titulo, vista?.name || "");
  };
  return wrap;
}

/** El «+ Nueva fila» de la lista y de la agenda del calendario.
 *
 * (La tabla no usa este: ahí la fila se escribe en la propia tabla, ver
 * `renderAltaEnTabla`.)
 *
 * Al pulsarlo no se crea nada todavía: aparece el campo con el nombre de la
 * fila y el foco dentro, y la fila nace al pulsar Enter. Así el nombre se
 * escribe de seguido y no queda un «Sin título.md» si el usuario se va a
 * medias -- el título ES el nombre del fichero de la fila (M4).
 *
 * `onCreate(nombre)` hace el POST (devuelve la promesa de `crearFilaEnVista`)
 * y `onDone(resultado)` repinta la superficie y lleva el foco a la fila nueva.
 * El aviso (`role=status`) vive aquí dentro, así que sobrevive al repintado de
 * las filas y se lee sin mover el foco.
 */
function renderAddRowControl({
  vista,
  plantillas = [],
  onCreate,
  onDone,
  className = "db-add-row",
}) {
  const wrap = document.createElement("div");
  wrap.className = className;
  // Dentro de una nota, el bloque de la base es un widget del editor con un
  // `mousedown` que se lleva el clic (previene el default y pone el cursor en
  // la valla) salvo en lo que lleve `data-db-action` (ver entry.js). Sin esto,
  // pulsar «Nueva fila» en la tabla incrustada metería el cursor en el bloque
  // en vez de abrir el campo.
  wrap.dataset.dbAction = "add-row";

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn db-add-row-btn";
  btn.innerHTML = `${svgIcon("i-plus")}Nueva fila`;

  const form = document.createElement("form");
  form.className = "db-add-row-form";
  form.hidden = true;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "db-add-row-input";
  input.placeholder = "Nombre de la fila";
  input.setAttribute("aria-label", "Nombre de la fila nueva");
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.className = "btn btn-primary";
  submit.textContent = "Añadir";
  // El desplegable de plantillas de fila (función 29): elegir una rellena el
  // alta con sus valores por defecto y su cuerpo. Sin plantillas en la base no
  // se pinta nada: un desplegable con una sola opción vacía es ruido.
  const selectorPlantilla = document.createElement("select");
  selectorPlantilla.className = "db-add-row-template";
  selectorPlantilla.setAttribute("aria-label", "Plantilla de fila");
  if (plantillas.length) {
    const vacia = document.createElement("option");
    vacia.value = "";
    vacia.textContent = "Sin plantilla";
    selectorPlantilla.appendChild(vacia);
    plantillas.forEach((plantilla) => {
      const opcion = document.createElement("option");
      opcion.value = plantilla.name;
      opcion.textContent = plantilla.name;
      selectorPlantilla.appendChild(opcion);
    });
    selectorPlantilla.addEventListener("change", () => input.focus());
    // Va delante del nombre: primero se elige la plantilla y luego se escribe
    // el nombre. `append` y no `insertBefore`: el `input` aún no es hijo del
    // formulario en este punto (se añade justo abajo).
    form.append(selectorPlantilla);
  } else {
    selectorPlantilla.hidden = true;
  }
  const plantillaElegida = () =>
    plantillas.find((plantilla) => plantilla.name === selectorPlantilla.value) || null;
  form.append(input, submit);

  const status = document.createElement("p");
  status.className = "db-add-row-status";
  status.setAttribute("role", "status");

  wrap.append(btn, form, status);

  const abrir = () => {
    status.textContent = "";
    btn.hidden = true;
    form.hidden = false;
    input.focus();
  };
  const cerrar = () => {
    input.value = "";
    selectorPlantilla.value = "";
    form.hidden = true;
    btn.hidden = false;
    btn.focus();
  };

  const enviar = () => {
    if (typeof form.requestSubmit === "function") form.requestSubmit();
    else submit.click();
  };

  btn.addEventListener("click", abrir);
  // El keymap del editor consume Enter/Espacio dentro del widget (con
  // preventDefault), así que un <button> de aquí no se activa con el teclado
  // si no lo hace él mismo.
  btn.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    e.stopPropagation();
    abrir();
  });
  submit.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    e.stopPropagation();
    enviar();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      // Enter crea la fila sin esperar al submit implícito del navegador.
      e.preventDefault();
      e.stopPropagation();
      enviar();
      return;
    }
    if (e.key !== "Escape") return;
    // Escape cancela el alta sin crear nada y devuelve el foco al botón.
    e.preventDefault();
    e.stopPropagation();
    cerrar();
  });

  let creando = false;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const titulo = input.value.trim();
    if (!titulo) {
      input.focus(); // el título es el nombre del fichero: sin él no hay fila
      return;
    }
    if (creando) return; // Enter dos veces seguidas = una sola fila
    creando = true;
    input.disabled = true;
    submit.disabled = true;
    try {
      const res = await onCreate(titulo, plantillaElegida());
      cerrar();
      status.textContent = res.visible
        ? `Fila «${titulo}» añadida.`
        : avisoDeFilaOculta(titulo, vista?.name || "");
      onDone?.(res);
    } catch (err) {
      // No se pierde lo escrito: el campo se queda abierto con el nombre.
      reportError("No se pudo añadir la fila", err);
      input.focus();
    } finally {
      creando = false;
      input.disabled = false;
      submit.disabled = false;
    }
  });

  return wrap;
}

// ----------------------------------------------------- relaciones entre bases
// Una celda de relación guarda en el `.md` el **id** de una fila (y el schema
// dice de qué base: `target_database`). El servidor devuelve además `related`,
// los ids de la respuesta resueltos a `{id, title, base_id}` con una sola
// consulta. Aquí vive lo que la interfaz hace con eso: enseñar el título
// (nunca el id), decir «(borrada)» cuando el id ya no resuelve, y elegir filas
// en la celda.

/** El tope de filas que ofrece el selector de una celda de relación.
 *
 * Es el mismo del selector de `/page` (PAGE_CHOICES_LIMIT en
 * `editor-src/page-choice.js`): 50 filas son más de las que se recorren antes
 * de escribir una letra más, y lo que queda fuera se avisa en una fila final
 * («… y N más, sigue escribiendo»). app.js no importa el bundle del editor, así
 * que la regla se repite aquí; es una constante, no una copia del motor. */
const RELATION_CHOICES_LIMIT = 50;

/** Los ids que enlaza una celda de relación, normalizados.
 *
 * `properties` trae un id suelto (relación simple) o una lista (múltiple);
 * ausente, `null` o `""` es «sin valor». El servidor **omite la clave en
 * `related`** cuando la celda está vacía: se lee como lista vacía. */
function idsDeRelacion(value) {
  if (value === null || value === undefined || value === "") return [];
  const crudos = Array.isArray(value) ? value : [value];
  const ids = [];
  for (const crudo of crudos) {
    const id = String(crudo ?? "").trim();
    if (id && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

/** Las filas que resolvió el servidor para una celda (`related`), por id.
 * La clave ausente es una celda sin valor: el mapa vacío lo dice. */
function refsDeRelacion(row, key) {
  const refs = row?.related?.[key];
  const mapa = new Map();
  if (!Array.isArray(refs)) return mapa;
  refs.forEach((ref) => {
    if (ref && ref.id != null) mapa.set(String(ref.id), ref);
  });
  return mapa;
}

/** ¿La relación enlaza varias filas?
 *
 * El `.md` guarda `multiple` tal y como lo leyó el parser de YAML: `false`
 * puede llegar como la cadena `"false"` (verdadera en Python). Se normaliza
 * igual que el servidor (`PropertyDef.multiple_is_truthy`). */
function relacionMultiple(prop) {
  const valor = prop?.multiple;
  if (valor === true) return true;
  if (valor === false || valor === undefined || valor === null) return false;
  return ["true", "1", "yes", "on", "si", "sí"].includes(
    String(valor).trim().toLowerCase(),
  );
}

/** Sin mayúsculas ni tildes: «ano» tiene que encontrar «Año». Misma regla que
 * `plegar` del selector de `/page`. */
function plegarTexto(texto) {
  return String(texto ?? "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

/** Las filas candidatas de una relación, filtradas y ordenadas como el
 * selector de `/page`: primero las que empiezan por lo escrito y luego por
 * título, sin distinguir mayúsculas ni tildes.
 *
 * Devuelve `{filas, ocultas}` con el tope ya aplicado: lo que sobra se cuenta
 * para poder avisarlo, que es lo que evita que una lista recortada parezca el
 * vault entero. */
function filtrarFilasDeRelacion(filas, consulta, limite = RELATION_CHOICES_LIMIT) {
  const q = plegarTexto(String(consulta ?? "").trim());
  const lista = Array.isArray(filas) ? filas : [];
  const porTitulo = (a, b) =>
    String(a.title || "").localeCompare(String(b.title || ""), "es", {
      sensitivity: "base",
    });
  let ordenadas;
  if (!q) {
    ordenadas = lista.slice().sort(porTitulo);
  } else {
    const encontradas = [];
    lista.forEach((fila) => {
      const titulo = plegarTexto(fila.title || "");
      if (!titulo.includes(q)) return;
      encontradas.push({ fila, rango: titulo.startsWith(q) ? 0 : 1 });
    });
    encontradas.sort((a, b) => a.rango - b.rango || porTitulo(a.fila, b.fila));
    ordenadas = encontradas.map((m) => m.fila);
  }
  const tope =
    Number.isFinite(limite) && limite >= 0 ? Math.floor(limite) : RELATION_CHOICES_LIMIT;
  return { filas: ordenadas.slice(0, tope), ocultas: Math.max(0, ordenadas.length - tope) };
}

/** Una celda de relación en HTML plano (la lista, la tarjeta del tablero):
 * el TÍTULO de cada fila enlazada, o «(borrada)» en gris con el id en el
 * `title` para poder buscarla. Nunca el id como texto. */
function htmlDeRelacion(value, prop, row) {
  const ids = idsDeRelacion(value);
  if (!ids.length) return "—";
  const refs = refsDeRelacion(row, prop.key);
  return `<span class="rel-cell">${ids
    .map((id) => {
      const ref = refs.get(id);
      const titulo = ref?.title || null;
      return titulo
        ? `<span class="rel-chip">${escapeHtml(titulo)}</span>`
        : `<span class="rel-chip rel-borrada" title="${escapeHtml(id)}">(borrada)</span>`;
    })
    .join("")}</span>`;
}

/** Abre la fila a la que apunta una celda de relación: una página más. */
async function abrirFilaDeRelacion(ref) {
  if (!ref?.id) return;
  await goTo("note", ref.id);
}

/** Pinta una celda de relación con los títulos que resolvió el servidor.
 *
 * El clic en un título abre esa fila; por eso editar la celda no puede ser el
 * clic (se comería la navegación): se edita con el lápiz de la propia celda o
 * con Enter sobre ella. En una celda vacía no hay títulos y el clic sigue
 * abriendo el selector, como en cualquier otra celda. */
function pintarRelacionEnCelda(td, database, row, prop, hooks = null) {
  const ids = idsDeRelacion(row.properties?.[prop.key]);
  td.textContent = "";
  if (!ids.length) {
    td.textContent = "—";
    return;
  }
  const refs = refsDeRelacion(row, prop.key);
  const caja = document.createElement("span");
  caja.className = "rel-cell";
  ids.forEach((id) => {
    const ref = refs.get(id);
    const titulo = ref?.title || null;
    if (!titulo) {
      // Fila borrada: no es un enlace (no hay a dónde ir). El `title` es el id
      // LITERAL, que es lo que hace falta para buscarla; la explicación va en
      // el `aria-label`.
      const rota = document.createElement("span");
      rota.className = "rel-chip rel-borrada";
      rota.textContent = "(borrada)";
      rota.title = id;
      rota.setAttribute("aria-label", `La fila ${id} ya no existe`);
      caja.appendChild(rota);
      return;
    }
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "rel-chip";
    chip.textContent = titulo;
    chip.title = `Abrir «${titulo}»`;
    chip.setAttribute("aria-label", `Abrir «${titulo}»`);
    // La tabla tiene UNA parada de Tab (la celda): los enlaces no añaden otra.
    chip.tabIndex = -1;
    if (hooks?.embed) chip.dataset.dbAction = "abrir-fila-relacion";
    chip.addEventListener("click", (e) => {
      e.stopPropagation();
      abrirFilaDeRelacion(ref, chip);
    });
    chip.addEventListener("keydown", (e) => e.stopPropagation());
    caja.appendChild(chip);
  });
  td.appendChild(caja);
  const lapiz = document.createElement("button");
  lapiz.type = "button";
  lapiz.className = "rel-edit";
  lapiz.innerHTML = svgIcon("i-pencil");
  lapiz.title = `Elegir las filas de «${prop.name}»`;
  lapiz.setAttribute("aria-label", `Elegir las filas de «${prop.name}»`);
  lapiz.tabIndex = -1;
  if (hooks?.embed) lapiz.dataset.dbAction = "editar-relacion";
  lapiz.addEventListener("click", (e) => {
    e.stopPropagation();
    editarCeldaDeRelacion(td, database, row, prop, hooks);
  });
  lapiz.addEventListener("keydown", (e) => e.stopPropagation());
  td.appendChild(lapiz);
}

// --- el selector de filas de una celda -----------------------------------

// El selector abierto (uno a la vez). Cuelga de <body>: `.table-wrap` tiene
// scroll horizontal y un desplegable dentro del `<td>` saldría recortado.
let selectorDeRelacion = null;

function cerrarSelectorDeRelacion({ foco = false } = {}) {
  const abierto = selectorDeRelacion;
  if (!abierto) return;
  selectorDeRelacion = null;
  abierto.limpiar();
  abierto.panel.remove();
  if (foco && abierto.ancla?.isConnected) abierto.ancla.focus({ preventScroll: true });
}

/** Abre el selector de filas de la base destino sobre una celda de relación.
 *
 * Reglas del selector de `/page`: se filtra escribiendo, sin mayúsculas ni
 * tildes, y la lista se corta a 50 con «… y N más». Con `multiple` se añaden
 * varias filas y se quitan de una en una (la × de la fila elegida); sin él,
 * elegir sustituye a la anterior y el panel se cierra. Cada cambio se guarda
 * en el `.md` por el mismo PATCH que una celda. */
async function editarCeldaDeRelacion(td, database, row, prop, hooks = null) {
  if (selectorDeRelacion) return;
  const target = String(prop.target_database || "");
  let base = null;
  let filas = [];
  let error = "";
  if (!target) {
    error =
      "Esta relación no dice a qué base apunta: elígela en «Propiedades» (el menú de la columna).";
  } else {
    try {
      const [datosBase, lista] = await Promise.all([
        api(`/databases/${target}`),
        api(`/databases/${target}/rows`),
      ]);
      base = datosBase;
      filas = Array.isArray(lista) ? lista : [];
    } catch (e) {
      error =
        e?.status === 404
          ? "La base a la que apunta esta relación ya no existe."
          : `No se pudieron leer las filas: ${describeError(e)}`;
    }
  }
  // La tabla pudo repintarse mientras llegaba la respuesta (otro bloque, otra
  // pestaña): sin ancla no hay dónde colgar el panel.
  if (!td.isConnected) return;

  const panel = document.createElement("div");
  panel.className = "rel-picker";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", `Elegir las filas de «${prop.name}»`);

  const cabecera = document.createElement("div");
  cabecera.className = "rel-picker-head";
  const nombre = document.createElement("span");
  nombre.className = "rel-picker-name";
  nombre.textContent = prop.name;
  const modo = document.createElement("span");
  modo.className = "rel-picker-mode";
  modo.textContent = relacionMultiple(prop) ? "varias filas" : "una fila";
  cabecera.append(nombre, modo);

  const elegidas = document.createElement("div");
  elegidas.className = "rel-picker-chosen";

  const buscar = document.createElement("input");
  buscar.type = "search";
  buscar.className = "rel-picker-search";
  buscar.placeholder = "Buscar por título…";
  buscar.autocomplete = "off";
  buscar.setAttribute("aria-label", "Buscar una fila por su título");
  buscar.disabled = Boolean(error) || !filas.length;

  const lista = document.createElement("div");
  lista.className = "rel-picker-list";
  lista.setAttribute("role", "listbox");

  const nota = document.createElement("p");
  nota.className = "rel-picker-note";
  nota.setAttribute("role", "status");

  panel.append(cabecera, elegidas);
  if (!error) panel.append(buscar);
  panel.append(lista, nota);

  const estado = { activo: -1, opciones: [] };
  const valorActual = () => idsDeRelacion(row.properties?.[prop.key]);

  // Un PATCH por turno, en cadena: dos toques seguidos en una relación
  // múltiple no pueden pisarse (el segundo se calcula sobre lo que dejó el
  // primero, ya guardado).
  let cola = Promise.resolve();
  const encolar = (fn) => {
    cola = cola.then(fn).catch(() => {});
    return cola;
  };

  const guardarIds = async (ids) => {
    const valor = relacionMultiple(prop) ? (ids.length ? ids : null) : ids[0] ?? null;
    if (sameValue(valor, row.properties?.[prop.key] ?? null)) return;
    try {
      await saveRow(database, row, { properties: { [prop.key]: valor } }, hooks);
      nota.textContent = "Guardado.";
    } catch (e) {
      nota.textContent = `No se pudo guardar: ${describeError(e)}`;
      reportError("No se pudo guardar la relación", e);
    }
    repaintCell(td, database, row, prop, false, hooks);
    pintarElegidas();
    pintarOpciones();
    // El foco sigue en el buscador: elegir en una relación múltiple no puede
    // devolverlo a la celda a mitad de la selección.
    if (selectorDeRelacion) buscar.focus({ preventScroll: true });
  };

  function pintarElegidas() {
    elegidas.textContent = "";
    const ids = valorActual();
    if (!ids.length) {
      const vacio = document.createElement("span");
      vacio.className = "rel-picker-empty";
      vacio.textContent = "Sin filas elegidas";
      elegidas.appendChild(vacio);
      return;
    }
    const refs = refsDeRelacion(row, prop.key);
    ids.forEach((id) => {
      const ref = refs.get(id);
      const chip = document.createElement("span");
      chip.className = "rel-picker-chip" + (ref?.title ? "" : " rel-borrada");
      const texto = document.createElement("span");
      texto.className = "rel-picker-chip-text";
      texto.textContent = ref?.title || "(borrada)";
      if (!ref?.title) texto.title = id;
      const quitar = document.createElement("button");
      quitar.type = "button";
      quitar.className = "rel-picker-chip-x";
      quitar.innerHTML = svgIcon("i-x");
      quitar.title = `Quitar «${ref?.title || "la fila borrada"}»`;
      quitar.setAttribute("aria-label", quitar.title);
      quitar.addEventListener("click", (e) => {
        e.stopPropagation();
        encolar(() => guardarIds(valorActual().filter((otro) => otro !== id)));
      });
      chip.append(texto, quitar);
      elegidas.appendChild(chip);
    });
  }

  function elegir(fila) {
    const id = String(fila.id);
    if (!relacionMultiple(prop)) {
      encolar(() => guardarIds([id])).then(() => {
        // Si el guardado falló, el panel se queda abierto con el motivo dentro
        // (cerrarlo dejaría al usuario con un aviso y sin el sitio donde
        // arreglarlo).
        if (idsDeRelacion(row.properties?.[prop.key]).includes(id)) {
          cerrarSelectorDeRelacion({ foco: true });
        }
      });
      return;
    }
    encolar(() => {
      const ids = valorActual();
      const nuevos = ids.includes(id) ? ids.filter((otro) => otro !== id) : [...ids, id];
      return guardarIds(nuevos);
    });
  }

  function pintarOpciones() {
    lista.textContent = "";
    estado.opciones = [];
    estado.activo = -1;
    if (error) {
      const p = document.createElement("p");
      p.className = "rel-picker-note";
      p.textContent = error;
      lista.appendChild(p);
      return;
    }
    const { filas: visibles, ocultas } = filtrarFilasDeRelacion(filas, buscar.value);
    if (!visibles.length) {
      const p = document.createElement("p");
      p.className = "rel-picker-note";
      p.textContent = filas.length
        ? "Ninguna fila coincide con lo escrito."
        : `La base «${base?.title || "de destino"}» todavía no tiene filas.`;
      lista.appendChild(p);
      return;
    }
    const elegidasIds = new Set(valorActual());
    visibles.forEach((fila) => {
      const id = String(fila.id);
      const marcada = elegidasIds.has(id);
      const opcion = document.createElement("button");
      opcion.type = "button";
      opcion.className = "rel-picker-option" + (marcada ? " is-chosen" : "");
      opcion.setAttribute("role", "option");
      opcion.setAttribute("aria-selected", marcada ? "true" : "false");
      const titulo = document.createElement("span");
      titulo.className = "rel-picker-option-title";
      titulo.textContent = fila.title || "Sin título";
      const suBase = document.createElement("span");
      suBase.className = "rel-picker-option-base";
      suBase.textContent = base?.title || "su base";
      opcion.append(titulo, suBase);
      if (marcada) {
        const marca = document.createElement("span");
        marca.className = "rel-picker-check";
        marca.setAttribute("aria-hidden", "true");
        marca.textContent = "✓";
        opcion.appendChild(marca);
      }
      opcion.addEventListener("click", (e) => {
        e.stopPropagation();
        elegir(fila);
      });
      estado.opciones.push(opcion);
      lista.appendChild(opcion);
    });
    if (ocultas > 0) {
      const mas = document.createElement("p");
      mas.className = "rel-picker-more";
      mas.textContent = `… y ${ocultas} más, sigue escribiendo`;
      lista.appendChild(mas);
    }
  }

  buscar.addEventListener("input", () => pintarOpciones());
  buscar.addEventListener("keydown", (e) => {
    // Dentro de una nota, lo que no se consuma aquí acaba en el keymap de
    // CodeMirror (su Enter insertaría un salto en el `.md` de la nota).
    e.stopPropagation();
    if (e.key === "Enter") {
      e.preventDefault();
      const opcion = estado.opciones[Math.max(estado.activo, 0)];
      opcion?.click();
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    if (!estado.opciones.length) return;
    e.preventDefault();
    estado.activo =
      e.key === "ArrowDown"
        ? (estado.activo + 1) % estado.opciones.length
        : (estado.activo - 1 + estado.opciones.length) % estado.opciones.length;
    estado.opciones.forEach((op, i) => op.classList.toggle("is-active", i === estado.activo));
    estado.opciones[estado.activo].scrollIntoView({ block: "nearest" });
  });

  document.body.appendChild(panel);
  colocarPanelFlotante(panel, td);
  const fuera = (e) => {
    if (panel.contains(e.target) || td.contains(e.target)) return;
    cerrarSelectorDeRelacion();
  };
  // Escape se escucha en el documento y no en el buscador: cuando la relación
  // no tiene base destino el panel no lleva buscador, así que no habría ningún
  // control enfocado que recibiera la tecla, y un panel que solo se cierra con
  // el ratón deja tirado a quien va con el teclado.
  const teclas = (e) => {
    if (e.key !== "Escape") return;
    e.preventDefault();
    e.stopPropagation();
    cerrarSelectorDeRelacion({ foco: true });
  };
  const recolocar = () => {
    if (!td.isConnected) {
      cerrarSelectorDeRelacion();
      return;
    }
    colocarPanelFlotante(panel, td);
  };
  document.addEventListener("mousedown", fuera, true);
  document.addEventListener("keydown", teclas, true);
  window.addEventListener("scroll", recolocar, true);
  window.addEventListener("resize", recolocar);
  // El panel crece al llegar las filas (ya están) o al escribir: se recoloca
  // para no quedarse pegado al borde de la ventana.
  const observador =
    typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => recolocar());
  observador?.observe(panel);
  selectorDeRelacion = {
    panel,
    ancla: td,
    limpiar: () => {
      observador?.disconnect();
      document.removeEventListener("mousedown", fuera, true);
      document.removeEventListener("keydown", teclas, true);
      window.removeEventListener("scroll", recolocar, true);
      window.removeEventListener("resize", recolocar);
    },
  };
  pintarElegidas();
  pintarOpciones();
  if (error) {
    panel.tabIndex = -1;
    panel.focus({ preventScroll: true });
  } else {
    buscar.focus({ preventScroll: true });
  }
}

// ------------------------------------------------------------------ personas
// Una celda `person` guarda en el `.md` el **id** de un usuario (tabla
// `users`); el servidor devuelve además `people`, con su nombre visible y su
// color (una consulta por respuesta, como `related`). Aquí vive lo que la
// interfaz hace con eso: enseñar el nombre y nunca el id, decir «(ya no está)»
// cuando el id ya no resuelve, y elegir la persona en la celda.

/** El tope de personas que ofrece el selector: mismo criterio que las filas de
 * una relación, no es una lista que se recorra entera escribiendo. */
const PERSONAS_CHOICES_LIMIT = 50;

/** `id -> {id, display_name, username, color}` de `GET /people`, y la promesa en
 * curso (una sola petición aunque haya diez celdas esperando). */
const personasCache = new Map();
let personasPromesa = null;

function cargarPersonas({ forzar = false } = {}) {
  if (personasPromesa && !forzar) return personasPromesa;
  personasPromesa = api("/people")
    .then((gente) => {
      personasCache.clear();
      (Array.isArray(gente) ? gente : []).forEach((p) => {
        if (p && p.id != null) personasCache.set(String(p.id), p);
      });
      return [...personasCache.values()];
    })
    .catch((e) => {
      personasPromesa = null; // el siguiente intento vuelve a pedirlas
      throw e;
    });
  return personasPromesa;
}

/** La persona que el servidor resolvió para una fila (`people`), o `null`. */
function personaDeFila(row, key) {
  const ref = row?.people?.[key];
  return ref && ref.id != null && ref.display_name ? ref : null;
}

/** El id de usuario que guarda una celda de persona ('' si está vacía). */
function personaIdDeFila(row, key) {
  return String(row?.properties?.[key] ?? "").trim();
}

/** El nombre visible de un id de usuario: el de la fila, o el del directorio. */
function nombreDePersona(id, row = null, key = null) {
  const ref = row && key ? row?.people?.[key] : null;
  if (ref?.display_name) return ref.display_name;
  return personasCache.get(String(id ?? ""))?.display_name || null;
}

/** Una celda de persona en HTML plano (la lista, la tarjeta del tablero, la
 * ficha de la fila): el NOMBRE con su color; nunca el id. */
function htmlDePersona(value, prop, row) {
  const id = String(value ?? "").trim();
  if (!id) return "—";
  const ref = personaDeFila(row, prop.key);
  if (!ref) {
    return `<span class="rel-cell"><span class="rel-chip rel-borrada person-gone" title="${escapeHtml(id)}">(ya no está)</span></span>`;
  }
  const color = ref.color ? ` style="background:${escapeHtml(ref.color)}"` : "";
  const marca = avatarHtml(ref.avatar) || `<span class="person-dot"${color} aria-hidden="true"></span>`;
  return `<span class="person-cell">${marca}<span class="person-name">${escapeHtml(ref.display_name)}</span></span>`;
}

/** Pinta la celda de una persona (tabla y tabla incrustada). El clic abre el
 * selector; el lápiz hace lo mismo sin depender de que haya hueco. */
function pintarPersonaEnCelda(td, database, row, prop, hooks = null) {
  const id = personaIdDeFila(row, prop.key);
  td.textContent = "";
  if (!id) {
    td.textContent = "—";
    return;
  }
  const ref = personaDeFila(row, prop.key);
  const caja = document.createElement("span");
  caja.className = "person-cell";
  if (!ref) {
    const roto = document.createElement("span");
    roto.className = "person-gone";
    roto.textContent = "(ya no está)";
    roto.title = id;
    roto.setAttribute("aria-label", `El usuario ${id} ya no existe`);
    caja.appendChild(roto);
  } else {
    let punto = avatarImg(ref.avatar);
    if (!punto) {
      punto = document.createElement("span");
      punto.className = "person-dot";
      if (ref.color) punto.style.background = ref.color;
      punto.setAttribute("aria-hidden", "true");
    }
    const nombre = document.createElement("span");
    nombre.className = "person-name";
    nombre.textContent = ref.display_name;
    caja.title = ref.username ? `${ref.display_name} (@${ref.username})` : ref.display_name;
    caja.append(punto, nombre);
  }
  td.appendChild(caja);
  const lapiz = document.createElement("button");
  lapiz.type = "button";
  lapiz.className = "rel-edit";
  lapiz.innerHTML = svgIcon("i-pencil");
  lapiz.title = `Elegir la persona de «${prop.name}»`;
  lapiz.setAttribute("aria-label", `Elegir la persona de «${prop.name}»`);
  lapiz.tabIndex = -1;
  if (hooks?.embed) lapiz.dataset.dbAction = "editar-persona";
  lapiz.addEventListener("click", (e) => {
    e.stopPropagation();
    editarCeldaDePersona(td, database, row, prop, hooks).catch((err) =>
      reportError("No se pudo abrir el selector de personas", err),
    );
  });
  lapiz.addEventListener("keydown", (e) => e.stopPropagation());
  td.appendChild(lapiz);
}

// --- el selector de personas de una celda --------------------------------

let selectorDePersona = null;

function cerrarSelectorDePersona({ foco = false } = {}) {
  const abierto = selectorDePersona;
  if (!abierto) return;
  selectorDePersona = null;
  abierto.limpiar();
  abierto.panel.remove();
  if (foco && abierto.ancla?.isConnected) abierto.ancla.focus({ preventScroll: true });
}

/** Abre el selector de personas sobre una celda `person`.
 *
 * Se filtra escribiendo (sin mayúsculas ni tildes), como el de relaciones, y la
 * primera opción es «Sin asignar»: una celda vacía es un valor legítimo —es lo
 * que hace que `is_empty` del filtro sirva—. Elegir guarda por el mismo PATCH
 * que cualquier otra celda.
 */
async function editarCeldaDePersona(td, database, row, prop, hooks = null) {
  if (selectorDePersona) return;
  cerrarSelectorDeRelacion();
  let gente = [];
  let error = "";
  try {
    gente = await cargarPersonas({ forzar: true });
  } catch (e) {
    // Con una lista ya cargada, se sigue con ella: elegir a alguien de un
    // directorio de hace un rato es mejor que no poder elegir.
    gente = [...personasCache.values()];
    if (!gente.length) error = `No se pudo leer la lista de personas: ${describeError(e)}`;
  }
  // La tabla pudo repintarse mientras llegaba la respuesta.
  if (!td.isConnected) return;

  const panel = document.createElement("div");
  panel.className = "rel-picker person-picker";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", `Elegir la persona de «${prop.name}»`);

  const cabecera = document.createElement("div");
  cabecera.className = "rel-picker-head";
  const nombre = document.createElement("span");
  nombre.className = "rel-picker-name";
  nombre.textContent = prop.name;
  const modo = document.createElement("span");
  modo.className = "rel-picker-mode";
  modo.textContent = "una persona";
  cabecera.append(nombre, modo);

  const buscar = document.createElement("input");
  buscar.type = "search";
  buscar.className = "rel-picker-search";
  buscar.placeholder = "Buscar por nombre o @usuario…";
  buscar.autocomplete = "off";
  buscar.setAttribute("aria-label", "Buscar una persona");
  buscar.disabled = Boolean(error) || !gente.length;

  const lista = document.createElement("div");
  lista.className = "rel-picker-list";
  lista.setAttribute("role", "listbox");

  const nota = document.createElement("p");
  nota.className = "rel-picker-note";
  nota.setAttribute("role", "status");

  panel.append(cabecera);
  if (!error) panel.append(buscar);
  panel.append(lista, nota);

  const estado = { activo: -1, opciones: [] };
  const actual = () => personaIdDeFila(row, prop.key);

  const guardar = async (id) => {
    const valor = id || null;
    if (sameValue(valor, row.properties?.[prop.key] ?? null)) {
      cerrarSelectorDePersona({ foco: true });
      return;
    }
    try {
      await saveRow(database, row, { properties: { [prop.key]: valor } }, hooks);
      nota.textContent = "Guardado.";
      repaintCell(td, database, row, prop, false, hooks);
      cerrarSelectorDePersona({ foco: true });
    } catch (e) {
      // Se queda abierto con el motivo dentro: cerrarlo dejaría al usuario con
      // un aviso y sin el sitio donde arreglarlo.
      nota.textContent = `No se pudo guardar: ${describeError(e)}`;
      reportError("No se pudo guardar la persona", e);
      repaintCell(td, database, row, prop, false, hooks);
    }
  };

  function pintarOpciones() {
    lista.textContent = "";
    estado.opciones = [];
    estado.activo = -1;
    if (error) {
      const p = document.createElement("p");
      p.className = "rel-picker-note";
      p.textContent = error;
      lista.appendChild(p);
      return;
    }
    const consulta = plegarTexto(buscar.value.trim());
    const candidatas = [{ id: "", display_name: "Sin asignar", username: "" }, ...gente].filter(
      (p) =>
        !consulta ||
        plegarTexto(p.display_name || "").includes(consulta) ||
        plegarTexto(p.username || "").includes(consulta),
    );
    const visibles = candidatas.slice(0, PERSONAS_CHOICES_LIMIT);
    if (!visibles.length) {
      const p = document.createElement("p");
      p.className = "rel-picker-note";
      p.textContent = "Nadie coincide con lo escrito.";
      lista.appendChild(p);
      return;
    }
    const elegida = actual();
    visibles.forEach((persona) => {
      const esVacio = !persona.id;
      const marcada = esVacio ? !elegida : String(persona.id) === elegida;
      const opcion = document.createElement("button");
      opcion.type = "button";
      opcion.className = "rel-picker-option" + (marcada ? " is-chosen" : "");
      opcion.setAttribute("role", "option");
      opcion.setAttribute("aria-selected", marcada ? "true" : "false");
      const titulo = document.createElement("span");
      titulo.className = "rel-picker-option-title";
      if (!esVacio) {
        let punto = avatarImg(persona.avatar);
        if (!punto) {
          punto = document.createElement("span");
          punto.className = "person-dot";
          if (persona.color) punto.style.background = persona.color;
          punto.setAttribute("aria-hidden", "true");
        }
        titulo.appendChild(punto);
      }
      const texto = document.createElement("span");
      texto.textContent = esVacio ? "Sin asignar" : persona.display_name || "(sin nombre)";
      titulo.appendChild(texto);
      const suyo = document.createElement("span");
      suyo.className = "rel-picker-option-base";
      suyo.textContent = esVacio ? "la celda queda vacía" : `@${persona.username || "usuario"}`;
      opcion.append(titulo, suyo);
      if (marcada) {
        const marca = document.createElement("span");
        marca.className = "rel-picker-check";
        marca.setAttribute("aria-hidden", "true");
        marca.textContent = "✓";
        opcion.appendChild(marca);
      }
      opcion.addEventListener("click", (e) => {
        e.stopPropagation();
        guardar(persona.id || null);
      });
      estado.opciones.push(opcion);
      lista.appendChild(opcion);
    });
    if (candidatas.length > visibles.length) {
      const mas = document.createElement("p");
      mas.className = "rel-picker-more";
      mas.textContent = `… y ${candidatas.length - visibles.length} más, sigue escribiendo`;
      lista.appendChild(mas);
    }
  }

  buscar.addEventListener("input", () => pintarOpciones());
  buscar.addEventListener("keydown", (e) => {
    // Dentro de una nota, lo que no se consuma acaba en el keymap de
    // CodeMirror (su Enter insertaría un salto en el `.md`).
    e.stopPropagation();
    if (e.key === "Enter") {
      e.preventDefault();
      estado.opciones[Math.max(estado.activo, 0)]?.click();
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    if (!estado.opciones.length) return;
    e.preventDefault();
    estado.activo =
      e.key === "ArrowDown"
        ? (estado.activo + 1) % estado.opciones.length
        : (estado.activo - 1 + estado.opciones.length) % estado.opciones.length;
    estado.opciones.forEach((op, i) => op.classList.toggle("is-active", i === estado.activo));
    estado.opciones[estado.activo].scrollIntoView({ block: "nearest" });
  });

  document.body.appendChild(panel);
  colocarPanelFlotante(panel, td);
  const fuera = (e) => {
    if (panel.contains(e.target) || td.contains(e.target)) return;
    cerrarSelectorDePersona();
  };
  // Escape en el documento: cuando la lista de personas no se pudo leer no hay
  // buscador que lo reciba, y un panel que solo se cierra con el ratón deja
  // tirado a quien va con el teclado.
  const teclas = (e) => {
    if (e.key !== "Escape") return;
    e.preventDefault();
    cerrarSelectorDePersona({ foco: true });
  };
  const recolocar = () => {
    if (!td.isConnected) {
      cerrarSelectorDePersona();
      return;
    }
    colocarPanelFlotante(panel, td);
  };
  document.addEventListener("mousedown", fuera, true);
  document.addEventListener("keydown", teclas, true);
  window.addEventListener("scroll", recolocar, true);
  window.addEventListener("resize", recolocar);
  const observador =
    typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => recolocar());
  observador?.observe(panel);
  selectorDePersona = {
    panel,
    ancla: td,
    limpiar: () => {
      observador?.disconnect();
      document.removeEventListener("mousedown", fuera, true);
      document.removeEventListener("keydown", teclas, true);
      window.removeEventListener("scroll", recolocar, true);
      window.removeEventListener("resize", recolocar);
    },
  };
  pintarOpciones();
  if (error) {
    panel.tabIndex = -1;
    panel.focus({ preventScroll: true });
  } else {
    buscar.focus({ preventScroll: true });
  }
}

function formatPropValue(value, prop) {
  if (value === undefined || value === null || value === "") return "—";
  if (prop.type === "checkbox") return value ? "✓" : "—";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

/** Un valor de selección, como etiqueta. El tono sale de la POSICIÓN del valor
 * en las opciones de la propiedad (`prop.options`), así que el mismo valor
 * lleva el mismo color en la tabla, en la lista y en el tablero, y no hay nada
 * que guardar: es presentación. Un valor que no está entre las opciones (texto
 * libre heredado) va en el tono neutro. Los tonos son los `--tag-N` del CSS. */
const TONOS_DE_ETIQUETA = 7;
function htmlDeEtiqueta(valor, prop) {
  const posicion = (prop.options || []).indexOf(valor);
  const tono = posicion < 0 ? 1 : (posicion % TONOS_DE_ETIQUETA) + 1;
  return `<span class="prop-tag prop-tag-${tono}">${escapeHtml(String(valor))}</span>`;
}

/** Igual que formatPropValue, pero listo para innerHTML: el "sí" de un
 * checkbox es el icono de check del sprite (con texto para lectores de
 * pantalla), una relación enseña el TÍTULO de sus filas (no el id: `row` es
 * quien trae los títulos resueltos) y todo lo demás va escapado. */
function formatPropValueHtml(value, prop, row = null) {
  if (prop.type === "checkbox") {
    return value ? `${svgIcon("i-check")}<span class="sr-only">Sí</span>` : "—";
  }
  if (prop.type === "select" || prop.type === "multiselect") {
    const valores = (Array.isArray(value) ? value : [value]).filter(
      (v) => v !== undefined && v !== null && v !== "",
    );
    if (valores.length) return valores.map((v) => htmlDeEtiqueta(v, prop)).join("");
  }
  if (prop.type === "relation") return htmlDeRelacion(value, prop, row);
  if (prop.type === "person") return htmlDePersona(value, prop, row);
  return escapeHtml(formatPropValue(value, prop));
}

/** El valor que sale de un control, en la forma que espera la API.
 * `null` = borrar esa propiedad del frontmatter (no se escribe `"null"` como
 * texto): vaciar una celda la quita del `.md`, igual que en las notas. */
function readPropertyValue(prop, input) {
  if (prop.type === "checkbox") return input.checked;
  const raw = String(input.value ?? "").trim();
  if (raw === "") return null;
  if (prop.type === "multiselect") {
    return raw.split(",").map((s) => s.trim()).filter(Boolean);
  }
  if (prop.type === "number") {
    const n = Number(raw);
    // Un valor que no es número se manda tal cual: el backend lo rechaza con
    // un 400 y la fila se queda como estaba. Devolver NaN lo convertiría en
    // `null` al serializar el JSON, y eso borraría la clave sin avisar.
    return Number.isFinite(n) ? n : raw;
  }
  return raw; // text, date, url, relation, select
}

/** Input para una propiedad según su tipo, con su valor actual. Único sitio
 * que decide qué control usa cada tipo: lo comparten el alta de fila, la
 * edición de una celda y el panel de fila del tablero. Devuelve también
 * `read()`, que traduce el control a valor (`null` = sin valor). */
function buildPropertyInput(prop, value, ref = null) {
  let input;
  if (prop.type === "select") {
    input = document.createElement("select");
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "—";
    input.appendChild(blank);
    // Un valor guardado que ya no está en `options` se sigue viendo y se
    // conserva: si no se añadiera, abrir la celda lo convertiría en vacío.
    const options = [...(prop.options || [])];
    if (value !== undefined && value !== null && value !== "" && !options.includes(value)) {
      options.push(value);
    }
    options.forEach((o) => {
      const opt = document.createElement("option");
      opt.value = o;
      opt.textContent = o;
      input.appendChild(opt);
    });
    input.value = value ?? "";
  } else if (prop.type === "checkbox") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
  } else if (prop.type === "number") {
    input = document.createElement("input");
    input.type = "number";
    input.step = "any";
    input.value = value ?? "";
  } else if (prop.type === "date") {
    input = document.createElement("input");
    input.type = "date";
    input.value = value ?? "";
  } else if (prop.type === "person") {
    // Una persona se elige de la lista, no se escribe: el valor es el id de un
    // usuario y el `.md` no admite texto libre. El valor guardado se conserva
    // como opción aunque el directorio no haya llegado todavía.
    input = document.createElement("select");
    const vacio = document.createElement("option");
    vacio.value = "";
    vacio.textContent = "— sin asignar";
    input.appendChild(vacio);
    const sembrado = value === undefined || value === null ? "" : String(value);
    if (sembrado) {
      const suya = document.createElement("option");
      suya.value = sembrado;
      // El nombre que trae la fila (`people`, resuelto por el servidor) manda
      // sobre el directorio: es el que se acaba de leer.
      suya.textContent = ref?.display_name || nombreDePersona(sembrado) || sembrado;
      input.appendChild(suya);
    }
    input.value = sembrado;
    const pintarGente = (gente) => {
      const elegido = input.value;
      [...input.querySelectorAll("option")].slice(1).forEach((opcion) => opcion.remove());
      (gente || []).forEach((persona) => {
        const opcion = document.createElement("option");
        opcion.value = String(persona.id);
        opcion.textContent = persona.display_name || persona.username || String(persona.id);
        input.appendChild(opcion);
      });
      if (elegido && ![...input.options].some((opcion) => opcion.value === elegido)) {
        const suya = document.createElement("option");
        suya.value = elegido;
        suya.textContent = nombreDePersona(elegido) || elegido;
        input.appendChild(suya);
      }
      input.value = elegido;
    };
    if (personasCache.size) {
      pintarGente([...personasCache.values()]);
    } else {
      cargarPersonas().then(pintarGente).catch(() => {});
    }
  } else {
    input = document.createElement("input");
    input.type = "text";
    if (prop.type === "multiselect") {
      input.placeholder = "valores separados por coma";
      input.value = Array.isArray(value) ? value.join(", ") : (value ?? "");
    } else {
      input.value = value ?? "";
    }
  }

  // Un control tipado puede no poder con el valor guardado (una propiedad
  // `number` con "A-12" dentro, una `date` con hora): el navegador lo deja
  // vacío y guardar borraría el dato. En ese caso se edita como texto, igual
  // que el `select` conserva un valor que ya no está en `options`.
  if (
    (prop.type === "number" || prop.type === "date") &&
    value !== undefined &&
    value !== null &&
    value !== "" &&
    input.value !== String(value)
  ) {
    input = document.createElement("input");
    input.type = "text";
    input.value = String(value);
  }

  return { input, read: () => readPropertyValue(prop, input) };
}

function sameValue(a, b) {
  if (a === b) return true;
  if (a === null || a === undefined) return b === null || b === undefined;
  if (b === null || b === undefined) return false;
  if (Array.isArray(a) || Array.isArray(b)) return JSON.stringify(a) === JSON.stringify(b);
  return false;
}

/** Único camino de guardado de una fila: un PATCH y el estado local al día
 * con lo que el servidor acaba de escribir en el `.md`. Si lanza, quien llama
 * avisa con reportError() y repinta el valor viejo: nada se da por guardado
 * sin haberse guardado.
 *
 * `hooks` es opcional y solo lo usan las celdas de la tabla incrustada en una
 * nota (ver `renderTable`): `onBeforeSave()` justo antes del PATCH y
 * `onSaved(guardada)` solo si el servidor lo aceptó. */
async function saveRow(database, row, changes, hooks = null) {
  hooks?.onBeforeSave?.();
  const saved = await api(`/databases/${database.id}/rows/${row.id}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
  Object.assign(row, saved);
  hooks?.onSaved?.(saved);
  return saved;
}

// ------------------------------------------- selección múltiple de filas
//
// Lo marcado vive aquí y no en el DOM: la vista se repinta entera al cambiar de
// pestaña, al guardar una celda y cuando otra ventana toca la base, así que una
// selección que se pierde a cada repintado no sirve para borrar veinte filas.
// La clave es el id de la base; el valor, un Map id de fila -> título, que es
// lo que permite decir CUÁL falló sin volver a preguntarle al servidor.

const seleccionPorBase = new Map();

/** El informe de la última acción en bloque, por base. Tiene que sobrevivir al
 * repintado que sigue a la acción, que es justo cuando hay que leerlo. */
const informeDeBloque = new Map();

/** Aviso de que la selección de una base ha cambiado.
 *
 * La selección es UNA por base, pero puede haber varias barras vivas a la vez
 * (una nota con dos vallas ```mdm-database de la misma base, patrón normal: ver
 * `refrescarOtrosEmbeds`). Si solo repintara la barra que recibió el clic, las
 * demás enseñarían un recuento viejo —o ninguna barra— mientras la acción
 * cambiaría más filas de las que dicen. Cada barra escucha este aviso y repinta
 * lo suyo, que es lo único que sabe (su lista de visibles).
 */
const EVENTO_SELECCION_BARRAS = "mdm:seleccion-barras";

function avisarCambioDeSeleccion(databaseId) {
  document.dispatchEvent(
    new CustomEvent(EVENTO_SELECCION_BARRAS, { detail: { clave: String(databaseId) } }),
  );
}

function seleccionDeBase(databaseId) {
  const clave = String(databaseId);
  let seleccion = seleccionPorBase.get(clave);
  if (!seleccion) {
    seleccion = new Map();
    seleccionPorBase.set(clave, seleccion);
  }
  return seleccion;
}

/** «1 fila» / «3 filas». */
function recuentoDeFilas(n) {
  return `${n} ${n === 1 ? "fila" : "filas"}`;
}

/** «a», «b» y «c». Con tope: veinte títulos seguidos no se leen, y lo que no se
 * lee es como si no se dijera. */
function enumerarTitulos(titulos, max = 6) {
  const citados = titulos.slice(0, max).map((t) => `«${t}»`);
  if (titulos.length <= max) return citados.join(", ");
  return `${citados.join(", ")} y ${titulos.length - max} más`;
}

/** Un valor de propiedad en palabras, para el informe («ganado», «sí», «—»). */
function textoDeValor(valor) {
  if (valor === null || valor === undefined || valor === "") return "vacío";
  if (typeof valor === "boolean") return valor ? "sí" : "no";
  if (Array.isArray(valor)) return valor.length ? valor.join(", ") : "vacío";
  return String(valor);
}

/** Un control (botón, casilla) que puede vivir DENTRO de una nota: las teclas
 * que el control ya sabe usar no pueden subir al keymap de CodeMirror, que
 * convertiría un Enter en un salto de línea en el `.md` de la nota. Fuera de
 * una nota no estorba a nadie. */
function aislarTeclasDeControl(el) {
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " " || e.key === "Escape") e.stopPropagation();
  });
}

/** El informe de una acción en bloque: cuántas se hicieron, cuáles y —si
 * fallaron— cuáles no y por qué. Una acción a medias sin decir qué se quedó
 * fuera es peor que no hacerla, así que el informe se queda a la vista hasta
 * que se descarta. */
function renderInformeDeBloque(informe, alDescartar) {
  const total = informe.ok.length + informe.fallos.length;
  const caja = document.createElement("div");
  caja.className = "db-sel-report";
  caja.setAttribute("role", "status");

  const cabeza = document.createElement("div");
  cabeza.className = "db-sel-report-head";
  const titulo = document.createElement("p");
  titulo.className = "db-sel-report-title";
  titulo.textContent = `${informe.titulo}: ${informe.ok.length} de ${recuentoDeFilas(total)}.`;
  const cerrar = document.createElement("button");
  cerrar.type = "button";
  cerrar.className = "db-sel-report-close";
  cerrar.innerHTML = svgIcon("i-x");
  cerrar.title = "Descartar el informe";
  cerrar.setAttribute("aria-label", "Descartar el informe");
  aislarTeclasDeControl(cerrar);
  cerrar.addEventListener("click", alDescartar);
  cabeza.append(titulo, cerrar);
  caja.appendChild(cabeza);

  if (informe.ok.length) {
    const hechas = document.createElement("p");
    hechas.className = "db-sel-report-ok";
    hechas.textContent = `${informe.hecho}: ${enumerarTitulos(informe.ok.map((f) => f.title))}`;
    caja.appendChild(hechas);
  }
  if (informe.fallos.length) {
    // Una a una y con su motivo: «se borraron 7 de 10» a secas no dice qué hay
    // que volver a mirar, y aquí lo que hace falta es eso.
    const fallidas = document.createElement("p");
    fallidas.className = "db-sel-report-bad";
    fallidas.textContent =
      `${informe.noHecho}: ` + informe.fallos.map((f) => `«${f.title}» (${f.motivo})`).join("; ");
    caja.appendChild(fallidas);
  }
  return caja;
}

/** Las acciones en bloque van fila a fila, en serie.
 *
 * Cada PATCH/DELETE escribe SU `.md` y el servidor publica un aviso por cada
 * uno: en paralelo los fallos se mezclarían y el informe ya no sabría decir cuál
 * sí y cuál no. El precio es el tiempo, y el avance se cuenta en la barra. */
async function ejecutarEnBloque({ filas, accion, informe, alAvanzar }) {
  for (let i = 0; i < filas.length; i += 1) {
    try {
      await accion(filas[i]);
      informe.ok.push(filas[i]);
    } catch (e) {
      informe.fallos.push({ title: filas[i].title || "Sin título", motivo: describeError(e) });
    }
    alAvanzar?.(i + 1, filas.length);
  }
  return informe;
}

/** Pregunta la propiedad y el valor nuevo, y se lo pasa a `alAplicar`.
 *
 * El control del valor es el MISMO del clic en una celda (`buildPropertyInput`),
 * así que vale para cualquier tipo: texto, número, fecha, casilla, selección,
 * multiselección y url. Un valor vacío deja la propiedad sin valor.
 *
 * El diálogo se crea aquí y no en el HTML porque solo existe mientras se usa;
 * cuelga de `<body>` para poder abrirse también desde la tabla de una nota. */
function abrirDialogoDePropiedadEnBloque({ database, filas, alAplicar }) {
  const props = database.schema_def || [];
  if (!props.length) return;
  const cuantas = recuentoDeFilas(filas.length);

  const dialogo = document.createElement("dialog");
  dialogo.className = "mdm-bulk-dialog";

  const forma = document.createElement("form");
  forma.noValidate = true;

  const titulo = document.createElement("h2");
  titulo.className = "mdm-bulk-title";
  titulo.textContent = `Cambiar una propiedad en ${cuantas}`;

  const campoProp = document.createElement("div");
  campoProp.className = "field";
  const etiquetaProp = document.createElement("label");
  etiquetaProp.textContent = "Propiedad";
  etiquetaProp.htmlFor = "mdm-bulk-prop";
  const select = document.createElement("select");
  select.id = "mdm-bulk-prop";
  props.forEach((p) => {
    const opcion = document.createElement("option");
    opcion.value = p.key;
    opcion.textContent = `${p.name} (${tipoVisible(p.type)})`;
    select.appendChild(opcion);
  });
  campoProp.append(etiquetaProp, select);

  const campoValor = document.createElement("div");
  campoValor.className = "field";
  const etiquetaValor = document.createElement("label");
  etiquetaValor.textContent = "Valor nuevo";
  etiquetaValor.htmlFor = "mdm-bulk-valor";

  // El control se rehace cada vez que cambia la propiedad: un texto y una
  // casilla no se piden igual. Se siembra vacío porque el valor es el NUEVO,
  // no el de ninguna fila en concreto.
  let lectura = null;
  const pintarValor = () => {
    const prop = props.find((p) => p.key === select.value) || props[0];
    const { input, read } = buildPropertyInput(prop, undefined);
    input.id = "mdm-bulk-valor";
    campoValor.textContent = "";
    campoValor.append(etiquetaValor, input);
    lectura = () => ({ prop, valor: read() });
  };
  select.addEventListener("change", pintarValor);
  pintarValor();

  const nota = document.createElement("p");
  nota.className = "mdm-bulk-note";
  nota.textContent =
    `Se escribe en el .md de ${cuantas}, una a una. ` +
    "Las que no se puedan escribir se dirán una por una, con el motivo.";

  const acciones = document.createElement("div");
  acciones.className = "dialog-actions";
  // El `close` del <dialog> no se dispara de forma fiable en todas las
  // versiones, así que el nodo se retira a mano en los tres caminos (Aplicar,
  // Cancelar y Escape); si no, cada apertura dejaba un diálogo colgando.
  const retirar = () => {
    if (dialogo.open) dialogo.close();
    dialogo.remove();
  };

  const cancelar = document.createElement("button");
  cancelar.type = "button";
  cancelar.className = "btn";
  cancelar.textContent = "Cancelar";
  cancelar.addEventListener("click", retirar);
  const aplicar = document.createElement("button");
  aplicar.type = "submit";
  aplicar.className = "btn btn-primary";
  aplicar.textContent = `Aplicar a ${cuantas}`;
  acciones.append(cancelar, aplicar);
  forma.addEventListener("submit", (e) => {
    e.preventDefault();
    const { prop, valor } = lectura ? lectura() : {};
    if (!prop) return;
    retirar();
    alAplicar(prop, valor);
  });

  forma.append(campoProp, campoValor, nota, acciones);
  dialogo.append(titulo, forma);
  document.body.appendChild(dialogo);
  dialogo.addEventListener("cancel", retirar);
  dialogo.showModal();
}

/** La selección múltiple de una vista de filas: la barra de acciones, la casilla
 * de cada fila y el estado que las une.
 *
 * La usan la tabla (también la incrustada en una nota) y la lista, para que las
 * dos se comporten igual. Devuelve los nodos que van encima de las filas
 * (`barra`, `informes`), los controles (`casillaDeFila`, `casillaDeCabecera`),
 * `registrarFila` (qué nodo se marca) y `teclas` (Ctrl+A, Espacio, Supr, Esc).
 *
 * `puedeBorrar: false` es la tabla de una nota, donde borrar filas se hace en la
 * base: la barra lo dice en vez de ofrecer un botón que no va a ninguna parte.
 */
function crearSeleccionDeFilas({
  database,
  rows,
  puedeBorrar = true,
  hooks = null,
  onRepintar = null,
  // Dónde va la barra: la línea de las vistas de la base, la cabecera del
  // bloque en una nota. Con selección, la barra ocupa esa línea en su lugar
  // (ver `.db-sel-host` en el CSS); sin anfitrión, va encima de la tabla.
  anfitrion = null,
  // El envoltorio de la tabla o de la lista: con selección lleva
  // `con-seleccion`, que es lo que deja a la vista todas las casillas.
  marco = null,
}) {
  const claveBase = String(database.id);
  const sel = seleccionDeBase(claveBase);
  const visibles = new Map(rows.map((r) => [String(r.id), r]));
  const cajas = new Map(); // id de fila -> { contenedor, casilla }
  let ocupado = false;

  // Una fila renombrada lleva el título nuevo a la selección: el informe tiene
  // que nombrarla como se llama ahora, no como se llamaba al marcarla.
  for (const id of [...sel.keys()]) {
    const fila = visibles.get(id);
    if (fila) sel.set(id, fila.title || "Sin título");
  }

  const barra = document.createElement("div");
  barra.className = "db-sel-bar";
  barra.setAttribute("role", "region");
  barra.setAttribute("aria-label", "Acciones de las filas seleccionadas");
  barra.hidden = true;
  // En una nota, un clic en el hueco de la barra no abre la valla en crudo.
  if (hooks?.embed) barra.dataset.dbAction = "seleccion";

  const cuenta = document.createElement("span");
  cuenta.className = "db-sel-count";
  cuenta.setAttribute("aria-live", "polite");

  // Lo que el filtro ha dejado fuera se cuenta aparte y se dice: si no, el
  // recuento de la barra no cuadra con las filas marcadas que se ven.
  const fuera = document.createElement("span");
  fuera.className = "db-sel-fuera";

  const avance = document.createElement("span");
  avance.className = "db-sel-progress";
  avance.hidden = true;

  const acciones = document.createElement("div");
  acciones.className = "db-sel-actions";

  const informes = document.createElement("div");
  informes.className = "db-sel-reports";

  /** Otra instancia viva de la MISMA base ha cambiado la selección: esta barra
   * repinta su recuento (y aparece si toca) con lo que hay ahora en el Map
   * compartido. Una barra que ya no está en el documento —la nota se repintó, se
   * cambió de vista— se da de baja aquí, que es cuando se nota, para no seguir
   * escuchando para siempre. */
  function alCambiarSeleccion(e) {
    if (!e.detail || e.detail.clave !== claveBase) return;
    if (!barra.isConnected) {
      document.removeEventListener(EVENTO_SELECCION_BARRAS, alCambiarSeleccion);
      return;
    }
    sincronizar();
  }

  const seleccionadas = () => [...sel.entries()].map(([id, title]) => ({ id, title }));
  const hayFueraDeVista = () => [...sel.keys()].filter((id) => !visibles.has(id)).length;

  const cabecera = document.createElement("input");
  cabecera.type = "checkbox";
  cabecera.className = "row-check";
  cabecera.addEventListener("change", () => {
    if (cabecera.checked) seleccionarTodas();
    else limpiar();
  });
  aislarTeclasDeControl(cabecera);

  function sincronizar() {
    const total = sel.size;
    const fueraDeVista = hayFueraDeVista();
    barra.hidden = total === 0;
    marco?.classList.toggle("con-seleccion", total > 0);
    if (anfitrion && barra.parentElement === anfitrion) {
      anfitrion.classList.toggle("con-seleccion", total > 0);
    }
    cuenta.textContent = `${total} ${total === 1 ? "seleccionada" : "seleccionadas"}`;
    fuera.hidden = fueraDeVista === 0;
    // Corto, que la barra es de una línea; la frase entera va en el `title`.
    fuera.textContent = fueraDeVista === 0 ? "" : `${fueraDeVista} fuera de la vista`;
    fuera.title =
      fueraDeVista === 0
        ? ""
        : `${fueraDeVista} de ellas ${fueraDeVista === 1 ? "no está" : "no están"} en esta vista: ${
            fueraDeVista === 1 ? "entra" : "entran"
          } igual en la acción`;
    for (const [id, caja] of cajas) {
      const marcada = sel.has(id);
      caja.casilla.checked = marcada;
      caja.contenedor.classList.toggle("is-selected", marcada);
    }
    // La cabecera solo habla de lo VISIBLE: entera si están todas, a medias si
    // hay algunas, y nunca marcada si la vista no enseña ninguna fila.
    const visiblesMarcadas = [...visibles.keys()].filter((id) => sel.has(id)).length;
    cabecera.disabled = visibles.size === 0;
    cabecera.checked = visibles.size > 0 && visiblesMarcadas === visibles.size;
    cabecera.indeterminate = visiblesMarcadas > 0 && visiblesMarcadas < visibles.size;
    cabecera.title =
      visibles.size === 0
        ? "No hay filas visibles que seleccionar"
        : `Seleccionar las ${visibles.size} filas visibles`;
    cabecera.setAttribute("aria-label", cabecera.title);
    if (!ocupado) avance.hidden = true;
  }

  function alternar(id) {
    const clave = String(id);
    if (sel.has(clave)) {
      sel.delete(clave);
    } else {
      const fila = visibles.get(clave);
      sel.set(clave, fila ? fila.title || "Sin título" : "Sin título");
    }
    avisarCambioDeSeleccion(claveBase);
  }

  function seleccionarTodas() {
    for (const [id, fila] of visibles) sel.set(id, fila.title || "Sin título");
    avisarCambioDeSeleccion(claveBase);
  }

  function limpiar() {
    sel.clear();
    avisarCambioDeSeleccion(claveBase);
  }

  /** El botón (o casilla) que marca una fila. `checked` se siembra ya, para que
   * un repintado con selección encima nazca con las casillas correctas. */
  function casillaDeFila(row) {
    const marcada = sel.has(String(row.id));
    const input = document.createElement("input");
    input.type = "checkbox";
    input.className = "row-check";
    input.checked = marcada;
    input.tabIndex = -1; // la tabla tiene UNA parada de Tab, y es la celda
    const nombre = `Seleccionar «${row.title || "Sin título"}»`;
    input.title = nombre;
    input.setAttribute("aria-label", nombre);
    input.addEventListener("change", () => alternar(row.id));
    aislarTeclasDeControl(input);
    return input;
  }

  /** `contenedor` es lo que se marca visualmente (el `<tr>`, la línea de la
   * lista) y `casilla` el control que la representa. */
  function registrarFila(row, contenedor, casilla) {
    cajas.set(String(row.id), { contenedor, casilla });
  }

  /** La vista ha vuelto a pintar sus filas sin rehacer la barra (el alta en
   * línea de la lista): las visibles son estas y no las de antes. */
  function fijarVisibles(filas) {
    visibles.clear();
    for (const fila of filas) visibles.set(String(fila.id), fila);
    sincronizar();
  }

  function pintarInforme() {
    informes.textContent = "";
    const informe = informeDeBloque.get(claveBase);
    if (!informe) return;
    informes.appendChild(
      renderInformeDeBloque(informe, () => {
        informeDeBloque.delete(claveBase);
        pintarInforme();
      }),
    );
  }

  function marcarOcupado(valor) {
    ocupado = valor;
    // «Cambiar propiedad» sigue apagado si la base no tiene propiedades: al
    // terminar una acción no se le devuelve un botón que no puede hacer nada.
    const sinPropiedades = !(database.schema_def || []).length;
    for (const boton of acciones.querySelectorAll("button")) {
      boton.disabled = valor || (sinPropiedades && boton === btnPropiedad);
    }
    avance.hidden = !valor;
    if (!valor) avance.textContent = "";
    sincronizar();
  }

  async function borrarSeleccion() {
    if (ocupado) return;
    const filas = seleccionadas();
    if (!filas.length) return;
    const fueraDeVista = hayFueraDeVista();
    const avisoFuera = fueraDeVista
      ? `\n${fueraDeVista} de ellas no ${fueraDeVista === 1 ? "está" : "están"} en esta vista, pero ${
          fueraDeVista === 1 ? "entra" : "entran"
        } igual en el borrado.`
      : "";
    // Lo que se dice y lo que pasa son lo mismo: van a la PAPELERA del vault
    // (`.trash/<fecha y hora UTC>/`, un directorio por borrado), no se borran
    // del disco y se pueden recuperar.
    if (
      !confirm(
        `¿Borrar ${recuentoDeFilas(filas.length)}?${avisoFuera}\n\n` +
          "Cada una va a su carpeta de la papelera del vault (.trash/<fecha y hora UTC>/):" +
          " no se borran del disco.",
      )
    ) {
      return;
    }

    const informe = {
      titulo: `Borrar ${recuentoDeFilas(filas.length)} (a la papelera)`,
      hecho: "Borradas",
      noHecho: "No se borraron",
      ok: [],
      fallos: [],
    };
    marcarOcupado(true);
    await ejecutarEnBloque({
      filas,
      informe,
      alAvanzar: (i, n) => {
        avance.textContent = `Borrando ${i} de ${n}…`;
      },
      accion: async (fila) => {
        // El eco del SSE no debe repintar la nota a media tanda.
        hooks?.onBeforeSave?.();
        await api(`/databases/${database.id}/rows/${fila.id}`, { method: "DELETE" });
      },
    });
    // Lo borrado sale de la selección; lo que falló se queda marcado, que es
    // justo lo que hay que volver a mirar.
    for (const fila of informe.ok) sel.delete(String(fila.id));
    if (informe.ok.length) avisarCambioDeSeleccion(claveBase);
    informeDeBloque.set(claveBase, informe);
    marcarOcupado(false);
    pintarInforme();
    await onRepintar?.();
  }

  async function aplicarPropiedad(filas, prop, valor) {
    const informe = {
      titulo: `Cambiar «${prop.name}» a «${textoDeValor(valor)}»`,
      hecho: "Cambiadas",
      noHecho: "No se cambiaron",
      ok: [],
      fallos: [],
    };
    marcarOcupado(true);
    await ejecutarEnBloque({
      filas,
      informe,
      alAvanzar: (i, n) => {
        avance.textContent = `Guardando ${i} de ${n}…`;
      },
      accion: async (fila) => {
        hooks?.onBeforeSave?.();
        await api(`/databases/${database.id}/rows/${fila.id}`, {
          method: "PATCH",
          body: JSON.stringify({ properties: { [prop.key]: valor } }),
        });
      },
    });
    // La selección NO se toca: cambiar una propiedad no es terminar con las
    // filas (lo normal es cambiar y luego borrar, o al revés).
    informeDeBloque.set(claveBase, informe);
    marcarOcupado(false);
    pintarInforme();
    await onRepintar?.();
  }

  /** Ctrl/⌘+A selecciona las visibles, Espacio marca la fila con el foco, Supr
   * borra la selección y Esc la quita. Se llama desde la captura de las filas,
   * así que llega antes que el `keydown` de la celda (que se queda con Enter)
   * y antes que el keymap de CodeMirror. */
  function teclas(e) {
    if (e.defaultPrevented || !(e.target instanceof Element)) return;
    const activo = e.target;
    // Un control de EDICIÓN manda: Ctrl+A selecciona su texto, Supr borra una
    // letra y Espacio es un espacio. Una casilla no escribe nada, así que con
    // ella enfocada Supr y Ctrl+A siguen siendo de la selección (Espacio, en
    // cambio, es su tecla: la marca la propia casilla).
    //
    // Lo que se mira es el control, NO un `[contenteditable]` de por medio: la
    // tabla de una nota cuelga del `contenteditable` del editor, y con esa
    // comprobación el teclado de la selección no funcionaba ahí dentro.
    const campo = activo.closest("input, select, textarea");
    const esCasilla = Boolean(
      campo && campo.tagName === "INPUT" && (campo.type === "checkbox" || campo.type === "radio"),
    );
    if (campo && !esCasilla) return;

    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.key === "a" || e.key === "A")) {
      e.preventDefault();
      e.stopPropagation();
      seleccionarTodas();
      return;
    }
    if (e.key === "Escape") {
      if (!sel.size) return;
      e.preventDefault();
      e.stopPropagation();
      limpiar();
      return;
    }
    if (e.key === "Delete") {
      if (!sel.size) return;
      if (!puedeBorrar) {
        // En la tabla de una nota no se borran filas: la tecla se queda aquí y
        // no sube al keymap del editor, que suprimiría texto del `.md`.
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      e.preventDefault();
      e.stopPropagation();
      borrarSeleccion();
      return;
    }
    if (e.key === " ") {
      if (esCasilla) return; // el navegador ya marca/desmarca la casilla
      const fila = activo.closest("[data-row-id]");
      if (!fila) return;
      // En la tabla el foco está en una celda y Espacio marca; en la lista la
      // fila ES un botón y también marca (abrir es Enter). Los demás botones
      // de la fila (borrar) se siguen pulsando con Espacio como siempre.
      const esBotonDeFila = fila.classList.contains("list-row");
      if (!esBotonDeFila && activo.closest("button, a[href]")) return;
      e.preventDefault();
      e.stopPropagation();
      alternar(fila.dataset.rowId);
    }
  }

  function botonDeBarra(texto, accion, alPulsar, clase = "btn") {
    const boton = document.createElement("button");
    boton.type = "button";
    boton.className = clase;
    boton.textContent = texto;
    // Dentro de una nota, el widget del editor se lleva el `mousedown` de todo
    // lo que no lleve este marcador (ver DatabaseBlockWidget en entry.js).
    if (hooks?.embed) boton.dataset.dbAction = accion;
    aislarTeclasDeControl(boton);
    boton.addEventListener("click", alPulsar);
    return boton;
  }

  const btnPropiedad = botonDeBarra("Cambiar propiedad", "cambiar-propiedad", () => {
    if (ocupado) return;
    const filas = seleccionadas();
    if (!filas.length) return;
    abrirDialogoDePropiedadEnBloque({
      database,
      filas,
      alAplicar: (prop, valor) => aplicarPropiedad(filas, prop, valor),
    });
  });
  if (!(database.schema_def || []).length) {
    btnPropiedad.disabled = true;
    btnPropiedad.title = "La base no tiene propiedades: no hay nada que cambiar.";
  }

  const btnQuitar = botonDeBarra("Quitar selección", "quitar-seleccion", () => limpiar(), "btn db-sel-icon");
  btnQuitar.innerHTML = svgIcon("i-x");
  btnQuitar.title = "Quitar selección (Esc)";
  btnQuitar.setAttribute("aria-label", "Quitar selección");
  acciones.append(btnPropiedad);
  if (puedeBorrar) {
    const btnBorrar = botonDeBarra("Borrar", "borrar-seleccion", () => borrarSeleccion(), "btn btn-danger");
    btnBorrar.innerHTML = `${svgIcon("i-trash")}<span>Borrar</span>`;
    btnBorrar.title = "Borrar las filas seleccionadas (Supr)";
    acciones.append(btnBorrar);
  } else {
    // En la tabla de una nota no se borran filas: borrar se hace en la base.
    const nota = document.createElement("span");
    nota.className = "db-sel-note";
    nota.textContent = "Para borrar, abre la base";
    acciones.append(nota);
  }
  acciones.append(btnQuitar);

  barra.append(cuenta, fuera, avance, acciones);
  if (anfitrion) {
    // La de un pintado anterior (la tabla se repinta sin repintar la línea de
    // las vistas) se va: en cada anfitrión hay una sola barra.
    anfitrion.querySelectorAll(":scope > .db-sel-bar").forEach((vieja) => vieja.remove());
    anfitrion.classList.add("db-sel-host");
    anfitrion.appendChild(barra);
  }
  document.addEventListener(EVENTO_SELECCION_BARRAS, alCambiarSeleccion);
  sincronizar();
  pintarInforme();

  return {
    barra,
    informes,
    teclas,
    registrarFila,
    fijarVisibles,
    casillaDeFila,
    casillaDeCabecera: () => cabecera,
    sincronizar,
  };
}

// --------------------------------------- ancho y orden de las columnas
// Las dos cosas son de LA VISTA, no de la base: viven en su `config` --el
// frontmatter de `Bases/<slug>.md`-- junto al filtro y al orden de las filas.
// `widths` es un mapa clave -> píxeles y `order` la lista de claves en el orden
// en que se enseñan. «Todas (sin vista)» no tiene entrada donde guardarlos, así
// que sigue con el orden del schema y el reparto del navegador: prometer un
// ancho que se pierde al recargar sería peor que no ofrecerlo.

/** Lo que hay que devolver a su sitio cuando el repintado de una columna rehace
 * la base entera: el desplazamiento horizontal de la tabla y el foco.
 *
 * El ancho y el orden viven en la vista, así que guardar cualquiera de los dos
 * vuelve a pedir la base y a pintar la tabla de cero. Con anchos guardados la
 * tabla se sale del panel: sin devolver el `scrollLeft`, soltar el asa con la
 * tabla desplazada la mandaba a la primera columna. Y sin el foco, `Alt+flecha`
 * (mover una columna con el teclado) lo dejaba en `<body>`: dos reordenaciones
 * seguidas exigían volver a enfocar la cabecera.
 *
 * Se busca la tabla en el documento y no en una referencia guardada: el
 * repintado crea un `#main` nuevo. */
function estadoDeColumnas() {
  const wrap = document.querySelector("#main .table-wrap");
  if (!wrap) return null;
  const activo = document.activeElement;
  let foco = null;
  if (activo && wrap.contains(activo)) {
    const th = activo.closest?.("th[data-col-key]");
    if (th) foco = { clave: th.dataset.colKey, asa: Boolean(activo.closest(".col-resizer")) };
  }
  return { scrollLeft: wrap.scrollLeft, foco };
}

/** Devuelve el desplazamiento y el foco que apuntó `estadoDeColumnas`, sobre la
 * tabla recién pintada. Si la columna ya no está (o la vista cambió de tipo) no
 * se toca nada: el foco se queda donde lo haya dejado el navegador. */
function restaurarColumnas({ scrollLeft, foco }) {
  const wrap = document.querySelector("#main .table-wrap");
  if (!wrap) return;
  if (scrollLeft) wrap.scrollLeft = scrollLeft;
  if (!foco) return;
  const th = wrap.querySelector(`th[data-col-key="${CSS.escape(foco.clave)}"]`);
  if (!th) return;
  // El asa (`separator`) o el botón de ordenar, que es lo que tiene el foco
  // cuando se reordena con el teclado.
  const destino = foco.asa ? th.querySelector(".col-resizer") : th.querySelector(".th-sort");
  destino?.focus({ preventScroll: true });
}

const ANCHO_MINIMO = 64;   // por debajo no cabe ni el nombre de la columna
const ANCHO_MAXIMO = 560;  // más que esto y la columna se come la tabla

function acotarAncho(px) {
  return Math.min(ANCHO_MAXIMO, Math.max(ANCHO_MINIMO, Math.round(px)));
}

/** Las columnas que no son del schema: la de abrir la fila, la del título y la
 * de acciones. En el DOM llevan `@` delante para no chocar con una propiedad
 * con esa clave (`open`, `title`, `actions`), pero solo la del título tiene
 * ancho guardado, y bajo la clave `title`. */
const CLAVES_DE_SERVICIO = new Set(["@open", "@title", "@actions", "@add"]);

/** La clave con la que se guarda el ancho de una columna en `views[].widths`:
 * la de la propiedad, o `title` para la columna del título. Las de servicio
 * (abrir la fila y acciones) no tienen: su ancho es parte del dibujo de la
 * tabla, no un dato de la vista, y por eso no llevan asa. */
function claveDeAncho(domKey) {
  if (domKey === "@title") return "title";
  return CLAVES_DE_SERVICIO.has(domKey) ? null : domKey;
}

/** El ancho de una columna que la vista no ha guardado, como en Notion: cada
 * columna nace con un ancho fijo según su tipo (el título algo más ancho), y
 * la tabla mide la suma. Antes la tabla ocupaba todo el ancho de la página y
 * el navegador repartía el hueco, así que el «+» de añadir columna se iba al
 * borde derecho por lejos que quedara la última. */
const ANCHO_POR_TIPO = { checkbox: 110, number: 130, date: 170, url: 200, person: 170 };
function anchoPorDefecto(claveMd, propiedades) {
  if (claveMd === "title") return 260;
  const prop = propiedades.find((p) => p.key === claveMd);
  return ANCHO_POR_TIPO[prop?.type] || 200;
}

/** Las columnas de servicio (la casilla de la fila, abrir la fila, el «+») no
 * se guardan en la vista: su ancho es parte del dibujo y es el mismo que les
 * da el CSS (44 px en el móvil, donde se tocan con el dedo). */
function anchoDeServicio(domKey) {
  const movil = typeof window !== "undefined" && window.matchMedia?.("(max-width: 768px)").matches;
  if (domKey === "@actions") return movil ? 44 : 32;
  if (domKey === "@add") return 40;
  return movil ? 44 : 34; // @open
}

/** Los anchos guardados de una vista: clave de propiedad (o `title` para la
 * columna del título) -> píxeles, ya dentro de los límites.
 *
 * El `.md` guarda el ancho como entero y el índice lo devuelve así, pero un
 * fichero escrito a mano puede traerlo como texto: `Number` y el filtro de
 * `Number.isFinite` dejan fuera lo que no sea un número sin romper la tabla. */
function anchosDeVista(view) {
  const guardados = view?.config?.widths;
  const anchos = new Map();
  if (!guardados || typeof guardados !== "object") return anchos;
  for (const [clave, valor] of Object.entries(guardados)) {
    const px = Number(valor);
    if (Number.isFinite(px) && px > 0) anchos.set(String(clave), acotarAncho(px));
  }
  return anchos;
}

/** Las columnas de una vista, en su orden: `order` manda y
 * `visible_properties` quita. Lo que no esté en `order` va detrás, en el orden
 * del schema, así que añadir una propiedad no la esconde ni la manda al
 * principio. Sin vista («Todas») o sin nada guardado, es el schema tal cual.
 *
 * Las claves de `order` que ya no existen (una propiedad renombrada o borrada)
 * se saltan sin más: el `.md` es del usuario y puede hablar de una columna que
 * la app ya no tiene. */
function columnasEnOrden(database, view) {
  // La celda de subtareas no es una columna: es el vínculo con la fila madre y
  // su valor es un id, que no dice nada en una celda. La jerarquía se lee en la
  // indentación y en el contador de la tarjeta.
  const schema = (database.schema_def || []).filter(
    (p) => p && p.type !== "parent_row",
  );
  if (!view) return schema.slice();
  const porClave = new Map(schema.map((p) => [p.key, p]));
  const orden = Array.isArray(view.config?.order) ? view.config.order : [];
  const columnas = [];
  for (const clave of orden) {
    const prop = porClave.get(String(clave));
    if (!prop) continue;
    columnas.push(prop);
    porClave.delete(prop.key);
  }
  for (const prop of schema) {
    if (porClave.has(prop.key)) columnas.push(prop);
  }
  return columnas;
}

/** Las columnas que la tabla enseña: el orden de `ordenadas` sin las ocultas
 * (`visible_properties`). Una lista vacía es «todas» (el `.md` no la escribe):
 * solo se filtra cuando la vista dice de verdad cuáles enseña. */
function columnasDeVista(database, view) {
  const columnas = columnasEnOrden(database, view);
  const ocultas = view?.config?.visible_properties;
  if (!Array.isArray(ocultas) || ocultas.length === 0) return columnas;
  const visibles = new Set(ocultas.map(String));
  return columnas.filter((p) => visibles.has(p.key));
}

/**
 * El ancho de la tabla es la suma de sus columnas. Con `table-layout: fixed`
 * el ancho de la tabla manda: si no cuadra con la suma de los `<col>`, el
 * navegador reparte la diferencia y la columna que no se está tocando cambia
 * de tamaño sola. Fijarlo a la suma deja cada columna exactamente en lo que
 * dice la vista.
 */
function fijarAnchoDeTabla(table, cols) {
  let suma = 0;
  for (const col of cols.values()) suma += parseFloat(col.style.width) || 0;
  if (suma > 0) table.style.width = `${suma}px`;
}

/** Con anchos guardados el reparto lo manda la vista y no el contenido, así
 * que la tabla pasa a `table-layout: fixed`: en `auto` una columna nunca se
 * estrecha por debajo de su texto, y el asa parecería no responder al
 * arrastrarla hacia dentro. Para que las demás columnas no se repartan el
 * hueco a partes iguales, primero se mide lo que ocupaban con el reparto
 * automático (`getBoundingClientRect` del `<th>`, que es el ancho de la
 * columna) y se congelan: a partir de ahí solo cambia la que se arrastra.
 *
 * Necesita la tabla en el documento: si aún no lo está (medidas a cero) no
 * toca nada y se queda el reparto del navegador con el ancho guardado como
 * mínimo, que es lo que había antes de esta función. */
function congelarAnchos(table, cols, anchos) {
  if (table.classList.contains("has-col-widths") || cols.size === 0) return;
  // Se mide con el reparto automático y SIN los anchos guardados: un `<col>`
  // con ancho hace que el navegador mida las demás por su mínimo (la palabra
  // más larga), no por su contenido, y al congelar se quedarían estrechas.
  const previos = new Map([...cols].map(([clave, col]) => [clave, col.style.width]));
  for (const col of cols.values()) col.style.width = "";
  const medidos = new Map();
  for (const clave of cols.keys()) {
    const celda = table.querySelector(`th[data-col-key="${CSS.escape(clave)}"]`);
    const natural = Math.ceil(celda ? celda.getBoundingClientRect().width : 0);
    if (!natural) {
      // La tabla aún no tiene caja (no está en el documento): no se puede
      // medir. Se deja como estaba y manda el reparto del navegador.
      for (const [k, ancho] of previos) {
        const col = cols.get(k);
        if (col && ancho) col.style.width = ancho;
      }
      return;
    }
    medidos.set(clave, natural);
  }
  table.classList.add("has-col-widths");
  for (const [clave, col] of cols) {
    const anchoMd = claveDeAncho(clave);
    const ancho = (anchoMd && anchos.get(anchoMd)) || medidos.get(clave);
    if (ancho) col.style.width = `${ancho}px`;
  }
  fijarAnchoDeTabla(table, cols);
}

/** El ancho que ocupa el contenido de una columna, medido con el reparto
 * automático y con la tabla a su ancho natural (`max-content`): es lo que hace
 * un doble clic en el asa, como en una hoja de cálculo. Deja la tabla
 * congelada con las demás columnas como estaban y devuelve el ancho acotado.
 *
 * La medida se toma SIN el `max-width: 240px` de las celdas (`is-measuring`):
 * ese tope es el recorte de la pintura, no el contenido, y cortaba la medida en
 * 240 px, así que cualquier texto más largo se quedaba en 240 y con puntos
 * suspensivos —justo el caso en el que se usa el doble clic—. El tope de
 * verdad lo pone `acotarAncho` (64-560). */
function ajustarAlContenido(table, cols, domKey, anchos) {
  const previos = new Map([...cols].map(([k, col]) => [k, col.style.width]));
  const eraFija = table.classList.contains("has-col-widths");
  const anchoTabla = table.style.width;
  table.classList.remove("has-col-widths");
  table.classList.add("is-measuring");
  table.style.width = "max-content";
  for (const col of cols.values()) col.style.width = "";
  const celda = table.querySelector(`th[data-col-key="${CSS.escape(domKey)}"]`);
  const medido = Math.ceil(celda ? celda.getBoundingClientRect().width : 0);
  table.style.width = anchoTabla;
  table.classList.remove("is-measuring");
  for (const [k, ancho] of previos) {
    const col = cols.get(k);
    if (col) col.style.width = ancho;
  }
  if (eraFija) table.classList.add("has-col-widths");
  if (!medido) return null;
  congelarAnchos(table, cols, anchos);
  const px = acotarAncho(medido);
  const col = cols.get(domKey);
  if (col) col.style.width = `${px}px`;
  fijarAnchoDeTabla(table, cols);
  return px;
}

/** El asa del borde derecho de una cabecera: arrastra para el ancho, doble
 * clic para ajustarlo al contenido y las flechas (con el foco en el asa) para
 * moverlo sin ratón.
 *
 * Es un `div` con `role="separator"` y no un botón: no tiene texto propio y su
 * nombre accesible es el de la columna. Mide 9 px de zona táctil aunque se vea
 * como una línea de 1 px, y lleva `touch-action: none` para que el dedo ajuste
 * el ancho en vez de desplazar la tabla.
 *
 * `clave` es la clave con la que se guarda el ancho en la vista y `domKey` la
 * de la columna en la tabla: desde que las columnas de servicio llevan `@`
 * delante (`@title`) no son la misma, y una propiedad llamada «Open» no puede
 * quedarse con el ancho de la columna del botón de abrir.
 *
 * El `.md` se escribe UNA vez, al soltar: mientras se arrastra solo cambia el
 * `<col>` de esa columna, así que la pantalla responde en cada píxel sin
 * mandar un PATCH por movimiento. */
function asaDeAncho({ clave, domKey, nombre, table, cols, anchos, onCambio }) {
  const asa = document.createElement("div");
  asa.className = "col-resizer";
  asa.dataset.colResizer = clave;
  asa.setAttribute("role", "separator");
  asa.setAttribute("aria-orientation", "vertical");
  asa.setAttribute("aria-label", `Ancho de la columna «${nombre}»`);
  asa.title = `Arrastra para cambiar el ancho de «${nombre}» · doble clic: ajustar al contenido`;
  asa.tabIndex = 0;

  const celdaDe = () => table.querySelector(`th[data-col-key="${CSS.escape(domKey)}"]`);
  const guardar = (px) => {
    if (px === null) return;
    anchos.set(clave, px);
    onCambio({ widths: Object.fromEntries(anchos) });
  };
  const anchoActual = () => {
    const celda = celdaDe();
    return Math.ceil(celda ? celda.getBoundingClientRect().width : 0);
  };

  let arrastre = null;
  let conCalma = null;
  asa.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    // Antes de mover nada: la tabla pasa a `fixed` con las columnas de ahora
    // congeladas (si no, arrastrar hacia dentro no estrecha nada).
    congelarAnchos(table, cols, anchos);
    arrastre = { x: e.clientX, inicio: anchoActual(), ultimo: null };
    asa.setPointerCapture?.(e.pointerId);
    asa.classList.add("is-resizing");
  });
  asa.addEventListener("pointermove", (e) => {
    if (!arrastre) return;
    const px = acotarAncho(arrastre.inicio + (e.clientX - arrastre.x));
    arrastre.ultimo = px;
    const col = cols.get(domKey);
    if (col) col.style.width = `${px}px`;
    fijarAnchoDeTabla(table, cols);
  });
  const soltar = () => {
    if (!arrastre) return;
    const px = arrastre.ultimo;
    arrastre = null;
    asa.classList.remove("is-resizing");
    // Sin movimiento no hay nada que guardar: un clic en el asa no escribe en
    // el `.md` (y así el doble clic puede ser otra cosa).
    if (px !== null) guardar(px);
  };
  asa.addEventListener("pointerup", soltar);
  asa.addEventListener("pointercancel", soltar);

  asa.addEventListener("dblclick", (e) => {
    e.preventDefault();
    e.stopPropagation();
    guardar(ajustarAlContenido(table, cols, domKey, anchos));
  });

  asa.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    e.stopPropagation();
    const paso = (e.key === "ArrowLeft" ? -8 : 8) * (e.shiftKey ? 4 : 1);
    congelarAnchos(table, cols, anchos);
    const px = acotarAncho(anchoActual() + paso);
    const col = cols.get(domKey);
    if (col) col.style.width = `${px}px`;
    fijarAnchoDeTabla(table, cols);
    // Las flechas se repiten: se guarda cuando dejas de pulsar, no una vez por
    // píxel (cada guardado repinta la base entera y se llevaría el foco).
    clearTimeout(conCalma);
    conCalma = setTimeout(() => guardar(px), 300);
  });
  return asa;
}

/** Arrastrar una cabecera para cambiar el orden de las columnas de la vista.
 *
 * Con eventos de puntero y no con el arrastre nativo del navegador: el nativo
 * no existe en táctil, no deja pintar una línea de inserción propia y su
 * `dataTransfer` no aporta nada aquí. Empieza tras 5 px de movimiento para que
 * un clic siga siendo un clic (la cabecera ordena) y el arrastre sea otra cosa.
 *
 * El ciclo de vida va en `document` y el puntero se captura en cuanto el
 * arrastre empieza de verdad. Colgado de `pointerup` en la cabecera, soltar
 * sobre las filas (el gesto más normal: arrastras y sueltas un poco más abajo)
 * no disparaba nada: la línea de inserción y la cabecera al 45 % se quedaban
 * puestas y el siguiente `pointerup` suelto —sin pulsar— ejecutaba el
 * `reordenar` pendiente y reescribía `views[].order` solo.
 *
 * Solo con ratón: en el móvil arrastrar sobre la cabecera es el scroll
 * horizontal de la tabla, y quedarse ese gesto dejaría la tabla sin
 * desplazamiento. El ancho, en cambio, sí se ajusta con el dedo (el asa es su
 * propia zona táctil). Con teclado, Alt+flecha mueve la columna enfocada.
 */
function instalarArrastreDeColumnas({ wrap, headRow, columnas, todas, onCambio }) {
  const claves = columnas.map((p) => p.key);
  const sinOcultar = (todas || columnas).map((p) => p.key);
  const cabeceras = () =>
    [...headRow.querySelectorAll("th[data-col-key]")].filter((th) =>
      claves.includes(th.dataset.colKey),
    );

  let arrastre = null;
  let linea = null;
  let ultimoArrastre = 0;

  const terminar = () => {
    if (linea) linea.remove();
    linea = null;
    wrap.classList.remove("is-col-dragging");
    cabeceras().forEach((th) => th.classList.remove("is-col-dragging"));
    arrastre = null;
    document.removeEventListener("pointermove", mover);
    document.removeEventListener("pointerup", soltar);
    document.removeEventListener("pointercancel", terminar);
    headRow.removeEventListener("lostpointercapture", terminar);
  };

  /** El orden nuevo, sobre TODAS las columnas: las ocultas se quedan en su
   * sitio y las visibles se recolocan entre ellas, que es lo que el usuario ve
   * y lo que no puede perder al volver a mostrar una columna.
   *
   * `destino` se cuenta sobre las columnas visibles de ahora, antes de quitar
   * la que se suelta. */
  const reordenar = (clave, destino) => {
    const desde = claves.indexOf(clave);
    if (desde === -1 || destino === null) return false;
    const hueco = destino > desde ? destino - 1 : destino;
    if (hueco === desde) return false; // se ha soltado donde estaba
    const nuevas = claves.filter((k) => k !== clave);
    nuevas.splice(hueco, 0, clave);
    let i = 0;
    onCambio({ order: sinOcultar.map((k) => (claves.includes(k) ? nuevas[i++] : k)) });
    return true;
  };

  /** Se suelta el botón: se cierra el arrastre SIEMPRE (haya o no arrastre de
   * verdad, esté el puntero sobre la cabecera o sobre una fila) y solo se
   * reordena si de verdad se ha arrastrado. */
  const soltar = () => {
    const estado = arrastre;
    terminar();
    if (!estado || !estado.activo) return;
    ultimoArrastre = Date.now();
    reordenar(estado.clave, estado.destino);
  };

  const mover = (e) => {
    if (!arrastre) return;
    if (!arrastre.activo) {
      if (Math.abs(e.clientX - arrastre.x) < 5) return;
      arrastre.activo = true;
      arrastre.th.classList.add("is-col-dragging");
      wrap.classList.add("is-col-dragging");
      linea = document.createElement("div");
      linea.className = "col-drop-line";
      wrap.appendChild(linea);
      // La captura se pide aquí y no en el `pointerdown`: capturar antes se
      // comería el clic que ordena la columna. Con la captura puesta,
      // `pointerup` llega aunque se suelte fuera de la ventana.
      try {
        headRow.setPointerCapture(e.pointerId);
      } catch {
        // Sin captura basta con los oyentes del documento.
      }
    }
    const cajas = cabeceras();
    // La mitad de cada cabecera es el punto de cambio: a la izquierda de su
    // centro la columna cae antes, a la derecha después.
    let destino = cajas.length;
    for (let i = 0; i < cajas.length; i += 1) {
      const caja = cajas[i].getBoundingClientRect();
      if (e.clientX < caja.left + caja.width / 2) {
        destino = i;
        break;
      }
    }
    arrastre.destino = destino;
    const fila = headRow.getBoundingClientRect();
    const marco = wrap.getBoundingClientRect();
    const x = destino === 0 ? cajas[0].getBoundingClientRect().left : cajas[destino - 1].getBoundingClientRect().right;
    linea.style.left = `${x - marco.left + wrap.scrollLeft - 1}px`;
    linea.style.top = `${fila.top - marco.top}px`;
    linea.style.height = `${fila.height}px`;
  };

  headRow.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.pointerType !== "mouse") return;
    // En el borde manda el asa, y el «⋯» y el «+» son botones: un arrastre que
    // empiece en ellos no reordena nada (se quedaría el menú de la columna sin
    // abrir, o el formulario de la columna nueva).
    if (e.target.closest(".col-resizer, .db-col-menu-btn, .db-col-add-btn")) return;
    const th = e.target.closest("th[data-col-key]");
    if (!th || !claves.includes(th.dataset.colKey)) return;
    terminar(); // un arrastre anterior a medias no se hereda
    arrastre = { th, clave: th.dataset.colKey, x: e.clientX, activo: false, destino: null };
    document.addEventListener("pointermove", mover);
    document.addEventListener("pointerup", soltar);
    document.addEventListener("pointercancel", terminar);
    headRow.addEventListener("lostpointercapture", terminar);
  });

  // El clic que cierra el arrastre no puede ordenar la columna: soltar una
  // cabecera encima de otra posición no es «ordena por esta».
  headRow.addEventListener(
    "click",
    (e) => {
      if (Date.now() - ultimoArrastre < 250) {
        e.preventDefault();
        e.stopPropagation();
      }
    },
    true,
  );

  headRow.addEventListener("keydown", (e) => {
    if (!e.altKey || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
    const th = e.target.closest?.("th[data-col-key]");
    if (!th || !claves.includes(th.dataset.colKey)) return;
    // La tecla se consume ANTES de mirar el borde: en la primera columna
    // `Alt+←` no tiene a dónde mover, pero el navegador sí sabe qué hacer con
    // ella (atrás) y la app se iría a la página anterior —justo la pulsación
    // de más al llevar una columna al principio—. Lo mismo con `Alt+→` en la
    // última, que dispara «adelante».
    e.preventDefault();
    e.stopPropagation();
    const desde = claves.indexOf(th.dataset.colKey);
    const hasta = desde + (e.key === "ArrowLeft" ? -1 : 1);
    if (hasta < 0 || hasta >= claves.length) return;
    const nuevas = claves.slice();
    nuevas.splice(desde, 1);
    nuevas.splice(hasta, 0, th.dataset.colKey);
    let i = 0;
    onCambio({ order: sinOcultar.map((k) => (claves.includes(k) ? nuevas[i++] : k)) });
  });
}

/** La tabla de una base. Es la MISMA en la página de la base y dentro de una
 * nota (bloque ```mdm-database, P17.2): mismos valores, mismas clases y las
 * mismas celdas editables -- dos renders distintos acabarían enseñando cosas
 * distintas. La única diferencia es `deletable: false`, que quita el botón de
 * borrar de la columna de acciones (en una nota no se borran filas); la casilla
 * de la selección está en las dos.
 *
 * `hooks` es el comportamiento extra de la copia incrustada y se pasa de mano
 * en mano hasta `saveRow` (`paintCell` -> `editCell` -> `saveRow`); una celda
 * repintada lo vuelve a recibir (`repaintCell`), así que no se pierde:
 *   - `embed`: marca las celdas con `data-db-action` para que el widget del
 *     editor no se trague el clic y para que las teclas que consumimos no
 *     lleguen al keymap de CodeMirror (ver `DatabaseBlockWidget` en entry.js).
 *     También es lo que habilita el recorrido con Tab celda a celda: en la
 *     página de la base el Tab es el del navegador, que pasa por los botones de
 *     borrar de cada fila (aquí no hay columna de acciones).
 *   - `onBeforeSave()` / `onSaved()`: ver `renderDatabaseEmbed`.
 *
 * La primera columna es la puerta a la fila como página: un botón de abrir
 * explícito, porque el clic en una celda —que es lo que edita— no puede
 * comerse la apertura. También se abre con Ctrl/Cmd+Enter desde cualquier
 * celda de la fila (ver el listener del tbody al final).
 *
 * La última es la de acciones: la casilla de la selección múltiple (y el botón
 * de borrar cuando `deletable`). `onRepintar` es lo que se llama después de una
 * acción en bloque para volver a pintar con lo que diga el servidor.
 */
// ------------------------------------ columnas desde la cabecera (P3)
//
// La cabecera de una columna es también su menú —renombrar, ocultar en la
// vista, cambiar el tipo y borrar la propiedad—, y el «+» del final añade una
// columna sin salir de la tabla. Todo pasa por el mismo camino que el diálogo
// de «Propiedades»: un PATCH del `schema_def` con las claves que ya había, y
// `plan_schema` (app/properties.py) deriva la clave nueva y migra las filas y
// las vistas. Aquí no se reimplementa ninguna migración: si el servidor falla,
// el `.md` no ha cambiado.
//
// El menú y el formulario del «+» se cuelgan de <body> con `position: fixed`:
// dentro de `.table-wrap` (que tiene scroll horizontal) un desplegable absoluto
// quedaría recortado, y en la tabla incrustada de una nota el widget del editor
// se tragaría los clics que no lleven `data-db-action`.

/** Las claves que enseña una vista, en su orden, o `null` si las enseña todas.
 *
 * `visible_properties` vacío (o ausente) significa «todas»: es el mismo
 * contrato que usa la exportación (`app/export.py`), para que exportar una
 * vista y mirarla enseñen las mismas columnas. Una lista con claves que ya no
 * existen se queda en las que sí están; si no queda ninguna, todas (una tabla
 * de solo títulos no es una vista, es un fallo).
 */
function clavesVisibles(database, view) {
  const visible = view?.config?.visible_properties;
  if (!Array.isArray(visible) || visible.length === 0) return null;
  const existentes = new Set((database.schema_def || []).map((p) => p.key));
  const claves = [...new Set(visible.filter((k) => existentes.has(k)))];
  return claves.length ? claves : null;
}

/** Un valor de celda cuenta como «tiene valor» en el recuento de la
 * confirmación de borrar: `false` es un valor guardado (la casilla existe y
 * está desmarcada) y una lista vacía no lo es. */
function tieneValorDeColumna(valor) {
  if (valor === null || valor === undefined) return false;
  if (typeof valor === "string") return valor.trim() !== "";
  if (Array.isArray(valor)) return valor.length > 0;
  return true;
}

/** `schema_def` con una propiedad cambiada, sin tocar las demás.
 *
 * Lo que deja de tener sentido con el tipo nuevo se quita, igual que hace el
 * diálogo de «Propiedades»: `options` solo vale en Selección y Multiselección, y
 * `target_database`/`multiple` solo en Relación. */
function esquemaConPropiedad(database, key, cambios) {
  return (database.schema_def || []).map((p) => {
    if (p.key !== key) return { ...p };
    const nueva = { ...p, ...cambios };
    if (nueva.type !== "select" && nueva.type !== "multiselect") {
      // El orden declarado, su modo y las opciones solo existen en una selección.
      delete nueva.options;
      delete nueva.sort_mode;
      delete nueva.sort_order;
    }
    if (nueva.type !== "relation") {
      delete nueva.target_database;
      delete nueva.multiple;
    }
    return nueva;
  });
}

/** El PATCH del schema, único camino: la respuesta del servidor es el estado
 * nuevo (puede haber cambiado una clave) y se copia sobre `database`. */
async function guardarEsquemaDeColumnas(database, schema) {
  const actualizada = await api(`/databases/${database.id}`, {
    method: "PATCH",
    body: JSON.stringify({ schema_def: schema }),
  });
  Object.assign(database, actualizada);
  return actualizada;
}

// --- panel flotante (menú de columna y formulario del «+») ---------------

let panelDeColumnaAbierto = null; // { el, anchor, quitar, alCerrar }

/** Cierra el panel flotante. `foco` devuelve el foco a la cabecera que lo
 * abrió (el botón desaparece del DOM si no). */
function cerrarPanelDeColumna({ foco = false } = {}) {
  const abierto = panelDeColumnaAbierto;
  if (!abierto) return;
  panelDeColumnaAbierto = null;
  abierto.quitar();
  abierto.el.remove();
  abierto.alCerrar?.();
  if (foco && abierto.anchor?.isConnected) abierto.anchor.focus({ preventScroll: true });
  if (refrescoDeBasesPendiente) {
    refrescoDeBasesPendiente = false;
    // Un panel que se abre desde otro (el menú pasa al formulario de la
    // columna nueva) cierra el primero: el repintado espera al último.
    setTimeout(() => {
      if (panelDeColumnaAbierto) {
        refrescoDeBasesPendiente = true;
        return;
      }
      state.noteSession?.refreshDatabaseEmbeds?.();
      state.noteSession?.refreshRowPanel?.();
    }, 0);
  }
}

/** Coloca un panel `position: fixed` pegado a su ancla, sin salirse del
 * viewport. A 375 px el menú mide como mucho `100vw - 16px` y se pega al
 * borde; si abajo no cabe, se abre hacia arriba. */
// ------------------------------------------------ compartir (3.0, hito 3)
// Quién puede qué sobre una página o una base: `GET/PUT /acl/{tipo}/{id}`.
// Como en Notion, cada cambio se guarda en el acto (no hay botón «Guardar»).
// Si la página hereda, se enseña de quién y con qué entradas; dejar de heredar
// copia esas entradas como punto de partida (y te deja a ti de propietario).

const NIVELES_ACL = [
  ["view", "Puede ver"],
  ["edit", "Puede editar"],
  ["owner", "Propietario"],
];

function botonCompartir(tipo, id, titulo) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn share-btn";
  btn.innerHTML = `${svgIcon("i-users")}<span>Compartir</span>`;
  btn.title = "Quién puede ver o editar esto";
  btn.setAttribute("aria-haspopup", "dialog");
  btn.addEventListener("click", () => abrirCompartir(btn, tipo, id, titulo()));
  return btn;
}

async function abrirCompartir(anchor, tipo, id, titulo) {
  let datos;
  let gente;
  try {
    [datos, gente] = await Promise.all([api(`/acl/${tipo}/${id}`), cargarPersonas()]);
  } catch (e) {
    reportError("No se pudieron leer los permisos", e);
    return;
  }
  const panel = document.createElement("div");
  panel.className = "db-col-menu share-panel";
  panel.setAttribute("role", "dialog");
  const yo = String(sessionUser.me?.id ?? "");
  const pintar = () => pintarCompartir(panel, { tipo, id, titulo, datos, gente, yo, guardar });
  async function guardar(cuerpo) {
    try {
      datos = await api(`/acl/${tipo}/${id}`, { method: "PUT", body: JSON.stringify(cuerpo) });
      pintar();
      scheduleTreeRefresh(); // el candado de la barra lateral
    } catch (e) {
      reportError("No se pudieron guardar los permisos", e);
    }
  }
  pintar();
  abrirPanelFlotante(anchor, panel, { etiqueta: `Compartir «${titulo}»`, foco: "select, input" });
}

function pintarCompartir(panel, { titulo, datos, gente, yo, guardar }) {
  panel.textContent = "";
  const h = document.createElement("h3");
  h.textContent = `Compartir «${titulo}»`;
  panel.append(h);
  const esPropietario = datos.your_level === "owner";
  const entradas = datos.entries.map((e) => ({ ...e }));
  const declarar = (lista) =>
    guardar({
      inherit: false,
      entries: lista.map(({ subject_type, subject_id, level }) => ({ subject_type, subject_id, level })),
    });

  if (!datos.is_root) {
    const fila = document.createElement("label");
    fila.className = "db-switch-row";
    const textos = document.createElement("span");
    textos.className = "db-switch-text";
    const t1 = document.createElement("span");
    t1.className = "db-switch-title";
    t1.textContent = datos.source_title ? `Heredar de «${datos.source_title}»` : "Heredar de la página superior";
    const t2 = document.createElement("span");
    t2.className = "db-switch-note";
    t2.textContent = datos.inherit
      ? "Quien ve la página de arriba ve esta."
      : "Esta página decide sus propios permisos.";
    textos.append(t1, t2);
    const sw = document.createElement("input");
    sw.type = "checkbox";
    sw.className = "db-switch";
    sw.setAttribute("role", "switch");
    sw.checked = datos.inherit;
    sw.disabled = !esPropietario;
    sw.addEventListener("change", () => {
      if (sw.checked) {
        guardar({ inherit: true });
        return;
      }
      // Dejar de heredar: lo heredado es el punto de partida, y tú propietario.
      const lista = entradas.filter((e) => !(e.subject_type === "user" && e.subject_id === yo));
      lista.unshift({ subject_type: "user", subject_id: yo, level: "owner" });
      declarar(lista);
    });
    fila.append(textos, sw);
    panel.append(fila);
  }

  const lista = document.createElement("div");
  lista.className = "share-list";
  const editable = esPropietario && !datos.inherit;
  if (!entradas.length) {
    const nadie = document.createElement("div");
    nadie.className = "share-note";
    nadie.textContent = "Solo la ven los administradores.";
    lista.append(nadie);
  }
  entradas.forEach((e, i) => {
    const fila = document.createElement("div");
    fila.className = "share-row";
    const dot = document.createElement("span");
    dot.className = "share-dot";
    if (e.subject_type === "workspace") {
      dot.innerHTML = svgIcon("i-users");
    } else if (avatarImg(e.avatar)) {
      dot.classList.add("has-img");
      dot.appendChild(avatarImg(e.avatar, ""));
    } else {
      dot.textContent = (e.name || "?").slice(0, 1).toUpperCase();
      if (e.color) dot.style.background = e.color;
    }
    const nombre = document.createElement("span");
    nombre.className = "share-name";
    nombre.textContent =
      (e.name || "Usuario borrado") + (e.subject_type === "user" && e.subject_id === yo ? " (tú)" : "");
    fila.append(dot, nombre);
    if (editable) {
      const nivel = document.createElement("select");
      nivel.className = "share-level";
      nivel.setAttribute("aria-label", `Nivel de ${e.name || "esta entrada"}`);
      NIVELES_ACL.forEach(([valor, texto]) => nivel.add(new Option(texto, valor, false, valor === e.level)));
      nivel.addEventListener("change", () => {
        entradas[i].level = nivel.value;
        declarar(entradas);
      });
      const quitar = document.createElement("button");
      quitar.type = "button";
      quitar.className = "icon-btn share-remove";
      quitar.innerHTML = svgIcon("i-x");
      quitar.title = "Quitar el acceso";
      quitar.setAttribute("aria-label", `Quitar a ${e.name || "esta entrada"}`);
      quitar.addEventListener("click", () => declarar(entradas.filter((_, j) => j !== i)));
      fila.append(nivel, quitar);
    } else {
      const nivel = document.createElement("span");
      nivel.className = "share-note";
      nivel.textContent = (NIVELES_ACL.find(([v]) => v === e.level) || [, e.level])[1];
      fila.append(nivel);
    }
    lista.append(fila);
  });
  panel.append(lista);

  if (editable) {
    const add = document.createElement("div");
    add.className = "share-add";
    const quien = document.createElement("select");
    quien.setAttribute("aria-label", "Añadir a alguien");
    quien.add(new Option("Añadir a alguien…", ""));
    const ya = new Set(entradas.map((e) => `${e.subject_type}:${e.subject_id}`));
    if (!ya.has(`workspace:${datos.workspace_id}`)) {
      quien.add(new Option("Todo el espacio", `workspace:${datos.workspace_id}`));
    }
    gente
      .filter((p) => !ya.has(`user:${p.id}`))
      .forEach((p) => quien.add(new Option(p.display_name || p.username, `user:${p.id}`)));
    const nivel = document.createElement("select");
    nivel.setAttribute("aria-label", "Con qué permiso");
    NIVELES_ACL.forEach(([valor, texto]) => nivel.add(new Option(texto, valor, false, valor === "view")));
    const invitar = document.createElement("button");
    invitar.type = "button";
    invitar.className = "btn";
    invitar.textContent = "Añadir";
    invitar.addEventListener("click", () => {
      if (!quien.value) return;
      const [subject_type, subject_id] = quien.value.split(":");
      declarar([...entradas, { subject_type, subject_id, level: nivel.value }]);
    });
    add.append(quien, nivel, invitar);
    panel.append(add);
  } else {
    const nota = document.createElement("div");
    nota.className = "share-note";
    nota.textContent = !esPropietario
      ? "Solo quien es propietario cambia quién entra."
      : "Para cambiarlos aquí, deja de heredar.";
    panel.append(nota);
  }
}

function colocarPanelFlotante(el, anchor) {
  const r = anchor.getBoundingClientRect();
  const caja = el.getBoundingClientRect();
  const margen = 8;
  let left = r.left;
  if (left + caja.width > window.innerWidth - margen) left = window.innerWidth - caja.width - margen;
  left = Math.max(margen, left);
  // Debajo del ancla, sin taparla nunca: si no cabe entero, se queda debajo
  // con su propio scroll mientras haya un sitio razonable; si abajo casi no hay
  // sitio, va encima (también con scroll si hace falta). Antes, sin sitio, se
  // subía hasta tapar la cabecera que lo había abierto.
  const abajo = window.innerHeight - margen - (r.bottom + 4);
  const encima = r.top - 4 - margen;
  el.style.maxHeight = "";
  const natural = el.scrollHeight || caja.height;
  let top;
  if (natural <= abajo || abajo >= 240 || abajo >= encima) {
    top = r.bottom + 4;
    if (natural > abajo) el.style.maxHeight = `${Math.max(120, Math.floor(abajo))}px`;
  } else {
    const alto = Math.min(natural, Math.floor(encima));
    if (natural > encima) el.style.maxHeight = `${Math.floor(encima)}px`;
    top = r.top - 4 - alto;
  }
  el.style.left = `${Math.round(left)}px`;
  el.style.top = `${Math.round(Math.max(margen, top))}px`;
}

/** Cuelga `el` de <body> junto a `anchor` y lo cierra al pinchar fuera, con
 * Escape, o si el ancla desaparece (un repintado de la tabla). */
function abrirPanelFlotante(anchor, el, { etiqueta = "", foco = null, alCerrar = null } = {}) {
  cerrarPanelDeColumna();
  el.classList.add("db-col-floating");
  if (!el.hasAttribute("role")) el.setAttribute("role", "menu");
  if (etiqueta) el.setAttribute("aria-label", etiqueta);
  document.body.appendChild(el);
  colocarPanelFlotante(el, anchor);
  anchor.setAttribute("aria-expanded", "true");

  const fuera = (e) => {
    if (el.contains(e.target) || anchor.contains(e.target)) return;
    cerrarPanelDeColumna();
  };
  const teclas = (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      cerrarPanelDeColumna({ foco: true });
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const items = [...el.querySelectorAll('[role^="menuitem"]:not(:disabled)')];
    if (!items.length) return;
    e.preventDefault();
    const i = items.indexOf(document.activeElement);
    const siguiente =
      e.key === "ArrowDown" ? (i + 1) % items.length : (i - 1 + items.length) % items.length;
    items[siguiente].focus();
  };
  // El ancla se va con un repintado (el menú guarda el tipo, el panel repinta
  // la tabla): sin ancla no hay sitio donde colgarse, así que se cierra.
  const recolocar = () => {
    if (!anchor.isConnected) {
      cerrarPanelDeColumna();
      return;
    }
    colocarPanelFlotante(el, anchor);
  };

  // El contenido puede crecer DESPUÉS de abrirse (la lista de bases destino
  // de una relación llega por la API): el panel se vuelve a colocar para no
  // quedarse pegado al borde de la ventana.
  const observador =
    typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => recolocar());
  observador?.observe(el);

  document.addEventListener("mousedown", fuera, true);
  document.addEventListener("keydown", teclas, true);
  window.addEventListener("resize", recolocar);
  window.addEventListener("scroll", recolocar, true);
  panelDeColumnaAbierto = {
    el,
    anchor,
    alCerrar,
    quitar: () => {
      observador?.disconnect();
      document.removeEventListener("mousedown", fuera, true);
      document.removeEventListener("keydown", teclas, true);
      window.removeEventListener("resize", recolocar);
      window.removeEventListener("scroll", recolocar, true);
      if (anchor.isConnected) anchor.setAttribute("aria-expanded", "false");
    },
  };

  const destino =
    (foco && el.querySelector(foco)) ||
    el.querySelector('[role^="menuitem"]:not(:disabled), input');
  destino?.focus?.({ preventScroll: true });
}

/** Una fila del menú. `motivo` (con `alElegir` nulo) la deja visible pero
 * apagada: al pulsarla se escribe el motivo dentro del menú, que es lo que
 * hace un estado apagado sin explicación (y en táctil no hay `hover`). */
function itemDeColumna(
  texto,
  { alElegir = null, peligro = false, motivo = "", marca = "", radio = false, icono = "" } = {},
) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "db-col-menu-item" + (peligro ? " danger" : "") + (motivo ? " is-off" : "");
  btn.setAttribute("role", radio ? "menuitemradio" : "menuitem");
  if (radio) btn.setAttribute("aria-checked", marca === "✓" ? "true" : "false");
  if (motivo) btn.title = motivo;
  if (icono) {
    const ic = document.createElement("span");
    ic.className = "db-col-menu-icon";
    ic.setAttribute("aria-hidden", "true");
    ic.innerHTML = icono;
    btn.appendChild(ic);
  }
  const etiqueta = document.createElement("span");
  etiqueta.className = "db-col-menu-text";
  etiqueta.textContent = texto;
  btn.appendChild(etiqueta);
  if (marca) {
    const m = document.createElement("span");
    m.className = "db-col-menu-mark";
    m.setAttribute("aria-hidden", "true");
    m.textContent = marca;
    btn.appendChild(m);
  }
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    alElegir?.(btn);
  });
  return btn;
}

function separadorDeMenu() {
  const sep = document.createElement("div");
  sep.className = "db-col-menu-sep";
  sep.setAttribute("role", "separator");
  return sep;
}

/** La nota de un menú (motivo de un ítem apagado, error del formulario). */
function notaDeMenu(menu) {
  let nota = menu.querySelector(".db-col-menu-note");
  if (!nota) {
    nota = document.createElement("p");
    nota.className = "db-col-menu-note";
    nota.setAttribute("role", "status");
    nota.hidden = true;
    menu.appendChild(nota);
  }
  return nota;
}

// --- acciones ------------------------------------------------------------

/** Las bases a las que puede apuntar una relación: todas menos la propia. */
async function basesParaRelacion(database) {
  const bases = await api("/databases");
  return (bases || []).filter((b) => String(b.id) !== String(database.id));
}

/** Pregunta la base destino de una relación con un panel junto a `anchor`.
 * Devuelve el id, `null` para «sin base destino», o `undefined` si se cierra
 * sin elegir. */
async function pedirBaseDestino(anchor, database, nombre) {
  let bases;
  try {
    bases = await basesParaRelacion(database);
  } catch (e) {
    reportError("No se pudieron leer las bases", e);
    return undefined;
  }
  return new Promise((resolve) => {
    const panel = document.createElement("div");
    panel.className = "db-col-menu";
    const cabecera = document.createElement("div");
    cabecera.className = "db-col-menu-label";
    cabecera.textContent = `¿A qué base apunta «${nombre}»?`;
    panel.appendChild(cabecera);
    if (!bases.length) {
      const nota = document.createElement("p");
      nota.className = "db-col-menu-note";
      nota.hidden = false;
      nota.textContent = "No hay otra base en el vault: una relación necesita una base a la que apuntar.";
      panel.appendChild(nota);
    }
    bases.forEach((b) => {
      panel.appendChild(
        itemDeColumna(b.title || "Sin título", {
          alElegir: () => {
            resolve(String(b.id));
            cerrarPanelDeColumna();
          },
        }),
      );
    });
    if (bases.length) {
      panel.appendChild(separadorDeMenu());
      panel.appendChild(
        itemDeColumna("Sin base destino", {
          alElegir: () => {
            resolve(null);
            cerrarPanelDeColumna();
          },
        }),
      );
    }
    abrirPanelFlotante(anchor, panel, {
      etiqueta: "Base destino de la relación",
      alCerrar: () => resolve(undefined),
    });
  });
}

/** Renombra una columna: el título va por `title_name` y una propiedad por el
 * schema (el servidor deriva la clave nueva y migra filas y vistas). */
async function renombrarColumna({ database, prop, onChanged }, nombre) {
  if (!prop) {
    const actualizada = await api(`/databases/${database.id}`, {
      method: "PATCH",
      body: JSON.stringify({ title_name: nombre }),
    });
    Object.assign(database, actualizada);
  } else {
    await guardarEsquemaDeColumnas(
      database,
      esquemaConPropiedad(database, prop.key, { name: nombre }),
    );
  }
  await onChanged?.();
}

/** Deja el rótulo de la columna como un campo de texto: Enter guarda, Escape
 * cancela y salir del campo guarda (como el título de una nota). */
function editarNombreEnCabecera(th, { nombre, prop, database, onChanged, label }) {
  const previos = [...th.childNodes];
  th.textContent = "";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "db-col-rename";
  input.value = nombre;
  input.setAttribute("aria-label", label);
  th.appendChild(input);
  input.focus();
  input.select();

  let terminado = false;
  const restaurar = () => {
    th.textContent = "";
    previos.forEach((nodo) => th.appendChild(nodo));
  };
  const terminar = async (guardar) => {
    if (terminado) return;
    terminado = true;
    const valor = input.value.trim();
    if (!guardar || !valor || valor === nombre) {
      restaurar();
      return;
    }
    input.disabled = true;
    try {
      await renombrarColumna({ database, prop, onChanged }, valor);
    } catch (e) {
      reportError("No se pudo renombrar la columna", e);
      restaurar();
    }
  };
  input.addEventListener("keydown", (e) => {
    // Dentro de una nota, lo que no se consuma aquí acaba en el keymap de
    // CodeMirror (su Enter insertaría un salto en el `.md` de la nota).
    e.stopPropagation();
    if (e.key === "Enter") {
      e.preventDefault();
      terminar(true);
    } else if (e.key === "Escape") {
      e.preventDefault();
      terminar(false);
    }
  });
  input.addEventListener("blur", () => terminar(true));
}

/** Oculta la columna SOLO en esta vista: `views[].visible_properties` en el
 * `.md` de la base. La propiedad sigue en el esquema y en «Todas». */
async function ocultarColumna({ database, view, prop, onChanged }) {
  if (!view || !prop) return;
  const visibles = clavesVisibles(database, view) || (database.schema_def || []).map((p) => p.key);
  const nuevas = visibles.filter((k) => k !== prop.key);
  // Una lista vacía se lee como «todas» (ver `clavesVisibles`), así que
  // esconder la última propiedad no se puede guardar y el menú ni lo ofrece.
  // Si se llega aquí es que la guarda falló: callarlo dejaría la vista
  // enseñándolo todo como si hubiera funcionado.
  if (!nuevas.length) {
    throw new Error(
      "no se puede ocultar la última propiedad de la vista: la lista vacía se lee como «todas»",
    );
  }
  const actualizada = await api(`/databases/${database.id}/views/${view.id}`, {
    method: "PATCH",
    body: JSON.stringify({ config: { ...(view.config || {}), visible_properties: nuevas } }),
  });
  Object.assign(view, actualizada);
  await onChanged?.();
}

/** Vuelve a enseñar una columna oculta en la vista. Con todas de vuelta, se
 * quita `visible_properties` (el estado «todas» es la ausencia de la lista, no
 * una lista con todas). */
async function mostrarColumna({ database, view, prop, onChanged }) {
  if (!view || !prop) return;
  const visibles = clavesVisibles(database, view);
  // Sin lista explícita la vista enseña todas: no hay nada oculto que devolver.
  if (!visibles) return;
  const config = { ...(view.config || {}) };
  // La devuelta entra AL FINAL de la lista de la vista. Reconstruir la lista
  // desde `schema_def` (lo que hacía antes) la devolvía a su sitio del
  // esquema y reordenaba una vista que tenía su propio orden: volver a
  // enseñar una columna no puede mover las demás. Su posición anterior no
  // está guardada en ningún sitio (al ocultarla sale de `visible_properties`),
  // así que el final es el único hueco que no descoloca a nadie.
  const nuevas = [...visibles.filter((k) => k !== prop.key), prop.key];
  const esquema = (database.schema_def || []).map((p) => p.key);
  // «Todas» es la ausencia de la clave, pero solo si el orden es el del
  // esquema: con un orden propio, borrar la lista lo perdería.
  const enOrdenDelEsquema =
    nuevas.length === esquema.length && nuevas.every((k, i) => k === esquema[i]);
  if (enOrdenDelEsquema) delete config.visible_properties;
  else config.visible_properties = nuevas;
  const actualizada = await api(`/databases/${database.id}/views/${view.id}`, {
    method: "PATCH",
    body: JSON.stringify({ config }),
  });
  Object.assign(view, actualizada);
  await onChanged?.();
}

async function mostrarTodasLasColumnas({ database, view, onChanged }) {
  if (!view) return;
  const config = { ...(view.config || {}) };
  delete config.visible_properties;
  const actualizada = await api(`/databases/${database.id}/views/${view.id}`, {
    method: "PATCH",
    body: JSON.stringify({ config }),
  });
  Object.assign(view, actualizada);
  await onChanged?.();
}

/** Cambia el tipo de una propiedad. A Relación se le pregunta la base destino
 * si todavía no la tiene (sin destino la columna no puede enseñar nada). */
async function cambiarTipoDeColumna(contexto, anchor, tipo) {
  const { database, prop, onChanged } = contexto;
  if (tipo === "relation" && !prop.target_database) {
    const destino = await pedirBaseDestino(anchor, database, prop.name);
    if (destino === undefined) return; // se cerró sin elegir
    await guardarEsquemaDeColumnas(
      database,
      esquemaConPropiedad(database, prop.key, { type: tipo, target_database: destino }),
    );
  } else {
    await guardarEsquemaDeColumnas(
      database,
      esquemaConPropiedad(database, prop.key, { type: tipo }),
    );
  }
  await onChanged?.();
}

/** Borra la propiedad del schema, con la cuenta de filas que tienen valor y la
 * promesa explícita de que los valores NO se van del `.md`. */
async function borrarColumna({ database, prop, onChanged }) {
  let filas;
  try {
    // Todas las filas de la base, no las de la vista: el recuento no puede
    // depender del filtro de la pestaña en la que esté el usuario.
    filas = await api(`/databases/${database.id}/rows`);
  } catch (e) {
    reportError("No se pudo contar las filas con valor", e);
    return;
  }
  const conValor = (filas || []).filter((f) =>
    tieneValorDeColumna(f.properties?.[prop.key]),
  ).length;
  const recuento =
    conValor === 0
      ? "Ninguna fila tiene valor en esta columna."
      : conValor === 1
        ? "1 fila tiene valor en esta columna."
        : `${conValor} filas tienen valor en esta columna.`;
  const pregunta =
    `¿Borrar la propiedad «${prop.name}»?\n\n${recuento}\n\n` +
    "Esos valores se quedan en el .md de cada fila: borrar la columna no borra " +
    "datos (es así hoy; el fichero de la fila no se toca). La propiedad solo " +
    "desaparece del esquema de la base y de las vistas que la nombran.";
  if (!confirm(pregunta)) return;
  const schema = (database.schema_def || [])
    .filter((p) => p.key !== prop.key)
    .map((p) => ({ ...p }));
  await guardarEsquemaDeColumnas(database, schema);
  await onChanged?.();
}

/** Añade una columna al final del schema. Si la vista limita sus columnas
 * (`visible_properties`), la nueva entra también al final de la lista: una
 * columna recién creada que no se ve en la pestaña en la que estás es un
 * misterio, no un resultado. */
async function crearColumnaEnLaBase(
  contexto,
  { nombre, tipo, destino, multiple = false, posicion = null, opciones = [], icono = "" },
) {
  const { database, view, onChanged } = contexto;
  const nueva = { name: nombre, type: tipo };
  if (opciones.length) nueva.options = opciones.slice();
  if (icono) nueva.icon = icono;
  if (tipo === "relation") {
    if (destino) nueva.target_database = destino;
    if (multiple) nueva.multiple = true;
  }
  const schema = [...(database.schema_def || []).map((p) => ({ ...p })), nueva];
  const actualizada = await guardarEsquemaDeColumnas(database, schema);
  const creada = actualizada.schema_def[actualizada.schema_def.length - 1];
  const visibles = view?.config?.visible_properties;
  if (creada && Array.isArray(visibles) && visibles.length) {
    const config = {
      ...(view.config || {}),
      visible_properties: [...visibles.filter((k) => k !== creada.key), creada.key],
    };
    const vista = await api(`/databases/${database.id}/views/${view.id}`, {
      method: "PATCH",
      body: JSON.stringify({ config }),
    });
    Object.assign(view, vista);
  }
  // «Insertar a la izquierda/derecha»: la columna nueva se coloca junto a la
  // de partida en el orden de la vista (`views[].order`). Sin dónde guardar
  // el orden, va al final, como el «+».
  if (creada && posicion && contexto.onCambioColumnas) {
    const claves = columnasEnOrden(database, view).map((p) => p.key).filter((k) => k !== creada.key);
    let i = posicion.junto === "@title" ? 0 : claves.indexOf(posicion.junto);
    if (i < 0) i = claves.length;
    else if (posicion.junto !== "@title" && posicion.lado === "derecha") i += 1;
    claves.splice(i, 0, creada.key);
    await guardarColumnasDeVista(contexto, { order: claves });
    return;
  }
  await onChanged?.();
}

// --- el menú de una columna ----------------------------------------------

/** Los tipos, con el actual marcado; sustituyen al contenido del menú (en el
 * sitio y no en un submenú volante: a 375 px un submenú al lado no cabe). */
function mostrarTiposDeColumna(menu, contexto, anchor) {
  const { prop } = contexto;
  menu.textContent = "";
  const cabecera = document.createElement("div");
  cabecera.className = "db-col-menu-label";
  cabecera.textContent = `Tipo de «${prop.name}»`;
  menu.appendChild(cabecera);
  menu.appendChild(
    itemDeColumna("‹ Volver", {
      alElegir: () => abrirMenuDeColumna(anchor, contexto),
    }),
  );
  Object.entries(TIPOS_VISIBLES).forEach(([tipo, etiqueta]) => {
    const actual = prop.type === tipo;
    menu.appendChild(
      itemDeColumna(etiqueta, {
        radio: true,
        marca: actual ? "✓" : "",
        alElegir: () => {
          if (actual) {
            cerrarPanelDeColumna({ foco: true });
            return;
          }
          cerrarPanelDeColumna();
          cambiarTipoDeColumna(contexto, anchor, tipo).catch((e) =>
            reportError("No se pudo cambiar el tipo de la columna", e),
          );
        },
      }),
    );
  });
  colocarPanelFlotante(menu, anchor);
  menu.querySelector('[role^="menuitem"]:not(:disabled)')?.focus({ preventScroll: true });
}

/** El submenú del estado de la base: «esta columna dice si está hecha», con
 * sus valores marcables.
 *
 * La regla es de la BASE, no de la columna (`closed: {property, values}` en su
 * `.md`): una sola por base, así que marcar otra columna mueve la regla y el
 * menú lo dice. Aquí solo se elige; quién cuenta como cerrada lo decide el
 * servidor con esa misma regla.
 *
 * El panel se queda abierto tras guardar (con las marcas nuevas y una nota) y
 * repinta la tabla al cerrarse: los contadores de las madres cambian en cuanto
 * la regla cambia, y el `.md` de ninguna fila se toca.
 */
function mostrarEstadoDeColumna(menu, contexto, anchor) {
  pintarEstadoDeColumna(menu, contexto, anchor);
  abrirPanelFlotante(anchor, menu, {
    etiqueta: `Estado de la base según «${contexto.prop.name}»`,
    alCerrar: () => contexto.onChanged?.(),
  });
}

/** Pinta las opciones del estado sin abrir ni recolocar el panel: lo usan la
 * primera apertura y cada guardado (para enseñar las marcas nuevas). */
function pintarEstadoDeColumna(menu, contexto, anchor) {
  const { database, prop } = contexto;
  menu.textContent = "";
  const cabecera = document.createElement("div");
  cabecera.className = "db-col-menu-label";
  cabecera.textContent = `«${prop.name}» dice si está hecha`;
  menu.appendChild(cabecera);
  menu.appendChild(
    itemDeColumna("‹ Volver", { alElegir: () => abrirMenuDeColumna(anchor, contexto) }),
  );

  const regla = reglaDeCierre(database);
  const activa = Boolean(regla && regla.property === prop.key);
  const valores = activa ? regla.values : [];
  const mismo = (a, b) => String(a).trim().toLowerCase() === String(b).trim().toLowerCase();

  const avisar = (texto) => {
    const nota = notaDeMenu(menu);
    nota.textContent = texto;
    nota.hidden = false;
  };

  const aplicar = async (nuevos) => {
    const otra =
      regla && regla.property !== prop.key ? nombreDePropiedad(database, regla.property) : null;
    try {
      const actualizada =
        nuevos === null
          ? await api(`/databases/${database.id}/closed`, { method: "DELETE" })
          : await api(`/databases/${database.id}/closed`, {
              method: "PUT",
              body: JSON.stringify({ property: prop.key, values: nuevos }),
            });
      Object.assign(database, actualizada);
      pintarEstadoDeColumna(menu, contexto, anchor);
      colocarPanelFlotante(menu, anchor);
      avisar(
        nuevos === null
          ? `«${prop.name}» deja de decir si una fila está hecha: la base no tiene estado y nada cuenta como cerrado.`
          : otra
            ? `«${prop.name}» pasa a ser el estado de la base y «${otra}» deja de decirlo: una base tiene un solo estado.`
            : "Guardado en el `.md` de la base: no se ha tocado ninguna fila.",
      );
    } catch (e) {
      reportError("No se pudo guardar el estado de la base", e);
      avisar(describeError(e));
    }
  };

  if (prop.type === "checkbox") {
    menu.appendChild(
      itemDeColumna("Marcada cuenta como hecha", {
        radio: true,
        marca: activa ? "✓" : "",
        alElegir: () => aplicar(activa ? null : []),
      }),
    );
  } else {
    const opciones = Array.isArray(prop.options) ? prop.options : [];
    if (!opciones.length) {
      menu.appendChild(
        itemDeColumna("Esta columna no tiene valores", {
          motivo: "Añade opciones a la columna («Propiedades…») antes de usarla como estado.",
          alElegir: () =>
            avisar("Añade opciones a la columna («Propiedades…») antes de usarla como estado."),
        }),
      );
    }
    opciones.forEach((valor) => {
      const marcada = activa && valores.some((v) => mismo(v, valor));
      menu.appendChild(
        itemDeColumna(valor, {
          radio: true,
          marca: marcada ? "✓" : "",
          alElegir: () => {
            const resto = activa ? valores.filter((v) => !mismo(v, valor)) : [];
            const nuevos = marcada ? resto : [...resto, valor];
            if (!nuevos.length) {
              avisar("Marca al menos un valor: sin valores no hay nada que cuente como cerrado.");
              return;
            }
            aplicar(nuevos);
          },
        }),
      );
    });
    if (activa) {
      menu.appendChild(separadorDeMenu());
      menu.appendChild(
        itemDeColumna("Esta columna deja de decir si está hecha", {
          peligro: true,
          alElegir: () => aplicar(null),
        }),
      );
    }
  }

  if (activa && regla.source === "done") {
    avisar(
      "Esta base lo dice con el marcador «done» anterior. Se sigue leyendo; al marcar aquí se guarda como regla de la base.",
    );
  }
}

const ICONOS_DE_MENU = (() => {
  const svg = (d) =>
    `<svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
  return {
    asc: svg('<path d="M12 19V5M5 12l7-7 7 7"/>'),
    desc: svg('<path d="M12 5v14M19 12l-7 7-7-7"/>'),
    sinOrden: svg('<path d="M18 6 6 18M6 6l12 12"/>'),
    ocultar: svg('<path d="M9.9 4.2A10 10 0 0 1 12 4c7 0 10 8 10 8a17 17 0 0 1-2.2 3.2M6.6 6.6C3.6 8.6 2 12 2 12s3 8 10 8a9.7 9.7 0 0 0 5.4-1.6"/><path d="m2 2 20 20"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>'),
    izquierda: svg('<path d="M4 4v16"/><path d="M20 12H9M13 8l-4 4 4 4"/>'),
    derecha: svg('<path d="M20 4v16"/><path d="M4 12h11M11 8l4 4-4 4"/>'),
    hecha: svg('<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>'),
    borrar: svg('<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'),
    ajustes: svg('<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>'),
  };
})();

/** Guarda un cambio de ancho/orden de columnas en la vista. `contexto.onCambioColumnas`
 * ya sabe dónde (y crea la vista si la base no tiene ninguna). */
function guardarColumnasDeVista(contexto, cambios) {
  if (!contexto.onCambioColumnas) return Promise.resolve(false);
  return Promise.resolve(contexto.onCambioColumnas(cambios)).then(() => true);
}

/** Las opciones de una selección, dentro del menú de su columna: cada valor
 * como su etiqueta de color, con su botón de quitar, y un campo para añadir
 * uno nuevo con Enter. Es la alternativa a la lista separada por comas del
 * diálogo de propiedades: se ve lo que hay, con el color que tendrá en la
 * tabla, y cada valor es un control con nombre propio para el lector de
 * pantalla. Guardar no cierra el menú (se pueden añadir varios seguidos); la
 * tabla se repinta al cerrarlo. */
function seccionDeOpciones(menu, contexto, marcarCambio) {
  const { database, prop } = contexto;
  const caja = document.createElement("div");
  caja.className = "db-col-options";
  const rotulo = document.createElement("div");
  rotulo.className = "db-col-menu-label";
  rotulo.id = `db-col-opts-${prop.key}`;
  rotulo.textContent = "Opciones";
  const lista = document.createElement("ul");
  lista.className = "db-col-options-list";
  lista.setAttribute("aria-labelledby", rotulo.id);
  const nueva = document.createElement("input");
  nueva.type = "text";
  nueva.className = "db-col-options-add";
  nueva.placeholder = "Añadir una opción…";
  nueva.setAttribute("aria-label", `Añadir una opción a «${prop.name}»`);
  nueva.autocomplete = "off";
  const aviso = document.createElement("p");
  aviso.className = "db-col-menu-note";
  aviso.setAttribute("role", "status");
  aviso.hidden = true;
  const avisar = (texto) => {
    aviso.textContent = texto;
    aviso.hidden = !texto;
  };

  const actual = () =>
    (database.schema_def || []).find((p) => p.key === prop.key) || prop;
  const guardar = async (opciones, texto) => {
    try {
      const cambios = { options: opciones };
      const p = actual();
      if (p.sort_mode === "manual") {
        cambios.sort_order = (p.sort_order || []).filter((v) => opciones.includes(v));
        opciones.forEach((v) => {
          if (!cambios.sort_order.includes(v)) cambios.sort_order.push(v);
        });
      }
      await guardarEsquemaDeColumnas(database, esquemaConPropiedad(database, prop.key, cambios));
      marcarCambio();
      pintar();
      avisar(texto);
    } catch (e) {
      reportError("No se pudieron guardar las opciones", e);
      avisar(describeError(e));
    }
  };

  const pintar = () => {
    const p = actual();
    const opciones = Array.isArray(p.options) ? p.options : [];
    lista.textContent = "";
    if (!opciones.length) {
      const vacio = document.createElement("li");
      vacio.className = "db-col-options-empty";
      vacio.textContent = "Todavía no hay ninguna. Escribe una abajo y pulsa Intro.";
      lista.appendChild(vacio);
    }
    opciones.forEach((valor) => {
      const li = document.createElement("li");
      li.className = "db-col-option";
      const tag = document.createElement("span");
      tag.innerHTML = htmlDeEtiqueta(valor, p);
      const quitar = document.createElement("button");
      quitar.type = "button";
      quitar.className = "db-col-option-remove";
      quitar.innerHTML = svgIcon("i-x");
      quitar.setAttribute("aria-label", `Quitar la opción «${valor}»`);
      quitar.title = "Quitar la opción (las filas que la tengan la conservan)";
      quitar.addEventListener("click", (e) => {
        e.stopPropagation();
        guardar(
          opciones.filter((v) => v !== valor),
          `«${valor}» ya no es una opción. Las filas que la tenían la conservan.`,
        ).then(() => nueva.focus({ preventScroll: true }));
      });
      li.append(tag, quitar);
      lista.appendChild(li);
    });
  };

  nueva.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    e.stopPropagation();
    const valor = nueva.value.trim();
    if (!valor) return;
    const opciones = Array.isArray(actual().options) ? actual().options : [];
    if (opciones.some((v) => v.toLowerCase() === valor.toLowerCase())) {
      avisar(`«${valor}» ya es una opción.`);
      return;
    }
    nueva.value = "";
    guardar([...opciones, valor], `Añadida «${valor}».`);
  });

  pintar();
  caja.append(rotulo, lista, nueva, aviso);
  return caja;
}

/** El menú de una columna, como el de Notion: arriba el nombre (se edita ahí
 * mismo: Intro o cerrar el menú guarda) con el icono de su tipo; debajo el
 * tipo, las opciones si es una selección, ordenar, ocultar, insertar a la
 * izquierda o a la derecha, el estado de la base, borrar y el diálogo de todas
 * las propiedades. Va colgado debajo de la cabecera, alineado a su IZQUIERDA.
 * `prop` nulo es la columna del título: no tiene tipo, no se borra y no se le
 * puede insertar nada a la izquierda (es siempre la primera). */
function abrirMenuDeColumna(anchor, contexto) {
  const { database, view, prop } = contexto;
  const esTitulo = !prop;
  const nombre = esTitulo ? database.title_name || "Título" : prop.name;
  const menu = document.createElement("div");
  menu.className = "db-col-menu db-col-menu-notion";
  let huboCambios = false;
  const marcarCambio = () => {
    huboCambios = true;
  };

  const avisar = (texto) => {
    const nota = notaDeMenu(menu);
    nota.textContent = texto;
    nota.hidden = false;
  };

  // --- el nombre, editable en el sitio
  const filaNombre = document.createElement("div");
  filaNombre.className = "db-col-menu-name";
  if (esTitulo) {
    filaNombre.innerHTML = iconoDeTipo("title");
  } else {
    // El icono de la propiedad se cambia aquí mismo, como en Notion.
    const botonIcono = document.createElement("button");
    botonIcono.type = "button";
    botonIcono.className = "db-prop-icon-btn";
    botonIcono.setAttribute("aria-label", "Cambiar el icono de la propiedad");
    botonIcono.setAttribute("aria-expanded", "false");
    botonIcono.title = "Icono de la propiedad";
    let iconoActual = prop.icon || "";
    const pintar = () => {
      botonIcono.innerHTML = iconoActual
        ? `<span class="db-prop-emoji">${escapeHtml(iconoActual)}</span>`
        : iconoDeTipo(prop.type);
    };
    pintar();
    const picker = selectorDeIcono(async (emoji) => {
      picker.hidden = true;
      botonIcono.setAttribute("aria-expanded", "false");
      if (emoji === iconoActual) return;
      try {
        await guardarEsquemaDeColumnas(database, esquemaConPropiedad(database, prop.key, { icon: emoji }));
        iconoActual = emoji;
        pintar();
        marcarCambio();
      } catch (e) {
        reportError("No se pudo cambiar el icono", e);
      }
    });
    picker.hidden = true;
    botonIcono.addEventListener("click", (e) => {
      e.stopPropagation();
      picker.hidden = !picker.hidden;
      botonIcono.setAttribute("aria-expanded", String(!picker.hidden));
    });
    filaNombre.appendChild(botonIcono);
    queueMicrotask(() => filaNombre.after(picker));
  }
  const campo = document.createElement("input");
  campo.type = "text";
  campo.value = nombre;
  campo.autocomplete = "off";
  campo.setAttribute("aria-label", esTitulo ? "Nombre de la columna del título" : "Nombre de la propiedad");
  filaNombre.appendChild(campo);
  menu.appendChild(filaNombre);
  let nombreGuardado = false;
  const guardarNombre = async () => {
    const nuevo = campo.value.trim();
    if (nombreGuardado || !nuevo || nuevo === nombre) return false;
    nombreGuardado = true;
    try {
      await renombrarColumna({ database, prop, onChanged: null }, nuevo);
      marcarCambio();
      return true;
    } catch (e) {
      nombreGuardado = false;
      reportError("No se pudo renombrar la columna", e);
      return false;
    }
  };
  campo.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      cerrarPanelDeColumna({ foco: true });
    }
  });

  // Lo que se hace al elegir una acción: primero se guarda el nombre si se ha
  // tocado (la acción puede repintar la tabla), luego se cierra el menú.
  const accion = (fn) => async () => {
    await guardarNombre();
    huboCambios = false; // la acción repinta por su cuenta
    cerrarPanelDeColumna();
    try {
      await fn();
    } catch (e) {
      reportError("No se pudo completar la acción", e);
    }
  };

  // --- tipo y opciones
  if (!esTitulo) {
    menu.appendChild(
      itemDeColumna("Tipo", {
        icono: ICONOS_DE_TIPO[prop.type] || ICONOS_DE_TIPO.text,
        marca: `${tipoVisible(prop.type)} ›`,
        alElegir: async () => {
          await guardarNombre();
          mostrarTiposDeColumna(menu, contexto, anchor);
        },
      }),
    );
    if (prop.type === "select" || prop.type === "multiselect") {
      menu.appendChild(separadorDeMenu());
      menu.appendChild(seccionDeOpciones(menu, contexto, marcarCambio));
    }
  }

  // --- ordenar
  const claveOrden = contexto.claveOrden;
  if (claveOrden && contexto.onOrdenar) {
    const actual = (contexto.sort || []).find((c) => c.key === claveOrden);
    menu.appendChild(separadorDeMenu());
    menu.appendChild(
      itemDeColumna("Orden ascendente", {
        icono: ICONOS_DE_MENU.asc,
        radio: true,
        marca: actual?.dir === "asc" ? "✓" : "",
        alElegir: accion(() => contexto.onOrdenar(claveOrden, "asc")),
      }),
    );
    menu.appendChild(
      itemDeColumna("Orden descendente", {
        icono: ICONOS_DE_MENU.desc,
        radio: true,
        marca: actual?.dir === "desc" ? "✓" : "",
        alElegir: accion(() => contexto.onOrdenar(claveOrden, "desc")),
      }),
    );
    if (actual) {
      menu.appendChild(
        itemDeColumna("Quitar el orden", {
          icono: ICONOS_DE_MENU.sinOrden,
          alElegir: accion(() => contexto.onOrdenar(claveOrden, null)),
        }),
      );
    }
  }

  menu.appendChild(separadorDeMenu());

  // --- ocultar
  // Desde la página de una fila (`enFila`) no hay vista: ocultar una columna e
  // insertar otra a su lado son cosas de una tabla, y allí siguen.
  if (contexto.enFila) {
    // nada: la fila enseña todas las propiedades de su base
  } else if (esTitulo) {
    // La columna del título no es una propiedad del schema: ocultarla es de la
    // BASE (`title_hidden`), no de la vista, y el rótulo lo dice.
    menu.appendChild(
      itemDeColumna("Ocultar (en toda la base)", {
        icono: ICONOS_DE_MENU.ocultar,
        alElegir: accion(async () => {
          const actualizada = await api(`/databases/${database.id}`, {
            method: "PATCH",
            body: JSON.stringify({ title_hidden: true }),
          });
          Object.assign(database, actualizada);
          await contexto.onChanged?.();
        }),
      }),
    );
  } else {
    const visibles =
      clavesVisibles(database, view) || (database.schema_def || []).map((p) => p.key);
    let motivo = "";
    if (!view && !contexto.onCambioColumnas) {
      motivo = "«Todas (sin vista)» no guarda columnas: elige una vista y se ocultará solo en ella.";
    } else if (visibles.length <= 1) {
      motivo = database.title_hidden
        ? "Es la única propiedad de esta vista: ocultarla dejaría la tabla sin ninguna columna."
        : "Es la única propiedad de esta vista: quedaría solo la columna del título, y una vista sin propiedades se guarda como «todas».";
    }
    menu.appendChild(
      motivo
        ? itemDeColumna("Ocultar", { icono: ICONOS_DE_MENU.ocultar, motivo, alElegir: () => avisar(motivo) })
        : itemDeColumna("Ocultar", {
            icono: ICONOS_DE_MENU.ocultar,
            // Sin vista (una base que aún no tiene ninguna), ocultar crea su
            // «Tabla» con la lista de columnas visibles.
            alElegir: accion(() =>
              view
                ? ocultarColumna({ database, view, prop, onChanged: contexto.onChanged })
                : guardarColumnasDeVista(contexto, {
                    visible_properties: visibles.filter((k) => k !== prop.key),
                  }),
            ),
          }),
    );
  }

  // --- insertar
  if (!contexto.enFila && (contexto.onCambioColumnas || contexto.onChanged)) {
    if (!esTitulo) {
      menu.appendChild(
        itemDeColumna("Insertar a la izquierda", {
          icono: ICONOS_DE_MENU.izquierda,
          alElegir: async () => {
            await guardarNombre();
            huboCambios = false; // la columna nueva repinta la tabla
            abrirFormularioDeColumna(anchor, contexto, { junto: prop.key, lado: "izquierda" });
          },
        }),
      );
    }
    menu.appendChild(
      itemDeColumna("Insertar a la derecha", {
        icono: ICONOS_DE_MENU.derecha,
        alElegir: async () => {
          await guardarNombre();
          huboCambios = false; // la columna nueva repinta la tabla
          abrirFormularioDeColumna(anchor, contexto, {
            junto: esTitulo ? "@title" : prop.key,
            lado: "derecha",
          });
        },
      }),
    );
  }

  // --- estado de la base
  if (!esTitulo && ["checkbox", "select", "multiselect"].includes(prop.type)) {
    const regla = reglaDeCierre(database);
    menu.appendChild(
      itemDeColumna("Dice si la fila está hecha", {
        icono: ICONOS_DE_MENU.hecha,
        marca: regla && regla.property === prop.key ? "✓ ›" : "›",
        alElegir: async () => {
          await guardarNombre();
          huboCambios = false; // el panel del estado repinta al cerrarse
          mostrarEstadoDeColumna(menu, contexto, anchor);
        },
      }),
    );
  }

  menu.appendChild(separadorDeMenu());
  if (!esTitulo) {
    menu.appendChild(
      itemDeColumna("Borrar la propiedad", {
        icono: ICONOS_DE_MENU.borrar,
        peligro: true,
        alElegir: accion(() =>
          borrarColumna({ database, prop, onChanged: contexto.onChanged }),
        ),
      }),
    );
  }
  menu.appendChild(
    itemDeColumna("Todas las propiedades…", {
      icono: ICONOS_DE_MENU.ajustes,
      alElegir: accion(() => openDatabaseDialog(database, contexto.onChanged)),
    }),
  );

  abrirPanelFlotante(anchor, menu, {
    etiqueta: esTitulo ? "Opciones de la columna del título" : `Opciones de la columna «${nombre}»`,
    foco: "input",
    // Cerrar el menú guarda el nombre si se ha tocado, y repinta la tabla si
    // algo ha cambiado (nombre u opciones).
    alCerrar: () => {
      guardarNombre().then(() => {
        if (huboCambios) {
          huboCambios = false;
          contexto.onChanged?.();
        }
      });
    },
  });
  campo.select();
}

// --- el «+» del final de la cabecera -------------------------------------

/** Los emojis que se ofrecen como icono de una propiedad: los que más se usan
 * en una base de tareas o de contactos. Escribir uno a mano también vale. */
const ICONOS_DE_PROPIEDAD = [
  "📌", "⭐", "🚦", "✅", "⏳", "📅", "🗓️", "⏰", "🔥", "⚡", "🎯", "🏷️",
  "📝", "📄", "📎", "🔗", "💬", "📞", "✉️", "👤", "👥", "🏢", "📍", "🌍",
  "💰", "💶", "📈", "📊", "🔢", "#️⃣", "🧩", "🛠️", "🔒", "❗", "❓", "💡",
];

/** Un selector de icono pequeño (rejilla de emojis + «Quitar»), dentro del
 * panel que lo abre. `alElegir(icono)` recibe el emoji o "" para quitarlo. */
function selectorDeIcono(alElegir) {
  const caja = document.createElement("div");
  caja.className = "db-icon-picker";
  caja.setAttribute("role", "listbox");
  caja.setAttribute("aria-label", "Icono de la propiedad");
  ICONOS_DE_PROPIEDAD.forEach((emoji) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "db-icon-option";
    b.textContent = emoji;
    b.setAttribute("role", "option");
    b.setAttribute("aria-label", `Icono ${emoji}`);
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      alElegir(emoji);
    });
    caja.appendChild(b);
  });
  const quitar = document.createElement("button");
  quitar.type = "button";
  quitar.className = "db-icon-remove";
  quitar.textContent = "Sin icono";
  quitar.addEventListener("click", (e) => {
    e.stopPropagation();
    alElegir("");
  });
  caja.appendChild(quitar);
  return caja;
}

/** «Nueva propiedad», como el panel de Notion: arriba el icono y el nombre;
 * debajo los tipos en una rejilla con su icono; si es una selección, sus
 * opciones se crean ahí mismo (etiquetas de color con «×» y un campo para
 * añadir); si es una relación, la base a la que apunta. Intro en el nombre o
 * «Crear propiedad» la guarda. `posicion` viene de «Insertar a la izquierda /
 * derecha» del menú de una columna. */
function abrirFormularioDeColumna(anchor, contexto, posicion = null) {
  const { database } = contexto;
  const form = document.createElement("form");
  form.className = "db-col-add-form db-prop-new";
  form.setAttribute("role", "dialog");
  form.setAttribute("aria-label", "Nueva propiedad");
  form.noValidate = true;

  let tipoElegido = "text";
  let icono = "";
  const opciones = [];

  const titulo = document.createElement("div");
  titulo.className = "db-col-menu-label";
  titulo.textContent = !posicion
    ? "Nueva propiedad"
    : posicion.lado === "izquierda"
      ? "Nueva propiedad a la izquierda"
      : "Nueva propiedad a la derecha";

  // --- icono + nombre
  const filaNombre = document.createElement("div");
  filaNombre.className = "db-col-menu-name";
  const botonIcono = document.createElement("button");
  botonIcono.type = "button";
  botonIcono.className = "db-prop-icon-btn";
  botonIcono.setAttribute("aria-label", "Elegir un icono");
  botonIcono.title = "Icono de la propiedad";
  botonIcono.setAttribute("aria-expanded", "false");
  const pintarIcono = () => {
    botonIcono.innerHTML = icono
      ? `<span class="db-prop-emoji">${escapeHtml(icono)}</span>`
      : iconoDeTipo(tipoElegido);
  };
  const nombre = document.createElement("input");
  nombre.type = "text";
  nombre.autocomplete = "off";
  nombre.className = "db-col-add-name";
  nombre.placeholder = "Nombre de la propiedad";
  nombre.setAttribute("aria-label", "Nombre de la propiedad");
  filaNombre.append(botonIcono, nombre);

  const picker = selectorDeIcono((emoji) => {
    icono = emoji;
    picker.hidden = true;
    botonIcono.setAttribute("aria-expanded", "false");
    pintarIcono();
    nombre.focus();
  });
  picker.hidden = true;
  botonIcono.addEventListener("click", (e) => {
    e.stopPropagation();
    picker.hidden = !picker.hidden;
    botonIcono.setAttribute("aria-expanded", String(!picker.hidden));
    colocarPanelFlotante(form, anchor);
  });

  // --- tipos
  const rotuloTipo = document.createElement("div");
  rotuloTipo.className = "db-col-menu-label";
  rotuloTipo.id = `db-prop-new-tipo-${Date.now()}`;
  rotuloTipo.textContent = "Tipo";
  const rejilla = document.createElement("div");
  rejilla.className = "db-type-grid";
  rejilla.setAttribute("role", "radiogroup");
  rejilla.setAttribute("aria-labelledby", rotuloTipo.id);
  const botonesTipo = new Map();
  Object.entries(TIPOS_VISIBLES).forEach(([valor, etiqueta]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "db-type-option";
    b.setAttribute("role", "radio");
    b.innerHTML = `${iconoDeTipo(valor)}<span>${escapeHtml(etiqueta)}</span>`;
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      elegirTipo(valor);
    });
    botonesTipo.set(valor, b);
    rejilla.appendChild(b);
  });

  // --- opciones (selección)
  const cajaOpciones = document.createElement("div");
  cajaOpciones.className = "db-col-options";
  const rotuloOpciones = document.createElement("div");
  rotuloOpciones.className = "db-col-menu-label";
  rotuloOpciones.textContent = "Opciones";
  const lista = document.createElement("ul");
  lista.className = "db-col-options-list";
  const nuevaOpcion = document.createElement("input");
  nuevaOpcion.type = "text";
  nuevaOpcion.className = "db-col-options-add";
  nuevaOpcion.placeholder = "Añadir una opción… (Intro)";
  nuevaOpcion.setAttribute("aria-label", "Añadir una opción");
  nuevaOpcion.autocomplete = "off";
  cajaOpciones.append(rotuloOpciones, lista, nuevaOpcion);
  const pintarOpciones = () => {
    lista.textContent = "";
    const falsa = { options: opciones };
    opciones.forEach((valor) => {
      const li = document.createElement("li");
      li.className = "db-col-option";
      const tag = document.createElement("span");
      tag.innerHTML = htmlDeEtiqueta(valor, falsa);
      const quitar = document.createElement("button");
      quitar.type = "button";
      quitar.className = "db-col-option-remove";
      quitar.innerHTML = svgIcon("i-x");
      quitar.setAttribute("aria-label", `Quitar la opción «${valor}»`);
      quitar.addEventListener("click", (e) => {
        e.stopPropagation();
        opciones.splice(opciones.indexOf(valor), 1);
        pintarOpciones();
        nuevaOpcion.focus();
      });
      li.append(tag, quitar);
      lista.appendChild(li);
    });
  };
  nuevaOpcion.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    e.stopPropagation();
    const valor = nuevaOpcion.value.trim();
    if (!valor) return;
    if (!opciones.some((v) => v.toLowerCase() === valor.toLowerCase())) opciones.push(valor);
    nuevaOpcion.value = "";
    pintarOpciones();
    colocarPanelFlotante(form, anchor);
  });

  // --- relación
  const campoDestinoWrap = document.createElement("div");
  campoDestinoWrap.className = "db-col-add-field";
  const campoDestino = document.createElement("label");
  campoDestino.textContent = "Base a la que apunta";
  const destino = document.createElement("select");
  destino.setAttribute("aria-label", "Base a la que apunta");
  // «Varias filas»: un interruptor en su propia fila, con lo que significa
  // debajo (como el «Límite» de una relación en Notion), no una casilla suelta.
  const campoMultiple = document.createElement("label");
  campoMultiple.className = "db-switch-row";
  const textoMultiple = document.createElement("span");
  textoMultiple.className = "db-switch-text";
  const tituloMultiple = document.createElement("span");
  tituloMultiple.className = "db-switch-title";
  tituloMultiple.textContent = "Varias filas";
  const notaMultiple = document.createElement("span");
  notaMultiple.className = "db-switch-note";
  notaMultiple.textContent = "Cada fila puede enlazar más de una fila de esa base.";
  textoMultiple.append(tituloMultiple, notaMultiple);
  const multiple = document.createElement("input");
  multiple.type = "checkbox";
  multiple.className = "db-switch";
  multiple.setAttribute("role", "switch");
  campoMultiple.append(textoMultiple, multiple);
  campoDestinoWrap.append(campoDestino, destino, campoMultiple);

  const error = document.createElement("p");
  error.className = "db-col-add-error";
  error.setAttribute("role", "alert");

  const acciones = document.createElement("div");
  acciones.className = "db-col-add-actions";
  const cancelar = document.createElement("button");
  cancelar.type = "button";
  cancelar.className = "btn";
  cancelar.textContent = "Cancelar";
  cancelar.addEventListener("click", () => cerrarPanelDeColumna({ foco: true }));
  const enviar = document.createElement("button");
  enviar.type = "submit";
  enviar.className = "btn btn-primary";
  enviar.textContent = "Crear propiedad";
  acciones.append(cancelar, enviar);

  form.append(titulo, filaNombre, picker, rotuloTipo, rejilla, cajaOpciones, campoDestinoWrap, error, acciones);

  let basesPrometidas = null;
  const cargarBases = () => {
    if (basesPrometidas) return basesPrometidas;
    basesPrometidas = (async () => {
      const bases = await basesParaRelacion(database);
      destino.textContent = "";
      if (!bases.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "No hay otra base en el vault";
        destino.appendChild(opt);
        enviar.disabled = true;
        error.textContent = "Una relación necesita una base a la que apuntar.";
        return;
      }
      bases.forEach((b) => {
        const opt = document.createElement("option");
        opt.value = String(b.id);
        opt.textContent = b.title || "Sin título";
        destino.appendChild(opt);
      });
      enviar.disabled = false;
    })().catch((e) => reportError("No se pudieron leer las bases", e));
    return basesPrometidas;
  };

  const elegirTipo = (tipo) => {
    tipoElegido = tipo;
    for (const [valor, b] of botonesTipo) {
      const activo = valor === tipo;
      b.classList.toggle("is-active", activo);
      b.setAttribute("aria-checked", String(activo));
      b.tabIndex = activo ? 0 : -1;
    }
    cajaOpciones.hidden = !(tipo === "select" || tipo === "multiselect");
    campoDestinoWrap.hidden = tipo !== "relation";
    if (tipo === "relation") {
      cargarBases();
    } else {
      enviar.disabled = false;
      error.textContent = "";
    }
    pintarIcono();
    colocarPanelFlotante(form, anchor);
  };
  // Flechas dentro de la rejilla de tipos: es un grupo de radios.
  rejilla.addEventListener("keydown", (e) => {
    const claves = [...botonesTipo.keys()];
    const i = claves.indexOf(tipoElegido);
    const paso = { ArrowRight: 1, ArrowDown: 2, ArrowLeft: -1, ArrowUp: -2 }[e.key];
    if (!paso) return;
    e.preventDefault();
    e.stopPropagation();
    const siguiente = claves[(i + paso + claves.length) % claves.length];
    elegirTipo(siguiente);
    botonesTipo.get(siguiente).focus();
  });

  // Dentro de una nota, las teclas del formulario no pueden subir al keymap de
  // CodeMirror (Enter insertaría un salto en el `.md` de la nota).
  form.addEventListener("keydown", (e) => e.stopPropagation());

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    const texto = nombre.value.trim();
    if (!texto) {
      error.textContent = "La propiedad necesita un nombre.";
      nombre.focus();
      return;
    }
    if (tipoElegido === "relation" && !destino.value) {
      error.textContent = "Elige la base a la que apunta la relación.";
      return;
    }
    // Una opción escrita y sin Intro también cuenta.
    const pendiente = nuevaOpcion.value.trim();
    if (pendiente && !opciones.some((v) => v.toLowerCase() === pendiente.toLowerCase())) opciones.push(pendiente);
    enviar.disabled = true;
    try {
      await crearColumnaEnLaBase(contexto, {
        nombre: texto,
        tipo: tipoElegido,
        destino: tipoElegido === "relation" ? destino.value : null,
        multiple: tipoElegido === "relation" ? multiple.checked : false,
        opciones: tipoElegido === "select" || tipoElegido === "multiselect" ? opciones : [],
        icono,
        posicion,
      });
      cerrarPanelDeColumna();
    } catch (err) {
      enviar.disabled = false;
      error.textContent = describeError(err);
    }
  });

  elegirTipo("text");
  abrirPanelFlotante(anchor, form, {
    etiqueta: "Nueva propiedad",
    foco: "input",
  });
}


/** El icono del tipo de una columna, como en Notion: delante del nombre en la
 * cabecera y en la cabecera de su menú. `title` es la columna del título. */
const ICONOS_DE_TIPO = {
  title: '<span class="db-type-aa" aria-hidden="true">Aa</span>',
  text: '<svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10"/></svg>',
  number: '<svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" aria-hidden="true"><path d="M5 9h14M5 15h14M10 4 8 20M16 4l-2 16"/></svg>',
  date: '<svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 2v4M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/></svg>',
  select: '<svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m8 11 4 4 4-4"/></svg>',
  multiselect: '<svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" aria-hidden="true"><path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/></svg>',
  checkbox: '<svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="18" x="3" y="3" rx="3"/><path d="m8 12 3 3 5-6"/></svg>',
  url: '<svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/></svg>',
  relation: '<svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17 17 7M8 7h9v9"/></svg>',
  person: '<svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
  parent_row: '<svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12h-8M21 6H8M21 18h-8M3 6v4c0 1.1.9 2 2 2h3M3 10v6c0 1.1.9 2 2 2h3"/></svg>',
};
function iconoDeTipo(tipo) {
  return `<span class="db-col-type">${ICONOS_DE_TIPO[tipo] || ICONOS_DE_TIPO.text}</span>`;
}

/** Un `<th>` de columna, como en Notion: la cabecera ENTERA es un botón (icono
 * del tipo + nombre) que abre el menú de la columna, anclado a su izquierda.
 * Al pasar el ratón se resalta la celda entera. Ordenar está dentro del menú
 * (ascendente, descendente, quitar); la flecha y la prioridad del orden se
 * siguen viendo junto al nombre, y `aria-sort` en el `<th>`.
 *
 * Sin `onChanged` (una tabla de solo lectura) el botón solo ordena, si hay
 * dónde guardar el orden. */
function cabeceraDeColumna(contexto, prop, nombre, paraOrdenar = prop) {
  const th = document.createElement("th");
  th.scope = "col";
  th.className = "db-col-head";
  // La clave de la columna en el DOM: de aquí cuelgan el asa del ancho y el
  // arrastre para reordenar (`views[].widths` / `views[].order`). Las columnas
  // de servicio llevan `@` delante para no chocar con una propiedad con esa
  // clave; `claveDeAncho` traduce `@title` a `title`.
  th.dataset.colKey = prop ? prop.key : "@title";
  // En el bloque incrustado, el widget del editor previene el `mousedown` del
  // bloque salvo en lo que lleve `data-db-action` (ver DatabaseBlockWidget).
  if (contexto.embed) th.dataset.dbAction = "columna";

  const claveOrden = paraOrdenar?.key || null;
  const orden = contexto.sort || [];
  const indice = claveOrden ? orden.findIndex((c) => c.key === claveOrden) : -1;
  const puedeOrdenar = Boolean(claveOrden && (contexto.onOrdenar || contexto.onToggleSort));
  if (puedeOrdenar) {
    th.setAttribute(
      "aria-sort",
      indice < 0 ? "none" : orden[indice].dir === "desc" ? "descending" : "ascending",
    );
  }

  const boton = document.createElement("button");
  boton.type = "button";
  boton.className = "db-col-head-btn" + (indice >= 0 ? " is-sorted" : "");
  boton.innerHTML = prop?.icon
    ? `<span class="db-col-type db-col-emoji" aria-hidden="true">${escapeHtml(prop.icon)}</span>`
    : iconoDeTipo(prop ? prop.type : "title");
  const etiqueta = document.createElement("span");
  etiqueta.className = "db-col-label";
  etiqueta.textContent = nombre;
  boton.appendChild(etiqueta);
  if (indice >= 0) {
    const flecha = document.createElement("span");
    flecha.className = "sort-arrow";
    flecha.setAttribute("aria-hidden", "true");
    flecha.textContent = orden[indice].dir === "desc" ? "↓" : "↑";
    boton.appendChild(flecha);
    if (orden.length > 1) {
      const prioridad = document.createElement("span");
      prioridad.className = "sort-priority";
      prioridad.textContent = String(indice + 1);
      prioridad.title = `Clave ${indice + 1} de ${orden.length}`;
      boton.appendChild(prioridad);
    }
  }
  if (contexto.embed) boton.dataset.dbAction = "columna";
  boton.addEventListener("keydown", (e) => {
    // En la tabla incrustada, el Enter que activa el botón no puede subir al
    // keymap de CodeMirror (insertaría un salto en el `.md` de la nota).
    if (e.key === "Enter" || e.key === " ") e.stopPropagation();
  });

  if (contexto.onChanged) {
    boton.setAttribute("aria-haspopup", "menu");
    boton.setAttribute("aria-expanded", "false");
    boton.title = `Opciones de «${nombre}»`;
    boton.addEventListener("click", (e) => {
      e.stopPropagation();
      abrirMenuDeColumna(boton, { ...contexto, th, prop, boton, claveOrden });
    });
  } else if (puedeOrdenar) {
    boton.title = indice >= 0 ? `Ordenado por ${nombre}: pulsa para invertir` : `Ordenar por ${nombre}`;
    boton.addEventListener("click", () => contexto.onToggleSort?.(claveOrden));
  } else {
    boton.disabled = true;
  }
  th.appendChild(boton);
  return th;
}

/** El menú de las columnas que la vista deja ocultas. Se construye entero cada
 * vez que se abre: los listeners van en los botones, no en un nodo clonado. */
function menuDeColumnasOcultas(contexto, ocultas) {
  const { database, view, onChanged } = contexto;
  const menu = document.createElement("div");
  menu.className = "db-col-menu";
  const cabecera = document.createElement("div");
  cabecera.className = "db-col-menu-label";
  cabecera.textContent = "Columnas ocultas en esta vista";
  menu.appendChild(cabecera);
  ocultas.forEach((p) => {
    menu.appendChild(
      itemDeColumna(p.name, {
        alElegir: () => {
          cerrarPanelDeColumna();
          mostrarColumna({ database, view, prop: p, onChanged }).catch((e) =>
            reportError("No se pudo volver a mostrar la columna", e),
          );
        },
      }),
    );
  });
  menu.appendChild(separadorDeMenu());
  menu.appendChild(
    itemDeColumna("Mostrar todas", {
      alElegir: () => {
        cerrarPanelDeColumna();
        mostrarTodasLasColumnas({ database, view, onChanged }).catch((e) =>
          reportError("No se pudieron mostrar las columnas", e),
        );
      },
    }),
  );
  return menu;
}

/** «N columnas ocultas en esta vista · Mostrar»: el camino de vuelta para una
 * columna oculta desde la cabecera (si no, ocultar sería una puerta de un solo
 * sentido). */
function barraDeColumnasOcultas(contexto, ocultas) {
  const barra = document.createElement("div");
  barra.className = "db-hidden-cols";
  const texto = document.createElement("span");
  texto.textContent =
    ocultas.length === 1
      ? "1 columna oculta en esta vista"
      : `${ocultas.length} columnas ocultas en esta vista`;
  const boton = document.createElement("button");
  boton.type = "button";
  boton.className = "db-hidden-cols-btn";
  boton.textContent = "Mostrar";
  boton.setAttribute("aria-haspopup", "menu");
  boton.setAttribute("aria-expanded", "false");
  boton.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") e.stopPropagation();
  });
  boton.addEventListener("click", (e) => {
    e.stopPropagation();
    abrirPanelFlotante(boton, menuDeColumnasOcultas(contexto, ocultas), {
      etiqueta: "Columnas ocultas en esta vista",
    });
  });
  barra.append(texto, boton);
  return barra;
}

function renderTable(
  database,
  rows,
  {
    deletable = true,
    hooks = null,
    sort = [],
    onToggleSort = null,
    vacio = null,
    view = null,
    onChanged = null,
    onRepintar = null,
    onCambioColumnas = null,
    onOrdenar = null,
    onAlta = null,
    barraEn = null,
  } = {},
) {
  // La selección múltiple envuelve la tabla (barra e informe encima), pero
  // `.table-wrap` sigue siendo el nodo que desplaza: quien pinte esto encuentra
  // la tabla donde estaba.
  const shell = document.createElement("div");
  shell.className = "db-sel-shell";
  const sel = crearSeleccionDeFilas({
    database,
    rows,
    puedeBorrar: deletable,
    hooks,
    onRepintar,
    anfitrion: barraEn,
    marco: shell,
  });

  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  // Qué columnas enseña ESTA vista, en su orden: `order` manda, `visible_properties`
  // quita y lo que no esté en `order` va detrás, en el orden del schema (así
  // añadir una propiedad no la esconde ni la manda al principio). Sin vista
  // («Todas») es el schema tal cual.
  const propiedades = columnasDeVista(database, view);
  // Las mismas sin quitar las ocultas: el orden que se guarda al arrastrar
  // tiene que decir también dónde estaban las que esa vista no enseña.
  const todasLasColumnas = columnasEnOrden(database, view);
  // Los anchos de ESTA vista (`views[].widths`), ya acotados y con la clave de
  // la propiedad (o `title` para la columna del título).
  const anchos = anchosDeVista(view);
  const contextoDeColumna = {
    database,
    view,
    onChanged,
    sort,
    onToggleSort,
    onOrdenar,
    onCambioColumnas,
    embed: Boolean(hooks?.embed),
  };

  const table = document.createElement("table");
  table.className = "rows-table";
  // Un `<col>` por columna: el ancho guardado se declara una vez y vale para la
  // cabecera y para las celdas de todas las filas, y es lo único que se toca
  // mientras se arrastra el asa (sin repintar la tabla en cada píxel).
  const colgroup = document.createElement("colgroup");
  const cols = new Map();

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  // La columna del título es una columna más: se puede renombrar
  // (`title_name`) y quitar (`title_hidden`). Si se quita, la tabla deja de
  // enseñarla; la fila se sigue llamando por su `id`, que es su fichero.
  const muestraTitulo = !database.title_hidden;
  const rotuloTitulo = database.title_name || "Título";
  const abrirHead = document.createElement("th");
  abrirHead.scope = "col";
  abrirHead.className = "row-open-head";
  // `@` delante: las columnas de servicio no pueden chocar con una propiedad del
  // schema. Con una propiedad llamada «Open» (clave `open`), las dos columnas
  // compartían clave, así que el ancho de una se pintaba en la otra, la
  // propiedad se quedaba sin asa y la suma de la tabla no cuadraba.
  // `claveDeAncho` traduce la clave del DOM a la del `.md` (`@title` ->
  // `title`); las de servicio no tienen y por eso no se redimensionan.
  abrirHead.dataset.colKey = "@open";
  const abrirSolo = document.createElement("span");
  abrirSolo.className = "sr-only";
  abrirSolo.textContent = "Abrir";
  abrirHead.appendChild(abrirSolo);
  // Con la columna del título a la vista, «Abrir» va DENTRO de su celda (como
  // en Notion) y esta columna sobra. Solo con el título oculto (`title_hidden`)
  // hace falta su propia columna.
  if (!muestraTitulo) headRow.appendChild(abrirHead);
  if (muestraTitulo) {
    // El título no ordena (el servidor no ordena por él: una flecha que no
    // hace nada sería mentir) pero sí tiene menú, para renombrarlo y quitarlo.
    headRow.appendChild(
      cabeceraDeColumna(contextoDeColumna, null, rotuloTitulo, {
        key: "title",
        name: rotuloTitulo,
      }),
    );
  }
  // Cada propiedad es una cabecera que ordena (`filtros-vista`) y, además, el
  // menú de la columna: el rótulo ordena y el botón de tres puntos abre el menú.
  propiedades.forEach((p) => headRow.appendChild(cabeceraDeColumna(contextoDeColumna, p, p.name)));
  // El «+» al final de la cabecera: una columna nueva sin salir de la tabla.
  // Solo con `onChanged` (hay un sitio donde repintar después de guardar).
  if (onChanged) {
    const addHead = document.createElement("th");
    addHead.scope = "col";
    addHead.className = "db-col-add-head";
    addHead.dataset.colKey = "@add";
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "db-col-add-btn";
    addBtn.innerHTML = svgIcon("i-plus");
    addBtn.title = "Añadir una columna";
    addBtn.setAttribute("aria-label", "Añadir una columna");
    addBtn.setAttribute("aria-haspopup", "dialog");
    if (hooks?.embed) addBtn.dataset.dbAction = "columna";
    addBtn.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") e.stopPropagation();
    });
    addBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      abrirFormularioDeColumna(addBtn, contextoDeColumna);
    });
    addHead.appendChild(addBtn);
    headRow.appendChild(addHead);
  }
  // La columna de la casilla, la PRIMERA (a la izquierda, como en Notion). Está
  // también en la tabla incrustada, donde marcar en bloque sí vale. Su cabecera
  // es la casilla de «seleccionar todas las visibles». Con ratón, las casillas
  // solo se ven en la fila por la que se pasa o cuando ya hay algo marcado.
  {
    const th = document.createElement("th");
    th.scope = "col";
    th.className = "row-sel-head";
    th.dataset.colKey = "@actions";
    const soloTexto = document.createElement("span");
    soloTexto.className = "sr-only";
    soloTexto.textContent = "Seleccionar";
    th.append(sel.casillaDeCabecera(), soloTexto);
    headRow.insertBefore(th, headRow.firstChild);
  }
  // El ancho y el orden viven en la vista del `.md`: sin vista activa no hay
  // dónde guardarlos, así que «Todas (sin vista)» se queda con el reparto del
  // navegador, el orden del schema y sin asas ni arrastre.
  headRow.querySelectorAll("th[data-col-key]").forEach((th) => {
    const clave = th.dataset.colKey;
    const col = document.createElement("col");
    col.dataset.colKey = clave;
    // Solo las columnas del schema (y el título) tienen ancho guardado: un
    // `widths` con una clave que no es de ninguna columna —o una de servicio—
    // no pinta nada.
    const anchoMd = claveDeAncho(clave);
    const ancho = anchoMd
      ? anchos.get(anchoMd) || anchoPorDefecto(anchoMd, propiedades)
      : anchoDeServicio(clave);
    col.style.width = `${ancho}px`;
    cols.set(clave, col);
    colgroup.appendChild(col);
  });
  // Siempre con anchos: la tabla mide lo que sus columnas (y el «+» queda
  // justo detrás de la última), no el ancho de la página.
  table.classList.add("has-col-widths");
  fijarAnchoDeTabla(table, cols);
  if (onCambioColumnas) {
    headRow.querySelectorAll("th[data-col-key]").forEach((th) => {
      const clave = claveDeAncho(th.dataset.colKey);
      // Las de servicio (abrir la fila, el «+» y acciones) no se redimensionan:
      // su ancho es parte del dibujo de la tabla, no un dato de la vista.
      if (!clave) return;
      const prop = propiedades.find((p) => p.key === clave);
      th.appendChild(
        asaDeAncho({
          clave,
          domKey: th.dataset.colKey,
          nombre: prop ? prop.name : rotuloTitulo,
          table,
          cols,
          anchos,
          onCambio: onCambioColumnas,
        }),
      );
    });
  }
  thead.appendChild(headRow);

  // Por qué una columna no está: con la lista de la vista en el `.md`, lo que
  // se ha ocultado tiene un camino de vuelta a la vista, no al diálogo.
  const ocultas = (database.schema_def || []).filter(
    (p) =>
      p &&
      p.type !== "parent_row" &&
      !propiedades.some((visible) => visible.key === p.key),
  );
  if (view && ocultas.length) wrap.appendChild(barraDeColumnasOcultas(contextoDeColumna, ocultas));

  const tbody = document.createElement("tbody");
  // Cómo repintar cada fila cuando la vista de fila guarda algo (se rellena
  // en el bucle): lo usa también la puerta de teclado del final.
  const refrescadores = new Map();
  // La línea de «no hay filas»: se aparta mientras hay una fila fantasma y
  // vuelve si la fantasma se abandona vacía.
  let filaVacia = null;
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    filaVacia = tr;
    const td = document.createElement("td");
    // Abrir + título + propiedades visibles + hueco del «+» (si hay dónde
    // repintar) + la columna de acciones, que ahora está siempre.
    td.colSpan =
      propiedades.length + (muestraTitulo ? 1 : 0) + 2 + (onChanged ? 1 : 0);
    // Un vacío dice qué hacer: con filtro, que es el filtro; sin él, dónde se
    // añade la primera fila (el alta está justo debajo de la tabla).
    td.className = "search-empty rows-empty";
    const tituloVacio = document.createElement("span");
    tituloVacio.className = "empty-hint-title";
    tituloVacio.textContent = vacio || "Esta base aún no tiene filas.";
    const notaVacio = document.createElement("span");
    notaVacio.className = "empty-hint-note";
    notaVacio.textContent = vacio
      ? "Cambia o quita una condición en «Filtrar» para ver más."
      : "Pulsa «Nueva fila», aquí debajo, y escribe su nombre.";
    td.append(tituloVacio, notaVacio);
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  // La jerarquía, calculada una vez para toda la tabla: el servidor ya manda
  // las hijas detrás de su madre, así que aquí solo falta cuánto indentar y el
  // contador de cada madre.
  const claveJerarquia = claveDeJerarquia(database);
  const profundidades = mapaDeProfundidades(rows, claveJerarquia);
  // Una vista agrupada pinta cada fila donde cae su grupo, no bajo su madre:
  // ahí la sangría mentiría sobre dónde está la jerarquía. El contador sí se
  // pinta igual (es un dato de la fila, no una posición).
  const indenta = Boolean(claveJerarquia) && !view?.config?.group_by;
  const pintarFila = (row) => {
    const tr = document.createElement("tr");
    // `data-row-id`: el «+ Nueva fila» de la vista deja el foco en el nombre de
    // la fila que acaba de crear (ver `enfocarFila`).
    tr.dataset.rowId = row.id;

    // 1) Abrir la fila como página. En su propia columna para que funcione
    // también con el título oculto (`title_hidden`), y fuera de la celda del
    // título: ahí el clic edita, y editar no puede tragarse la apertura.
    const openTd = document.createElement("td");
    openTd.className = "row-open-cell";
    openTd.dataset.colKey = "@open";
    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "row-open";
    openBtn.innerHTML = `${svgIcon("i-expand")}<span class="row-open-label">Abrir</span>`;
    // El teclado abre con Ctrl/Cmd+Enter desde la celda: así la tabla sigue
    // teniendo UNA sola parada de Tab (el recorrido de celdas es de cuadrícula)
    // en vez de una más por fila. Vale igual dentro de una nota: allí las
    // celdas también se editan, así que el recorrido es el mismo.
    openBtn.tabIndex = -1;
    // Dentro de una nota, el widget del editor se lleva el `mousedown` del
    // bloque; `data-db-action` es el permiso que deja pasar el clic (ver
    // DatabaseBlockWidget en entry.js). Fuera de una nota no molesta a nadie.
    openBtn.dataset.dbAction = "abrir-fila";
    const etiquetar = () => {
      const nombre = `Abrir «${row.title || "Sin título"}» como página (Ctrl+Enter)`;
      openBtn.title = nombre;
      openBtn.setAttribute("aria-label", nombre);
    };
    etiquetar();
    // Con el título oculto, el botón va en su propia columna; si no, dentro de
    // la celda del título (ver abajo).
    if (!muestraTitulo) {
      openTd.appendChild(openBtn);
      tr.appendChild(openTd);
    }

    // 2) Título y propiedades editables en el sitio: lo que se ve es el valor
    // del `.md`, y un clic abre el control del tipo de esa propiedad. Dentro de
    // una nota es lo mismo: se edita ahí y se guarda en el `.md` de la fila.
    const celdas = new Map(); // clave de la propiedad (null = título) -> <td>

    if (muestraTitulo) {
      const titleTd = document.createElement("td");
      titleTd.dataset.colKey = "@title";
      paintCell(titleTd, database, row, null, hooks);
      if (indenta) {
        const prof = profundidades.get(String(row.id)) || 0;
        if (prof) {
          // Hija: se sangra bajo su madre. La sangría es de la celda y no del
          // `tr` para que la casilla de selección siga en su columna.
          titleTd.classList.add("is-subtask");
          titleTd.style.paddingLeft = `${10 + 20 * prof}px`;
          tr.dataset.depth = String(prof);
        }
      }
      // El contador va FUERA de `indenta`: es un dato de la fila y se pinta
      // agrupe o no la vista. Lo que la agrupación desmiente es la sangría,
      // que es una posición (la fila cae en su grupo, no bajo su madre).
      const cuenta = contadorDeSubtareas(row);
      let badge = null;
      if (cuenta) {
        // El contador es un dato, no un control: no puede quitarle el clic a
        // la celda (que edita el título). El `title` dice qué cuenta.
        badge = document.createElement("span");
        badge.className = "cell-subtasks";
        badge.textContent = cuenta;
        badge.title = `${row.children.closed} de ${row.children.total} subtareas cerradas`;
      }
      // Lo que la celda del título lleva además del texto: el contador y el
      // botón «Abrir». `paintCell` rehace el contenido de la celda en cada
      // guardado, así que los vuelve a poner con esto (ver `adornarTitulo`).
      titleTd.adornarTitulo = () => {
        if (badge) titleTd.appendChild(badge);
        titleTd.appendChild(openBtn);
      };
      titleTd.adornarTitulo();
      celdas.set(null, titleTd);
      tr.appendChild(titleTd);
    }

    propiedades.forEach((p) => {
      const td = document.createElement("td");
      td.dataset.colKey = p.key;
      paintCell(td, database, row, p, hooks);
      celdas.set(p.key, td);
      tr.appendChild(td);
    });

    // 3) El hueco del «+» de la cabecera. El `<th>` del «+» va ANTES del de
    // «Acciones» y las filas también: sin esta celda la cabecera tendría una
    // celda más que cada fila, el «+» caería encima de la papelera de borrar
    // fila y sobraría una franja de cabecera a la derecha. Misma anchura que
    // el `<th>` (ver `.db-col-add-cell`).
    if (onChanged) {
      const addTd = document.createElement("td");
      addTd.className = "db-col-add-cell";
      tr.appendChild(addTd);
    }

    const propDe = (clave) =>
      clave === null ? null : database.schema_def.find((p) => p.key === clave) || null;

    /** El servidor acaba de aceptar un PATCH de esta fila desde la vista de
     * fila: se repinta la fila con `row`, que queda al día con el `.md`. Sin
     * repintar la tabla entera: eso perdería el foco y el testigo del recorrido
     * con teclado. */
    const refrescar = () => {
      etiquetar();
      const testigo = tr.querySelector('td[data-editable="1"][tabindex="0"]');
      for (const [clave, td] of celdas) {
        paintCell(td, database, row, propDe(clave), hooks);
      }
      if (testigo) setRovingCell(testigo);
    };

    refrescadores.set(String(row.id), refrescar);

    const abrir = () =>
      openRowPage(database, row, {
        origin: openBtn,
        rows,
        index: rows.indexOf(row),
        onSaved: refrescar,
      });

    // El clic del botón no puede llegar a la celda (abriría el editor).
    openBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      abrir();
    });

    // La casilla de la fila, en la primera columna. Dentro de una nota, `data-db-action` deja pasar el clic: sin él
    // el widget del editor se lo traga y la tabla se desmonta.
    const accTd = document.createElement("td");
    accTd.className = "row-sel-cell";
    accTd.dataset.colKey = "@actions";
    if (hooks?.embed) accTd.dataset.dbAction = "seleccionar-fila";
    const casilla = sel.casillaDeFila(row);
    accTd.appendChild(casilla);
    tr.insertBefore(accTd, tr.firstChild);
    sel.registrarFila(row, tr, casilla);

    // La fila no lleva botón de borrar propio: se marca y se borra desde la
    // barra de selección, que sale encima de la tabla (donde la vista deja).
    return tr;
  };
  rows.forEach((row) => tbody.appendChild(pintarFila(row)));

  // Ctrl/Cmd+Enter en cualquier celda de la fila abre la fila como página. En
  // captura: el `keydown` de la celda (Enter edita) no llega a ver la
  // combinación, que si no abriría el editor y la página a la vez.
  tbody.addEventListener(
    "keydown",
    (e) => {
      if (!(e.ctrlKey || e.metaKey) || e.key !== "Enter") return;
      const tr = e.target.closest?.("tr[data-row-id]");
      if (!tr) return;
      const fila = rows.find((r) => String(r.id) === tr.dataset.rowId);
      if (!fila) return;
      e.preventDefault();
      e.stopPropagation();
      openRowPage(database, fila, {
        origin: e.target.closest("td") || tr,
        rows,
        index: rows.indexOf(fila),
        onSaved: refrescadores.get(String(fila.id)) || null,
      });
    },
    true,
  );

  // Arrastrar una cabecera cambia el orden de las columnas de esta vista.
  if (onCambioColumnas && propiedades.length > 1) {
    instalarArrastreDeColumnas({
      wrap,
      headRow,
      columnas: propiedades,
      todas: todasLasColumnas,
      onCambio: onCambioColumnas,
    });
  }

  table.append(colgroup, thead, tbody);
  // Una sola parada de Tab en toda la tabla: la primera celda editable. Las
  // flechas mueven el foco (y el tabIndex=0) entre celdas -- ver paintCell.
  const firstCell = tbody.querySelector('td[data-editable="1"]');
  if (firstCell) setRovingCell(firstCell);

  // Teclado de la selección, en captura: llega antes que el `keydown` de la
  // celda (que se queda con Enter) y antes que el keymap de CodeMirror cuando
  // la tabla vive dentro de una nota.
  table.addEventListener("keydown", (e) => sel.teclas(e), true);
  // Las casillas nacen con lo que diga la selección; esto pinta además la
  // cabecera y el recuento.
  sel.sincronizar();

  wrap.appendChild(table);
  if (anchos.size) {
    // La tabla todavía no está en el documento y no se puede medir: se congela
    // en la microtarea que sigue, cuando el llamante ya la ha colgado y antes
    // de que se pinte nada (nada de parpadeo con el reparto de antes).
    queueMicrotask(() => congelarAnchos(table, cols, anchos));
  }
  shell.append(...(barraEn ? [] : [sel.barra]), sel.informes, wrap);

  // ------------------------------------------------ alta en el sitio
  // «+ Nueva fila» no abre un formulario: pinta una fila FANTASMA al final de
  // la tabla con el título en edición. La fantasma no existe en el `.md`: nace
  // (POST de siempre) al resolverse, con el título completo. Abandonada vacía,
  // se quita y no se ha creado nada (ver `armarCampoFantasma`).

  /** La línea de «no hay filas» vuelve si la tabla se queda sin filas y sin
   * fantasma abierta. */
  const reponerFilaVacia = () => {
    if (rows.length === 0 && filaVacia && tbody.isConnected && !tbody.querySelector("tr.row-ghost")) {
      tbody.appendChild(filaVacia);
    }
  };

  /** Añade al final una fila que ya existe en el servidor. */
  const colgarFila = (row, en = null) => {
    filaVacia?.remove();
    rows.push(row);
    const tr = pintarFila(row);
    if (en && en.parentNode === tbody) tbody.replaceChild(tr, en);
    else tbody.appendChild(tr);
    sel.fijarVisibles(rows);
    marcarComoNueva(tr);
    return tr;
  };

  /** Quita de la tabla una fila que el servidor ya no enseña en esta vista. */
  const descolgarFila = (row, tr) => {
    const i = rows.indexOf(row);
    if (i >= 0) rows.splice(i, 1);
    refrescadores.delete(String(row.id));
    tr.remove();
    sel.fijarVisibles(rows);
    reponerFilaVacia();
    const primera = tbody.querySelector('td[data-editable="1"]');
    if (primera && !tbody.querySelector('td[data-editable="1"][tabindex="0"]')) {
      setRovingCell(primera);
    }
  };

  /** La fila ya está en el `.md`: se le pregunta al servidor dónde cae en ESTA
   * vista (su filtro y su orden son suyos, ver `crearFilaEnVista`). Si la vista
   * no la enseña se quita y se avisa; si la ordena en otro sitio, se mueve ahí.
   * No se repinta la tabla: lo que el usuario esté haciendo se queda como está. */
  const asentar = async (row, tr) => {
    let filas;
    let total = null;
    try {
      filas = await api(`/databases/${database.id}/rows` + (view ? `?view=${view.id}` : ""));
      if (filtrosDeVista(view).length || (view && nivelDeVista(view, database) === "roots")) {
        total = (await api(`/databases/${database.id}/rows`)).length;
      }
    } catch (e) {
      reportError("No se pudo comprobar dónde cae la fila nueva", e);
      return { visible: true };
    }
    const visible = filas.some((r) => String(r.id) === String(row.id));
    if (tr.isConnected) {
      if (!visible) {
        descolgarFila(row, tr);
      } else {
        // Detrás de la que el servidor pone justo antes; si no está pintada
        // (otra ventana la acaba de crear), la fila se queda donde está.
        const i = filas.findIndex((r) => String(r.id) === String(row.id));
        const anterior = i > 0 ? String(filas[i - 1].id) : null;
        const trAnterior = anterior
          ? tbody.querySelector(`tr[data-row-id="${CSS.escape(anterior)}"]`)
          : null;
        const sitio = anterior ? trAnterior?.nextElementSibling : tbody.firstElementChild;
        const activo = document.activeElement;
        const editando = tr.querySelector("td[data-editable] input, td[data-editable] select");
        if ((trAnterior || !anterior) && sitio !== tr && !editando) {
          tbody.insertBefore(tr, sitio || null);
          // Mover un nodo le quita el foco: se le devuelve.
          if (activo && tr.contains(activo)) activo.focus({ preventScroll: true });
          const pos = filas.map((r) => String(r.id));
          rows.sort((a, b) => pos.indexOf(String(a.id)) - pos.indexOf(String(b.id)));
        }
      }
    }
    onAlta?.({ fila: row, visible, visibles: filas.length, total });
    return { visible };
  };

  /** Pinta la fila fantasma con el foco en el título. `extra` son propiedades
   * con las que nace la fila; `volverA`, a dónde va el foco si se abandona. */
  shell.nuevaFila = ({ extra = {}, volverA = null } = {}) => {
    const tr = document.createElement("tr");
    tr.className = "row-ghost";
    const abrirTd = document.createElement("td");
    abrirTd.className = "row-open-cell";
    const td = document.createElement("td");
    td.className = "cell-title row-ghost-title";
    // Con el título oculto (`title_hidden`) la fila sigue necesitando nombre:
    // el campo ocupa el ancho de las propiedades.
    if (!muestraTitulo) td.colSpan = Math.max(1, propiedades.length);
    // Dentro de una nota, sin esto el widget del editor se queda el clic.
    if (hooks?.embed) td.dataset.dbAction = "editar";
    const { input } = buildPropertyInput({ key: "title", name: "Título", type: "text" }, "");
    input.style.width = "100%";
    input.style.boxSizing = "border-box";
    input.placeholder = "Nombre de la fila";
    input.setAttribute("aria-label", "Título de la fila nueva");
    td.appendChild(input);
    const accTdFantasma = document.createElement("td");
    accTdFantasma.className = "row-sel-cell";
    tr.append(accTdFantasma);
    if (!muestraTitulo) tr.append(abrirTd);
    tr.append(td);
    if (muestraTitulo) propiedades.forEach(() => tr.appendChild(document.createElement("td")));
    if (onChanged) {
      const addTd = document.createElement("td");
      addTd.className = "db-col-add-cell";
      tr.appendChild(addTd);
    }

    filaVacia?.remove();
    tbody.appendChild(tr);

    const quitar = (foco) => {
      tr.remove();
      reponerFilaVacia();
      if (foco) volverA?.focus?.({ preventScroll: true });
    };
    armarCampoFantasma(input, {
      database,
      extra,
      alAbandonar: quitar,
      alCrearse: async (row, { foco, aLaSiguiente }) => {
        if (!tr.isConnected) {
          // La tabla se repintó mientras se guardaba: la fila está en el `.md`
          // y el repintado siguiente la enseña.
          onAlta?.({ fila: row, visible: true, visibles: null, total: null });
          return;
        }
        const real = colgarFila(row, tr);
        if (foco) {
          const celdas = celdasEditables(real);
          const destino = (aLaSiguiente && celdas[1]) || celdas[0];
          if (destino) enfocarCelda(destino);
        }
        await asentar(row, real);
      },
    });

    wrap.scrollLeft = 0;
    input.focus();
    tr.scrollIntoView?.({ block: "nearest" });
  };

  /** Crea una fila desde una plantilla de fila: nace ya rellena (los valores y
   * el cuerpo los pone el servidor, que valida la plantilla) y con el nombre de
   * la plantilla, listo para cambiarlo. Con cuerpo, se abre como página. */
  shell.filaDePlantilla = async (plantilla, { extra = {}, volverA = null } = {}) => {
    ultimoCambioPropio = { databaseId: String(database.id), ts: Date.now() };
    let row;
    try {
      row = await api(`/databases/${database.id}/rows`, {
        method: "POST",
        body: JSON.stringify({
          title: plantilla.name,
          template: plantilla.name,
          properties: extra,
        }),
      });
    } catch (e) {
      reportError("No se pudo crear la fila desde la plantilla", e);
      return;
    }
    const tr = colgarFila(row);
    const { visible } = await asentar(row, tr);
    const conCuerpo = Boolean((plantilla.body || "").trim());
    if (conCuerpo) {
      openRowPage(database, row, {
        origin: (visible && tr.querySelector(".row-open")) || volverA,
        rows,
        index: rows.indexOf(row),
        onSaved: refrescadores.get(String(row.id)) || null,
      });
      return;
    }
    if (!visible) return;
    // Sin cuerpo: la fila queda con el título en edición y seleccionado, para
    // escribir el suyo encima.
    const celda = celdasEditables(tr)[0];
    if (!celda) return;
    setRovingCell(celda);
    if (muestraTitulo) celda.click();
    else celda.focus();
  };

  return shell;
}

/** Deja el testigo del roving tabindex en una sola celda de la tabla: la
 * recibida se queda con tabIndex=0 y todas las demás con -1. Sin esta
 * normalización el testigo se duplica (dos paradas de Tab) o se pierde del
 * todo (la tabla deja de ser alcanzable por teclado) en cuanto una celda se
 * repinta fuera del orden en que se pintó la tabla. */
function setRovingCell(td) {
  const table = td.closest("table");
  if (!table) {
    td.tabIndex = 0;
    return;
  }
  table.querySelectorAll('td[data-editable="1"]').forEach((cell) => {
    cell.tabIndex = cell === td ? 0 : -1;
  });
  td.tabIndex = 0;
}

/** Repinta una celda y normaliza el roving tabindex: siempre queda exactamente
 * una celda con tabIndex=0, y esa es la repintada.
 *
 * `focus`: true devuelve el foco a la celda (Enter/Escape: el input que tenía
 * el foco desaparece al repintar), false no lo toca nunca (blur: el usuario se
 * ha ido a otro control y robarle el foco es perder lo que estaba haciendo).
 * Sin valor decide por el estado: lo recupera si el foco era de la propia
 * celda o se quedó huérfano en <body>. */
function repaintCell(td, database, row, prop, focus, hooks = null) {
  // Antes de repintar: el input de editCell() se destruye al reemplazar el
  // innerHTML, así que "el foco era de la celda" hay que mirarlo ahora.
  const ownedFocus = td.contains(document.activeElement);
  paintCell(td, database, row, prop, hooks);
  if (!td.isConnected) return;
  repintarFechasDeLaFila(td, database, row, hooks);
  setRovingCell(td);
  const shouldFocus = focus === undefined ? ownedFocus || document.activeElement === document.body : focus;
  if (shouldFocus) td.focus();
}

/** Repinta las celdas de fecha de la fila, menos la que se acaba de guardar.
 *
 * La marca de «fecha vencida» depende de si la **fila** está cerrada —una tarea
 * hecha tarde no está vencida—, así que guardar el estado cambia celdas que no
 * se han tocado. Sin esto, cerrar una tarea desde la tabla dejaba su fecha en
 * tono de aviso hasta el siguiente repintado de la vista: el servidor ya había
 * dicho que no estaba vencida y la fila decía que sí. Solo se hace en bases que
 * declaran regla de cierre (en las demás no hay marca que cambiar) y solo con
 * las propiedades de tipo `date` de esa fila. */
function repintarFechasDeLaFila(td, database, row, hooks = null) {
  if (!reglaDeCierre(database)) return;
  const tr = td?.closest("tr");
  if (!tr) return;
  for (const otra of tr.querySelectorAll("td[data-col-key]")) {
    if (otra === td) continue;
    const clave = otra.dataset.colKey;
    if (!clave || clave === "@title") continue;
    const prop = (database.schema_def || []).find((p) => p && p.key === clave);
    if (prop?.type !== "date") continue;
    paintCell(otra, database, row, prop, hooks);
  }
}

/** Pinta el valor de una celda y la deja lista para editarse con un clic.
 * `prop === null` es la celda del título. */
function paintCell(td, database, row, prop, hooks = null) {
  const esRelacion = prop?.type === "relation";
  const esPersona = prop?.type === "person";
  if (esRelacion) {
    pintarRelacionEnCelda(td, database, row, prop, hooks);
  } else if (esPersona) {
    pintarPersonaEnCelda(td, database, row, prop, hooks);
  } else {
    td.innerHTML = prop
      ? formatPropValueHtml(row.properties?.[prop.key], prop, row)
      : escapeHtml(row.title || "Sin título");
  }
  // Una fecha que ya pasó se marca con el tono de aviso del panel. Qué fechas
  // son lo dice el servidor en `overdue_keys`, que ya aplicó las dos reglas: la
  // base tiene que declarar regla de cierre (sin estado no hay «sin terminar») y
  // la fila no puede estar cerrada (una tarea hecha tarde no está vencida). Aquí
  // no se compara ninguna fecha con el reloj del navegador.
  const vencida = Boolean(
    prop?.type === "date" && (row.overdue_keys || []).includes(prop.key),
  );
  td.classList.toggle("cell-overdue", vencida);
  // Sin valor: la raya se pinta apagada (solo presentación, ver `.cell-empty`).
  const valorDeCelda = prop ? row.properties?.[prop.key] : row.title;
  td.classList.toggle(
    "cell-empty",
    Boolean(prop) &&
      prop.type !== "checkbox" &&
      (valorDeCelda === undefined ||
        valorDeCelda === null ||
        valorDeCelda === "" ||
        (Array.isArray(valorDeCelda) && valorDeCelda.length === 0)),
  );
  // La columna del título se lee como jerarquía (la regla CSS va por clase:
  // la primera columna de la tabla es la de abrir la fila).
  td.classList.toggle("cell-title", !prop);
  // El contador de subtareas y «Abrir» viven dentro de la celda del título:
  // el `innerHTML` de arriba se los ha llevado.
  if (!prop) td.adornarTitulo?.();
  // `cell-rel` deja que los títulos salten de línea: el `td` recorta a 240 px
  // y un id (que ya no se enseña) salía siempre en una sola línea.
  td.classList.toggle("cell-rel", Boolean(esRelacion));
  td.classList.toggle("cell-person", Boolean(esPersona));
  td.title = vencida
    ? "Fecha vencida (la fila sigue abierta) · clic para editar"
    : esRelacion
      ? "Clic para elegir filas · los títulos abren su fila · el lápiz edita"
      : esPersona
        ? "Clic para asignar una persona · el lápiz edita"
        : "Clic para editar";
  td.style.cursor = "pointer";
  td.dataset.editable = "1";
  // Dentro de una nota, el widget del editor se lleva el `mousedown` del bloque
  // (previene el default, mete el cursor en la valla y la tabla desaparece)
  // salvo en lo que lleve `data-db-action` -- ver DatabaseBlockWidget en
  // entry.js. Mismo marcador que el encabezado y el «+ Nueva fila».
  if (hooks?.embed) td.dataset.dbAction = "editar";
  // Roving tabindex: las celdas no son 72 paradas de Tab, solo la que está en
  // curso (renderTable deja tabIndex=0 en la primera). Enter abre la edición,
  // igual que el clic (Espacio marca la fila: ver `crearSeleccionDeFilas`); las
  // flechas y Tab mueven el foco como en una hoja.
  td.tabIndex = -1;
  td.onkeydown = (e) => {
    if (e.target !== td) return; // el <input>/<select> de editCell() manda
    // Dentro de una nota todo lo que no consumamos se lo quedaría el keymap de
    // CodeMirror (su Enter inserta un salto de línea en el `.md`): se corta en
    // la celda. En la página de la base no hay nada encima que lo quiera.
    const parar = () => e.stopPropagation();
    if (e.key === "Tab" && hooks?.embed) {
      // Tab / Shift+Tab van celda a celda y de fila en fila, pero SOLO en el
      // bloque incrustado. En la página de la base el recorrido es el del
      // navegador, que pasa por los botones «Borrar fila» de cada fila: aquí no
      // hay columna de acciones y saltárselos era una regresión. En los
      // extremos de la tabla no se consume, para poder salir con Tab.
      const destino = celdaVecina(td, e.shiftKey ? -1 : 1);
      if (!destino) return;
      e.preventDefault();
      parar();
      enfocarCelda(destino);
      return;
    }
    // Enter abre la edición; Espacio NO: con la selección múltiple marca la
    // fila (ver `crearSeleccionDeFilas`), como en un gestor de ficheros. El
    // `keydown` de la tabla llega antes (va en captura) y ya lo ha consumido.
    if (e.key === "Enter") {
      e.preventDefault();
      parar();
      td.click();
      return;
    }
    if (!["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"].includes(e.key)) return;
    const tr = td.parentElement;
    const tbody = tr.parentElement;
    const allRows = [...tbody.querySelectorAll("tr")];
    const rIdx = allRows.indexOf(tr);
    const cells = celdasEditables(tr);
    const cIdx = cells.indexOf(td);
    const editableIn = (rowIdx) => celdasEditables(allRows[rowIdx]);
    let target = null;
    if (e.key === "ArrowRight") target = cells[cIdx + 1] || editableIn(rIdx + 1)[0] || null;
    else if (e.key === "ArrowLeft") {
      const prev = editableIn(rIdx - 1);
      target = cells[cIdx - 1] || prev[prev.length - 1] || null;
    } else if (e.key === "ArrowDown") target = editableIn(rIdx + 1)[cIdx] || null;
    else if (e.key === "ArrowUp") target = editableIn(rIdx - 1)[cIdx] || null;
    if (!target) {
      if (hooks?.embed) parar(); // sin destino, la flecha no debe mover el cursor de la nota
      return;
    }
    e.preventDefault();
    parar();
    enfocarCelda(target);
  };
  td.onclick =
    prop && prop.type === "checkbox"
      ? () => toggleCheckbox(td, database, row, prop, hooks)
      : () => editCell(td, database, row, prop, hooks);
}

/** Las celdas editables de una fila, en orden. */
function celdasEditables(tr) {
  return tr ? [...tr.querySelectorAll('td[data-editable="1"]')] : [];
}

/** La celda que toca al pasar de una celda a otra con Tab (`paso` = +1 / -1),
 * en orden de lectura: al final de una fila, la primera de la siguiente.
 * `null` en los extremos, donde Tab sigue su camino de siempre (salir de la
 * tabla). */
function celdaVecina(td, paso) {
  const table = td.closest("table");
  if (!table) return null;
  const celdas = [...table.querySelectorAll('tbody td[data-editable="1"]')];
  const i = celdas.indexOf(td);
  return i === -1 ? null : celdas[i + paso] || null;
}

/** La celda de la misma columna en la fila siguiente: Enter guarda y baja, como
 * en una hoja de cálculo. Salta las filas sin celdas editables (la del «Sin
 * filas todavía»). */
function celdaDeAbajo(td) {
  const tr = td.parentElement;
  const tbody = tr?.parentElement;
  if (!tbody) return null;
  const filas = [...tbody.querySelectorAll("tr")];
  const col = celdasEditables(tr).indexOf(td);
  for (let i = filas.indexOf(tr) + 1; i < filas.length; i += 1) {
    const abajo = celdasEditables(filas[i])[col];
    if (abajo) return abajo;
  }
  return null;
}

/** Mueve el foco a otra celda de la tabla, con el testigo del roving tabindex
 * (una sola parada de Tab en la tabla, la de la celda en curso). */
function enfocarCelda(td) {
  if (!td) return false;
  setRovingCell(td);
  td.focus();
  return true;
}

/** Un booleano no tiene "modo edición": el clic lo cambia y se guarda. */
async function toggleCheckbox(td, database, row, prop, hooks = null) {
  const next = !row.properties?.[prop.key];
  try {
    await saveRow(database, row, { properties: { [prop.key]: next } }, hooks);
  } catch (e) {
    reportError("No se pudo guardar el cambio", e);
  }
  // Si falló, lo que se repinta es el valor del servidor.
  repaintCell(td, database, row, prop, undefined, hooks);
}

/** Edita una celda con el input de su tipo. Guarda al confirmar (Enter, Tab o
 * salir del campo) y, si el PATCH falla, repinta el valor viejo: la tabla nunca
 * enseña un valor que el `.md` no tiene.
 *
 * Teclado: Enter guarda y baja a la celda de la misma columna en la fila
 * siguiente; Tab / Shift+Tab guardan y pasan a la celda siguiente / anterior
 * (de la fila, o al final de la fila a la primera de la siguiente); Esc cancela
 * y deja la celda como estaba. Así se recorre una fila entera sin ratón.
 */
function editCell(td, database, row, prop, hooks = null) {
  if (td.querySelector("input, select, textarea")) return;
  // Una relación no se edita escribiendo un id: se elige la fila en un
  // selector con títulos (ver `editarCeldaDeRelacion`).
  if (prop?.type === "relation") {
    editarCeldaDeRelacion(td, database, row, prop, hooks).catch((e) =>
      reportError("No se pudo abrir el selector de filas", e),
    );
    return;
  }
  // Una persona tampoco se edita escribiendo: se elige de la lista (el valor es
  // el id de un usuario, y el `.md` no admite texto libre).
  if (prop?.type === "person") {
    editarCeldaDePersona(td, database, row, prop, hooks).catch((e) =>
      reportError("No se pudo abrir el selector de personas", e),
    );
    return;
  }
  const isTitle = !prop;
  const def = isTitle ? { key: "title", name: "Título", type: "text" } : prop;
  const { input, read } = buildPropertyInput(def, isTitle ? row.title : row.properties?.[def.key]);
  input.style.width = "100%";
  input.style.boxSizing = "border-box";
  let finished = false;
  // Con lo que se sembró el control. Si al salir sigue igual, no se ha tocado
  // y no hay nada que guardar: sin esta comparación, un control que no supo
  // representar el valor (`number` con "A-12") se lee como vacío y lo borra.
  const seeded = input.value;

  // Ver repaintCell(): repintar puede dejar el foco en <body> y con roving
  // tabindex hay que devolverlo a la celda para no perder la posición, pero
  // solo si el repintado lo provoca el propio control (Enter, cambio, Escape);
  // al salir con blur el foco es del control al que el usuario se ha ido.
  function repaint(focus) {
    repaintCell(td, database, row, prop, focus, hooks);
  }

  /** Repinta la celda y deja el foco donde toca: en `destino` si la tecla pide
   * moverse (Tab/Enter), en la propia celda si el guardado lo provocó el
   * usuario y no hay a dónde ir, y en ningún sitio si el foco se fue a otro
   * control (blur: robarlo es perder lo que estaba haciendo). El repintado se
   * hace sin foco propio y lo pone `enfocarCelda`/`td.focus` al final: así hay
   * una sola parada de Tab, la de la celda que se queda. */
  function terminar(focus, destino) {
    repaint(false);
    if (destino) enfocarCelda(destino);
    else if (focus) td.focus();
  }

  async function commit(focus, destino = null) {
    if (finished) return;
    finished = true;
    // Control apartado por un repintado (ver `celdaEnEdicion`): se está
    // destruyendo, no se guarda. Ni PATCH ni repintado -- la celda la vuelve a
    // abrir `reabrirCeldaEnEdicion`, y la fila de `filas` sigue siendo verdad.
    if (input.__descartado) return;
    if (input.value === seeded) {
      // Sin tocar el control no hay PATCH: el `.md` se queda como está.
      terminar(focus, destino);
      return;
    }
    let changes = null;
    if (isTitle) {
      const title = input.value.trim() || "Sin título";
      if (!sameValue(title, row.title)) changes = { title };
    } else {
      const value = read();
      if (!sameValue(value, row.properties?.[def.key])) changes = { properties: { [def.key]: value } };
    }
    if (changes) {
      try {
        // El título vive en el frontmatter: el `.md` no se mueve; saveRow trae el nuevo.
        await saveRow(database, row, changes, hooks);
      } catch (e) {
        reportError("No se pudo guardar el cambio", e);
      }
    }
    terminar(focus, destino);
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      // Enter guarda y baja: la tabla sigue siendo navegable sin ratón.
      commit(true, celdaDeAbajo(td));
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      finished = true; // se sale sin guardar: la celda queda como estaba
      repaint(true);
      return;
    }
    // Tab / Shift+Tab guardan y pasan a la celda siguiente/anterior, solo en el
    // bloque incrustado (mismo motivo que en `paintCell`: en la página de la
    // base el Tab del navegador conserva su recorrido, que incluye «Borrar
    // fila»). En los extremos de la tabla no se consume: Tab sale y el blur
    // guarda lo escrito.
    if (e.key !== "Tab" || !hooks?.embed) return;
    const destino = celdaVecina(td, e.shiftKey ? -1 : 1);
    if (!destino) return;
    e.preventDefault();
    e.stopPropagation();
    commit(true, destino);
  });
  input.addEventListener("blur", () => commit(false));
  if (input.tagName === "SELECT") input.addEventListener("change", () => commit(true));

  td.textContent = "";
  td.appendChild(input);
  input.focus?.();
  if (input.select) input.select();
}

/** Tarjeta que se está arrastrando. `dataTransfer` no siempre está (y para
 * nada hace falta: el dato viaja en esta variable); en un navegador real los
 * eventos lo traen y además se rellena por si lo lee alguien más. */
let boardDrag = null;

/** Columna que se está arrastrando para recolocarla. Es un arrastre distinto
 * del de las tarjetas: `boardDrag` mueve una fila a otra columna, este mueve la
 * columna de sitio (y lo que cambia es el `sort_order` de la propiedad). */
let boardColumnDrag = null;

/** Quita las marcas de un arrastre de columnas que se canceló (Esc). */
function limpiarArrastreDeColumnas() {
  document
    .querySelectorAll(".board-col.is-col-dragover, .board-col.is-col-dragging")
    .forEach((c) => c.classList.remove("is-col-dragover", "is-col-dragging"));
}

/** `lista` con `movido` en el sitio de `destino`.
 *
 * Se mueve el valor, no se intercambian dos: lo que el usuario ve al soltar es
 * que su columna cae donde apuntaba y las demás se corren un puesto. Si va
 * hacia la derecha cae DESPUÉS de la de destino y si va hacia la izquierda,
 * antes — así arrastrar un puesto a la derecha mueve de verdad en vez de
 * dejarla donde estaba.
 */
function moverEnLista(lista, movido, destino) {
  const desde = lista.findIndex((v) => mismoValorDeColumna(v, movido));
  const hasta = lista.findIndex((v) => mismoValorDeColumna(v, destino));
  if (desde < 0 || hasta < 0 || desde === hasta) return [...lista];
  const resto = lista.filter((_, i) => i !== desde);
  const antes = lista.slice(0, hasta).filter((_, i) => i !== desde).length;
  const posicion = antes + (desde < hasta ? 1 : 0);
  return [...resto.slice(0, posicion), lista[desde], ...resto.slice(posicion)];
}

/** En qué modo ordena una selección: `manual` (el orden declarado, que se
 * arrastra) o `auto` (alfabético natural). Es la misma regla que el backend
 * (`app.properties.sort_mode`): sin `sort_mode`, una propiedad que declara
 * `sort_order` está en manual y una que no declara nada en automático. */
function modoDeOrden(prop) {
  const modo = String(prop?.sort_mode || "").trim().toLowerCase();
  if (modo === "manual" || modo === "auto") return modo;
  return (prop?.sort_order || []).length ? "manual" : "auto";
}

/** Guarda el orden de las columnas en la PROPIEDAD (`sort_mode: manual` y
 * `sort_order`), no en la vista: es el mismo PATCH del `schema_def` que usa el
 * menú de una columna, con la lista limpia de valores vacíos («Sin valor» no es
 * un valor de la propiedad). El `.md` de la base es la verdad, así que recargar
 * lo mantiene.
 *
 * Si la propiedad estaba en **automático**, arrastrar una columna la pasa a
 * manual con el orden que el usuario acaba de poner, y se le dice: un arrastre
 * que no hace nada (porque el modo sigue ordenando por alfabeto) sería peor.
 */
async function guardarOrdenDeColumnas(database, groupKey, orden, refresh) {
  const limpio = (orden || []).filter(
    (v) => v !== null && v !== undefined && String(v).trim() !== "",
  );
  const prop = (database.schema_def || []).find((p) => p.key === groupKey);
  const eraAuto = modoDeOrden(prop) === "auto";
  try {
    await guardarEsquemaDeColumnas(
      database,
      esquemaConPropiedad(database, groupKey, {
        sort_mode: "manual",
        sort_order: limpio,
      }),
    );
    refresh?.();
    if (eraAuto) {
      showNotice(
        `«${prop?.name || "Esta propiedad"}» pasa a orden manual: se guarda el orden de columnas que acabas de poner.`,
      );
    }
  } catch (e) {
    reportError("No se pudo guardar el orden de las columnas", e);
  }
}

function renderBoard(database, rows, view, refresh) {
  const groupKey = view.config.group_by;
  const prop = database.schema_def.find((p) => p.key === groupKey);

  const board = document.createElement("div");
  board.className = "board";
  // Las filas que se están pintando, por id: una tarjeta dice de qué fila es
  // subtarea sin pedir nada al servidor (su madre puede estar en otra columna).
  const porId = new Map(rows.map((r) => [String(r.id), r]));

  // Una relación agrupa por el TÍTULO de la fila destino: una columna por
  // título, no por id (un id no se lee). Solo vale con una relación simple;
  // con `multiple` una fila caería en varias columnas y arrastrarla escribiría
  // un id suelto encima de la lista, así que se dice en vez de romper el dato.
  // Una persona agrupa por su NOMBRE visible, como una relación por el título:
  // una columna por persona, y el valor que se escribe al soltar una tarjeta es
  // el id (lo que guarda el `.md`).
  if (prop?.type === "person") {
    const etiquetas = new Map();
    rows.forEach((r) => {
      const id = personaIdDeFila(r, groupKey);
      if (id && !etiquetas.has(id)) {
        etiquetas.set(id, nombreDePersona(id, r, groupKey) || "(ya no está)");
      }
    });
    const grupos = new Map();
    const sueltas = [];
    rows.forEach((r) => {
      const id = personaIdDeFila(r, groupKey);
      if (!id) {
        sueltas.push(r);
        return;
      }
      if (!grupos.has(id)) grupos.set(id, []);
      grupos.get(id).push(r);
    });
    grupos.forEach((filas, id) =>
      board.appendChild(
        renderBoardColumn(database, etiquetas.get(id) || "(ya no está)", filas, {
          groupKey,
          refresh,
          value: id,
        }),
      ),
    );
    if (sueltas.length) {
      board.appendChild(
        renderBoardColumn(database, "Sin valor", sueltas, { groupKey, refresh, value: null }),
      );
    }
    return board;
  }

  if (prop?.type === "relation") {
    if (relacionMultiple(prop)) {
      return avisoDeVista(
        "Esta vista agrupa por una relación que enlaza varias filas.",
        "Una fila con varias filas enlazadas no cae en una sola columna. Cambia la agrupación a una relación simple.",
      );
    }
    const etiquetas = new Map();
    rows.forEach((r) => {
      refsDeRelacion(r, groupKey).forEach((ref, id) => {
        if (ref?.title && !etiquetas.has(id)) etiquetas.set(id, ref.title);
      });
    });
    const grupos = new Map();
    const sueltas = [];
    rows.forEach((r) => {
      const id = idsDeRelacion(r.properties?.[groupKey])[0] || null;
      if (!id) {
        sueltas.push(r);
        return;
      }
      if (!grupos.has(id)) grupos.set(id, []);
      grupos.get(id).push(r);
    });
    grupos.forEach((filas, id) =>
      board.appendChild(
        renderBoardColumn(database, etiquetas.get(id) || "(borrada)", filas, {
          groupKey,
          refresh,
          porId,
          // El valor que se escribe al soltar una tarjeta es el ID (lo que
          // guarda el `.md`); lo que se lee en la columna es el título.
          value: id,
        }),
      ),
    );
    if (sueltas.length) {
      board.appendChild(
        renderBoardColumn(database, "Sin valor", sueltas, { groupKey, refresh, porId, value: null }),
      );
    }
    return board;
  }

  // Las columnas, en el orden que declara la propiedad (`sort_order` en el
  // `schema` de la base): `[alta, media, baja]` se lee por importancia y no por
  // alfabeto, y el tablero es donde eso se ve. Sin orden declarado, el de
  // `options` (el de siempre) y después los valores que solo están en filas.
  const modo = modoDeOrden(prop);
  const columnas = columnasDelTablero(prop, rows, groupKey, modo);
  // Solo los valores de la propiedad: recolocar escribe el orden de esos (un
  // valor que solo está en una fila no pasa a estar declarado por arrastrar).
  const valoresDeColumnas = columnas.filter((c) => c.declarada).map((c) => c.valor);
  // Solo una selección puede declarar el orden de sus valores, así que solo
  // ahí la cabecera de la columna se arrastra: el orden se guarda en la
  // PROPIEDAD (no en la vista) para que lo vean todas las vistas del tablero.
  const reordenable = prop?.type === "select" || prop?.type === "multiselect";

  // Las filas se agrupan por la MISMA clave normalizada con la que se
  // construyen las columnas (sin mayúsculas ni espacios). Antes se hacía con
  // igualdad exacta: un `sort_order: [Baja, ...]` escrito a mano dejaba todas
  // las columnas vacías y las filas en «Sin valor», mientras la tabla de la
  // misma base ordenaba bien.
  const grouped = new Map(columnas.map((c) => [claveDeColumna(c.valor), []]));
  const other = [];
  rows.forEach((r) => {
    const clave = claveDeColumna(r.properties?.[groupKey]);
    if (clave && grouped.has(clave)) grouped.get(clave).push(r);
    else other.push(r);
  });

  const options = { groupKey, refresh, porId };
  columnas.forEach((columna) =>
    board.appendChild(
      renderBoardColumn(database, columna.valor, grouped.get(claveDeColumna(columna.valor)) || [], {
        ...options,
        soltar: columna.soltar,
        // Solo las columnas de un valor de la propiedad se recolocan: un valor
        // que únicamente está en una fila va al final por la regla de siempre,
        // así que su columna no tiene sitio que guardar.
        columna:
          reordenable && columna.declarada
            ? { valor: columna.valor, valores: valoresDeColumnas }
            : null,
      }),
    ),
  );
  // "Sin valor" también es destino: soltar ahí vacía la propiedad, que es lo
  // mismo que sacar la tarjeta de cualquier columna.
  if (other.length) {
    board.appendChild(renderBoardColumn(database, "Sin valor", other, { ...options, value: null }));
  }

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

/** Las columnas de un tablero, en orden: `[{ valor, soltar, declarada }]`.
 *
 * Primero los valores que declara `sort_order` (el orden semántico: alta,
 * media, baja), después los de `options` que no estén declarados y al final los
 * que solo existen en las filas (escritos a mano en un `.md`, o de una opción
 * borrada). Un valor sin sitio va al final y no al principio: es la misma regla
 * que la ordenación de la tabla, y por lo mismo — lo que apareció después no es
 * «lo más importante».
 *
 * `soltar` dice si una tarjeta puede caer en esa columna. Con `options`
 * declaradas el servidor rechaza un valor que no esté en la lista (400), así
 * que una columna de un valor suelto no es un destino: se pinta para que la
 * fila se pueda leer, no para soltarle cosas encima.
 *
 * `declarada` dice si el valor es de la propiedad (`options`/`sort_order`) o
 * solo aparece en una fila. Recolocar las columnas guarda el orden de las
 * declaradas: un valor que nadie declaró no se queda escrito en el `schema` por
 * haber arrastrado otra columna (seguirá al final por la regla de siempre).
 */
function columnasDelTablero(prop, rows, groupKey, modo = modoDeOrden(prop)) {
  const opciones = new Set((prop?.options || []).map((v) => String(v).trim().toLowerCase()));
  const conOpciones = opciones.size > 0;
  const vistas = new Set();
  const columnas = [];
  const anadir = (valor, declarada) => {
    // Una multiselección guarda una lista: no es un valor de columna.
    if (typeof valor !== "string" && typeof valor !== "number") return;
    const texto = String(valor);
    const clave = texto.trim().toLowerCase();
    if (!clave || vistas.has(clave)) return;
    vistas.add(clave);
    columnas.push({
      valor: texto,
      soltar: !conOpciones || opciones.has(clave),
      declarada,
    });
  };
  // En manual manda el orden declarado; en automático, el de `options` (el de
  // creación), que es lo de siempre. Después, los valores que solo están en
  // las filas.
  if (modo === "manual") (prop?.sort_order || []).forEach((v) => anadir(v, true));
  (prop?.options || []).forEach((v) => anadir(v, true));
  uniqueValues(rows, groupKey).forEach((v) => anadir(v, false));
  return columnas;
}

/** La clave con la que se comparan una columna y el valor de una fila: sin
 * mayúsculas ni espacios, como `option_key` en el backend. Una lista (una
 * multiselección) no es un valor de columna: devuelve `""`. */
function claveDeColumna(valor) {
  if (typeof valor !== "string" && typeof valor !== "number") return "";
  return String(valor).trim().toLowerCase();
}

/** Dos valores de columna son el mismo si lo son sin mayúsculas ni espacios:
 * el `.md` lo escribe una persona y `Alta` y `alta` son la misma opción. */
function mismoValorDeColumna(a, b) {
  return String(a ?? "").trim().toLowerCase() === String(b ?? "").trim().toLowerCase();
}

function renderBoardColumn(
  database,
  label,
  rows,
  { groupKey, refresh, value = label, porId = null, soltar = true, columna = null },
) {
  const col = document.createElement("div");
  col.className = "board-col";
  col.dataset.columna = String(label);

  const head = document.createElement("div");
  head.className = "board-col-head";
  const labelSpan = document.createElement("span");
  labelSpan.className = "board-col-label";
  labelSpan.textContent = String(label);
  // El punto de la cabecera lleva el mismo tono que la etiqueta de ese valor
  // en la tabla (ver `htmlDeEtiqueta`). Solo presentación: sin opción que
  // coincida («(sin valor)», un texto libre) se queda en el tono neutro.
  const propDeGrupo = groupKey ? database.schema_def.find((p) => p.key === groupKey) : null;
  const posicionDeTono = (propDeGrupo?.options || []).indexOf(value);
  if (posicionDeTono >= 0) {
    labelSpan.classList.add(`prop-tag-${(posicionDeTono % TONOS_DE_ETIQUETA) + 1}`);
  }
  const countSpan = document.createElement("span");
  countSpan.className = "board-col-count";
  countSpan.textContent = String(rows.length);
  head.append(labelSpan, countSpan);
  col.appendChild(head);

  // El asa de la cabecera recoloca la columna. El orden se guarda en el
  // `schema` de la base (`sort_order`), así que sobrevive a recargar y lo ven
  // todas las vistas de este tablero: no es un orden de la vista.
  if (columna && groupKey) {
    const asa = document.createElement("span");
    asa.className = "board-col-grip";
    asa.textContent = "⠿";
    asa.title = "Arrastra para recolocar la columna";
    asa.setAttribute("aria-hidden", "true");
    head.prepend(asa);
    head.classList.add("is-reorderable");
    head.draggable = true;
    head.addEventListener("dragstart", (e) => {
      boardColumnDrag = {
        databaseId: database.id,
        valor: columna.valor,
        valores: columna.valores,
      };
      col.classList.add("is-col-dragging");
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        try {
          e.dataTransfer.setData("text/plain", String(columna.valor));
        } catch {
          // algunos navegadores no dejan tocar dataTransfer en dragstart
        }
      }
    });
    head.addEventListener("dragend", () => {
      boardColumnDrag = null;
      limpiarArrastreDeColumnas();
    });
    col.addEventListener("dragover", (e) => {
      if (!boardColumnDrag || boardColumnDrag.databaseId !== database.id) return;
      e.preventDefault();
      col.classList.add("is-col-dragover");
    });
    col.addEventListener("dragleave", (e) => {
      if (e.relatedTarget && col.contains(e.relatedTarget)) return;
      col.classList.remove("is-col-dragover");
    });
    col.addEventListener("drop", (e) => {
      const drag = boardColumnDrag;
      if (!drag || drag.databaseId !== database.id) return;
      e.preventDefault();
      boardColumnDrag = null;
      limpiarArrastreDeColumnas();
      if (mismoValorDeColumna(drag.valor, columna.valor)) return;
      guardarOrdenDeColumnas(
        database,
        groupKey,
        moverEnLista(drag.valores, drag.valor, columna.valor),
        refresh,
      );
    });
  }

  // Sin `group_by` no hay arrastre: sin clave no se escribe nada, así que la
  // columna no acepta drops.
  if (groupKey && soltar) {
    col.addEventListener("dragover", (e) => {
      if (!boardDrag || boardDrag.databaseId !== database.id) return;
      e.preventDefault();
      // Marca el destino para que se vea dónde va a caer la tarjeta.
      col.classList.add("is-dragover");
    });
    col.addEventListener("dragleave", (e) => {
      // Al pasar de la columna a una tarjeta hija también salta dragleave:
      // solo se limpia si el puntero sale de la columna de verdad.
      if (e.relatedTarget && col.contains(e.relatedTarget)) return;
      col.classList.remove("is-dragover");
    });
    col.addEventListener("drop", (e) => {
      e.preventDefault();
      col.classList.remove("is-dragover");
      const drag = boardDrag;
      boardDrag = null;
      if (!drag || drag.databaseId !== database.id) return;
      moveCard(drag.row, value, database, groupKey, refresh);
    });
  }

  rows.forEach((row) => {
    const card = document.createElement("div");
    card.className = "board-card";
    card.dataset.rowId = row.id;

    const t = document.createElement("div");
    t.className = "board-card-title";
    t.textContent = row.title;
    card.appendChild(t);

    // La tarjeta de una subtarea dice de quién es: en el tablero la columna es
    // la agrupación de la vista, así que la madre puede estar en otra columna y
    // sin esta línea la jerarquía no se leería.
    const madre = row.parent_id ? porId?.get(String(row.parent_id)) : null;
    if (madre) {
      const sub = document.createElement("div");
      sub.className = "board-card-parent";
      sub.textContent = `↳ ${madre.title || "Sin título"}`;
      card.appendChild(sub);
    }

    Object.entries(row.properties || {}).forEach(([k, v]) => {
      const propDef = database.schema_def.find((p) => p.key === k);
      if (!propDef || propDef.type === "parent_row") return;
      if (v === null || v === "" || v === undefined) return;
      const line = document.createElement("div");
      // La propiedad por la que agrupa el tablero ya la dice la columna: en la
      // tarjeta se marca para que el CSS no la repita.
      line.className = "board-card-prop" + (k === groupKey ? " is-group-key" : "");
      line.innerHTML =
        `<span class="board-card-key">${escapeHtml(propDef.name)}</span>` +
        `<span class="sr-only">: </span>` +
        `<span class="board-card-val">${formatPropValueHtml(v, propDef, row)}</span>`;
      card.appendChild(line);
    });

    const cuenta = contadorDeSubtareas(row);
    if (cuenta) {
      const badge = document.createElement("div");
      badge.className = "board-card-subtasks";
      badge.textContent = cuenta;
      badge.title = `${row.children.closed} de ${row.children.total} subtareas cerradas`;
      card.appendChild(badge);
    }

    if (groupKey) {
      card.draggable = true;
      card.addEventListener("dragstart", (e) => {
        boardDrag = { databaseId: database.id, row };
        card.classList.add("is-dragging");
        if (e.dataTransfer) {
          e.dataTransfer.effectAllowed = "move";
          try {
            e.dataTransfer.setData("text/plain", row.id);
          } catch {
            // algunos navegadores no dejan tocar dataTransfer en dragstart
          }
        }
      });
      card.addEventListener("dragend", () => {
        boardDrag = null;
        card.classList.remove("is-dragging");
        // Si el arrastre se cancela (Esc, fuera de la ventana) el drop nunca
        // llega: se limpian aquí los carriles que se quedaron marcados.
        document.querySelectorAll(".board-col.is-dragover").forEach((c) => c.classList.remove("is-dragover"));
      });
    }
    // La tarjeta abre su fila como página, con el cuerpo y las propiedades.
    // El tablero no es una secuencia (no hay «fila siguiente»): se cierra y se
    // repinta con lo que se haya guardado.
    card.addEventListener("click", () =>
      openRowPage(database, row, { origin: card, onClose: () => refresh?.() }),
    );
    // Con teclado, Enter/Espacio abren la misma fila que el clic.
    makeKeyboardActivatable(card, "button", 0);

    col.appendChild(card);
  });

  // «+ Nueva fila» de la columna: la fila nace con el valor de ESTA columna en
  // la propiedad que agrupa. Se escribe en una tarjeta fantasma, igual que en
  // la tabla: nada existe hasta que se resuelve con texto. Donde soltar una tarjeta no
  // está permitido (una relación múltiple) tampoco se crea con valor: solo la
  // columna «Sin valor» lo ofrece.
  if (soltar || value === null) {
    const extra =
      groupKey && soltar && value !== null && value !== undefined ? { [groupKey]: value } : {};
    const alta = botonDeAlta({
      database,
      alCrear: (btn) => {
        const card = document.createElement("div");
        card.className = "board-card board-card-ghost";
        const input = document.createElement("input");
        input.type = "text";
        input.className = "board-card-input";
        input.placeholder = "Nombre de la fila";
        input.setAttribute("aria-label", `Título de la fila nueva en «${label}»`);
        card.appendChild(input);
        col.insertBefore(card, alta);
        armarCampoFantasma(input, {
          database,
          extra,
          alAbandonar: (foco) => {
            card.remove();
            if (foco) btn.focus({ preventScroll: true });
          },
          // El tablero se repinta con lo que diga el servidor: la tarjeta cae
          // en su columna y en su orden (o no sale, si el filtro la deja fuera).
          alCrearse: async (row) => {
            await refresh?.();
            avisarAltaEnTablero(database, row);
          },
        });
        input.focus();
      },
      alElegirPlantilla: async (plantilla, btn) => {
        let row;
        try {
          row = await api(`/databases/${database.id}/rows`, {
            method: "POST",
            body: JSON.stringify({
              title: plantilla.name,
              template: plantilla.name,
              properties: extra,
            }),
          });
        } catch (e) {
          reportError("No se pudo crear la fila desde la plantilla", e);
          return;
        }
        if ((plantilla.body || "").trim()) {
          openRowPage(database, row, { origin: btn, onClose: () => refresh?.() });
          return;
        }
        await refresh?.();
        avisarAltaEnTablero(database, row);
      },
    });
    alta.classList.add("board-col-add");
    col.appendChild(alta);
  }

  return col;
}

/** Tras un alta desde el tablero (ya repintado): la tarjeta nueva entra
 * marcada y con el foco; si no está, es que el filtro de la vista la deja
 * fuera, y se dice (misma frase que en la tabla). */
function avisarAltaEnTablero(database, row) {
  const card = document.querySelector(`#main .board-card[data-row-id="${CSS.escape(String(row.id))}"]`);
  if (card) {
    marcarComoNueva(card);
    card.focus({ preventScroll: true });
    card.scrollIntoView?.({ block: "nearest", inline: "nearest" });
    return;
  }
  const pestana = document.querySelector("#main .view-tab.active");
  const vista = (pestana?.textContent || "").replace(/\s*\([^)]*\)\s*$/, "");
  showNotice(avisoDeFilaOculta(row.title || "Sin título", vista));
}

/** Mover una tarjeta = escribir la propiedad de agrupación de la vista con el
 * valor de la columna destino, por el mismo PATCH que usa la tabla. */
async function moveCard(row, value, database, groupKey, refresh) {
  if (sameValue(value, row.properties?.[groupKey] ?? null)) return; // misma columna
  try {
    await saveRow(database, row, { properties: { [groupKey]: value } });
    refresh?.(); // el tablero se repinta con lo que hay en el servidor
  } catch (e) {
    // La tarjeta no se ha movido: el valor del servidor sigue intacto.
    reportError("No se pudo mover la tarjeta", e);
  }
}

// ------------------------------------------------------------- calendario
/** La clave `YYYY-MM-DD` de un valor de una propiedad fecha, leída del texto y
 * no de `new Date(valor)`.
 *
 * `new Date("2026-11-10")` se interpreta como medianoche UTC: en cualquier huso
 * al oeste de Greenwich cae en el 9 de noviembre y la fila aparece un día
 * antes. Una fecha del vault es una fecha civil —el día que se escribió, sin
 * hora ni huso— y por eso se compara como texto. Un valor con hora
 * (`2026-11-10T23:00`) se queda con el día que lleva escrito.
 */
function claveDeFecha(value) {
  if (value === null || value === undefined) return null;
  const m = String(value).trim().match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return null;
  // Un `YYYY-MM-DD` con mes o día imposibles (`2026-11-31`, `2026-13-01`) no
  // es una fecha: aceptarlo lo mandaba a una celda que no existe y la fila
  // desaparecía del calendario sin contarse en ningún aviso. Se valida contra
  // el mes real y sin `Date`, que además desplaza los años de cuatro cifras
  // pequeñas (año 99 -> 1999).
  const anio = Number(m[1]);
  const mes = Number(m[2]);
  const dia = Number(m[3]);
  if (mes < 1 || mes > 12) return null;
  const bisiesto = (anio % 4 === 0 && anio % 100 !== 0) || anio % 400 === 0;
  const diasDelMes = [31, bisiesto ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (dia < 1 || dia > diasDelMes[mes - 1]) return null;
  return `${m[1]}-${m[2]}-${m[3]}`;
}

/** El día de hoy en el huso del navegador, con la misma forma que
 * `claveDeFecha` (`toISOString()` daría el día en UTC, que no es el del
 * calendario que está mirando el usuario). */
function claveDeHoy() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

const MESES = [
  "enero", "febrero", "marzo", "abril", "mayo", "junio",
  "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
];
// Semana que empieza en lunes, como los calendarios de aquí.
const DIAS_SEMANA = ["L", "M", "X", "J", "V", "S", "D"];
// Para la etiqueta de una barra: «10 nov – 20 nov» cabe donde «10 de
// noviembre» no.
const MESES_CORTOS = [
  "ene", "feb", "mar", "abr", "may", "jun",
  "jul", "ago", "sep", "oct", "nov", "dic",
];

// --- fechas civiles, sin husos ni `Date` --------------------------------
//
// Una fecha del vault es un día civil. `Date` no sirve para hacer cuentas con
// ella: `new Date("2026-11-10")` es medianoche UTC (un día menos al oeste de
// Greenwich) y `Date.UTC(99, …)` convierte el año 99 en 1999. Estas dos
// funciones son el algoritmo de Hinnant (`days_from_civil`/`civil_from_days`),
// el mismo que usan las bibliotecas de fechas: días enteros desde el 1 de
// enero de 1970 y vuelta, exactos para cualquier año de cuatro cifras.

/** Días desde 1970-01-01 hasta una clave `YYYY-MM-DD`. */
function diasDeClave(clave) {
  const [textoAnio, textoMes, textoDia] = String(clave).split("-");
  let anio = Number(textoAnio);
  let mes = Number(textoMes);
  const dia = Number(textoDia);
  if (mes <= 2) {
    anio -= 1;
    mes += 12;
  }
  const era = Math.floor(anio / 400);
  const anioDeEra = anio - era * 400;
  const diaDelAnio = Math.floor((153 * (mes - 3) + 2) / 5) + dia - 1;
  const diaDeEra =
    anioDeEra * 365 + Math.floor(anioDeEra / 4) - Math.floor(anioDeEra / 100) + diaDelAnio;
  return era * 146097 + diaDeEra - 719468;
}

/** La clave `YYYY-MM-DD` que cae `dias` días después de 1970-01-01. */
function claveDeDias(dias) {
  const n = dias + 719468;
  const era = Math.floor(n / 146097);
  const diaDeEra = n - era * 146097;
  const anioDeEra = Math.floor(
    (diaDeEra -
      Math.floor(diaDeEra / 1460) +
      Math.floor(diaDeEra / 36524) -
      Math.floor(diaDeEra / 146096)) /
      365,
  );
  const anio = anioDeEra + era * 400;
  const diaDelAnio =
    diaDeEra - (365 * anioDeEra + Math.floor(anioDeEra / 4) - Math.floor(anioDeEra / 100));
  const mesAproximado = Math.floor((5 * diaDelAnio + 2) / 153);
  const dia = diaDelAnio - Math.floor((153 * mesAproximado + 2) / 5) + 1;
  const mes = mesAproximado < 10 ? mesAproximado + 3 : mesAproximado - 9;
  return `${String(anio).padStart(4, "0")}-${String(mes).padStart(2, "0")}-${String(dia).padStart(2, "0")}`;
}

/** El valor de una fecha (con su posible hora) puesto en el día `clave`.
 *
 * Mover una barra cambia el DÍA, no el valor entero: un `2026-11-10T23:00` que
 * se arrastra al 15 sigue siendo de las once de la noche del 15.
 */
function fechaEnDia(valor, clave) {
  const m = String(valor ?? "").match(/^\d{4}-\d{2}-\d{2}(.*)$/);
  return m ? clave + m[1] : clave;
}

/** «10 nov» para la etiqueta de una barra. */
function fechaCorta(clave) {
  const mes = Number(clave.slice(5, 7));
  return `${Number(clave.slice(8, 10))} ${MESES_CORTOS[mes - 1] || ""}`.trim();
}

/** «10 nov – 20 nov», o «10 nov» si el tramo dura un día. */
function textoDeTramo(desde, hasta) {
  return desde === hasta ? fechaCorta(desde) : `${fechaCorta(desde)} – ${fechaCorta(hasta)}`;
}

// --- el rango de una vista de calendario --------------------------------
//
// `config.start_property` y `config.end_property` son la clave de dos
// propiedades de fecha: la fila se pinta como una barra del primer día al
// último, los dos incluidos. Sin ninguna de las dos claves la vista no declara
// rango y el calendario sigue pintando un punto por fila (`date_property`),
// como siempre.

/** Las dos propiedades que dibujan la barra, o `null` si la vista no declara
 * rango.
 *
 * `end_property` puede faltar (filas de un día) y una vista que solo declara
 * `end_property` coloca la fila en el día de fin: una barra de un día no
 * necesita un inicio inventado.
 */
function rangoDeVista(database, view) {
  const fechas = (database.schema_def || []).filter((p) => p.type === "date");
  const buscar = (clave) => fechas.find((p) => p.key === clave) || null;
  const inicio = buscar(view.config?.start_property);
  const fin = buscar(view.config?.end_property);
  if (!inicio && !fin) return null;
  if (!inicio) return { inicio: fin, fin: null };
  return { inicio, fin: fin && fin.key !== inicio.key ? fin : null };
}

/** Los días que ocupa una fila en un calendario con rango.
 *
 * Devuelve `{desde, hasta}` con claves civiles y `hasta >= desde`, o el motivo
 * por el que no se puede pintar: `{falta}` (no hay ninguna de las dos fechas) o
 * `{invalida}` (hay un valor que no es un día real —31-nov—, un fin sin inicio
 * que no es fecha, o un fin ANTES del inicio: una barra al revés no se pinta y
 * la fila cuenta como «fecha no válida», igual que la imposible).
 */
function tramoDeFila(row, rango) {
  const vacio = (v) => v === null || v === undefined || String(v).trim() === "";
  const crudoInicio = row.properties?.[rango.inicio.key];
  const crudoFin = rango.fin ? row.properties?.[rango.fin.key] : null;
  const inicio = claveDeFecha(crudoInicio);
  const fin = claveDeFecha(crudoFin);
  if (vacio(crudoInicio)) {
    // Sin inicio: si hay fin, la fila se coloca en su día de fin (un día).
    if (!rango.fin || vacio(crudoFin)) return { falta: true };
    return fin ? { desde: fin, hasta: fin } : { invalida: true };
  }
  if (!inicio) return { invalida: true };
  if (!rango.fin || vacio(crudoFin)) return { desde: inicio, hasta: inicio };
  if (!fin) return { invalida: true };
  if (diasDeClave(fin) < diasDeClave(inicio)) return { invalida: true };
  return { desde: inicio, hasta: fin };
}

/** Reparte las filas del mes en carriles para que dos barras que se pisan no
 * se tapen, y para que una barra que cruza de semana no cambie de altura al
 * pasar el domingo: el carril se decide sobre el tramo entero, no por trozos. */
function asignarCarriles(filas) {
  const ocupados = [];
  [...filas]
    .sort((a, b) =>
      a.desde < b.desde ? -1 : a.desde > b.desde ? 1 : a.hasta < b.hasta ? -1 : 1,
    )
    .forEach((f) => {
      let carril = 0;
      for (;;) {
        if (!ocupados[carril]) ocupados[carril] = [];
        const franjas = ocupados[carril];
        if (!franjas.some(([desde, hasta]) => !(f.hasta < desde || f.desde > hasta))) {
          f.carril = carril;
          franjas.push([f.desde, f.hasta]);
          break;
        }
        carril += 1;
      }
    });
  return filas;
}

/** Las semanas que pinta la rejilla de un mes: filas de siete días civiles,
 * con los días del mes anterior al principio y los del siguiente al final.
 *
 * Se devuelven enteras (huecos incluidos) porque una barra que empieza el 28
 * del mes anterior y acaba el 3 se ve en los dos sitios: recortarla a los días
 * del mes la haría desaparecer de la semana en la que de verdad empieza.
 */
function semanasDeMes(anio, mes) {
  const dias = new Date(anio, mes + 1, 0).getDate(); // día 0 del mes siguiente
  const primerDia = new Date(anio, mes, 1);
  const hueco = (primerDia.getDay() + 6) % 7; // lunes = 0
  const inicio = diasDeClave(`${anio}-${String(mes + 1).padStart(2, "0")}-01`) - hueco;
  const total = Math.ceil((hueco + dias) / 7) * 7;
  const semanas = [];
  for (let i = 0; i < total; i += 7) {
    const semana = [];
    for (let d = 0; d < 7; d++) semana.push(claveDeDias(inicio + i + d));
    semanas.push(semana);
  }
  return semanas;
}

// Alto de una barra y separación entre carriles, en píxeles: el alto de la
// semana se calcula con ellos (`TOPE_DE_BARRAS` es la línea del número del
// día, que la capa de barras no tapa).
const ALTO_DE_BARRA = 18;
const HUECO_DE_BARRAS = 3;
const TOPE_DE_BARRAS = 22;

// El último gesto del calendario, para deshacerlo con UN `Ctrl+Z`: se guarda el
// valor que tenían las dos fechas antes del arrastre, así que un solo atajo
// devuelve el gesto entero (no hay un paso por fecha). Una sola casilla: se
// deshace lo último, y solo en el calendario donde se hizo (`clave`).
let ultimoGestoDeRango = null;

/** Deshacer el gesto del calendario: un PATCH con las dos fechas de antes. */
async function deshacerGestoDeRango(gesto) {
  try {
    await saveRow(gesto.database, gesto.row, { properties: gesto.antes });
    gesto.repintar?.();
  } catch (e) {
    reportError("No se pudo deshacer el movimiento del calendario", e);
  }
}

document.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
  if (e.key !== "z" && e.key !== "Z") return;
  const gesto = ultimoGestoDeRango;
  if (!gesto) return;
  // Solo en el calendario donde se hizo y que se está mirando: si el usuario se
  // ha ido a una nota, el Ctrl+Z ya no es de este gesto.
  if (!calendarioVivo?.wrap?.isConnected || calendarioVivo.clave !== gesto.clave) return;
  // Dentro de un campo (el nombre de una fila nueva, un selector, el editor de
  // la nota) el Ctrl+Z es del campo: deshacer el arrastre desde ahí sería un
  // secuestro.
  const destino = e.target;
  if (
    destino &&
    destino.closest &&
    destino.closest("input, textarea, select, [contenteditable='true']")
  ) {
    return;
  }
  e.preventDefault();
  ultimoGestoDeRango = null;
  deshacerGestoDeRango(gesto);
});


// El mes que está mirando el usuario, por vista. `refresh` (guardar una fila,
// mover una tarjeta) reconstruye la vista entera; sin esto, editar un evento
// de otro mes devolvía el calendario al mes de hoy y se perdía el sitio desde
// el que se estaba editando.
const mesDeCalendario = new Map();

function claveDeMesDeVista(database, view) {
  return `${database.id}:${view.id}`;
}

// El ancho que decide si el calendario se pinta como rejilla o como agenda. La
// consulta y su listener son de módulo, como `drawerQuery`: un MediaQueryList
// por render dejaría un listener vivo por cada pestaña abierta o cada guardado
// (cada `renderDatabase` pinta el calendario otra vez) y cada uno repintaría su
// rejilla aunque ya estuviera fuera del documento, así que el coste de mover la
// ventana crecía con las veces que se hubiera abierto el calendario.
const CALENDAR_NARROW_QUERY = "(max-width: 640px)";
const calendarQuery = window.matchMedia(CALENDAR_NARROW_QUERY);
let calendarioVivo = null; // { wrap, repaint } del último renderCalendar

calendarQuery.addEventListener("change", () => {
  if (!calendarioVivo || !calendarioVivo.wrap.isConnected) return;
  calendarioVivo.repaint();
});

/** El aviso de una vista que no puede pintarse: un título y qué hacer. Nunca
 * una vista en blanco: si el calendario no tiene fecha, lo dice. */
function avisoDeVista(titulo, detalle) {
  const box = document.createElement("div");
  box.className = "db-view-empty";
  const h = document.createElement("p");
  h.className = "db-view-empty-title";
  h.textContent = titulo;
  box.appendChild(h);
  if (detalle) {
    const p = document.createElement("p");
    p.className = "db-view-empty-note";
    p.textContent = detalle;
    box.appendChild(p);
  }
  return box;
}

/** El recuento de las filas que el calendario no puede colocar: las que no
 * tienen valor en la propiedad de fecha y las que tienen un valor que no es un
 * día real (`2026-11-31`). Las dos se cuentan y se dicen; antes la segunda
 * desaparecía en silencio. */
function textoDeFilasFuera(sueltas, invalidas) {
  const partes = [];
  if (sueltas) partes.push(sueltas === 1 ? "1 fila sin fecha" : `${sueltas} filas sin fecha`);
  if (invalidas) {
    partes.push(invalidas === 1 ? "1 fila con fecha no válida" : `${invalidas} filas con fecha no válida`);
  }
  return partes.join(" · ");
}

/** Vista de calendario.
 *
 * Dos modos, según lo que declare la vista:
 *
 * - **rango** (`config.start_property`/`end_property`): cada fila es una barra
 *   continua de su primer día a su último día, con el título dentro. La barra
 *   se corta en el borde de la semana y SIGUE en la siguiente (el trozo del
 *   lunes no lleva asa de inicio: el inicio está en otra semana) y se arrastra:
 *   el cuerpo mueve la tarea entera —las dos fechas, los mismos días— y las
 *   asas de los bordes estiran o encogen una sola. Un gesto es UN PATCH, así
 *   que un solo `Ctrl+Z` lo deshace.
 * - **un día** (sin rango, como siempre): cada fila es un chip en el día que
 *   tiene en `config.date_property`.
 *
 * Qué fecha se usa lo dice la vista; una vista antigua sin esa clave, o con una
 * propiedad que ya no existe, cae en la primera propiedad de fecha del esquema.
 * Si la base no tiene ninguna, no se pinta una tabla por debajo ni una caja
 * vacía: se dice lo que pasa y cómo arreglarlo.
 */
function renderCalendar(database, rows, view, refresh) {
  const dateProps = database.schema_def.filter((p) => p.type === "date");
  if (dateProps.length === 0) {
    return avisoDeVista(
      "Esta base no tiene ninguna propiedad de fecha.",
      "El calendario coloca cada fila en su día, así que necesita una propiedad de tipo fecha. Añádela en «Propiedades» y vuelve a abrir el calendario.",
    );
  }

  // Un gesto de OTRO calendario no se deshace desde aquí: el Ctrl+Z es del
  // calendario que se está mirando.
  const claveDeVista = claveDeMesDeVista(database, view);
  if (ultimoGestoDeRango && ultimoGestoDeRango.clave !== claveDeVista) {
    ultimoGestoDeRango = null;
  }

  // El rango, si la vista lo declara; sin él, la fecha de siempre. La fecha
  // «base» —donde nace una fila nueva y donde se cuentan las que no se pueden
  // colocar— es el inicio del rango cuando lo hay.
  let rango = rangoDeVista(database, view);
  let dateProp = rango
    ? rango.inicio
    : dateProps.find((p) => p.key === view.config?.date_property) || dateProps[0];

  const hoy = claveDeHoy();
  const [anioHoy, mesHoy] = [Number(hoy.slice(0, 4)), Number(hoy.slice(5, 7)) - 1];
  // Si esta vista ya estaba abierta en otro mes, se vuelve a ese mes: un
  // repintado (guardar una fila desde un chip) no puede devolver al de hoy.
  const mesGuardado = mesDeCalendario.get(claveDeVista);
  let anio = mesGuardado?.anio ?? anioHoy;
  let mes = mesGuardado?.mes ?? mesHoy;
  const recordarMes = () => mesDeCalendario.set(claveDeVista, { anio, mes });

  const wrap = document.createElement("div");
  wrap.className = "calendar-wrap";

  const toolbar = document.createElement("div");
  toolbar.className = "calendar-toolbar";

  const nav = document.createElement("div");
  nav.className = "cal-nav";
  const botonNav = (texto, etiqueta) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn";
    b.textContent = texto;
    b.setAttribute("aria-label", etiqueta);
    return b;
  };
  const prevBtn = botonNav("‹", "Mes anterior");
  const todayBtn = botonNav("Hoy", "Ir al mes actual");
  const nextBtn = botonNav("›", "Mes siguiente");
  nav.append(prevBtn, todayBtn, nextBtn);

  const titulo = document.createElement("h3");
  titulo.className = "cal-title";

  const lado = document.createElement("div");
  lado.className = "cal-side";

  /** Guardar el `config` de la vista y repintar la base entera: el rango cambia
   * la forma de la vista (barra o punto), así que se vuelve a pedir todo en vez
   * de repintar con una copia que ya no vale. */
  const guardarConfig = async (config, aviso) => {
    try {
      await api(`/databases/${database.id}/views/${view.id}`, {
        method: "PATCH",
        body: JSON.stringify({ config }),
      });
      view.config = config;
      if (refresh) refresh();
      else repaint();
    } catch (e) {
      reportError(aviso, e);
    }
  };

  // Con una sola propiedad de fecha no hay nada que elegir; con dos o más, se
  // elige aquí y la elección se guarda en la vista.
  if (dateProps.length > 1) {
    const pick = document.createElement("label");
    pick.className = "cal-date-pick";
    const pickText = document.createElement("span");
    pickText.textContent = "Fecha";
    const sel = document.createElement("select");
    sel.className = "cal-date-select";
    sel.setAttribute("aria-label", "Propiedad de fecha del calendario");
    dateProps.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.key;
      opt.textContent = p.name;
      sel.appendChild(opt);
    });
    sel.value = dateProp.key;
    pick.append(pickText, sel);
    sel.addEventListener("change", () => {
      const config = { ...(view.config || {}) };
      if (rango) {
        // En una vista con rango, esta es la propiedad del INICIO de la barra.
        config.start_property = sel.value;
        if (config.end_property === sel.value) delete config.end_property;
      } else {
        config.date_property = sel.value;
      }
      guardarConfig(config, "No se pudo guardar la fecha de la vista");
    });
    lado.appendChild(pick);

    // El fin del rango. «—» deja la vista en barras de un día (o en el punto de
    // siempre si nunca declaró inicio).
    const pickFin = document.createElement("label");
    pickFin.className = "cal-date-pick";
    const finText = document.createElement("span");
    finText.textContent = "Hasta";
    const selFin = document.createElement("select");
    selFin.className = "cal-date-select";
    selFin.setAttribute("aria-label", "Propiedad de fin del rango del calendario");
    const sinFin = document.createElement("option");
    sinFin.value = "";
    sinFin.textContent = "—";
    selFin.appendChild(sinFin);
    dateProps.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.key;
      opt.textContent = p.name;
      selFin.appendChild(opt);
    });
    selFin.value = rango?.fin?.key || "";
    pickFin.append(finText, selFin);
    selFin.addEventListener("change", () => {
      const config = { ...(view.config || {}) };
      if (!selFin.value) {
        if (!rango) return; // no declaraba rango: no hay nada que quitar
        delete config.end_property;
        // La vista se queda con la fecha que estaba usando: si solo tenía
        // fin, esa fecha es la de sus barras de un día.
        config.start_property = rango.inicio.key;
      } else {
        config.start_property = rango ? rango.inicio.key : dateProp.key;
        config.end_property = selFin.value;
      }
      guardarConfig(config, "No se pudo guardar el rango de la vista");
    });
    lado.appendChild(pickFin);
  }

  const sinFecha = document.createElement("span");
  sinFecha.className = "cal-undated";
  lado.appendChild(sinFecha);

  toolbar.append(nav, titulo, lado);

  const grid = document.createElement("div");
  grid.className = "calendar-grid";
  grid.setAttribute("aria-label", `Calendario de ${database.title}`);

  // En pantalla estrecha una rejilla de siete columnas deja 45 px por día: ni
  // el número se lee bien. Ahí la misma lista de días se pinta como agenda
  // (un bloque por día con sus filas), que es lo que cabe a 375 px. El cambio
  // de ancho lo sigue el listener de módulo de `calendarQuery`, que repinta el
  // calendario que esté en el documento (ver arriba).

  const chipDeFila = (row) => {
    // Un botón de verdad: se abre con Tab/Enter como el resto del panel, y
    // abre la misma fila que la tarjeta del tablero (ahí se edita).
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "cal-event";
    chip.textContent = row.title || "Sin título";
    chip.title = row.title || "Sin título";
    chip.dataset.rowId = row.id;
    chip.addEventListener("click", () =>
      openRowPage(database, row, { origin: chip, onClose: () => refresh?.() }),
    );
    return chip;
  };

  // ---------------------------------------------------------- arrastre
  // Un gesto empieza en una barra (el cuerpo mueve, un asa estira) y termina al
  // soltar: mientras dura se marca el día de destino bajo el puntero y no se
  // escribe nada. Al soltar se manda UN PATCH con las fechas que cambian.
  let gesto = null;
  let suprimirClic = false;

  /** La clave del día que hay debajo del puntero, mirando la semana por
   * geometría: así sirve también cuando lo que hay debajo es la capa de barras
   * (que tapa las celdas) o el hueco entre dos días. */
  const diaEnPunto = (x, y) => {
    const el = document.elementFromPoint(x, y);
    const semanaEl = el?.closest?.(".cal-week");
    if (!semanaEl) return null;
    const caja = semanaEl.getBoundingClientRect();
    const col = Math.min(6, Math.max(0, Math.floor(((x - caja.left) / caja.width) * 7)));
    const celda = semanaEl.querySelectorAll(".cal-day")[col];
    return celda?.dataset.dia || null;
  };

  /** Los días que resultarían del gesto si se soltara en el día `destino`. */
  const prevision = (g, destino) => {
    if (!destino) return null;
    if (g.modo === "mover") {
      // Mover la tarea entera: las dos fechas se desplazan los mismos días.
      const delta = diasDeClave(destino) - diasDeClave(g.diaEnPuntero);
      return {
        desde: claveDeDias(diasDeClave(g.desde) + delta),
        hasta: claveDeDias(diasDeClave(g.hasta) + delta),
      };
    }
    // Los bordes cambian SOLO su fecha, y no se cruzan: arrastrar el inicio más
    // allá del fin lo deja pegado al fin (la barra no se da la vuelta).
    if (g.modo === "inicio") {
      return {
        desde: diasDeClave(destino) <= diasDeClave(g.hasta) ? destino : g.hasta,
        hasta: g.hasta,
      };
    }
    return {
      desde: g.desde,
      hasta: diasDeClave(destino) >= diasDeClave(g.desde) ? destino : g.desde,
    };
  };

  const marcarDestino = (tramo) => {
    grid.querySelectorAll(".cal-day.is-destino").forEach((c) => c.classList.remove("is-destino"));
    if (!tramo) return;
    const desde = diasDeClave(tramo.desde);
    const hasta = diasDeClave(tramo.hasta);
    grid.querySelectorAll(".cal-day[data-dia]").forEach((c) => {
      const n = diasDeClave(c.dataset.dia);
      if (n >= desde && n <= hasta) c.classList.add("is-destino");
    });
  };

  const escucharArrastre = (activar) => {
    const metodo = activar ? "addEventListener" : "removeEventListener";
    document[metodo]("pointermove", moverArrastre, true);
    document[metodo]("pointerup", soltarArrastre, true);
    document[metodo]("pointercancel", soltarArrastre, true);
    document[metodo]("keydown", teclaDeArrastre, true);
  };

  /** Escribir el gesto: un PATCH con la fecha de inicio, la de fin, o las dos.
   *
   * `modo` dice qué se está haciendo —`mover`, `inicio` o `fin`—, y con eso se
   * decide qué fechas se escriben. Al MOVER solo se desplazan las que la fila
   * ya tenía: rellenar la que faltaba convertía una fila de un día (solo
   * `inicio`) en una con `entrega`, y el `.md` es la verdad del repo —los
   * filtros `is_empty`/`is_not_empty`, el orden y la tabla leen esa clave—, así
   * que el gesto no debe inventar propiedades. Un asa sí crea la suya: es la
   * fecha que se está estirando.
   */
  const aplicarGesto = async (row, desde, hasta, modo) => {
    const properties = {};
    const antes = {};
    const claveActual = (prop) => claveDeFecha(row.properties?.[prop.key]);
    // Solo se toca la fecha que cambia y, según el gesto, solo la que le toca:
    // mover las que existen; un asa, la suya (aunque la fila no la tuviera).
    const seEscribe = (prop, clave, modoPropio) =>
      clave !== claveActual(prop) &&
      (modo === "mover" ? claveActual(prop) !== null : modo === modoPropio);
    if (seEscribe(rango.inicio, desde, "inicio")) {
      antes[rango.inicio.key] = row.properties?.[rango.inicio.key] ?? null;
      properties[rango.inicio.key] = fechaEnDia(row.properties?.[rango.inicio.key], desde);
    }
    if (rango.fin && seEscribe(rango.fin, hasta, "fin")) {
      antes[rango.fin.key] = row.properties?.[rango.fin.key] ?? null;
      properties[rango.fin.key] = fechaEnDia(row.properties?.[rango.fin.key], hasta);
    }
    if (!Object.keys(properties).length) return;
    try {
      await saveRow(database, row, { properties });
      // Un solo PATCH por gesto: el Ctrl+Z devuelve las dos fechas de una vez,
      // no una por fecha.
      ultimoGestoDeRango = {
        clave: claveDeVista,
        database,
        row,
        antes,
        repintar: () => (refresh ? refresh() : repaint()),
      };
      if (refresh) refresh();
      else repaint();
    } catch (e) {
      reportError("No se pudo mover la fila en el calendario", e);
    }
  };

  const soltarArrastre = (e) => {
    const g = gesto;
    if (!g) return;
    gesto = null;
    escucharArrastre(false);
    g.barra.classList.remove("is-dragging");
    wrap.classList.remove("is-arrastrando");
    marcarDestino(null);
    const destino = diaEnPunto(e.clientX, e.clientY) || g.destino || g.diaEnPuntero;
    const fin = prevision(g, destino);
    if (!fin) return;
    if (fin.desde === g.desde && fin.hasta === g.hasta) return; // un clic, no un gesto
    suprimirClic = true;
    aplicarGesto(g.row, fin.desde, fin.hasta, g.modo);
  };

  function moverArrastre(e) {
    if (!gesto) return;
    gesto.destino = diaEnPunto(e.clientX, e.clientY);
    marcarDestino(prevision(gesto, gesto.destino));
  }

  /** Esc cancela el gesto: la barra se queda donde estaba y no se escribe nada. */
  function teclaDeArrastre(e) {
    if (e.key !== "Escape" || !gesto) return;
    e.preventDefault();
    e.stopPropagation();
    const g = gesto;
    gesto = null;
    escucharArrastre(false);
    g.barra.classList.remove("is-dragging");
    wrap.classList.remove("is-arrastrando");
    marcarDestino(null);
  }

  /** Una barra de la rejilla: un trozo del tramo de una fila dentro de UNA
   * semana. Los asideros solo se pintan donde está el borde de verdad: el trozo
   * que sigue la semana siguiente no lleva el asa de inicio. */
  const barraDeTramo = (tramo, semana, colIni, colFin) => {
    const barra = document.createElement("div");
    barra.className = "cal-bar";
    barra.dataset.rowId = String(tramo.row.id);
    barra.dataset.desde = tramo.desde;
    barra.dataset.hasta = tramo.hasta;
    const cortaIzq = tramo.desde < semana[0];
    const cortaDer = tramo.hasta > semana[6];
    if (cortaIzq) barra.classList.add("is-corta-izq");
    if (cortaDer) barra.classList.add("is-corta-der");
    barra.style.left = `calc(${(colIni * 100) / 7}% + 1px)`;
    barra.style.width = `calc(${((colFin - colIni + 1) * 100) / 7}% - 2px)`;
    barra.style.top = `${TOPE_DE_BARRAS + tramo.carril * (ALTO_DE_BARRA + HUECO_DE_BARRAS)}px`;
    barra.style.height = `${ALTO_DE_BARRA}px`;
    barra.title = `${tramo.row.title || "Sin título"} · ${textoDeTramo(tramo.desde, tramo.hasta)}`;

    const texto = document.createElement("span");
    texto.className = "cal-bar-title";
    texto.textContent = tramo.row.title || "Sin título";
    barra.appendChild(texto);

    const empezar = (modo) => (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      if (gesto) return;
      const dia = diaEnPunto(e.clientX, e.clientY);
      if (!dia) return;
      gesto = {
        modo,
        row: tramo.row,
        barra,
        desde: tramo.desde,
        hasta: tramo.hasta,
        diaEnPuntero: dia,
        destino: dia,
      };
      suprimirClic = false;
      barra.classList.add("is-dragging");
      wrap.classList.add("is-arrastrando");
      escucharArrastre(true);
    };
    barra.addEventListener("pointerdown", empezar("mover"));

    if (rango.fin) {
      const asa = (modo, etiqueta) => {
        const el = document.createElement("span");
        el.className = `cal-bar-handle is-${modo}`;
        el.setAttribute("aria-hidden", "true");
        el.title = etiqueta;
        // El asa vive DENTRO de la barra: sin parar el `pointerdown` aquí, el
        // de la barra empezaría un «mover» a la vez que el «estirar».
        el.addEventListener("pointerdown", (e) => {
          e.stopPropagation();
          empezar(modo)(e);
        });
        return el;
      };
      // El asa de inicio va DELANTE del título (borde izquierdo de la barra) y
      // la de fin detrás (borde derecho): el orden del flex es el orden en que
      // se pintan.
      if (!cortaIzq) {
        barra.insertBefore(asa("inicio", "Arrastra para cambiar el día de inicio"), barra.firstChild);
      }
      if (!cortaDer) barra.appendChild(asa("fin", "Arrastra para cambiar el día de fin"));
    }

    barra.addEventListener("click", (e) => {
      if (suprimirClic) {
        suprimirClic = false;
        e.preventDefault();
        return;
      }
      openRowPage(database, tramo.row, { origin: barra, onClose: () => refresh?.() });
    });
    makeKeyboardActivatable(barra, "button", 0);
    return barra;
  };

  const repaint = () => {
    // La vista puede haber cambiado de fecha sin que se reconstruya el
    // calendario (un repintado por el ancho): se resuelve otra vez.
    rango = rangoDeVista(database, view);
    dateProp = rango
      ? rango.inicio
      : dateProps.find((p) => p.key === view.config?.date_property) || dateProps[0];

    // Una pasada por fila: las que no tienen fecha o tienen una imposible no se
    // colocan, se cuentan y se avisa de cada grupo por separado.
    let sueltas = 0;
    let invalidas = 0;
    const tramos = [];
    const porDia = new Map();
    if (rango) {
      rows.forEach((row) => {
        const tramo = tramoDeFila(row, rango);
        if (tramo.falta) {
          sueltas += 1;
          return;
        }
        if (tramo.invalida) {
          invalidas += 1;
          return;
        }
        tramos.push({ row, desde: tramo.desde, hasta: tramo.hasta });
      });
      asignarCarriles(tramos);
    } else {
      rows.forEach((row) => {
        const valor = row.properties?.[dateProp.key];
        const key = claveDeFecha(valor);
        if (!key) {
          if (valor === null || valor === undefined || String(valor).trim() === "") sueltas += 1;
          else invalidas += 1;
          return;
        }
        if (!porDia.has(key)) porDia.set(key, []);
        porDia.get(key).push(row);
      });
    }

    /** Las filas que tocan un día: en modo rango, todas las barras que lo
     * cubren (una tarea del 10 al 20 está en los once días). */
    const filasDelDia = (clave) => {
      if (!rango) return porDia.get(clave) || [];
      return tramos.filter((t) => t.desde <= clave && t.hasta >= clave).map((t) => t.row);
    };

    const etiquetaDeFechas = rango
      ? rango.fin
        ? `«${rango.inicio.name}» y «${rango.fin.name}»`
        : `«${rango.inicio.name}»`
      : `«${dateProp.name}»`;

    titulo.textContent = `${MESES[mes]} de ${anio}`;
    sinFecha.textContent = textoDeFilasFuera(sueltas, invalidas);
    sinFecha.title = invalidas
      ? `«Sin fecha»: no tienen valor en ${etiquetaDeFechas}. «Fecha no válida»: el valor no es un día real (por ejemplo, 31 de noviembre)` +
        (rango?.fin ? ", o el fin es anterior al inicio," : "") +
        " y hay que corregirlo."
      : `Filas que no se pueden colocar: no tienen valor en ${etiquetaDeFechas}.`;
    sinFecha.classList.toggle("is-warn", invalidas > 0);
    sinFecha.hidden = sueltas + invalidas === 0 || calendarQuery.matches;

    grid.innerHTML = "";
    const dias = new Date(anio, mes + 1, 0).getDate(); // día 0 del mes siguiente

    if (calendarQuery.matches) {
      grid.className = "calendar-agenda";
      let pintados = 0;
      for (let dia = 1; dia <= dias; dia++) {
        const clave = `${anio}-${String(mes + 1).padStart(2, "0")}-${String(dia).padStart(2, "0")}`;
        const delDia = filasDelDia(clave);
        if (!delDia.length) continue; // en la agenda, los días vacíos no ocupan
        pintados += 1;
        const bloque = document.createElement("div");
        bloque.className = "cal-agenda-day" + (clave === hoy ? " is-today" : "");
        const fecha = document.createElement("span");
        fecha.className = "cal-agenda-date";
        fecha.textContent = `${dia} de ${MESES[mes]}`;
        const eventos = document.createElement("div");
        eventos.className = "cal-events";
        delDia.forEach((row) => {
          const chip = chipDeFila(row);
          // En una agenda una barra no cabe: se dice que sigue, para que una
          // tarea de once días no parezca once tareas de un día.
          const tramo = rango ? tramos.find((t) => t.row === row) : null;
          if (tramo && (tramo.desde < clave || tramo.hasta > clave)) {
            chip.textContent =
              (tramo.desde < clave ? "← " : "") +
              (row.title || "Sin título") +
              (tramo.hasta > clave ? " →" : "");
            chip.title = `${row.title || "Sin título"} · ${textoDeTramo(tramo.desde, tramo.hasta)}`;
            chip.classList.add("is-continua");
          }
          eventos.appendChild(chip);
        });
        bloque.append(fecha, eventos);
        grid.appendChild(bloque);
      }
      if (pintados === 0) {
        const vacio = document.createElement("p");
        vacio.className = "cal-agenda-empty";
        vacio.textContent = `Ninguna fila en ${MESES[mes]} de ${anio}.`;
        grid.appendChild(vacio);
      }
      if (sueltas) {
        const nota = document.createElement("p");
        nota.className = "cal-agenda-empty";
        nota.textContent =
          sueltas === 1
            ? `1 fila sin fecha no se puede colocar: rellena ${etiquetaDeFechas}.`
            : `${sueltas} filas sin fecha no se pueden colocar: rellena ${etiquetaDeFechas}.`;
        grid.appendChild(nota);
      }
      if (invalidas) {
        const nota = document.createElement("p");
        nota.className = "cal-agenda-empty is-warn";
        nota.textContent =
          invalidas === 1
            ? `1 fila tiene una fecha no válida en ${etiquetaDeFechas} y no se puede colocar: corrige el día y el mes.`
            : `${invalidas} filas tienen una fecha no válida en ${etiquetaDeFechas} y no se pueden colocar: corrige el día y el mes.`;
        grid.appendChild(nota);
      }
      return;
    }

    grid.className = "calendar-grid";

    DIAS_SEMANA.forEach((d) => {
      const head = document.createElement("div");
      head.className = "cal-dow";
      head.textContent = d;
      grid.appendChild(head);
    });

    if (!rango) {
      // El calendario de un punto por fila, como siempre.
      // Hueco de los días de la semana anterior al día 1 (lunes = 0).
      const primerDia = new Date(anio, mes, 1);
      const hueco = (primerDia.getDay() + 6) % 7;
      for (let i = 0; i < hueco; i++) {
        const vacia = document.createElement("div");
        vacia.className = "cal-day is-out";
        grid.appendChild(vacia);
      }

      for (let dia = 1; dia <= dias; dia++) {
        const clave = `${anio}-${String(mes + 1).padStart(2, "0")}-${String(dia).padStart(2, "0")}`;
        const delDia = porDia.get(clave) || [];
        const celda = document.createElement("div");
        celda.className = "cal-day" + (clave === hoy ? " is-today" : "");
        celda.dataset.dia = clave;
        celda.setAttribute(
          "aria-label",
          `${dia} de ${MESES[mes]}` +
            (delDia.length ? `, ${delDia.length === 1 ? "1 fila" : `${delDia.length} filas`}` : ""),
        );
        const num = document.createElement("span");
        num.className = "cal-daynum";
        num.textContent = String(dia);
        celda.appendChild(num);
        const eventos = document.createElement("div");
        eventos.className = "cal-events";
        delDia.forEach((row) => eventos.appendChild(chipDeFila(row)));
        celda.appendChild(eventos);
        grid.appendChild(celda);
      }
      // Completa la última semana: la rejilla no deja huecos a la derecha.
      while (grid.children.length % 7 !== 0) {
        const vacia = document.createElement("div");
        vacia.className = "cal-day is-out";
        grid.appendChild(vacia);
      }
      return;
    }

    // Modo rango: una fila de la rejilla por semana, con sus siete días y una
    // capa encima para las barras. Así una barra cruza de día de una pieza y el
    // recorte al borde de la semana es solo su `left`/`width` en porcentaje.
    semanasDeMes(anio, mes).forEach((semana) => {
      const semanaEl = document.createElement("div");
      semanaEl.className = "cal-week";
      const delMes = `${anio}-${String(mes + 1).padStart(2, "0")}`;
      const tramosDeLaSemana = tramos.filter(
        (t) => t.desde <= semana[6] && t.hasta >= semana[0],
      );
      const carriles = tramosDeLaSemana.length
        ? Math.max(...tramosDeLaSemana.map((t) => t.carril)) + 1
        : 0;
      semanaEl.style.minHeight = `${Math.max(
        84,
        TOPE_DE_BARRAS + carriles * (ALTO_DE_BARRA + HUECO_DE_BARRAS) + 10,
      )}px`;

      semana.forEach((clave) => {
        const esDelMes = clave.slice(0, 7) === delMes;
        const cuantas = filasDelDia(clave).length;
        const celda = document.createElement("div");
        celda.className =
          "cal-day" + (esDelMes ? "" : " is-out") + (clave === hoy ? " is-today" : "");
        celda.dataset.dia = clave;
        celda.setAttribute(
          "aria-label",
          `${Number(clave.slice(8, 10))} de ${MESES[Number(clave.slice(5, 7)) - 1]}` +
            (cuantas ? `, ${cuantas === 1 ? "1 fila" : `${cuantas} filas`}` : ""),
        );
        const num = document.createElement("span");
        num.className = "cal-daynum";
        num.textContent = String(Number(clave.slice(8, 10)));
        celda.appendChild(num);
        semanaEl.appendChild(celda);
      });

      const capa = document.createElement("div");
      capa.className = "cal-week-bars";
      tramosDeLaSemana.forEach((tramo) => {
        const colIni = Math.max(0, diasDeClave(tramo.desde) - diasDeClave(semana[0]));
        const colFin = Math.min(6, diasDeClave(tramo.hasta) - diasDeClave(semana[0]));
        capa.appendChild(barraDeTramo(tramo, semana, colIni, colFin));
      });
      semanaEl.appendChild(capa);
      grid.appendChild(semanaEl);
    });
  };

  const mover = (delta) => {
    mes += delta;
    if (mes < 0) {
      mes = 11;
      anio -= 1;
    } else if (mes > 11) {
      mes = 0;
      anio += 1;
    }
    recordarMes();
    repaint();
  };
  prevBtn.addEventListener("click", () => mover(-1));
  nextBtn.addEventListener("click", () => mover(1));
  todayBtn.addEventListener("click", () => {
    anio = anioHoy;
    mes = mesHoy;
    recordarMes();
    repaint();
  });

  // «+ Nueva fila» debajo del calendario (rejilla o agenda). La fila nace con
  // la fecha de HOY en la propiedad de esta vista: un evento sin fecha no cae
  // en ningún día y no se vería, así que se le pone el único día que es «el
  // día en que se añade». Si el calendario está mirando otro mes, se va al de
  // hoy para enseñarla; el filtro de la vista, si la deja fuera, se dice.
  const addRow = renderAddRowControl({
    vista: view,
    plantillas: database.row_templates || [],
    onCreate: (titulo, plantilla) => {
      const extra = { ...(plantilla?.properties || {}) };
      // Un evento sin fecha no cae en ningún día y no se vería: si la plantilla
      // no trae la fecha, se pone la de hoy (el día en que se añade). Con rango,
      // en la propiedad de inicio: una barra de un día.
      const propDeAlta = rango ? rango.inicio : dateProp;
      if (extra[propDeAlta.key] === undefined) extra[propDeAlta.key] = claveDeHoy();
      return crearFilaEnVista(database, view, titulo, extra, plantilla?.body || "");
    },
    onDone: (res) => {
      refrescarRecuentoDeVista(grid, res.filas.length, res.total);
      if (!res.visible) return; // el aviso del control ya dice por qué no se ve
      rows.length = 0;
      rows.push(...res.filas);
      anio = anioHoy;
      mes = mesHoy;
      recordarMes();
      repaint();
      enfocarFila(grid, res.fila.id);
    },
  });

  wrap.append(toolbar, grid, addRow);
  repaint();
  // Este es el calendario que hay que repintar al cambiar el ancho. El anterior
  // (otra pestaña, la misma vista repintada tras guardar) queda sin
  // referencias y se puede recoger. `clave` dice cuál es, para que el Ctrl+Z de
  // un gesto no se dispare desde otro calendario.
  calendarioVivo = { wrap, repaint, clave: claveDeVista };
  return wrap;
}

/** Vista de lista: una línea por fila, con el título y los valores que tiene
 * (los vacíos no se pintan). Es la vista de repasar muchas filas de un vistazo;
 * cada línea abre la fila, que es donde se edita, igual que una tarjeta del
 * tablero. */
function renderList(database, rows, view, refresh, { barraEn = null } = {}) {
  const shell = document.createElement("div");
  shell.className = "db-sel-shell";
  // Misma selección que la tabla: una fila marcada en la lista y luego vista en
  // la tabla (otra pestaña) sigue marcada.
  const sel = crearSeleccionDeFilas({
    database,
    rows,
    onRepintar: refresh,
    anfitrion: barraEn,
    marco: shell,
  });

  const wrap = document.createElement("div");
  wrap.className = "db-list";

  // La lista no tiene cabecera de columnas, así que «seleccionar todas las
  // visibles» va en una línea propia, encima de la primera fila.
  const cabeza = document.createElement("div");
  cabeza.className = "db-list-head";
  const etiquetaCabeza = document.createElement("span");
  etiquetaCabeza.className = "db-list-head-text";
  etiquetaCabeza.textContent = "Seleccionar todas";
  cabeza.append(sel.casillaDeCabecera(), etiquetaCabeza);
  wrap.appendChild(cabeza);

  // El contenido de una línea se pinta aparte para poder REPINTARLA en el
  // sitio tras guardar en la vista de fila: el botón (que es el que tiene el
  // foco al volver) no se reemplaza, solo lo de dentro.
  // La jerarquía: el servidor ya manda las hijas detrás de su madre, así que
  // aquí solo se sangra y se pone el contador de cada madre.
  const claveJerarquia = claveDeJerarquia(database);
  const profundidades = mapaDeProfundidades(rows, claveJerarquia);
  const pintarLinea = (item, row) => {
    item.textContent = "";
    const prof = profundidades.get(String(row.id)) || 0;
    if (prof) {
      const hueco = document.createElement("span");
      hueco.className = "list-row-indent";
      hueco.style.width = `${14 * prof}px`;
      hueco.setAttribute("aria-hidden", "true");
      item.appendChild(hueco);
    }
    const title = document.createElement("span");
    title.className = "list-row-title";
    title.textContent = row.title || "Sin título";
    item.appendChild(title);
    // Las mismas columnas que la tabla: el orden de la vista y las ocultas fuera.
    columnasDeVista(database, view).forEach((p) => {
      const v = row.properties?.[p.key];
      if (v === null || v === undefined || v === "" || (Array.isArray(v) && v.length === 0)) return;
      const span = document.createElement("span");
      span.className = "list-row-prop";
      span.innerHTML = `${escapeHtml(p.name)}: ${formatPropValueHtml(v, p, row)}`;
      item.appendChild(span);
    });
    const cuenta = contadorDeSubtareas(row);
    if (cuenta) {
      const badge = document.createElement("span");
      badge.className = "list-row-count";
      badge.textContent = cuenta;
      badge.title = `${row.children.closed} de ${row.children.total} subtareas cerradas`;
      item.appendChild(badge);
    }
    const nombre = `Abrir «${row.title || "Sin título"}» como página`;
    item.title = nombre;
    item.setAttribute("aria-label", nombre);
  };

  const lineaDeFila = (row) => {
    // La casilla va FUERA del botón: dentro no puede ir (un botón no lleva
    // otro control) y, si se envolviera, el clic de la casilla abriría la fila.
    const item = document.createElement("div");
    item.className = "list-row-item";
    const casilla = sel.casillaDeFila(row);
    const boton = document.createElement("button");
    boton.type = "button";
    boton.className = "list-row";
    boton.dataset.rowId = row.id;
    pintarLinea(boton, row);
    // En la lista la fila ENTERA abre la página: no hay celdas que editar en
    // el sitio, así que no hay dos gestos que separar (Espacio la marca).
    boton.addEventListener("click", () =>
      openRowPage(database, row, {
        origin: boton,
        rows,
        index: rows.indexOf(row),
        onSaved: () => pintarLinea(boton, row),
      }),
    );
    item.append(casilla, boton);
    sel.registrarFila(row, item, casilla);
    return item;
  };

  const avisoVacio = () =>
    avisoDeVista(
      "Aquí no hay filas todavía.",
      "Añade una con «Nueva fila», aquí debajo. Si la base ya tiene filas, las está dejando fuera el filtro de la vista.",
    );
  rows.forEach((row) => wrap.appendChild(lineaDeFila(row)));
  if (rows.length === 0) wrap.appendChild(avisoVacio());

  // «+ Nueva fila» al final de la lista, como una línea más. Al crear, la
  // lista se rehace con lo que devolvió el servidor: la fila nueva aparece
  // donde la deje el orden (y el filtro) de la vista, no al final por haber
  // sido la última en llegar.
  const addRow = renderAddRowControl({
    vista: view,
    className: "db-add-row db-add-row--list",
    plantillas: database.row_templates || [],
    onCreate: (titulo, plantilla) =>
      crearFilaEnVista(
        database,
        view,
        titulo,
        plantilla?.properties || {},
        plantilla?.body || "",
      ),
    onDone: (res) => {
      // La cabecera (la casilla de «todas») se queda: se rehacen las filas.
      for (const hijo of [...wrap.children]) {
        if (hijo !== addRow && hijo !== cabeza) hijo.remove();
      }
      if (res.filas.length === 0) wrap.insertBefore(avisoVacio(), addRow);
      res.filas.forEach((fila) => wrap.insertBefore(lineaDeFila(fila), addRow));
      sel.fijarVisibles(res.filas);
      refrescarRecuentoDeVista(wrap, res.filas.length, res.total);
      if (res.visible) enfocarFila(wrap, res.fila.id);
    },
  });
  wrap.appendChild(addRow);

  // Mismo teclado que la tabla, con el foco en la lista.
  wrap.addEventListener("keydown", (e) => sel.teclas(e), true);
  sel.sincronizar();

  shell.append(...(barraEn ? [] : [sel.barra]), sel.informes, wrap);
  return shell;
}

// ------------------------------------------------ filas: páginas normales
// En Notion cada fila es una página, y aquí también: una fila se abre por el
// MISMO camino que una nota (`goTo("note", id)` → `renderNote`), con su
// dirección (`/<id>`), su cabecera, sus backlinks, compartir (heredado de su
// base) y edición en vivo. Volver es el «atrás» del navegador, como en
// cualquier otra página.
//
// Lo único que tiene una fila y no una nota son sus PROPIEDADES: las de su
// base. Se pintan en un panel entre el título y el cuerpo
// (`montarPanelDeFila`), y desde ahí se cambia su valor (se guarda en la fila)
// o la propiedad misma —nombre, tipo, opciones, borrarla, o añadir una nueva—,
// que es de la BASE: la tienen todas sus filas, como en Notion.
//
// El título, el icono, la descripción y el cuerpo se guardan como los de una
// nota (`PATCH /pages/{id}` y la sala); las propiedades, por
// `PATCH /databases/{base}/rows/{id}`, que es quien las valida contra el
// esquema.

/** Abre una fila como página. `database` ya no hace falta para pintarla (la
 * página pide su base), pero se deja en la firma: es la de todas las vistas
 * que abren filas. */
async function openRowPage(_database, resumen) {
  if (!resumen?.id) return;
  await goTo("note", resumen.id);
}

/** El panel de propiedades (y subtareas) de una fila abierta como página.
 *
 * `caja` es el contenedor (ya en el documento); `page` es la página de la fila
 * (`database_id` dice su base). Devuelve `{refrescar}`: lo llama el aviso
 * `database` de `/events` cuando otra pestaña cambia la fila o el esquema. */
function montarPanelDeFila(page, caja, { alNavegar = null, alCargarBase = null } = {}) {
  const estado = { database: null, detalle: null, vivo: true, pidiendo: null };
  let seq = 0; // una carga que llega tarde no pisa la de después

  caja.className = "row-props-panel";
  caja.setAttribute("aria-label", "Propiedades de la fila");

  const propsEl = document.createElement("div");
  propsEl.className = "row-props";
  const subtareasEl = document.createElement("section");
  subtareasEl.className = "row-page-subtasks";
  subtareasEl.setAttribute("aria-label", "Subtareas de la fila");
  caja.append(propsEl, subtareasEl);

  /** ¿Se está editando algo del panel? Entonces un aviso de otra pestaña no
   * repinta: se llevaría lo que el usuario está escribiendo. */
  function ocupado() {
    const activo = document.activeElement;
    return Boolean(activo && caja.contains(activo) && activo.matches("input, select, textarea"));
  }

  async function cargar() {
    const mio = ++seq;
    try {
      const [database, detalle] = await Promise.all([
        api(`/databases/${page.database_id}`),
        api(`/databases/${page.database_id}/rows/${page.id}`),
      ]);
      if (!estado.vivo || !caja.isConnected || mio !== seq) return;
      estado.database = database;
      estado.detalle = detalle;
      alCargarBase?.(database);
      pintar();
    } catch (e) {
      if (!estado.vivo || !caja.isConnected || e?.status === 401) return;
      propsEl.textContent = "";
      const aviso = document.createElement("p");
      aviso.className = "row-page-prop-empty";
      aviso.textContent = "No se pudieron leer las propiedades de la fila.";
      propsEl.appendChild(aviso);
      reportError("No se pudieron leer las propiedades de la fila", e);
    }
  }

  function pintar() {
    pintarPropiedades();
    pintarSubtareas();
  }

  // ------------------------------------------------------- propiedades

  /** Guarda el valor de UNA propiedad en la fila. La respuesta trae la versión
   * nueva de la fila, y `api()` la apunta: el siguiente guardado del título
   * (con `If-Match`) no ve un conflicto que no es. */
  async function guardarValor(prop, valor) {
    const { database, detalle } = estado;
    if (sameValue(valor, detalle.properties?.[prop.key])) return;
    ultimoCambioPropio = { databaseId: String(database.id), ts: Date.now() };
    try {
      const guardada = await api(`/databases/${database.id}/rows/${page.id}`, {
        method: "PATCH",
        body: JSON.stringify({ properties: { [prop.key]: valor } }),
      });
      if (!estado.vivo) return;
      Object.assign(detalle, guardada);
      page.version = guardada.version;
      // Un cambio de estado puede cerrar la fila (el contador de su madre) y
      // una relación nueva cambia lo que se enseña: se relee lo que pinta el
      // servidor, que es quien sabe.
      if (prop.type === "relation" || prop.type === "person") await cargar();
    } catch (e) {
      reportError(`No se pudo guardar «${prop.name}»`, e);
      await cargar(); // se vuelve a lo que de verdad hay guardado
    }
  }

  function contextoDeColumna(prop) {
    return {
      database: estado.database,
      view: null,
      prop,
      enFila: true,
      onChanged: () => cargar(),
    };
  }

  function pintarPropiedades() {
    const { database, detalle } = estado;
    propsEl.textContent = "";
    // La celda de subtareas no es un campo: guarda el id de la madre, y lo que
    // se enseña es la lista de abajo (y «Subtarea de», si es una hija).
    const campos = (database.schema_def || []).filter((p) => p && p.type !== "parent_row");
    campos.forEach((prop, i) => {
      const fila = document.createElement("div");
      fila.className = "row-prop";

      // El nombre es un botón: abre el mismo menú que la cabecera de la
      // columna en la tabla (renombrar, tipo, opciones, borrar…). Lo que se
      // cambia ahí es de la base, como en Notion.
      const nombre = document.createElement("button");
      nombre.type = "button";
      nombre.className = "row-prop-name";
      nombre.innerHTML = `${
        prop.icon ? `<span class="db-prop-emoji">${escapeHtml(prop.icon)}</span>` : iconoDeTipo(prop.type)
      }<span class="row-prop-label"></span>`;
      nombre.querySelector(".row-prop-label").textContent = prop.name;
      nombre.title = `Opciones de «${prop.name}» (en toda la base)`;
      nombre.setAttribute("aria-haspopup", "dialog");
      nombre.addEventListener("click", () => abrirMenuDeColumna(nombre, contextoDeColumna(prop)));

      const { input, read } = buildPropertyInput(
        prop,
        detalle.properties?.[prop.key],
        detalle.people?.[prop.key] || null,
      );
      const inputId = idDeCampo(`fila-${page.id}`, i, prop.key);
      input.id = inputId;
      input.setAttribute("aria-label", prop.name);
      input.classList.add("row-prop-input");
      // Un campo de texto vacío no se ve (no tiene caja hasta que se toca): se
      // dice que está vacío, como en Notion.
      if (input.tagName === "INPUT" && !["checkbox", "date"].includes(input.type) && !input.placeholder) {
        input.placeholder = "Vacío";
      }
      // Como una celda: se guarda al confirmar (`change`: Intro o salir del
      // campo), no en cada tecla.
      input.addEventListener("change", () => guardarValor(prop, read()));
      if (input.tagName !== "SELECT" && input.type !== "checkbox") {
        input.addEventListener("keydown", (e) => {
          if (e.key !== "Enter") return;
          e.preventDefault();
          input.blur(); // el `change` del blur es el que guarda
        });
      }
      const valor = document.createElement("div");
      valor.className = "row-prop-value";
      valor.appendChild(input);
      fila.append(nombre, valor);
      propsEl.appendChild(fila);
    });

    // «+ Añadir una propiedad»: el formulario de la columna nueva de la tabla.
    const nueva = document.createElement("button");
    nueva.type = "button";
    nueva.className = "row-prop-add";
    nueva.innerHTML = `${svgIcon("i-plus")}Añadir una propiedad`;
    nueva.title = "La propiedad nueva es de la base: la tendrán todas sus filas";
    nueva.addEventListener("click", () =>
      abrirFormularioDeColumna(nueva, { database: estado.database, view: null, onChanged: () => cargar() }),
    );
    propsEl.appendChild(nueva);
  }

  // ------------------------------------------------------- subtareas
  // Lo que cuelga de una fila es un plan: las hijas se ven aquí, con su estado
  // y su fecha. Quién está cerrada lo dice el servidor (`closed`, con la regla
  // de la base): esa regla no se repite en el cliente.

  function fechaDeSubtarea(hija, propFecha) {
    const valor = propFecha ? hija.properties?.[propFecha.key] : null;
    const texto = String(valor || hija.updated_at || "");
    const partes = texto.match(/^(\d{4})-(\d{2})-(\d{2})/);
    return partes ? `${partes[3]}/${partes[2]}/${partes[1]}` : "";
  }

  function filaDeSubtarea(hija, propEstado, propFecha) {
    const fila = document.createElement("div");
    fila.className = "row-subtask";
    fila.dataset.rowId = hija.id;

    const abrir = document.createElement("button");
    abrir.type = "button";
    abrir.className = "row-subtask-title";
    abrir.textContent = hija.title || "Sin título";
    abrir.title = `Abrir «${hija.title || "sin título"}»`;
    abrir.addEventListener("click", () => (alNavegar || navigateToPage)(hija.id));
    fila.appendChild(abrir);

    const estadoEl = document.createElement("span");
    estadoEl.className = "row-subtask-state" + (hija.closed ? " is-closed" : "");
    estadoEl.innerHTML = propEstado
      ? formatPropValueHtml(hija.properties?.[propEstado.key], propEstado, hija)
      : hija.closed
        ? "Cerrada"
        : "—";
    fila.appendChild(estadoEl);

    const fecha = document.createElement("span");
    fecha.className = "row-subtask-date";
    fecha.textContent = fechaDeSubtarea(hija, propFecha);
    fila.appendChild(fecha);
    return fila;
  }

  /** «+ Añadir subtarea»: crea la fila YA enlazada (el id de esta fila va en la
   * celda de jerarquía). Se queda abierto para encadenar varios pasos. */
  function controlDeAlta(claveJerarquia) {
    const alta = document.createElement("div");
    alta.className = "row-subtasks-add";

    const boton = document.createElement("button");
    boton.type = "button";
    boton.className = "btn row-subtasks-add-btn";
    boton.textContent = "+ Añadir subtarea";

    const form = document.createElement("form");
    form.className = "row-subtasks-form";
    form.hidden = true;
    const input = document.createElement("input");
    input.type = "text";
    input.className = "row-subtasks-input";
    input.placeholder = "Título de la subtarea";
    input.setAttribute("aria-label", "Título de la subtarea nueva");
    const crear = document.createElement("button");
    crear.type = "submit";
    crear.className = "btn btn-primary";
    crear.textContent = "Añadir";
    const cancelar = document.createElement("button");
    cancelar.type = "button";
    cancelar.className = "btn";
    cancelar.textContent = "Cancelar";
    form.append(input, crear, cancelar);

    boton.addEventListener("click", () => {
      boton.hidden = true;
      form.hidden = false;
      input.value = "";
      input.focus();
    });
    cancelar.addEventListener("click", () => {
      form.hidden = true;
      boton.hidden = false;
      boton.focus();
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const nombre = input.value.trim();
      if (!nombre) {
        input.focus();
        return;
      }
      crear.disabled = true;
      try {
        ultimoCambioPropio = { databaseId: String(estado.database.id), ts: Date.now() };
        await api(`/databases/${estado.database.id}/rows`, {
          method: "POST",
          body: JSON.stringify({ title: nombre, properties: { [claveJerarquia]: String(page.id) } }),
        });
        await cargar();
        subtareasEl.querySelector(".row-subtasks-add-btn")?.click();
      } catch (err) {
        reportError("No se pudo crear la subtarea", err);
      } finally {
        crear.disabled = false;
      }
    });
    alta.append(boton, form);
    return alta;
  }

  function pintarSubtareas() {
    const { database, detalle } = estado;
    subtareasEl.textContent = "";
    const claveJerarquia = claveDeJerarquia(database);
    if (!claveJerarquia) return; // base sin jerarquía: ni rastro de la sección
    const propEstado = propiedadDeCierre(database);
    const propFecha = (database.schema_def || []).find((p) => p && p.key && p.type === "date") || null;

    const hijas = Array.isArray(detalle.subtasks) ? detalle.subtasks : [];
    const cerradas = hijas.filter((h) => h.closed).length;

    if (detalle.parent_title && detalle.parent_id) {
      const deQuien = document.createElement("p");
      deQuien.className = "row-subtasks-parent";
      const enlace = document.createElement("button");
      enlace.type = "button";
      enlace.className = "row-subtasks-parent-link";
      enlace.textContent = detalle.parent_title;
      enlace.addEventListener("click", () => (alNavegar || navigateToPage)(detalle.parent_id));
      deQuien.append("↳ Subtarea de ", enlace);
      subtareasEl.appendChild(deQuien);
    }

    const cabeza = document.createElement("div");
    cabeza.className = "row-subtasks-head";
    const rotulo = document.createElement("h2");
    rotulo.className = "row-subtasks-title";
    rotulo.textContent = "Subtareas";
    cabeza.appendChild(rotulo);
    if (hijas.length) {
      const cuenta = document.createElement("span");
      cuenta.className = "row-subtasks-count";
      cuenta.textContent = `${cerradas} de ${hijas.length}`;
      cuenta.title = `${cerradas} de ${hijas.length} subtareas cerradas`;
      cabeza.appendChild(cuenta);
    }
    subtareasEl.appendChild(cabeza);

    if (hijas.length) {
      const cabecera = document.createElement("div");
      cabecera.className = "row-subtasks-row is-head";
      ["Título", "Estado", "Fecha"].forEach((texto) => {
        const celda = document.createElement("span");
        celda.textContent = texto;
        cabecera.appendChild(celda);
      });
      subtareasEl.appendChild(cabecera);
      hijas.forEach((hija) => subtareasEl.appendChild(filaDeSubtarea(hija, propEstado, propFecha)));
    } else {
      const vacio = document.createElement("p");
      vacio.className = "row-subtasks-empty";
      vacio.textContent = "Todavía no tiene subtareas: los pasos de esta fila van aquí.";
      subtareasEl.appendChild(vacio);
    }
    subtareasEl.appendChild(controlDeAlta(claveJerarquia));
  }

  cargar();

  return {
    get database() {
      return estado.database;
    },
    /** Un aviso de otra pestaña (la fila o el esquema cambiaron). Si se está
     * escribiendo en el panel, se espera a que se suelte el campo. */
    refrescar() {
      if (!estado.vivo || !caja.isConnected) return;
      if (panelDeColumnaAbierto) {
        // El menú de una propiedad está abierto: repintar se llevaría su ancla.
        // Se repinta al cerrarlo (`cerrarPanelDeColumna`).
        refrescoDeBasesPendiente = true;
        return;
      }
      if (ocupado()) {
        caja.addEventListener("focusout", () => setTimeout(() => this.refrescar(), 0), { once: true });
        return;
      }
      cargar();
    },
    destruir() {
      estado.vivo = false;
    },
  };
}


// ------------------------------------------------ diálogo: base de datos
// Uno solo para el alta y para editar el schema de una base existente: mismas
// filas de propiedad y mismo botón de guardar. En edición la clave no se toca
// (las filas guardan sus valores por clave en el frontmatter): se añade o se
// quita la propiedad, no se renombra.

const dbDialog = document.getElementById("dialog-database");
const propRowsContainer = document.getElementById("prop-rows");
// `titleHidden` es el estado de la columna del título en el diálogo (se quita
// y se vuelve a poner sin cerrarlo); `database` es null al crear.
let dbDialogContext = null; // { database: Database|null, refresh, titleHidden, basesPrometidas }

/** Las bases del vault para el `<select>` de una relación del diálogo.
 *
 * Lista TODAS, también la propia: una relación de una base consigo misma es
 * válida y el servidor la admite, así que esconderla dejaría bases que no se
 * pueden guardar. Se pide una vez por diálogo y solo cuando hay una fila de
 * tipo Relación: abrir «Propiedades» de una base sin relaciones no la llama. */
function basesDelVaultParaElDialogo() {
  const ctx = dbDialogContext;
  if (!ctx) return Promise.resolve([]);
  if (!ctx.basesPrometidas) {
    ctx.basesPrometidas = api("/databases")
      .then((bases) => (Array.isArray(bases) ? bases : []))
      .catch((e) => {
        reportError("No se pudieron leer las bases del vault", e);
        return [];
      });
  }
  return ctx.basesPrometidas;
}

/** Apaga «Guardar» mientras alguna relación no tenga base destino y dice por
 * qué, en una línea.
 *
 * Sin destino la propiedad no se puede guardar: la celda de una relación se
 * valida contra la base a la que apunta (que la fila exista y sea de allí) y
 * sin ella no hay nada que validar. El botón apagado sin explicación sería un
 * callejón, así que el motivo va en `#db-save-hint`. */
function actualizarGuardadoDelDialogo() {
  const guardar = document.getElementById("btn-save-database");
  const aviso = document.getElementById("db-save-hint");
  if (!guardar || !aviso) return;
  const sinDestino = [];
  propRowsContainer.querySelectorAll(".prop-row-wrap:not(.prop-title-row)").forEach((wrap) => {
    const tipo = wrap.querySelector(".prop-type")?.value;
    const nombre = wrap.querySelector(".prop-name")?.value.trim() || "una propiedad";
    if (tipo === "relation" && !wrap.querySelector(".prop-target")?.value) {
      sinDestino.push(nombre);
    }
  });
  const avisos = [];
  if (sinDestino.length) {
    avisos.push(
      sinDestino.length === 1
        ? `Elige la base destino de «${sinDestino[0]}» para poder guardar.`
        : `Elige la base destino de ${sinDestino
            .map((n) => `«${n}»`)
            .join(", ")} para poder guardar.`,
    );
  }
  if (!avisos.length) {
    guardar.disabled = false;
    aviso.textContent = "";
    aviso.hidden = true;
    return;
  }
  aviso.hidden = false;
  aviso.textContent = avisos.join(" ");
  guardar.disabled = true;
}

/** La fila de la columna del título.
 *
 * El título es una columna más —se renombra y se quita—, pero su clave
 * (`title`) es de la fila y no viaja en el `schema_def`: por eso su nombre y su
 * visibilidad van aparte (`title_name`, `title_hidden`) y tiene su propia fila
 * en el diálogo. Al quitarla, la tabla deja de pintarla; cada fila se sigue
 * llamando por su `id`, que es su fichero.
 */
function addTitlePropertyRow(database) {
  const wrap = document.createElement("div");
  wrap.className = "prop-row-wrap prop-title-row";

  const row = document.createElement("div");
  row.className = "prop-row";

  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.className = "prop-name";
  nameInput.id = "title-prop-name";
  nameInput.placeholder = "Título";
  nameInput.setAttribute("aria-label", "Nombre de la columna del título");
  nameInput.value = database?.title_name || "Título";

  // El tipo no se elige: el título es texto. Se enseña igual que el de las
  // demás propiedades para que la fila se lea como una columna más.
  const tipo = document.createElement("span");
  tipo.className = "prop-type-fixed";
  tipo.textContent = tipoVisible("text");

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "icon-btn";
  toggle.id = "btn-title-prop";

  const aplicar = () => {
    const oculta = Boolean(dbDialogContext?.titleHidden);
    toggle.innerHTML = svgIcon(oculta ? "i-plus" : "i-x");
    toggle.title = oculta
      ? "Volver a mostrar la columna del título"
      : "Quitar la columna del título (las filas se siguen llamando por su id)";
    toggle.setAttribute("aria-label", toggle.title);
    nameInput.disabled = oculta;
    wrap.classList.toggle("is-off", oculta);
  };
  toggle.addEventListener("click", () => {
    dbDialogContext.titleHidden = !dbDialogContext.titleHidden;
    aplicar();
  });
  aplicar();

  row.append(nameInput, tipo, toggle);
  wrap.appendChild(row);
  propRowsContainer.appendChild(wrap);
}

/** Añade una fila de propiedad al diálogo. Con `prop`, la rellena (edición).
 *
 * Solo se pide el **nombre**: la clave snake_case se deriva en el servidor
 * (`app.properties.slug_key`) y no se enseña en ningún sitio. La clave que
 * tenía la propiedad viaja en el nodo (`__propKey`) porque es su identidad: si
 * el nombre cambia, el servidor sabe qué migrar en las filas y en las vistas.
 * No se escribe en el HTML: la clave interna no se enseña en ningún sitio.
 */
function addPropertyRow(prop = null) {
  const wrap = document.createElement("div");
  wrap.className = "prop-row-wrap";
  // La clave que tenía la propiedad es su identidad y solo la usa este
  // diálogo: va en una propiedad del nodo, no en un atributo `data-*`, para
  // que la clave interna no aparezca ni en el HTML del diálogo.
  if (prop?.key) wrap.__propKey = prop.key;

  const row = document.createElement("div");
  row.className = "prop-row";

  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.placeholder = "Nombre de la propiedad";
  nameInput.className = "prop-name";
  nameInput.setAttribute("aria-label", "Nombre de la propiedad");

  const typeSelect = document.createElement("select");
  typeSelect.className = "prop-type";
  typeSelect.setAttribute("aria-label", "Tipo de la propiedad");
  Object.entries(TIPOS_VISIBLES).forEach(([value, label]) => {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    typeSelect.appendChild(opt);
  });

  const removeBtn = document.createElement("button");
  removeBtn.type = "button";
  removeBtn.innerHTML = svgIcon("i-x");
  removeBtn.className = "icon-btn";
  removeBtn.title = "Quitar esta propiedad";
  removeBtn.setAttribute("aria-label", "Quitar esta propiedad");

  row.append(nameInput, typeSelect, removeBtn);

  const optionsInput = document.createElement("input");
  optionsInput.placeholder = "opciones separadas por coma (Selección / Multiselección)";
  optionsInput.className = "prop-options-input";
  optionsInput.hidden = true;

  // Orden de los valores de una selección: `auto` (alfabético natural) o
  // `manual` (la lista de abajo, que se arrastra). Es la misma decisión que se
  // guarda en el `schema` (`sort_mode`/`sort_order`) y la que usa el tablero
  // para poner sus columnas, así que no hay dos verdades sobre el orden.
  const sortWrap = document.createElement("div");
  sortWrap.className = "prop-sort";
  sortWrap.hidden = true;

  const sortModeLabel = document.createElement("label");
  sortModeLabel.className = "prop-sort-mode";
  const sortModeText = document.createElement("span");
  sortModeText.textContent = "Orden de los valores";
  const sortModeSelect = document.createElement("select");
  sortModeSelect.className = "prop-sort-mode-select";
  sortModeSelect.setAttribute("aria-label", "Modo de orden de los valores");
  [
    ["auto", "Automático (alfabético)"],
    ["manual", "Manual (arrastra los valores)"],
  ].forEach(([valor, texto]) => {
    const opcion = document.createElement("option");
    opcion.value = valor;
    opcion.textContent = texto;
    sortModeSelect.appendChild(opcion);
  });
  sortModeLabel.append(sortModeText, sortModeSelect);

  const sortHint = document.createElement("p");
  sortHint.className = "prop-sort-hint";
  const sortList = document.createElement("ul");
  sortList.className = "prop-sort-list";
  sortList.setAttribute("aria-label", "Orden manual de los valores");
  sortWrap.append(sortModeLabel, sortHint, sortList);

  // El orden manual que se está editando y lo que ya venía declarado en el
  // `.md` (puede nombrar valores que ya no están en `options`: no se pierden).
  // Se arranca con el orden declarado (no con el de `options`): abrir el
  // diálogo y guardar sin tocar nada no puede cambiar el orden que ya había.
  wrap.__orden = [...(prop?.sort_order || [])];
  let arrastrandoValor = null;

  /** Los valores de `options` como están ahora en el campo de texto, sin
   * repetir y con la grafía del primero. */
  const valoresDeOpciones = () => {
    const vistos = new Set();
    const valores = [];
    optionsInput.value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
      .forEach((valor) => {
        const clave = valor.toLowerCase();
        if (vistos.has(clave)) return;
        vistos.add(clave);
        valores.push(valor);
      });
    return valores;
  };

  /** El orden vigente de los valores de `options`: primero los que ya estaban
   * ordenados y luego los nuevos, al final. */
  const ordenVigente = () => {
    const valores = valoresDeOpciones();
    const claves = new Set(valores.map((v) => v.toLowerCase()));
    const previos = (wrap.__orden || []).filter((v) => claves.has(String(v).toLowerCase()));
    const ya = new Set(previos.map((v) => String(v).toLowerCase()));
    return [...previos, ...valores.filter((v) => !ya.has(v.toLowerCase()))];
  };

  const pintarOrden = () => {
    const manual = sortModeSelect.value === "manual";
    sortList.hidden = !manual;
    sortHint.hidden = !manual;
    sortHint.textContent = manual
      ? "Arrastra una fila (o usa ↑ / ↓) para cambiar el orden en que se leen y se agrupan los valores."
      : "Se ordenan alfabéticamente, sin distinguir mayúsculas ni tildes.";
    sortList.textContent = "";
    if (!manual) return;
    wrap.__orden = ordenVigente();
    wrap.__orden.forEach((valor, indice) => {
      const item = document.createElement("li");
      item.className = "prop-sort-item";
      item.draggable = true;
      const asa = document.createElement("span");
      asa.className = "prop-sort-grip";
      asa.textContent = "⠿";
      asa.setAttribute("aria-hidden", "true");
      const etiqueta = document.createElement("span");
      etiqueta.className = "prop-sort-value";
      etiqueta.textContent = valor;
      item.append(asa, etiqueta);
      const mover = (direccion, texto) => {
        const boton = document.createElement("button");
        boton.type = "button";
        boton.className = "prop-sort-move";
        boton.textContent = direccion < 0 ? "↑" : "↓";
        boton.title = texto;
        boton.setAttribute("aria-label", texto);
        boton.disabled = direccion < 0 ? indice === 0 : indice === wrap.__orden.length - 1;
        boton.addEventListener("click", () => {
          const destino = wrap.__orden[indice + direccion];
          if (destino === undefined) return;
          wrap.__orden = moverEnLista(wrap.__orden, valor, destino);
          pintarOrden();
          actualizarGuardadoDelDialogo();
        });
        return boton;
      };
      item.append(mover(-1, `Subir «${valor}»`), mover(1, `Bajar «${valor}»`));
      item.addEventListener("dragstart", (e) => {
        arrastrandoValor = valor;
        item.classList.add("is-dragging");
        if (e.dataTransfer) {
          e.dataTransfer.effectAllowed = "move";
          try {
            e.dataTransfer.setData("text/plain", valor);
          } catch {
            // hay navegadores que no dejan tocar dataTransfer en dragstart
          }
        }
      });
      item.addEventListener("dragend", () => {
        arrastrandoValor = null;
        sortList.querySelectorAll(".is-dragging, .is-dragover").forEach((n) => {
          n.classList.remove("is-dragging", "is-dragover");
        });
      });
      item.addEventListener("dragover", (e) => {
        if (arrastrandoValor === null) return;
        e.preventDefault();
        item.classList.add("is-dragover");
      });
      item.addEventListener("dragleave", () => item.classList.remove("is-dragover"));
      item.addEventListener("drop", (e) => {
        if (arrastrandoValor === null) return;
        e.preventDefault();
        const movido = arrastrandoValor;
        arrastrandoValor = null;
        if (mismoValorDeColumna(movido, valor)) return;
        wrap.__orden = moverEnLista(wrap.__orden, movido, valor);
        pintarOrden();
        actualizarGuardadoDelDialogo();
      });
      sortList.appendChild(item);
    });
  };

  // Relación: a qué base apunta (`target_database`) y si enlaza una fila o
  // varias (`multiple`). Solo se enseña con el tipo Relación.
  const relWrap = document.createElement("div");
  relWrap.className = "prop-relation";
  relWrap.hidden = true;

  const targetLabel = document.createElement("label");
  targetLabel.className = "prop-relation-label";
  const targetText = document.createElement("span");
  targetText.textContent = "Base destino";
  const targetSelect = document.createElement("select");
  targetSelect.className = "prop-target";
  targetSelect.setAttribute("aria-label", "Base a la que apunta la relación");
  targetLabel.append(targetText, targetSelect);

  const multipleLabel = document.createElement("label");
  multipleLabel.className = "prop-relation-multiple";
  const multipleBox = document.createElement("input");
  multipleBox.type = "checkbox";
  multipleBox.className = "prop-multiple";
  multipleBox.checked = relacionMultiple(prop);
  multipleLabel.append(multipleBox, document.createTextNode(" Permite varias filas"));

  relWrap.append(targetLabel, multipleLabel);

  // El cierre NO se configura aquí: qué cuenta como fila cerrada es una regla
  // de la BASE (`closed: {property, values}`) y se marca en el menú de la
  // columna («Esta columna dice si está hecha»). Tenerlo en los dos sitios
  // serían dos verdades sobre la misma pregunta.
  let destinoPedido = false;
  /** Rellena el `<select>` la primera vez que hace falta: una base sin
   * relaciones no pide la lista de bases. */
  const llenarDestino = () => {
    if (destinoPedido) return;
    destinoPedido = true;
    const actual = String(prop?.target_database || "");
    const vacio = document.createElement("option");
    vacio.value = "";
    vacio.textContent = "Elige una base…";
    targetSelect.appendChild(vacio);
    basesDelVaultParaElDialogo().then((bases) => {
      const hayActual = bases.some((b) => String(b.id) === actual);
      bases.forEach((b) => {
        const opt = document.createElement("option");
        opt.value = String(b.id);
        opt.textContent = b.title || "Sin título";
        targetSelect.appendChild(opt);
      });
      // Un destino que ya no está en el vault no se puede ofrecer por nombre:
      // se queda como opción (no se pierde lo que dice el `.md`) y se puede
      // cambiar por otra.
      if (actual && !hayActual) {
        const rota = document.createElement("option");
        rota.value = actual;
        rota.textContent = "Base ya no disponible (elige otra)";
        targetSelect.appendChild(rota);
      }
      targetSelect.value = actual;
      actualizarGuardadoDelDialogo();
    });
  };

  const aplicarVisibilidad = () => {
    const tipo = typeSelect.value;
    const esRelacion = tipo === "relation";
    const esSeleccion = ["select", "multiselect"].includes(tipo);
    relWrap.hidden = !esRelacion;
    optionsInput.hidden = !esSeleccion;
    sortWrap.hidden = !esSeleccion;
    if (esRelacion) llenarDestino();
    if (esSeleccion) pintarOrden();
  };

  if (prop) {
    nameInput.value = prop.name;
    typeSelect.value = prop.type;
    optionsInput.value = (prop.options || []).join(", ");
    optionsInput.hidden = !["select", "multiselect"].includes(prop.type);
    sortModeSelect.value = modoDeOrden(prop);
  }

  typeSelect.addEventListener("change", () => {
    aplicarVisibilidad();
    actualizarGuardadoDelDialogo();
  });
  nameInput.addEventListener("input", () => actualizarGuardadoDelDialogo());
  targetSelect.addEventListener("change", () => actualizarGuardadoDelDialogo());
  // Al quitar una fila de propiedad, el aviso y el botón se recalculan: la
  // relación incompleta que bloqueaba puede ser justo la que se ha quitado.
  removeBtn.addEventListener("click", () => {
    wrap.remove();
    actualizarGuardadoDelDialogo();
  });

  optionsInput.addEventListener("input", () => {
    wrap.__orden = ordenVigente();
    pintarOrden();
  });
  sortModeSelect.addEventListener("change", () => {
    pintarOrden();
    actualizarGuardadoDelDialogo();
  });
  wrap.append(row, optionsInput, sortWrap, relWrap);
  propRowsContainer.appendChild(wrap);
  aplicarVisibilidad();
}


// --------------------------------------------------- plantillas (función 29)
// Una plantilla es parte de la base: el catálogo de serie vive en el servidor
// (`GET /databases/templates`) y las plantillas de fila en el `.md` de cada
// base (`row_templates:`). Aquí solo se eligen, se aplican y se editan.

let plantillasDeSerie = null;

async function cargarPlantillasDeSerie() {
  if (!plantillasDeSerie) plantillasDeSerie = await api("/databases/templates");
  return plantillasDeSerie;
}

/** Las notas donde puede nacer una base (la carpeta destino es una nota).
 * En la raíz la base vive en `Bases/`; colgando de una nota, en su carpeta. */
function notasParaCarpetaDePlantilla() {
  return [...state.pagesById.values()]
    .filter((n) => n && n.type === "note")
    .sort((a, b) =>
      String(a.title || "").localeCompare(String(b.title || ""), "es", {
        sensitivity: "base",
      }),
    );
}

/** «Crear desde plantilla»: nombre, carpeta destino y una plantilla de serie.
 *
 * El servidor valida la plantilla entera antes de escribir nada y contesta un
 * 400 con el motivo si algo no encaja; el diálogo se queda abierto con esa
 * frase a la vista y no se crea nada a medias.
 */
async function openTemplateDialog() {
  const dialog = document.getElementById("dialog-template");
  const lista = document.getElementById("template-list");
  const nombre = document.getElementById("template-name");
  const carpeta = document.getElementById("template-folder");
  const crear = document.getElementById("btn-create-template");
  const cancelar = document.getElementById("btn-cancel-template");
  const hint = document.getElementById("template-hint");
  hint.hidden = true;
  hint.textContent = "";

  let catalogo;
  try {
    catalogo = await cargarPlantillasDeSerie();
  } catch (e) {
    reportError("No se pudieron cargar las plantillas", e);
    return;
  }

  let elegida = catalogo[0]?.key || null;
  const pintarSeleccion = () => {
    lista.querySelectorAll(".template-option").forEach((opcion) => {
      opcion.setAttribute(
        "aria-checked",
        opcion.dataset.key === elegida ? "true" : "false",
      );
    });
    crear.disabled = !nombre.value.trim() || !elegida;
  };

  lista.innerHTML = "";
  catalogo.forEach((plantilla) => {
    const opcion = document.createElement("button");
    opcion.type = "button";
    opcion.className = "template-option";
    opcion.dataset.key = plantilla.key;
    opcion.setAttribute("role", "radio");
    const titulo = document.createElement("span");
    titulo.className = "template-option-name";
    titulo.textContent = plantilla.title;
    const descripcion = document.createElement("span");
    descripcion.className = "template-option-desc";
    descripcion.textContent = plantilla.description || "";
    const meta = document.createElement("span");
    meta.className = "template-option-meta";
    meta.textContent = `${plantilla.properties.length} propiedades · ${(
      plantilla.views || []
    ).join(", ")}`;
    opcion.append(titulo, descripcion, meta);
    opcion.addEventListener("click", () => {
      elegida = plantilla.key;
      pintarSeleccion();
      if (!nombre.dataset.tocado) {
        nombre.placeholder = `Mi ${plantilla.title.toLowerCase()}`;
      }
    });
    lista.appendChild(opcion);
  });

  carpeta.innerHTML = "";
  const raiz = document.createElement("option");
  raiz.value = "";
  raiz.textContent = "Bases (raíz)";
  carpeta.appendChild(raiz);
  notasParaCarpetaDePlantilla().forEach((nota) => {
    const opcion = document.createElement("option");
    opcion.value = nota.id;
    opcion.textContent = nota.title || "Sin título";
    carpeta.appendChild(opcion);
  });

  nombre.value = "";
  delete nombre.dataset.tocado;
  nombre.placeholder = catalogo[0]
    ? `Mi ${catalogo[0].title.toLowerCase()}`
    : "Mi proyecto";
  pintarSeleccion();

  nombre.oninput = () => {
    nombre.dataset.tocado = "1";
    pintarSeleccion();
  };
  cancelar.onclick = () => dialog.close();
  crear.onclick = async () => {
    const titulo = nombre.value.trim();
    if (!titulo) {
      nombre.focus();
      return;
    }
    crear.disabled = true;
    hint.hidden = true;
    try {
      const base = await api("/databases/from-template", {
        method: "POST",
        body: JSON.stringify({
          template: elegida,
          title: titulo,
          ...(carpeta.value ? { parent_id: carpeta.value } : {}),
        }),
      });
      dialog.close();
      await loadTree();
      await goTo("database", base.id);
    } catch (e) {
      hint.textContent = describeError(e);
      hint.hidden = false;
      crear.disabled = false;
    }
  };
  dialog.showModal();
  nombre.focus();
}

/** Un campo de valor por defecto de una plantilla de fila.
 *
 * Reusa `buildPropertyInput`, que es el único sitio que decide qué control usa
 * cada tipo y cómo se lee, para que lo que se guarda aquí sea exactamente lo
 * que se escribiría en una fila. */
function campoDeValorPorDefecto(prop, valor, indice) {
  const campo = document.createElement("div");
  campo.className = "field";
  const label = document.createElement("label");
  label.textContent = prop.name;
  const { input, read } = buildPropertyInput(prop, valor);
  const id = idDeCampo("plantilla-fila", indice, prop.key);
  input.id = id;
  label.htmlFor = id;
  campo.append(label, input);
  return { campo, prop, read };
}

/** Lo que una tarjeta del editor de plantillas de fila manda al guardar: su
 * nombre, su cuerpo y los valores por defecto que sus controles pueden
 * representar. Es el único sitio que decide qué viaja —el guardado y la lista
 * de lo que se descarta usan esto mismo—, para que el aviso y lo que se escribe
 * no puedan decir cosas distintas. */
function leerPlantillaDeTarjeta(card) {
  const properties = {};
  for (const { prop, read } of card.__lectores) {
    const valor = read();
    if (valor === null || valor === undefined || valor === "") continue;
    if (Array.isArray(valor) && valor.length === 0) continue;
    // Un checkbox sin marcar que nadie ha tocado no se guarda como `false`:
    // sería inventarle un valor por defecto a una propiedad que la plantilla
    // no mencionaba. Si estaba declarado, el `false` es del usuario.
    if (prop.type === "checkbox" && valor === false && !card.__previos.has(prop.key)) {
      continue;
    }
    properties[prop.key] = valor;
  }
  return { name: card.__nombre.value.trim(), properties, body: card.__cuerpo.value };
}

/** Los valores que el `.md` de la base declara y el editor no puede pintar: una
 * opción que ya no existe en la columna, una columna que se borró. Al guardar
 * no se reenvían —el servidor los rechazaría, y son del fichero del usuario, no
 * de este diálogo—, así que se pierden: se nombran aquí, antes de Guardar, en
 * vez de desaparecer porque alguien estaba renombrando otra tarjeta. */
function valoresQueSeDescartan(card, schema) {
  const { properties } = leerPlantillaDeTarjeta(card);
  return Object.entries(card.__declarados)
    .filter(([key]) => !(key in properties))
    .map(([key, valor]) => {
      const nombre = schema.find((p) => p.key === key)?.name || key;
      return `${nombre} = «${valor}»`;
    });
}

/** Una tarjeta del editor de plantillas de fila: nombre, valores por defecto
 * de las propiedades que admiten literal y cuerpo inicial. */
function plantillaDeFilaEditable(template, indice, schema, alQuitar) {
  const card = document.createElement("div");
  card.className = "row-template-card";

  const head = document.createElement("div");
  head.className = "row-template-head";
  const campoNombre = document.createElement("div");
  campoNombre.className = "field";
  const labelNombre = document.createElement("label");
  labelNombre.textContent = "Nombre";
  const nombre = document.createElement("input");
  nombre.type = "text";
  nombre.className = "row-template-name";
  nombre.value = template?.name || "";
  const idNombre = idDeCampo("plantilla-fila-nombre", indice, "nombre");
  nombre.id = idNombre;
  labelNombre.htmlFor = idNombre;
  campoNombre.append(labelNombre, nombre);

  const quitar = document.createElement("button");
  quitar.type = "button";
  quitar.className = "icon-btn row-template-remove";
  quitar.title = "Quitar plantilla";
  quitar.setAttribute("aria-label", "Quitar plantilla");
  quitar.textContent = "×";
  quitar.addEventListener("click", () => alQuitar(card));
  head.append(campoNombre, quitar);
  card.appendChild(head);

  // Lo que la plantilla declara ya coaccionado por el servidor: pintar
  // `properties` (el texto del `.md`) marcaba la casilla de un `false`.
  const declarados =
    template && template.values ? template.values : (template?.properties || {});

  // Una relación, una subtarea o una persona no pueden traer un valor por
  // defecto: se guardan por ids que no existen hasta que hay filas o usuarios.
  const editables = schema.filter(
    (prop) => !["relation", "parent_row", "person"].includes(prop.type),
  );
  const lectores = [];
  if (editables.length) {
    const defaults = document.createElement("div");
    defaults.className = "row-template-defaults";
    editables.forEach((prop) => {
      const { campo, read } = campoDeValorPorDefecto(
        prop,
        declarados[prop.key],
        indice,
      );
      defaults.appendChild(campo);
      lectores.push({ prop, read });
    });
    card.appendChild(defaults);
  }

  const labelCuerpo = document.createElement("label");
  labelCuerpo.className = "row-template-body-label";
  labelCuerpo.textContent = "Cuerpo inicial";
  const cuerpo = document.createElement("textarea");
  cuerpo.className = "row-template-body";
  cuerpo.rows = 3;
  cuerpo.value = template?.body || "";
  labelCuerpo.appendChild(cuerpo);
  card.appendChild(labelCuerpo);

  card.__nombre = nombre;
  card.__cuerpo = cuerpo;
  card.__lectores = lectores;
  // Lo que la plantilla YA declaraba: un checkbox sin marcar que nadie ha
  // tocado no se guarda como `false` (sería inventarle un valor por defecto a
  // una propiedad que la plantilla no mencionaba). Si estaba declarado, el
  // `false` es lo que el usuario acaba de elegir y sí se guarda.
  card.__previos = new Set([
    ...Object.keys(template?.values || {}),
    ...Object.keys(template?.properties || {}),
  ]);
  // Lo que el `.md` declara tal cual, que es lo que se compara con lo que los
  // controles pueden devolver para saber qué se queda fuera al guardar.
  card.__declarados = template?.properties || {};

  // Un valor que la plantilla declara y ningún control de aquí puede pintar se
  // pierde al guardar (el `.md` se reescribe con lo que el diálogo enseña). Se
  // dice en la tarjeta, con su propiedad y su valor, y se recalcula mientras se
  // edita: si el usuario lo arregla —o vacía el control— la línea se va.
  const descartes = document.createElement("p");
  descartes.className = "db-save-hint row-template-warning row-template-dropped";
  descartes.setAttribute("role", "status");
  descartes.hidden = true;
  // El motivo que da el servidor habla de lo que el `.md` tiene ahora: en cuanto
  // el valor se arregla en su control deja de ser verdad, así que se apaga con
  // la línea de descarte (las dos describen lo mismo: qué pasa al guardar).
  let avisoMotivo = null;
  card.__pintarDescartes = () => {
    // Sin nombre la tarjeta no se guarda: no hay nada que perder que decir.
    const perdidos = card.__nombre.value.trim()
      ? valoresQueSeDescartan(card, schema)
      : [];
    descartes.textContent = perdidos.length
      ? `Guardar quitará de «${card.__nombre.value.trim()}»: ${perdidos.join(", ")}.`
      : "";
    descartes.hidden = !perdidos.length;
    if (avisoMotivo) avisoMotivo.hidden = !perdidos.length;
  };
  card.addEventListener("input", card.__pintarDescartes);
  card.addEventListener("change", card.__pintarDescartes);
  card.insertBefore(descartes, head.nextSibling);

  // Una plantilla que ya no encaja con la base se edita igual, pero con el
  // motivo a la vista: lo que el formulario no puede pintar no se arregla desde
  // aquí, y lo que se queda fuera al guardar lo dice la línea de arriba.
  if (template?.error) {
    avisoMotivo = document.createElement("p");
    avisoMotivo.className = "db-save-hint row-template-warning";
    avisoMotivo.setAttribute("role", "status");
    avisoMotivo.textContent =
      "Esta plantilla ya no encaja con la base: " + template.error;
    card.insertBefore(avisoMotivo, descartes);
  }
  card.__pintarDescartes();
  return card;
}

/** «Plantillas de fila» (menú de la base): lo que se guarda es parte del `.md`
 * de la base, no una preferencia de esta pestaña. Al guardar, `refresh` relee
 * la base para que el «+ Nueva fila» ofrezca las plantillas nuevas. */
function openRowTemplatesDialog(database, refresh = null) {
  const dialog = document.getElementById("dialog-row-templates");
  const lista = document.getElementById("row-templates-list");
  const hint = document.getElementById("row-templates-hint");
  const guardar = document.getElementById("btn-save-row-templates");
  const schema = (database.schema_def || []).filter((p) => p && p.key);
  hint.hidden = true;
  hint.textContent = "";
  lista.innerHTML = "";

  const alQuitar = (card) => card.remove();
  // Contador propio, no el número de tarjetas: quitar una y añadir otra
  // reutilizaba el índice y las etiquetas de la nueva apuntaban a los controles
  // de la primera (`id` repetidos en el documento).
  let siguiente = 0;
  // Lo que la última pulsación de Guardar avisó que se perdería: la siguiente
  // pulsación guarda solo si el descarte es exactamente el mismo (si se toca
  // algo, cambia la firma y se vuelve a avisar).
  let descarteAvisado = null;
  const anadir = (template) => {
    lista.appendChild(
      plantillaDeFilaEditable(template, siguiente++, schema, alQuitar),
    );
  };
  (database.row_templates || []).forEach((t) => anadir(t));
  if (!(database.row_templates || []).length) anadir(null);

  document.getElementById("btn-add-row-template").onclick = () => {
    const card = plantillaDeFilaEditable(null, siguiente++, schema, alQuitar);
    lista.appendChild(card);
    card.querySelector(".row-template-name").focus();
  };
  document.getElementById("btn-cancel-row-templates").onclick = () => dialog.close();
  guardar.onclick = async () => {
    const row_templates = [];
    for (const card of [...lista.querySelectorAll(".row-template-card")]) {
      const { name, properties, body } = leerPlantillaDeTarjeta(card);
      if (!name) {
        // Una tarjeta vacía no es una plantilla; una con contenido y sin nombre
        // no se puede elegir en el alta, así que se dice y no se guarda.
        if (body.trim() || Object.keys(properties).length) {
          hint.textContent = "Cada plantilla necesita un nombre.";
          hint.hidden = false;
          card.__nombre.focus();
          return;
        }
        continue;
      }
      row_templates.push({ name, properties, body });
    }
    // Las plantillas que van a perder valores lo avisan en su tarjeta, pero la
    // tarjeta puede haberse quedado fuera de la vista del scroll: perder un
    // dato no puede depender de haberlo leído, así que la primera pulsación de
    // Guardar no escribe nada y lo dice aquí, pegado al botón. La segunda, con
    // el mismo descarte, guarda.
    const sePierden = [...lista.querySelectorAll(".row-template-card")]
      .filter((card) => card.__nombre.value.trim())
      .map((card) => {
        const perdidos = valoresQueSeDescartan(card, schema);
        return perdidos.length
          ? `«${card.__nombre.value.trim()}»: ${perdidos.join(", ")}`
          : null;
      })
      .filter(Boolean);
    const descarte = sePierden.join(" · ");
    if (descarte && descarte !== descarteAvisado) {
      descarteAvisado = descarte;
      hint.textContent = `Guardar quitará ${descarte}. Pulsa Guardar otra vez para confirmar.`;
      hint.hidden = false;
      return;
    }
    descarteAvisado = null;
    hint.hidden = true;
    hint.textContent = "";
    guardar.disabled = true;
    try {
      const actualizada = await api(`/databases/${database.id}/row-templates`, {
        method: "PUT",
        body: JSON.stringify({ row_templates }),
      });
      database.row_templates = actualizada.row_templates;
      dialog.close();
      if (refresh) await refresh();
    } catch (e) {
      // El servidor dice qué valor o qué clave no existe; el diálogo sigue
      // abierto y el `.md` no se ha tocado.
      hint.textContent = describeError(e);
      hint.hidden = false;
    } finally {
      guardar.disabled = false;
    }
  };
  dialog.showModal();
}

document.getElementById("btn-new-from-template").addEventListener("click", () => {
  openTemplateDialog().catch((e) =>
    reportError("No se pudo abrir «Crear desde plantilla»", e),
  );
});

/** Abre el diálogo en alta (`database` nulo) o para editar una existente.
 * `refresh` repinta la vista actual cuando el PATCH del schema termina; el
 * diálogo recibe uno que relee las vistas, porque el servidor ha podido
 * reescribir su configuración al migrar el rename. */
function openDatabaseDialog(database = null, refresh = null) {
  dbDialogContext = {
    database,
    refresh,
    titleHidden: Boolean(database?.title_hidden),
    basesPrometidas: null,
  };
  document.getElementById("db-dialog-title").textContent = database
    ? "Propiedades de la base de datos"
    : "Nueva base de datos";
  document.getElementById("btn-save-database").textContent = database ? "Guardar" : "Crear";
  document.getElementById("db-name").value = database ? database.title : "";
  propRowsContainer.innerHTML = "";
  addTitlePropertyRow(database);
  // Sin propiedades de serie: una base nueva nace con `schema_def: []`. Para
  // añadir una columna está el botón «Propiedad».
  (database?.schema_def || []).forEach((p) => addPropertyRow(p));
  actualizarGuardadoDelDialogo();
  dbDialog.showModal();
}

/** El `schema_def` que describen las filas del diálogo, en el formato de la API. */
function readSchemaFromDialog() {
  const schema_def = [];
  propRowsContainer.querySelectorAll(".prop-row-wrap:not(.prop-title-row)").forEach((wrap) => {
    const name = wrap.querySelector(".prop-name").value.trim();
    const type = wrap.querySelector(".prop-type").value;
    const optionsRaw = wrap.querySelector(".prop-options-input").value.trim();
    const sortMode = wrap.querySelector(".prop-sort-mode-select")?.value || "auto";
    // Una fila sin nombre no es una propiedad: no se inventa una clave con el
    // tipo por nombre (y así el botón «Propiedad» puede quedarse vacío).
    if (!name) return;
    const prop = { name, type };
    if (wrap.__propKey) prop.key = wrap.__propKey;
    if (["select", "multiselect"].includes(type)) {
      if (optionsRaw) {
        prop.options = optionsRaw.split(",").map((s) => s.trim()).filter(Boolean);
      }
      // El modo SIEMPRE viaja: en `auto` el servidor suelta el `sort_order`
      // (en automático el orden es el alfabético) y en `manual` guarda la
      // lista en el orden en que se ha quedado — solo con los valores que
      // siguen en `options`, que son los que el diálogo enseña. Un valor
      // retirado sale del orden declarado y cae al final al ordenar (el tramo
      // «fuera del orden declarado»); así lo que se ve y lo que se escribe en
      // el `.md` coinciden y no queda un valor escondido que no se puede
      // mover ni quitar.
      prop.sort_mode = sortMode === "manual" ? "manual" : "auto";
      if (sortMode === "manual") {
        const enOpciones = new Set((prop.options || []).map((v) => v.toLowerCase()));
        prop.sort_order = (wrap.__orden || []).filter((v) =>
          enOpciones.has(String(v).toLowerCase()),
        );
      }
    }
    if (type === "relation") {
      // Se manda SIEMPRE lo que dice el `<select>`, también vacío: el servidor
      // conserva el destino de antes cuando la clave no viaja, y aquí lo que
      // vale es lo que el usuario ve en el diálogo.
      prop.target_database = wrap.querySelector(".prop-target")?.value || "";
      prop.multiple = Boolean(wrap.querySelector(".prop-multiple")?.checked);
    }
    schema_def.push(prop);
  });
  return schema_def;
}

/** El nombre y la visibilidad de la columna del título tal y como están en el
 * diálogo. Vaciar el nombre no la deja sin nombre: vuelve a «Título». */
function readTitleColumnFromDialog() {
  const input = document.getElementById("title-prop-name");
  const title_name = (input?.value || "").trim() || "Título";
  return { title_name, title_hidden: Boolean(dbDialogContext?.titleHidden) };
}

document.getElementById("btn-add-prop").addEventListener("click", () => {
  addPropertyRow();
  actualizarGuardadoDelDialogo();
});
document.getElementById("btn-cancel-database").addEventListener("click", () => {
  dbDialogContext = null;
  dbDialog.close();
});

document.getElementById("btn-save-database").addEventListener("click", async () => {
  const title = document.getElementById("db-name").value.trim() || "Sin título";
  const schema_def = readSchemaFromDialog();
  const { title_name, title_hidden } = readTitleColumnFromDialog();
  const { database, refresh } = dbDialogContext || {};
  // El botón ya está apagado; esto es la red por si se llega por otra vía
  // (Enter en un campo). El motivo lo escribe `actualizarGuardadoDelDialogo`.
  if (schema_def.some((p) => p.type === "relation" && !p.target_database)) {
    actualizarGuardadoDelDialogo();
    return;
  }

  if (!database) {
    try {
      const created = await api("/databases", { method: "POST", body: JSON.stringify({ title, schema_def }) });
      // La columna del título también se renombra/quita desde el diálogo de
      // alta: solo se manda el PATCH si no son los valores de serie.
      if (title_name !== "Título" || title_hidden) {
        await api(`/databases/${created.id}`, {
          method: "PATCH",
          body: JSON.stringify({ title_name, title_hidden }),
        });
      }
      dbDialogContext = null;
      dbDialog.close();
      await loadTree();
      await goTo("database", created.id);
    } catch (e) {
      reportError("No se pudo crear la base de datos", e);
    }
    return;
  }

  // Editar el schema es escribir `schema` en el `.md` de la base (PATCH). El
  // servidor deriva las claves nuevas y migra las filas y las vistas de lo que
  // se renombre; si falla, el diálogo sigue abierto y no ha cambiado nada.
  try {
    const updated = await api(`/databases/${database.id}`, {
      method: "PATCH",
      body: JSON.stringify({ title, schema_def, title_name, title_hidden }),
    });
    Object.assign(database, updated); // el título puede haber renombrado el fichero
  } catch (e) {
    reportError("No se pudo guardar el esquema", e);
    return;
  }
  dbDialogContext = null;
  dbDialog.close();
  await loadTree();
  markActive(database.id);
  // El repintado que llega aquí relee las vistas: el servidor ha podido
  // reescribir `group_by`/`date_property`/… al migrar el rename.
  await refresh?.();
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
    // Una relación múltiple no puede ser columna de un tablero: una fila con
    // varias filas enlazadas caería en varias a la vez.
    if (p.type === "relation" && relacionMultiple(p)) return;
    const opt = document.createElement("option");
    opt.value = p.key;
    opt.textContent = p.name;
    groupSelect.appendChild(opt);
  });
  // Las fechas del calendario: solo las propiedades de tipo fecha, que son las
  // únicas que colocan una fila en un día.
  const dateSelect = document.getElementById("view-date");
  dateSelect.innerHTML = "";
  database.schema_def
    .filter((p) => p.type === "date")
    .forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.key;
      opt.textContent = p.name;
      dateSelect.appendChild(opt);
    });
  // El fin del rango: «—» (una fila, un día) o una segunda fecha. Se rellena
  // igual que la primera y se elige cuál es el fin al crear.
  const endSelect = document.getElementById("view-date-end");
  endSelect.innerHTML = "";
  const sinFin = document.createElement("option");
  sinFin.value = "";
  sinFin.textContent = "— (un día)";
  endSelect.appendChild(sinFin);
  database.schema_def
    .filter((p) => p.type === "date")
    .forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.key;
      opt.textContent = p.name;
      endSelect.appendChild(opt);
    });
  document.getElementById("view-groupby-field").hidden = true;
  document.getElementById("view-date-field").hidden = true;
  document.getElementById("view-date-end-field").hidden = true;
  viewDialog.showModal();
}

document.getElementById("view-type").addEventListener("change", (e) => {
  document.getElementById("view-groupby-field").hidden = e.target.value !== "board";
  // Sin propiedades de fecha no hay nada que elegir: el calendario se crea
  // igual y dentro dice que a la base le falta una fecha.
  const hayFechas = document.getElementById("view-date").options.length > 0;
  document.getElementById("view-date-field").hidden = e.target.value !== "calendar" || !hayFechas;
  // El fin del rango necesita una SEGUNDA fecha: con una sola no hay nada que
  // estirar y el campo sobraría.
  document.getElementById("view-date-end-field").hidden =
    e.target.value !== "calendar" || document.getElementById("view-date").options.length < 2;
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
  if (type === "calendar") {
    const dateKey = document.getElementById("view-date").value;
    const endKey = document.getElementById("view-date-end").value;
    if (dateKey && endKey && endKey !== dateKey) {
      // Con fin: la vista declara un rango y sus filas se pintan como barras.
      config.start_property = dateKey;
      config.end_property = endKey;
    } else if (dateKey) {
      config.date_property = dateKey;
    }
  }
  try {
    const created = await api(`/databases/${viewDialogDatabase.id}/views`, {
      method: "POST",
      body: JSON.stringify({ name, type, config }),
    });
    viewDialog.close();
    // Abrir la vista recién creada, no la primera de la base: crear un
    // calendario y aterrizar en la tabla de siempre hacía dudar de si la vista
    // existía siquiera.
    const views = await api(`/databases/${viewDialogDatabase.id}/views`);
    await renderDatabase(viewDialogDatabase, views, created?.id ?? views[0]?.id ?? null);
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
      const hitIcon = document.createElement("span");
      hitIcon.className = "type-icon";
      paintPageIcon(hitIcon, r.type, r.icon);
      el.append(hitIcon, ` ${r.title}`);
      el.addEventListener("click", () => {
        searchInput.value = "";
        searchResults.hidden = true;
        navSections.hidden = false;
        navigateToPage(r.id);
      });
      makeKeyboardActivatable(el, "button", 0);
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

// --------------------------------------------------------------- grafo (P9)
// El visor de nodos vive en graph.js (Canvas 2D, sin dependencias): aquí solo
// se le pasa la página abierta y a dónde ir al pulsar un nodo. El grafo no
// navega por su cuenta, y el id de una database abierta no es el id de su
// página (ver database_id), así que se resuelve como en "subir un nivel".

function currentGraphPageId() {
  if (!state.current) return null;
  if (state.current.kind === "note") return state.current.id;
  const node = [...state.pagesById.values()].find((n) => n.database_id === state.current.id);
  return node?.id ?? null;
}

function openGraphView() {
  if (document.querySelector(".graph-view") || document.querySelector("dialog[open]")) return;
  openGraph({ pageId: currentGraphPageId(), onOpenPage: navigateToPage });
}

document.getElementById("btn-graph").addEventListener("click", openGraphView);

// "g" abre el grafo, salvo que estés escribiendo (el editor y los campos se
// quedan con la tecla) o que ya haya un diálogo abierto.
document.addEventListener("keydown", (e) => {
  if (e.key !== "g" || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
  if (e.target.closest?.("input, textarea, select, [contenteditable='true']")) return;
  openGraphView();
});

// -------------------------------------------------- colapsar la barra lateral
// Se recuerda en localStorage (no en el servidor -- es preferencia de
// pantalla, no dato del workspace) para que no vuelva a aparecer sola cada
// vez que se recarga.
const SIDEBAR_COLLAPSED_KEY = "mdm_sidebar_collapsed";
const layoutEl = document.querySelector(".layout");

// Lo que dura la salida de la barra (`mdm-side-out` en el CSS) más un margen:
// el hueco se cierra al acabar la animación y, si el evento no llega, a este
// plazo. Con «reducir movimiento» no hay animación que esperar.
const SIDEBAR_SALIDA_MS = 180;
const sinMovimiento = window.matchMedia("(prefers-reduced-motion: reduce)");
let sidebarSaliendo = null; // el temporizador de una salida en curso

/** `animar` solo lo pide el botón: al cargar la página la barra se pinta en su
 * estado, sin entrar ni salir. La preferencia se guarda en el acto; lo único
 * que se retrasa (lo que dura la salida) es quitar la columna. */
function setSidebarCollapsed(collapsed, { animar = false } = {}) {
  localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
  clearTimeout(sidebarSaliendo);
  sidebarSaliendo = null;
  layoutEl.classList.remove("sidebar-entering", "sidebar-leaving");
  // Fijarla o plegarla acaba con el asomo: deja de flotar.
  terminarAsomo({ animar: false });
  if (!animar || sinMovimiento.matches || esMovil()) {
    layoutEl.classList.toggle("sidebar-collapsed", collapsed);
    return;
  }
  if (!collapsed) {
    layoutEl.classList.remove("sidebar-collapsed");
    layoutEl.classList.add("sidebar-entering");
    sidebarSaliendo = setTimeout(() => layoutEl.classList.remove("sidebar-entering"), SIDEBAR_SALIDA_MS + 40);
    return;
  }
  layoutEl.classList.add("sidebar-leaving");
  sidebarSaliendo = setTimeout(() => {
    sidebarSaliendo = null;
    layoutEl.classList.remove("sidebar-leaving");
    layoutEl.classList.add("sidebar-collapsed");
  }, SIDEBAR_SALIDA_MS - 40);
}

/** ¿La barra está (o va camino de estar) oculta? Durante la salida todavía no
 * lleva `sidebar-collapsed`, y un segundo clic tiene que volver a mostrarla. */
function sidebarOculta() {
  return (
    layoutEl.classList.contains("sidebar-collapsed") ||
    layoutEl.classList.contains("sidebar-leaving")
  );
}

document.getElementById("btn-toggle-sidebar").addEventListener("click", () => {
  if (esMovil()) return setCajonAbierto(!cajonAbierto());
  setSidebarCollapsed(!sidebarOculta(), { animar: true });
});

// ------------------------------------- asomar la barra plegada (como Notion)
// Con la barra plegada, llevar el ratón al borde izquierdo (o al botón de la
// barra) la hace ASOMAR: flota sobre el contenido, sin empujarlo, y se recoge
// sola al sacar el ratón. Pulsar el botón mientras asoma la deja fija. Solo
// en escritorio: en el móvil es el cajón, y sin ratón no hay a qué asomar.
const zonaDeAsomo = document.createElement("div");
zonaDeAsomo.className = "sidebar-peek-zone";
zonaDeAsomo.setAttribute("aria-hidden", "true");
layoutEl.appendChild(zonaDeAsomo);
const ASOMO_RECOGER_MS = 280; // margen para cruzar el hueco sin que se cierre
const ASOMO_SALIDA_MS = 140; // lo que dura `mdm-side-out`
let asomoCierre = null;
let asomoSalida = null;
const hayRaton = window.matchMedia("(hover: hover) and (pointer: fine)");
// Los mismos nodos que `sidebarEl` y `toggleSidebarBtn` (declarados más abajo,
// con el cajón del móvil): aquí todavía no se pueden usar esos nombres.
const barraAside = document.getElementById("sidebar-aside");
const botonDeBarra = document.getElementById("btn-toggle-sidebar");

function asomando() {
  return layoutEl.classList.contains("sidebar-peek");
}
function asomar() {
  clearTimeout(asomoCierre);
  asomoCierre = null;
  if (!layoutEl.classList.contains("sidebar-collapsed") || esMovil() || !hayRaton.matches) return;
  clearTimeout(asomoSalida);
  layoutEl.classList.remove("sidebar-peek-leaving");
  layoutEl.classList.add("sidebar-peek");
}
function terminarAsomo({ animar = true } = {}) {
  clearTimeout(asomoCierre);
  asomoCierre = null;
  if (!asomando()) return;
  clearTimeout(asomoSalida);
  if (!animar || sinMovimiento.matches) {
    layoutEl.classList.remove("sidebar-peek", "sidebar-peek-leaving");
    return;
  }
  layoutEl.classList.add("sidebar-peek-leaving");
  asomoSalida = setTimeout(() => {
    layoutEl.classList.remove("sidebar-peek", "sidebar-peek-leaving");
  }, ASOMO_SALIDA_MS);
}
/** Recoger al cabo de un momento, salvo que algo la siga usando: escribir en
 * la búsqueda o un menú contextual abierto desde el árbol (vive en <body>,
 * fuera de la barra, y sacar el ratón hacia él no es irse). */
function recogerAsomoLuego() {
  if (!asomando()) return;
  clearTimeout(asomoCierre);
  asomoCierre = setTimeout(function intentar() {
    const foco = document.activeElement;
    const escribiendo = foco && barraAside.contains(foco) && foco.matches("input, textarea");
    const menu = document.querySelector(".mdm-context-menu");
    if (escribiendo || (menu && !menu.hidden)) {
      asomoCierre = setTimeout(intentar, ASOMO_RECOGER_MS);
      return;
    }
    terminarAsomo();
  }, ASOMO_RECOGER_MS);
}
for (const el of [zonaDeAsomo, barraAside, botonDeBarra]) {
  el.addEventListener("mouseenter", (e) => {
    // Arrastrando (seleccionar texto, mover una tarjeta) no se abre nada.
    if (e.buttons) return;
    if (el === barraAside && !asomando()) return;
    asomar();
  });
  el.addEventListener("mouseleave", recogerAsomoLuego);
}
// Escape la recoge sin esperar, como cualquier panel flotante.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && asomando() && !document.querySelector(".mdm-context-menu")) {
    terminarAsomo();
  }
});

setSidebarCollapsed(localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1");

// ------------------------------------------------- cajón lateral en móvil (P14)
// Por debajo de 768 px la barra lateral no ocupa columna: es un cajón encima
// del contenido (ver el bloque «móvil» del CSS). Aquí solo se abre y se cierra,
// y se cierra solo al elegir una página o al tocar el fondo oscurecido. Es un
// estado de un momento, no una preferencia de pantalla: NO se guarda en
// localStorage ni toca `sidebar-collapsed`, así que abrir el cajón en el móvil
// no deja la barra colapsada la próxima vez que abras la app en el escritorio.
const DRAWER_QUERY = "(max-width: 768px)";
const drawerQuery = window.matchMedia(DRAWER_QUERY);
const sidebarEl = document.getElementById("sidebar-aside");
const scrimEl = document.getElementById("sidebar-scrim");
const toggleSidebarBtn = document.getElementById("btn-toggle-sidebar");

function esMovil() {
  return drawerQuery.matches;
}
function cajonAbierto() {
  return layoutEl.classList.contains("sidebar-drawer-open");
}
/** Deja la primera sección de la barra lateral dentro del cajón.
 *
 * La barra lateral es la misma columna con o sin cajón: conserva el
 * `scrollTop` que tuviera, así que con el árbol desplazado la bandeja (o «Mis
 * tareas») se abría por encima del borde superior (y no se podía ni tocar). Al
 * abrir se corrige el scroll lo justo para que se vea; si ya se veía, no se
 * mueve (C2). */
function mostrarSeccionesEnElCajon() {
  const section = ["mytasks-section", "inbox-section"]
    .map((id) => document.getElementById(id))
    .find((el) => el && !el.hidden);
  if (!sidebarEl || !section) return;
  const caja = sidebarEl.getBoundingClientRect();
  const seccion = section.getBoundingClientRect();
  if (seccion.top < caja.top) sidebarEl.scrollTop -= caja.top - seccion.top;
  else if (seccion.bottom > caja.bottom) sidebarEl.scrollTop += seccion.bottom - caja.bottom;
}

function setCajonAbierto(open) {
  const abierto = Boolean(open) && esMovil();
  layoutEl.classList.toggle("sidebar-drawer-open", abierto);
  if (abierto) mostrarSeccionesEnElCajon();
  if (scrimEl) scrimEl.hidden = !abierto;
  if (!toggleSidebarBtn) return;
  // aria-expanded solo tiene sentido en el cajón: en escritorio el botón no
  // despliega nada, colapsa la columna.
  if (esMovil()) toggleSidebarBtn.setAttribute("aria-expanded", abierto ? "true" : "false");
  else toggleSidebarBtn.removeAttribute("aria-expanded");
}

scrimEl?.addEventListener("click", () => setCajonAbierto(false));

// Elegir una página (o procesar una captura) cierra el cajón: ya no hace falta.
sidebarEl?.addEventListener("click", (e) => {
  if (!cajonAbierto()) return;
  if (e.target.closest(".tree-row, .search-hit, .inbox-open, .inbox-process, #btn-mytasks, #btn-nav-home, #btn-graph")) setCajonAbierto(false);
});

// Escape cierra el cajón. En captura, porque el editor y los diálogos también
// escuchan Escape y aquí se llega antes.
document.addEventListener(
  "keydown",
  (e) => {
    if (e.key === "Escape" && cajonAbierto()) setCajonAbierto(false);
  },
  true,
);

// Al pasar a escritorio el cajón deja de existir: se cierra y se quita el fondo.
drawerQuery.addEventListener("change", (e) => {
  if (!e.matches) setCajonAbierto(false);
});

setCajonAbierto(false);

// ------------------------------------------------------------------- tema
// Oscuro por defecto; se cambia en Ajustes > Aplicación y la elección se
// recuerda en localStorage (preferencia de pantalla, no dato del workspace) --
// SPEC-B §1.1.
const THEME_KEY = "mdm_theme";

function applyTheme(theme) {
  const resolved = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = resolved;
  localStorage.setItem(THEME_KEY, resolved);
  sincronizarTemaEnAjustes();
}

applyTheme(localStorage.getItem(THEME_KEY) || "dark");

// -------------------------------------------------------------------- arranque

// Quién eres (P11b) antes de nada: si no hay sesión, /auth/me responde 401 y
// api() manda al login. Los usuarios que este navegador ya conoce se leen antes
// para que la autoría de las notas salga con su color desde el primer pintado.
loadKnownUsers();
// El directorio de personas (celdas `person`): una vez por sesión, para que el
// selector y los formularios de alta lo tengan sin esperar.
cargarPersonas().catch(() => {});
montarMarca();
initUserChip();
loadCurrentUser();

// El arranque: cargado el árbol, se abre la dirección de la barra (o «donde lo
// dejaste» si es `/`) -- ver `abrirAlArrancar`.
abrirAlArrancar().catch((e) => reportError("No se pudo abrir el panel", e));
pollStatus();
setInterval(pollStatus, 4000);
// P8: avisos de lo que cambia en el vault (los `.md` editados por fuera).
connectVaultEvents();

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

// Ctrl/Cmd+S fuerza el guardado ya, sin esperar el debounce (y sin pasar por el
// diálogo del navegador). UN solo listener para toda la sesión, delegado en la
// nota abierta: registrarlo dentro de renderNote() dejaba uno por nota visitada
// sobre el mismo #main (nadie los quitaba), y cada Ctrl+S reenviaba el guardado
// de TODAS las notas abiertas en la sesión con el cuerpo congelado de cada una
// -- el texto viejo de una pisaba lo que tuviera ahora su fichero. Si no hay
// nada pendiente no se manda nada.
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "s") {
    e.preventDefault();
    state.flushPendingSave?.();
  }
});

// Giro del móvil o cambio de tamaño de la ventana: el alto del título y de la
// descripción de la nota abierta se midió con el ancho anterior y hay que
// volver a medirlo (lo que no quepa queda invisible en un <textarea> sin
// scroll). El plegado de la barra lateral en escritorio no pasa por aquí: lo
// cubre el ResizeObserver que monta renderNote().
for (const evento of ["resize", "orientationchange"]) {
  window.addEventListener(evento, () => {
    state.refitNoteText?.();
    // `orientationchange` puede llegar antes de que el navegador rehaga el
    // layout; el segundo intento, ya con el ancho nuevo, no cuesta nada.
    requestAnimationFrame(() => state.refitNoteText?.());
  });
}
