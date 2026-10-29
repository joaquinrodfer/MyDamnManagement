# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). Versionado según [SemVer](https://semver.org/lang/es/): cada fase del roadmap avanza la versión menor (`0.x.0`) hasta que el proyecto se considere estable.

## [Sin publicar]

### Cambiado

- **P2 — las bases de datos y sus filas pasan a ser ficheros del vault**: `Bases/<slug>.md` con `type: database`, el `schema` y las `views` en el frontmatter, y una fila por `.md` en `Bases/<slug>/`, con las propiedades en el frontmatter y **sin** `title` (el título de una fila es el nombre del fichero; `title` sigue siendo clave reservada del `schema`). Con esto se cumple el criterio de la v1.0: todo está en `.md` y el índice se puede tirar. Crear la base (también desde la plantilla CRM/Tareas), editar `schema` o `views`, crear/editar/borrar filas y renombrar cualquiera de las dos cosas escriben el fichero y actualizan el índice en la misma operación, con escritura atómica; renombrar una base renombra su carpeta de filas, y borrar manda a la papelera el `.md` de la base con los de sus filas. El reindexado (arranque y `POST /reindex`) reconstruye bases, filas y vistas, y una fila huérfana (su `Bases/<slug>.md` no existe) se avisa sin abortar. La deriva de `/status` cuenta ya todos los `.md` indexados. Script de una vez, idempotente y con contadores, para volcar al vault las bases y filas que solo estaban en el índice: `backend/scripts/migrate_databases_to_vault.py`. Cambios de forma que conviene saber: el `id` de una base pasa a ser el de su página (`database.id == page_id`), las filas llevan `created`/`updated` en el frontmatter, una base que cuelga de una nota se escribe en la carpeta de hijas de esa nota (antes no tenía fichero y solo vivía en el índice), y `VAULT_ALLOW_EMPTY=1` sobre un vault vaciado deja ahora el índice **vacío** en vez de conservar bases y filas sin padre. El escritor de YAML (`app/vault/fm.py`) baja ya a listas de mapas y mapas anidados: los `filters`/`sort` de una vista se escribían como su `repr` de Python.

- **P1.2b — los adjuntos pasan a vivir dentro del vault** (`_attachments/<año>/<mes>/header-<id>.<ext>`, con año y mes UTC del momento de la subida). Hasta aquí la imagen de cabecera se guardaba en `/app/attachments`, un volumen aparte: copiar el vault se llevaba los `.md` y **no** las imágenes, así que el backup que promete la v1.0 no lo era. `/files` deja de ser un `StaticFiles` y pasa a ser una ruta que sirve `<vault>/<ruta>`: lo que se guarda en `header_image` (frontmatter) y `header_image_path` es la propia ruta del fichero en el vault, el `Content-Type` se decide por los **magic bytes** del fichero (no por su extensión ni por lo que declaró el cliente al subirlo) y fuera de `_attachments/` no se sirve nada. Escribir un adjunto es atómico, como los `.md`. Script de una vez, idempotente y con contadores (`movidos`, `ya_estaban`, `notas_actualizadas`, `indice_actualizado`) para mover los adjuntos del volumen viejo y reescribir las referencias en el frontmatter y en el índice: `backend/scripts/migrate_attachments_into_vault.py`. Desaparece el volumen `attachments` de `docker-compose.yml` y la variable `ATTACHMENTS_DIR`.

- **P1.2a — las notas pasan a ser ficheros `.md`** (`VAULT_PATH`, por defecto `/data/vault`). Guardar una nota (`POST /pages`, `PATCH /pages/{id}`, cabecera) escribe su fichero con frontmatter (`id`, `icon`, `description`, `created`, `updated`, más `created_by` y `header_image`, que si no se perderían al reindexar) y actualiza el índice en la misma operación, con escritura atómica `tmp`+`rename`. Renombrar renombra el fichero (y su carpeta de subpáginas) conservando el `id`; `PATCH` acepta `parent_id` para mover de carpeta (`null` = raíz); borrar mueve a `.trash/<fecha UTC>/<ruta original>`. El índice pasa a ser derivado: se reconstruye desde el vault en **cada arranque** y con `POST /reindex` (nuevo) cuando se editan los `.md` por fuera. Ficheros con CRLF, BOM, Latin-1 o frontmatter sin cerrar se leen con aviso y no abortan el reindexado; un `.md` creado a mano sin `id` recibe un UUID derivado de su ruta que se materializa en el fichero al guardarlo. Script de una vez, idempotente y con contadores, para volcar al vault las notas que ya estaban en el índice: `backend/scripts/migrate_index_to_vault.py`. Los ids de enlace pasan a ser deterministas (uuid5) y el `workspace` por defecto tiene id fijo, para que borrar el índice y reconstruirlo dé exactamente el mismo contenido. Un PATCH que solo cambia el título ya no reescribe el cuerpo del fichero. Las bases de datos y sus filas siguen en el índice (P2).

- **La app ya no necesita Postgres**: el almacén pasa a ser un único índice SQLite (`backend/app/index.py`) con capa de repositorio (`backend/app/repository.py`), sin servicio `db` en `docker-compose.yml`, sin puerto 5432 y sin Alembic al arrancar. Las rutas y las respuestas de la API no cambian. La búsqueda pasa de `tsvector` a FTS5 y sigue encontrando sin acentos (`reunion` encuentra `Reunión`). Un índice ilegible se aparta y se rehace al arrancar. Incluye `pytest` sin servicios externos (`backend/tests/`) y un script de una vez para traer los datos de Postgres (`backend/scripts/migrate_postgres_to_sqlite.py`).

### Corregido

