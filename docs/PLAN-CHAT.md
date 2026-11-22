# Plan: chat del equipo

Hablar con el equipo sin salir de Notemil. El chat va en el panel derecho, junto a «Miembros», así que se habla sin dejar la página que se está mirando.

## Decisiones del dueño (2026-11-22)

1. **Conversaciones:**
   - **un canal por espacio**, que ven sus miembros;
   - **mensajes directos** entre personas.
2. **Dónde:** el **panel derecho**, con una pestaña «Chat» junto a «Miembros».
3. **Cuánto se guarda:** **siempre**, como las notas. El administrador no lee lo que no le toca (plan de roles, R1).
4. **Desde la primera versión:**
   - menciones y avisos;
   - enlaces a páginas;
   - adjuntos;
   - convertir un mensaje en tarea.

## Modelo

### Conversaciones
- **Canal de espacio:** uno por espacio, que se crea solo. Lo leen y escriben los miembros del espacio con al menos el rol que diga el ajuste del espacio «Quién ve el chat» (`chat_min_role`):
  - **por defecto, editores y administradores**: un lector no lo ve (decisión del dueño, 2026-11-22);
  - se puede abrir a todos los miembros o cerrar a solo administradores;
  - los espacios personales no tienen canal.
- **Mensaje directo:** entre 2 personas, o un grupo pequeño de hasta 8. Solo lo ven sus participantes. Si alguien deja la conversación, deja de verla.
- **El administrador de la instalación** no lee canales de espacios en los que no está ni mensajes directos ajenos. Es lo mismo que con las páginas.

### Mensajes
- **Texto en Markdown corto:** negrita, código, listas y `[[Página]]`. Una mención es `@usuario`.
- **Editar y borrar:** solo quien lo escribió. Se marca «editado», y lo borrado deja «Mensaje borrado».
- **Adjuntos:** los mismos que en las páginas (`/files`), con sus límites.
- **Hilos:** no en la primera versión. Se responde citando.

### Datos
Todo va en el índice, como `notifications`; no va al vault, porque no son notas.

| Tabla | Qué guarda |
|---|---|
| `chat_conversations` | id, tipo (`workspace` o `direct`), `workspace_id`, creada |
| `chat_members` | las personas de un directo (un canal saca las suyas del espacio) |
| `chat_messages` | id, conversación, autor, texto, adjuntos, creado, editado, borrado |
| `chat_reads` | hasta dónde ha leído cada persona cada conversación (para «sin leer») |

- **Búsqueda:** un índice FTS propio para los mensajes, aparte del de las páginas.
- **Avisos:** la tabla `notifications` limita los tipos con un `CHECK`, así que hace falta una migración que rehaga la tabla para añadir `chat_mention`.

### En vivo
- **Mensajes en tiempo real:** van por el canal de eventos que ya existe (SSE, `/events`). Cada mensaje nuevo llega solo a quien puede verlo.
- **«Está escribiendo…»:** un evento efímero que no se guarda.

## Interfaz

- **Panel derecho:** pestañas «Miembros» y «Chat». En «Chat»:
  - la lista de conversaciones: primero los canales de mis espacios, luego los directos, con su contador de no leídos;
  - al abrir una, los mensajes (los más nuevos abajo, con carga hacia atrás) y el cuadro de escribir con `@`, `[[` y adjuntar.
- **Directo nuevo:** desde «Miembros», clic en una persona → «Escribir».
- **Contador global:** los mensajes sin leer suman al punto rojo de la pestaña del navegador, igual que los avisos.
- **Convertir en tarea:** en el menú del mensaje. Se elige la base de Tareas (o «Cuenta para la carga») y se crea una fila con el texto como título, quien lo escribió como responsable propuesto y un enlace al mensaje.
- **Móvil:** el panel derecho ya es una hoja flotante en pantallas estrechas, y el chat va dentro de esa misma hoja.

## Fases

### CH1 — Canales de espacio (hecho)
- Tablas, permisos (`can_read` y `can_write` por conversación) y la API:
  - listar conversaciones;
  - leer mensajes por páginas;
  - enviar, editar y borrar;
  - marcar como leído.
- Eventos SSE de mensaje nuevo, editado y borrado.
- La pestaña «Chat» del panel derecho, con lista, conversación y escribir.
- El contador de no leídos, en la pestaña y en el título del navegador.

### CH2 — Mensajes directos (hecho)
- Crear un directo (1 a 1 o grupo pequeño), añadir y salir.
- «Escribir» desde la ficha de una persona en «Miembros».

### CH3 — Menciones, enlaces y adjuntos (hecho)
- `@usuario` con autocompletado y aviso `chat_mention` (migración de `notifications`).
- `[[Página]]` con la misma vista previa y clic que en el editor.
- Adjuntar archivos e imágenes; las imágenes se ven dentro del mensaje.

### CH4 — Convertir en tarea y buscar (hecho)
- «Convertir en tarea» desde el mensaje.
- Buscar en el chat, con los mismos permisos.
- «Está escribiendo…».

## Para más adelante (anotado, no planificado)
- Hilos de respuesta.
- Reacciones con emoji.
- Canales libres (#diseño, #soporte) y una conversación pegada a cada página: descartados por ahora.
- Llamadas o videollamadas.
