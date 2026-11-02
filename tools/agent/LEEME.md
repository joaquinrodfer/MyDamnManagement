# El agente de hitos (`tools/agent/hitos.py`)

Un programa que se ejecuta **una vez al día desde `cron`**, lee las notas que el
dueño ha dejado preparadas, pide a un modelo una lista de hitos y los deja como
**sugerencias** en el sidecar de cada nota. El dueño las ve en el editor, en el
panel de sugerencias, y acepta o rechaza cada una: el agente no escribe nunca
dentro de una nota.

No es un endpoint y no vive en `backend/`: no importa nada de la app, solo usa
la biblioteca estándar (`urllib`). Se habla con el servidor por `/agent/*`, con
el token de agente de P10b.

## 1. Antes de programarlo

1. **Un agente con nombre en el servidor.** En el `.env` del servidor (no en el
   repo), dentro de `AGENT_TOKENS`, un par `token:nombre`:

   ```sh
   AGENT_TOKENS="un-token-largo-y-aleatorio:agente-hitos"
   ```

   El nombre tiene que ser `agente-hitos` (o el que pongas en `MDM_AGENTE`): es
   lo que firma las sugerencias y el nombre de su nota de instrucciones.
   Reinicia el servicio después de cambiarlo.

2. **Su brief en el vault**: una nota `Agentes/agente-hitos.md` (o
   `Notas/Agentes/agente-hitos.md`, si la creas desde la app). Ahí van dos
   cosas: **qué notas mirar** y cómo quieres los hitos. Las notas se escriben
   una por línea, o como wikilink:

   ```markdown
   Eres el agente de hitos. Propón hitos concretos, sin inventar fechas.

   ## Proyectos

   - Notas/Proyectos/Web.md
   - [[CRM]]
   ```

   Un `[[CRM]]` se busca en `Notas/CRM.md`, que es donde la app crea las notas
   nuevas. Si el brief no dice ninguna nota, el programa no adivina: sale con
   `1` y lo explica.

3. **El token, en un fichero de entorno fuera del repo**, legible solo por tu
   usuario (`chmod 600`). Nada de secretos en el `.env` del repo ni en la línea
   del `crontab` (el `crontab` se puede listar sin privilegios):

   ```sh
   # /etc/mdm-agente-hitos.env   (root:root, 600)
   set -a
   MDM_URL=http://127.0.0.1:8000
   MDM_AGENT_TOKEN=un-token-largo-y-aleatorio
   MDM_AGENTE=agente-hitos
   MDM_PROVEEDOR=openai
   MDM_MODELO=gpt-4o-mini
   MDM_API_KEY=sk-...
   # MDM_API_BASE=http://127.0.0.1:11434/v1   # un modelo local, sin clave
   set +a
   ```

   El `set -a` hace que el `.` del `crontab` deje las variables **exportadas**
   al proceso; sin él quedan en el shell y `hitos.py` no ve el token.

   | Variable | Para qué | Por defecto |
   | --- | --- | --- |
   | `MDM_URL` | base de la API | `http://127.0.0.1:8000` |
   | `MDM_AGENT_TOKEN` | el token **de este agente** | — |
   | `MDM_AGENTE` | nombre del agente (el de `AGENT_TOKENS`) | `agente-hitos` |
   | `MDM_NOTAS` | rutas separadas por comas, en vez del brief | — |
   | `MDM_PROVEEDOR` | `openai` (compatible) o `anthropic` | `openai` |
   | `MDM_MODELO` | modelo del proveedor | — (obligatorio) |
   | `MDM_API_KEY` | clave del proveedor | — (salvo `MDM_API_BASE`) |
   | `MDM_API_BASE` | base del proveedor (Ollama, vLLM, proxy) | la del proveedor |
   | `MDM_TIMEOUT` | segundos para el servidor | `30` |
   | `MDM_TIMEOUT_MODELO` | segundos para el modelo | `120` |
   | `MDM_MAX_HITOS` | tope de hitos por ejecución | `8` |

4. **Comprobación en seco**, antes de programar nada. Lee el contexto de verdad
   y llama al modelo, pero **no escribe ni una sugerencia**:

   ```sh
   set -a; . /etc/mdm-agente-hitos.env; set +a
   python3 /ruta/al/repo/tools/agent/hitos.py --dry-run
   ```

   Imprime el prompt entero, las sugerencias que crearía y, en su caso, las que
   descarta y por qué. Si aquí no sale lo que esperas, el cron tampoco.

## 2. La línea del `crontab`

```cron
17 6 * * *  . /etc/mdm-agente-hitos.env && /usr/bin/python3 /opt/mydamnmanagement/tools/agent/hitos.py >> /var/log/mdm-hitos.log 2>&1
```

- `17 6 * * *` — todos los días a las 06:17 (la hora del servidor). Una hora
  «rara» evita el minuto 0, donde coinciden todos los crons de la máquina.
- `. /etc/mdm-agente-hitos.env` — cron arranca con un entorno mínimo, así que
  las variables (el token incluido) se cargan del fichero antes de ejecutar. El
  fichero lleva `set -a`, que es lo que las exporta al proceso.
- `>> /var/log/mdm-hitos.log 2>&1` — el resumen de cada pasada va al log (no al
  correo de cron): es lo que se mira cuando faltan sugerencias. Rótalo con
  `logrotate` si te molesta que crezca.