- **Bases en ficheros (P2), restos de la revisión**
  - `VaultStore.delete()` **ya no tiene un camino que borre solo el índice**: una base o una fila se delegan a `delete_database`/`delete_row` (su `.md` y el de sus filas van a la papelera, y las páginas de las filas se sueltan del índice, que la cascada no se las lleva); el único borrado solo-índice que queda es el de una página sin `path`, que no tiene fichero que el reindexado pueda leer. Antes, llamar `delete()` con una base borraba su fila del índice y dejaba el `.md`, así que el siguiente reindexado la resucitaba (D1). El guardia al borrar una nota de la que cuelga una base dice ya la razón real: esos ficheros van a la papelera con la nota, pero `POST /trash/restore` devuelve un fichero a la vez, así que la nota volvería sin su base.
  - El aviso de arranque con **`VAULT_ALLOW_EMPTY=1`** dice lo que pasa: el índice se queda **vacío**, no «con las bases, filas y vistas conservadas, sin padre» (eso era P1.2a; desde P2 también son ficheros). Corregido en el log de arranque, en la ayuda del guardia del vault y en el comentario de la config.
  - El ADR documenta las **claves reservadas del `schema`** que ya aplica `RESERVED_ROW_KEYS`: `title`, `id`, `icon`, `description`, `created`, `updated` y `type`. Solo nombraba `title`, así que una propiedad `id` desaparecía del índice con un aviso que el ADR no explicaba; los avisos del lector y del escritor (`app/vault/reader.py`, `app/vault/writer.py`) nombran ya la lista y no atribuyen todo a que el título es el nombre del fichero.

- **Bases en ficheros (P2)**
  - **Copiar el fichero de una base a mano ya no tumba el reindexado ni el arranque**: `Bases/Contactos.md` y `Bases/Contactos copia.md` traen las mismas `views` con el mismo `id` en el frontmatter, y `_index_database` las insertaba tal cual — `UNIQUE constraint failed: views.id`, `/reindex` en 500 y el siguiente arranque sin levantar. Ahora la primera base por ruta (determinista) se queda con los ids del fichero y la copia recibe ids derivados y estables (uuid5 de su base, su posición y su nombre) con aviso, como ya se hacía con el `id` duplicado de una página; dos reindexados dan la misma huella. Vale también para dos vistas con el mismo `id` dentro del mismo fichero.
  - `_repath_children` comparaba el prefijo de la carpeta **sin la barra final** (`substr(path, 1, len)` contra `"Notas/X/"`), así que no actualizaba nada: al renombrar o mover una nota, el índice se quedaba con la ruta vieja de cada descendiente hasta el siguiente reindexado. Lo destapó la comprobación de que, tras cada escritura, reindexar deja la huella idéntica — que ahora es un test.

- **P1.2b, tras la revisión**
  - La **migración de adjuntos ya no deja fuera las notas de la papelera**: además de las notas vivas, reescribe el `header_image` de los `.md` que están en `.trash/<sello>/` (solo el frontmatter; el fichero se queda donde está), con su propio contador `papelera_actualizadas`. Antes, una nota borrada antes de migrar se quedaba apuntando a `/files/pages/<id>/…`, su adjunto migrado quedaba sin ninguna referencia viva (huérfano) y al restaurarla la cabecera daba **404** — y la salida del script no decía nada. Se arregla en la migración, que es cuando se mueve el fichero al que apunta, y no en cada restauración futura. Las notas que apuntan a una cabecera que no existe ni en el origen ni en el vault siguen saliendo como `aviso:` y se dejan intactas.
  - **`POST /trash/restore` con un ancestro de la ruta ocupado por un fichero es 409**, con el motivo (p. ej. existe `Notas/Proyectos` como fichero sin extensión y se restaura `Notas/Proyectos/Bugs.md`). Antes `vaultfs.move` hacía `os.makedirs` del directorio destino, `FileExistsError` no se capturaba y salía un **500** mudo; nada se perdía (la entrada sigue en la papelera y el intruso intacto), pero el cliente no sabía qué pasaba. Además del chequeo explícito, el router traduce cualquier `OSError` del disco a 409 con el motivo en vez de 500.
  - La migración de adjuntos acepta **`--reindex`**, como su script hermano `migrate_index_to_vault.py`: reescribir el frontmatter cambia el `mtime`, así que sin él `GET /status` mostraba deriva (`drift.modified` = notas reescritas) justo después de migrar. El README documenta el paso (y por qué). Respeta el guardia de siempre: si `--vault` apunta a un sitio sin `.md` y el índice tiene notas, no reindexa, lo dice y sale con 1, sin tocar el índice (los ficheros migrados se quedan como están). Y si el `--index` **no existe**, tampoco reindexa ni lo crea: avisa (`no se reindexa: no hay índice en …`), deja los contadores como están y sale con 0 — antes `open_index` creaba un índice nuevo con el vault y el script decía «reindexado», un éxito falso cuando el `--index` era una errata o faltaba el volumen (el índice real se quedaba con deriva). Mismo criterio que `migrate_index`.

- **Notas en ficheros (P1.2a), tras la revisión**
  - El **arranque reindexa siempre** desde el vault (ADR-0001 D2), no solo cuando el índice se ha tenido que recrear. Si el proceso murió entre escribir el `.md` y actualizar el índice, el arranque recoge el fichero antes de servir la primera petición; antes la API servía el cuerpo viejo y el autoguardado del panel lo escribía encima del fichero, que era el único que tenía el último guardado.
  - Un **`VAULT_PATH` que no existe o un vault sin ningún `.md` ya no vacían el índice**: `POST /reindex` devuelve **409** con el motivo y el arranque falla en claro (el índice queda intacto). `POST /reindex?force=true` es la salida explícita para vaciar el índice a propósito. Un volumen sin montar no es un vault vacío.
  - Mover una página **dentro de sí misma o de una de sus subpáginas** es **400** con el motivo (antes `rename()` devolvía `EINVAL` y salía un 500 mudo), y subir o quitar la **cabecera de una nota cuyo `.md` ya no está** también es 400 (la imagen recién subida se retira, sin huérfanos).
  - Borrar una nota de la que **cuelga una base de datos** es **400** en P1.2a: la base y sus filas viven solo en el índice y la cascada se las llevaría sin papelera (simétrico con «una nota no puede colgar de una base»).
  - `POST` y `PATCH` escriben el `.md` con **el mismo formato** (línea en blanco tras el frontmatter y salto final); antes el mismo documento cambiaba de bytes según se hubiera creado o editado.
  - La **migración índice → vault resuelve los nombres finales antes de escribir**: con dos hermanas del mismo título, las hijas de la segunda ya no acaban en la carpeta de la primera (`Misma/` vs `Misma (2)/`).
  - El **arranque ya no puede dejar el servicio caído sin salida practicable**: con el índice lleno y el vault vacío a propósito (vaciado a mano, restore parcial del volumen del vault, `VAULT_PATH` a un directorio vacío), se arranca una vez con **`VAULT_ALLOW_EMPTY=1`** — una vía que no depende de `POST /reindex?force=true`, que es justo el servicio que no está en pie (con `restart: unless-stopped`, el RuntimeError era un bucle de reinicio). Ese arranque deja **0 notas** y **conserva las bases, filas y vistas** del índice, sin padre. Una raíz inexistente sigue fallando: un volumen sin montar no es un vault vacío. El mensaje de error nombra la variable y ya no recomienda el endpoint.
  - `stored_body` ya no añade un salto de más cuando el cuerpo empieza por salto (`"\n\nhola\n"`): `POST` y `PATCH` escriben el `.md` idéntico byte a byte. El criterio vive en un solo sitio (`writer.body_block`).

