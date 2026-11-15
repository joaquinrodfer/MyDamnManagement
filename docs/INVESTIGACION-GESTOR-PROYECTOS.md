# Investigación: ¿puede MyDamnManagement ser un gestor de proyectos?

**Fecha de consulta de todas las fuentes externas: 2026-11-15.**
Autor: diseño (perfil `disenador`). Encargo: producto, no diseño y no código. Este
documento no decide nada: prepara la decisión del dueño.

---

## Resumen de una página

1. **Casi todo lo que hace un gestor de proyectos ya lo puedes improvisar aquí.**
   De las 24 funciones del inventario (§3), **7 ya están** y **9 se hacen a medias**
   con una base de datos y una relación. De las **8 que no existen**, solo dos se
   improvisan (responsable con una base `Personas`, comentarios a mano en el
   cuerpo); las otras seis necesitan motor. Y lo que no se puede improvisar es
   justo lo que depende de que la app *sepa* qué es una tarea: persona real
   asignada, subtareas con recuento, dependencias que se cumplen, carga de
   trabajo, cronograma.
2. **La diferencia entre Notion y un gestor de proyectos no son las vistas: es el
   modelo.** Notion tiene tabla, tablero, calendario, cronograma y hasta
   dependencias con desplazamiento de fechas [3], pero cada fila es una página
   genérica: no hay tipo «tarea», ni persona asignada obligatoria, ni cierre en
   cascada. Cuando lo fuerzas, aparecen los parches: en tablero, calendario y
   galería los subelementos **solo** admiten el filtro «solo padres» [3].
3. **Los tres que mejor resuelven el ciclo de una tarea coinciden en una cosa:
   pocos conceptos, muy rígidos.** Linear fija cinco prioridades y se niega a
   permitir prioridades personalizadas, con el argumento explícito de que «es
   fácil venirse arriba con la especificidad» [23]. Planner hace lo mismo con el
   estado (no iniciada / en curso / completada) y esconde todo lo demás tras
   licencia premium [8][11]. La lección no es copiar su catálogo: es que un
   gestor se define por lo que **no** deja configurar.
4. **Los que tienen más funciones las cobran en complejidad de aprendizaje.**
   ClickUp anuncia 36 funciones de vista y 33 de automatización [32]; Asana
   esconde las subtareas de Timeline, Calendar, Workload y Rules —lo dice su
   propia documentación— [30]; Jira obliga a elegir entre espacios
   «gestionados por la empresa» y «gestionados por el equipo» antes de crear
   nada [34][35]. Eso es lo que **no** queremos.
5. **El tiempo real no es un requisito de «gestor de proyectos»: es un requisito
   de «varias personas a la vez».** Hoy el tablero ya se repinta solo cuando otra
   pestaña cambia una fila, porque hay un canal SSE (`/events`) [R7]. Lo que no
   hay —y es otra cosa— es edición simultánea del mismo texto. Y despegarse del
   `.md` no afecta solo a las notas: **hoy las bases y cada fila también son
   ficheros** (`Bases/Contactos.md`, `Bases/Contactos/Acme.md`) [R2]. La decisión
   es más grande de lo que parece y está en §6.

---

## 1. Cómo se ha hecho esto (y sus límites)

- **Fuentes:** solo documentación oficial de cada producto y sus propios blogs de
  producto. Cero comparativas de terceros, cero vídeos de «las 10 mejores». Cuando
  no he encontrado un dato, la celda dice `?` y el texto dice «no encontrado»: no
  lo he rellenado a ojo.
- **Dónde he mirado el repo:** `frontend/app.js` (11.696 líneas), `backend/app/routers/databases.py`,
  `backend/app/templates.py`, `backend/app/properties.py`, `backend/app/views.py`,
  `backend/app/routers/dashboard.py`, `backend/app/events.py`, `docs/ARCHITECTURE.md`,
  `docs/ADR-0001-formato-del-vault.md`, `README.md`. Las afirmaciones sobre lo
  nuestro llevan la referencia `[Rn]` de §7.
- **Lo que esta investigación no cubre:** precios por usuario, integraciones
  (Slack/GitHub/Outlook), IA de cada producto, y móvil. No son la pregunta.
- **Productos añadidos a la lista pedida:** he separado **Trello** de **Jira**
  (comparten dueño, no modelo: uno es minimalismo deliberado y el otro jerarquía
  ágil) y he tratado **Planner** y **To Do** como un solo caso, porque en la
  práctica son dos caras del mismo modelo: plan/bucket/tarea en Planner y la
  lista personal en To Do, con «Asignadas a mí» cruzando los dos [12][13][14].
  No he investigado monday.com, Basecamp ni Smartsheet a propósito: no aportan
  ninguna pieza que los siete de arriba no cubran ya, y añadirlos habría alargado
  el documento sin cambiar ninguna decisión.

---

## 2. Las siete fichas

**Leyenda de las fichas:** ● lo tiene · ◐ lo tiene a medias o solo en el plan de
pago · ○ no lo tiene como pieza propia (se improvisa) · **?** no lo he
encontrado en la documentación consultada (no significa que no exista: ver §9).

**Vista de pájaro** — las 16 piezas del encargo, producto a producto:

| Pieza | Notion | Planner | AppFlowy | Linear | Asana | ClickUp | Jira | Trello |
|---|---|---|---|---|---|---|---|---|
| Tarea | ● | ● | ● | ● | ● | ● | ● | ● |
| Proyecto | ● | ● | ◐ | ● | ● | ● | ● | ◐ |
| Responsable | ● | ● | ? | ● | ● | ● | ● | ● |
| Estado | ● | ● | ● | ● | ◐ | ● | ● | ● |
| Prioridad | ◐ | ● | ◐ | ● | ◐ | ● | ? | ○ |
| Etiqueta | ◐ | ● | ● | ● | ◐ | ● | ? | ● |
| Dependencia | ● | ◐ | ? | ● | ● | ● | ? | ? |
| Subtarea | ● | ◐ | ◐ | ● | ● | ● | ● | ◐ |
| Hito | ○ | ◐ | ? | ● | ● | ● | ? | ○ |
| Comentario | ● | ● | ? | ● | ● | ● | ● | ● |
| Adjunto | ● | ● | ● | ● | ● | ● | ● | ● |
| Mención | ● | ● | ? | ● | ● | ● | ? | ? |
| Notificación | ● | ● | ? | ● | ● | ● | ● | ? |
| Automatización | ● | ○ | ? | ◐ | ● | ● | ? | ● |
| Plantilla | ● | ● | ● | ● | ● | ● | ? | ● |
| Recurrencia | ◐ | ? | ● | ○ | ● | ● | ? | ? |

