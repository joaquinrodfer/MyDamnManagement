// Edición en vivo de una nota (3.0, hito 4): el lado del navegador.
//
// El servidor es la autoridad (ADR-0009). Este módulo:
//
// - abre `WS /ws/pages/{id}` y se une a la sala (`join`);
// - manda los cambios locales que `@codemirror/collab` tiene sin confirmar,
//   con la versión desde la que se hicieron (`update`), de uno en uno en vuelo;
// - aplica los que llegan (`update`, `ack`, `reject` con cambios): ahí
//   `receiveUpdates` rebasa los locales sin confirmar encima, que es lo que hace
//   que dos personas escribiendo a la vez no pierdan nada;
// - si el servidor no puede ponerle al día (`reject` con documento), recarga
//   el documento entero;
// - pinta el cursor de los demás (`presence`) y manda el propio;
// - se reconecta solo si se cae la conexión, sin perder lo no confirmado.
//
// Las transacciones que vienen del servidor llevan `filter: false` (ningún
// filtro del editor puede reescribir un cambio que ya es de la autoridad: el
// documento dejaría de ser el del servidor) y la anotación `remoteChange`, con
// la que el resto del editor sabe que no son del usuario (no recorta, no
// autoguarda, no entra en el historial de deshacer).

import {
  collab,
  getClientID,
  getSyncedVersion,
  receiveUpdates,
  sendableUpdates,
} from "@codemirror/collab";
import {
  Annotation,
  ChangeSet,
  Compartment,
  EditorState,
  Facet,
  StateEffect,
  StateField,
  Transaction,
} from "@codemirror/state";
import { Decoration, EditorView, WidgetType } from "@codemirror/view";

/** Marca de las transacciones que llegan del servidor. */
export const remoteChange = Annotation.define();

/** `true` mientras la nota está en una sala (el resto del editor lo consulta). */
export const collabActive = Facet.define({ combine: (v) => v.some(Boolean) });

export const collabCompartment = new Compartment();
export const readOnlyCompartment = new Compartment();

// ------------------------------------------------------- cursores de los demás

const setPeer = StateEffect.define();
const dropPeer = StateEffect.define();
const dropAllPeers = StateEffect.define();

class CaretWidget extends WidgetType {
  constructor(name, color) {
    super();
    this.name = name;
    this.color = color;
  }
  eq(other) {
    return other.name === this.name && other.color === this.color;
  }
  toDOM() {
    const caret = document.createElement("span");
    caret.className = "cm-mdm-peer-caret";
    caret.style.setProperty("--peer", this.color);
    const label = document.createElement("span");
    label.className = "cm-mdm-peer-label";
    label.textContent = this.name;
    caret.append(label);
    // El nombre sale al pasar el ratón (CSS); para un lector de pantalla, aquí.
    caret.setAttribute("aria-label", `Cursor de ${this.name}`);
    return caret;
  }
  ignoreEvent() {
    return true;
  }
}

const peersField = StateField.define({
  create: () => new Map(),
  update(peers, tr) {
    let next = peers;
    if (tr.docChanged && peers.size) {
      next = new Map();
      for (const [id, p] of peers) {
        next.set(id, {
          ...p,
          anchor: tr.changes.mapPos(p.anchor, 1),
          head: tr.changes.mapPos(p.head, 1),
        });
      }
    }
    for (const e of tr.effects) {
      if (e.is(setPeer)) {
        if (next === peers) next = new Map(peers);
        const len = tr.newDoc.length;
        next.set(e.value.peer, {
          ...e.value,
          anchor: Math.max(0, Math.min(len, e.value.anchor)),
          head: Math.max(0, Math.min(len, e.value.head)),
        });
      } else if (e.is(dropPeer)) {
        if (next === peers) next = new Map(peers);
        next.delete(e.value);
      } else if (e.is(dropAllPeers)) {
        next = new Map();
      }
    }
    return next;
  },
  provide: (f) =>
    EditorView.decorations.from(f, (peers) => {
      const ranges = [];
      for (const p of peers.values()) {
        const color = p.user?.color || "#8b5cf6";
        const name = p.user?.name || "Alguien";
        const from = Math.min(p.anchor, p.head);
        const to = Math.max(p.anchor, p.head);
        if (from < to) {
          ranges.push(
            Decoration.mark({
              class: "cm-mdm-peer-sel",
              attributes: { style: `--peer: ${color}` },
            }).range(from, to),
          );
        }
        ranges.push(Decoration.widget({ widget: new CaretWidget(name, color), side: 1 }).range(p.head));
      }
      return Decoration.set(ranges, true);
    }),
});

export const collabBaseExtensions = [
  collabCompartment.of([]),
  readOnlyCompartment.of([]),
  peersField,
];

// ------------------------------------------------------------- la conexión

const RECONNECT_MS = [500, 1000, 2000, 4000, 8000];
const GRACE_MS = 2000;

