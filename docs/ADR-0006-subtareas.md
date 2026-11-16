# ADR 0006 — Subtareas: una fila hija de otra

- **Estado:** aceptado (2026-11-16)
- **Contexto:** MyDamnManagement, gestor de proyectos en ficheros `.md`. Hasta
  ahora todas las filas de una base están al mismo nivel: un proyecto es una
  lista de cosas, no un plan. Esta es la función 2 del
  [backlog](BACKLOG-GESTOR-PROYECTOS.md): «subtareas con estado propio y
  recuento en la tarjeta», y sin el recuento no se usa.
- **Relacionado:** [ADR-0001](ADR-0001-formato-del-vault.md) (el `.md` es la
  verdad) y [ADR-0002](ADR-0002-indice-sqlite.md) (índice derivado, sin otra
  fuente de verdad). La [ADR-0005](ADR-0005-verdad-en-base-de-datos.md) aplaza
  el cambio de fuente de la verdad a una versión mayor; esto se implementa con
  la arquitectura de hoy (ficheros), que es lo que la ADR-0005 mantiene
  mientras tanto.

## Decisiones

**D1 — La jerarquía es una PROPIEDAD de tipo `parent_row`, no un campo del
esquema.** La celda guarda el `id` de la fila madre y vive en el frontmatter de
la hija, como una `relation`:

```yaml
# Bases/Plan.md
schema:
  - key: paso_de
    name: "Subtarea de"
    type: parent_row
  - key: estado
    name: Estado
    type: select
    options: [Pendiente, "En curso", Hecho]
    done: [Hecho]
```

```yaml
# Bases/Plan/9f3c1a2b.md   (la hija)
id: 9f3c1a2b-...
title: Escribir el capítulo 2
paso_de: 7a1b4c5d-...      # id de la fila madre, no su título
estado: Pendiente
```

El motivo: la celda tiene que estar en el `.md` de la fila, y todo lo que hace
falta para que una celda funcione —lectura con coacción por tipo, escritura
byte a byte, migración de claves al renombrar la propiedad, índice, filtros,
orden, exportación— ya existe y es exactamente la maquinaria de una propiedad.
Un campo aparte (`hierarchy: {parent: paso_de}`) obligaría a duplicarla y a
mantener dos caminos de lectura en paralelo. **Una sola por base**: la
jerarquía es una, no una por columna; el servidor rechaza con 400 la segunda.

**D2 — El id, nunca el título**, igual que `relation`: los títulos cambian y un
vínculo guardado por título se rompería al renombrar la madre.

**D3 — Madre e hija son de la MISMA base, y no hay ciclos.** Al guardar se
comprueba que la fila madre existe y es de esa base, que la fila no es su propia
madre y que la madre no es una descendiente suya (subiendo por la cadena de
madres). El rechazo es un **400 con el motivo y la ruta del círculo**:

```
«Subtarea de»: no se puede colgar «Lanzar la web» de «Diseño» porque ya cuelga
de ella (Diseño → Textos → Lanzar la web): sería un ciclo. Elige otra fila madre.
```

Un ciclo solo podría entrar editando el `.md` a mano; el resto del código no
se cuelga si lo encuentra. `arrange` emite cada fila una vez, y `only_roots`
(la vista «solo madres») recorre la cadena de madres con un conjunto de
visitadas: una fila cuya ascendencia solo da vueltas no cuelga de ninguna raíz
de la lista, así que **se enseña como raíz**. Antes se descartaba por tener
madre en la lista y las dos filas del círculo desaparecían de la tabla y del
tablero por defecto, sin aviso; un dato raro del usuario no puede vaciar la
vista.

**D4 — «Cerrada» lo decide una REGLA DE LA BASE** (revisado por la
[ADR-0007](ADR-0007-estado-cerrado.md)).

El contador no interpreta el schema: pide la regla de la base
(`closed: {property, values}` en su frontmatter, o los marcadores `done` viejos
como compatibilidad de lectura) y cuenta con ella. **Sin regla, ninguna hija
cuenta como cerrada** (`0 de N`): la base no sabe qué es estar cerrada y el
contador no se lo inventa.

Para el lector de esta ADR: el `done` por propiedad que describía la versión
anterior de este apartado **ya no se escribe**. Se sigue leyendo para no romper
las bases que lo tenían, y al marcar la regla desde la app se convierte en el
bloque `closed:` y el marcador se retira. La interfaz ya no lo pregunta en el
diálogo de Propiedades: se marca en el menú de la columna del estado, y el
servidor lo valida contra las opciones reales de esa columna.