Y las vistas que pide el encargo:

| Vista | Notion | Planner | AppFlowy | Linear | Asana | ClickUp | Jira | Trello |
|---|---|---|---|---|---|---|---|---|
| Lista | ● | ◐ | ● | ● | ● | ● | ● | ○ |
| Tabla | ● | ● | ● | ○ | ? | ● | ? | ○ |
| Tablero | ● | ● | ● | ● | ● | ● | ● | ● |
| Calendario | ● | ● | ● | ○ | ● | ● | ? | ◐ |
| Cronograma / Gantt | ◐ | ◐ | ? | ◐ | ● | ● | ◐ | ○ |
| Carga de trabajo | ? | ◐ | ? | ○ | ● | ● | ? | ○ |

Lo que dicen estas dos tablas de un vistazo: **nadie tiene las 16 piezas y las 6
vistas**; los que más se acercan (ClickUp, Asana) son los más caros de aprender;
y los dos primeros de la lista en «piezas bien resueltas» (Notion y Linear) se
quedan cortos justo en vistas de tiempo (carga y Gantt). No hay un producto que
copiar entero: hay que elegir el subconjunto.

### 2.1 Notion — el techo de la flexibilidad y el suelo del modelo

- **Qué es:** una base de datos de páginas con propiedades tipadas. Cada fila
  **es** una página con cuerpo libre, y cada base admite vistas encima [1].
- **Piezas:** tarea/página ●, proyecto ● (otra base relacionada), responsable ●
  (propiedad `persona`), estado ● (propiedad), prioridad ◐ (una propiedad más),
  etiqueta ◐ (multiselección), dependencia ● (con desplazamiento automático de
  fechas: solo si se solapan, mantener el margen, o no tocar nada; y opción de no
  caer en fin de semana) [3], subtarea ● (subelementos, con opciones de
  visualización y tres modos de filtro) [3], hito ○ (no hay tipo hito: se
  improvisa con una propiedad), comentario ●, adjunto ●, mención ●,
  notificación ●, automatización ● (disparadores y acciones; solo en planes de
  pago, y una automatización **no** puede disparar otra) [5], plantilla ●
  (plantillas de base, incluidas **recurrentes** diarias/semanales/mensuales/
  anuales, con límite de 3 niveles de anidamiento y prohibido dentro de una
  diaria) [4], recurrencia ◐ (recurren las plantillas, no las tareas).
- **Vistas:** tabla, tablero, cronograma, calendario, lista, galería y gráficos
  [2]; agrupar y **subagrupar**; filtros con `Y`/`O` anidados hasta tres niveles
  [2]. **Carga de trabajo: no encontrado** (no aparece en la lista de vistas).
- **En lote:** no encontrado en la documentación consultada.
- **Coste de aprenderlo (juicio propio):** medio-alto. Crear una tarea es fácil;
  hacer que la base se comporte como un proyecto (relaciones, rollups,
  automatizaciones, plantillas recurrentes) es un trabajo de configuración que
  nadie te enseña y que hay que rehacer si el esquema cambia.
- **Lo que me llevo:** el modelo «fila = página con cuerpo» ya lo tenemos y es
  bueno; las plantillas recurrentes son una idea barata y muy útil; los filtros
  anidados con Y/O son la diferencia entre «una vista» y «la vista que necesito».
- **Lo que no quiero:** dependencias que se ven en el cronograma pero no impiden
  cerrar la tarea. Y los techos documentados: **500 propiedades**, **250.000
  filas**, 2,5 MB de propiedades por página, 1,5 MB de esquema, y un consejo
  explícito de no meter muchas bases incrustadas en páginas de mucho tráfico
  porque «cuantas más bases se ven a la vez, más tensión» [6]. Un gestor de
  proyectos que se degrada con el uso no es un gestor de proyectos.

### 2.2 Microsoft Teams: Planner + To Do — el modelo «plan / bucket / tarea / asignado»

- **Qué es:** Planner es el tablero de un plan de equipo dentro de Teams; To Do
  es la lista personal. Se conectan: lo que te asignan en Planner aparece en
  «Asignadas a mí» de To Do [14], y To Do añade listas inteligentes —Mi día,
  Importante, Planificado, Correo marcado— que no existen en Planner [13].
- **Piezas (plan básico):** tarea ●, plan ●, responsable ● (**hasta 11 personas**
  por tarea, y si una la marca hecha se marca para todas) [9], estado ● (no
  iniciada/en curso/completada **y** el bucket, que son dos ejes distintos: se
  agrupa por bucket, persona, progreso, fecha, etiqueta o prioridad) [10],
  prioridad ●, etiqueta ●, dependencia ◐ (**solo premium**), subtarea ◐
  (checklist en básico, subtareas de verdad en premium), hito ◐ (premium),
  comentario ●, adjunto ●, mención ●, notificación ●, automatización ○ (no está
  en la app: si hace falta, Power Automate), plantilla ●, recurrencia **no
  encontrado**.
- **Vistas:** rejilla, tablero, programación (calendario) y gráficos en el plan
  básico; **cronograma (Gantt), People (capacidad), Goals y Assignments** en
  premium [8][11]. O sea: la carga de trabajo existe, pero es una función de pago
  que además cambia el modelo de plan (convertir un plan básico en premium) [11].
- **En lote:** agrupar, filtrar y arrastrar entre grupos [10]; edición masiva no
  encontrada.
- **Coste de aprenderlo (juicio propio):** bajo para lo básico, y tramposo: la
  frontera entre lo que hay y lo que hay que pagar no se ve hasta que la
  necesitas. Mucha gente descubre que no tiene dependencias cuando ya ha montado
  el plan.
- **Lo que me llevo:** **la separación plan / lista personal**. «Mis tareas» es
  la vista que hace que la gente entre a la herramienta cada mañana; sin ella, el
  resto da igual. También: varios responsables por tarea es barato y evita
  duplicar tareas.
- **Lo que no quiero:** que la mitad del producto esté detrás de una licencia, ni
  dos ejes de estado (progreso + bucket) que hay que explicar.

### 2.3 AppFlowy — el espejo local-first

- **Qué es:** el clon open source de Notion (AGPLv3) [19], en Flutter + Rust,
  con app nativa, modo offline y autoalojamiento [15]. Es el producto que más se
  parece a lo nuestro en filosofía: los datos son tuyos y viven donde tú digas.
- **Piezas:** tarea ● (= fila de una base), proyecto ◐ (una página o un tablero;
  no hay entidad «proyecto»), responsable **no encontrado**, estado ● (campo
  select + tablero), prioridad ◐ (un select más), etiqueta ● (multiselección),
  dependencia **no encontrado**, subtarea ◐ (subpágina o checklist), hito **no
  encontrado**, comentario **no encontrado**, adjunto ●, mención **no
  encontrado**, notificación **no encontrado**, automatización **no
  encontrado**, plantilla ●, recurrencia ● (eventos recurrentes en el calendario)
  [18].