- `/usr/bin/python3` — el intérprete del sistema; el programa solo usa la
  biblioteca estándar, así que no hace falta el venv del repo. Si tu Python es
  más viejo que 3.8, usa el del venv (`/opt/mydamnmanagement/.venv/bin/python`).

Para probar la línea sin esperar a mañana, ponla a mano:

```sh
set -a; . /etc/mdm-agente-hitos.env; set +a
/usr/bin/python3 /opt/mydamnmanagement/tools/agent/hitos.py; echo "salida: $?"
```

## 3. Códigos de salida

| Código | Qué significa | Qué mirar |
| --- | --- | --- |
| `0` | Todo bien: leyó, propuso y creó lo que había que crear (o nada, si ya estaba) | el resumen del log |
| `1` | Error de configuración | falta `MDM_AGENT_TOKEN`; el token no vale o el servidor no tiene `AGENT_TOKENS`; el brief no existe o no dice ninguna nota; falta `MDM_MODELO`/`MDM_API_KEY`; una ruta mal escrita |
| `2` | El servidor no responde | `MDM_URL`, el servicio está parado, el puerto cerrado, un 5xx |
| `3` | El modelo falló | clave o modelo equivocados, proveedor caído, cuota, o una respuesta que no es el array JSON de hitos |
| `70` | Fallo interno del programa | el servidor rechazó una petición que el programa construyó (un 4xx que no es el tope de pendientes); con `MDM_DEBUG=1` sale la traza |
| `130` | Interrumpido con Ctrl-C | — |

`0` incluye el caso «el servidor dijo que no por el tope de pendientes»
(`429`): el programa funcionó, es el dueño el que tiene veinte sugerencias sin
decidir y el resumen lo dice. No es un fallo del cron.

**Ningún fallo sale como traza de Python**: cada uno imprime una línea con lo
que pasó y con qué revisar.

## 4. Cuando no aparecen sugerencias

En orden, que es el orden en que se rompen las cosas:

1. **¿Salió el cron?** `grep mdm-hitos /var/log/syslog` o el log del fichero.
   Si no hay línea del día, el problema es el `crontab` (ruta del script,
   intérprete, usuario), no el agente.
2. **¿Qué código de salida dejó?** La tabla de arriba. `1` casi siempre es
   token; `2`, el servicio; `3`, el modelo.
3. **¿Qué dijo el resumen?** Cada pasada escribe cuántas creó y, para las que
   no, el motivo:
   - `descartadas: N — «Título»: el ancla no aparece literalmente en Notas/X.md`
     → el modelo citó un texto que no está en la nota (o la nota cambió entre
     la lectura y la propuesta). Es el descarte normal y el que evita
     sugerencias huérfanas. Si pasa siempre, dale al brief una pista: «ancla
     los hitos en el encabezado `## Hitos`».
   - `descartadas: … origen_nota = 'X' no es ninguna de las notas leídas` → el
     modelo devolvió una ruta que no es la del brief. Revisa el brief.
   - `descartadas: … sin old_text` → el modelo no citó la nota.
   - `ya propuestas (no se repiten): N` → idempotencia: esa misma sugerencia ya
     existe (pendiente, aceptada o rechazada). Si quieres que vuelva a
     proponerla, **recházala y cámbiala** en la nota (o borra el `.json` del
     sidecar y espera a la pasada siguiente).
4. **¿Está la sugerencia en el panel y no la ves?** El panel enseña las
   pendientes de la nota abierta (`N sugerencias por decidir`). Si el agente no
   leyó esa nota, no habrá nada: mira el resumen («notas: …») y el brief.
5. **¿Se creó y el dueño no la encuentra por el texto?** El `.json` está en
   `<vault>/<ruta de la nota>.sugerencias/<id>.json`; ahí se ve el `old_text`,
   el `new_text`, el `agent` y el estado.
6. **Prueba en seco**: `--dry-run` enseña el prompt exacto que recibe el modelo
   y lo que haría con su respuesta. Es la forma rápida de separar «el modelo
   propone mal» de «el programa descarta bien».

## 5. Qué escribe (y qué no)

- Una sugerencia por hito, con `kind: "edit"`: `old_text` es el fragmento que
  citó el modelo y `new_text` es ese mismo fragmento con el hito detrás
  (`- **Título** — detalle`). El `old_text` tiene que aparecer **literalmente**
  en la nota; si no, no se crea nada.
- El `agent` del `.json` lo pone el servidor a partir del token: el programa no
  puede firmar como otro.
- **No** acepta ni rechaza sugerencias (el token no tiene esas rutas), **no**
  toca los `.md` y **no** lee el vault del disco: todo pasa por `/agent/*`.
- Idempotente: la misma nota, el mismo ancla y el mismo texto nuevo no se
  proponen dos veces. Un cron diario no llena el panel de clones.

## 6. Un modelo local, sin clave

```sh
MDM_PROVEEDOR=openai
MDM_MODELO=llama3.1:8b
MDM_API_BASE=http://127.0.0.1:11434/v1
```

`openai` es el dialecto, no el fabricante: vale cualquier servidor compatible
con `/chat/completions` (Ollama, vLLM, llama.cpp, un proxy). Con `MDM_API_BASE`
puesta, `MDM_API_KEY` es opcional.