/**
 * Une `view` a la sala de la nota `pageId`. Devuelve `{stop, status}`.
 *
 * `onStatus(estado, detalle)` cuenta cómo va: `connecting`, `live`,
 * `readonly`, `saving` (hay cambios sin confirmar), `offline` (se cayó y se
 * reintenta), `unavailable` (no hay sala: la app sigue guardando como antes),
 * `gone` (la nota ya no existe).
 */
export function startCollab(view, { pageId, onStatus = () => {}, onRemoteReset = () => {} }) {
  let ws = null;
  let stopped = false;
  let joined = false;
  let epoch = null;
  let inFlight = false;
  let retry = 0;
  let everJoined = false;
  let presenceTimer = null;
  let lastPresence = "";
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/pages/${pageId}`;

  const status = (s, d) => {
    try {
      onStatus(s, d);
    } catch (e) {
      console.error(e);
    }
  };

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }

  // El documento tal y como lo tiene el servidor en nuestra versión confirmada.
  // Con él, al reconectar a una sala que renació (el servidor se reinició) con
  // ese mismo texto, lo que había sin confirmar se vuelve a aplicar encima en
  // vez de perderse (ver `joined`).
  let confirmedDoc = null;

  function applyRemote(updates) {
    const parsed = updates.map((u) => ({
      changes: ChangeSet.fromJSON(u.changes),
      clientID: u.clientID,
    }));
    if (confirmedDoc) {
      for (const u of parsed) confirmedDoc = u.changes.apply(confirmedDoc);
    }
    const spec = receiveUpdates(view.state, parsed);
    view.dispatch({
      ...spec,
      filter: false,
      annotations: [
        ...[].concat(spec.annotations || []),
        remoteChange.of(true),
        Transaction.addToHistory.of(false),
      ],
    });
  }

  /** Aplica `updates` que empiezan en `from`, si encajan con lo que hay. */
  function receive(from, updates) {
    const synced = getSyncedVersion(view.state);
    if (from > synced) {
      send({ type: "pull", version: synced }); // nos falta algo: que lo mande
      return false;
    }
    const nuevos = updates.slice(synced - from);
    if (nuevos.length) applyRemote(nuevos);
    return true;
  }

  function resetTo(doc, version) {
    // El documento de la sala manda (al entrar, o cuando la sala no puede
    // ponernos al día). Lo que hubiera sin confirmar aquí se pierde: es el caso
    // raro (la sala se reinició) y la app lo avisa (`onRemoteReset`). Si el
    // texto ya es el mismo no se toca: reemplazarlo entero movería el cursor.
    const igual = doc === view.state.doc.toString();
    view.dispatch({
      ...(igual ? {} : { changes: { from: 0, to: view.state.doc.length, insert: doc } }),
      filter: false,
      annotations: [remoteChange.of(true), Transaction.addToHistory.of(false)],
      effects: [
        collabCompartment.reconfigure([]),
        dropAllPeers.of(null),
      ],
    });
    view.dispatch({
      effects: collabCompartment.reconfigure([
        collab({ startVersion: version, clientID: getClientIDSafe() }),
        collabActive.of(true),
      ]),
    });
    confirmedDoc = view.state.doc;
    inFlight = false;
  }

  /** Lo pendiente (relativo a `confirmedDoc`), como un solo ChangeSet. */
  function pendingChanges() {
    let all = null;
    for (const u of sendableUpdates(view.state)) all = all ? all.compose(u.changes) : u.changes;
    return all;
  }

  let clientID = null;
  function getClientIDSafe() {
    if (!clientID) clientID = Math.random().toString(36).slice(2, 11);
    return clientID;
  }

  function push() {
    if (!joined || inFlight || !ws || ws.readyState !== WebSocket.OPEN) return;
    const updates = sendableUpdates(view.state);
    if (!updates.length) {
      status(view.state.facet(EditorState.readOnly) ? "readonly" : "live");
      return;
    }
    inFlight = true;
    status("saving");
    send({
      type: "update",
      version: getSyncedVersion(view.state),
      updates: updates.map((u) => ({ clientID: u.clientID, changes: u.changes.toJSON() })),
    });
  }

  function sendPresence() {
    presenceTimer = null;
    if (!joined) return;
    const { anchor, head } = view.state.selection.main;
    const key = `${anchor}:${head}:${getSyncedVersion(view.state)}`;
    if (key === lastPresence) return;
    lastPresence = key;
    send({ type: "presence", anchor, head, version: getSyncedVersion(view.state) });
  }

  function onMessage(ev) {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (closing) {
      // Saliendo: solo interesa saber que lo último llegó.
      if (msg.type === "ack" || msg.type === "reject") close();
      return;
    }
    switch (msg.type) {
      case "joined": {
        retry = 0;
        const reconexion = joined || everJoined;
        if (msg.doc !== undefined) {
          const pendiente = reconexion ? pendingChanges() : null;
          const misma = confirmedDoc && msg.doc === confirmedDoc.toString();
          if (pendiente && !misma) onRemoteReset(); // no hay forma de colocarlo: se pierde
          const sel = view.state.selection;
          resetTo(msg.doc, msg.version);
          if (pendiente && misma) {
            // La sala renació con el texto que teníamos confirmado: lo nuestro
            // encaja encima. Se vuelve a aplicar (ya como cambio nuevo de la
            // sala nueva) y se manda abajo, con `push()`.
            view.dispatch({
              changes: pendiente,
              // `sel` es del texto con lo pendiente, que es el que queda ahora.
              selection: sel,
              annotations: [Transaction.addToHistory.of(false)],
              userEvent: "input.collab",
            });
          }
        } else if (Array.isArray(msg.updates)) {
          receive(msg.version, msg.updates);
        }
        epoch = msg.epoch;
        joined = true;
        everJoined = true;
        view.dispatch({
          effects: [
            readOnlyCompartment.reconfigure(
              msg.readonly ? [EditorState.readOnly.of(true), EditorView.editable.of(false)] : [],
            ),
            dropAllPeers.of(null),
            ...(msg.peers || [])
              .filter((p) => Number.isInteger(p.head))
              .map((p) => setPeer.of(p)),
          ],
        });
        status(msg.readonly ? "readonly" : "live");
        inFlight = false;
        push();
        sendPresence();
        break;
      }
      case "ack":
        if (receive(msg.version, msg.updates || [])) inFlight = false;
        push();
        break;
      case "update":
        receive(msg.version, msg.updates || []);
        break;
      case "reject":
        inFlight = false;
        if (msg.doc !== undefined) {
          if (msg.epoch) epoch = msg.epoch;
          if (sendableUpdates(view.state).length) onRemoteReset();
          resetTo(msg.doc, msg.version);
          status("live");
        } else if (msg.reason === "readonly") {
          status("readonly");
        } else if (Array.isArray(msg.updates)) {
          receive(msg.version, msg.updates);
          push(); // ya rebasados: otra vez
        }
        break;
      case "presence":
        if (msg.gone) view.dispatch({ effects: dropPeer.of(msg.peer) });
        else if (Number.isInteger(msg.head)) view.dispatch({ effects: setPeer.of(msg) });
        break;
      case "gone":
        status("gone");
        stop();
        break;
      default:
        break;
    }
  }

  function connect() {
    if (stopped) return;
    status(everJoined ? "offline" : "connecting");
    ws = new WebSocket(url);
    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          type: "join",
          lastSeenVersion: everJoined ? getSyncedVersion(view.state) : null,
          epoch: everJoined ? epoch : null,
        }),
      );
    };
    ws.onmessage = onMessage;
    ws.onclose = (ev) => {
      joined = false;
      inFlight = false;
      ws = null;
      if (stopped) return;
      if (ev.code === 4401 || ev.code === 4403 || ev.code === 4404) {
        // Sin permiso o sin sesión: no hay sala que reintentar.
        status(ev.code === 4404 && everJoined ? "gone" : "unavailable", ev.code);
        stopped = true;
        return;
      }
      if (!everJoined && retry >= 2) {
        // Nunca llegó a entrar (un servidor sin salas, un proxy sin WebSocket):
        // la app sigue con el guardado de siempre.
        status("unavailable", ev.code);
        stopped = true;
        return;
      }
      const espera = RECONNECT_MS[Math.min(retry, RECONNECT_MS.length - 1)];
      retry += 1;
      setTimeout(connect, espera);
    };
  }

  // Cada cambio local (o de selección) se manda; los remotos no.
  const listener = EditorView.updateListener.of((u) => {
    const remoto = u.transactions.some((tr) => tr.annotation(remoteChange));
    if (u.docChanged && !remoto) push();
    if ((u.selectionSet || u.docChanged) && !presenceTimer) {
      presenceTimer = setTimeout(sendPresence, 120);
    }
  });
  view.dispatch({ effects: StateEffect.appendConfig.of(listener) });

  let closing = false;

  /** Sale de la sala. Con `graceful`, si queda algo sin confirmar, se manda y
   * se espera al `ack` (como mucho `GRACE_MS`) antes de cerrar: navegar justo
   * después de escribir no pierde la última palabra. Mientras tanto ya no se
   * aplica nada al editor (puede estar destruido). */
  function stop({ graceful = false } = {}) {
    stopped = true;
    if (presenceTimer) clearTimeout(presenceTimer);
    const quedan = joined && sendableUpdates(view.state).length > 0;
    if (graceful && quedan && ws && ws.readyState === WebSocket.OPEN) {
      closing = true;
      if (!inFlight) {
        inFlight = true;
        send({
          type: "update",
          version: getSyncedVersion(view.state),
          updates: sendableUpdates(view.state).map((u) => ({
            clientID: u.clientID,
            changes: u.changes.toJSON(),
          })),
        });
      }
      setTimeout(() => close(), GRACE_MS);
      return;
    }
    close();
  }

  function close() {
    try {
      ws?.close(1000);
    } catch {
      /* ya cerrada */
    }
  }

  connect();
  return {
    stop,
    /** ¿Queda algo sin confirmar por el servidor? */
    pending: () => joined === false || sendableUpdates(view.state).length > 0,
    get clientID() {
      return joined ? getClientID(view.state) : null;
    },
  };
}