- **Vistas:** rejilla, tablero y calendario, compartiendo la misma base [16]. El
  tablero agrupa por select simple, select múltiple, casilla, URL y fecha, y
  arrastrar una tarjeta **escribe la celda de agrupación** [17]. El calendario
  tiene rangos de fecha, arrastrar para reprogramar, lista de «sin fecha» y
  recurrencia [18]. **Cronograma: no encontrado. Carga: no encontrado.**
- **En lote:** no encontrado; sí arrastre de tarjetas entre grupos [17].
- **Coste de aprenderlo (juicio propio):** bajo. Es el más fácil de los siete
  para alguien que ya haya usado Notion, porque tiene menos superficie.
- **Lo que me llevo:** la confirmación de que **nuestro modelo de vistas ya es el
  suyo** (misma base, varias vistas, arrastrar escribe la celda) y que se puede
  ser un producto serio sin cronograma ni carga. Y el detalle del calendario:
  rango inicio-fin y lista de lo que no tiene fecha.
- **Lo que no quiero:** que «proyecto» sea solo una convención. Sin entidad, los
  informes y los permisos por proyecto no llegan nunca.
- **Una advertencia:** AppFlowy anuncia edición multiusuario en tiempo real en la
  misma página [15]. Es decir: el local-first y el tiempo real **no son
  incompatibles**, pero ellos lo resolvieron con un motor de colaboración
  (CRDT, del tipo de Yjs [39]) desde el principio, no después.

### 2.4 Linear — el que mejor hace el ciclo de una tarea

- **Qué es:** incidencias (issues) organizadas por equipos, con proyectos,
  ciclos e iniciativas encima [20]. Su tesis es que el trabajo se escribe como
  incidencia, no como historia de usuario [28].
- **Piezas:** tarea ● (issue), proyecto ●, iniciativa ● (proyectos agrupados),
  ciclo ● (periodo de 1 a 8 semanas que se repite solo, con «cooldown» opcional
  en el que no se puede asignar trabajo) [26], responsable ●, estado ● (flujo por
  equipo, ordenado), **prioridad ● con cinco niveles fijos** (sin prioridad,
  baja, media, alta, urgente) y negativa explícita a permitir prioridades
  personalizadas [23], etiqueta ●, estimación ● (puntos o tallas), dependencia ●
  (bloquea / bloqueado por / relacionado / duplicado; la marca cambia de color
  cuando el bloqueo se resuelve) [24]; dependencias **entre proyectos** solo
  fin→inicio, dibujadas en el cronograma, en rojo si se incumplen [25], subtarea
  ● (hereda equipo, prioridad y proyecto del padre; las etiquetas no) [22], hito
  ● (en proyectos), comentario ●, adjunto ●, mención ●, notificación ●,
  automatización ◐ (reglas e integraciones; no consultado en detalle), plantilla
  ● (de incidencia y de subincidencia), recurrencia ○ (los ciclos se repiten, las
  tareas no).
- **Vistas:** lista y tablero por vista, con agrupación por estado, persona,
  proyecto, prioridad, ciclo, etiqueta, padre, equipo, cliente, release o SLA, y
  **subagrupación en filas tipo swimlane**; orden manual que se guarda para todo
  el espacio de trabajo [21]. Cronograma en vistas de proyecto [21]. **Calendario
  y carga de trabajo: no aparecen** [21].
- **En lote:** sí: seleccionar varias incidencias y pulsar `P` para cambiar la
  prioridad, arrastrar para mover entre grupos [23][21].
- **Coste de aprenderlo (juicio propio):** medio. Muchos conceptos (equipo,
  ciclo, proyecto, iniciativa), pero cada uno hace una cosa y no se puede
  reconfigurar. Es el que menos «configuración» pide y más «convención» impone.
- **Lo que me llevo:** tres cosas concretas y baratas. (a) **La marca de bloqueo
  que cambia de color al resolverse** — comunicación sin notificación.
  (b) **Herencia de propiedades del padre a la subtarea** — ahorra teclear y
  evita subtareas huérfanas. (c) **El orden manual guardado para todos**: hoy
  nuestras vistas solo ordenan por propiedades [R7].
- **Lo que no quiero:** ciclos. Un ciclo es un sprint con otro nombre y su propio
  vocabulario que aprender.

### 2.5 Asana — el equipo grande, con costuras visibles

- **Qué es:** proyectos con secciones, tareas, subtareas, hitos y carteras;
  varias vistas encima del mismo proyecto [29][31].
- **Piezas:** tarea ●, proyecto ●, responsable ●, estado ◐ (secciones + campos
  personalizados), prioridad ◐ (campo), etiqueta ◐, dependencia ● («bloquea» /
  «bloqueado por», con aviso en la bandeja cuando cambia la fecha o se completa
  la bloqueante) [29], subtarea ●, hito ●, comentario ●, adjunto ●, mención ●,
  notificación ● (bandeja), automatización ● (reglas: disparador → acción),
  plantilla ● (plantillas de tarea con subtareas, dependencias, campos y
  responsable por defecto) [31], recurrencia ● (tareas repetidas; no consultado
  en detalle), y dos piezas que no tiene nadie más: **aprobaciones** (aprobar /
  pedir cambios / rechazar) y **multi-homing** (la misma tarea en varios
  proyectos) [29].
- **Vistas:** lista, tablero, **cronograma (Timeline)** y calendario [30];
  carteras para agregar proyectos; **carga de trabajo (Workload)** mencionada
  como función de pago [30].
- **En lote:** no encontrado.
- **Coste de aprenderlo (juicio propio):** alto. No por una función, sino por la
  cantidad de objetos (tarea, subtarea, hito, sección, proyecto, cartera,
  objetivo, regla, plantilla) y porque cada uno tiene su propia configuración.
- **Lo que me llevo:** **multi-homing** es la respuesta elegante a «esta tarea es
  de dos proyectos» y aquí sería casi gratis (una relación múltiple). Y las
  aprobaciones: un estado que no es «hecho» sino «esperando a alguien».
- **La costura que hay que leer dos veces:** su propia documentación admite que
  las subtareas **no salen** en Timeline ni Calendar, no se ven por defecto en
  Lista ni Tablero, y no las alcanzan Workload ni Rules [30]. Su solución
  recomendada es «convierte la tarea en proyecto». Ese es exactamente el tipo de
  trampa que queremos evitar: si las subtareas no cuentan para la carga ni para
  el calendario, no son subtareas, son decoración.

