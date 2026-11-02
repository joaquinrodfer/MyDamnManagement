# Medidas de P16 — arranque en segundo plano y `/pages` con 12.000 notas

Fecha: 2026-11-02. Rama `feat/escala` (antes: `release/v1.0`, `9a1bee5`).

Todo lo de aquí está medido en esta máquina, no estimado. Dónde se midió y con
qué, primero, porque cambia la lectura de los números:

- WSL2, 16 vCPU, 7.8 GB de RAM, **sistema de ficheros `overlay`** para el vault
  y el índice (`/var/tmp/p16`). No se usó el montaje `9p` de Windows porque ahí
  cada `open` es una ida y vuelta al host: el mismo vault tarda 48 s en
  escribirse y el reindexado no termina ni en 7 minutos. Producción usa un
  volumen de Docker, que se parece más al `overlay`.
- Vault generado con un script (`gen_vault.py`, no commiteado): **12.000 notas**
  de ~1,2 KB en 40 carpetas, con `frontmatter`, dos `[[wikilinks]]` por nota y
  fechas. 14,0 MB de `.md`, índice resultante de 35,3 MB.
- Un worker de `uvicorn`, como en `docker-compose.prod.yml`, con
  `WATCH_ENABLED=0` (el vigilante no tiene nada que ver con el arranque y sí
  mete ruido en las medidas).

## 1. Arranque en frío (índice de cero, 12.000 notas)

| Medida | Antes (`release/v1.0`) | Después (`feat/escala`) |
|---|---|---|
| Primera respuesta de `GET /health` | **249,3 s** (el socket no acepta hasta terminar) | **4,5 s** |
| Qué contesta en ese momento | nada: `ECONNREFUSED` | `200 {"status":"starting","progress":{"phase":"comprobando el vault",…}}` |
| `GET /health` diciendo `ok` (índice completo) | 249,3 s | 276,4 s |
| `GET /pages` **durante** el indexado | `ECONNREFUSED` | `503` con `detail` y `progress` (222 B, 2 ms), `Retry-After: 5` |
| `GET /pages/tree` durante el indexado | `ECONNREFUSED` | `503` igual (222 B, 2 ms) |
| RSS del proceso al terminar | 145,8 MB | 123,3 MB |
| `healthcheck` del compose | falla (`no responde en /health`) | `indexando: …` → 0 |

El total de indexado no baja (hace el mismo trabajo, ahora sin bloquear): 249 s
antes y 271-276 s después, dentro de la varianza de esta máquina. La diferencia
entre esas dos cifras **no** es del cambio: el reindexado es I/O y CPU y las
medidas están tomadas en momentos distintos con la máquina cargada de otras
cosas. Lo que sí es del cambio es *cuándo* se puede hablar con el servidor.

Repetido con el índice ya construido (reinicio del contenedor con el volumen de
datos en su sitio): primera respuesta a **4,64 s** y `ok` a **272,6 s**. Es el
mismo comportamiento que en frío porque el arranque **sigue reindexando en cada
arranque** (ADR-0001 D2: los ficheros son la verdad; un proceso que muere entre
escribir el `.md` y actualizar el índice dejaría el índice sirviendo el cuerpo
viejo). Lo que cambia con P16 es que ese trabajo ya no tiene al servidor mudo.

### Dónde se va el tiempo (perfil del reindexado)

Fases que publica `/health` mientras indexa (una muestra por segundo):

| Fase | Empieza en | Comentario |
|---|---|---|
| `comprobando el vault` | 5,1 s | la comprobación rápida del arranque |
| `leyendo el vault` | 6,1 s | 12.001 ficheros, ~2 s |
| `escribiendo el índice` | 8,1 s | abre la transacción del volcado |
| `enlazando` | 42,4 s | **el 85 % del tiempo**: `sync_wikilinks` de 24.000 enlaces |
| `ok` | 276,4 s | |

Ese reparto no lo cambia P16 (no se ha tocado el reindexado): es la deuda que
queda a la vista. El enlazado es lo que hace que 12.000 notas tarden minutos, no
la lectura de los `.md`.

Ojo al leer «empieza en»: son marcas de reloj de esta ejecución, no cortes
exactos de fase, y la máquina estaba cargada de otras cosas (en la ronda 2 el
volcado acabó a los 35 s y el enlazado empezó ahí). El **porcentaje** que
publica `/health` mientras tanto se mide en la sección 3.

## 2. `/pages` y `/pages/tree` con el índice ya construido

| Ruta | Bytes | Servidor | Navegador |
|---|---|---|---|
| `GET /pages` (todas las páginas **con cuerpo**) | 16.416.895 (16,4 MB) | 0,46 s | no lo usa el panel |
| `GET /pages/tree` (id, título, tipo, icono, padre) | 1.740.001 (1,7 MB) | 0,19 s | 356 ms de descarga |

