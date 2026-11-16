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

## Lote 1 — «quién hace qué» (en curso)

| # | Función | Coste | Estado |
|---|---|---|---|
| 6 | Persona como propiedad (usuario real, con avatar) | medio | en tarjeta |
| 8 | «Mis tareas» global, cruzando todas las bases | medio | en tarjeta (espera a 6) |
| 2 | Subtareas con estado propio y recuento en la tarjeta | medio | en tarjeta |

## Lote 2 — «que la base tenga reglas»

| # | Función | Coste | Estado |
|---|---|---|---|
| 3 | Prioridad con orden semántico | bajo | pendiente |
| 5 | Estado «cerrado» con semántica (deja de contar en todas partes) | bajo | pendiente |
| 29 | Plantilla de tarea y de proyecto completo | bajo | pendiente |

## Lote 3 — «el día a día»

| # | Función | Coste | Estado |
|---|---|---|---|
| 19 | Calendario con rango inicio-fin y arrastre | medio | pendiente |
| 12 | Aviso de vencimiento dentro de la app | bajo | pendiente |

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

## Descartadas (y por qué)

Ninguna todavía. Las «aplazadas» no están descartadas: están esperando a que
exista la pieza de la que dependen.
