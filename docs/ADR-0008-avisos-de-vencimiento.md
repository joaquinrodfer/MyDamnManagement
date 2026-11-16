# ADR 0008 — Avisos de vencimiento: lo vencido se ve al entrar

- **Estado:** aceptado (2026-11-16)
- **Contexto:** MyDamnManagement, gestor de proyectos en ficheros `.md`. Esta es
  la función 12 del [backlog](BACKLOG-GESTOR-PROYECTOS.md): «que no se pase una
  fecha sin enterarte». «Mis tareas» ya agrupaba en *Vencidas*, pero había que
  acordarse de abrir la vista; si una fecha se pasaba y nadie entraba a mirarla,
  no pasaba nada. Y qué era «hoy» lo decidía cada pantalla con el reloj del
  navegador: dos personas del mismo equipo en husos distintos podían ver dos
  «hoy» distintos para la misma fila.
- **Relacionado:** [ADR-0001](ADR-0001-formato-del-vault.md) (el `.md` es la
  verdad), [ADR-0006](ADR-0006-subtareas.md) y
  [ADR-0007](ADR-0007-estado-cerrado.md) (qué cuenta como fila cerrada).

## Decisiones

**D1 — El día es el del servidor, y viaja en la respuesta.**

Una fecha de una fila es un día del calendario (`YYYY-MM-DD`) y decir si pasó es
una resta contra «hoy». Esa resta se hace en **un solo sitio**,
`backend/app/due.py`, contra `server_today()` (el día local del proceso que
sirve la app). `GET /my-tasks` manda el resultado en vez de dejar que el cliente
lo calcule: `today` (el día con el que se comparó), `counts`
(`overdue` / `today`) y, por tarea, `due` (`overdue` / `today` / `week` /
`later`, o `null`) y `days`.

Con eso, la barra lateral, el aviso y los bloques de la vista leen **la misma
resta**: no pueden contradecirse. La alternativa —cada pantalla con su reloj—
era la que ya existía y tenía justo ese fallo: un contador de «2 vencidas» que
abría una lista de «Vencidas: 0» porque para ese portátil todavía era ayer.

**D2 — Dos números, no una suma.** «3 vencidas» y «1 hoy» responden a dos
preguntas distintas (lo que ya se pasó y lo que se pasa hoy) y se leen sin
leyenda; una sola cifra sumada las mezclaría y habría que explicarla. Solo
aparecen si hay algo, y **el total no va en la etiqueta**: lo urgente es lo que
se ve, el total está al abrir la vista (y en el `title` / `aria-label`). Los
colores son los que el panel ya usa: `--down` es «vencido» y `--warn` es «hoy /
en curso».

**D3 — El aviso es de la app y de la sesión.** Al entrar, si hay algo vencido,
sale un aviso discreto y descartable («tienes 3 tareas vencidas») en el mismo
sitio donde salen los demás avisos: no es un modal, no bloquea nada y no se va
solo (hay que poder leerlo con calma). La marca de «ya salió» va en
`sessionStorage`, no en `localStorage`: se pone al pintarlo —para que un
refresco del árbol no lo repita— y cerrar la pestaña vuelve a empezar, porque
mañana el aviso tiene que decir la verdad de mañana. Sin nada vencido no se
pinta.

**Nada fuera de la app.** Sin correo, sin push, sin servicio en segundo plano:
no hay nada que programar ni que mantener vivo, y el aviso no puede llegar por
un canal que el usuario no ha pedido. Esto es «cuando entras, lo ves».

**D4 — La marca de la tabla también la decide el servidor, fila a fila.** Cada
fila viaja con `overdue_keys`: las claves de sus propiedades de fecha que ya
pasaron **y que se tienen que marcar**. No se marca

- en una base **sin regla de cierre** (`closed:` en su `.md`): sin estado no hay
  «sin terminar», y marcar todas las fechas viejas sería una opinión del
  cliente;
- en una **fila cerrada**: una tarea hecha tarde no está vencida.

Las dos reglas las aplica `app.due.overdue_date_keys`, con la misma regla de
cierre que esconde filas en «Mis tareas» y cuenta subtareas (ADR-0007). El
cliente solo pone la clase `cell-overdue` en la celda de esa clave (tono de
`--down` en el texto y al 10 % en el fondo: color, no solo negrita, porque en
una tabla de números una negrita más no se distingue).

**D5 — Qué no cuenta como vencido.** Una **fecha imposible** (`cuando pueda`,
`2026-13-45`) no es una fecha: no cuenta como vencida ni como nada, y la fila se
queda sin fecha, que es la verdad. Una fila **cerrada** con fecha pasada no
cuenta (D4). Una fila de una base **sin** regla de cierre **sí** cuenta en «Mis
tareas» —nada puede cerrarla, luego sigue abierta— pero no se marca en su tabla.

## Consecuencias

- El contrato de `GET /my-tasks` y de las filas crece con campos aditivos
  (`today`, `counts`, `due`, `days`, `overdue_keys`): un cliente viejo los
  ignora y sigue funcionando.
- Los tests que dependen del día fijan `app.due.server_today` con
  `monkeypatch`, así que no cambian de resultado según el día en que se
  ejecuten. `app/due.py` se llama por módulo (`due.server_today()`), no por
  nombre importado, justo para que ese parche alcance al código de verdad.
- El coste es una resta por fila y propiedad de fecha en las respuestas que ya
  se servían. No hay índice nuevo ni consulta nueva.
