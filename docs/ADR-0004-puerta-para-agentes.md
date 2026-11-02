# ADR 0004 — La puerta para los agentes: token, contexto y canal de vuelta

- **Estado:** aceptado (P10b, 2026-11-02)
- **Relacionado:** [`ADR-0003`](ADR-0003-sugerencias-de-agentes.md) (el modelo de sugerencias, P10a), [`ADR-0001`](ADR-0001-formato-del-vault.md) (formato del vault), `ROADMAP-v1.0.md` §P10
- **Fuera de alcance:** la interfaz de sugerencias (su tarjeta), el editor y cualquier login del dueño. Esta tarjeta no inventa autenticación para la app; solo para los agentes.

## Contexto

P10a dejó el modelo —un parche `old_text → new_text` en un sidecar, que el dueño
acepta o rechaza— y una API para el dueño. Dejó también, explícito en el ADR-0003,
que el campo `agent` era una **etiqueta de autoría**: lo ponía quien llamaba, así
que cualquiera podía firmar como otro. Y las sugerencias solo se podían crear
desde dentro de la app.

Para que un agente externo trabaje sobre el vault hacen falta tres cosas
distintas, y ninguna es «darle la API»:

1. **Identidad.** Que el servidor sepa qué agente llama, sin creerse lo que diga
   el cuerpo, y que un token filtrado no valga nada más que para proponer.
2. **Contexto.** Un agente que hace cinco llamadas para escribir un parche
   cuesta cinco veces más y se equivoca más. Tiene que poder pedir «dame la nota
   y su vecindario» de una vez.
3. **Canal de vuelta.** Un agente que no sabe qué se aceptó y qué se rechazó
   propone lo mismo cada mañana. El hilo de la sugerencia ya era el canal; nadie
   se lo estaba sirviendo.

## Decisión

### 1. Un token por agente, y nada más

```
AGENT_TOKENS="token1:agente-hitos,token2:agente-revision"
```

Pares `token:nombre` separados por comas. El ejemplo de la tarjeta fija el
orden —primero el token, luego el nombre— y es el que se implementa; el nombre
es lo que se lee en los `.json` y en los logs. Los tokens son opacos: se
comparan, no se interpretan.

- **Sin `AGENT_TOKENS` (o vacía) las rutas de `/agent` dan 404.** No 403. Un 403
  dice «existe y no puedes»; un despliegue que no ha configurado agentes no
  tiene esa superficie y no debe anunciarla; 404 es lo que ya hace la app con
  cualquier ruta desconocida. Con `DOCS_ENABLED` encendido (el valor por
  defecto) `/openapi.json` **sí** publica las tres rutas aunque no haya tokens:
  lo que no se anuncia es la superficie *activa*, no la posibilidad de
  configurarla. Quien quiera ni eso, `DOCS_ENABLED=0`.
- **La cabecera es `X-Agent-Token`**, no un parámetro de la URL. Las URLs
  acaban en el log de acceso, en el historial y en los `Referer`; una cabecera,
  no. FastAPI/uvicorn no registra cabeceras en el log de acceso por defecto.
- **Comparación en tiempo constante y sin cortocircuito**: se recorre *todos*
  los tokens con `hmac.compare_digest` y se decide al final. Comparar con `==`
  filtra por temporización cuántos caracteres iniciales acertaste; cortar en el
  primer acierto filtra la posición del token en la lista. Se comparan bytes
  UTF-8 porque `compare_digest` sobre `str` exige ASCII y un token con un acento
  lanzaría `TypeError` (un 500 por una cabecera rara).
- **401 con el mismo mensaje** para «no mandaste token» y «ese token no existe».
  La diferencia solo le sirve a quien está probando tokens.
- **El nombre sale del token.** `AgentSuggestionCreate` no declara `agent`: si
  el cuerpo lo manda, Pydantic lo descarta (`extra="ignore"`). Un agente no
  puede firmar como otro ni por error.
- **Los tokens no se registran nunca**: ni en un acierto, ni en un 401, ni en
  los avisos de configuración. Los avisos hablan de la posición de la entrada y
  del nombre del agente, que es lo que hace falta para arreglar la variable sin
  volcar el secreto en el log. La línea que deja una sugerencia creada dice
  agente, nota, `kind` y tamaño del parche: **nunca** su texto.
- Un nombre de agente se valida como nombre de fichero portable
  (`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`) porque es también el nombre de su nota de
  instrucciones. Una entrada con un nombre imposible se descarta con un aviso y
  las demás siguen valiendo: añadir un agente no puede tumbar a los que ya
  funcionaban.

### 2. `GET /agent/context`: una llamada, no cinco

```
GET /agent/context?page_id=<uuid>
GET /agent/context?path=Notas/Algo.md
```

