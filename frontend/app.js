// MyDamnManagement — panel visual. Vanilla JS, sin build, sin dependencias
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
  history: [], // [{kind, id}, ...] -- pila de navegación para los botones atrás/adelante
  historyPos: -1, // índice de "dónde estamos" dentro de state.history
  noteSession: null, // nota abierta: qué queda sin guardar y cómo refrescarla (P8, /events)
  repaintAuthor: null, // repinta «Creado por …» de la nota abierta cuando llega /auth/me (P11b)
  refitNoteText: null, // remide el alto de título/descripción de la nota abierta (P14, cambio de ancho)
  noteWidthObserver: null, // ResizeObserver del ancho de la nota abierta (P14); se corta al navegar
  ownRemoteEvents: new Map(), // page_id -> ms: cambio en disco que ha hecho esta app (no es «de fuera»)
  inbox: { dir: "Bandeja", count: 0, pages: [] }, // bandeja de capturas (P13), de GET /inbox
  inboxIds: new Set(), // ids de esa bandeja: el árbol no las repite
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
  close.innerHTML = svgIcon("i-x");

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
  // La fila abierta como página cuelga de <body>, no de #main: si se navega
  // (un wikilink, atrás, borrar la página abierta) hay que cerrarla, o se
  // quedaría encima de otra página con el editor de la fila viejo dentro.
  cerrarRowPage({ devolverFoco: false });
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
    recentSection.innerHTML += '<div class="search-empty">Elige una página o crea una nueva.</div>';
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
  const el = document.getElementById(`dot-${name}`);
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
    is_admin: Boolean(user.is_admin),
  });
  if (!persist) return;
  try {
    localStorage.setItem(KNOWN_USERS_KEY, JSON.stringify([...knownUsers.values()]));
  } catch {
    // sin sitio en localStorage: el color se pierde al recargar, nada más
  }
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
function paintCreatedBy(el, createdBy, createdByColor) {
  el.textContent = "";
  const name = (createdBy || "").trim();
  if (!name) {
    el.textContent = "Creado por —";
    return;
  }
  const color = createdByColor || colorForAuthor(name);
  if (color) {
    const dot = document.createElement("span");
    dot.className = "user-dot";
    dot.style.background = color;
    dot.setAttribute("aria-hidden", "true");
    el.appendChild(dot);
  }
  el.appendChild(document.createTextNode(`Creado por ${name}`));
}

// ---------------------------------------------------------------- el chip

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
  chip.querySelector(".user-dot").style.background = me.color || "var(--ink-muted)";
  chip.querySelector(".user-chip-name").textContent = me.display_name;
  chip.title = `${me.display_name} (@${me.username})${me.is_admin ? " · administrador" : ""}`;
  chip.setAttribute("aria-label", `Tu cuenta: ${me.display_name}. Abrir el menú`);
  // «Usuarios» solo existe para un admin: a los demás no se les enseña un botón
  // que solo puede dar 403.
  menu.querySelector('[data-action="users"]').hidden = !me.is_admin;
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

  menu.querySelector('[data-action="password"]').addEventListener("click", () => {
    closeMenu();
    openPasswordDialog();
  });
  menu.querySelector('[data-action="users"]').addEventListener("click", () => {
    closeMenu();
    openUsersDialog();
  });
  menu.querySelector('[data-action="tokens"]').addEventListener("click", () => {
    closeMenu();
    openTokensDialog();
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

const passwordDialog = document.getElementById("dialog-password");
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

function openPasswordDialog() {
  if (!passwordDialog) return;
  passwordForm.reset();
  setFormNote(passwordNote, PASSWORD_NOTE);
  document.getElementById("btn-save-password").disabled = false;
  passwordDialog.showModal();
  document.getElementById("pw-current").focus();
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

document.getElementById("btn-cancel-password")?.addEventListener("click", () => {
  passwordDialog.close();
});

// --------------------------------------------------------- diálogo: usuarios

const usersDialog = document.getElementById("dialog-users");
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
    const dot = document.createElement("span");
    dot.className = "user-dot";
    dot.style.background = u.color || "var(--ink-muted)";
    dot.setAttribute("aria-hidden", "true");
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

async function openUsersDialog() {
  if (!usersDialog) return;
  newUserForm.reset();
  setFormNote(newUserNote, NEW_USER_NOTE);
  // Se vuelve a preguntar en cada apertura: puede que la API ya lo tenga.
  usersListUnavailable = false;
  renderKnownUsers();
  setFormNote(usersNote, "Cargando usuarios…");
  usersDialog.showModal();
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
      }),
    });
    rememberUser(created);
    newUserForm.reset();
    renderKnownUsers();
    setFormNote(newUserNote, `Usuario «${created.display_name}» creado.`, "ok");
  } catch (e) {
    if (e?.status === 401) return;
    setFormNote(newUserNote, describeError(e), "error");
  } finally {
    create.disabled = false;
  }
});