### 2.6 ClickUp — el catálogo

- **Qué es:** el producto que intenta tenerlo todo: espacios, carpetas, listas,
  tareas, y una vista para cada cosa [32].
- **Piezas:** todo ●. Tarea con hasta **1.000 subtareas** y subtareas anidadas,
  responsables múltiples, prioridad de cuatro niveles, dependencias con **tipos**
  (fin→inicio, inicio→inicio…), **cumplimiento** de dependencia (no deja avanzar
  una tarea bloqueada) y aviso antes de cerrar; hitos; duración en vez de fechas
  fijas; reprogramación de subtareas al mover el padre; campos personalizados de
  ~20 tipos (persona, fórmula, rollup, progreso automático, relación, firma,
  voto); recurrencia; automatizaciones con hasta 6 acciones y 15 condiciones
  [32].
- **Vistas:** lista, tablero (con subgrupos), tabla, **Gantt** con líneas base y
  **camino crítico**, línea de tiempo, **carga de trabajo**, calendario, mapa,
  actividad, equipo, formularios, docs, pizarra, chat [32]. Muchas, con límites
  por plan: el Gantt son 60 usos en el plan gratuito y la carga 60 usos [33].
- **En lote:** sí, «actualizaciones masivas» en la vista de tabla [32].
- **Coste de aprenderlo (juicio propio):** el más alto de los siete. La propia
  página de funciones enumera 36 funciones de vista y 33 de automatización [32];
  eso no es un producto, es un catálogo, y cada plan recorta una parte distinta
  [33].
- **Lo que me llevo:** el **camino crítico** y las **líneas base** (planificado
  frente a real) son la diferencia entre un cronograma decorativo y uno que sirve
  para responder «¿llego?». Pero son caros y llegan tarde.
- **Lo que no quiero:** casi todo lo demás. Un gestor de proyectos para una
  persona no se hace con 36 vistas.

### 2.7 Jira y Trello — el vocabulario y el antídoto

- **Jira:** la jerarquía ágil completa. Una **épica** es «un cuerpo grande de
  trabajo que se descompone en historias» [34][36]; las historias viven en
  **sprints** dentro de un **backlog**, y se planifican por **versiones** [35].
  Antes de crear nada hay que decidir si el espacio es «gestionado por la
  empresa» o «gestionado por el equipo», y las dos mitades no se mezclan [34][35].
  Tiene todo lo que un equipo ágil necesita y nada de lo que una persona sola
  necesita.
- **Trello:** el opuesto. **Tablero, lista, tarjeta** y nada más [37]; las
  tarjetas tienen dorso con descripción, checklists, adjuntos y conversación, y
  lo demás (calendario, campos personalizados, checklists avanzadas con
  responsable y fecha por ítem, automatizaciones Butler) llega por Power-Ups y
  planes de pago [38].
- **Lo que me llevo de los dos:** de Trello, que **tres conceptos bastan** para
  que alguien empiece a trabajar en dos minutos, y que todo lo demás puede
  llegar después sin romper el modelo. De Jira, el vocabulario para *no*
  usarlo: si en algún momento aparece la palabra «épica» en nuestra app, hemos
  fallado.
- **Coste de aprenderlo (juicio propio):** Jira alto (conceptual, no visual);
  Trello mínimo.

---

## 3. Cruce con lo que ya tenemos

Leyenda de la tercera columna: cómo se haría **hoy**, con una base de datos, sus
tipos de propiedad (`texto`, `número`, `fecha`, `selección`, `multiselección`,
`casilla`, `enlace`, `relación`) [R3] y sus vistas (`tabla`, `tablero`,
`calendario`, `lista`) [R6].

| Función | ¿La tenemos? | Cómo se haría hoy con bases |
|---|---|---|
| **Tarea** | **Sí** | Una fila. Y además cada fila **es** una página con cuerpo Markdown propio (se abre y se escribe como una nota) [R1][R4] |
| **Proyecto** | A medias | Base `Proyectos` y una propiedad `relación` desde `Tareas`. No hay jerarquía: no se puede «entrar» al proyecto y ver sus tareas, ni heredar nada del proyecto |
| **Responsable** | **No** | No existe el tipo «persona». Se improvisa con una base `Personas` + relación múltiple, o texto libre. Sin avatar, sin «lo mío», sin validar que la persona exista |
| **Estado** | **Sí** | `selección` + vista `tablero` con `group_by` [R3][R6]. Arrastrar la tarjeta escribe la celda [R6] |
| **Prioridad** | **Sí** | `selección`. Ojo: se ordena como texto, así que «alta, baja, media» sale alfabético, no por urgencia [R7] |
| **Etiqueta** | **Sí** | `multiselección`, con filtro por opción |
| **Dependencia** | A medias | `relación` a la misma base. Se ve, se filtra, no se cumple: nada impide cerrar una tarea bloqueada |
| **Subtarea** | A medias | `relación` a la misma base («padre») o una base `Subtareas` aparte. Sin recuento en la tarjeta, sin jerarquía en la vista, sin herencia |
| **Hito** | A medias | `casilla` + `fecha` en la base de tareas, o una base `Hitos` con una fecha y una vista de calendario |
| **Comentario** | **No** | Se improvisa: cuerpo Markdown de la fila con `## Comentarios` a mano. Sin autor, sin fecha automática, sin orden |
| **Adjunto** | A medias | Hay imagen de cabecera y un `_attachments/` servido por `/files` [R2]; **no** hay lista de adjuntos por fila |
| **Mención** | **No** | No hay usuarios dentro del texto. Lo único enlazable es `[[título de nota]]` [R2] |
| **Notificación** | **No** | Solo avisos de cambio: si el `.md` cambia fuera (o en otra pestaña), la app repinta y avisa [R7]. No hay bandeja ni «me asignaron» |
| **Automatización** | A medias | Agentes que **proponen** parches y el dueño acepta o rechaza [R8][R9]. Es automatización con revisión humana, no reglas disparador→acción |
| **Plantilla** | A medias | `POST /databases/from-template` con dos plantillas (`crm`, `tasks`) que solo traen esquema y vistas [R5]. No hay plantilla de fila ni de proyecto completo |
| **Recurrencia** | **No** | Nada. Se duplica a mano |
| **Vista lista / tabla / tablero** | **Sí** | Las tres [R6], con filtros (`Y`), orden por varias claves y columnas ocultas/reordenadas por vista [R7] |
| **Vista calendario** | A medias | Una fecha por fila, mes a mes [R6]. Sin rango inicio-fin, sin arrastrar para reprogramar, sin lista de «sin fecha» |
| **Cronograma / Gantt** | **No** | No hay nada. Ni barras, ni dependencias dibujadas |
| **Carga de trabajo** | **No** | Sin persona y sin estimación que agregar, no hay nada que sumar |
| **Acciones en lote** | **Sí** | Selección múltiple de filas → «Cambiar propiedad» (cualquier tipo, en todas a la vez) y «Borrar» (a la papelera del vault) [R6] |
| **Búsqueda** | **Sí** | FTS5 sobre título **y cuerpo**, sin acentos [R1] |
| **Historial de cambios** | A medias | `created`/`updated` por fila y el `.md` en disco; con git encima, historial de verdad. Sin «quién cambió qué» |
| **Permisos** | **No** | Cualquier usuario con sesión lee y escribe **todo**; lo único de admin es crear usuarios [R10] |

