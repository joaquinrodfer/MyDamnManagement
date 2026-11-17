# ADR 0009 — La colaboración en vivo va antes que el CRDT, y la autoridad es el servidor

- **Estado:** aceptado (2026-11-17), por el dueño.
- **Contexto:** MyDamnManagement apunta a comercializarse a empresas «en un futuro
  muy futuro», y antes de eso el dueño quiere **probar la colaboración de verdad
  en su trabajo**: dos personas en la misma nota, a la vez. El
  [ADR-0005](ADR-0005-verdad-en-base-de-datos.md) dejaba la concurrencia en la
  etapa 3 (`If-Match`) y el tiempo real en la etapa 4, con una capa de
  sincronización CRDT «sobre el documento guardado». Con la vista puesta en
  vender, ese orden deja lo más demostrable para el final.
- **Relacionado:** [ADR-0005](ADR-0005-verdad-en-base-de-datos.md) (la verdad en
  la base), [ADR-0001](ADR-0001-formato-del-vault.md) (el `.md` como formato de
  escritura y exportación), [ADR-0002](ADR-0002-indice-sqlite.md) (el índice como
  dato derivado mientras los ficheros sean la verdad),
  [BACKLOG](BACKLOG-GESTOR-PROYECTOS.md) función 26 (presencia y edición
  simultánea).

## Decisiones

**D1 — Se adelanta la colaboración: nace la etapa 2.5.**

Entre «los cuerpos dejan de necesitar el fichero» (etapa 2 del ADR-0005) y
«varios escritores con `If-Match`» (etapa 3) entra una etapa nueva: **edición en
vivo de la misma nota por dos personas, con cursores y presencia**. Es lo que se
puede enseñar y probar en el trabajo, y no depende de tener permisos finos ni
multiespacio.

**D2 — La autoridad es el servidor; el cliente propone, no dicta.**

El servidor guarda, por documento, el cuerpo y un número de `version`. El cliente
manda sus cambios junto con la versión desde la que los hizo (`lastSeenVersion`).
Si el servidor está en otra versión, **rebasa** los cambios que le llegan sobre
lo que ya tiene y los aplica; si no puede rebasar sin ambigüedad, rechaza y el
cliente vuelve a pedir el documento. Es el modelo de `prosemirror-collab`, no el
de CRDT.

El motivo es concreto y no ideológico: aquí **ya hay una autoridad** (la API y su
base). Lo único que un CRDT compra por encima de esto es edición *peer-to-peer
sin autoridad*, que en este producto no aporta nada. A cambio, un CRDT solo
garantiza **convergencia**, no corrección: dos clientes pueden coincidir en un
documento corrupto y no habrá ningún error que lo delate. Para un cliente de
empresa, la corrupción silenciosa es el peor fallo posible.

**D3 — `version` por documento entra en la etapa 2, no después.**

La columna se añade cuando el cuerpo pasa a la base, en el mismo cambio, aunque
todavía no haya nadie colaborando. Es barata entonces y es la pieza sin la cual
ni el rebase ni un futuro CRDT tienen dónde apoyarse. Se sube en cada escritura
del cuerpo y la usan tanto la colaboración como `If-Match`.

**D4 — El CRDT se aplaza, y antes hay que decidir texto contra bloques.**

No se descarta: se aplaza hasta que la etapa 2.5 esté en uso real. Si llega el
día, la decisión previa no es de librería sino de modelo: **¿el documento es
texto Markdown o son bloques?**. Con bloques (lo que hace Notion) el CRDT es
tratable y el editor se reescribe por dentro; con texto Markdown, el camino sano
es el rebase de D2. Este ADR no cierra esa puerta: deja escrito que se abre
después de la 2.5 y con datos de uso, no antes.

**D5 — Alcance mínimo de la etapa 2.5, dicho para que no se estire.**

Dos personas, la misma nota, cursores y presencia. Sin trabajo offline, sin
peer-to-peer y sin permisos por nota. Un WebSocket por nota, rebase en el
servidor, y el `.md` (etapa 2) se sigue escribiendo como proyección para el
export.

**D6 — Lo que esta decisión NO cambia.**

El export íntegro de la etapa 1 sigue siendo la red de seguridad y va primero.
Nada nuevo se guarda de una forma que no se pueda serializar a Markdown (la
obligación de la etapa 0 del ADR-0005 sigue vigente). Y los permisos por espacio
—hoy aplazados por producto— siguen siendo lo primero que pide una empresa, por
delante de la colaboración en vivo.

## Consecuencias

- **Coste de la etapa 2:** una columna `version` por documento y subirla en cada
  escritura. Bajo.
- **Coste de la etapa 2.5:** una ruta WebSocket, un registro de salas por nota en
  memoria, `awareness` para cursores y presencia, y adaptar el editor a aplicar
  cambios remotos sin romper las decoraciones (líneas en blanco, asas, bloques).
  Es la parte cara, y por eso tiene alcance cerrado en D5.
- **El editor deja de ser «abro, escribo, guardo»**: pasa a aplicar cambios que
  vienen de fuera. Los atajos y el menú `/` se mantienen; lo que cambia es que el
  documento puede moverse solo.
- **El export sigue mandando** mientras la verdad sea el `.md`: cualquier cambio
  de la colaboración tiene que poder escribirse a Markdown sin pérdida.
- **Prueba de aceptación de la 2.5**, la única que cuenta: dos navegadores en la
  misma nota, escribiendo a la vez en párrafos distintos y en el mismo, sin que
  ninguno pierda texto y viendo el cursor del otro.
- **Si la 2.5 no aguanta lo que se le pide**, ese es el dato que justifica el
  CRDT; no antes.
