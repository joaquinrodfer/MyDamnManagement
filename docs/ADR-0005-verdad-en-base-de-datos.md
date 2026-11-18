# ADR 0005 — La verdad pasa a la base de datos; el Markdown queda como autoría y exportación

- **Estado:** aceptado (2026-11-15), por el dueño.
- **Sustituye parcialmente:** [ADR-0001](ADR-0001-formato-del-vault.md) D1 («los ficheros son la verdad; el índice es reconstruible»).
- **Contexto:** decisión tomada al planificar el gestor de proyectos, con la investigación de producto delante ([INVESTIGACION-GESTOR-PROYECTOS.md](INVESTIGACION-GESTOR-PROYECTOS.md), §6.4, opción **B**).

## La decisión

**B — La base de datos es la verdad; el Markdown es el formato de escritura y de exportación.**

En palabras del dueño: «me gustaría que todo tenga el formato al escribir MD para conseguir una exportación sencilla y posible, pero realmente se va a trabajar todo desde la app».

Eso son tres cosas distintas que conviene no mezclar:

1. **Se sigue escribiendo Markdown.** El editor no cambia: bloques, sintaxis `.md`, atajos, menú `/`. Lo que se escribe y lo que se ve sigue siendo Markdown.
2. **Se guarda en la base de datos.** Deja de haber un fichero que sea la fuente de la verdad. La app lee y escribe la base; el fichero, cuando exista, es una **proyección**.
3. **Se exporta a `.md` cuando se quiera**, completo y sin pérdida, y ese export es una función de primera clase del producto, no un extra.

## Por qué

- **Concurrencia real.** Con dos o tres personas escribiendo a la vez, «un solo escritor: la API» y un fichero por nota son la pared. La opción A (ficheros + CRDT encima) conserva todo, pero obliga a resolver qué pasa cuando el `.md` cambia por fuera con una sesión colaborativa abierta, y a mantener una capa de sincronización sobre ficheros que no ganan nada.
- **Consultas.** «Mis tareas», carga de trabajo, contadores y agregados son consultas, no ficheros. Con la base como verdad, son SQL; con ficheros, son recorrer el vault.
- **Tiempo real sin pelear con el sistema de ficheros.** El día que se aborde, el documento vive donde se sincroniza, no en un `.md` que hay que materializar.

## Lo que se pierde, dicho claro

- **Abrir las notas sin la app deja de ser cierto** salvo que se exporte. Ese era uno de los argumentos del producto y hay que asumirlo conscientemente.
- **«Borra el índice y se reconstruye» deja de ser una garantía.** El índice pasa a ser el dato y **hay que respaldarlo**: el backup del vault ya no basta.
- **El historial tipo git** deja de salir de los ficheros. Si se quiere historial, se implementa sobre la base o sobre el export periódico.
- **La migración de datos de fuera** (Obsidian y demás) pasa a ser importación, no «copiar ficheros».

## Lo que no cambia todavía (etapa 0)

**No se migra nada hoy.** Este ADR fija la dirección y, con ella, dos obligaciones inmediatas en todo lo que se escriba a partir de ahora:

1. **Nada nuevo se guarda de una forma que no se pueda serializar a Markdown.** Toda función nueva (propiedades, subtareas, comentarios si llegan) guarda estructurado y sabe escribirse a `.md` sin pérdida: si un dato no cabe en el formato, o se representa en él o se dice explícitamente que no se exporta.
2. **El export completo es la red de seguridad.** Mientras exista un `POST /export` que reconstruya **todo** el vault desde la base y un test que lo compare con el original, volver a la opción A sigue siendo barato. El día que ese export no exista o mienta, B se vuelve irreversible.

## Camino por etapas

Cada etapa tiene sentido por sí sola y deja la app funcionando:

- **Etapa 1 — export íntegro y verificado.** Del índice a un vault completo (notas, bases, filas, adjuntos, papelera) y un test de ida y vuelta: exportar, comparar fichero a fichero con el original. Sin esta etapa, B no se toca.
- **Etapa 2 — los cuerpos dejan de necesitar el fichero.** `pages.body` ya guarda el cuerpo; la verdad de una nota pasa a ser esa columna y el `.md` se escribe solo al exportar (o en una proyección periódica). Las bases ya están estructuradas en el índice: aquí es donde el modelo se invierte de verdad.
- **Etapa 3 — varios escritores.** Lo que hoy bloquea la concurrencia: versiones por documento (`If-Match`), y después la capa de sincronización (CRDT) sobre el documento guardado.
- **Etapa 4 — tiempo real.** Presencia, cursores, edición simultánea. Es la función 26 del backlog y no se adelanta al resto.

## Consecuencias para el backlog

- **26 (presencia / edición simultánea) deja de ser «ver la decisión de arquitectura»**: la decisión está tomada y lo que falta es la etapa 3.
- **33 (importar/exportar CSV)** y el export a Markdown se vuelven más importantes de lo que parecían: son la puerta de entrada y de salida de los datos.
- **32 (permisos)** sigue aplazada por producto, no por arquitectura: con una persona no aporta, y con varias ya no es un parche.

## Estado de la implementación (3.0)

- **Etapa 1 — hecha (3.0, hito 1).** `POST /export` reconstruye el vault entero desde la base (zip o carpeta) y `tests/test_h1_export_vault.py` hace la ida y vuelta: export byte a byte igual al vault, y reindexar el export da la misma huella de índice.
- **Etapa 2 — hecha (3.0, hito 2).** Cada `.md` del vault (papelera incluida) es una fila de `documents` y el almacén lee y escribe ahí en la misma transacción que el índice (`app/vault/docfs.py`). El primer arranque importa el vault de disco una sola vez (`meta.storage = db`); los siguientes no lo leen. El `.md` en disco es una proyección opcional (`VAULT_PROJECTION=1`). Cada documento lleva `version` y `PATCH` acepta `If-Match` (409 con la versión y el documento actuales). Adjuntos y sugerencias de agentes siguen en disco (no son `.md`).