- **Índice SQLite (P1.1), tras la revisión**
  - La migración de Postgres **ya no aborta si una página hija se inserta antes que su padre** (`SELECT * FROM pages` no garantiza orden: basta con editar el padre después de crear al hijo): las claves foráneas se difieren al `COMMIT` y se comprueban justo antes con `PRAGMA foreign_key_check`. Un fallo de datos sale ahora con código 3 y un mensaje que dice tabla y fila, no con un traceback.
  - Una **fila de database con `database_id` a NULL** (el esquema viejo lo permitía al borrar la database) ya no tumba la migración: se omite, se cuenta y se avisa con su id.
  - Los **wikilinks vuelven a resolver como antes**: `[[REUNIÓN DE EQUIPO]]` encuentra `Reunión de equipo` (`COLLATE NOCASE` de SQLite solo pliega ASCII) y un título repetido recibe el backlink en **todas** sus páginas, no en una al azar (`LIMIT 1`). `Peña` y `Pena` siguen siendo títulos distintos, como con el `lower()` de Postgres.
  - Un cuerpo que nombra **el mismo destino dos veces con formas distintas** (`[[REUNIÓN DE EQUIPO]]` y `[[Reunión de equipo]]`, o la misma cadena en NFC y en NFD) ya no deja **dos backlinks ni dos filas en `links`**: vuelve a haber un vínculo por página destino, como antes de P1.1 (el `IN` de SQLAlchemy devolvía cada página una vez). De las formas se queda la primera del cuerpo, así que guardar dos veces el mismo texto escribe la misma fila.
  - Un **`INDEX_PATH` equivocado ya no toca un fichero ajeno**: la inspección es de solo lectura y un esquema que no es el nuestro (o una `pages` con otras columnas) para el arranque con un error claro, sin ponerle el esquema encima ni renombrarlo como «corrupto».
  - Y un `PATCH` con el mismo título/icono/descripción ya no mueve `updated_at` (el viejo tampoco lo movía; guardar el cuerpo sí lo mueve, como antes).
  - La imagen fija su base (`python:3.12-slim-bookworm`) y comprueba la versión de SQLite en el build; la app exige `>= 3.27` en cada conexión. `.env.example` ya no tiene `POSTGRES_*` y documenta `INDEX_PATH`. Tests nuevos para todo lo anterior (orden inverso en la migración, fila sin `database_id`, FK rota, títulos con acentos/repetidos, fichero ajeno).

### Añadido

- **`GET /trash` y `POST /trash/restore`** (P1.2b): recuperar algo borrado deja de ser un `mv` a mano. El listado da ruta original, fecha del borrado (UTC, del sello de la carpeta) y tamaño; restaurar devuelve el fichero a su ruta y reindexa en la misma petición, así que la nota vuelve a la app con su id y su cuerpo. Si la ruta original está ocupada, **409** con el motivo. Sin UI: eso es P4.

- **Deriva entre índice y disco en `/status`** (P1.2b): cuántos ficheros han cambiado por fuera desde el último reindexado — modificados, añadidos y desaparecidos — comparando `mtime` + tamaño, sin leer cuerpos (una pasada de `scandir` y una consulta al índice), más `last_reindex_at`. Esa marca vive en la tabla `meta` del índice (esquema **versión 2**, con migración desde la 1); `meta` queda fuera de la huella de contenido, que si no dos reindexados del mismo vault darían hashes distintos. Tras `POST /reindex` la deriva vuelve a cero. Demostración ejecutable del backup completo (copiar el vault y abrirlo solo): `backend/scripts/verify_p1_2b.py`.

- **Citas (`> texto`), anidables (`> > texto`)**: nuevo tipo de bloque, convertible desde el menú contextual y desde el comando `/quote`. Al editar se ve el Markdown crudo (el `>` tenue, como el resto de marcadores); en cuanto se deja de editar, se ve una barra de color de acento por nivel de anidación (una cita dentro de otra se nota) sobre un fondo gris clarito con las esquinas redondeadas arriba y abajo. Toda la cita (con independencia de cuántos niveles tenga dentro) es un único bloque seleccionable/convertible/borrable, igual que un bloque de código.
- **Índice de la página**: desplegable flotante (no una barra a todo lo ancho) fijo arriba a la derecha, con los encabezados (H1-H5) de la nota abierta; clic en uno para saltar a esa posición. Se recalcula en cada pulsación (no hace falta guardar para verlo actualizado) y solo aparece si la página tiene algún encabezado.

- **Botón de inicio** (🏠, junto a atrás/adelante/subir) para volver a la landing de páginas recientes/fechas cercanas cuando se quiera, no solo al arrancar o al borrar la página abierta. Se integra con el historial: `atrás` después de ir al inicio vuelve a donde estabas.

### Corregido

