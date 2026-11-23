# Backlog: gestor de proyectos

Las 34 funciones candidatas de
[INVESTIGACION-GESTOR-PROYECTOS.md](INVESTIGACION-GESTOR-PROYECTOS.md), con su
estado. Este fichero es la recámara: la investigación decide **qué** se podría
hacer, esto dice **en qué punto está cada cosa**.

Reglas de este backlog:

- **Nada se borra de aquí**: una función que se descarta se queda con `descartada`
  y el motivo, porque el motivo suele volver a preguntarse meses después.
- **El estado lo mueve el orquestador al cerrar la tarjeta**, no el que
  implementa: `pendiente` → `en tarjeta` (con el id) → `hecha`.
- Un lote son **1-3 tarjetas** que se pueden probar juntas. No se abre un lote
  hasta cerrar el anterior: con un trabajador, tres lotes abiertos son tres
  frentes a medias.

## Lote 1 — «quién hace qué» (cerrado)

| # | Función | Coste | Estado |
|---|---|---|---|
| 6 | Persona como propiedad (usuario real, con avatar) | medio | hecha (v2.5.x, 5770157) |
| 8 | «Mis tareas» global, cruzando todas las bases | medio | hecha (8259823) |
| 2 | Subtareas con estado propio y recuento en la tarjeta | medio | hecha (16c8e57) |

## Lote 2 — «que la base tenga reglas» (cerrado)

| # | Función | Coste | Estado |
|---|---|---|---|
| 5 | Estado «cerrado» con semántica (deja de contar en todas partes) | bajo | hecha (b72329e) |
| 3 | Orden por tipo, con manual/auto en las selecciones | bajo | hecha (dd0d1ae) |
| 29 | Plantilla de tarea y de proyecto completo | bajo | hecha (645963c) |

## Lote 3 — «el día a día» (cerrado, v2.6.0)

| # | Función | Coste | Estado |
|---|---|---|---|
| 19 | Calendario con rango inicio-fin y arrastre | medio | hecha (`6c3a8b0`) |
| 12 | Aviso de vencimiento dentro de la app | bajo | hecha (`c88e9a6`) |

## Fuera de lote, ya hecho

| Función | Estado |
|---|---|
| Añadir fila en el sitio, como en Notion (fila fantasma en vez del formulario, plantillas en el «▾», estado de la columna en el tablero) | hecha (`903c54b`), después de la 2.6.0 |
| Pulido visual completo (dirección «Mesa de Operaciones»: tabla, tablero, calendario, barra lateral, estados vacíos, foco, móvil 375 px) | hecha (`18da5a6`) |

## Lote 4 — «memoria del trabajo»

| # | Función | Coste | Estado |
|---|---|---|---|
| 23 | Comentarios con hilo y autor | medio | pendiente |
| 27 | Historial de cambios por tarea | medio | pendiente |

## Resto, por tema