Exactamente una de las dos formas (ninguna o las dos: **400**). El `path` es la
ruta del vault tal cual la tiene el índice —el agente que lee el disco conoce
rutas, no ids—, pasa por `vaultfs.safe_rel` (un `../` es 400, no un 500) y no se
«adivina» con prefijos: `Notas/X.md` y `X.md` pueden ser dos notas distintas.

La respuesta lleva todo lo que hace falta para proponer algo sensato:

| Campo | De dónde sale | Por qué está |
| --- | --- | --- |
| `page` | índice | id, título, ruta, tipo, `updated_at` |
| `body` | índice, recortado a `AGENT_CONTEXT_MAX_BODY` (20 KB) | el texto sobre el que se propone |
| `body_truncated` | — | si se cortó, quien lee tiene que saber que no lo ha visto entero |
| `backlinks` | tabla `links` | quién nombra esta nota |
| `links` | wikilinks del cuerpo + títulos del índice | qué enlaces resuelven **hoy** y cuáles no |
| `suggestions` | sidecar de la nota, **todas** | el canal de vuelta |
| `brief` | `Agentes/<nombre>.md` | las instrucciones del dueño para ese agente |

Tres detalles que no son de adorno:

- **`links[].exists` se decide con `fold_title`**, el mismo plegado que usa el
  guardado al resolver wikilinks (NFKC + `casefold`, sin quitar acentos). Si
  dijera «existe» con otro criterio, el agente crearía un enlace que no crea
  backlink. Es una consulta y un `set` en Python: barato y coherente.
- **`suggestions` incluye las resueltas.** Un tablón de pendientes no es un
  canal de vuelta: el agente tiene que ver que aquello se rechazó y **por qué**
  (`reason`, `replies`). El coste es leer la carpeta del sidecar, que ya se leía
  para contar pendientes.
- **El cuerpo se corta por bytes, no por caracteres**, y se decodifica sin
  partir un carácter (`errors="ignore"` en el borde). Cortar por caracteres deja
  pasar notas de 60 KB en japonés; cortar por bytes a lo bruto deja un U+FFFD.
- **`brief` se busca en dos sitios, en orden**: `Agentes/<nombre>.md` (la ruta
  de la tarjeta, y la que hay si alguien crea la nota a mano en la raíz del
  vault) y `Notas/Agentes/<nombre>.md` (donde nace la misma nota si se crea
  desde la app, porque toda nota nueva aparece bajo `Notas/`). Sin el segundo,
  el caso normal —crear el brief desde la app— no funcionaría. Gana el primero
  que exista. Si no hay ninguno, `null`.

### 3. `POST /agent/suggestions`: el mismo modelo, con dos topes

El cuerpo es el de P10a más `page_id` (la ruta del agente no cuelga de la nota):
`page_id`, `kind`, `old_text`, `new_text`, `rationale`, `title`, `offset`. Los
topes de tamaño son **los mismos** (413: 8 KB por texto de parche, 32 KB por
cuerpo de `new_page`, 200 B de título; `rationale` 4 KB) y el mensaje de error
sale del mismo sitio (`app/limits.py`), para que las dos puertas no se
distancien con el tiempo.

Encima hay dos topes de **sugerencias sin resolver**, y son dos a propósito:

| Tope | Ajuste | A quién protege |
| --- | --- | --- |
| sin resolver **por nota** | `AGENT_MAX_PENDING_NOTE` (20) | al dueño que abre la nota: da igual quién las proponga, veinte parches son ruido |
| sin resolver **por agente** | `AGENT_MAX_PENDING_AGENT` (20) | al vault: un bucle que reparte uno por nota tampoco pasa |

«Sin resolver» es `pending` **y** `orphaned`, no solo `pending`. Una sugerencia
cuyo ancla ya no está nace `orphaned` y sigue esperando una decisión: cuenta
igual. Es el caso normal —el dueño edita la nota y lo que el agente leyó deja
de encajar— y sin contarlo el agente que más ruido hace sería justo el que
nunca toca el 429. `accepted` y `rejected` ya están decididas y no cuentan.

Por encima de cualquiera de los dos: **429** con el número y el tope en el
mensaje. Los dos contadores se calculan en el mismo recorrido de sidecars que ya
hace falta para el segundo. Aceptar o rechazar una libera sitio, así que el
límite no atasca a un agente que trabaja de verdad.

La escritura es la de siempre: `Repository.create_suggestion`, el mismo modelo,
el mismo sidecar, el mismo `orphaned` si el ancla no está. No hay una segunda
ruta de escritura por ser agente.

Y queda una línea de rastro por sugerencia creada, en el logger `mdm.agents`:

```
INFO mdm.agents: sugerencia <id>: agente=agente-hitos nota=Notas/Hitos.md kind=edit parche=98 B
```

