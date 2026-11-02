#!/bin/sh
# captura.sh — apuntar algo en MyDamnManagement desde Linux (P13).
#
# El atajo global lo pone el escritorio (ver `mdm-captura.desktop` y las líneas
# de `gsettings` del README, sección «Capturar desde fuera»); este script es
# solo el cuadro de texto y el `POST`. Sin dependencias: usa `zenity`,
# `kdialog` o `rofi` si están y, si no hay ninguno, lee de la entrada estándar
# —que es como se prueba en un contenedor—.
#
# Variables de entorno:
#   MDM_TOKEN  obligatorio. Token de captura: panel → menú del usuario →
#              «Tokens de captura» → Crear token (se enseña una sola vez).
#              Revocado, deja de valer al instante.
#   MDM_URL    opcional. Por defecto http://127.0.0.1:8000
#
# Uso:
#   captura.sh                  # cuadro de texto (o stdin si no hay ninguno)
#   printf 'idea' | captura.sh  # desde una tubería
#   captura.sh "idea"           # el texto como argumento
#
# Sale 0 si la nota se ha creado, 1 si el servidor o el token fallan y 2 si no
# hay ni cuadro de texto ni entrada (para que un script que lo llame lo note).

set -u

URL="${MDM_URL:-http://127.0.0.1:8000}"
TOKEN="${MDM_TOKEN:-}"

# ---------------------------------------------------------------- avisos
# Un fallo no se puede quedar en la consola: cuando esto lo lanza un atajo, no
# hay consola que mirar. Se usa lo que tenga el escritorio.
avisar() {
  _mensaje=$1
  _grave=${2:-info}
  if command -v notify-send >/dev/null 2>&1; then
    if [ "$_grave" = error ]; then
      notify-send -u critical "MyDamnManagement" "$_mensaje"
    else
      notify-send "MyDamnManagement" "$_mensaje"
    fi
    return 0
  fi
  if command -v zenity >/dev/null 2>&1; then
    if [ "$_grave" = error ]; then
      zenity --error --title="MyDamnManagement" --text="$_mensaje"
    else
      zenity --info --title="MyDamnManagement" --timeout=3 --text="$_mensaje"
    fi
    return 0
  fi
  if command -v kdialog >/dev/null 2>&1; then
    if [ "$_grave" = error ]; then
      kdialog --title "MyDamnManagement" --error "$_mensaje"
    else
      kdialog --title "MyDamnManagement" --passivepopup "$_mensaje" 3
    fi
    return 0
  fi
  # Sin escritorio gráfico (o sin ninguno de los tres): a stderr, que al menos
  # queda en el log del sistema.
  printf 'MyDamnManagement: %s\n' "$_mensaje" >&2
}

# ------------------------------------------------------------- cuadro de texto
pedir_texto() {
  if command -v zenity >/dev/null 2>&1; then
    zenity --entry --title="Capturar" --text="¿Qué apuntas?" --width=520
    return $?
  fi
  if command -v kdialog >/dev/null 2>&1; then
    kdialog --title "Capturar" --inputbox "¿Qué apuntas?"
    return $?
  fi
  if command -v rofi >/dev/null 2>&1; then
    printf '' | rofi -dmenu -p "¿Qué apuntas?" -lines 0
    return $?
  fi
  printf 'captura.sh: no hay zenity, kdialog ni rofi: pasa el texto por stdin o como argumento.\n' >&2
  return 2
}

# ------------------------------------------------------------ JSON a mano
# Sin `jq` ni python: se escapan los caracteres que rompen una cadena JSON
# (barra invertida, comillas, tabulador, retorno y saltos de línea). `awk` está
# en cualquier Linux.
json_escape() {
  awk 'BEGIN { ORS = ""; primero = 1 }
       {
         gsub(/\\/, "\\\\")
         gsub(/"/, "\\\"")
         gsub(/\t/, "\\t")
         gsub(/\r/, "\\r")
         if (!primero) printf "\\n"
         printf "%s", $0
         primero = 0
       }'
}

# `campo_json <clave>`: valor de una cadena de primer nivel, sin jq. Sobra para
# leer `title` y `detail` de esta API, que son texto plano.
campo_json() {
  sed -n 's/.*"'"$1"'": *"\([^"]*\)".*/\1/p' | head -n 1
}

# ------------------------------------------------------------------ el texto
if [ "$#" -gt 0 ]; then
  texto=$*
elif [ -t 0 ]; then
  texto=$(pedir_texto)
  estado=$?
  [ "$estado" -eq 0 ] || exit "$estado"    # cancelar (1) no es un fallo
else
  texto=$(cat)
fi

if [ -z "$(printf '%s' "$texto" | tr -d '[:space:]')" ]; then
  avisar "No hay nada que apuntar: el texto está vacío." error
  exit 1
fi

if [ -z "$TOKEN" ]; then
  avisar "Falta MDM_TOKEN: crea un token en el panel (menú del usuario → Tokens de captura) y expórtalo." error
  exit 1
fi

# ------------------------------------------------------------------- el POST
cuerpo=$(printf '{"text":"%s","source":"cli"}' "$(printf '%s' "$texto" | json_escape)")

respuesta=$(printf '%s' "$cuerpo" | curl -sS --max-time 15 \
  -X POST "$URL/capture" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/json' \
  --data-binary @- -w '\n%{http_code}' 2>&1)
estado_curl=$?

if [ "$estado_curl" -ne 0 ]; then
  # El último renglón de `$respuesta` es el código (`000` si no hubo ni
  # conexión): sobra en el aviso, y el mensaje de curl puede traer saltos.
  detalle_red=$(printf '%s\n' "$respuesta" | sed '$d' | tr '\n' ' ')
  avisar "No se pudo conectar con $URL: ¿está arrancada la app? ($detalle_red)" error
  exit 1
fi

codigo=$(printf '%s' "$respuesta" | tail -n 1)
cuerpo_resp=$(printf '%s' "$respuesta" | sed '$d')
detalle=$(printf '%s' "$cuerpo_resp" | campo_json detail)

case "$codigo" in
  2??)
    titulo=$(printf '%s' "$cuerpo_resp" | campo_json title)
    avisar "Capturado: ${titulo:-nota creada}"
    exit 0
    ;;
  401)
    avisar "El token no vale (¿revocado?): crea otro en el panel → Tokens de captura." error
    exit 1
    ;;
  403)
    avisar "Ese token solo sirve para /capture: no abre el resto de la API." error
    exit 1
    ;;
  400|413)
    avisar "La captura no se guardó: ${detalle:-HTTP $codigo}" error
    exit 1
    ;;
  *)
    avisar "La captura falló (HTTP $codigo)$( [ -n "$detalle" ] && printf ': %s' "$detalle" )." error
    exit 1
    ;;
esac