| # | Función | Coste | Estado |
|---|---|---|---|
| 1 | Tarea = página con cuerpo y checklist | bajo | ya se puede con bases (una fila es una página desde v2.5) |
| 4 | Etiquetas multivalor entre proyectos | bajo | ya se puede con bases (multiselección) |
| 7 | Varios responsables por tarea | bajo | pendiente (depende de 6) |
| 9 | Estimación por tarea | bajo | ya se puede con bases (número) |
| 10 | Carga de trabajo por persona y semana | alto | pendiente (necesita 6 y 9) |
| 11 | Fecha de inicio + límite (duración) | bajo | ya se puede con bases (dos fechas) |
| 13 | Dependencia declarada (bloquea / bloqueado por) | medio | pendiente |
| 14 | Bloqueo efectivo (no cerrar una tarea bloqueada) | alto | pendiente |
| 15 | Reprogramación en cascada al mover una fecha | alto | pendiente |
| 16 | Hito (fecha sin duración, en el calendario) | bajo | pendiente |
| 17 | Recurrencia de tareas | medio | pendiente |
| 18 | Cronograma / Gantt | alto | aplazada (necesita 6, 9 y 13) |
| 20 | Vista de carga (personas en columnas) | alto | aplazada (necesita 10) |
| 21 | Subagrupación / swimlanes | medio | pendiente |
| 22 | Vista guardada con URL propia | bajo | ya se puede con bases (cada vista es una pestaña) |
| 24 | Menciones `@usuario` con aviso | medio | pendiente (depende de 23) |
| 25 | Bandeja de notificaciones | medio | pendiente (depende de 23 y 12) |
| 26 | Presencia / edición simultánea | alto | aplazada: la arquitectura ya está decidida (ADR-0005, opción B); falta la etapa 3 |
| 28 | Reglas «si X entonces Y» | alto | pendiente (depende de 5) |
| 30 | Campos calculados (fórmula, rollup, % completado) | medio | pendiente (depende de 2) |
| 31 | Agente que propone cambios con revisión | bajo | ya se puede (P15, sugerencias con revisión) |
| 32 | Permisos por proyecto o base | alto | aplazada (con una persona, cero valor) |
| 33 | Importar/exportar CSV con esquema | medio | pendiente, y sube de prioridad con ADR-0005 |
| 34 | API con webhooks salientes | medio | pendiente |

## Operación: que todo se ajuste desde la app

Pedido por el dueño el 2026-11-23, al activar las copias en producción (#84).
Hoy, para cambiar algo de esto, hay que tocar el `docker-compose.prod.yml` del
servidor.

| # | Función | Coste | Estado |
|---|---------|-------|--------|
| O1 | **Copias fuera de la máquina desde Ajustes → Copias:** elegir un destino (Google Drive primero; luego WebDAV/S3/carpeta de red), conectarlo con OAuth sin pegar credenciales a mano y subir cada copia programada. Cifrar antes de subir: la copia lleva los hashes de las contraseñas. Avisar si una subida falla. | medio | pendiente |
| O2 | **Carpeta, frecuencia y nº de copias** desde la app, no por variables de entorno. Hoy la frecuencia y el nº ya se eligen ahí; la carpeta (`BACKUP_DIR`) no. | bajo | pendiente |
| O3 | **Dirección pública (`PUBLIC_URL`)** editable desde Ajustes, con una prueba de que responde por HTTPS antes de guardarla. | bajo | pendiente |
| O4 | **Estado del servidor en Ajustes:** espacio libre en disco, última copia buena y si la última subida externa falló, con aviso al administrador. | bajo | pendiente |

## Aplazado a una versión grande: la migración a base de datos

[ADR-0005](ADR-0005-verdad-en-base-de-datos.md) decidió que la verdad pase a la
base y el Markdown quede como autoría y exportación. **El dueño lo ha aplazado
explícitamente**: será una versión mayor (v3 o v4), no parte de esta fase. El
orden sigue siendo:

1. **Export íntegro y verificado** (del índice a un vault completo, con test de
   ida y vuelta). Es la red de seguridad: sin ella la decisión no es reversible.
2. **Los cuerpos dejan de necesitar el fichero** (`pages.body` ya guarda el
   cuerpo) **y, en el mismo cambio, una `version` por documento**
   ([ADR-0009](ADR-0009-colaboracion-con-autoridad.md) D3).
2.5. **Colaboración en vivo con autoridad en el servidor** (ADR-0009): dos
   personas en la misma nota, cursores y presencia, cambios rebasados por el
   servidor con `lastSeenVersion`. Sin offline y sin peer-to-peer.
3. **Versiones por documento y varios escritores** (`If-Match`).
4. **Tiempo real** (presencia, cursores) — que es la función 26 de arriba.
   El CRDT se aplaza hasta después de la 2.5, y antes hay que decidir si el
   documento son bloques o texto (ADR-0009 D4).

Mientras esté aquí, la obligación que sí aplica desde ya: **nada nuevo se guarda
de una forma que no se pueda escribir a `.md` sin pérdida**.

## Descartadas (y por qué)

Ninguna todavía. Las «aplazadas» no están descartadas: están esperando a que
exista la pieza de la que dependen.
