# ADR 0003 — Sugerencias de agentes: parche, sidecar y anclaje

- **Estado:** aceptado (P10a, 2026-11-01)
- **Relacionado:** [`ADR-0001`](ADR-0001-formato-del-vault.md) (formato del vault, P0), [`ADR-0002`](ADR-0002-indice-sqlite.md) (índice SQLite, P1.1), `ROADMAP-v1.0.md` §5-P10
- **Fuera de alcance:** la interfaz (tarjeta siguiente) y el token de agente (otra tarjeta). Aquí solo el modelo, el almacenamiento y la API.

## Contexto

El dueño quiere que un agente —un proceso que lee el vault y propone cosas— pueda
sugerir cambios sobre sus notas: «esta lista de hitos la tienes desactualizada»,
«esto va en otra nota». Hoy la única forma de que un agente escriba es que escriba
de verdad: `PATCH /pages/{id}` con el cuerpo entero, sin revisión y sin vuelta
atrás. Eso son dos problemas distintos:

1. **No hay revisión.** Un cambio de un agente es un cambio del dueño: no queda
   rastro de quién lo propuso ni forma de decir «no».
2. **Reescribir es frágil.** Un agente que manda el cuerpo entero de una nota
   pisa lo que el dueño haya editado mientras tanto, aunque el cambio propuesto
   sea una línea.

La primera entrega de P10 pide, por eso, un modelo de **sugerencias**: el agente
propone, el dueño ve el cambio en su sitio, y acepta o rechaza.

## Decisión

### 1. Una sugerencia es un parche, no un texto libre

Una sugerencia es `old_text → new_text`: **el texto exacto que se sustituye y el
que entra**, más `rationale` (por qué) y un hilo `replies[]` (la vuelta al
agente). Nunca es «el cuerpo nuevo de la nota».

- Un texto libre obliga al dueño a comparar dos documentos enteros para encontrar
  el cambio. Un parche se lee en una línea y se aplica en un sitio.
- Un parche **se puede aplicar aunque la nota haya cambiado** en otra parte: el
  ancla es el texto, no la versión del fichero. Es lo que hace útil la revisión
  diferida: el agente propone por la mañana y el dueño acepta por la tarde.
- El caso «propón una nota nueva» existe (`kind: new_page`) porque el dueño
  quiere que un agente pueda mandarle un listado de hitos como **nota propuesta**
  y no solo retocar las existentes. Ahí no hay parche que aplicar: `old_text` va
  vacío, `new_text` es el cuerpo y `title` es obligatorio.

### 2. Sidecar, nunca dentro del `.md`

Una sugerencia se guarda junto a la nota, en una carpeta hermana:

```
Notas/Proyectos.md
Notas/Proyectos.md.sugerencias/7f3c1a2e-....json
```

y el `.md` **no se toca**: ni marcas, ni bloques `<!-- -->`, ni un hilo en el
frontmatter. Eso es deliberado:

- **El fichero tiene que seguir siendo Markdown limpio** para cualquier otra
  herramienta: un editor cualquiera, un `grep`, un exportador, el `git diff` del
  dueño. Meter comentarios de revisión cambia el documento para todos.
- **Un `.md` con marcas deja de poder compararse con su copia de seguridad**, y
  cada guardado desde la app tendría que preservar el bloque de sugerencias byte
  a byte para no perder el hilo. Eso es un segundo formato que mantener dentro
  del primero.
- El sidecar es **un fichero por sugerencia** y no un único array: aceptar o
  rechazar reescribe un objeto, no un documento compartido. Dos agentes pueden
  proponer a la vez sin pisarse, y una sugerencia corrupta no se lleva el resto.

Consecuencia directa: el sidecar **no es una nota**. El recorrido del vault
(`reader.walk_markdown`, que usan el reindexado y el vigilante) solo recoge `.md`
y además excluye por nombre las carpetas `*.md.sugerencias/`; la carpeta contiene
solo `.json`, así que ni `/pages` ni la deriva de `/status` la ven.