document.getElementById("btn-close-users")?.addEventListener("click", () => {
  usersDialog.close();
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

const tokensDialog = document.getElementById("dialog-tokens");
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

async function openTokensDialog() {
  if (!tokensDialog) return;
  if (tokenReveal) tokenReveal.hidden = true;
  if (newTokenForm) newTokenForm.reset();
  setFormNote(tokensNote, TOKENS_NOTE);
  setFormNote(newTokenNote, NEW_TOKEN_NOTE);
  tokensDialog.showModal();
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

document.getElementById("btn-close-tokens")?.addEventListener("click", () => {
  tokensDialog.close();
});

document.getElementById("btn-cancel-procesar")?.addEventListener("click", () => {
  procesarDialog.close();
});

document.getElementById("btn-do-procesar")?.addEventListener("click", procesarNota);

initInbox();

// ------------------------------------------------------------------ árbol

async function loadTree() {
  try {
    // La bandeja (P13) se refresca con el árbol: es el mismo momento en el que
    // puede haber cambiado (un `.md` nuevo, una captura desde un atajo).
    await loadInbox();
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

    if (notes.length === 0) treeContainer.innerHTML = '<div class="search-empty">Sin páginas aún.</div>';
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
  paintCreatedBy(meta, page.created_by, page.created_by_color);
  // /auth/me puede llegar después que la nota: entonces se repinta solo esto
  // (el color del servidor no cambia; el respaldo local sí puede).
  state.repaintAuthor = () =>
    paintCreatedBy(meta, page.created_by, page.created_by_color);

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
    return JSON.stringify({
      title: titleInput.value,
      icon: page.icon,
      description: descInput.value,
      body_markdown: bodyText(),
    });
  }

  function scheduleSave() {
    dirty = true;
    changeSeq += 1;
    setSaveState("dirty");
    clearTimeout(saveTimer);
    if (blocked) return; // hay un cambio en disco sin decidir: no se pisa
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
      const updated = await api(`/pages/${page.id}`, { method: "PATCH", body });
      page.updated_at = updated.updated_at;
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

  main.append(topbar, headerWrap, fileInput, titleRow, meta, descInput, bodyMount);
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
    onChange: () => {
      noteBodyFallback = bodyText();
      scheduleSave();
      renderOutline();
    },
    onCreatePage: createChildPage,
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
    onCreateDatabase: createDatabaseInNote,
    renderDatabaseBlock: renderDatabaseEmbed,
  });
  noteEditor = state.editor;

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
        if (authorColor) {
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
function showRemoteBanner({ deleted = false, session, onReload, onDismiss }) {
  const main = document.getElementById("main");
  if (!main) return;
  main.querySelector(".note-remote-banner")?.remove();

  const banner = document.createElement("div");
  banner.className = "note-remote-banner";
  banner.setAttribute("role", "status");

  const text = document.createElement("span");
  text.className = "note-remote-banner-text";
  text.textContent = deleted
    ? "Esta nota se ha borrado en disco. Tienes cambios sin guardar."
    : "Esta nota cambió en disco. Tienes cambios sin guardar.";

  const reloadBtn = document.createElement("button");
  reloadBtn.type = "button";
  reloadBtn.className = "btn";
  reloadBtn.textContent = "Recargar";
  reloadBtn.title = deleted
    ? "Cerrar la nota y perder lo que no has guardado"
    : "Recargar desde el disco y perder lo que no has guardado";
  reloadBtn.addEventListener("click", () => {
    banner.remove();
    if (state.noteSession !== session) return; // mientras tanto se navegó
    onReload();
  });

  const dismissBtn = document.createElement("button");
  dismissBtn.type = "button";
  dismissBtn.className = "btn";
  dismissBtn.textContent = "Descartar";
  dismissBtn.title = "Seguir con lo que hay en el editor";
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
      showEmpty();
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
    if (state.current?.kind === "note" && state.current.id === id) showEmpty();
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
      showEmpty(); // ya no está: se vuelve al inicio, como al borrarla desde la app
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
  const session = state.noteSession;
  if (!session) return;
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
  session.refreshDatabaseEmbeds?.();
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
  // Abrir una base con una navegación nueva (árbol, recientes, búsqueda, una
  // plantilla) deja el foco en el primer campo del alta; volver con atrás o
  // adelante no, que el foco es del usuario, no del historial.
  if (kind === "database") await selectDatabase(id, { focus: fromHistory ? null : "addRow" });
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

async function selectDatabase(id, { focus = null } = {}) {
  const previous = state.current;
  state.current = { kind: "database", id };
  markActive(id);
  try {
    const [database, views] = await Promise.all([api(`/databases/${id}`), api(`/databases/${id}/views`)]);
    await renderDatabase(database, views, views[0]?.id ?? null, { focus });
  } catch (e) {
    // Igual que en selectNote: si falla la carga, no se ha pintado nada y el
    // estado vuelve a donde estaba.
    state.current = previous;
    markActive(previous ? previous.id : null);
    reportError("No se pudo abrir la base de datos", e);
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
};

function tipoVisible(tipo) {
  return TIPOS_VISIBLES[tipo] || "Texto";
}

async function renderDatabase(database, views, activeViewId, { focus = null } = {}) {
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

  const headerActions = document.createElement("div");
  headerActions.className = "btn-row";

  // La vista activa hace falta ya para el botón de borrarla: «Todas» no es una
  // vista, es la base sin filtro, y no hay nada que quitar.
  const activeView = views.find((v) => v.id === activeViewId) || null;

  const schemaBtn = document.createElement("button");
  schemaBtn.className = "btn";
  schemaBtn.textContent = "Propiedades";
  schemaBtn.addEventListener("click", () => openDatabaseDialog(database, recargarVistas));

  const newViewBtn = document.createElement("button");
  newViewBtn.className = "btn";
  newViewBtn.innerHTML = `${svgIcon("i-plus")}Vista`;
  newViewBtn.addEventListener("click", () => openViewDialog(database));

  // Borrar la vista activa: se lleva su filtro, su orden y su tipo, y ninguna
  // fila (los datos son de la base, no de la vista). Solo con una vista de
  // verdad activa, y nunca la última: sin ninguna, la base se quedaría sin la
  // pestaña con la que se abre. Con una sola, el botón se queda ahí apagado y
  // el motivo escrito debajo de las pestañas, en vez de desaparecer sin decir
  // nada -- el estado no es mudo.
  const esLaUltimaVista = Boolean(activeView) && views.length <= 1;
  const deleteViewBtn = document.createElement("button");
  deleteViewBtn.type = "button";
  deleteViewBtn.className = "btn btn-danger";
  deleteViewBtn.textContent = "Borrar vista";
  if (activeView) {
    deleteViewBtn.disabled = esLaUltimaVista;
    deleteViewBtn.title = esLaUltimaVista
      ? "Es la última vista: una base necesita al menos una. Las filas no se tocan."
      : "";
    deleteViewBtn.addEventListener("click", async () => {
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
      // la primera si era la primera -- y el foco vuelve a la pestaña: el botón
      // que se acaba de pulsar ya no está, y el foco no puede quedarse en
      // <body> (el siguiente Tab empezaría por arriba del documento).
      const indice = views.findIndex((v) => v.id === activeView.id);
      const quedan = views.filter((v) => v.id !== activeView.id);
      const siguiente = quedan[Math.min(indice, quedan.length - 1)] || null;
      await renderDatabase(database, quedan, siguiente ? siguiente.id : null, {
        focus: "viewTab",
      });
    });
  }

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

  headerActions.append(schemaBtn, newViewBtn);
  if (activeView) headerActions.append(deleteViewBtn);
  headerActions.append(deleteDbBtn);
  header.append(title, headerActions);

  const hint = document.createElement("div");
  hint.className = "db-schema-hint";
  hint.textContent =
    database.schema_def.map((p) => `${p.name} (${tipoVisible(p.type)})`).join(" · ") ||
    "Sin propiedades definidas.";

  const tabs = document.createElement("div");
  tabs.className = "view-tabs";
  // La pestaña de cada vista (null = «sin vista»). El repintado destruye estos
  // botones, así que se guardan aquí para poder devolver el foco al activo.
  const tabButtons = new Map();

  const defaultTab = document.createElement("button");
  defaultTab.className = "view-tab" + (activeViewId === null ? " active" : "");
  defaultTab.textContent = "Todas (sin vista)";
  defaultTab.addEventListener("click", () => renderDatabase(database, views, null, { focus: "viewTab" }));
  tabButtons.set(null, defaultTab);
  tabs.appendChild(defaultTab);

  views.forEach((v) => {
    const tab = document.createElement("button");
    tab.className = "view-tab" + (v.id === activeViewId ? " active" : "");
    tab.textContent = `${v.name} (${v.type})`;
    tab.addEventListener("click", () => renderDatabase(database, views, v.id, { focus: "viewTab" }));
    tabButtons.set(v.id, tab);
    tabs.appendChild(tab);
  });

  const content = document.createElement("div");
  // El cuerpo de la vista (tabla/tablero/lista/calendario) entra con un
  // fundido de 160 ms solo cuando se cambia de pestaña; al repintar por
  // guardar una fila, mover una tarjeta o editar el esquema no se anima nada
  // (repintar no es navegar, y el parpadeo se notaría a cada celda).
  content.className = "db-view" + (focus === "viewTab" ? " is-entering" : "");
  main.append(header, hint, tabs, content);

  // El motivo escrito y no en un `title` escondido: un botón apagado sin
  // explicación a la vista es un estado mudo (y en táctil no hay hover).
  if (esLaUltimaVista) {
    const nota = document.createElement("p");
    nota.className = "db-view-note";
    nota.textContent =
      `«${activeView.name}» es la última vista de esta base: no se puede borrar. ` +
      "Las filas no dependen de ella y no se tocan.";
    main.insertBefore(nota, content);
  }

  // El filtro y el orden son de la vista y los aplica el servidor (`?view=`),
  // que es el mismo `apply_view` que exporta el `.md`: no hay dos versiones de
  // la regla. M no sale de la lista filtrada: cuando hay filtro se pide
  // también la base entera, porque «3 de 10» es lo que hace honesto el recuento.
  const filtros = filtrosDeVista(activeView);
  const clavesOrden = clavesDeOrden(activeView?.config);
  let rows;
  let total = null;
  if (!activeView) {
    rows = await api(`/databases/${database.id}/rows`);
  } else if (filtros.length) {
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
  const aplicarConfig = async (cambios) => {
    try {
      await guardarConfigDeVista(database, activeView, cambios);
    } catch (e) {
      reportError("No se pudo guardar el filtro ni el orden de la vista", e);
      return;
    }
    await recargarVistas();
  };

  if (activeView) {
    main.insertBefore(
      renderViewTools({
        database,
        view: activeView,
        visibles: rows.length,
        total,
        onCambio: aplicarConfig,
      }),
      content,
    );
  } else if (views.length) {
    // «Todas» es la base entera y no tiene vista donde guardar un filtro: se
    // dice, en vez de enseñar unos botones que no podrían recordar nada.
    const notaTodas = document.createElement("p");
    notaTodas.className = "db-view-note";
    notaTodas.textContent =
      "«Todas» es la base entera: el filtro y el orden son de una vista. Elige una pestaña para filtrar.";
    main.insertBefore(notaTodas, content);
  }

  // Vacío honesto: no una tabla en blanco, sino el motivo escrito y la salida.
  if (filtros.length && rows.length === 0) {
    // Sin filas no hay tabla, pero la selección de antes sigue viva: la barra
    // se pinta igual (dice cuántas hay y que el filtro las ha dejado fuera).
    // Esconderla dejaría una selección invisible que, aun así, actúa.
    const hueco = document.createElement("div");
    hueco.className = "db-sel-shell";
    const sel = crearSeleccionDeFilas({ database, rows: [], onRepintar: refresh });
    hueco.append(sel.barra, sel.informes, avisoDeFiltroVacio(() => aplicarConfig({ filters: [] })));
    content.appendChild(hueco);
  } else if (activeView && activeView.type === "board" && activeView.config?.group_by) {
    content.appendChild(renderBoard(database, rows, activeView, refresh));
  } else if (activeView && activeView.type === "calendar") {
    content.appendChild(renderCalendar(database, rows, activeView, refresh));
  } else if (activeView && activeView.type === "list") {
    content.appendChild(renderList(database, rows, activeView, refresh));
  } else {
    content.appendChild(
      renderTable(database, rows, {
        sort: clavesOrden,
        onToggleSort: activeView
          ? (key) => aplicarConfig({ sort: alternarOrden(clavesOrden, key) })
          : null,
        vacio: filtros.length ? "Ninguna fila cumple el filtro." : null,
        // Tras borrar o cambiar una propiedad en bloque, la tabla se repinta
        // con lo que diga el servidor: nada de ir tocando filas a mano.
        onRepintar: refresh,
      }),
    );
  }

  const addRowForm = renderAddRowForm(database);
  content.appendChild(addRowForm);
  // El foco se mueve solo cuando hay un motivo, no en cada repintado: abrir la
  // base (el primer campo del alta, que si no hay que recorrer la tabla entera
  // para llegar a «Nueva fila») o cambiar de vista (la pestaña, que acaba de
  // repintarse). `refresh` —mover una tarjeta, guardar una fila, guardar el
  // esquema— no pide nada: antes el foco acababa en el alta, al final del
  // contenido, y el siguiente Tab seguía desde ahí. `preventScroll` porque si
  // el destino cae bajo el pliegue no queremos dar un salto.
  if (focus === "addRow") {
    addRowForm.querySelector("#nueva-fila-titulo")?.focus({ preventScroll: true });
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
};

/** Los operadores que no llevan valor: el estado lo dice el propio operador. */
const OPS_SIN_VALOR = new Set(["is_empty", "is_not_empty", "checked", "unchecked"]);

// Los títulos de las filas de las bases destino de una relación, para que el
// valor de «incluye» sea un nombre y no un id. Se cachea por clave de
// propiedad; si la base destino no se puede leer, el control pide el id.
const titulosDeRelacion = new Map();

function operadoresDe(prop) {
  return OPERADORES_FILTRO[prop?.type] || OPERADORES_TEXTO;
}

function etiquetaDeOperador(prop, op) {
  const par = operadoresDe(prop).find(([valor]) => valor === op);
  return par ? par[1] : op;
}

function operadorPorDefecto(prop) {
  return operadoresDe(prop)[0][0];
}

/** Los filtros de una vista, normalizados (una vista vieja puede no tener). */
function filtrosDeVista(view) {
  const filtros = view?.config?.filters;
  if (!Array.isArray(filtros)) return [];
  return filtros.filter((f) => f && typeof f === "object" && f.key && f.op);
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
      (Array.isArray(valor) && valor.length === 0)
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
  return database.schema_def.find((p) => p.key === key)?.name || key;
}

/** Una condición en palabras, para el chip: «Fase es «lead»». */
function textoDeCondicion(cond, database) {
  const prop = database.schema_def.find((p) => p.key === cond.key);
  if (!prop) return `${cond.key} ${cond.op}`;
  const operador = etiquetaDeOperador(prop, cond.op);
  if (OPS_SIN_VALOR.has(cond.op)) return `${prop.name} ${operador}`;
  const valor =
    prop.type === "relation"
      ? titulosDeRelacion.get(prop.key)?.get(cond.value) || String(cond.value ?? "")
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

/** La barra de la vista: «Filtrar», «Ordenar», el recuento honesto y los
 * chips de las condiciones activas (encima de la tabla, como se pide). */
function renderViewTools({ database, view, visibles, total, onCambio }) {
  const filtros = filtrosDeVista(view);
  const claves = clavesDeOrden(view.config);

  const tools = document.createElement("div");
  tools.className = "db-tools";

  const cabeza = document.createElement("div");
  cabeza.className = "db-tools-head";

  const filtrar = document.createElement("button");
  filtrar.type = "button";
  filtrar.className = "btn" + (filtros.length ? " is-active" : "");
  filtrar.textContent = filtros.length ? `Filtrar (${filtros.length})` : "Filtrar";
  filtrar.setAttribute("aria-haspopup", "dialog");
  if (database.schema_def.length === 0) {
    // Sin propiedades no hay nada que filtrar: se dice, no se abre un panel
    // vacío.
    filtrar.disabled = true;
    filtrar.title = "La base no tiene propiedades: no hay nada que filtrar.";
  } else {
    filtrar.addEventListener("click", () => openFilterDialog(database, view, onCambio));
  }

  const ordenar = document.createElement("button");
  ordenar.type = "button";
  ordenar.className = "btn" + (claves.length ? " is-active" : "");
  ordenar.textContent = claves.length ? `Ordenar (${claves.length})` : "Ordenar";
  ordenar.setAttribute("aria-haspopup", "dialog");
  if (database.schema_def.length === 0) {
    ordenar.disabled = true;
    ordenar.title = "La base no tiene propiedades: no hay nada por lo que ordenar.";
  } else {
    ordenar.addEventListener("click", () => openSortDialog(database, view, onCambio));
  }

  const cuenta = document.createElement("span");
  cuenta.className = "db-count";
  cuenta.textContent = textoDeRecuento(visibles, filtros.length ? total : null);

  cabeza.append(filtrar, ordenar, cuenta);
  tools.appendChild(cabeza);

  const chips = document.createElement("div");
  chips.className = "db-chips";
  if (filtros.length || claves.length) tools.appendChild(chips);
  pintarChipsDeVista(chips, database, filtros, claves, onCambio);

  // Un filtro sobre una relación enseña un id hasta que se conoce el título de
  // la fila: con la lista de la base destino, el chip dice el nombre.
  if (filtros.some((f) => database.schema_def.find((p) => p.key === f.key)?.type === "relation")) {
    cargarTitulosDeRelacion(database).then(() => {
      if (!chips.isConnected) return;
      pintarChipsDeVista(chips, database, filtros, claves, onCambio);
    });
  }

  return tools;
}

/** Reescribe el recuento de la barra SIN repintar la vista.
 *
 * El alta en línea de la lista y del calendario repinta solo su cuerpo, y el
 * «N de M filas» vive en la barra que pinta `renderViewTools`: sin esto el
 * contador se quedaba con el número de antes del alta (un contador que miente
 * es peor que no tenerlo). `total` nulo es «la vista no filtra».
 *
 * `desde` es el cuerpo de la vista (dentro de `.db-view`); la barra es el
 * hermano que va justo antes.
 */
function refrescarRecuentoDeVista(desde, visibles, total) {
  const cuerpo = desde.closest(".db-view");
  const barra = cuerpo ? cuerpo.previousElementSibling : null;
  const cuenta = barra ? barra.querySelector(".db-count") : null;
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
  if (!prop) opciones.unshift({ key: cond.key, name: `${cond.key} (ya no existe)` });
  opciones.forEach((p) => {
    const opcion = document.createElement("option");
    opcion.value = p.key;
    opcion.textContent = p.name;
    propSelect.appendChild(opcion);
  });
  const propParaElControl = prop || { key: cond.key, name: cond.key, type: "text" };
  propSelect.value = propParaElControl.key;
  propSelect.addEventListener("change", () => {
    const nueva = database.schema_def.find((p) => p.key === propSelect.value);
    if (!nueva) return;
    cond.key = nueva.key;
    cond.op = operadorPorDefecto(nueva);
    cond.value = "";
    pintarFilasDeFiltro();
  });

  const opSelect = document.createElement("select");
  opSelect.className = "filter-op";
  opSelect.setAttribute("aria-label", `Operador de la condición ${indice + 1}`);
  operadoresDe(propParaElControl).forEach(([valor, etiqueta]) => {
    const opcion = document.createElement("option");
    opcion.value = valor;
    opcion.textContent = etiqueta;
    opSelect.appendChild(opcion);
  });
  if (!operadoresDe(propParaElControl).some(([valor]) => valor === cond.op)) {
    cond.op = operadorPorDefecto(propParaElControl);
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
  ctx.condiciones.push({ key: prop.key, op: operadorPorDefecto(prop), value: "" });
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
  database.schema_def.forEach((prop) => {
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
  const libre = ctx.database.schema_def.find((prop) => !usadas.has(prop.key));
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
 * Lo que NO se hace desde aquí: borrar filas ni tocar el esquema. La tabla va
 * sin columna de acciones (`deletable: false`) porque borrar desde dentro de
 * una nota es demasiado fácil de hacer sin querer; el encabezado (un botón)
 * lleva a la base entera, que es donde viven las dos cosas.
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
  const activeView = views.find((v) => view && v.name === view) || views[0] || null;

  // La tabla incrustada usa la vista con la que está configurada, y eso
  // incluye su filtro y su orden: los aplica el mismo `apply_view` del
  // servidor, así que no hay una segunda versión de la regla en el navegador.
  const filtros = filtrosDeVista(activeView);
  const clavesOrden = clavesDeOrden(activeView?.config);
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

  const open = document.createElement("button");
  open.type = "button";
  open.className = "db-embed-title";
  // El editor no trata este clic como «entrar en el bloque» (ver entry.js).
  open.dataset.dbAction = "open";
  open.textContent = database.title || title || "Base de datos";
  open.title = "Abrir la base";
  // Mismo camino que el árbol: deja entrada en el historial, así que
  // «atrás» vuelve a la nota desde la que se abrió.
  open.addEventListener("click", () => goTo("database", database.id));

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
      renderTable(database, filas, {
        deletable: false,
        hooks,
        sort: clavesOrden,
        vacio: filtros.length ? "Ninguna fila cumple el filtro." : null,
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
      }),
    );
    if (enEdicion) reabrirCeldaEnEdicion(body, database, filas, enEdicion, hooks);
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

  head.append(open, meta);
  container.append(
    head,
    body,
    // Sin recargar la nota ni la página: POST de siempre y repintado de la
    // tabla. El aviso de «el filtro la deja fuera» vive dentro del control.
    renderAddRowControl({
      vista: activeView,
      onCreate: (titulo) => crearFilaEnVista(database, activeView, titulo),
      onDone: (res) => {
        // Con el total de la base: si la vista filtra, el contador sigue
        // diciendo «N de M» en vez de perder el «de M» al crear la fila.
        pintar(res.filas, res.total);
        if (res.visible) enfocarFila(body, res.fila.id);
        // Y los DEMÁS bloques de esta misma base, con los datos del servidor:
        // el eco de este POST se descarta entero (ver `handleDatabaseEvent`),
        // así que si no se piden aquí se quedan con el contador y las filas de
        // antes. Este bloque no entra: ya está repintado con `res.filas` y el
        // aviso y el foco están puestos.
        refrescarOtrosEmbeds(database.id, container);
      },
    }),
  );
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
function reabrirCeldaEnEdicion(container, database, filas, estado, hooks) {
  const fila = filas.find((r) => String(r.id) === estado.rowId);
  if (!fila) return false;
  const tr = container.querySelector(`tr[data-row-id="${CSS.escape(estado.rowId)}"]`);
  const td = tr ? celdasEditables(tr)[estado.columna] : null;
  if (!td) return false;
  // La columna 0 es el título cuando la tabla lo enseña (`renderTable`).
  const muestraTitulo = !database.title_hidden;
  const prop = muestraTitulo
    ? database.schema_def[estado.columna - 1] || null
    : database.schema_def[estado.columna] || null;
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
async function crearFilaEnVista(database, activeView, titulo, extra = {}) {
  // ANTES del POST: el servidor publica `rows` en /events (M2) antes de
  // responder, así que el aviso le llega a esta pestaña antes que la fila. Se
  // apunta que el cambio es de aquí para ignorar el eco (si no, el editor
  // repinta el bloque desde cero y se lleva por delante el aviso y el foco que
  // se ponen al volver). El cambio de OTRA ventana no pasa por aquí.
  ultimoCambioPropio = { databaseId: String(database.id), ts: Date.now() };
  const fila = await api(`/databases/${database.id}/rows`, {
    method: "POST",
    body: JSON.stringify({ title: titulo, properties: extra }),
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
function enfocarFila(container, rowId) {
  const el = container.querySelector(`[data-row-id="${rowId}"]`);
  if (!el) return false;
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

/** El «+ Nueva fila» de una vista (tabla incrustada en una nota, lista y
 * agenda del calendario).
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
function renderAddRowControl({ vista, onCreate, onDone, className = "db-add-row" }) {
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
      const res = await onCreate(titulo);
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

function formatPropValue(value, prop) {
  if (value === undefined || value === null || value === "") return "—";
  if (prop.type === "checkbox") return value ? "✓" : "—";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

/** Igual que formatPropValue, pero listo para innerHTML: el "sí" de un
 * checkbox es el icono de check del sprite (con texto para lectores de
 * pantalla) y todo lo demás va escapado. */
function formatPropValueHtml(value, prop) {
  if (prop.type === "checkbox") {
    return value ? `${svgIcon("i-check")}<span class="sr-only">Sí</span>` : "—";
  }
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
function buildPropertyInput(prop, value) {
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
    cuenta.textContent = `${total} ${total === 1 ? "seleccionada" : "seleccionadas"}`;
    fuera.hidden = fueraDeVista === 0;
    fuera.textContent =
      fueraDeVista === 0
        ? ""
        : `· ${fueraDeVista} de ellas ${fueraDeVista === 1 ? "no está" : "no están"} en esta vista: ${
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
    sincronizar();
  }

  function seleccionarTodas() {
    for (const [id, fila] of visibles) sel.set(id, fila.title || "Sin título");
    sincronizar();
  }

  function limpiar() {
    sel.clear();
    sincronizar();
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
    for (const boton of acciones.querySelectorAll("button")) boton.disabled = valor;
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
    const campo = activo.closest("input, select, textarea, [contenteditable='true']");
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
      if (!sel.size || !puedeBorrar) return;
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

  const btnQuitar = botonDeBarra("Quitar selección", "quitar-seleccion", () => limpiar());
  acciones.append(btnPropiedad);
  if (puedeBorrar) {
    const btnBorrar = botonDeBarra("Borrar", "borrar-seleccion", () => borrarSeleccion(), "btn btn-danger");
    acciones.append(btnBorrar);
  } else {
    // En la tabla de una nota no se borran filas: borrar se hace en la base.
    const nota = document.createElement("span");
    nota.className = "db-sel-note";
    nota.textContent = "Para borrar, abre la base.";
    acciones.append(nota);
  }
  acciones.append(btnQuitar);

  barra.append(cuenta, fuera, avance, acciones);
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
function renderTable(
  database,
  rows,
  {
    deletable = true,
    hooks = null,
    sort = [],
    onToggleSort = null,
    vacio = null,
    onRepintar = null,
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
  });

  const wrap = document.createElement("div");
  wrap.className = "table-wrap";

  const table = document.createElement("table");
  table.className = "rows-table";

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  // La columna del título es una columna más: se puede renombrar
  // (`title_name`) y quitar (`title_hidden`). Si se quita, la tabla deja de
  // enseñarla; la fila se sigue llamando por su `id`, que es su fichero.
  const muestraTitulo = !database.title_hidden;
  const rotuloTitulo = database.title_name || "Título";
  // 1) La columna de abrir la fila, en su sitio y con su texto para lectores
  //    de pantalla (ver el bucle de filas).
  const abrirHead = document.createElement("th");
  abrirHead.scope = "col";
  abrirHead.className = "row-open-head";
  const abrirSolo = document.createElement("span");
  abrirSolo.className = "sr-only";
  abrirSolo.textContent = "Abrir";
  abrirHead.appendChild(abrirSolo);
  headRow.appendChild(abrirHead);
  if (muestraTitulo) {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = rotuloTitulo;
    headRow.appendChild(th);
  }
  // Cada propiedad es una cabecera que ordena, cuando hay dónde guardar ese
  // orden (la pestaña «Todas» no tiene vista en la que escribirlo, y la tabla
  // incrustada es de solo lectura). El título no: el servidor no ordena por
  // él, y una flecha que no hace nada sería mentir.
  database.schema_def.forEach((p) =>
    headRow.appendChild(celdaDeCabecera(p, sort, onToggleSort)),
  );
  // La columna de acciones lleva la casilla de la fila y, cuando la vista deja
  // borrar (`deletable`), también el botón de borrar. La casilla está SIEMPRE,
  // también en la tabla incrustada de una nota (allí no hay botón de borrar,
  // pero marcar en bloque sí vale). Su cabecera es la casilla de «seleccionar
  // todas las visibles».
  {
    const th = document.createElement("th");
    th.scope = "col";
    th.className = "row-sel-head";
    const soloTexto = document.createElement("span");
    soloTexto.className = "sr-only";
    soloTexto.textContent = deletable ? "Seleccionar y acciones" : "Seleccionar";
    th.append(sel.casillaDeCabecera(), soloTexto);
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);

  const tbody = document.createElement("tbody");
  // Cómo repintar cada fila cuando la vista de fila guarda algo (se rellena
  // en el bucle): lo usa también la puerta de teclado del final.
  const refrescadores = new Map();
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    // +2: la columna de abrir la fila y la de acciones, que ahora está siempre.
    td.colSpan = database.schema_def.length + (muestraTitulo ? 1 : 0) + 2;
    td.className = "search-empty";
    td.textContent = vacio || "Sin filas todavía.";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  rows.forEach((row) => {
    const tr = document.createElement("tr");
    // `data-row-id`: el «+ Nueva fila» de la vista deja el foco en el nombre de
    // la fila que acaba de crear (ver `enfocarFila`).
    tr.dataset.rowId = row.id;

    // 1) Abrir la fila como página. En su propia columna para que funcione
    // también con el título oculto (`title_hidden`), y fuera de la celda del
    // título: ahí el clic edita, y editar no puede tragarse la apertura.
    const openTd = document.createElement("td");
    openTd.className = "row-open-cell";
    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "row-open";
    openBtn.innerHTML = svgIcon("i-expand");
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
    openTd.appendChild(openBtn);
    tr.appendChild(openTd);

    // 2) Título y propiedades editables en el sitio: lo que se ve es el valor
    // del `.md`, y un clic abre el control del tipo de esa propiedad. Dentro de
    // una nota es lo mismo: se edita ahí y se guarda en el `.md` de la fila.
    const celdas = new Map(); // clave de la propiedad (null = título) -> <td>

    if (muestraTitulo) {
      const titleTd = document.createElement("td");
      paintCell(titleTd, database, row, null, hooks);
      celdas.set(null, titleTd);
      tr.appendChild(titleTd);
    }

    database.schema_def.forEach((p) => {
      const td = document.createElement("td");
      paintCell(td, database, row, p, hooks);
      celdas.set(p.key, td);
      tr.appendChild(td);
    });

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

    // La casilla de la fila, en la columna de acciones y antes del botón de
    // borrar. Dentro de una nota, `data-db-action` deja pasar el clic: sin él
    // el widget del editor se lo traga y la tabla se desmonta.
    const accTd = document.createElement("td");
    accTd.className = "row-sel-cell";
    if (hooks?.embed) accTd.dataset.dbAction = "seleccionar-fila";
    const casilla = sel.casillaDeFila(row);
    accTd.appendChild(casilla);
    tr.appendChild(accTd);
    sel.registrarFila(row, tr, casilla);

    if (!deletable) {
      // Sin botón de borrar: la fila se borra desde la página de la base (en
      // una nota, borrar filas es demasiado fácil de hacer sin querer).
      tbody.appendChild(tr);
      return;
    }

    const delBtn = document.createElement("button");
    delBtn.className = "row-del";
    delBtn.innerHTML = svgIcon("i-trash");
    delBtn.title = "Borrar fila";
    delBtn.setAttribute("aria-label", "Borrar fila");
    delBtn.addEventListener("click", async () => {
      if (!confirm(`¿Borrar "${row.title}"?`)) return;
      await api(`/databases/${database.id}/rows/${row.id}`, { method: "DELETE" });
      selectDatabase(database.id);
    });
    accTd.appendChild(delBtn);

    tbody.appendChild(tr);
  });

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

  table.append(thead, tbody);
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
  shell.append(sel.barra, sel.informes, wrap);
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
  setRovingCell(td);
  const shouldFocus = focus === undefined ? ownedFocus || document.activeElement === document.body : focus;
  if (shouldFocus) td.focus();
}

/** Pinta el valor de una celda y la deja lista para editarse con un clic.
 * `prop === null` es la celda del título. */
function paintCell(td, database, row, prop, hooks = null) {
  td.innerHTML = prop
    ? formatPropValueHtml(row.properties?.[prop.key], prop)
    : escapeHtml(row.title || "Sin título");
  // La columna del título se lee como jerarquía (la regla CSS va por clase:
  // la primera columna de la tabla es la de abrir la fila).
  td.classList.toggle("cell-title", !prop);
  td.title = "Clic para editar";
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

function renderBoard(database, rows, view, refresh) {
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

  const options = { groupKey, refresh };
  columns.forEach((col) =>
    board.appendChild(renderBoardColumn(database, col, grouped.get(col) || [], options))
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

function renderBoardColumn(database, label, rows, { groupKey, refresh, value = label }) {
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

  // Sin `group_by` no hay arrastre: sin clave no se escribe nada, así que la
  // columna no acepta drops.
  if (groupKey) {
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

    const t = document.createElement("div");
    t.className = "board-card-title";
    t.textContent = row.title;
    card.appendChild(t);

    Object.entries(row.properties || {}).forEach(([k, v]) => {
      const propDef = database.schema_def.find((p) => p.key === k);
      if (!propDef || v === null || v === "" || v === undefined) return;
      const line = document.createElement("div");
      line.className = "board-card-prop";
      line.innerHTML = `${escapeHtml(propDef.name)}: ${formatPropValueHtml(v, propDef)}`;
      card.appendChild(line);
    });

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

  return col;
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

/** Vista de calendario: cada fila en el día que tiene en una propiedad de
 * fecha, en una rejilla mensual con navegación.
 *
 * Qué fecha se usa lo dice `config.date_property` (lo elige el diálogo de la
 * vista); una vista antigua sin esa clave, o con una propiedad que ya no
 * existe, cae en la primera propiedad de fecha del esquema. Si la base no
 * tiene ninguna, no se pinta una tabla por debajo ni una caja vacía: se dice
 * lo que pasa y cómo arreglarlo.
 */
function renderCalendar(database, rows, view, refresh) {
  const dateProps = database.schema_def.filter((p) => p.type === "date");
  if (dateProps.length === 0) {
    return avisoDeVista(
      "Esta base no tiene ninguna propiedad de fecha.",
      "El calendario coloca cada fila en su día, así que necesita una propiedad de tipo fecha. Añádela en «Propiedades» y vuelve a abrir el calendario.",
    );
  }

  // La elegida por la vista, si sigue existiendo; si no, la primera.
  let dateProp = dateProps.find((p) => p.key === view.config?.date_property) || dateProps[0];

  const hoy = claveDeHoy();
  const [anioHoy, mesHoy] = [Number(hoy.slice(0, 4)), Number(hoy.slice(5, 7)) - 1];
  // Si esta vista ya estaba abierta en otro mes, se vuelve a ese mes: un
  // repintado (guardar una fila desde un chip) no puede devolver al de hoy.
  const mesGuardado = mesDeCalendario.get(claveDeMesDeVista(database, view));
  let anio = mesGuardado?.anio ?? anioHoy;
  let mes = mesGuardado?.mes ?? mesHoy;
  const recordarMes = () => mesDeCalendario.set(claveDeMesDeVista(database, view), { anio, mes });

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
    sel.addEventListener("change", async () => {
      dateProp = dateProps.find((p) => p.key === sel.value) || dateProps[0];
      repaint();
      const config = { ...(view.config || {}), date_property: dateProp.key };
      try {
        await api(`/databases/${database.id}/views/${view.id}`, {
          method: "PATCH",
          body: JSON.stringify({ config }),
        });
        view.config = config;
      } catch (e) {
        // El calendario ya está pintado con la fecha nueva; lo que no se pudo
        // guardar es que la vista se acuerde la próxima vez.
        reportError("No se pudo guardar la fecha de la vista", e);
      }
    });
    lado.appendChild(pick);
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

  const repaintAgenda = (porDia, dias, sueltas, invalidas) => {
    let pintados = 0;
    for (let dia = 1; dia <= dias; dia++) {
      const clave = `${anio}-${String(mes + 1).padStart(2, "0")}-${String(dia).padStart(2, "0")}`;
      const delDia = porDia.get(clave) || [];
      if (!delDia.length) continue; // en la agenda, los días vacíos no ocupan
      pintados += 1;
      const bloque = document.createElement("div");
      bloque.className = "cal-agenda-day" + (clave === hoy ? " is-today" : "");
      const fecha = document.createElement("span");
      fecha.className = "cal-agenda-date";
      fecha.textContent = `${dia} de ${MESES[mes]}`;
      const eventos = document.createElement("div");
      eventos.className = "cal-events";
      delDia.forEach((row) => eventos.appendChild(chipDeFila(row)));
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
          ? "1 fila sin fecha no se puede colocar: rellena «" + dateProp.name + "»."
          : `${sueltas} filas sin fecha no se pueden colocar: rellena «${dateProp.name}».`;
      grid.appendChild(nota);
    }
    if (invalidas) {
      const nota = document.createElement("p");
      nota.className = "cal-agenda-empty is-warn";
      nota.textContent =
        invalidas === 1
          ? `1 fila tiene una fecha no válida en «${dateProp.name}» y no se puede colocar: corrige el día y el mes.`
          : `${invalidas} filas tienen una fecha no válida en «${dateProp.name}» y no se pueden colocar: corrige el día y el mes.`;
      grid.appendChild(nota);
    }
  };

  const repaint = () => {
    // Una pasada por fila: las que no tienen fecha o tienen una imposible no se
    // colocan, se cuentan y se avisa de cada grupo por separado.
    const porDia = new Map();
    let sueltas = 0;
    let invalidas = 0;
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

    titulo.textContent = `${MESES[mes]} de ${anio}`;
    sinFecha.textContent = textoDeFilasFuera(sueltas, invalidas);
    sinFecha.title = invalidas
      ? "«Sin fecha»: no tienen valor en esta propiedad. «Fecha no válida»: el valor no es un día real (por ejemplo, 31 de noviembre) y hay que corregirlo."
      : "Filas que no se pueden colocar: no tienen valor en esta propiedad de fecha.";
    sinFecha.classList.toggle("is-warn", invalidas > 0);
    sinFecha.hidden = sueltas + invalidas === 0 || calendarQuery.matches;

    grid.innerHTML = "";
    const dias = new Date(anio, mes + 1, 0).getDate(); // día 0 del mes siguiente
    if (calendarQuery.matches) {
      grid.className = "calendar-agenda";
      repaintAgenda(porDia, dias, sueltas, invalidas);
      return;
    }
    grid.className = "calendar-grid";

    DIAS_SEMANA.forEach((d) => {
      const head = document.createElement("div");
      head.className = "cal-dow";
      head.textContent = d;
      grid.appendChild(head);
    });

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
    onCreate: (titulo) =>
      crearFilaEnVista(database, view, titulo, { [dateProp.key]: claveDeHoy() }),
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
  // referencias y se puede recoger.
  calendarioVivo = { wrap, repaint };
  return wrap;
}

/** Vista de lista: una línea por fila, con el título y los valores que tiene
 * (los vacíos no se pintan). Es la vista de repasar muchas filas de un vistazo;
 * cada línea abre la fila, que es donde se edita, igual que una tarjeta del
 * tablero. */
function renderList(database, rows, view, refresh) {
  const shell = document.createElement("div");
  shell.className = "db-sel-shell";
  // Misma selección que la tabla: una fila marcada en la lista y luego vista en
  // la tabla (otra pestaña) sigue marcada.
  const sel = crearSeleccionDeFilas({ database, rows, onRepintar: refresh });

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
  const pintarLinea = (item, row) => {
    item.textContent = "";
    const title = document.createElement("span");
    title.className = "list-row-title";
    title.textContent = row.title || "Sin título";
    item.appendChild(title);
    database.schema_def.forEach((p) => {
      const v = row.properties?.[p.key];
      if (v === null || v === undefined || v === "" || (Array.isArray(v) && v.length === 0)) return;
      const span = document.createElement("span");
      span.className = "list-row-prop";
      span.innerHTML = `${escapeHtml(p.name)}: ${formatPropValueHtml(v, p)}`;
      item.appendChild(span);
    });
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

  const avisoVacio = () => avisoDeVista("Sin filas todavía.", null);
  rows.forEach((row) => wrap.appendChild(lineaDeFila(row)));
  if (rows.length === 0) wrap.appendChild(avisoVacio());

  // «+ Nueva fila» al final de la lista, como una línea más. Al crear, la
  // lista se rehace con lo que devolvió el servidor: la fila nueva aparece
  // donde la deje el orden (y el filtro) de la vista, no al final por haber
  // sido la última en llegar.
  const addRow = renderAddRowControl({
    vista: view,
    className: "db-add-row db-add-row--list",
    onCreate: (titulo) => crearFilaEnVista(database, view, titulo),
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

  shell.append(sel.barra, sel.informes, wrap);
  return shell;
}

// ------------------------------------------------ fila abierta como página
// En Notion cada fila es una página. Aquí la fila YA es un `.md`
// (`Bases/<slug>/<id8>.md`) con frontmatter y cuerpo: el cuerpo es el cuerpo
// del fichero y las propiedades su frontmatter. Lo que faltaba era la puerta y
// la vista.
//
// La vista se pinta ENCIMA de lo que estabas mirando (la tabla de la base, la
// tabla incrustada en una nota, la lista) y deja la de debajo viva: cerrar
// devuelve al mismo sitio, con el foco donde estaba, sin volver a pedir ni
// repintar nada. Por eso no es una ruta más del historial (volver atrás no es
// «cerrar la fila») ni un `<dialog>`: sus menús flotantes —el `/`, el del asa,
// el contextual— cuelgan de `<body>` y se quedarían debajo del top layer.
//
// Se guarda en el MISMO `.md`, con el mismo PATCH que una celda: el título y
// las propiedades al frontmatter, el cuerpo al cuerpo. El fichero no se mueve
// (se llama por el id de la fila) y el índice se actualiza en la misma
// operación.
//
// Lo que se edita es el MISMO editor de notas (`createNoteEditor`, el bundle
// de `frontend/editor-src`): mismos bloques, mismos atajos y mismo menú `/`.
// Lo único que no ofrece es crear páginas o bases colgando de la fila: una
// fila no es carpeta (`_folder_for_parent` en el vault lo rechaza), así que el
// nombre se queda escrito y se dice por qué.

const ROW_PAGE_DEBOUNCE_MS = 700;

let rowPage = null; // la fila abierta ahora mismo, o null

/** Cierra la fila abierta. `devolverFoco` es lo normal (volver a la celda, al
 * botón o a la línea desde la que se abrió); al navegar a otra página no, que
 * el foco es de la página nueva.
 *
 * Lo que quede sin guardar se manda con `keepalive` ANTES de destruir el
 * editor: cerrar por un repintado de la vista de debajo (o al ocultar la
 * pestaña) no puede esperar a la respuesta. */
function cerrarRowPage({ devolverFoco = true, avisar = true } = {}) {
  const ctx = rowPage;
  if (!ctx) return;
  rowPage = null;
  ctx.cerrada = true;
  clearTimeout(ctx.temporizador);
  ctx.temporizador = null;
  ctx.beacon?.();
  try {
    ctx.editor?.destroy();
  } catch (_) {
    // destruir dos veces no es un fallo que haya que contar
  }
  ctx.editor = null;
  // Los flotantes del editor (el menú `/`, el contextual, la vista previa de
  // un wikilink) cuelgan de <body>: cerrar la fila tiene que llevárselos.
  closeSlashMenu();
  closeContextMenu();
  stopLinkPreview();
  ctx.caja.remove();
  for (const el of ctx.inertes) el.inert = false;
  document.body.classList.remove("row-page-open");
  // La tabla y la lista repintan su fila con `onSaved`; el tablero y el
  // calendario no tienen una fila que repintar en el sitio y se rehacen aquí.
  // Antes del foco: el repintado puede cambiar el DOM al que se vuelve.
  if (avisar && ctx.guardadoAlgo) ctx.onClose?.();
  if (devolverFoco) devolverElFoco(ctx);
}

/** ¿El evento viene del cuerpo editable de la fila? Ahí los atajos son los del
 * editor (`Alt+↑/↓` mueve el bloque, `Escape` cierra sus menús): el cuerpo es
 * el mismo editor de notas, no un campo de la vista de fila. */
function esDelCuerpoDeLaFila(target) {
  return target instanceof Element && Boolean(target.closest("#row-page-body"));
}

/** Los reintentos de devolver el foco, en ms desde el cierre: el primero va
 * inmediato (abajo) y estos son los que vienen detrás.
 *
 * Uno solo no basta cuando la vuelta es a un control que el editor rehace: el
 * eco del guardado repinta la tabla incrustada en una nota, CodeMirror
 * reemplaza el widget —el botón de abrir incluido— y la tabla nueva aparece
 * cuando llegan sus filas. Mientras tanto el foco se cae a `<body>`; con un
 * intento único se queda ahí. */
const FOCO_ESPERAS_MS = [60, 150, 300, 600, 1200];
const FOCO_PLAZO_MS = 1600;

/** La restauración de foco en curso, si la hay (`{vivos, interrumpir}`). */
let focoPendiente = null;

/** Deja de insistir en devolver el foco: lo tomó el usuario, se abrió otra
 * fila o se acabó el plazo. */
function cancelarFocoPendiente() {
  const pendiente = focoPendiente;
  if (!pendiente) return;
  focoPendiente = null;
  for (const id of pendiente.vivos) clearTimeout(id);
  document.removeEventListener("keydown", pendiente.interrumpir, true);
  document.removeEventListener("pointerdown", pendiente.interrumpir, true);
}

/** ¿El foco se ha quedado sin dueño? Pasa al quitar la fila y cada vez que se
 * va el nodo enfocado (el navegador lo deja en `<body>`). */
function focoPerdido() {
  const activo = document.activeElement;
  return (
    !activo || activo === document.body || activo === document.documentElement || !activo.isConnected
  );
}

/** El bloque de base incrustada en el que vive un control de la fila, si vive
 * en uno: su base y su posición entre los bloques de esa base. El editor
 * rehace el widget entero al repintar la nota, así que para volver al MISMO
 * bloque cuando la misma base está incrustada dos veces se recuerda cuál era. */
function bloqueDeOrigen(origin) {
  const bloque = origin?.closest?.(".cm-mdm-dblock-body");
  const databaseId = bloque?.dataset?.databaseId;
  if (!databaseId) return null;
  const hermanos = [...document.querySelectorAll(".cm-mdm-dblock-body")].filter(
    (c) => c.dataset.databaseId === databaseId,
  );
  const orden = hermanos.indexOf(bloque);
  return orden < 0 ? null : { databaseId, orden };
}

/** El control al que hay que volver: el de la fila, dentro del bloque del que
 * salió (si salió de uno) y, si ese bloque ya no está, en cualquier otro. */
function controlDeVuelta(ctx) {
  if (ctx.origin?.isConnected) return ctx.origin;
  let raiz = document;
  if (ctx.originEmbed) {
    const bloques = [...document.querySelectorAll(".cm-mdm-dblock-body")].filter(
      (c) => c.dataset.databaseId === ctx.originEmbed.databaseId,
    );
    raiz = bloques[ctx.originEmbed.orden] || document;
  }
  const fila = raiz.querySelector(`[data-row-id="${ctx.resumen.id}"]`);
  if (!fila) return null;
  return fila.matches("button") ? fila : fila.querySelector(".row-open");
}

/** Devuelve el foco a donde estaba: el control desde el que se abrió la fila
 * (la celda, el botón de abrir, la línea de la lista).
 *
 * Si la vista de debajo se repintó mientras la fila estaba abierta —el eco del
 * guardado repinta la tabla incrustada en una nota, por ejemplo—, ese control
 * ya no está en el documento: se busca el de la MISMA fila en lo que hay
 * ahora, que es el sitio equivalente.
 *
 * Y como el repintado puede llegar DESPUÉS de cerrar (el widget se rehace
 * cuando el aviso de la base llega por `/events`), no se intenta una sola vez:
 * se reintenta durante un margen corto, solo mientras el foco siga perdido y
 * el usuario no haya tocado el teclado ni el ratón. */
function devolverElFoco(ctx) {
  cancelarFocoPendiente();
  const pendiente = { vivos: [], interrumpir: () => cancelarFocoPendiente() };
  focoPendiente = pendiente;
  const intentar = () => {
    if (focoPendiente !== pendiente || !focoPerdido()) return;
    controlDeVuelta(ctx)?.focus();
  };
  intentar(); // el caso normal: el control sigue donde estaba
  for (const espera of FOCO_ESPERAS_MS) pendiente.vivos.push(setTimeout(intentar, espera));
  pendiente.vivos.push(setTimeout(cancelarFocoPendiente, FOCO_PLAZO_MS));
  // El usuario manda: en cuanto toca el teclado o el ratón, la restauración se
  // cancela (no le robamos el foco a lo que acaba de enfocar).
  document.addEventListener("keydown", pendiente.interrumpir, true);
  document.addEventListener("pointerdown", pendiente.interrumpir, true);
}

/** Abre una fila como página encima de la vista actual.
 *
 * `origin` es el control desde el que se abre (la celda, el botón de abrir, la
 * línea de la lista): al cerrar, el foco vuelve ahí. `rows` —con `index`, la
 * posición de esta fila— es la vista que se está mirando, para `Alt+↑/↓`.
 * `onSaved(guardada, cambios)` se llama tras cada PATCH aceptado (la tabla y
 * la lista repintan su fila en el sitio con eso) y `onClose()` al cerrar, solo
 * si algo se guardó.
 */
async function openRowPage(
  database,
  resumen,
  { origin = null, rows = null, index = -1, onSaved = null, onClose = null } = {},
) {
  if (rowPage) return; // una fila abierta a la vez: no hay filas dentro de filas
  cancelarFocoPendiente(); // el foco de la fila anterior ya no es de nadie

  // Lo de debajo se queda en el documento (para volver sin repintar) pero
  // `inert`: no se puede tabular ni clicar dentro mientras la fila está
  // abierta, y el foco no se escapa de la vista de fila.
  const inertes = ["header", "#sidebar-aside", "#main"]
    .map((sel) => document.querySelector(sel))
    .filter(Boolean);

  const caja = document.createElement("div");
  caja.className = "row-page";

  const fondo = document.createElement("div");
  fondo.className = "row-page-backdrop";

  const panel = document.createElement("div");
  panel.className = "row-page-panel";
  panel.tabIndex = -1;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-label", `Fila de ${database.title || "la base"}`);

  const barra = document.createElement("div");
  barra.className = "row-page-bar";
  const ruta = document.createElement("span");
  ruta.className = "row-page-crumbs";
  ruta.textContent = database.title || "Base de datos";
  const estado = document.createElement("span");
  estado.className = "row-page-savestatus";
  estado.setAttribute("role", "status");
  const cerrar = document.createElement("button");
  cerrar.type = "button";
  cerrar.className = "icon-btn row-page-close";
  cerrar.innerHTML = svgIcon("i-x");
  cerrar.title = "Cerrar la fila";
  cerrar.setAttribute("aria-label", "Cerrar la fila");
  barra.append(ruta, estado, cerrar);

  const scroll = document.createElement("div");
  scroll.className = "row-page-scroll";
  const titulo = document.createElement("textarea");
  titulo.id = "row-page-title";
  titulo.className = "row-page-title";
  titulo.rows = 1;
  titulo.setAttribute("aria-label", "Título de la fila");
  const propsEl = document.createElement("div");
  propsEl.className = "row-page-props";
  const cuerpoEl = document.createElement("div");
  cuerpoEl.id = "row-page-body";
  cuerpoEl.className = "row-page-body";
  scroll.append(titulo, propsEl, cuerpoEl);
  panel.append(barra, scroll);
  caja.append(fondo, panel);

  const ctx = {
    database,
    resumen,
    rows,
    index,
    origin,
    originEmbed: bloqueDeOrigen(origin),
    onSaved,
    onClose,
    caja,
    titulo,
    propsEl,
    cuerpoEl,
    estado,
    inertes,
    editor: null,
    campos: [], // [{prop, input, read}]
    cuerpo: "", // lo que hay escrito AHORA en el cuerpo
    enviado: "", // lo último que el servidor aceptó
    ultimoBeacon: "",
    cola: Promise.resolve(), // un PATCH por turno, en cadena
    temporizador: null,
    guardadoAlgo: false,
    montada: false,
    cerrada: false,
  };
  rowPage = ctx;

  document.body.appendChild(caja);
  document.body.classList.add("row-page-open");
  for (const el of inertes) el.inert = true;
  panel.focus();

  // ------------------------------------------------------------ guardar

  function marcar(texto) {
    estado.textContent =
      texto === "saving"
        ? "Guardando…"
        : texto === "dirty"
          ? "Cambios sin guardar…"
          : texto === "error"
            ? "Error al guardar"
            : "Guardado";
    estado.classList.toggle("is-error", texto === "error");
  }

  /** Lo que hay que mandar: solo lo que difiere de lo que el servidor ya tiene.
   * El título va aparte del mapa de propiedades, como en la API. */
  function cambios() {
    const salida = {};
    if (!ctx.montada) return salida;
    const nombre = (titulo.value || "").trim() || "Sin título";
    if (!sameValue(nombre, ctx.resumen.title)) salida.title = nombre;
    const properties = {};
    for (const { prop, read } of ctx.campos) {
      const valor = read();
      const actual = ctx.resumen.properties?.[prop.key];
      // Un checkbox que la fila no tenía y sigue sin marcar no se escribe: al
      // `.md` solo va lo que ha cambiado.
      if (prop.type === "checkbox" && actual === undefined && valor === false) continue;
      if (!sameValue(valor, actual)) properties[prop.key] = valor;
    }
    if (Object.keys(properties).length) salida.properties = properties;
    if (ctx.cuerpo !== ctx.enviado) salida.body_markdown = ctx.cuerpo;
    return salida;
  }

  /** Guardado de emergencia: la petición sigue viva aunque la pestaña se vaya
   * (`keepalive`) y el contenido que ya iba de camino no se repite. Los
   * cambios no se dan por guardados: si la pestaña sigue, el guardado normal
   * los reenvía. */
  function beacon() {
    const cambiosAhora = cambios();
    if (!Object.keys(cambiosAhora).length) return;
    const cuerpo = JSON.stringify(cambiosAhora);
    if (cuerpo === ctx.ultimoBeacon) return;
    ctx.ultimoBeacon = cuerpo;
    try {
      fetch(`/databases/${ctx.database.id}/rows/${ctx.resumen.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: cuerpo,
        keepalive: new TextEncoder().encode(cuerpo).length <= KEEPALIVE_MAX_BYTES,
      }).catch(() => {});
    } catch {
      // sin red o página ya descargándose
    }
  }
  ctx.beacon = beacon; // lo llaman cerrarRowPage y el guardado de emergencia

  function programar() {
    clearTimeout(ctx.temporizador);
    ctx.temporizador = setTimeout(() => {
      ctx.temporizador = null;
      guardar();
    }, ROW_PAGE_DEBOUNCE_MS);
  }

  /** Un PATCH por turno, en cadena: dos escrituras solapadas con contenidos
   * distintos pueden aplicarse al revés y dejar en el servidor el texto viejo.
   * Cada vuelta lee el estado de AHORA (título, campos, cuerpo), así que lo que
   * se escribió mientras la anterior estaba en vuelo no se pierde.
   * Devuelve `true` si al final no queda nada por guardar. */
  function guardar() {
    clearTimeout(ctx.temporizador);
    ctx.temporizador = null;
    ctx.cola = ctx.cola.then(async () => {
      if (ctx.cerrada) return true;
      const pendiente = cambios();
      if (!Object.keys(pendiente).length) {
        marcar("saved");
        return true;
      }
      marcar("saving");
      const cuerpoEnviado = "body_markdown" in pendiente ? pendiente.body_markdown : null;
      try {
        const guardada = await api(`/databases/${ctx.database.id}/rows/${ctx.resumen.id}`, {
          method: "PATCH",
          body: JSON.stringify(pendiente),
        });
        Object.assign(ctx.resumen, guardada);
        if (cuerpoEnviado !== null) ctx.enviado = cuerpoEnviado;
        ctx.guardadoAlgo = true;
        ctx.onSaved?.(guardada, pendiente);
        marcar("saved");
        return true;
      } catch (e) {
        // No se da por guardado: al cerrar se vuelve a intentar y, si no hay
        // manera, la fila se queda abierta con el motivo a la vista.
        marcar("error");
        reportError("No se pudo guardar la fila", e);
        return false;
      }
    });
    return ctx.cola;
  }

  /** Cerrar guarda: cerrar sin guardar daría a entender que se escribió en el
   * `.md`. Si el PATCH falla, la fila se queda abierta (el motivo ya se ha
   * avisado) y se puede corregir el campo y reintentar. */
  async function intentarCerrar() {
    if (rowPage !== ctx) return;
    const ok = await guardar();
    if (!ok || rowPage !== ctx) return;
    cerrarRowPage();
  }

  cerrar.addEventListener("click", () => intentarCerrar());
  fondo.addEventListener("click", () => intentarCerrar());

  // ------------------------------------------------------ montar la fila

  function ajustarTitulo() {
    titulo.style.height = "auto";
    titulo.style.height = `${titulo.scrollHeight}px`;
  }
  titulo.addEventListener("input", ajustarTitulo);
  // Enter guarda y sale del campo, como el título de una nota; Escape no
  // cancela: el texto ya está en el campo y se guarda al cerrar.
  titulo.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.shiftKey) return;
    e.preventDefault();
    titulo.blur();
  });
  titulo.addEventListener("change", () => guardar());
  titulo.addEventListener("blur", () => guardar());

  function montarPropiedades(detalle) {
    propsEl.textContent = "";
    ctx.campos = [];
    if (!database.schema_def.length) {
      const vacio = document.createElement("p");
      vacio.className = "row-page-prop-empty";
      vacio.textContent = "Esta base no tiene propiedades.";
      propsEl.appendChild(vacio);
      return;
    }
    database.schema_def.forEach((prop, i) => {
      const campo = document.createElement("div");
      campo.className = "row-page-prop";
      const label = document.createElement("label");
      label.textContent = prop.name;
      const { input, read } = buildPropertyInput(prop, detalle.properties?.[prop.key]);
      const inputId = idDeCampo("fila-pagina", i, prop.key);
      input.id = inputId;
      label.htmlFor = inputId;
      // El mismo criterio que una celda: se guarda al confirmar el control
      // (`change`: Enter o salir del campo), no en cada tecla.
      input.addEventListener("change", () => guardar());
      if (input.tagName !== "SELECT" && input.type !== "checkbox") {
        input.addEventListener("keydown", (e) => {
          if (e.key !== "Enter") return;
          e.preventDefault();
          input.blur(); // el `change` del blur es el que guarda
        });
      }
      campo.append(label, input);
      propsEl.appendChild(campo);
      ctx.campos.push({ prop, input, read });
    });
  }

  /** Irse a otra página desde dentro de la fila (un wikilink, el selector de
   * `/page`): se guarda lo escrito antes de moverse, y no se navega si el
   * guardado falló -- el texto es del usuario y no se tira por navegar. */
  async function salirANavegar(pageId) {
    if (!pageId) return;
    const ok = await guardar();
    if (!ok) return;
    if (rowPage === ctx) cerrarRowPage({ devolverFoco: false });
    await navigateToPage(pageId);
  }

  function montar(detalle) {
    // El objeto de la tabla/lista es el mismo que se queda al día con cada
    // PATCH (`ctx.resumen`): el repintado en el sitio lee de aquí.
    Object.assign(ctx.resumen, detalle);
    ctx.cuerpo = detalle.body_markdown || "";
    ctx.enviado = ctx.cuerpo;
    ctx.ultimoBeacon = "";

    titulo.value = detalle.title || "";
    ajustarTitulo();
    montarPropiedades(detalle);

    cuerpoEl.textContent = "";
    try {
      ctx.editor = createNoteEditor({
        parent: cuerpoEl,
        doc: ctx.cuerpo,
        resolvePage: (nombre) => findPageByTitle(nombre),
        onNavigate: (pageId) => salirANavegar(pageId),
        onChange: () => {
          ctx.cuerpo = ctx.editor?.getValue() ?? ctx.cuerpo;
          marcar("dirty");
          programar();
        },
        // Una fila no es carpeta de nada (`_folder_for_parent` lo rechaza en
        // el vault): se dice en vez de dejar un comando que no hace nada.
        onCreatePage: () => {
          showError(
            "Una fila no puede tener subpáginas: crea la nota desde la barra lateral.",
          );
          return null;
        },
        onCreateDatabase: () => {
          showError(
            "Una fila no puede tener una base colgando: crea la base desde una nota.",
          );
          return null;
        },
        onOpenPage: (pageId) => salirANavegar(pageId),
        onRenamePage: renombrarPaginaDelVault,
        onListPages: listPageEntries,
        currentPageId: detalle.id,
        onCreateLink: (nombre) => crearNotaHermana(null, nombre),
        onBlockContextMenu: (payload) =>
          mostrarMenuDeBloque(payload, () => {
            ctx.cuerpo = ctx.editor?.getValue() ?? ctx.cuerpo;
            marcar("dirty");
            programar();
          }),
        onWikilinkHover: (pageId, rect) => startLinkPreview(pageId, rect),
        onWikilinkHoverEnd: () => stopLinkPreview(),
        onSlashMenu: (menuState) => renderSlashMenu(menuState),
        onListDatabases: listDatabaseEntries,
        onPickDatabase: databaseBlockFor,
        renderDatabaseBlock: renderDatabaseEmbed,
      });
    } catch (e) {
      reportError("No se pudo abrir el editor de la fila", e);
    }
    ctx.editor?.setLinkTargets?.(linkTargets());
    ctx.montada = true;
    marcar("saved");
    // El foco entra por el título (y el cursor al final): es lo que se toca
    // primero al abrir una fila y lo que Notion deja listo para renombrar.
    titulo.focus();
    titulo.setSelectionRange(titulo.value.length, titulo.value.length);
  }

  // -------------------------------------------------- teclado de la vista

  caja.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !e.defaultPrevented) {
      // El editor consume el suyo cuando tiene un menú abierto (y lo deja
      // `defaultPrevented`): ahí Escape cierra el menú, no la fila.
      e.preventDefault();
      e.stopPropagation();
      intentarCerrar();
      return;
    }
    if (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      // Dentro del cuerpo el atajo es del editor: `moveBlockKeymap` usa
      // `Alt+↑/↓` para mover el bloque, y el evento sube hasta aquí porque su
      // keymap no lo detiene (`defaultPrevented` no basta como guarda). Si
      // además se cambiara de fila, el bloque reordenado acabaría guardado en
      // la fila que ya no estás mirando. La navegación es para el título y las
      // propiedades; en el cuerpo, `Alt+↑/↓` mueve bloques.
      if (e.defaultPrevented || esDelCuerpoDeLaFila(e.target)) return;
      e.preventDefault();
      e.stopPropagation();
      irAFilaVecina(e.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) {
      e.preventDefault();
      e.stopPropagation();
      guardar();
    }
  });

  /** `Alt+↑/↓`: la fila anterior/siguiente de la vista que se está mirando,
   * para revisar una lista seguida sin salir y volver a entrar. Solo cuando
   * se abrió desde una vista con orden (tabla y lista); en el tablero y el
   * calendario la vista no es una secuencia y no se ofrece. */
  async function irAFilaVecina(paso) {
    if (!ctx.rows || ctx.index < 0) return;
    const destino = ctx.rows[ctx.index + paso];
    if (!destino) return;
    const ok = await guardar();
    if (!ok) return;
    const { origin, rows, onSaved, onClose } = ctx;
    const indice = ctx.index + paso;
    // Se cierra sin devolver el foco (lo va a tomar la fila nueva) y sin
    // repintar lo de debajo: la tabla sigue ahí y su fila ya está al día.
    cerrarRowPage({ devolverFoco: false, avisar: false });
    await openRowPage(database, destino, {
      origin,
      rows,
      index: indice,
      onSaved,
      onClose,
    });
  }

  // ------------------------------------------------------------ abrir

  let detalle;
  try {
    detalle = await api(`/databases/${database.id}/rows/${resumen.id}`);
  } catch (e) {
    cerrarRowPage({ devolverFoco: true });
    // Sacarla del todo y pintar la vista de debajo: ya está intacta.
    if (!(e && e.status === 401)) reportError("No se pudo abrir la fila", e);
    return;
  }
  if (rowPage !== ctx) return; // se cerró mientras llegaba la respuesta
  montar(detalle);
}

function renderAddRowForm(database) {
  const wrap = document.createElement("div");
  wrap.className = "inline-form";

  const head = document.createElement("div");
  head.className = "panel-head";
  head.innerHTML = `${svgIcon("i-plus")}Nueva fila`;
  wrap.appendChild(head);

  const titleField = document.createElement("div");
  titleField.className = "field";
  const titleLabel = document.createElement("label");
  titleLabel.textContent = "Título";
  titleLabel.htmlFor = "nueva-fila-titulo";
  const titleInput = document.createElement("input");
  titleInput.type = "text";
  titleInput.id = "nueva-fila-titulo";
  titleField.append(titleLabel, titleInput);
  wrap.appendChild(titleField);

  const propInputs = [];
  database.schema_def.forEach((p, index) => {
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.textContent = `${p.name} (${tipoVisible(p.type)})`;
    const { input, read } = buildPropertyInput(p, undefined);
    // Sin `for`/`id` un lector anuncia seis «cuadro de edición» iguales y la
    // etiqueta no enfoca nada al pulsarla (M5).
    const inputId = idDeCampo("nueva-fila", index, p.key);
    input.id = inputId;
    label.htmlFor = inputId;
    field.append(label, input);
    wrap.appendChild(field);
    propInputs.push({ prop: p, input, read });
  });

  const submitBtn = document.createElement("button");
  submitBtn.className = "btn btn-primary";
  submitBtn.textContent = "Añadir fila";
  // El título de la fila ES el nombre del fichero (`Bases/<base>/<título>.md`):
  // una fila sin título no tiene identidad, y crearla dejaba un «Sin título.md»
  // de frontmatter y nada más que había que borrar a mano (M4).
  const SIN_TITULO = "Escribe un título: es el nombre del fichero de la fila.";
  const refrescarBoton = () => {
    submitBtn.disabled = !titleInput.value.trim();
    submitBtn.title = submitBtn.disabled ? SIN_TITULO : "";
  };
  submitBtn.disabled = true;
  submitBtn.title = SIN_TITULO;
  titleInput.addEventListener("input", refrescarBoton);
  submitBtn.addEventListener("click", async () => {
    const titulo = titleInput.value.trim();
    if (!titulo) {
      refrescarBoton();
      titleInput.focus();
      return;
    }
    const properties = {};
    for (const { prop, input, read } of propInputs) {
      // Al crear se guarda también un checkbox desmarcado; el resto de tipos
      // sin valor no se escriben (no hay que dejar `null` en el fichero).
      if (prop.type === "checkbox") {
        properties[prop.key] = input.checked;
      } else {
        const value = read();
        if (value !== null) properties[prop.key] = value;
      }
    }
    try {
      await api(`/databases/${database.id}/rows`, {
        method: "POST",
        body: JSON.stringify({ title: titulo, properties }),
      });
      selectDatabase(database.id, { focus: "addRow" });
    } catch (e) {
      alert("Error al crear la fila: " + e.message);
    }
  });
  wrap.appendChild(submitBtn);

  return wrap;
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
let dbDialogContext = null; // { database: Database|null, refresh, titleHidden }

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
  removeBtn.addEventListener("click", () => wrap.remove());

  row.append(nameInput, typeSelect, removeBtn);

  const optionsInput = document.createElement("input");
  optionsInput.placeholder = "opciones separadas por coma (Selección / Multiselección)";
  optionsInput.className = "prop-options-input";
  optionsInput.hidden = true;

  if (prop) {
    nameInput.value = prop.name;
    typeSelect.value = prop.type;
    optionsInput.value = (prop.options || []).join(", ");
    optionsInput.hidden = !["select", "multiselect"].includes(prop.type);
  }

  typeSelect.addEventListener("change", () => {
    optionsInput.hidden = !["select", "multiselect"].includes(typeSelect.value);
  });

  wrap.append(row, optionsInput);
  propRowsContainer.appendChild(wrap);
}

/** Abre el diálogo en alta (`database` nulo) o para editar una existente.
 * `refresh` repinta la vista actual cuando el PATCH del schema termina; el
 * diálogo recibe uno que relee las vistas, porque el servidor ha podido
 * reescribir su configuración al migrar el rename. */
function openDatabaseDialog(database = null, refresh = null) {
  dbDialogContext = {
    database,
    refresh,
    titleHidden: Boolean(database?.title_hidden),
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
  dbDialog.showModal();
}

/** El `schema_def` que describen las filas del diálogo, en el formato de la API. */
function readSchemaFromDialog() {
  const schema_def = [];
  propRowsContainer.querySelectorAll(".prop-row-wrap:not(.prop-title-row)").forEach((wrap) => {
    const name = wrap.querySelector(".prop-name").value.trim();
    const type = wrap.querySelector(".prop-type").value;
    const optionsRaw = wrap.querySelector(".prop-options-input").value.trim();
    // Una fila sin nombre no es una propiedad: no se inventa una clave con el
    // tipo por nombre (y así el botón «Propiedad» puede quedarse vacío).
    if (!name) return;
    const prop = { name, type };
    if (wrap.__propKey) prop.key = wrap.__propKey;
    if (["select", "multiselect"].includes(type) && optionsRaw) {
      prop.options = optionsRaw.split(",").map((s) => s.trim()).filter(Boolean);
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

document.getElementById("btn-add-prop").addEventListener("click", () => addPropertyRow());
document.getElementById("btn-cancel-database").addEventListener("click", () => {
  dbDialogContext = null;
  dbDialog.close();
});

document.getElementById("btn-save-database").addEventListener("click", async () => {
  const title = document.getElementById("db-name").value.trim() || "Sin título";
  const schema_def = readSchemaFromDialog();
  const { title_name, title_hidden } = readTitleColumnFromDialog();
  const { database, refresh } = dbDialogContext || {};

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
  document.getElementById("view-groupby-field").hidden = true;
  document.getElementById("view-date-field").hidden = true;
  viewDialog.showModal();
}

document.getElementById("view-type").addEventListener("change", (e) => {
  document.getElementById("view-groupby-field").hidden = e.target.value !== "board";
  // Sin propiedades de fecha no hay nada que elegir: el calendario se crea
  // igual y dentro dice que a la base le falta una fecha.
  document.getElementById("view-date-field").hidden =
    e.target.value !== "calendar" || document.getElementById("view-date").options.length === 0;
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
    if (dateKey) config.date_property = dateKey;
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

function setSidebarCollapsed(collapsed) {
  layoutEl.classList.toggle("sidebar-collapsed", collapsed);
  localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
}

document.getElementById("btn-toggle-sidebar").addEventListener("click", () => {
  if (esMovil()) return setCajonAbierto(!cajonAbierto());
  setSidebarCollapsed(!layoutEl.classList.contains("sidebar-collapsed"));
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
/** Deja la sección de la bandeja dentro del cajón.
 *
 * La barra lateral es la misma columna con o sin cajón: conserva el
 * `scrollTop` que tuviera, así que con el árbol desplazado la bandeja se
 * abría por encima del borde superior (y no se podía ni tocar). Al abrir se
 * corrige el scroll lo justo para que se vea; si ya se veía, no se mueve
 * (C2). */
function mostrarBandejaEnElCajon() {
  const section = document.getElementById("inbox-section");
  if (!sidebarEl || !section || section.hidden) return;
  const caja = sidebarEl.getBoundingClientRect();
  const bandeja = section.getBoundingClientRect();
  if (bandeja.top < caja.top) sidebarEl.scrollTop -= caja.top - bandeja.top;
  else if (bandeja.bottom > caja.bottom) sidebarEl.scrollTop += bandeja.bottom - caja.bottom;
}

function setCajonAbierto(open) {
  const abierto = Boolean(open) && esMovil();
  layoutEl.classList.toggle("sidebar-drawer-open", abierto);
  if (abierto) mostrarBandejaEnElCajon();
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
  if (e.target.closest(".tree-row, .search-hit, .inbox-open, .inbox-process")) setCajonAbierto(false);
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
// Oscuro por defecto; el interruptor de la cabecera conmuta a claro y la
// elección se recuerda en localStorage (preferencia de pantalla, no dato del
// workspace) -- SPEC-B §1.1.
const THEME_KEY = "mdm_theme";
const themeBtn = document.getElementById("btn-toggle-theme");

function applyTheme(theme) {
  const resolved = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = resolved;
  localStorage.setItem(THEME_KEY, resolved);
  if (themeBtn) {
    const isLight = resolved === "light";
    themeBtn.setAttribute("aria-pressed", isLight ? "true" : "false");
    themeBtn.title = isLight ? "Cambiar a tema oscuro" : "Cambiar a tema claro";
  }
}

themeBtn?.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  applyTheme(next);
});

applyTheme(localStorage.getItem(THEME_KEY) || "dark");

// -------------------------------------------------------------------- arranque

// Quién eres (P11b) antes de nada: si no hay sesión, /auth/me responde 401 y
// api() manda al login. Los usuarios que este navegador ya conoce se leen antes
// para que la autoría de las notas salga con su color desde el primer pintado.
loadKnownUsers();
initUserChip();
loadCurrentUser();

loadTree();
showEmpty(); // landing con páginas recientes + fechas cercanas, en vez del hueco estático de index.html
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
  try {
    // La fila abierta como página se guarda por el mismo camino: su cuerpo es
    // texto del usuario igual que el de una nota.
    rowPage?.beacon?.();
  } catch {
    // ídem
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
