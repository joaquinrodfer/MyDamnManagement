# Capturar desde macOS con un Atajo de Shortcuts (P13)

Un **atajo de Shortcuts** (la app Atajos) con `⌘⇧N` como atajo de teclado: al
pulsarlo pide el texto en una cajita, hace el `POST` y avisa con una
notificación. Nada que instalar ni compilar.

> Aquí no hay capturas de pantalla: el contenedor donde se preparó esto no es
> un Mac, así que no se pueden generar. Van los **nombres exactos** de cada
> acción, en español y en inglés (Atajos cambia de idioma con el sistema).

## 0. El token (una vez)

En el panel: menú del usuario (arriba a la derecha) → **Tokens de captura** →
**Crear token**. Se enseña **una sola vez**; cópialo.

En el Mac no hace falta variable de entorno: el token va dentro del atajo, en la
cabecera `Authorization`. Es un secreto que vive en tu Shortcuts, no en un
fichero del repo; si algún día lo compartes con la app Atajos de otro Mac,
revócalo y crea otro.

## 1. Crear el atajo

1. Abre **Atajos** (*Shortcuts*) → **+** (Nuevo atajo).
2. Ponle nombre: **Capturar en MyDamnManagement**.
3. Panel derecho → pestaña **Detalles del atajo** (el icono ⓘ / *Shortcut
   Details*):
   - **Recibir** (*Receive*): elige **Texto** (*Text*).
   - **de** (*from*): **Entrada rápida** (*Quick Input*).
   - **Atajo de teclado** (*Keyboard Shortcut*) → **Añadir atajo de teclado** y
     pulsa **⌘⇧N**.

   Con eso, al pulsar `⌘⇧N` macOS abre una caja de texto y lo que escribas
   entra en el atajo como **Entrada de atajo** (*Shortcut Input*).

   > Nota: `⌘⇧N` también es «Nueva carpeta» en el Finder y «Nueva ventana» en
   > algunas apps. Si en el Finder no salta tu atajo, usa `⌃⌥⌘N` (mismo sitio,
   > otra combinación): lo que importa es que sea global.

4. En el lienzo del atajo, añade las acciones en este orden.

## 2. Las acciones

### Acción 1 — «Obtener contenido de URL» (*Get Contents of URL*)

- **URL**: `http://127.0.0.1:8000/capture`
  (si la app no está en ese puerto, la tuya; el atajo no lee `MDM_URL`).
- Despliega **Mostrar más** (*Show More*) y pon:
  - **Método** (*Method*): **POST**.
  - **Cabeceras** (*Headers*) → **Añadir nueva cabecera**:
    - Clave: `Authorization`
    - Valor: `Bearer EL_TOKEN_QUE_COPIASTE` (la palabra `Bearer`, un espacio y
      el token; sin comillas).
  - **Solicitar cuerpo** (*Request Body*): **JSON**.
  - Añade dos campos con **Añadir nuevo campo** (*Add new field*):
    - `text` → tipo **Texto**, valor: la variable mágica **Entrada de atajo**
      (*Shortcut Input*) — la eliges en el selector de variables, no la
      escribas a mano.
    - `source` → tipo **Texto**, valor: `shortcut`.

### Acción 2 — «Obtener valor del diccionario para la clave» (*Get Dictionary Value*)

- **Obtener** (*Get*): **Valor para la clave** (*Value for key*) `title`.
- **En** (*In*): la **salida** de «Obtener contenido de URL» (la variable
  «Contenido de URL» / *Contents of URL*).

### Acción 3 — «Mostrar notificación» (*Show Notification*)

- Título: `MyDamnManagement`
- Texto: `Capturado: ` y, a continuación, la variable **Valor del diccionario**
  de la acción 2 (así se ve el título real, con el sufijo ` (2)` si ya había
  una nota con ese nombre).

## 3. Probarlo

1. Pulsa `⌘⇧N`. Debe salir la caja de **Entrada rápida**: escribe algo y pulsa
   Intro.
2. Aparece la notificación **«Capturado: Captura 2026-11-02 14-33»**.
3. En el panel, la nota está en **Bandeja (N)** sin recargar (el aviso llega
   por `/events`) y su fichero en `Bandeja/`.

## 4. Los errores

Shortcuts **no tiene** `try/catch`: si el servidor no responde o contesta un
error, el atajo se para y macOS enseña un aviso con el motivo («No se pudo
conectar al servidor», «No se pudo completar la operación»). No falla en
silencio —lo que no hace es un mensaje bonito propio—:

- **App apagada o URL mal**: el aviso de Shortcuts. Comprueba que
  `http://127.0.0.1:8000/health` responde.
- **Token revocado**: el `POST` devuelve 401 y el atajo se para igual. Crea un
  token nuevo y pégalo en la cabecera.
- **Texto enorme** (más de 64 KB): 413, también con el aviso de Shortcuts. Una
  captura es un apunte, no un documento.

## Alternativa: Raycast

Si usas Raycast, un **Script Command** (Bash) hace lo mismo con `source:
raycast` en lugar de `shortcut`:

```bash
#!/bin/bash
# @raycast.schemaVersion 1
# @raycast.title Capturar en MDM
# @raycast.mode silent
texto=$(osascript -e 'text returned of (display dialog "¿Qué apuntas?" default answer "")')
curl -s -X POST "${MDM_URL:-http://127.0.0.1:8000}/capture" \
  -H "Authorization: Bearer $MDM_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"text\":\"${texto//\"/\\\"}\",\"source\":\"raycast\"}"
```

Ahí sí hacen falta `MDM_TOKEN` y `MDM_URL` en el entorno de Raycast (Raycast
hereda el del login, así que `launchctl setenv MDM_TOKEN ...` o un `.zshrc`
sirven).