Y la otra mitad de esa frase, que costó tres rondas de revisión: **la nota y su
sidecar son una sola cosa en disco**. No puede existir un `Notas/X.md.sugerencias/`
sin su `Notas/X.md`. Toda operación del vault que saca un `.md` de su ruta pasa por
`suggestions.move_with_note` —o por `suggestions.remove_with_note` si algun día
hay purga definitiva—, que mueve el fichero y su carpeta juntos:

| Operación | Punto | El sidecar viaja |
| --- | --- | --- |
| renombrar / mover de carpeta una nota | `VaultStore.save` | sí |
| renombrar una fila | `VaultStore.save_row` | sí |
| renombrar una base | `VaultStore.save_database` | sí |
| borrar una nota | `VaultStore.delete` | sí, al sello de la papelera |
| borrar una nota con subpáginas | `VaultStore.delete` | sí, **cada hija con el suyo** |
| borrar una fila | `VaultStore.delete_row` | sí |
| borrar una base | `VaultStore.delete_database` | sí (la base y sus filas) |
| restaurar desde la papelera | `VaultStore.restore` | sí, vuelve con su nota |
| renombrar / borrar por la vía vieja | `writer.rename_page`, `writer.delete_page` | sí |
| purga definitiva (sin endpoint todavía) | `suggestions.remove_with_note` | sí |

No es una tabla de buenas intenciones: hay un helper único y un test que la
recorre. Después de **cada** operación de la lista, el test barre el vault y
`.trash/` y falla si alguna carpeta `*.md.sugerencias/` no tiene su `.md` al lado.
Es lo que faltaba en la cascada del borrado: al borrar una nota madre, los `.md`
de las hijas iban a la papelera y sus `.json` se quedaban en el vault. El
directorio de la madre sobrevivía a la poda (la siguiente nota con ese título
nacía «Otra (2)») y la sugerencia seguía viva por `sid` sin verse en ningún sitio.

La papelera respeta esa distinción: `GET /trash` **no** lista los `.json` del
sidecar como entradas sueltas (una nota con dos sugerencias es una entrada, no
tres) y `POST /trash/restore` rechaza restaurarlas por separado — deja un
sidecar de una nota que no existe. Se restauran con su nota.

Lo que **no** se hizo: una tabla en el índice SQLite. El índice es derivado y
reconstruible desde el vault (ADR-0001 D1); las sugerencias son estado propio del
usuario (un hilo de conversación que no se puede deducir de ningún `.md`), así
que un reindexado las borraría. Y el índice no debe tener dos fuentes de verdad.

### 3. Cuando el ancla desaparece

El ancla es `old_text`. Al crear se comprueba contra el cuerpo **de ese
momento**; al aceptar, contra el cuerpo **actual**. Los tres casos:

| Situación | Al crear | Al aceptar |
| --- | --- | --- |
| `old_text` aparece una vez | `pending` | se aplica ahí |
| aparece varias veces | `pending`, con `offset` del agente (o el de la primera aparición) | se aplica en la **más cercana a `offset`**; a igual distancia, la primera |
| no aparece | `orphaned` (se guarda igual) | **409 `{"error": "desanclada"}`**, se queda `orphaned`, el `.md` no se toca |

Tres decisiones detrás de esa tabla:

- **No se rechaza al crear.** Que el texto no esté solo significa que el agente
  se basó en una versión anterior; tirar la sugerencia borra el motivo y la
  conversación. Nace `orphaned`, se enseña como «ya no encaja», y el dueño puede
  responderle por el hilo.
- **Varias apariciones: `offset`, no la primera.** Un agente que propone cambiar
  el segundo `- Pendiente` de una lista sabe cuál es; se le pide el punto del
  cuerpo donde lo vio. Sin `offset` se usa la primera aparición, que es la misma
  regla que aplicaría un humano leyendo de arriba abajo.
- **Una sola sustitución.** Aceptar un parche no cambia las demás apariciones
  del mismo texto: el agente propuso un sitio concreto, no un reemplazo global.
  Si quería todas, manda una sugerencia por sitio.