**Recuento: 7 sí, 9 a medias, 8 no (24 funciones).**

Lo que esto significa en una frase: **la app ya es una base de datos con vistas
buenas; lo que no es, es un objeto «tarea»**. Y la diferencia no está en las
vistas: está en que hoy una tarea no sabe quién la tiene, no sabe de quién
depende y no sabe si está cerrada.

---

## 4. La tabla de decisión

34 funciones candidatas, agrupadas por tema. **La última columna está vacía a
propósito: la rellena el dueño.** Coste = esfuerzo de producto+implementación
para hacerlo **bien** (no un parche): bajo ≈ días, medio ≈ semanas, alto ≈ un
bloque de trabajo con diseño propio detrás.

### Tareas y estados

| Función | Qué resuelve | Coste | ¿Ya con bases? | Decisión del dueño |
|---|---|---|---|---|
| Tarea = página con cuerpo y checklist | Descripción rica sin salir de la tarea | bajo | sí | ⬜ sí ⬜ no ⬜ depende |
| Subtareas con estado propio y recuento en la tarjeta | «3 de 7 hechas» sin abrir nada | medio | a medias | ⬜ sí ⬜ no ⬜ depende |
| Prioridad con orden semántico | Que ordenar por prioridad ordene de verdad | bajo | a medias | ⬜ sí ⬜ no ⬜ depende |
| Etiquetas multivalor transversales | Cortar por tema entre proyectos | bajo | sí | ⬜ sí ⬜ no ⬜ depende |
| Estado «cerrado» con semántica (deja de contar, no se arrastra) | Que «hecha» signifique algo en todas partes | bajo | a medias | ⬜ sí ⬜ no ⬜ depende |

### Responsables y carga

| Función | Qué resuelve | Coste | ¿Ya con bases? | Decisión del dueño |
|---|---|---|---|---|
| Persona como propiedad (usuario real, con avatar) | Asignar a alguien que existe; sin texto libre | medio | no | ⬜ sí ⬜ no ⬜ depende |
| Varios responsables por tarea | Trabajo compartido sin duplicar tareas | bajo | a medias | ⬜ sí ⬜ no ⬜ depende |
| «Mis tareas» global, cruzando todas las bases | La vista que hace abrir la app cada mañana | medio | no | ⬜ sí ⬜ no ⬜ depende |
| Estimación por tarea (puntos u horas) | Poder decir «esto no cabe esta semana» | bajo | sí | ⬜ sí ⬜ no ⬜ depende |
| Carga de trabajo por persona y semana | Ver quién está por encima antes de asignar | alto | no | ⬜ sí ⬜ no ⬜ depende |

### Plazos y dependencias

| Función | Qué resuelve | Coste | ¿Ya con bases? | Decisión del dueño |
|---|---|---|---|---|
| Fecha de inicio + fecha límite (duración) | Saber si algo dura tres días o un mes | bajo | sí | ⬜ sí ⬜ no ⬜ depende |
| Aviso de vencimiento dentro de la app | Que no se pase una fecha sin enterarse | bajo | a medias | ⬜ sí ⬜ no ⬜ depende |
| Dependencia declarada (bloquea / bloqueado por) | Saber qué espera a qué, con la marca en la ficha | medio | a medias | ⬜ sí ⬜ no ⬜ depende |
| Bloqueo efectivo (no se puede cerrar una tarea bloqueada) | Que la dependencia no sea un dibujo | alto | no | ⬜ sí ⬜ no ⬜ depende |
| Reprogramación en cascada al mover una fecha | No rehacer el plan a mano cada vez | alto | no | ⬜ sí ⬜ no ⬜ depende |
| Hito (fecha sin duración, visible en el calendario) | Los puntos de control del proyecto | bajo | a medias | ⬜ sí ⬜ no ⬜ depende |
| Recurrencia (tarea que se repite) | Lo que se hace cada semana sin crearlo cada semana | medio | no | ⬜ sí ⬜ no ⬜ depende |

### Vistas

| Función | Qué resuelve | Coste | ¿Ya con bases? | Decisión del dueño |
|---|---|---|---|---|
| Cronograma / Gantt con barras y dependencias | Ver el plan en el tiempo, no en columnas | alto | no | ⬜ sí ⬜ no ⬜ depende |
| Calendario con rango inicio-fin y arrastre | Reprogramar con el ratón, no con el teclado | medio | a medias | ⬜ sí ⬜ no ⬜ depende |
| Vista de carga (personas en columnas) | Repartir trabajo viendo los huecos | alto | no | ⬜ sí ⬜ no ⬜ depende |
| Subagrupación / swimlanes (estado × persona) | Tablero de dos dimensiones sin abrir dos vistas | medio | no | ⬜ sí ⬜ no ⬜ depende |
| Vista guardada con URL propia | Compartir «mira esto» con un enlace | bajo | sí | ⬜ sí ⬜ no ⬜ depende |

### Colaboración

| Función | Qué resuelve | Coste | ¿Ya con bases? | Decisión del dueño |
|---|---|---|---|---|
| Comentarios con hilo y autor | La conversación pegada al trabajo, no en el cuerpo | medio | no | ⬜ sí ⬜ no ⬜ depende |
| Menciones `@usuario` con aviso | Que la persona se entere sin que se lo cuentes | medio | no | ⬜ sí ⬜ no ⬜ depende |
| Bandeja de notificaciones (asignación, mención, vencimiento) | Un solo sitio donde mirar qué te toca | medio | no | ⬜ sí ⬜ no ⬜ depende |
| Presencia / edición simultánea | Dos personas escribiendo sin pisarse | alto | no | ⬜ sí ⬜ no ⬜ depende |
| Historial de cambios por tarea (quién cambió qué) | Saber por qué esto está así | medio | a medias | ⬜ sí ⬜ no ⬜ depende |

### Automatización

