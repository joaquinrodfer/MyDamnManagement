# Plan: roles y permisos

Hoy el superadministrador (`users.is_admin`) lo junta todo:

- mantiene el sistema: usuarios, grupos, espacios y copias;
- gestiona personas: jornadas y ausencias;
- **lee el contenido de todos**, porque `acl` y la guarda le dejan pasar. Eso incluye:
  - las páginas privadas y los espacios personales;
  - la exportación del vault entero.

No tiene sentido para el producto, y choca con la protección de datos:

- el RGPD pide limitar el acceso a lo que cada uno necesita (art. 5) y proteger por defecto (art. 25);
- el art. 87 de la Ley de Protección de Datos española (LOPDGDD) y el art. 20.3 del Estatuto de los Trabajadores solo permiten acceder a lo que haya en las herramientas de trabajo con normas previas y de forma proporcionada.

Este plan separa **gestionar** de **leer**. Notemil es un producto en construcción, todavía sin uso real en una empresa: no hay datos de clientes que proteger en la migración, pero lo de hoy no debe romperse.

## Decisiones del dueño (2026-11-22)

1. **El administrador no ve el contenido de los demás.** Ve lo que le compartan, como cualquiera.
2. **«Gestionar personas» es un permiso aparte**, que se da a personas o a grupos. Cubre jornada, dedicación y ausencias de otros.
3. **Acceso de emergencia:** no, por ahora.
4. **Notas privadas de quien se va** (traspaso, borrado): nada, por ahora.
5. **Invitados externos:** la idea es que no los haya. El compartir página a página que ya existe se queda como está, sin ampliarlo.

## Modelo

### Roles de la instalación

| Rol | Gestiona | Contenido que ve |
|---|---|---|
| **Administrador** | Usuarios (alta, baja, contraseñas), grupos, espacios (crear, borrar, miembros), ajustes generales, y **conceder permisos** | Solo lo suyo y lo compartido con él. De un espacio en el que no está ve nombre, miembros y tamaño, no sus páginas. Los espacios personales de otros no le aparecen. |
| **Usuario** | Lo suyo | Sus espacios y lo que le compartan |

### Roles en un espacio (sin cambios)

- **Administrador del espacio:** miembros y ajustes del espacio. Ve lo compartido del espacio, no lo privado de cada miembro.
- **Editor** y **lector**.

### Permisos

Se conceden a una **persona** o a un **grupo**, para **todos los espacios** o para **uno o varios**.

| Permiso | Qué permite |
|---|---|
| **Gestionar personas** | Jornada, dedicación y ausencias de las personas de esos espacios. Es lo único que deja ver el **tipo** de ausencia (una baja es un dato de salud). |
| **Ver carga** | La carga de las personas de esos espacios: combinada y por espacio y proyecto. Es la fase C2 de `PLAN-CARGA.md`. Las ausencias salen como «Ausente», sin tipo ni nota. |
| **Gestionar festivos** | Los festivos comunes (con alcance «todos») o los de esos espacios, incluida la importación. |
| **Ver auditoría** | El registro de quién hizo qué. |
| **Copias de seguridad** | Hacer y descargar copias. Llevan **todo** el contenido, así que se dan a muy pocos y quedan registradas. |

Sigue siendo así:

- **Cada persona** ve siempre su carga, su jornada y sus ausencias, y apunta las suyas.
- **El administrador del espacio** sigue gestionando los festivos de su espacio, como hoy.

## Fases

### R1 — El administrador deja de leer lo ajeno (hecho)
- **Permisos de página:** `acl` y `guard` dejan de tratar a `is_admin` como «sistema». Solo el proceso interno (usuario `None`) se salta los permisos.
- **Partes que hoy usan `is_admin` como atajo y pasan a filtrar como para cualquiera:**
  - barra lateral (quita la sección «otros» de `_build_tree`);
  - búsqueda, grafo, panel de inicio y «Mis tareas»;
  - avisos (`notifications`);
  - eventos en vivo (`events`);
  - presencia (quién está en línea).
- **Espacios:**
  - `list_for` devuelve al administrador los espacios en los que no está como «gestionables»: nombre, miembros y número de páginas, sin entrar;
  - los personales de otros no salen;
  - puede cambiar los miembros de cualquier espacio no personal, para que nadie se quede fuera. Añadirse a sí mismo queda en la auditoría;
  - en un espacio personal no puede añadir a nadie.
- **Exportar el vault entero y la papelera** siguen siendo del administrador: son una copia y mantenimiento, no lectura del día a día. Pasan al permiso «Copias de seguridad» en R2. La exportación de una página («Exportar a Markdown») ya respetaba los permisos.
- **Páginas sin permisos** (un `.md` puesto a mano en el disco, por el vigilante, el reindexado o el script de migración): pasan a ser privadas del primer administrador, que es quien tiene el disco.
- **Subir una subpágina a la raíz** conserva los permisos que heredaba. Antes se quedaba sin ninguno y su dueño la perdía de vista.
- **Las copias de seguridad** las sigue haciendo el administrador hasta R2.

