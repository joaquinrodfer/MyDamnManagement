# Revisión de seguridad, 23 de noviembre de 2026

Revisión antes de abrir Notemil a gente de fuera (un amigo por Tailscale y,
después, un equipo). Se hizo sobre el código de `develop` y una instancia de
pruebas. Contra producción solo se hicieron lecturas: cabeceras, CORS y
documentación expuesta. Los tests que la fijan están en
`backend/tests/test_seguridad.py`.

## Cómo se probó

- **Accesos cruzados (IDOR):** se crean dos usuarias con espacios distintos, y
  la segunda intenta leer, editar y borrar todo lo de la primera usando sus
  ids. La prueba cubre:
  - páginas, bases, filas y vistas, también con `/databases/{mía}/rows/{ajena}`;
  - chat y mensajes;
  - avisos y fijadas;
  - espacios, miembros y webhooks;
  - jornada y carga;
  - usuarios, permisos (ACL) y enlaces;
  - además, busca los textos secretos en 11 listados: búsqueda, grafo,
    panel, carga, chat, presencia…
- **XSS:** se siembra código en todos los textos que puede escribir alguien:
  - títulos de páginas, bases, filas y vistas;
  - propiedades y opciones;
  - celdas de texto y de URL;
  - mensajes del chat;
  - nombre visible, grupos e invitaciones;
  - `javascript:` en enlaces.

  Luego se recorre la app en Chromium: inicio, nota, base con sus vistas, chat,
  búsqueda, Ajustes y carga.
- **CSRF**, sesión, límite de intentos, SSRF de los webhooks, servido de
  adjuntos, cabeceras y exposición del servidor.

## Hallazgos

| # | Gravedad | Qué | Estado |
|---|----------|-----|--------|
| 1 | Media | **Una relación podía leer títulos de filas ajenas.** Una base propia aceptaba una relación hacia una base que no ves; al poner en la celda el id de una fila ajena, la respuesta traía su título. Hacía falta conocer los ids, pero se filtraba. | Arreglado: la base destino tiene que ser visible para quien declara la relación, y las celdas solo resuelven (y aceptan) filas visibles. |
| 2 | Media | **Sin cabeceras de seguridad:** ni CSP, ni protección contra iframes (*clickjacking*), ni `nosniff` general, ni HSTS. | Arreglado (`app/security.py`): CSP solo con scripts propios (las pantallas de entrada, con el hash de su script), `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `Permissions-Policy` y HSTS cuando se entra por HTTPS. Recorrida la app sin una sola infracción de CSP. |
| 3 | Baja | **Escrituras desde otra web del mismo servidor.** La cookie es `SameSite=Lax` y la API solo acepta JSON, así que desde internet no se podía forzar una escritura. Pero `Lax` no separa las apps del mismo «sitio» (misma IP o dominio, otro puerto), y en este servidor hay varias: un fallo en otra de ellas podía mandar escrituras sin cuerpo (`POST /backups/run`…). | Arreglado: una escritura con un `Origin` que no es Notemil se rechaza con 403. |
| 4 | Baja | **Quedarse sin dueño de una página.** Producción tenía la base «qqq» con permisos propios en los que su único dueño se había dejado en «ver», así que nadie podía editarla ni borrarla. | Ya estaba cerrado en el código actual (al cambiar permisos hay que seguir siendo propietario). Ahora tiene test. El dato roto de producción se va con el reinicio previsto. |

## Lo que ya estaba bien

- **Autorización:** está centralizada (`app/guard.py`, más comprobaciones en el
  chat, los espacios y los permisos). De más de 60 intentos de acceso cruzado,
  ninguno pasó y ninguno filtró nada.
- **XSS:** ninguna carga se ejecutó; todo se pinta como texto. Los títulos de
  páginas y bases se limpian al guardarse.
- **Sesión:** cookie `HttpOnly` y `SameSite=Lax`, `Secure` por HTTPS. Las
  sesiones caducan (30 días sin uso, 90 como tope) y se cierran al cambiar la
  contraseña. El login responde igual a «no existe» y a «contraseña mala», y
  bloquea a los 10 intentos.
- **Webhooks (SSRF):** se resuelve la IP, se rechazan las privadas, la conexión
  va fijada a la IP comprobada (sin *DNS rebinding*) y no se siguen
  redirecciones.
- **Adjuntos:**
  - el tipo se decide por el contenido real, no por la extensión;
  - HTML, SVG y JS nunca se sirven en línea;
  - `nosniff`;
  - rutas comprobadas contra `..`.
- **Edición en vivo (WebSocket):** comprueba sesión, origen y permiso al entrar.
- **CORS:** cerrado. **Documentación de la API:** apagada y detrás de la sesión.
- **Enlaces de invitación y contraseña:** tienen 256 bits, solo se guarda su
  hash, son de un solo uso, caducan y hay límite de intentos.

## Riesgos aceptados y recomendaciones

- **Adjuntos por URL.** Quien tiene la URL de un adjunto (sesión iniciada) lo
  abre aunque no vea la página. La URL lleva 48 bits aleatorios y no se puede
  adivinar, pero si se reenvía, se comparte. Comprobar permisos por adjunto
  pide saber de qué páginas cuelga cada fichero. Queda anotado.
- **El contenedor corre como root.** Conviene un usuario sin privilegios en el
  `Dockerfile`. Hay que hacerlo con cuidado: los volúmenes actuales son de root.
- **Un administrador puede descargar copias**, y llevan todo, también lo
  privado de cada uno. Es lo que se decidió (las copias son de administración),
  pero hay que tenerlo presente al dar el permiso «Copias de seguridad».
- **Al invitar a alguien por Tailscale:** compartir solo esta máquina, y
  revisar las ACL del tailnet para que no vea el resto de servicios del servidor
  (Jellyfin…).