| Función | Qué resuelve | Coste | ¿Ya con bases? | Decisión del dueño |
|---|---|---|---|---|
| Reglas si-X-entonces-Y (mover estado, asignar) | Quitar el trabajo mecánico de mantener el tablero | alto | no | ⬜ sí ⬜ no ⬜ depende |
| Plantilla de tarea y de proyecto completo | Empezar un proyecto en un clic, no en una tarde | bajo | a medias | ⬜ sí ⬜ no ⬜ depende |
| Campos calculados (fórmula, rollup, % completado) | Totales y porcentajes que hoy se cuentan a mano | medio | no | ⬜ sí ⬜ no ⬜ depende |
| Agente que propone cambios (con revisión) | Mantener hitos y estados sin que el dueño escriba | bajo | sí | ⬜ sí ⬜ no ⬜ depende |

### Administración

| Función | Qué resuelve | Coste | ¿Ya con bases? | Decisión del dueño |
|---|---|---|---|---|
| Permisos por proyecto o base | Que no todo el mundo lo vea todo | alto | no | ⬜ sí ⬜ no ⬜ depende |
| Importar CSV / exportar con esquema | Meter datos de fuera y sacarlos sin perder columnas | medio | a medias | ⬜ sí ⬜ no ⬜ depende |
| API con webhooks salientes | Que un agente o un script se entere de un cambio | medio | a medias | ⬜ sí ⬜ no ⬜ depende |

---

## 5. Mi recomendación: las cinco primeras

Elegidas por una razón: **cada una cambia lo que la app *es*, no lo que la app
*muestra*.** Y las cinco juntas se pueden construir sin cronograma ni tiempo
real, que son las dos piezas caras.

1. **Persona como propiedad, con «Mis tareas» detrás.**
   Es la única que, sola, convierte esto en un gestor: hoy una tarea no tiene
   dueño, y sin dueño no hay responsabilidad, ni carga, ni notificaciones, ni
   «lo mío». Todo lo demás de esta lista depende de ella. Estimación: media,
   porque toca el validador de propiedades y el modelo de usuarios que ya existe.
2. **Subtareas con estado propio y recuento en la tarjeta.**
   Es lo que separa «una lista de cosas» de «un plan»: un proyecto de verdad
   tiene tres o cuatro entregables y cada uno tiene pasos. Sin recuento en la
   tarjeta no se usa; con recuento, se usa solo. Y es la pieza que Asana no
   resuelve (sus subtareas no cuentan para el calendario) [30]: aquí, si la
   hacemos, la hacemos contando.
3. **Estado «cerrado» con semántica + prioridad ordenable + plantilla de
   proyecto.**
   Van juntas porque son el mismo trabajo: hoy `hecha` es una opción de texto
   más, `alta` ordena antes que `baja` solo por alfabeto y una plantilla solo
   trae columnas [R5]. Con esto, una base deja de ser una tabla y pasa a tener
   reglas. Coste bajo y es lo que hace que las otras cuatro se sientan
   coherentes.
4. **Plazos que se ven: calendario con rango y arrastre, más aviso de
   vencimiento.**
   El panel ya enseña lo que vence en ±30 días [R7]; lo que falta es poder
   mover una fecha con el ratón y que la app avise. Es la función que la gente
   usa a diario y la más barata de las que se ven en pantalla.
5. **Comentarios con autor en la tarea.**
   No por colaboración: por **memoria**. Hoy la conversación de una tarea se
   escribe a mano en el cuerpo, sin autor ni fecha, y se pierde. Un comentario
   con autor y fecha es también el historial mínimo que hace innecesario el
   «quién cambió qué» durante mucho tiempo.

**Lo que he dejado fuera a propósito, y por qué:**

- **Cronograma/Gantt y carga de trabajo.** Son las dos más caras y las dos que
  nadie echa de menos hasta que hay más de tres proyectos a la vez. Además, sin
  persona (1) y sin estimación no hay nada que dibujar.
- **Dependencias con bloqueo efectivo y cascada.** Suena a gestor de proyectos
  de verdad, pero con una persona trabajando es teatro: nadie bloquea su propia
  tarea. La dependencia *declarada* sí la metería pronto; la que *impide* hacer
  cosas, no.
- **Permisos.** Con una persona, cero valor. Con dos, el primero que se pide.
  Pero es un rediseño de autorización, no una propiedad.

---

## 6. El punto del tiempo real (esto lo decide el dueño, no yo)

La frase del dueño es: «quizá además nos debamos despegar un poco del `.md` para
poder trabajar de forma síncrona en tiempo real». Son **dos decisiones
distintas** que van en la misma frase, y conviene separarlas: (A) edición
simultánea, y (B) dejar de tener el `.md` como verdad. Se puede tener A sin B
(Linear sincroniza en tiempo real y no expone ficheros [27]; AppFlowy lo hace y
guarda en local [15]). Y se puede tener B sin A, que es lo que ya tenemos.

### 6.1 Qué funciones **piden** edición simultánea

| Función | ¿Por qué pide tiempo real? | ¿Se nota si falta? |
|---|---|---|
| Comentarios en una tarea | Dos personas respondiendo a la vez, cada una con su hilo | mucho: uno de los dos pierde el comentario |
| Asignar mientras otro escribe la tarea | Dos escrituras sobre la misma fila en el mismo segundo | poco: es un campo; el último gana y nadie discute |
| Ver quién está mirando / editando | Evitar el «te estoy escribiendo encima» | medio: sin presencia, la gente duplica trabajo. Notion la muestra con las fotos de quién está leyendo o editando [7] |
| Tablero que se mueve solo | Que una tarjeta movida por otro aparezca movida | **ya lo tenemos**: el canal `/events` repinta la tabla cuando otra pestaña cambia filas [R7] |
| El cuerpo de la tarea (descripción) | Dos editando el mismo Markdown a la vez | mucho si pasa; raro si se escribe de uno en uno |

Lo importante: **de las cinco, solo dos son de texto** (comentarios y cuerpo). Las
otras tres son de estado, y para el estado la respuesta honesta es «el último
gana» — que es exactamente lo que hace Linear para todo lo que no es texto [27].

### 6.2 Qué funciones **no** necesitan tiempo real, y no pasa nada

Crear tareas, cambiar estados, mover tarjetas (con el aviso del canal ya basta),
filtros y orden (son de quien mira, no del documento), plantillas, exportar,
buscar, el panel de vencimientos, los permisos. En todos estos casos un refresco
de dos segundos es indistinguible de la magia.

### 6.3 Qué se pierde si el `.md` deja de ser la verdad

