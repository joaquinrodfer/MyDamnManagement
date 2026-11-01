// Visor de nodos del vault: Canvas 2D con simulación de fuerzas propia.
//
// Sin dependencias y sin meter nada en el bundle del editor: este módulo solo
// sabe pedir `GET /graph` y pintar lo que llega. El backend ya resuelve los dos
// modos (local / global), el tope de nodos, `truncated` y los enlaces rotos;
// aquí no se inventa nada de eso.
//
// Decisiones de pintado:
// - tamaño por grado (raíz cuadrada, para que un nodo con 300 enlaces no tape
//   media pantalla), color por tipo y etiqueta solo en los nodos grandes o al
//   pasar el ratón -- 400 etiquetas superpuestas no se leen.
// - la simulación se enfría (`alpha`) y el `requestAnimationFrame` **se para**
//   cuando se estabiliza: una animación perpetua se nota en la batería.
// - la repulsión la resuelve un quadtree (Barnes-Hut): el modo local a 2 saltos
//   puede traer más de mil nodos y un O(n²) por tick se arrastra.

const MAX_ZOOM = 4;
const MIN_ZOOM = 0.12;
const ALPHA_MIN = 0.002;
const ALPHA_DECAY = 0.022;
const CHARGE = 2600; // repulsión
const SPRING = 0.035; // rigidez de las aristas
const LINK_DISTANCE = 58; // longitud de reposo de una arista
const GRAVITY = 0.004; // tirón suave al centro, para que no se escape nada
const THETA2 = 0.81; // 0.9² -- cuándo un cuadrante se trata como una masa
const MOVE_TOLERANCE = 4; // px: por debajo de esto un clic no es un arrastre

let view = null; // la vista abierta (solo puede haber una)

/** Abre el visor sobre la página actual (o global si no hay ninguna).
 *
 * `onOpenPage(id)` lo pone app.js: el grafo no navega por su cuenta, solo
 * devuelve el id de la página que se ha pulsado.
 */
export function openGraph({ pageId = null, onOpenPage = null } = {}) {
  if (view) view.close();
  view = createView({ pageId, onOpenPage });
  return view;
}