- **Flechas ↑/↓ se saltaban un párrafo entero al llegar a una de sus líneas envueltas** (issue #3 en GitHub): el arreglo anterior del mismo bug (saltos de varios bloques por culpa de separadores colapsados) calculaba el movimiento SIEMPRE por línea lógica del documento, sin distinguir un párrafo envuelto en varias filas visuales de un cambio real de bloque -- así que bajar dentro de un párrafo largo se comía el resto del párrafo entero de un solo golpe. Ahora es un enfoque híbrido: se prueba primero el movimiento nativo de CodeMirror (fiable DENTRO de un mismo párrafo envuelto) y solo se sustituye por el cálculo propio, línea a línea, cuando el movimiento nativo decide saltar a un párrafo distinto (que es donde el cálculo en píxeles se desmadra con alturas muy dispares entre bloques).
- **Clic mal resuelto justo debajo de una línea horizontal** (issue #4 en GitHub, confirmado que seguía pasando específicamente ahí): el cálculo interno de CodeMirror por coordenadas (`posAtCoords`) no siempre acierta la posición cerca de un widget de bloque como el de la línea horizontal. En vez de intentar reproducir ese cálculo, tras cada clic simple (con la selección ya colapsada, nunca si hubo arrastre) se compara con lo que dice la API del navegador pensada para esto (`caretPositionFromPoint`/`caretRangeFromPoint`, que hace su propio hit-test sobre el DOM ya renderizado) y se corrige si no coincide.
- **"Nueva subpágina" en el menú contextual del árbol**: clic derecho sobre cualquier página (una sola, no con selección múltiple -- sería ambiguo bajo cuál crearla) para crear una nota hija de ella directamente. El `+` de la barra lateral pasa a crear **siempre** en la raíz, sin importar qué página esté abierta -- antes creaba como hija de la nota abierta, ambiguo con esta opción nueva.

### Corregido

- `Page.updated_at` no se actualizaba al guardar solo el cuerpo de una nota (título/icono/descripción sin cambios): el cuerpo vive en `page_content`, una tabla aparte, así que la `Page` en sí no quedaba "sucia" para que su `onupdate` disparase. Afectaba al orden de "Páginas recientes" del panel -- una página editada hace un minuto podía aparecer como si no se hubiera tocado en días. Ahora se toca `updated_at` explícitamente también al guardar el cuerpo.

### Corregido (grave)

- Un wikilink o enlace Markdown con un salto de línea en medio del texto (p. ej. `[[Título\ncon salto]]`, o un `[texto\n](url)` con un Enter sin querer entre corchetes) tiraba abajo el editor entero al abrir la página: `Decoration.replace()` que cruza un salto de línea no se admite desde un `ViewPlugin` (solo desde un `StateField`, como ya pasaba con la línea horizontal), y CodeMirror lo rechaza con una excepción sin capturar -- el editor nunca llegaba a montarse, así que la página se veía completamente vacía aunque el contenido siguiera intacto en la base de datos. Tanto `buildWikilinkDecorations` como `buildMarkdownLinkDecorations` ahora comprueban si el texto igualado tiene un `"\n"` y, si lo tiene, se quedan siempre en su forma cruda/editable en vez de intentar sustituirlo por el widget "bonito" -- más feo en ese caso concreto, pero no vuelve a romper nada.

### Añadido

- **Editor con más pinta de Notion**: la prosa del editor pasa de fuente monoespaciada a la misma familia sans-serif del resto del panel (`--font-body`, la fuente del sistema en cada SO) -- el código (en línea y de bloque) se queda explícitamente en `--font-mono`, para que siga leyéndose como código.
- **Jerarquía de encabezados más marcada**: H1 pasa de 1.7em a 1.9em, H2 de 1.4em a 1.5em, H3 de 1.15em a 1.25em -- más diferencia real entre un título y un párrafo suelto, como en Notion.
- **Separación real entre bloques**: cada bloque lleva ahora un hueco por encima según su tipo (un H1 deja mucho más aire que un párrafo suelto; los ítems de una misma lista quedan juntos, sin hueco extra entre ellos) -- antes el único "espacio" entre bloques era la línea en blanco colapsada a ~5px, sin distinguir tipos. Viable sin miedo a que las flechas ↑/↓ vuelvan a saltarse bloques porque ya no dependen de la altura en píxeles de cada línea (ver más abajo).
- **Asa de bloque más grande y solo visible al pasar el ratón**: el `+`/`⋮⋮` de cada bloque pasa de 14px a 18px y de estar siempre visible (tenue) a invisible por defecto, apareciendo solo al pasar el ratón por esa línea -- salvo que el bloque esté seleccionado, que se queda visible igual (`:has()` sobre el propio asa).

### Corregido

- Vista previa de un wikilink al pasar el ratón, duplicada y una copia imposible de cerrar salvo recargando (issue [#1](../../issues/1)): un clic sin `Ctrl` sobre un wikilink resuelto lo sustituye por su marcado en crudo justo bajo el propio ratón, lo que el navegador interpreta como un `mouseover` nuevo sin que el hover anterior se haya cerrado -- `startLinkPreview()` creaba la tarjeta nueva sin quitar la que ya hubiera, dejándola huérfana en el DOM. Ahora cierra cualquier tarjeta previa antes de crear una.
- Las flechas `↑`/`↓` podían saltarse varios bloques de golpe -- en un caso llegaba a comerse casi toda una nota corta de un solo toque (issue [#2](../../issues/2)): CodeMirror calcula a qué línea saltar por distancia en píxeles, y con varias líneas colapsadas a ~5px seguidas (separadores entre bloques, línea horizontal) esa distancia abarcaba de sobra varias líneas colapsadas a la vez. Sustituidas por una navegación propia que se mueve por líneas *lógicas* del documento (actual ± 1) en vez de por píxeles, con memoria de la columna "objetivo" entre pulsaciones consecutivas (como cualquier editor de texto normal) -- inmune a cualquier cosa que colapse o esconda contenido, ahora o en el futuro.

## [0.10.1] - 2026-10-29

### Corregido

- **El autoguardado normal podía pisar el texto al que se había vuelto** (la misma clase de pérdida silenciosa, por el otro camino): si se escribía algo, el autoguardado de 2 s salía y su respuesta tardaba, y antes de que contestara el usuario deshacía, volvía al texto que ya estaba guardado y ocultaba la pestaña, el guardado de emergencia daba ese contenido por guardado -- pero el PATCH viejo, con el texto abandonado, se aplicaba después en el servidor, y al cerrar ya no se reenviaba nada: el servidor se quedaba con el borrador, el indicador decía «Guardado» y el último texto del usuario se perdía en silencio. Ahora el guardado de emergencia mira también qué cuerpo lleva el autoguardado normal en vuelo: si es otro, no da nada por guardado y reenvía lo de la pantalla en cuanto contesta. Al cerrar de verdad, sin tiempo para esperar, se manda lo de la pantalla igualmente pero sin darlo por guardado, así que si la pestaña sobrevive (el cierre no llega a ocurrir) el autoguardado de 2 s lo reenvía. Y el indicador ya no dice «Guardado» mientras quede algo pendiente.

## [0.10.0] - 2026-10-29

### Corregido

- **Un fallo de red o un error de la API no se veían**: si el árbol, una página, una database o la búsqueda devolvían un 500, o se caía la conexión, el panel se quedaba en blanco o con contenido viejo y el único rastro era un mensaje en la consola del navegador. Ahora sale un aviso discreto abajo a la derecha, que se apila si llegan varios (el mismo mensaje no se repite y nunca hay más de tres a la vez), se descarta con un clic y desaparece solo a los 6 segundos; los fallos de conexión se cuentan como "Sin conexión con la API" en lugar de enseñar el texto crudo del navegador. Vale también para el borrado de páginas, para crear una nota y para borrar una base de datos, que hasta ahora solo escribían en la consola.
- **Los últimos dos segundos de una nota se perdían al cerrar la pestaña**: el guardado de emergencia era una petición normal y el navegador la cancela en cuanto empieza a descargar la página. Ahora esa petición va con `keepalive`, así que termina aunque la pestaña se cierre, y el guardado se dispara también al pasar la pestaña a segundo plano (cambiar de app en el móvil, minimizar, bloquear la pantalla), que es cuando el cierre de pestaña no llega a avisar. Si no hay cambios pendientes, o ya se guardó exactamente ese mismo contenido, no manda nada. Al pasar a segundo plano la pestaña sigue viva, así que ahí ese guardado solo se da por bueno cuando el servidor lo acepta (2xx): si falla (un 500, un corte de red), los cambios se quedan pendientes y el autoguardado normal de 2 s los reintenta, en vez de darlos por perdidos en silencio.
- **Dos guardados de emergencia solapados podían pisarse**: si la pestaña se ocultaba dos veces con textos distintos antes de que contestara el primero, los dos PATCH iban a la vez y podían aplicarse en el servidor al revés (el viejo el último), de modo que el servidor se quedaba con el texto anterior mientras la pantalla mostraba el nuevo y el indicador decía "Guardado"; al cerrar ya no se reenviaba nada, así que el último texto se perdía en silencio. Ahora, con la pestaña viva, solo hay una escritura de emergencia en vuelo: lo que se escriba mientras tanto espera a que conteste la anterior y se reenvía justo después. Además, una confirmación ya no da por guardado un contenido que no es el del editor (respuesta vieja detrás de una escritura más nueva): los cambios se quedan pendientes y se reenvía lo que hay en pantalla. Al cerrar de verdad, cuando no hay tiempo para esperar, se manda igualmente lo último escrito. Y si el usuario vuelve justo al texto que ya estaba confirmado mientras un guardado de emergencia va de camino, al contestar este se reenvía lo que hay en pantalla en vez de darlo por guardado (antes el servidor se quedaba con el borrador abandonado, el indicador se quedaba en «Cambios sin guardar» para siempre y al cerrar no se reenviaba nada).

## [0.9.3] - 2026-10-29

### Corregido

- **Las horas que devuelve la API se leían como hora local**: se serializaban sin sufijo de zona (`2026-10-29T10:00:00`), y JavaScript interpreta una fecha así como la hora del equipo que mira, de modo que el "hace X" del panel salía desfasado una o dos horas según dónde estuviera el navegador. Ahora todas las fechas que sirve la API (`Page`, databases, filas y dashboard) llevan su offset UTC explícito (`+00:00`). No cambia nada de lo que hay guardado en la base de datos.
- **Borrar una base de datos desde el árbol no borraba sus filas**: `DELETE /pages/{id}` sobre el nodo de una database eliminaba la página y, por cascada, la fila que la describía, pero las filas de datos se quedaban en la tabla con la referencia vacía -- invisibles en el árbol y en cualquier listado, e imposibles de recuperar desde la interfaz. El borrado del contenido pasa a un helper único que usan tanto `DELETE /databases/{id}` como `DELETE /pages/{id}`, así que los dos caminos dejan exactamente el mismo estado.
- **La subida de la imagen de cabecera se fiaba del nombre y del tipo declarado**: bastaba con subir un `.svg` o un `.html` diciendo que era `image/png` para que se guardara como imagen y se sirviera desde `/files` en el mismo origen que el panel -- una vía directa a ejecutar scripts con la sesión abierta. Ahora el formato se detecta por su firma binaria (PNG, JPEG, GIF o WebP; cualquier otra cosa se rechaza con un 415) y la extensión sale del formato detectado, nunca del nombre del archivo. Además `/files` se sirve con `nosniff` y `Content-Disposition`: solo esas cuatro extensiones se muestran en el navegador, todo lo demás se descarga.

## [0.9.2] — Imagen de `api` autocontenida (lista para Proxmox)

### Corregido

- `backend/Dockerfile` solo copiaba `backend/`; `frontend/` llegaba al contenedor exclusivamente por el bind-mount de desarrollo (`./frontend:/app/frontend:ro` en `docker-compose.yml`), que no existe fuera de esta máquina. Fuera de un entorno con ese bind-mount puesto (p. ej. un despliegue en Proxmox sin bind-mounts), `/app/frontend` se creaba vacío y el panel no se servía. El contexto de build de `api` pasa a ser la raíz del repo (`context: .`, no `./backend`) para que el Dockerfile pueda copiar `frontend/` (incluido `vendor/editor.bundle.js`, ya compilado) dentro de la imagen. En desarrollo no cambia nada — los bind-mounts se superponen a lo ya copiado, para seguir con recarga en caliente sin reconstruir.
- Añadido `.dockerignore` en la raíz (antes no hacía falta, el contexto era solo `backend/`): excluye `node_modules/` (incluido `frontend/editor-src/node_modules/`, ~15 MB que no pintan nada en la imagen), `__pycache__/`, `.venv/`, adjuntos/backups locales y `.env`.

### Verificado

- Build de la imagen con el contexto nuevo: `/app/frontend` dentro de la imagen trae `index.html`/`app.js`/`logo.png`/`vendor/editor.bundle.js` sin `node_modules`.
- Contenedor de prueba levantado desde la imagen SIN ningún bind-mount (`docker run`, no `docker compose`), conectado solo por red al `db` ya existente: `/`, `/health` y `/vendor/editor.bundle.js` responden 200 con el contenido real — simula exactamente el caso de Proxmox.
- Stack de desarrollo (`docker compose up -d --build`) reconstruido y probado de nuevo tras el cambio: panel, dashboard y hot-reload del frontend sin cambios de comportamiento.

## [0.9.0] — Editor al estilo Notion: comandos, iconos reales, autoformato

### Añadido

- **Menú `/` (comando de bloque)**: escribir `/` al principio de un bloque vacío abre un menú flotante con los tipos de bloque disponibles (párrafo `p`, títulos `h1`-`h5`, lista `list`, lista numerada `num`, código `code`, línea horizontal `horizontal`). Se filtra en vivo por lo que se escribe después de la `/` (prefijo de la abreviatura o substring de la etiqueta), se navega con `↑`/`↓`, se confirma con `Enter` o clic, se cancela con `Escape` dejando el texto tal cual.
- **Línea horizontal** (`---`) como bloque nuevo: seleccionable/convertible igual que el resto, se renderiza como una regla visual cuando el cursor no está en esa línea y vuelve a mostrarse como markdown editable al entrar en ella (mismo patrón cursor-aware que wikilinks y flechas).
- **`Mayús+Enter` crea un bloque nuevo**; `Enter` a solas sigue bajando de línea dentro del mismo bloque (comportamiento sin cambios, ahora explícitamente distinto del anterior).
- **Selector de emoji real** para el icono de página (`openEmojiPicker()` en `app.js`): rejilla con buscador por palabra clave, sustituye al campo de texto que había antes.
- **Autoformato tipográfico**: `->`, `<-`, `<->` se muestran como `→`, `←`, `↔` (igual que Obsidian/Notion) salvo dentro de un bloque de código o mientras el cursor está sobre ellos, momento en que vuelven a su forma editable en texto plano.
- **Wikilinks con aspecto de enlace normal**: un `[[Página]]` resuelto oculta los corchetes y se ve como un enlace de verdad, con una tarjeta de vista previa al pasar el ratón (contenido real de la página destino, con debounce de 350ms). Sigue siendo editable en markdown en cuanto el cursor entra en él.
- Logo de la app (`public/MDMLogo.png`, copiado a `frontend/logo.png`) como favicon y en la cabecera del panel.
- Indicador de guardado con icono de progreso (girando mientras `Guardando…`, marca al terminar) movido a la parte superior de la nota, junto con los backlinks ahora colapsados en un desplegable arriba (antes listados al final de la página).
- El cuerpo del editor ocupa todo el ancho de la nota, sin caja/borde propio — se siente como la propia página, no como un widget dentro de ella.

### Quitado

- Botón "Borrar página": redundante con el borrado por clic derecho ya disponible en el árbol de la barra lateral.

### Corregido

- Quitar la imagen de cabecera y subir otra inmediatamente después seguía mostrando la antigua hasta recargar: el mount `/files` no llevaba `Cache-Control: no-cache` (solo lo llevaban `app.js`/`index.html` desde la 0.6.0) — mismo nombre de archivo fijo por página (`header.<ext>`), así que el navegador la servía de caché sin revalidar. Añadido el mismo header ahí, más un `?t=timestamp` en el cliente para invalidar de raíz.
- La tarjeta de vista previa de un wikilink se quedaba en pantalla si se navegaba a otra página justo después de que apareciera (el hover no se limpiaba al cambiar de vista). `clearMain()` ahora también cierra cualquier preview y cualquier menú `/` abierto.
- Un wikilink a una página de tipo `database` (p. ej. `[[Tareas]]`) navegaba a una página inexistente: la navegación siempre llamaba a "abrir nota", nunca a "abrir database" — mismo tipo de bug que el de la 0.6.0 (dos UUID distintos para lo mismo, `Page.id` vs `DatabaseDef.id`). Nuevo `navigateToPage()` en `app.js` decide cuál de las dos según el tipo de nodo antes de navegar.
- Menú contextual de bloque: al abrirlo sobre un único bloque, ahora muestra su tipo actual (p. ej. "Título 1") como cabecera, antes del listado "Convertir a" — antes no había forma de saber qué tipo era el bloque seleccionado sin probar a convertirlo.

### Corregido / aprendido (CodeMirror)

- `Decoration.replace({..., block: true})` (necesaria para la línea horizontal) solo puede venir de un `StateField`, nunca de un `ViewPlugin` — CodeMirror lo rechaza con "Block decorations may not be specified via plugins". El plugin de la línea horizontal se reescribió como `StateField` (`hrField`).
- El menú `/` necesita `view.coordsAtPos()` para posicionarse junto al cursor, pero leer el layout del DOM de forma síncrona dentro de `ViewPlugin.update()` está prohibido ("Reading the editor layout isn't allowed during an update"). Probado primero con `requestAnimationFrame`: funciona en un navegador normal, pero no es la mejor opción — un frame de composición es más de lo que hace falta (solo hay que salir de la pila de la transacción en curso, no esperar a pintar). Cambiado a `setTimeout(fn, 0)`, que además es más robusto en pestañas en segundo plano.

## [0.8.0] — Edición por bloques + selección múltiple

### Añadido

- **Bloques en el editor**: cada párrafo/`H1`-`H4`/ítem de lista/bloque de código se calcula a partir del árbol de sintaxis de `@lezer/markdown` (`computeBlocks()` en `entry.js`) — no es un modelo de datos nuevo, `body_markdown` sigue siendo el mismo string de siempre. Cada bloque tiene un asa `⋮⋮` (widget posicionado en el margen izquierdo de su línea, no un gutter — ver la nota técnica más abajo).
  - Clic: selecciona el bloque. `Mayús`+clic: selecciona un rango. `Ctrl`/`Cmd`+clic: añade/quita uno suelto.
  - Clic derecho: menú con **Convertir a** (párrafo, H1-H4, lista, lista numerada, código) y **Eliminar**, aplicado a todos los bloques seleccionados a la vez.
- **Selección múltiple de páginas** en el árbol de la barra lateral: mismos atajos (`Ctrl`/`Mayús`+clic), clic derecho para eliminar varias de una vez. Distingue página `note` (`DELETE /pages/{id}`) de página `database` (`DELETE /databases/{database_id}`) automáticamente.
- Menú contextual genérico (`showContextMenu()` en `app.js`) compartido entre bloques y páginas.

### Corregido / aprendido

- La primera implementación de las asas de bloque usaba un `gutter()` de CodeMirror, igual que `lineNumbers()`. No se alineaba con el contenido: se comprobó que ni siquiera el propio `lineNumbers()` nativo alineaba bien contra líneas de altura variable (los encabezados son más altos que un párrafo normal) — los gutters de CM6 asumen altura de línea uniforme. Solución: el asa es un `Decoration.widget` colocado dentro de la propia línea y sacado al margen con `position: absolute` — al ser hijo real de esa línea, hereda su altura exacta sin cálculo aparte.
- Verificado con medidas reales (`getBoundingClientRect`), no solo inspección visual: con el gutter, el desfase entre el asa y su línea llegaba a acumular >60px en documentos con encabezados; con el widget, alineación exacta (diferencia 0px) en los 6 bloques de un documento de prueba con encabezado, párrafo, lista y código.

### Documentado

- `docs/ARCHITECTURE.md`: comparación completa entre "bloques de verdad" (modelo de datos nuevo) y "bloques calculados" (el enfoque elegido), y por qué.

## [0.7.0] — Metadatos de página, autoguardado, caret visible

### Añadido

- `page` gana tres columnas: `created_by` (se fija solo al crear, pensado para cuando haya espacios grupales/multiusuario), `description` y `header_image_path` — todas opcionales a nivel de base de datos; la API siempre rellena `icon`/`created_by` al crear una página, así que a nivel de producto son "obligatorias" sin forzar una migración de backfill sobre datos existentes.
- `POST/DELETE /pages/{id}/header-image`: sube (`multipart/form-data`) o quita la imagen de cabecera de una página; valida tipo de contenido y tamaño (8 MB máx.), sustituye cualquier cabecera anterior en vez de acumular archivos.
- `/files/...`: monta `/app/attachments` como estáticos para servir lo subido.
- Panel: icono editable (clic sobre el emoji), campo de descripción, imagen de cabecera con subida/borrado, línea "Creado por X" en la nota.
- **Autoguardado**: sin botón "Guardar" — cualquier cambio (título, icono, descripción, cuerpo) programa un guardado 2s después de la última pulsación; si llegan más cambios antes, se reinicia el plazo. Al navegar a otra página o cerrar la pestaña, cualquier guardado pendiente se fuerza antes (no se pierden los últimos <2s de cambios). `Ctrl/Cmd+S` sigue disponible para forzarlo al momento.
- El árbol de páginas ahora muestra el icono real de cada página (antes siempre el genérico 📄/🗄, ignorando `icon`).

### Corregido

- El caret del editor de notas se veía negro en tema oscuro: CodeMirror no dibuja un cursor propio a menos que se incluya la extensión `drawSelection()` (no la teníamos), así que el cursor real era el nativo del navegador — y ese sigue `caret-color`, no las reglas `.cm-cursor` que habíamos escrito. Añadido `caret-color: var(--ink)` en `#note-body .cm-content`.

### Config

- `DEFAULT_USER_NAME` (`.env`): nombre que se asigna como creador de cada página nueva mientras no haya login. Hay que recrear el contenedor `api` (`docker compose up -d api`) tras cambiarlo — las variables de entorno no se recogen con el hot-reload de `--reload`, solo al crear el contenedor.

## [0.6.0] — Editor con formato en vivo + correcciones de navegación

### Corregido

- **Bug real:** el árbol (`GET /pages/tree`) devolvía el id de la `page` para los nodos de tipo `database`, pero el frontend necesitaba el id de la fila `databases` (son dos UUID distintos) para pedir `/databases/{id}`. Cada clic en una base de datos desde la barra lateral o desde resultados de búsqueda fallaba con un 404 silencioso — causa probable de "no consigo ver las bases de datos". `GET /pages/tree` ahora incluye `database_id` en cada nodo de tipo `database`; el frontend lo usa para navegar.
- Los archivos estáticos (`app.js`, `index.html`, ahora también el editor) no llevaban cabecera de caché, así que el navegador podía servir una versión vieja sin avisar. `NoCacheStaticFiles` (en `main.py`) añade `Cache-Control: no-cache` — sigue siendo barato (revalida por ETag) pero nunca sirve una copia obsoleta sin preguntar.

### Añadido

- **Editor de notas con formato en vivo** (CodeMirror 6): un único cuadro para escribir y ver el resultado, no cuadro de texto + panel de vista previa separados. `#`/`##`/`###`, `**negrita**`, `*cursiva*` y `[[wikilinks]]` se renderizan al momento; los marcadores de sintaxis quedan visibles pero pequeños y discretos justo donde se escribieron. `Ctrl/Cmd+clic` en un wikilink resuelto navega; `Ctrl/Cmd+S` guarda.
- `frontend/editor-src/`: fuente del editor (paquetes `@codemirror/*` + `@lezer/highlight`), compilado una vez con esbuild a `frontend/vendor/editor.bundle.js` (vendorizado, commiteado, no depende de ningún CDN en tiempo de ejecución). Única pieza del frontend con build — el resto sigue siendo HTML/JS plano.
- `frontend/app.js` pasa a cargarse como módulo ES (`<script type="module">`) para poder importar el bundle del editor.

### Decisión

- Se planteó migrar todo el frontend a React + Vite. Se descartó por ahora: el problema real (que el editor fuera bueno) lo resuelve una librería de edición dedicada, no el framework — React no habría simplificado la gestión de cursor/selección del editor. Se optó por el punto intermedio: vanilla JS sin build para el resto de la app, CodeMirror vendorizado solo para el editor. Revisable si el resto de la interfaz (diálogos, listas, formularios) empieza a pesar demasiado para mantenerse a mano.

## [0.5.0] — Fase 3: plantillas de CRM y Tareas

### Añadido

- `backend/app/templates.py`: plantillas `crm` ("Contactos": empresa/fase/valor/próximo contacto) y `tasks` ("Tareas": estado/prioridad/fecha límite/proyecto), cada una con dos vistas por defecto (`board` agrupado + `table`). Son datos, no motor nuevo — pasan por las mismas operaciones de creación de la Fase 2.
- `GET /databases/templates`: lista las plantillas disponibles.
- `POST /databases/from-template`: crea database + schema + vistas en una sola llamada (`title` opcional, si no se pasa usa el de la plantilla).
- Panel visual: botones **CRM** / **Tareas** de un clic junto a "Bases de datos". Título de la database ahora editable (antes solo se fijaba al crearla) — hacía falta para poder renombrar lo que sale de una plantilla.

### Verificado

- De extremo a extremo en el navegador: clic en **CRM** → database con 4 propiedades y 2 vistas ya creadas, tablero con las 5 columnas de `fase` a 0; clic en **Tareas** → mismo patrón con su propio schema. Renombrado de título probado (persiste y se refleja en el árbol). `POST /databases/from-template` con una plantilla inexistente devuelve 404.

## [0.4.0] — Panel visual (adelanto de Fase 4)

### Añadido

- `frontend/index.html` + `frontend/app.js`: interfaz completa sobre las Fases 0–2, sin build ni dependencias (fetch directo a la API del propio origen).
- Árbol de páginas y bases de datos en la barra lateral, con búsqueda (`/search`) con debounce.
- Editor de notas: título, cuerpo Markdown, vista previa con `[[wikilinks]]` resueltos en vivo contra el árbol cargado en cliente, y lista de backlinks navegable.
- Diálogo de creación de `database`: propiedades dinámicas (clave/nombre/tipo, opciones para `select`/`multiselect`).
- Vista de `database`: pestañas por vista (`table`/`board`/`calendar`/`list`), tabla o tablero según el tipo activo, formulario de nueva fila que genera el input correcto por tipo de propiedad.
- `GET /pages/tree` ahora excluye `type=database_row`: las filas se ven dentro de su database, no como nodos sueltos del árbol de navegación.

### Verificado

- De extremo a extremo en el navegador (no solo `curl`): nota con wikilink sin resolver → creación de la página destino → backlink real tras volver a guardar; CRM completo (3 filas, vista board agrupada por Fase con conteos correctos por columna).

## [0.3.0] — Fase 2: motor de `database` + `view`

### Añadido

- `POST/GET/PATCH/DELETE /databases`: una `database` es una página + `schema_def` (lista de propiedades tipadas: `text`, `number`, `select`, `multiselect`, `date`, `checkbox`, `relation`, `url`).
- `properties.py`: valida `properties` de una fila contra el `schema_def` de su database (claves conocidas, tipo básico, opciones de `select`/`multiselect`) — 400 con mensaje claro si no cumple.
- `POST/GET/PATCH/DELETE /databases/{id}/rows`: filas (`Page` con `type=database_row`). `PATCH` hace merge parcial de `properties`, no reemplazo completo.
- `POST/GET/PATCH/DELETE /databases/{id}/views`: vistas (`table`/`board`/`calendar`/`list`) con `config` de filtros + orden, aplicados por el backend en `GET /rows?view={id}`; `group_by` y `visible_properties` quedan como metadato para el frontend.
- Verificado creando dos dominios reales con el mismo motor y cero código específico: un CRM ("Contactos": empresa/fase/valor) y un gestor de tareas ("Tareas": estado/prioridad/hecha), incluida una vista board filtrada y ordenada.

### Corregido

- `DatabaseDef` no tenía la relación ORM hacia `Page` (`db_def.page` no existía) — provocaba un 500 en `POST /databases` después de haber confirmado la escritura en base de datos (el commit ya había ocurrido; solo fallaba el serializado de la respuesta), dejando páginas de database huérfanas sin que el cliente lo supiera. Al añadir la relación hubo que desambiguar con `foreign_keys=[page_id]` porque `pages` y `databases` se referencian mutuamente.

## [0.2.0] — Fase 1: notas/wiki

### Añadido

- Alembic: migraciones versionadas, con `alembic upgrade head` en el arranque del contenedor `api`. Sustituye al `create_all()` de la Fase 0.
- `GET /pages/tree`: árbol de páginas por `parent_id`.
- Wikilinks `[[Título]]` / `[[Título|Alias]]`: se resuelven al guardar (`app/wikilinks.py`) y generan `Link` reales entre páginas.
- `GET /pages/{id}/backlinks`: páginas que enlazan a una página dada.
- `GET /search?q=`: búsqueda full-text (Postgres `tsvector`, configuración `spanish`) sobre título + cuerpo.

### Corregido

- La migración inicial autogenerada por Alembic fallaba al aplicarse: `pages` y `databases` se referencian mutuamente y autogenerate no resuelve ese ciclo por sí solo. Se separó la FK circular (`databases.page_id -> pages.id`) en un `create_foreign_key` aparte, después de crear ambas tablas.

## [0.1.1] — Panel de estado

### Añadido

- Panel de estado (`frontend/index.html`), servido por la propia API en `/`, con sondeo cada 4s del nuevo endpoint `GET /status` (comprueba `api` y conectividad real a `db`).

## [0.1.0] — Fase 0: esqueleto

### Añadido

- Docker Compose con `api` (FastAPI + Uvicorn) y `db` (Postgres 16).
- Modelo de datos completo: `workspace`, `page`, `page_content`, `databases`, `view`, `link`, `tag`, `attachment`.
- CRUD de `/pages` (GET, POST, PATCH, DELETE) probado de extremo a extremo.
- Documentación de arquitectura con diagramas ([`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)).
