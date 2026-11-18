# Medidas del hito 5 de la 3.0 — memoria y carga

Fecha: 2026-11-18. Rama `v3/h5-memoria` (sobre `develop` con los hitos 1 a 4).

Todo está medido en esta máquina, no estimado.

## Dónde y con qué

- **Máquina:** 4 vCPU, 7,8 GB de RAM, Linux 6.8. Es el mismo equipo donde corre producción, así que hay más contenedores encendidos.
- **App:** un solo proceso de uvicorn (un worker, como `docker-compose.prod.yml`), con `VAULT_PROJECTION=0`, que es el modo de producción.
- **Vault:** 5.000 notas de ~1,2 KB y 5 bases de 200 filas, generadas con `backend/scripts/gen_vault.py --notes 5000 --databases 5 --rows 200`. Son 6.005 `.md`, migrados a la base en el primer arranque. El índice ocupa 34 MB.
- **Carga:** `backend/scripts/carga_colaboracion.py`. Durante 60 s simula:
  - **10 usuarios** normales (no administradores), cada uno con:
    - **su WebSocket** (`/ws/pages/{id}`) escribiendo en **su propia nota** a 5 pulsaciones por segundo: un `update` de 1–2 caracteres que espera su `ack` antes de mandar el siguiente;
    - **su `/events` (SSE)** abierto todo el rato, como un navegador.
  - 3 de esos usuarios piden además `/pages/tree` cada 2 s.
  - Un undécimo cliente (el administrador) guarda otra nota por `PATCH` HTTP cada segundo: es el camino sin sala.
- **Qué se mide:**
  - RSS y CPU del proceso, leídos de `/proc/<pid>` cada 0,5 s;
  - el tiempo de ida y vuelta de `update` → `ack`;
  - lo que tarda la sala en **guardar** en la base (`/status` → `collab.persist_ms_p50/p95`, una métrica nueva de este hito, sobre los últimos 500 guardados);
  - el `PATCH`;
  - el árbol.

## Antes y después

«Antes» es el código del hito 4 más la métrica de `/status`, que solo cuenta. «Después» es esta rama.

| Medida (10 clientes, 60 s, 5.000 notas) | Antes | Después |
|---|---|---|
| RSS en reposo, recién migrado | 108 MB | 108 MB |
| RSS con los 10 clientes dentro | 131 MB | 129 MB |
| **RSS máximo durante la carga** | **157 MB** | **148 MB** |
| CPU media del proceso | 65 % | **37 %** |
| CPU p95 (muestras de 0,5 s) | 112 % | 84 % |
| `update` → `ack`, p50 / p95 | 3,0 / 94,6 ms | 4,1 / 81,3 ms |
| **Guardado de la sala en la base, p50 / p95** | 42,1 / 220,5 ms | **10,2 / 42,7 ms** |
| `PATCH` de una nota, p50 / p95 | 26,4 / 362,0 ms | **13,6 / 130,2 ms** |
| `/pages/tree` de un usuario normal, p95 | 270 ms | 188 ms |
| Cambios aceptados / rechazados | 2.990 / 0 | 2.990 / 0 |

**Sin fuga.** Con la misma carga durante **5 minutos** (14.940 cambios), el RSS máximo fue de 153,5 MB: igual que en 60 s. Las salas nacen y mueren con sus clientes, el registro de cambios de cada sala está acotado (500) y las colas de `/events` ya lo estaban.

## La pila completa en Docker

Imagen construida desde esta rama (`backend/Dockerfile`) y arrancada con el mismo vault de 5.000 notas en un volumen, en otro puerto. Producción no se tocó. Después se borraron el contenedor y la imagen.

| Pieza | Memoria |
|---|---|
| Contenedor de la app (`docker stats`, máximo durante la carga) | 144 MiB |
| Proceso de la app dentro (RSS máximo, `/proc`) | 159 MB |
| `dockerd` | 89 MB |
| `containerd` | 48 MB |
| `containerd-shim` (todos los del equipo, no solo el de la app) | 124 MB |
| **Total de la pila** | **≈ 405 MB** |

Con carga, dentro del contenedor: `ack` p95 107 ms, guardado de la sala p95 111 ms, `PATCH` p95 131 ms y árbol p95 368 ms. Son algo peores que fuera porque el equipo estaba ejecutando a la vez el resto de contenedores de producción.

## Presupuesto

| Objetivo | Medido | |
|---|---|---|
| Proceso < 400 MB de RSS con 10 usuarios y 5.000 notas | 148–159 MB | cabe, con 2,5× de margen |
| Pila completa < 1,5 GB en 4 vCPU / 8 GB | ≈ 405 MB | cabe, con 3,7× de margen |

## Qué se cambió en este hito, y por qué

1. **Resolver los `[[enlaces]]` de un guardado ya no carga todos los títulos.**
   - Cada guardado construía el mapa de títulos del workspace entero (`TitleIndex.load`: una consulta de 5.000 filas y otras tantas normalizaciones Unicode) solo para resolver los dos o tres enlaces de su cuerpo. Con 10 salas guardando cada medio segundo, era la mayor parte de la CPU.
   - Ahora `pages.title_key` guarda el título ya normalizado (esquema v12, con su índice), y el guardado pide solo los títulos que nombra (`TitleIndex.load_for`).
   - El reindexado sigue cargando el mapa una sola vez, como antes.
   - Si alguna página no tiene `title_key` (la escribió otro programa directamente en la base), se vuelve al mapa entero, así que el resultado es siempre el mismo.
   - Efecto: guardado de la sala de 42 a 10 ms (p50) y CPU media de 65 % a 37 %.
2. **`/status` cuenta las salas:** salas abiertas, clientes dentro y p50/p95 de los últimos 500 guardados. Es lo que hace falta para vigilarlas en producción.

## Lo que ya se cumplía y se ha comprobado

- **No se cargan todos los cuerpos en memoria.** El cuerpo se lee al abrir la nota (o cuando su sala nace). Los listados piden solo título e icono, y el export recorre un cursor.
- **Una conexión SQLite por petición, con WAL y `synchronous=NORMAL`.** Ya era así (`app.index.connect`). La sala abre la suya en cada guardado, en un hilo, sin bloquear el bucle de eventos.
- **Ninguna estructura por nota en RAM.** Las salas se crean al entrar y mueren al salir: el test `test_dos_clientes_la_autoridad_ordena_y_rebasa` comprueba que no queda ninguna.
- **Colas acotadas por cliente:** `/events` ya lo hacía. La sala no encola: cada envío espera a su socket.

## Lo que no he medido, o miraría después

- **Clientes en la misma nota.** El guion pone a cada cliente en su nota, como pide el enunciado. Si los 10 escriben en la misma, cada cambio se reparte a 10 sockets y los rechazos y rebases crecen; no lo he medido.
- **El árbol de un usuario normal** tarda unos 45 ms sin carga (35 ms el de un administrador), y su p95 con carga (190–370 ms) sale de competir por el GIL con los guardados. Si molesta, el siguiente paso es no mandar las 5.000 páginas enteras cada vez: hoy el panel pide el árbol completo.
- **Más de un worker.** Las salas viven en la memoria de un proceso. Con varios workers, dos personas en la misma nota podrían caer en procesos distintos. La 3.0 es un solo proceso a propósito; para escalar haría falta repartir las salas por nota (afinidad) o sacarlas a un servicio, y eso es la decisión que el enunciado pide parar y discutir.