### R2 — Permisos que se conceden (hecho)
- **Datos:** tabla `grants` (`subject_type` persona o grupo, `subject_id`, `capability`, `workspace_id`; NULL significa todos los espacios, más quién y cuándo). Va en el índice como las demás tablas de permisos.
- **Comprobación:** una sola función, `can(conn, user, capability, workspace_id=None)`, que mira la persona y sus grupos.
- **Pantalla:** en Ajustes → Administración, una sección «Permisos». Para cada permiso, quién lo tiene (personas y grupos) y en qué espacios. Conceder y retirar queda en la auditoría.
- **Migración:**
  - `can_view_global_workload` pasa a ser «Ver carga» en todos los espacios;
  - cada administrador actual recibe, de forma **explícita** y retirable, «Gestionar personas», «Gestionar festivos», «Ver auditoría» y «Copias de seguridad» en todos los espacios, para que no se rompa lo de hoy;
  - un administrador nuevo no recibe ninguno: tiene que concedérselo, y eso queda registrado.
- **Las rutas que hoy piden `is_admin` pasan a pedir su permiso:**
  - jornada, dedicación y ausencias de otros;
  - festivos comunes;
  - auditoría;
  - copias.
- **Cómo quedó:**
  - «Ver auditoría» y «Copias de seguridad» son solo para todos los espacios: la auditoría y una copia hablan de toda la instalación.
  - La papelera y exportar el vault entero van con «Copias de seguridad». Reindexar sigue siendo del administrador: no enseña contenido.
  - El administrador recibe también «Ver carga» al migrar, porque hoy la veía.
  - El primer usuario de una instalación nueva recibe todos: no tendría a nadie que se los diera.
  - El campo viejo `can_view_global_workload` del alta y la edición de usuarios se sigue aceptando y se traduce a «Ver carga» en todos.
  - Los festivos de un espacio, además de su administrador, los gestiona quien tiene «Gestionar festivos» en él, desde Ajustes → Calendario laboral.

### R3 — «Gestionar personas» y la privacidad de las ausencias (hecho)
- **Alcance:** jornada, dedicación y ausencias de otros, solo de las personas de los espacios del permiso.
- **El tipo y la nota de una ausencia** solo los ven la persona y quien la gestiona. En la carga y en el resto sale «Ausente».
- **Interfaz:** el botón «Jornada» de la lista de usuarios y el de la carga solo salen a quien tiene el permiso.
- **Cómo quedó:**
  - `GET /worktime/people` dice a quién gestiona cada uno; la carga marca cada persona con `manageable`.
  - Quien tiene el permiso sin ser administrador ve en Ajustes el apartado «Usuarios» solo con las personas que gestiona y su «Jornada», sin el alta de usuarios.
  - La carga solo cuenta los días de ausencia, nunca el tipo ni la nota; los ve la persona y quien la gestiona (`/absences`, `/worktime/users/{id}`).

### R4 — «Ver carga» por espacio (es C2) (hecho)
- **Qué ve:** a las personas de esos espacios, combinada y por espacio y proyecto, con la dedicación aplicada al mirar un solo espacio.
- **Sustituye** a `can_view_global_workload`: desde R2 ya no se usa; la columna se queda sin leer y el campo del alta se traduce al permiso.
- **Cómo quedó:**
  - Con el permiso en unos espacios, la carga enseña a sus miembros con todo lo suyo junto; lo de espacios que no ve sale como «Proyecto privado [Espacio]».
  - Un selector «Espacio» en la carga (`/workload?workspace=`) deja solo lo de ese espacio, con la capacidad de cada persona por su dedicación a él.
  - El equipo de «Mi semana» del inicio sigue la misma regla.

### R5 — Que se entienda
- **En el perfil de cada usuario,** «Qué puedo hacer»: su rol, sus permisos y de dónde le vienen (directo o por un grupo).
- **En la ficha de un espacio,** quién tiene permisos sobre él.
- **Textos de ayuda** que dejan claro que el administrador no lee lo privado.

## Para más adelante (anotado, no planificado)

- **Acceso de emergencia** al contenido de una persona, con motivo, registro, caducidad y aviso: descartado por ahora.
- **Notas privadas de quien deja la empresa** (traspaso o borrado): descartado por ahora.
- **Plantilla de normas de uso** para la empresa que adopte Notemil (lo que exige el art. 87 de la LOPDGDD), dentro de la documentación del producto.
- **Cifrar las copias de seguridad.**