Sin el texto del parche (es contenido del dueño y el log no es su sitio) y sin
el token (aquí ni se tiene). Para que esa línea **salga** hay que darle un
handler al logger `mdm`: la app no configura `logging` —no debe, los tests
capturan stdout/stderr— y uvicorn solo configura los suyos, así que el proceso
que arranca la app pasa `--log-config logging.json` (nuevo `backend/logging.json`,
que viaja en la imagen como `/app/logging.json`). Sin él la app se quedaba en el
`lastResort` de la librería, que solo deja pasar WARNING y superior: todo su
`INFO` se estaba tirando, no solo el de esta tarjeta.

### 4. `GET /agent/suggestions`: el canal de vuelta

```
GET /agent/suggestions?status=pending|accepted|rejected|orphaned&since=<iso>
```

Devuelve **solo las del agente del token** (el filtro no es un parámetro, es la
identidad) con su estado, su `reason` y sus `replies`. `?status=` filtra por
estado.

`?since=` responde a «¿qué ha pasado desde mi última visita?» y por eso mira la
**última actividad** —crear, decidir o la última respuesta del dueño—, no la
fecha de creación: una sugerencia propuesta anteayer con una respuesta de ayer
es novedad de ayer. El orden es esa misma marca, de más reciente a más antigua.
Un `since` que no es una fecha ISO 8601 es **400**, no una lista vacía: un
agente que manda basura tiene que enterarse, no creer que no pasó nada. Se
aceptan `Z` y desfases; sin zona se asume UTC.

### 5. Lo que un agente no puede hacer (y lo que el token no es)

- **No edita, no borra, no acepta ni rechaza.** Un `@app.middleware` en
  `app/main.py` corta con **403** cualquier petición que traiga un
  `X-Agent-Token` válido y no vaya a `/agent/...`: `PATCH /pages/{id}`,
  `DELETE /pages/{id}`, `/trash`, `accept`, `reject` y `reply` quedan fuera del
  permiso. El token deja de ser una etiqueta y pasa a ser un permiso
  **acotado**: si se filtra, sirve para leer contexto y proponer, nada más.
  Un token **inválido** no se rechaza en el guardia: no identifica a nadie, así
  que la petición sigue como cualquier cliente anónimo (401 en `/agent`, y la
  ruta del dueño no mira cabeceras).
- **No puede firmar como otro**: el `agent` del `.json` es siempre el nombre del
  token.
- **Lo que el token NO es: autenticación del dueño.** La API del dueño sigue sin
  login (deuda declarada de v1.3): quien alcance el puerto puede leer, escribir
  y borrar sin token ninguno. La frontera de seguridad hoy es la **red**
  (Tailscale), no el token. Lo que el token aporta es autoría y el permiso
  mínimo del agente; un token filtrado no da más que eso — llenar de
  sugerencias sin resolver tus notas, y para eso están los 429.

## Contrato

```
GET  /agent/context?page_id=<uuid>           200 | 400 (0 o 2 formas) | 404
GET  /agent/context?path=Notas/Algo.md       200 | 400 (ruta fuera)     | 404
POST /agent/suggestions                      200 | 400 | 404 | 413 | 429
GET  /agent/suggestions?status=&since=       200 | 400 (since) | 422 (status)
todas                                        + 401 sin token / token malo
todas                                        404 si no hay AGENT_TOKENS
cualquier otra ruta + X-Agent-Token válido   403 (permiso acotado a /agent/*)
```

## Consecuencias

- La API del dueño (`/pages`, `/pages/{id}/suggestions`, `/suggestions/{sid}/…`)
  **no cambia**: mismos códigos, mismos cuerpos. Lo único que se movió de sitio
  es el helper del 413 (`app/limits.py`), compartido por los dos routers. La
  excepción es una petición que se identifique como agente: el guardia de
  `app/main.py` la corta con 403 si no va a `/agent/...`, que es el permiso
  acotado que promete la sección 5.
- El dueño sigue sin autenticación. Este ADR no la inventa y no debe leerse
  como si la hubiera: la puerta del agente está cerrada con token, la del dueño
  está abierta a quien alcance el puerto hasta v1.3.
- El agente puede leer el vault entero por `?path=`, que es lo que ya permite
  `GET /pages` sin token. Esto no añade una vía de lectura nueva en la práctica;
  añade una forma cómoda para un LLM.
- Queda pendiente y explícito: autenticación del dueño (hoy no hay), limpieza de
  sugerencias resueltas antiguas, y una UI para verlas y decidirlas.
- El rastro de agente es una línea por sugerencia creada, no por lectura: un
  agente que lee contexto cien veces no llena el log (el log de acceso de
  uvicorn ya cuenta las peticiones, y la URL no lleva token).
- Deuda anotada: contar las no resueltas recorre los sidecars del vault en cada
  `POST /agent/suggestions` (milisegundos con un vault local; si algún día hay
  miles de sugerencias, se puede indexar), y `brief` no tiene tope de tamaño:
  es texto del dueño para su agente, y recortarlo en silencio sería peor que
  dejarlo entero.