**D5 — El contador cuenta las hijas DIRECTAS**, sobre **todas** las filas de la
base: «1 de 3» es «una de sus tres hijas», no de toda la descendencia, y no
cambia porque la vista esté filtrando por otra cosa. Las nietas se ven al abrir
la hija, que es madre a su vez.

**D6 — El nivel de filas es de la VISTA**, `parent_level: roots | all` en su
`config` (el frontmatter de la base, como `filters` o `sort`):

- `roots`: solo las filas que no son hijas de ninguna de la base. Es el
  **defecto en tabla y tablero**: una hija ya se ve bajo su madre, y repetirla
  es ruido.
- `all`: todas, con las hijas detrás de su madre (en la tabla y la lista, con
  sangría y flecha; en el tablero, la tarjeta dice «↳ madre»).
- Sin la clave manda el tipo de vista: `roots` en tabla y tablero, `all` en
  lista y calendario. **Una base sin jerarquía no cambia en nada**: sin
  propiedad `parent_row` no hay nivel que aplicar.

«Todas (sin vista)» no tiene dónde guardar un nivel y las enseña todas, pero en
orden de jerarquía.

**D7 — Orden: la hija va detrás de su madre; entre ramas manda la vista.** El
`sort` de la vista decide el orden de las ramas (y cuál va primero), y dentro de
una rama se repite la regla. Una hija cuya madre no está en la lista (la filtró
la vista, o apunta a un id que ya no existe) se queda como fila suelta en su
sitio: nada de lo que el filtro dejó pasar se esconde.

**D8 — El grafo dibuja madre → hija como arista `kind: "subtask"`**, no como un
enlace (`links`): los backlinks de una madre no tienen por qué listar sus
subtareas. Se deduce de la celda en cada petición, y la vecindad local la
recorre en los dos sentidos.

**D9 — La exportación no pinta la celda como columna** (es un id): la jerarquía
se lee en el orden de las filas y en el contador, no en una columna de uuids.

## Lo que ve quien usa la app

- En la tabla y la lista, la hija va sangrada bajo su madre (`↳`) y la madre
  lleva su contador («1 de 3», cerradas). La sangría de la tabla se pinta solo
  cuando la vista **no agrupa** por otra cosa: en una vista agrupada la fila cae
  donde la pone su grupo y la sangría mentiría.
- En el tablero, la tarjeta de una subtarea dice de quién es; las madres llevan
  su contador.
- La fila madre se abre como página y ahí están sus subtareas: una mini-tabla
  con título, estado (la propiedad que declara la regla `closed`, si la hay) y fecha, con «+ Añadir
  subtarea», que crea la fila **ya enlazada**. Es donde se trabaja un
  entregable.
- La barra de la vista tiene el selector «Filas» (solo madres / todas) y lo
  guarda en el `.md`.
- El diálogo de Propiedades marca cuál es la propiedad que dice si una fila
  está cerrada (D4), que es lo que alimenta el contador.

## Consecuencias

- El `.md` de una hija gana una clave más (`paso_de: <uuid>`), con el mismo
  criterio que una relación. Un vault sin subtareas se lee igual que antes, y
  una base sin la propiedad se comporta exactamente como antes (lo fija
  `test_una_base_sin_jerarquia_responde_igual_que_antes`).
- El contador y el nivel se calculan en Python sobre las filas de la base: la
  base se sirve entera en cada lectura de filas, así que no hay consulta nueva
  por fila. Una base de miles de filas con jerarquía no cambia el orden de
  magnitud.
- Borrar una madre no borra sus hijas: quedan con un `parent_id` que ya no
  resuelve y se leen como raíces (no se esconden). Si se quiere borrar el
  subárbol, es una tarjeta aparte (borrado en cascada, con confirmación).
- La página de la fila trae sus subtareas en la misma respuesta (`subtasks`): la
  tabla no las arrastra, para no convertir una respuesta de cien filas en cien
  árboles.
- **El nivel se aplica antes que los filtros de la vista.** Con «solo madres»,
  un filtro sobre una propiedad que solo tienen las hijas (el estado, sin ir
  más lejos) deja la vista vacía: la madre no cumple el filtro y una fila no se
  enseña «porque su hija cumple». Heredar el filtro hacia la madre —y decidir
  qué significa «cerrada» al filtrar— es una decisión de producto aparte; hoy,
  para filtrar hijas, se elige «todas las filas» en esa vista.
