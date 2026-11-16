# ADR 0007 — Estado «cerrado»: una regla de la base

- **Estado:** aceptado (2026-11-16)
- **Contexto:** MyDamnManagement, gestor de proyectos en ficheros `.md`. Esta es
  la función 5 del [backlog](BACKLOG-GESTOR-PROYECTOS.md): «estado cerrado con
  semántica (deja de contar en todas partes)». Hasta ahora «cerrada» lo decidía
  un marcador `done` **dentro de una propiedad** del `schema`
  ([ADR-0006](ADR-0006-subtareas.md), D4), que no se validaba contra las
  opciones reales de esa propiedad: se podía marcar como cerrado un valor que
  no existe y el contador no contaba nunca. Y la semántica solo la usaba el
  contador de subtareas: «Mis tareas» tenía su propia lista de valores cableada
  y los filtros no podían preguntar por «cerrada» sin enumerar valores.
- **Relacionado:** [ADR-0001](ADR-0001-formato-del-vault.md) (el `.md` es la
  verdad), [ADR-0002](ADR-0002-indice-sqlite.md) (índice derivado) y
  [ADR-0006](ADR-0006-subtareas.md) (subtareas y contador).

## Decisiones

**D1 — La regla es de la BASE y se declara en su frontmatter.**

```yaml
# Bases/Proyecto.md
closed:
  property: estado
  values: [hecha, cerrada]
```

`property` es la clave de la columna que dice el estado (una selección o una
casilla); `values` son los valores de esa columna que cuentan como cerrada (en
una casilla van vacíos: «marcada» es el único estado que cierra). Una sola
propiedad cerrada por base: si se marca otra, la regla se mueve, porque dos
propiedades no pueden dar dos respuestas a la misma pregunta —lo mismo que ya
decidía la ADR-0006 al mirar solo el primer marcador—.

El bloque **no** vive dentro del `schema` porque no es una propiedad de la fila:
una selección puede cambiar sus opciones (o su tipo) sin arrastrar consigo la
semántica de «cerrada», y `plan_schema` no tiene que saber nada de ella. Tampoco
es una columna de la fila: no hay nada que escribir en cada `.md`.

**D2 — La regla se valida contra las opciones reales.** `PUT
/databases/{id}/closed` comprueba que la propiedad existe, que es una selección o
una casilla y que cada valor es una opción de esa propiedad; si no, es un **400
que dice qué valores hay**. Es justo el agujero del marcador viejo: un `done`
que nombraba un valor inexistente se guardaba y no contaba nunca. Se guarda la
grafía de la opción, no la que teclee el cliente.

**D3 — Compatibilidad de lectura del `done`, escritura nueva.** Las bases que ya
tienen `done: [Hecho]` (o `done: true`) se leen como si declararan `closed`, sin
migración masiva y sin reescribir sus ficheros. Al guardar la regla desde la
interfaz se escribe el bloque `closed:` y se retiran los `done` del `schema` en
la misma escritura: el formato nuevo es el único que la app produce, y quitar
la regla después no puede resucitar un marcador olvidado.

**D4 — Una sola implementación.** `backend/app/closed.py` es el único sitio que
decide qué está cerrado, y todos los consumidores lo usan: el contador de
subtareas, el campo `closed` de cada fila, los filtros de una vista
(`is_closed` / `is_open`, que no necesitan enumerar valores ni acertar con la
clave de la columna) y `GET /my-tasks`. El índice guarda **la regla declarada**
(tabla `database_closed`, índice v7); la efectiva se resuelve al leer, así que un
bloque pegado a mano en el `.md` manda tras reindexar.

**D5 — Sin regla no hay estado.** Si la base no declara nada, nada se considera
cerrado: el contador sale «0 de N», «Mis tareas» la enseña entera y `is_closed`
no deja pasar ninguna fila. La lista de respaldo que tenía `/my-tasks`
(`CLOSED_STATES`: hecho, cerrada, done, ganado…) **se retira**: era una segunda
verdad escondida en el código que podía esconder filas que el `.md` de la base
no declaraba cerradas. Los valores que viajan en `closed_values` son ahora la
unión de los que declaran las bases, para que la interfaz explique el criterio.

**D6 — Nada de dos verdades.** Cambiar los valores de la regla recalcula
contadores sin tocar el `.md` de ninguna fila (los valores de las celdas siguen
donde estaban); quitar el bloque no cierra nada. Renombrar la propiedad del
estado mueve la regla a la clave nueva; borrarla, o cambiarle el tipo a uno que
no puede ser estado, retira la regla con el schema.

## Alternativas descartadas

- **Seguir con un `done` por propiedad, validado contra las opciones.** Arregla
  el 400 pero deja la semántica repartida por el schema, sin poder decir «esta
  base tiene un único estado», y sin un sitio donde la interfaz lo explique.
- **Migrar todas las bases de golpe.** Reescribir ficheros del usuario para
  ahorrar un camino de lectura es peor que leer dos formatos durante un tiempo:
  el `.md` es del usuario y la compatibilidad de lectura no cuesta nada.
- **Guardar el estado en el índice como columna de cada fila.** Duplicaría en el
  índice lo que ya dicen las celdas, y un cambio de regla tendría que reescribir
  miles de filas.

## Consecuencias

- Una base nueva que declare estado desde una plantilla ya puede traer la regla
  (`DatabaseCreate.closed`), que es lo que necesita la función 29 (plantillas).
- El contador, «Mis tareas», los filtros y el orden/recuento del tablero dicen lo
  mismo porque salen del mismo módulo; ninguna pantalla repite el criterio.
- Quien edite el `.md` a mano puede declarar la regla sin pasar por la app: al
  reindexar se aplica. Un valor que no exista en las opciones **no** se rechaza
  en la lectura (el `.md` es del usuario): simplemente no cuenta, que es lo
  honesto cuando el fichero se ha escrito fuera de la app.