function createView({ pageId, onOpenPage }) {
  const el = document.createElement("div");
  el.className = "graph-view";
  el.innerHTML = `
    <div class="graph-bar">
      <div class="graph-title">
        <svg class="svg-icon" aria-hidden="true" focusable="false"><use href="#i-graph"/></svg>
        <span>Grafo del vault</span>
      </div>
      <div class="graph-modes" role="group" aria-label="Modo del grafo">
        <button type="button" class="graph-tab" data-mode="local">Local</button>
        <button type="button" class="graph-tab" data-mode="global">Global</button>
      </div>
      <div class="graph-depths" role="group" aria-label="Saltos del grafo local">
        <span>Saltos</span>
        <button type="button" class="graph-depth" data-depth="1">1</button>
        <button type="button" class="graph-depth" data-depth="2">2</button>
      </div>
      <div class="graph-stats" id="graph-stats"></div>
      <div class="graph-actions">
        <button type="button" class="icon-btn" id="graph-fit" title="Encajar el grafo" aria-label="Encajar el grafo"><svg class="svg-icon" aria-hidden="true" focusable="false"><use href="#i-expand"/></svg></button>
        <button type="button" class="icon-btn" id="graph-close" title="Cerrar el grafo (Esc)" aria-label="Cerrar el grafo"><svg class="svg-icon" aria-hidden="true" focusable="false"><use href="#i-x"/></svg></button>
      </div>
    </div>
    <canvas id="graph-canvas" role="img" aria-label="Grafo de enlaces del vault"></canvas>
    <div class="graph-empty" id="graph-empty" hidden></div>
    <div class="graph-legend">
      <span><i class="graph-dot" data-type="note"></i>nota</span>
      <span><i class="graph-dot" data-type="database"></i>base de datos</span>
      <span class="graph-hint">arrastra · rueda para zoom · clic para abrir</span>
    </div>`;
  document.body.appendChild(el);

  const canvas = el.querySelector("#graph-canvas");
  const ctx = canvas.getContext("2d");
  const statsEl = el.querySelector("#graph-stats");
  const emptyEl = el.querySelector("#graph-empty");
  const tabs = [...el.querySelectorAll(".graph-tab")];
  const depthBtns = [...el.querySelectorAll(".graph-depth")];
  const colors = readColors();
  const bodyFont = getComputedStyle(document.body).fontFamily;

  const state = {
    mode: pageId ? "local" : "global",
    depth: 1,
    data: { nodes: [], edges: [], truncated: false, broken: 0 },
    nodes: [],
    edges: [],
    byId: new Map(),
    neighbors: new Map(),
    hover: null,
    dragging: null,
    panning: null,
    alpha: 1,
    raf: 0,
    transform: { x: 0, y: 0, k: 1 },
    moved: false,
    userMoved: false, // paneo/zoom/arrastre: no se reencuadra solo
    loadToken: 0,
  };

  function sizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, Math.round(rect.width * dpr));
    canvas.height = Math.max(1, Math.round(rect.height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function readColors() {
    const s = getComputedStyle(document.documentElement);
    const v = (name, fallback) => (s.getPropertyValue(name) || "").trim() || fallback;
    return {
      bg: v("--panel", "#161922"),
      edge: v("--panel-border", "#2a2f3d"),
      ink: v("--ink", "#eef0f6"),
      muted: v("--ink-muted", "#949db3"),
      accent: v("--accent-ink", "#9ba4ff"),
      note: v("--accent-ink", "#9ba4ff"),
      database: v("--warn", "#f5a623"),
      database_row: v("--ink-muted", "#949db3"),
    };
  }

  // ------------------------------------------------------------- simulación

  function tick() {
    const { nodes, edges, alpha } = state;
    if (!nodes.length) return;
    for (const n of nodes) {
      n.fx = 0;
      n.fy = 0;
    }
    // Repulsión Barnes-Hut.
    if (nodes.length > 1) {
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const n of nodes) {
        if (n.x < minX) minX = n.x;
        if (n.x > maxX) maxX = n.x;
        if (n.y < minY) minY = n.y;
        if (n.y > maxY) maxY = n.y;
      }
      const size = Math.max(maxX - minX, maxY - minY, 1) + 1;
      const tree = quad(nodes, minX, minY, size, 0);
      for (const n of nodes) repulse(n, tree);
    }
    // Muelles de las aristas: la estructura del vault se ve, que es el punto.
    // En un grafo pequeño los títulos son más largos que la distancia de
    // reposo y las etiquetas se pisarían: más aire cuando hay pocos nodos.
    const linkDistance = nodes.length <= 24 ? 96 : LINK_DISTANCE;
    for (const e of edges) {
      const a = e.a, b = e.b;
      let dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = (d - linkDistance) * SPRING * alpha;
      dx /= d; dy /= d;
      a.fx += dx * f; a.fy += dy * f;
      b.fx -= dx * f; b.fy -= dy * f;
    }
    let cx = 0, cy = 0;
    for (const n of nodes) { cx += n.x; cy += n.y; }
    cx /= nodes.length; cy /= nodes.length;
    for (const n of nodes) {
      if (n.pinned) { n.vx = 0; n.vy = 0; continue; }
      // Gravedad al centro de masas (no al origen): un componente suelto no
      // se va a la deriva y no obliga a buscar el grafo con el paneo.
      n.fx += (cx - n.x) * GRAVITY;
      n.fy += (cy - n.y) * GRAVITY;
      n.vx = (n.vx + n.fx * alpha) * 0.72;
      n.vy = (n.vy + n.fy * alpha) * 0.72;
      n.x += n.vx;
      n.y += n.vy;
    }
  }

  function quad(points, x0, y0, size, depth) {
    const n = points.length;
    if (n === 1) {
      const p = points[0];
      return { x0, y0, size, mass: 1, cx: p.x, cy: p.y, leaf: p, kids: null };
    }
    let sx = 0, sy = 0;
    for (const p of points) { sx += p.x; sy += p.y; }
    const cell = { x0, y0, size, mass: n, cx: sx / n, cy: sy / n, leaf: null, kids: null };
    // Demasiado profundo (o todos en el mismo píxel): se deja como masa única.
    if (depth >= 12 || size < 1) return cell;
    const h = size / 2;
    const buckets = [[], [], [], []];
    for (const p of points) {
      const i = (p.x >= x0 + h ? 1 : 0) + (p.y >= y0 + h ? 2 : 0);
      buckets[i].push(p);
    }
    cell.kids = buckets.map((b, i) =>
      b.length ? quad(b, x0 + (i & 1 ? h : 0), y0 + (i & 2 ? h : 0), h, depth + 1) : null
    );
    return cell;
  }

  function repulse(n, cell) {
    if (!cell || cell.mass === 0) return;
    if (cell.leaf === n) return; // no se repulsa a sí mismo
    const dx = n.x - cell.cx, dy = n.y - cell.cy;
    let d2 = dx * dx + dy * dy;
    // Un cuadrante lejano se trata como una masa puntual; uno cercano se abre.
    if (cell.leaf || (cell.size * cell.size) / d2 < THETA2) {
      if (d2 < 25) d2 = 25; // dos nodos en el mismo píxel: no dividas por ~0
      const d = Math.sqrt(d2);
      const f = (CHARGE * cell.mass) / d2;
      n.fx += (dx / d) * f;
      n.fy += (dy / d) * f;
      return;
    }
    for (const kid of cell.kids) repulse(n, kid);
  }

  function start() {
    if (state.raf) return;
    state.raf = requestAnimationFrame(frame);
  }

  function stop() {
    if (state.raf) cancelAnimationFrame(state.raf);
    state.raf = 0;
  }

  function frame() {
    state.raf = 0;
    if (!view) return;
    tick();
    state.alpha += (0 - state.alpha) * ALPHA_DECAY;
    draw();
    if (state.alpha < ALPHA_MIN && !state.dragging) {
      // Se acabó: el último fotograma ya está pintado y no se pide otro.
      state.alpha = 0;
      // Encuadre final con el layout ya asentado -- el `fit()` inicial se hace
      // sobre la espiral de arranque y el grafo crece al separarse los nodos.
      // Si el usuario ha paneado o hecho zoom, manda su vista.
      if (!state.userMoved) fit();
      return;
    }
    state.raf = requestAnimationFrame(frame);
  }

  function reheat(alpha = 0.5) {
    state.alpha = Math.max(state.alpha, alpha);
    start();
  }

  // ---------------------------------------------------------------- pintado

  function nodeRadius(node) {
    return Math.min(18, 4.5 + Math.sqrt(node.degree) * 2.2);
  }

  /** Umbral de etiqueta: los 12 de más grado, y todas si el grafo es pequeño
   * (en una vecindad de 5 nodos no sobra ninguna etiqueta). */
  function labelCutoff(nodes) {
    if (nodes.length <= 24) return 0;
    const grados = nodes.map((n) => n.degree).sort((a, b) => b - a);
    return Math.max(6, grados[11] ?? 0);
  }

  function draw() {
    const rect = canvas.getBoundingClientRect();
    const t = state.transform;
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = colors.bg;
    ctx.fillRect(0, 0, rect.width, rect.height);
    ctx.translate(t.x, t.y);
    ctx.scale(t.k, t.k);

    const hover = state.hover;
    const near = hover ? state.neighbors.get(hover.id) : null;
    const labelThreshold = labelCutoff(state.nodes);

    // Aristas primero (debajo de los nodos).
    ctx.lineWidth = 1 / t.k;
    for (const e of state.edges) {
      const active = hover && (e.a === hover || e.b === hover);
      ctx.strokeStyle = active ? colors.accent : colors.edge;
      ctx.globalAlpha = hover ? (active ? 0.9 : 0.12) : 0.55;
      ctx.beginPath();
      ctx.moveTo(e.a.x, e.a.y);
      ctx.lineTo(e.b.x, e.b.y);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    // Nodos.
    for (const n of state.nodes) {
      const r = nodeRadius(n);
      const faded = hover && n !== hover && !near?.has(n.id);
      ctx.globalAlpha = faded ? 0.16 : 1;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fillStyle = colors[n.type] || colors.muted;
      ctx.fill();
      if (n === hover) {
        ctx.lineWidth = 2 / t.k;
        ctx.strokeStyle = colors.ink;
        ctx.stroke();
      }
      if (!faded && (n.degree >= labelThreshold || n === hover || near?.has(n.id))) {
        ctx.fillStyle = colors.ink;
        ctx.font = `11px ${bodyFont}`;
        ctx.textAlign = "center";
        ctx.fillText(n.title, n.x, n.y - r - 5);
      }
    }
    ctx.globalAlpha = 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function fit() {
    const { nodes } = state;
    const rect = canvas.getBoundingClientRect();
    if (!nodes.length || !rect.width) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x);
      minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
    }
    const pad = 80;
    const k = Math.min(
      MAX_ZOOM,
      Math.max(MIN_ZOOM, Math.min(
        (rect.width - pad * 2) / Math.max(1, maxX - minX),
        (rect.height - pad * 2) / Math.max(1, maxY - minY)
      ))
    );
    state.transform = {
      k,
      x: rect.width / 2 - ((minX + maxX) / 2) * k,
      // 12 px de aire arriba: las etiquetas se pintan por encima del nodo.
      y: rect.height / 2 - ((minY + maxY) / 2) * k + 12,
    };
    draw();
  }

  // ----------------------------------------------------------- interacciones

  function toWorld(ev) {
    const rect = canvas.getBoundingClientRect();
    const t = state.transform;
    return { x: (ev.clientX - rect.left - t.x) / t.k, y: (ev.clientY - rect.top - t.y) / t.k };
  }

  function hit(ev) {
    const p = toWorld(ev);
    let best = null;
    let bestD = Infinity;
    const slack = 6 / state.transform.k;
    for (const n of state.nodes) {
      const d = Math.hypot(n.x - p.x, n.y - p.y);
      if (d <= nodeRadius(n) + slack && d < bestD) { best = n; bestD = d; }
    }
    return best;
  }

  canvas.addEventListener("pointerdown", (ev) => {
    canvas.setPointerCapture(ev.pointerId);
    state.moved = false;
    const n = hit(ev);
    if (n) {
      state.dragging = n;
      n.pinned = true;
      state.userMoved = true;
      n.vx = 0; n.vy = 0;
      reheat(0.6);
    } else {
      state.panning = { x: ev.clientX, y: ev.clientY, tx: state.transform.x, ty: state.transform.y };
      canvas.style.cursor = "grabbing";
    }
  });

  canvas.addEventListener("pointermove", (ev) => {
    if (state.dragging) {
      const p = toWorld(ev);
      const n = state.dragging;
      if (Math.hypot(p.x - n.x, p.y - n.y) > MOVE_TOLERANCE / state.transform.k) state.moved = true;
      n.x = p.x; n.y = p.y;
      n.vx = 0; n.vy = 0;
      reheat(0.3);
      return;
    }
    if (state.panning) {
      const dx = ev.clientX - state.panning.x, dy = ev.clientY - state.panning.y;
      if (Math.abs(dx) + Math.abs(dy) > MOVE_TOLERANCE) state.moved = true;
      state.transform.x = state.panning.tx + dx;
      state.transform.y = state.panning.ty + dy;
      state.userMoved = true;
      draw();
      return;
    }
    const n = hit(ev);
    if (n !== state.hover) {
      state.hover = n;
      canvas.style.cursor = n ? "pointer" : "grab";
      draw();
    }
  });

  function release() {
    if (state.dragging) {
      state.dragging.pinned = false;
      state.dragging = null;
      reheat(0.25);
    }
    state.panning = null;
    canvas.style.cursor = "grab";
  }

  canvas.addEventListener("pointerup", (ev) => {
    const node = state.dragging;
    release();
    if (state.moved) return;
    const target = node || hit(ev);
    if (target && onOpenPage) {
      const id = target.id;
      close();
      onOpenPage(id);
    }
  });
  canvas.addEventListener("pointercancel", release);

  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const t = state.transform;
    const factor = Math.exp(-ev.deltaY * 0.0015);
    const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, t.k * factor));
    const sx = ev.clientX - rect.left, sy = ev.clientY - rect.top;
    t.x = sx - ((sx - t.x) / t.k) * k;
    t.y = sy - ((sy - t.y) / t.k) * k;
    t.k = k;
    state.userMoved = true;
    draw();
  }, { passive: false });

  canvas.addEventListener("dblclick", () => fit());
  canvas.addEventListener("pointerleave", () => {
    if (state.hover) { state.hover = null; draw(); }
  });

  el.querySelector("#graph-fit").addEventListener("click", fit);
  el.querySelector("#graph-close").addEventListener("click", () => close());

  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      if (tab.disabled || state.mode === tab.dataset.mode) return;
      state.mode = tab.dataset.mode;
      load();
    });
  }
  for (const btn of depthBtns) {
    btn.addEventListener("click", () => {
      if (state.depth === Number(btn.dataset.depth)) return;
      state.depth = Number(btn.dataset.depth);
      if (state.mode === "local") load();
      else syncChrome();
    });
  }

  function onKeydown(ev) {
    if (ev.key === "Escape") close();
  }
  document.addEventListener("keydown", onKeydown);
  const onResize = () => { sizeCanvas(); draw(); };
  window.addEventListener("resize", onResize);
  canvas.style.cursor = "grab";

  function close() {
    if (!view) return;
    stop();
    document.removeEventListener("keydown", onKeydown);
    window.removeEventListener("resize", onResize);
    el.remove();
    view = null;
  }

  // ------------------------------------------------------------------ datos

  function syncChrome() {
    const localDisabled = !pageId;
    for (const tab of tabs) {
      tab.disabled = tab.dataset.mode === "local" && localDisabled;
      tab.classList.toggle("is-active", tab.dataset.mode === state.mode);
      tab.setAttribute("aria-pressed", String(tab.dataset.mode === state.mode));
      tab.title = tab.disabled ? "Abre una nota para ver su vecindad" : "";
    }
    for (const btn of depthBtns) {
      btn.classList.toggle("is-active", Number(btn.dataset.depth) === state.depth);
      btn.setAttribute("aria-pressed", String(Number(btn.dataset.depth) === state.depth));
      btn.disabled = state.mode !== "local";
    }
    el.querySelector(".graph-depths").hidden = state.mode !== "local";
    const { nodes, edges, truncated, broken } = state.data;
    const partes = [
      `${nodes.length} nodo${nodes.length === 1 ? "" : "s"}`,
      `${edges.length} enlace${edges.length === 1 ? "" : "s"}`,
    ];
    if (truncated) partes.push("vista recortada: hay más nodos");
    if (broken) partes.push(`${broken} enlace${broken === 1 ? "" : "s"} roto${broken === 1 ? "" : "s"} sin nodo`);
    statsEl.textContent = partes.join(" · ");
    statsEl.classList.toggle("is-warn", truncated || broken > 0);
  }

  async function load() {
    syncChrome();
    const token = ++state.loadToken;
    stop();
    emptyEl.hidden = true;
    const params = new URLSearchParams({ mode: state.mode });
    if (state.mode === "local") {
      params.set("page_id", pageId);
      params.set("depth", String(state.depth));
    } else {
      params.set("limit", "400");
    }
    let data;
    try {
      const res = await fetch(`/graph?${params}`);
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error((body && body.detail) || `HTTP ${res.status}`);
      }
      data = await res.json();
    } catch (e) {
      if (token !== state.loadToken) return;
      emptyEl.hidden = false;
      emptyEl.textContent = `No se pudo cargar el grafo: ${e.message || e}`;
      statsEl.textContent = "";
      return;
    }
    if (token !== state.loadToken) return;
    applyData(data);
  }

  function applyData(data) {
    state.data = data;
    state.nodes = data.nodes.map((n, i) => {
      // Espiral áurea: reparto inicial sin solapamientos y determinista, así
      // el primer fotograma no es una nube aleatoria distinta cada vez.
      const angle = i * 2.399963;
      const radius = 14 * Math.sqrt(i + 0.5);
      return {
        id: n.id, title: n.title || "(sin título)", type: n.type, degree: n.degree,
        x: Math.cos(angle) * radius, y: Math.sin(angle) * radius,
        vx: 0, vy: 0, fx: 0, fy: 0, pinned: false,
      };
    });
    state.byId = new Map(state.nodes.map((n) => [n.id, n]));
    state.edges = [];
    state.neighbors = new Map(state.nodes.map((n) => [n.id, new Set()]));
    for (const e of data.edges) {
      const a = state.byId.get(e.source), b = state.byId.get(e.target);
      if (!a || !b) continue; // una arista con un extremo fuera no se pinta
      state.edges.push({ a, b, kind: e.kind });
      state.neighbors.get(a.id).add(b.id);
      state.neighbors.get(b.id).add(a.id);
    }
    state.hover = null;
    state.alpha = 1;
    state.userMoved = false;
    syncChrome();
    sizeCanvas();
    if (!state.nodes.length) {
      emptyEl.hidden = false;
      emptyEl.textContent = "Este grafo está vacío: todavía no hay notas enlazadas.";
      draw();
      return;
    }
    fit();
    start();
  }

  sizeCanvas();
  syncChrome();
  load();
  return { close };
}
