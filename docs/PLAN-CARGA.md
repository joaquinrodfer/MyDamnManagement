# Plan: carga de trabajo personalizable

Segunda vuelta a la carga del equipo (la primera es el hito H5 de
`PLAN-ESPACIOS-Y-CARGA.md`). Objetivo: que sirva a **cualquier equipo**, sea cual
sea su ámbito y su forma de organizarse, sin obligar a nadie a configurar nada:
lo que no se toca sigue funcionando como hoy.

## Decisiones del dueño (2026-11-21)

1. **La configuración es por espacio**, y además **por persona**: cada usuario
   tiene su propia jornada (40, 35, 30 h… y cómo se reparten en la semana).
2. **Ausencias:** cada persona registra las suyas. Los roles (quién puede qué)
   se van a replantear más adelante; hasta entonces, el administrador también
   puede registrar las de cualquiera.
3. **Horas reales (registro de tiempo, cronómetro): no por ahora.**
4. **Ver la carga es un permiso nuevo**, que se da **por usuario o por grupo**
   y **por espacio**. Quien ve la carga de, por ejemplo, dos espacios ve a cada
   persona de esos espacios **combinada** (todo lo suyo junto) y **desglosada
   por espacio y por proyecto**.
5. **Dedicación por espacio:** cada persona puede tener un % de dedicación a
   cada espacio (60/40…). Sin declarar, cuenta toda su capacidad en cada uno.
6. **La jornada la cambia solo un administrador** (por ahora).
7. **Festivos:** un calendario común para toda la empresa que cada espacio
   puede ampliar con los suyos. Además, **importar calendarios** para que la
   empresa traiga el suyo (fase C1b).
8. **«Grupo»** en el permiso de carga = los grupos de personas creados en
   Administración (Ajustes).
9. **Nueva vista de la carga**, más clara y con más cortes: por **día, semana
   y mes**, y **general, por espacio, por proyecto** y por persona.

## Modelo: niveles de configuración

Gana el más concreto. Sin tocar nada, los valores de hoy.

| Nivel | Qué decide |
|---|---|
| Global | Valores por defecto y plantillas por tipo de equipo |
| Espacio | Unidad de esfuerzo, festivos, umbrales del semáforo |
| Base | Si cuenta para la carga, qué columna hace de qué, cómo se reparte |
| Persona | Jornada (horas por día de la semana), ausencias |

## Fases

### C1 — Jornada, festivos y ausencias (hecho: jornada, dedicación, ausencias y festivos a mano)
- Jornada de cada persona por día de la semana (L–V 8 h por defecto; 35 h,
  30 h, viernes corto, turnos con fin de semana…). Sustituye a la capacidad
  semanal fija, que pasa a calcularse.
- Festivos por espacio.
- Ausencias (vacaciones, baja, formación…) que cada persona registra; restan
  capacidad en los días que tocan.
- El reparto de las horas de una tarea usa los días que **esa persona**
  trabaja, no «de lunes a viernes».

### C1b — Importar calendarios de festivos (hecho)
- Subir un archivo **iCalendar (.ics)**, el formato que exportan Google
  Calendar, Outlook, Apple Calendar y casi cualquier calendario.
- **Suscribirse a una URL .ics** (también `webcal://`): los calendarios
  públicos de festivos de Google o un calendario publicado desde Outlook. Se
  vuelve a leer cada día.
- **Festivos oficiales por país y región** desde Nager.Date (servicio público
  y gratuito, sin clave): nacionales y autonómicos. Los locales, por .ics o a
  mano.

### C2 — Permiso «Ver carga» (hecho, como R4)
> Se hace dentro del plan de roles: fase R4 de `PLAN-ROLES.md`.

- Se concede a un usuario o a un grupo, para uno o varios espacios (o todos:
  sustituye a `can_view_global_workload`, que se migra como «todos»).
- Quien lo tiene ve a todas las personas de esos espacios: su carga combinada
  y desglosada por espacio y proyecto. Cada persona sigue viendo siempre la
  suya.

### C3 — Nueva vista de la carga
- Escala: día, semana, mes. Agrupación: general, por espacio, por proyecto,
  por persona (y por grupo).
- Mapa de calor persona × periodo con el semáforo; al pulsar una celda, las
  tareas que la llenan.
- Huecos visibles: «sin fecha» y «sin estimar». Exportar a CSV.

### C4 — Unidad de esfuerzo
- Por espacio: horas, días, puntos (con equivalencia en horas), tallas
  (S/M/L… = X h) o número de tareas.
- Peso por defecto para las tareas sin estimar.

### C5 — Reglas por espacio y por base
- Umbrales del semáforo (p. ej. 80 / 100 / 120 %) y aviso de infrautilización.
- Reparto en el tiempo: uniforme, al principio, al final, en el día de entrega.
- Reparto entre responsables: completas, a partes iguales o % de dedicación.

### C6 — Cualquier base cuenta
- Una base de cualquier tipo puede contar para la carga declarando qué
  columna es responsable, fechas y esfuerzo (tickets, encargos, turnos…).

### C7 — Ayudas para planificar
- Carga de cada persona junto a su nombre al elegir responsable.
- Aviso de sobrecarga para la semana siguiente.
- «Mi semana» del inicio con todo lo anterior.

### C8 — Plantillas por tipo de equipo
- Agencia, software (puntos), operaciones con turnos, consultoría, sin
  estimación…: un clic deja el espacio configurado.

## Para más adelante (anotado, no planificado)

- **Replantear los roles:** ya planificado en `PLAN-ROLES.md` (2026-11-22).
- **Botón de «Apunta algo» en el panel izquierdo:** abajo a la izquierda del
  panel desplegado. Con el panel cerrado, un botón flotante con «+» abajo a la
  izquierda que hace lo mismo.
- Horas reales (registro de tiempo): descartado por ahora.
