# Capturar desde Windows (P13)

Dos pasos: guardar el token y darle un atajo de teclado al script. Nada que
instalar salvo **PowerToys** (para el atajo); el script en sí solo usa lo que
trae Windows: PowerShell 5.1 y WinForms.

## 0. El token (una vez)

En el panel: menú del usuario (arriba a la derecha) → **Tokens de captura** →
**Crear token**. Se enseña **una sola vez**; cópialo.

Guárdalo como variable de entorno **de usuario** (no en el script, que acaba en
un backup o en una carpeta compartida):

```powershell
setx MDM_TOKEN "el-token-que-te-enseñó"
```

`setx` escribe la variable para las sesiones **nuevas**: la ventana en la que lo
lanzas no la ve. Si prefieres el ratón: Inicio → «Editar las variables de
entorno del usuario» → Nuevo → `MDM_TOKEN`.

Si la app no está en `http://127.0.0.1:8000`, añade también:

```powershell
setx MDM_URL "http://mi-servidor:8000"
```

## 1. Acceso directo al script

1. Guarda `captura.ps1` donde no se mueva, por ejemplo
   `C:\Users\TU_USUARIO\bin\captura.ps1`.
2. Clic derecho en el escritorio → **Nuevo → Acceso directo**.
3. En «Escriba la ubicación del elemento», pon esto (con **tu** ruta y con
   `powershell.exe`, que es el que arranca en STA —WinForms y el portapapeles
   lo necesitan—):

   ```
   powershell.exe -NoProfile -Sta -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Users\TU_USUARIO\bin\captura.ps1"
   ```

   - `-WindowStyle Hidden` evita el parpadeo de la consola.
   - `-ExecutionPolicy Bypass` es para ese proceso; no cambia la política del
     sistema. Si el `.ps1` vino de otro equipo y Windows lo tiene bloqueado,
     `Unblock-File .\captura.ps1`.
4. Nombre: **Capturar en MyDamnManagement**.

Puedes darle un atajo desde el propio Windows: clic derecho en el acceso
directo → **Propiedades** → «Tecla de método abreviado» → pulsa `Ctrl+Alt+N` →
Aceptar. Windows solo registra ese atajo si el acceso directo está en el
escritorio o en el menú Inicio, y funciona en todo el sistema.

## 2. El atajo con PowerToys → Teclado

Si no tienes el atajo de Windows o quieres el que ya usas:

1. Instala **PowerToys** y abre **Teclado** (Keyboard Manager).
2. **Remapear un atajo** (*Remap a shortcut*) → **+**.
3. **Atajo:** pulsa `Ctrl+Alt+N` (o el que quieras).
4. **Acción:** **Abrir una aplicación** (*Open application*) → elige el acceso
   directo del paso 1 → **Aceptar**.
5. PowerToys tiene que estar corriendo para que el atajo funcione.

## Cómo se comporta

- Abre un cuadro de texto con **lo que tengas en el portapapeles** como valor
  inicial (si acabas de copiar algo, ya está puesto). `Ctrl+Intro` guarda,
  `Esc` cancela.
- Al guardar, un globo de la bandeja dice **«Capturado: Título»**. La nota
  aparece en **Bandeja (N)** del panel sin recargar (llega por `/events`).
- Si la app no responde o el token no vale, sale un **cuadro de error** con el
  motivo; nunca falla en silencio.
- El texto viaja en UTF-8: tildes, eñes y emoji llegan bien.
- `captura.ps1 -Text "algo"` no abre el cuadro: es para encadenarlo desde otro
  script.