Esto es lo caro, y es más grande de lo que la frase sugiere: **hoy no son solo
las notas**. El contrato del vault dice que la base es un fichero
(`Bases/Contactos.md`, con esquema y vistas en el frontmatter) y que **cada fila
es un fichero** (`Bases/Contactos/Acme.md`) [R2]. Es decir: un gestor de
proyectos con el `.md` como verdad ya está construido; despegarse de él no es
tocar las notas, es cambiar el almacenamiento de todo.

Lo que se pierde, en orden de gravedad:

1. **Abrir las notas sin la app.** Hoy un `.md` se lee en el móvil, en un editor
   cualquiera, en el portátil del trabajo. Si la verdad es una base, hace falta
   la app y la app tiene que estar levantada.
2. **Git.** Hoy el vault es un árbol de ficheros: `git diff` de un cambio, `git
   log` de una tarea, volver atrás, ramas, revisiones. Con la verdad en una base,
   el historial deja de ser gratis y hay que construirlo (y entonces «historial
   de cambios» deja de ser una función opcional y pasa a ser un requisito).
3. **Buscar en el disco.** `grep`, `ripgrep`, el buscador del sistema, Spotlight.
   Hoy encuentran tareas. Después, no.
4. **Backup = copiar una carpeta.** Hoy el backup del vault es completo (y el
   índice se puede borrar y rehacer) [R1][R2]. Con la base como verdad, el
   backup es un volcado que hay que saber restaurar.
5. **Exportar.** Hoy exportar es leer el fichero que ya existe [R1]. Después,
   exportar es una función que hay que escribir y mantener.
6. **Los agentes.** El flujo actual funciona porque un agente puede leer el
   vault y proponer un parche de texto anclado [R8][R9]. Con la verdad en una
   base, el agente necesita la API, no el disco, y el anclaje por texto deja de
   tener sentido.
7. **Editar por fuera.** Hoy, si abres el `.md` en otro editor, la app lo detecta,
   avisa y recarga [R7]. Eso es una función que se cae con el modelo nuevo.

### 6.4 Las tres opciones, con su precio (sin recomendación de arquitectura)

Esta tarjeta no decide arquitectura; solo deja las opciones sobre la mesa para
que la decisión se pueda tomar con números:

- **A. Seguir con ficheros como verdad y añadir tiempo real encima** (capa de
  eventos/CRDT en memoria, escritura diferida al `.md`). Conserva todo lo de
  §6.3. Precio: deja de ser cierto «un solo escritor: la API» [R1], y hay que
  resolver qué pasa cuando el `.md` cambia por fuera mientras hay una sesión
  colaborativa abierta (hoy ya existe ese aviso; con dos escritores, es un
  conflicto real). No es teórico: Linear construyó su propio motor de
  sincronización para el estado y, para el texto que aún no era tiempo real, acabó
  apoyándose en Yjs [40].
- **B. Verdad en base de datos y exportación a `.md`** (el `.md` pasa de verdad
  a formato de salida). Gana tiempo real y consultas; pierde §6.3 entero salvo
  el export, que hay que construir. Es el modelo de Notion, Linear y ClickUp.
- **C. Híbrido por dominio:** las notas siguen en ficheros (nadie las edita a la
  vez) y las **tareas** viven en la base, donde sí hay concurrencia. Es el modelo
  de Planner (tareas en el servicio, adjuntos en SharePoint) y el que menos
  promete. Precio: dos verdades, dos copias de seguridad, dos formas de buscar.

**Lo que sí digo, como producto y no como arquitectura:** hoy el uso es de **una
persona** («uso personal en solitario» es una decisión escrita en la arquitectura
[R1], y los permisos son todo-o-nada [R10]). El tiempo real es una función para
el día en que haya dos o tres personas escribiendo a la vez. Ponerlo antes de la
función 1 de §5 sería construir la carretera antes que el coche. Y cuando llegue
ese día, la pregunta que decide entre A, B y C no es técnica: es **si el dueño
quiere poder abrir sus notas sin la app**. Si la respuesta es sí, A. Si es «me da
igual, quiero que vaya rápido y a la vez», B.

---

## 7. Fuentes del repo

- **[R1]** `docs/ARCHITECTURE.md` y `docs/ADR-0001-formato-del-vault.md` — ficheros
  como verdad, índice SQLite derivado, «un solo escritor: la API», un solo
  usuario, búsqueda FTS5.
- **[R2]** `docs/ADR-0001-formato-del-vault.md` §Contrato del vault — `Bases/<slug>.md`,
  `Bases/<slug>/<id8>.md` por fila, `_attachments/`, `.trash/`, frontmatter YAML.
- **[R3]** `backend/app/properties.py` + `backend/app/enums.py` — tipos de
  propiedad: `text`, `number`, `checkbox`, `select`, `multiselect`, `date`, `url`,
  `relation` (con `target_database` y `multiple`).
- **[R4]** `backend/app/routers/databases.py` — `GET /databases/{id}/rows/{row_id}`
  devuelve la fila **con** `body_markdown`: cada fila es una página.
- **[R5]** `backend/app/templates.py` — dos plantillas (`crm`, `tasks`), solo
  esquema y vistas.
- **[R6]** `backend/app/enums.py` (`ViewType`: `table`, `board`, `calendar`,
  `list`) y `frontend/app.js` — `renderTable`, `renderBoard` (arrastre),
  `renderCalendar`, `renderList`, `crearSeleccionDeFilas` (lote: cambiar
  propiedad, borrar a papelera).
- **[R7]** `backend/app/views.py`, `backend/app/routers/dashboard.py`,
  `backend/app/events.py`, `frontend/app.js` (`handleDatabaseEvent`) — filtros y
  orden en Python (comparación como número si ambos lados lo son, texto si no),
  panel de vencimientos a ±7/+30 días, canal SSE para repintar otras pestañas.
- **[R8]** `docs/ADR-0003-sugerencias-de-agentes.md` — el agente propone un parche
  anclado; el dueño acepta o rechaza.
- **[R9]** `docs/ADR-0004-puerta-para-agentes.md` — token de agente, contexto y
  canal de vuelta.
- **[R10]** `README.md` §Usuarios y acceso — «no hay permisos por nota: cualquier
  usuario con sesión lee y escribe todo el vault; lo único solo-admin es crear
  usuarios».

---

## 8. Fuentes externas (consultadas el 2026-11-15)