Las dos rutas se han medido dos veces (0,46 s y 0,19 s en la tabla; 1,39 s y
0,15 s en la primera pasada) y **ninguna se ha tocado en P16**: la diferencia
entre pasadas es varianza de esta máquina, no una mejora. En la ronda 2, con el
mismo vault: `/pages` 16.417.250 B / 0,45 s y `/pages/tree` 1.740.140 B / 0,15 s,
12.001 elementos cada una.

El panel **no pide `/pages`**: desde antes de P16 dibuja el árbol con
`/pages/tree` (`frontend/app.js`, `loadTree()`), que ya devuelve solo lo que
necesita. `/pages` sigue devolviendo el cuerpo de todas las páginas para quien
quiera el detalle (scripts, exportaciones), y no lo usa nadie en la interfaz.

### Qué pasa en el navegador

Medido con Chrome (CDP) sobre el panel real: se entra, se navega a `/` y un
`MutationObserver` + `requestAnimationFrame` marcan cuándo están las filas del
árbol en el DOM y cuándo se ha pintado.

| | 12.001 nodos en el árbol | 12 nodos (control) |
|---|---|---|
| `DOMContentLoaded` | 2.261 ms | 1.092 ms |
| `GET /pages/tree` (inicio · duración) | 3.021 ms · **356 ms** | 1.811 ms · 288 ms |
| Filas en el DOM | 3.732,8 ms | 2.100 ms |
| **Árbol pintado** (rAF después) | **3.733,4 ms** | ~2.100 ms |

Lectura, y hay que separar dos cosas que se suman en la misma cifra:

- **Carga del panel** (su `app.js` y el del editor, servidos desde el `9p` de
  Windows en esta máquina): 2,3 s con 12.001 notas y 1,1 s con 12. No depende
  del número de notas — depende del montaje y de la máquina —, y es la mitad
  larga del total.
- **Lo que cuesta tener 12.000 notas**, medido entre las dos ejecuciones:
  la petición del árbol pasa de 288 ms a 356 ms (1,7 MB frente a 2 KB) y, desde
  que llega la respuesta hasta que las 12.000 filas están en el DOM, ~0,35 s
  (con 12 filas es inapreciable). En total, **entre 0,4 y 0,7 s** atribuibles a
  las notas; el resto de la diferencia entre 3,7 s y 2,1 s es que la carga del
  panel salió más lenta en la ejecución grande.

`renderTreeNode` construye el subárbol entero, esté plegado o no: con 12.000
notas hay 12.000 `<div class="tree-row">` en el DOM y otros tantos nodos
alrededor. Eso es lo que no escala, y es interfaz, no API.

### Decisión: **no se pagina `/pages` ni el árbol**

- La respuesta del árbol son **1,7 MB**, no "unos pocos MB"; el endpoint ligero
  que la tarjeta proponía como salida ya existe y es el que usa el panel.
- El tiempo hasta pintar (3,7 s) supera los 2 s de la tarjeta, pero **paginar no
  lo arregla**: de esos 3,7 s, 2,3 s son la carga del panel (igual con 12 notas)
  y lo atribuible a las 12.000 notas son 0,4-0,7 s, de los que 0,36 s son la
  descarga del JSON. Paginar el árbol obligaría a reescribir la navegación
  entera (mover una nota a una rama que aún no se ha cargado, plegar/desplegar,
  el buscador del árbol, el arrastrar y soltar) para ahorrar una fracción de
  segundo.
- Lo que sí arreglaría el renderizado es virtualizar las filas o pintar solo
  las ramas desplegadas, y eso es un cambio del panel, no de la API. Queda como
  deuda anotada, no como parte de P16: la tarjeta pedía medir antes de tocar, y
  la medida dice que el problema no está en `/pages`.

Por tanto: `/pages` se queda como está (paginarlo rompería el contrato con los
scripts y no ahorra nada al panel) y `/pages/tree` también.

## 3. El porcentaje de `/health` mientras indexa (ronda 2)

La primera versión publicaba el progreso de la escritura como
`progress("escribiendo el índice", total, total)` **antes** de abrir la
transacción del volcado, así que `percent` valía **100 desde el primer
segundo** y el número caía a 0 al pasar a `enlazando`. Reproducido en la
revisión con 3.000 notas: 100 % de t=6,7 s a t=13,5 s de un indexado que
terminaba a t=21,3 s, y luego 7 %. Con 12.000 notas el 100 % duraba los ~150 s
del indexado y el `healthcheck` del compose lo imprimía tal cual
(`indexando: escribiendo el índice (100%), 1.1 s en marcha`).

Ahora el porcentaje es del reindexado **entero**, no de la fase: cada página se
toca en cuatro pasadas —leer, preparar, escribir y enlazar— y cada una ocupa un
cuarto de la barra (`done`/`total` van en unidades de trabajo, cuatro por
página). El volcado se hace a lotes de `PROGRESS_EVERY` dentro de la **misma**
transacción, así que el número avanza con las filas escritas y el vigilante de
«atascado» sigue viendo vida durante la escritura, que es lo que más tarda. Y
`starting` nunca dice 100: el 100 % es de `ok` (queda cerrar la transacción y
marcar el índice), así que `/health` se queda en 99 como mucho mientras arranca.

