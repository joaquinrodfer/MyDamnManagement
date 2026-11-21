# Plan: espacios de trabajo, bases de proyectos y tareas, carga del equipo y avisos

Estado: **implementado** (2026-11-18/19, PR #23 a #37). Lo que queda aparte está en «Pendiente» al final.

Sale del planning en cuatro fases que trajo el dueño y de la conversación posterior, donde se cambiaron varias piezas. Aquí está lo **decidido**; lo que todavía es suposición va marcado como tal en «Supuestos a confirmar», al final.

Cada hito es una rama y un PR contra `develop`. **Los fusiona Claude** cuando los tests, los guiones de verificación y la prueba en el navegador están en verde (acordado con el dueño el 2026-11-18: «si sale mal puedo volver atrás»). Develop despliega a producción en unos 5 minutos. Las dudas que surjan las decide Claude con el criterio de este plan y las apunta en el PR, en «Decisiones que he tomado yo». Un hito grande puede ir en varias tiradas, cada una con su PR.

**Volver atrás un hito con migración.** Revertir el código no deshace un cambio de esquema del índice. Por eso H0 incluye una copia automática del índice antes de cada migración: para volver atrás hay que revertir el PR **y** restaurar esa copia.

---

## Decisiones tomadas

### Espacios y permisos

- **Espacios separados.** Una persona puede estar en varios. Páginas, bases, búsqueda, grafo, Mis tareas, eventos en vivo, salas de edición, agentes y export se filtran por el espacio activo. Quien no está en un espacio no ve ni busca nada de él.
- **Migración.** El espacio único que existe hoy pasa a llamarse **General**, con todos los usuarios y todas las páginas dentro.
- **Grupos.** Se crean en *Ajustes › Administración* y se les meten usuarios. El acceso a un espacio se da a una persona o a un grupo entero.
- **Roles dentro de un espacio:**
  - `admin`: gestiona miembros y ajustes del espacio;
  - `editor`: crea, edita, mueve y borra;
  - `viewer`: solo lectura. La app no tiene comentarios, así que de momento no hay «lectura y comentarios».
- **Nivel instancia:**
  - el `is_admin` actual es el superadministrador y **puede leer el contenido de todos los espacios**. Se revisará si da problemas;
  - `can_view_global_workload` es un permiso aparte que da acceso a la Carga del equipo de todos los espacios sin ser administrador.
- **Lo nuevo es de todo el espacio.** Una página creada en un espacio la ve todo el espacio, cada uno con su rol.
- **Dos secciones en la barra lateral de cada espacio:**
  - **Compartido**: lo del espacio;
  - **Privado**: lo de cada usuario, que solo ve él (y el superadministrador).
- **Mover libremente.** Páginas y bases se pueden mover entre Privado y Compartido, de madre y de espacio.
- **Invitados.** Una página se puede compartir con alguien de fuera del espacio. Esa persona ve esa página y sus hijas, nada más. No aparece como miembro del espacio, sino como **invitado**.
- **Permisos por página.** Siguen existiendo, como excepción al rol del espacio (restringir una página, abrirla a un invitado). El modelo actual (`acl`, `acl_roots`, `acl_source`, una sola comprobación en `authorize`) se **amplía, no se sustituye**:
  - el grupo entra como un sujeto más;
  - el rol del espacio entra como el permiso de partida.

### Estado de las filas

Tres niveles:

1. **Opciones**, libres: «Bloqueada», «En revisión», «Esperando cliente»…
2. **Grupos**, que define cada equipo: «Pendiente», «En curso», «Hecho»… Cada opción va dentro de un grupo.
3. **Cerrado**, la única regla fija. Cada grupo dice si cuenta como cerrado.

Es el «cerrado» de la ADR-0007, subido del valor al grupo. Mis tareas, los contadores de subtareas, los avisos de vencimiento y la carga solo preguntan «¿está cerrada?», así que los grupos se pueden reorganizar sin romper ningún cálculo. Las bases que ya tienen una regla de cerrado se migran sin perderla.

### Tipos de base

Al crear una base sale una galería de tarjetas: **Proyectos**, **Tareas** y **Vacía**. La vacía es la base en blanco de ahora, sin tipo.

Cada tipo es una **ficha en el backend**, así que un tipo nuevo se añade escribiendo otra ficha y sin tocar las pantallas. La ficha define:
- nombre, icono y descripción de la tarjeta;
- sus **columnas obligatorias**, cada una con nombre, tipo y papel;
- los grupos de estado de partida;
- las vistas por defecto;
- las **opciones** de la base.

Las columnas obligatorias se pueden renombrar y ocultar, pero **no borrar ni cambiar de tipo**. También se puede convertir una base existente en Proyectos o en Tareas: la app reutiliza las columnas que encajan y crea las que faltan.

| Tipo | Columnas obligatorias | Opciones de la base |
|---|---|---|
| **Proyectos** | Responsable (persona), Estado, Fechas (inicio y fin) | Qué columna numérica es la **estimación** (opcional, la añade el usuario) |
| **Tareas** | Responsables (personas), Estado, Fechas (inicio y fin), Horas, Proyecto (relación con una base de Proyectos, **puede ir vacía**) | Reparto de horas entre varios responsables: **completas a cada uno** o **a partes iguales**. Habrá más. |

- **Horas del proyecto.** La suma de las horas de sus tareas sale siempre, calculada. Si la base tiene estimación, se ven las dos, por ejemplo «estimado 120 h · tareas 95 h».
- **Dentro de un proyecto** se ven sus tareas: la lista, el avance (cerradas sobre total) y las horas pendientes.
- **Tareas de otro espacio.** Lo decide una opción del espacio del proyecto: «sus proyectos aceptan tareas de otros espacios».
- **Subtareas.** Las que ya existen siguen valiendo dentro de una base de tareas.

### Carga del equipo

- **Dónde está:** un enlace en *Mis cosas*, visible solo con `is_admin` o `can_view_global_workload`. Abarca todos los espacios.
- **Qué cuenta:** las filas **no cerradas** de todas las bases de tipo Tareas.
- **Reparto de horas:** por igual entre los días laborables del rango. Si solo hay fecha de fin, todas caen en la semana de esa fecha. Con varios responsables, según la opción de la base.
- **Origen de cada hora.** El cálculo no guarda totales: produce una lista de **piezas** y todo lo demás son sumas sobre ella (por persona y semana o mes, por proyecto, por espacio). La API devuelve piezas y sumas, para poder añadir funciones después sin rehacer el cálculo. Cada pieza es:

  > persona · espacio · proyecto · base · tarea · semana · horas

- **Saturación:** verde, amarillo o rojo frente a la **capacidad semanal de cada persona**, con 40 h por defecto.
- **Espacios a los que no perteneces:** se ven las horas y las fechas, el nombre del espacio y el título tapado como «Proyecto privado [Espacio]».
- **Desglose:** al desplegar a una persona, sale en qué espacio, proyecto y tarea está cada carga.

---

## Hitos

### H0 — Copias de seguridad

Va primero porque, desde la ADR-0005, la base de datos es la verdad y copiar el vault ya no basta. Además, H1 toca los permisos de todo.

- **Copia consistente en caliente:** el índice SQLite con `VACUUM INTO`, más los adjuntos, empaquetados en un `.tar.gz` con un manifiesto (versión de esquema, fecha, recuentos).
- ***Ajustes › Administración › Copias de seguridad*** (solo superadministrador):
  - descargar la copia ahora, con un aviso de que lleva usuarios y contraseñas cifradas;
  - programarla cada día o cada semana en `BACKUP_DIR`, conservando las N últimas.
- **Restaurar:** un procedimiento documentado y probado (arrancar con una copia y comprobar que todo está).
- **Copia antes de migrar.** Al arrancar, si el esquema del índice va a cambiar, se copia el índice tal cual estaba, por ejemplo `index.sqlite.antes-v14`, junto a él o en `BACKUP_DIR`, y después se migra. Esto va en producción antes que cualquier hito que migre.

### H0b — Reajuste de la versión móvil

Lo que se ve hoy en el móvil (captura del dueño, 2026-11-18): el `+` y los seis puntos de cada bloque están siempre a la vista y se comen el margen izquierdo, así que el texto queda apretado a la derecha. La barra de la nota (Guardado, Compartir, Descargar, Backlinks) ocupa una fila entera.

- **Asas de bloque en el móvil:** no se ven siempre. Solo aparecen en el bloque donde está el cursor, o con una pulsación larga. El texto usa todo el ancho, sin reservar el margen de las asas.
- **Compartir arriba a la derecha**, en la fila de la cabecera (menú, atrás, adelante).
- **Menú «⋯» junto a Compartir** con Descargar y Backlinks. Ahí irán las opciones de la página y de la interfaz que se añadan más adelante. El mismo menú se usa también en escritorio, para que las dos versiones sean iguales.
- **Repaso general a 375 px** de las pantallas principales (nota, base en tabla, tablero y calendario, Inicio, Mis tareas, Ajustes, diálogos): nada que se salga, botones de al menos 44 px y nada tapado por el teclado.

### H1 — Espacios, grupos y roles

Seguramente en dos tiradas: primero modelo y permisos, después pantallas.

- **Modelo:** `workspaces` gana `slug`, `icon` y ajustes; se añaden `workspace_members` (persona o grupo, con rol), `groups` y `group_members`, y `users.can_view_global_workload`. El espacio actual se renombra a General.
- **Permisos:**
  - el rol del espacio da el permiso de partida;
  - el grupo entra en `acl` como sujeto;
  - `authorize` y `visible_clause` lo resuelven con una sola consulta, como ahora.
- **Aislamiento:** árbol, búsqueda, grafo, Mis tareas, Inicio, `/events`, salas de edición, agentes, export, papelera y wikilinks (resueltos dentro del espacio).
- **Pantallas:**
  - selector de espacio arriba de la barra lateral, encima del buscador;
  - *Administración › Espacios* (crear, renombrar, icono, miembros y roles, borrar);
  - *Administración › Grupos*;
  - casilla «Puede ver carga global» en el formulario de usuario.

### H2 — Privado, Compartido, mover e invitados

- Secciones **Compartido** y **Privado** en cada espacio.
- **Mover** páginas y bases entre secciones, entre madres y entre espacios, arrastrando y con «Mover a…», recalculando los permisos heredados. Si la página lleva cosas que no pueden cruzar (por ejemplo, relaciones con bases del otro espacio), se avisa antes de moverla.
- **Invitados:**
  - compartir una página con alguien de fuera da acceso a esa página y sus hijas;
  - el invitado aparece como tal en el diálogo de compartir;
  - en su barra lateral tiene una sección «Compartido conmigo».

### H3 — Estado con grupos

- La propiedad de estado gana grupos. Cada opción va en un grupo y cada grupo tiene «cuenta como cerrado».
- Se migran las reglas actuales de la ADR-0007.
- Editor de grupos en el menú de la columna. El tablero agrupa por grupo o por opción.

### H4 — Tipos de base: Proyectos y Tareas

- Fichas de tipo en el backend y galería de tarjetas (Proyectos, Tareas, Vacía).
- Columnas obligatorias protegidas y opciones de la base (reparto de horas, columna de estimación).
- Convertir una base existente en uno de los tipos.
- Proyecto ↔ tareas: relación obligatoria en el esquema y vacía permitida en la fila, con suma de horas, avance y lista de tareas dentro del proyecto.
- Opción del espacio «sus proyectos aceptan tareas de otros espacios».
- Inicio y su «Fechas cercanas» leen las fechas de las bases de tareas.

### H5 — Carga del equipo

- Cálculo por piezas en el backend, por persona y semana o mes, con el enmascarado de los espacios ajenos.
- Capacidad semanal por persona, editable en *Administración* y con 40 h por defecto.
- **Vista:** tabla por persona y periodo, con saturación por colores y desglose desplegable.

### H6 — Avisos: campana, menciones y plazos

- **Antes de nada:** arreglar que `/events` no deja apagar el servidor.
- **Modelo:** `notifications` (persona, espacio, página, tipo, mensaje, leído, fecha) y `page_subscriptions` (`all`, `mentions_only` por defecto, `muted`).
- **Menciones `@persona`** en el editor, con los miembros del espacio y los invitados de la página.
- **Tipos de aviso:**
  - mención;
  - tarea asignada;
  - plazo cerca (24 h);
  - plazo vencido;
  - cambios en la página, en modo `all`, **agrupados** (uno por persona y página cada cierto tiempo, porque la edición en vivo guarda cada segundo).
- **Plazos:** una tarea interna cada hora, con la misma resta de días de la ADR-0008 y sin duplicados por tarea, tipo y día.
- **Pantallas:**
  - botón de campana en cada página, junto a Compartir y Backlinks;
  - panel de avisos con contador;
  - **el sonar del logo** (`fijarMarcaSinLeer`) se enciende con lo que haya sin leer.

### H7 — Presencia

- **Estado manual** en el menú de usuario: Disponible, En foco, En reunión o un texto.
- **«Viendo: [página]»** al pasar sobre el avatar de un compañero, solo si quien mira puede ver esa página; si no, «En línea».

### H8 — Auditoría

- `audit_logs`: espacio creado, permiso cambiado, página borrada, base exportada, usuario creado, copia descargada…
- Tabla filtrable en *Administración › Auditoría*.

### H9 — Webhooks por espacio

- Una URL por espacio para tarea creada, tarea cerrada y plazo vencido, en JSON compatible con Slack, Discord, Mattermost y n8n.
- Firmados con un secreto, con reintentos y **bloqueando direcciones internas**, para que no sirva para hacer peticiones a la red local.

---

## Supuestos a confirmar

1. **Privado es por espacio.** Cada usuario tiene su sección Privado en cada espacio en el que está, como en Notion; no hay un Privado común a todos los espacios.
2. **Restricciones y el admin del espacio.** Una página del espacio restringida a unos pocos no la ve un `admin` del espacio que no esté entre ellos. Solo la ve el superadministrador.
3. **Mover a otro espacio.** Al mover una página a otro espacio, sus permisos propios se quitan y pasa a heredar los del destino; los invitados se mantienen.
4. **Invitado en varios espacios.** Ve en el selector solo los espacios donde tiene algo compartido.
5. **Días laborables:** de lunes a viernes, sin calendario de festivos de momento.
6. **Horas sin fechas.** Una tarea sin ninguna fecha cuenta en la carga como «sin fecha», en una columna aparte, y no en una semana concreta.

---

## Pendiente (tras implementar los diez hitos)

- Avisar al mover una base con relaciones a otras bases (H2).
- Tablero agrupado por **grupo** de estado, y que renombrar o borrar una opción actualice los grupos (H3).
- Convertir una propiedad `person` existente en «varias personas» (hoy solo al crearla) (H4).
- Columna calculada de horas en la tabla de proyectos (hoy, en la ficha de cada proyecto).
- ~~Vistas de la carga por proyecto y por espacio~~ — pasa a `docs/PLAN-CARGA.md` (segunda vuelta a la carga, fases C1–C8).
- ~~Autocompletado del `@` en el editor~~ — hecho (2026-11-21): lista de personas al escribir `@`, menciones conocidas como etiqueta.
- «En foco» no silencia los avisos todavía.
- Una URL de webhook por espacio (el plan lo pedía así); eventos adicionales si hacen falta.
- Revisar a 375 px: tabla de carga, auditoría, avisos y administración de espacios.