| # | Fuente | URL |
|---|---|---|
| 1 | Notion — Intro to databases | https://www.notion.com/help/intro-to-databases |
| 2 | Notion — Views, filters, sorts & groups | https://www.notion.com/help/views-filters-and-sorts |
| 3 | Notion — Sub-items & dependencies | https://www.notion.com/help/tasks-and-dependencies |
| 4 | Notion — Database templates (incl. repeating) | https://www.notion.com/help/database-templates |
| 5 | Notion — Database automations | https://www.notion.com/help/database-automations |
| 6 | Notion — Optimize database performance (límites) | https://www.notion.com/help/optimize-database-load-times-and-performance |
| 7 | Notion — Intro to workspaces (presencia) | https://www.notion.com/help/intro-to-workspaces |
| 8 | Microsoft — FAQ de Planner (vistas básicas vs. premium) | https://support.microsoft.com/en-US/Planner/frequently-asked-questions-about-microsoft-planner |
| 9 | Microsoft — Manage your team's plans with Planner in Teams (hasta 11 asignados) | https://support.microsoft.com/en-us/planner/teams/manage-your-team-s-plans-with-planner-in-teams |
| 10 | Microsoft — Organize your team's tasks in Microsoft Planner | https://support.microsoft.com/en-us/planner/training/organize-your-team-s-tasks-in-microsoft-planner |
| 11 | Microsoft — Planner: planes y precios (Plan 1: subtareas, hitos, Gantt, People) | https://www.microsoft.com/en-us/microsoft-365/planner/microsoft-planner-business-plans-and-pricing |
| 12 | Microsoft — My Tasks y My Day en Planner | https://support.microsoft.com/en-us/planner/training/manage-your-tasks-with-my-tasks-and-my-day |
| 13 | Microsoft — To Do: listas inteligentes y My Day | https://support.microsoft.com/en-us/outlook/create-and-manage-task-lists-with-my-day-in-outlook |
| 14 | Microsoft — Ver tareas de Planner en To Do | https://support.microsoft.com/en-us/planner/see-planner-tasks-in-microsoft-to-do |
| 15 | AppFlowy — Self-host overview (tiempo real, offline) | https://appflowy.com/docs/self-host-appflowy-overview |
| 16 | AppFlowy — Database View (grid / board / calendar) | https://docs.appflowy.io/docs/documentation/software-contributions/architecture/frontend/database-view |
| 17 | AppFlowy — Kanban board (campos de agrupación) | https://github.com/AppFlowy-IO/documentations/blob/main/essential-documentation/contribute-to-appflowy/architecture/frontend/database-view/kanban-board.md |
| 18 | AppFlowy — Calendar view (rangos, recurrencia, arrastre) | https://appflowy.com/blog/creating-a-calendar-view-for-the-appflowy-database |
| 19 | AppFlowy — repositorio (AGPLv3, Flutter + Rust) | https://github.com/AppFlowy-IO/AppFlowy |
| 20 | Linear — Concepts | https://linear.app/docs/conceptual-model |
| 21 | Linear — Display options | https://linear.app/docs/display-options |
| 22 | Linear — Parent and sub-issues | https://linear.app/docs/parent-and-sub-issues |
| 23 | Linear — Issue properties (prioridad, estimación) | https://linear.app/docs/issue-property |
| 24 | Linear — Issue relations | https://linear.app/docs/issue-relations |
| 25 | Linear — Project dependencies | https://linear.app/docs/project-dependencies |
| 26 | Linear — Cycles | https://linear.app/docs/use-cycles |
| 27 | Linear — Scaling the Linear Sync Engine | https://linear.app/blog/scaling-the-linear-sync-engine |
| 28 | Linear Method — Write issues not user stories | https://linear.app/method |
| 29 | Asana — Creating effective tasks (dependencias, hitos, multi-homing, aprobaciones) | https://help.asana.com/hc/en-us/articles/20160154656923-Creating-effective-tasks-in-Asana |
| 30 | Asana — Subtasks (no salen en Timeline/Calendar/Workload/Rules) | https://asana.com/resources/asana-tips-subtasks |
| 31 | Asana — Customize menu (reglas, plantillas de tarea, carteras) | https://help.asana.com/s/article/level-up-your-projects-with-the-customize-menu |
| 32 | ClickUp — Features (vistas, campos, automatizaciones) | https://clickup.com/features |
| 33 | ClickUp — Disponibilidad y límites de vistas por plan | https://help.clickup.com/hc/en-us/articles/32274881672599-Views-feature-availability-and-limits |
| 34 | Jira — What is an epic | https://support.atlassian.com/jira-software-cloud/docs/what-is-an-epic |
| 35 | Jira — Use your scrum backlog (sprints, versiones) | https://support.atlassian.com/jira-software-cloud/docs/use-your-scrum-backlog |
| 36 | Atlassian — Epics, stories, and initiatives | https://www.atlassian.com/agile/project-management/epics-stories-themes |
| 37 | Trello — Trello 101 (tablero / lista / tarjeta) | https://trello.com/en-US/guide/trello-101 |
| 38 | Trello — Tips (Power-Ups, Butler, checklists avanzadas) | https://atlassian.com/blog/trello/gigantic-list-trello-tips |
| 39 | Yjs — Introduction (CRDT, binding con CodeMirror) | https://docs.yjs.dev/ |
| 40 | Devtools FM #61 — Tuomas Artman (Linear): motor de sync y Yjs | https://www.devtools.fm/episode/61 |

---

## 9. Huecos de esta investigación (dichos, no disimulados)

- **Celdas ● sin corchete:** en las fichas, las piezas que llevan `[n]` detrás
  están documentadas en la fuente que se cita; las que no lo llevan (por ejemplo
  comentarios y adjuntos en Notion y Linear, o varias de Jira) son funciones que
  doy por hechas por el funcionamiento normal del producto pero que **no he
  verificado página a página**. Están marcadas así a propósito: ● dice «lo tiene»,
  no «lo he leído en su documentación».
- **Notion:** acciones en lote y vista de carga de trabajo — no encontrado.
- **Planner:** recurrencia de tareas y automatizaciones dentro de la app — no
  encontrado. Si existen, no están en la documentación que he consultado.
- **AppFlowy:** responsable, dependencias, hitos, comentarios, menciones,
  notificaciones y automatizaciones — no encontrado. Es el producto con más
  huecos en esta investigación, y precisamente por eso no debería usarse como
  referencia de «lo que hay que tener»: sí como referencia de «lo que se parece».
- **Linear:** calendario y carga de trabajo — no aparecen en las opciones de
  visualización consultadas [21]; comentarios y automatizaciones, no consultados
  en detalle.
- **Asana:** edición en lote — no encontrado.
- **Jira:** prioridades, dependencias entre incidencias y automatizaciones — no
  consultado en detalle; solo he mirado el vocabulario (épica / historia /
  sprint / versión), que era el encargo.
- **«Coste de aprenderlo»** es **juicio propio**, no un dato con fuente: no he
  encontrado ninguna medición pública comparable. Lo he argumentado con el número
  de conceptos y de vistas que cada producto exige antes de crear la primera
  tarea.