Serie medida con el vault de 12.000 notas, índice de cero, sondeo cada 200 ms
(1.170 muestras con progreso):

| t (s) | Fase | `done`/`total` | `percent` |
|---|---|---|---|
| 4,24 | `leyendo el vault` | 200/48.004 | 0 |
| 5,18 | `leyendo el vault` | 5.000/48.004 | 10 |
| 6,19 | `escribiendo el índice` | 24.202/48.004 | 50 |
| 8,14 | `escribiendo el índice` | 25.002/48.004 | 52 |
| 15,02 | `escribiendo el índice` | 27.802/48.004 | 58 |
| 25,15 | `escribiendo el índice` | 31.802/48.004 | 66 |
| 34,06 | `escribiendo el índice` | 35.402/48.004 | 74 |
| 35,07 | `escribiendo el índice` | 36.003/48.004 | 75 |
| 37,21 | `enlazando` | 36.003/48.004 | 75 |
| 60,04 | `enlazando` | 37.403/48.004 | 78 |
| 120,00 | `enlazando` | 40.803/48.004 | 85 |
| 180,00 | `enlazando` | 44.203/48.004 | 92 |
| 240,05 | `enlazando` | 47.603/48.004 | 99 |
| 243,80 | `enlazando` | 48.003/48.004 | 99 |
| 244,0 | `ok` | — | — |

- **0 retrocesos** en las 1.170 muestras y **ninguna** con `percent == 100`
  mientras `status` era `starting` (máximo 99). En la misma prueba con 400
  notas sobre el código anterior la serie era `[0, 50, 100, 50, 100, 100, 50,
  100]`: el 100 % sobre trabajo pendiente, y luego para abajo.
- Fases de esta ejecución: `leyendo` 4,2-6,0 s (0→24 %), `escribiendo`
  6,2-35,1 s (50→75 %, 29 s de volcado: por eso el número ahora se mueve),
  `enlazando` 35,3-243,8 s (75→99 %). `preparando páginas` dura menos que el
  intervalo de sondeo y no aparece en la tabla; sus muestras están en el test.
- El `503` de `/pages` durante todo ese rato dice el mismo número que `/health`
  (`el índice se está construyendo (54 %): escribiendo el índice. Vuelve en
  unos segundos.`) y nunca una lista a medias: 119 peticiones, 205-231 B,
  1-4 ms cada una.
- El `healthcheck` del compose, en esa misma ejecución mientras indexaba:
  `indexando: leyendo el vault (4%), 0.5 s en marcha; 12001 ficheros del vault`
  → **0**. El número depende del instante del chequeo (el de la sección 4, en
  otra ejecución, pilló el 12 %); lo que importa es que ya no dice 100.

## 4. El `healthcheck` del compose, ejecutado de verdad

El script se extrae de `docker-compose.prod.yml` (el que se despliega) y se
lanza con `python -c` contra `uvicorn` real, cambiando solo el puerto:

| Estado | Comando | Resultado |
|---|---|---|
| Indexando (vault de 12.000 notas, índice de cero) | tal cual | **0** — `indexando: leyendo el vault (12%), 0.5 s en marcha; 12001 ficheros del vault` |
| Listo | tal cual | **0** — `ok: índice v5, 12001 páginas, 12001 ficheros del vault` |
| Vault ilegible (`Notas/Cerrada` con `chmod 000`) | tal cual | **1** — `healthcheck: el vault no se puede recorrer: PermissionError(13, 'Permission denied')` |

El mismo script, con `/health` servido por un doble, en los otros dos casos que
tienen que fallar (tests automáticos, `backend/tests/test_p16_healthcheck.py`):
`/health` dice `error` → 1 `healthcheck: /health dice error: …`; índice vacío
con `.md` en el vault → 1 `el índice está vacío (0 páginas) pero hay 1 ficheros
en el vault …`; y sin nada escuchando → 1 `la API no responde en /health: …`.

## 5. Lo que no se ha podido comprobar

- **Docker no está disponible aquí**: no se ha ejecutado `docker compose
  config` ni el ciclo `healthy`/`unhealthy` del daemon. El script del
  `healthcheck` sí es el del YAML y se ha ejecutado contra la app de verdad.
- El navegador es Chrome headless **en esta máquina**, con los estáticos del
  panel servidos desde un montaje `9p`; en el contenedor van dentro de la
  imagen. La parte atribuible al número de notas (0,36 s de red + 0,35 s de
  DOM) es la que vale; la carga del panel está inflada aquí.
- No hay medidas de antes con el navegador: el panel no cambia en P16
  (`frontend/` no se toca), así que la medida del árbol vale para los dos lados.