Y un aviso más, que no bloquea: si el `updated` actual de la nota no es el
`base_revision` con el que se creó, la respuesta de `accept` trae `stale: true`.
El parche se aplica igual (el ancla seguía estando); es información para la
interfaz, no un segundo criterio de rechazo.

### 4. Quién puede proponer

Hoy, **cualquiera que llegue a la API**: no hay autenticación en la app (uso
personal sobre Tailscale) y esta tarjeta no la inventa. El campo `agent` es una
**etiqueta de autoría**, no una credencial: lo pone quien llama y el servidor lo
guarda tal cual, igual que `created_by` en una página.

El token de agente —que un proceso pueda proponer sin poder escribir, con su
identidad verificada— es la tarjeta siguiente. Cuando exista, `agent` pasará a
salir del token y no del cuerpo de la petición; el resto del modelo no cambia.
**Ya existe** ([`ADR-0004`](ADR-0004-puerta-para-agentes.md), P10b): por la
puerta `/agent/...` el `agent` sale siempre del token, y `POST
/pages/{id}/suggestions` sigue aceptando la etiqueta como antes (es la ruta del
dueño).

## Contrato de la API

```
POST /pages/{id}/suggestions     crear (413 si old_text/new_text > 8 KB; 32 KB en new_page)
GET  /pages/{id}/suggestions     listar, ?status=pending|accepted|rejected|orphaned
GET  /suggestions/{sid}          una sugerencia con su hilo
POST /suggestions/{sid}/accept   aplicar (409 desanclada / 409 ya_resuelta)
POST /suggestions/{sid}/reject   marcar rejected, con motivo opcional
POST /suggestions/{sid}/reply    añadir una entrada al hilo del dueño
```

Aceptar escribe la nota **por la ruta de un guardado normal**:
`Repository.update_page` (o `create_page` para `new_page`), es decir `.md`
atómico + índice al día, y publica el evento SSE `page` de la nota
(`modified`, o `added` si es una nota nueva) para que una pestaña abierta se
entere. Aceptar dos veces, o aceptar una rechazada, es **409 `ya_resuelta`**:
una sugerencia resuelta no se vuelve a aplicar.

Límites de tamaño en bytes UTF-8, aplicados a cualquier `kind` y a todo lo que
acaba escrito en el `.json` del vault: `old_text` y `new_text` de un parche, 8 KB;
el cuerpo de una nota propuesta, 32 KB; `rationale`, cada respuesta y el motivo
de un rechazo, 4 KB; `title`, 200 bytes; `agent` y el `author` de una respuesta,
200 bytes. Por encima, **413**.

## Consecuencias

- El `.md` sigue siendo Markdown limpio: lo puede leer cualquier herramienta, y
  borrar la carpeta de sugerencias a mano no rompe nada (se pierden los hilos,
  que es lo que son).
- Las sugerencias no entran en la búsqueda ni en el grafo: son un canal de
  revisión, no contenido de la nota hasta que se aceptan. Cuando se aceptan, el
  contenido es el del `.md` y lo indexa el guardado normal.
- Buscar una sugerencia por id (`/suggestions/{sid}/...`) recorre el vault para
  encontrar su sidecar, porque el id no dice de qué nota es. Es una operación
  por decisión del dueño, no un camino caliente; con el vault en local son
  milisegundos y no lee ningún cuerpo (solo `os.scandir` + el `.json`).
- Borrar la nota y restaurarla devuelve también el sidecar **si el sello de la
  papelera lo tiene al lado**; si ya había una carpeta de sugerencias en el
  destino, la de la papelera se queda ahí en vez de mezclar dos hilos.
- Si el dueño rechaza, el hilo se queda abierto para responder: rechazar no
  cierra la conversación, solo dice que ese parche no entra.
- Pendiente y explícito: la interfaz (ver la sugerencia en su sitio, aceptar y
  rechazar desde el panel), el token de agente, y una política de limpieza para
  sugerencias resueltas antiguas (hoy no se borran nunca; son ficheros de pocos
  cientos de bytes).
