# Desplegar en el Proxmox

Guía para dejar MyDamnManagement corriendo en un contenedor del Proxmox, con
imagen publicada, actualizaciones de un comando y vuelta atrás de otro.

Lo que hay al final:

```
Internet/local ──► Tailscale (TLS) ──► 127.0.0.1:8000 ──► contenedor mdm_api_prod
                                                          ├── volumen mdm-prod_mdm_vault  (las notas)
                                                          └── volumen mdm-prod_mdm_data   (índice SQLite)
```

La app **no se publica en ninguna interfaz de red**: el puerto vive en
`127.0.0.1` del contenedor y delante va `tailscale serve`, que pone el TLS. Si
algún día quieres abrirla a la LAN, es una decisión aparte y consciente.

---

## 0. Lo que hace falta

- Un **LXC** (Debian 12 o Ubuntu 24.04) o una VM en el Proxmox. LXC va sobrado y
  gasta mucho menos.
- **Docker** con el plugin `compose` v2.
- **Tailscale** instalado en el contenedor (o en el host, con el puerto
  reenviado: más fácil dentro).
- Una **etiqueta publicada** en GHCR (ver «Publicar una versión» abajo).

Tamaño razonable para uso personal con miles de notas: **2 vCPU, 2 GB de RAM,
8 GB de disco** para el sistema y el índice. El vault crece con tus notas y con
los adjuntos: dale espacio o móntalo en un disco aparte.

---

## 1. Preparar el contenedor

En el Proxmox, crear un LXC con Debian 12 sin privilegios, arrancarlo y dentro:

```bash
apt update && apt install -y curl ca-certificates gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  > /etc/apt/sources.list.d/docker.list
apt update && apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker
docker compose version     # tiene que decir v2.x
```

Si el LXC es **sin privilegios** y Docker se queja de permisos al arrancar,
añade en las opciones del contenedor del Proxmox:

```
features: nesting=1,keyctl=1
```

---

## 2. Publicar la imagen (una vez por versión)

La imagen se construye y se publica sola **al crear una etiqueta**:

```bash
git switch main && git pull --ff-only
git tag -a v2.2.0 -F <mensaje>      # o desde GitHub: Releases → Draft a new release
git push origin v2.2.0
```

El workflow `.github/workflows/release.yml` publica
`ghcr.io/joaquinrodfer/mydamnmanagement:v2.2.0` (y `2.2`, `2`). Se sigue en
**Actions → imagen**.

Si el paquete está **privado** (lo normal, porque el repo lo está), el servidor
necesita un token de lectura. En GitHub: *Settings → Developer settings →
Personal access tokens → Fine-grained* con permiso **`read:packages`** sobre el
paquete. Y en el servidor, una vez:

```bash
echo "<TOKEN>" | docker login ghcr.io -u <tu-usuario> --password-stdin
```

Alternativa sin token: *Package settings → Change visibility → Public*. Entonces
`docker pull` funciona sin login.

---

## 3. Instalar

```bash
mkdir -p /opt/mdm && cd /opt/mdm

# El compose y nada más: el código va DENTRO de la imagen.
curl -fsSLO https://raw.githubusercontent.com/joaquinrodfer/MyDamnManagement/main/docker-compose.prod.yml
# Si el PR de esta versión todavía no está fusionado en `main`, cambia `main`
# por `release/2.2` en la URL (o copia el fichero del repo a mano).

cat > .env <<'EOF'
# Qué versión se levanta. Subir de versión = cambiar esta línea.
MDM_TAG=v2.2.0
# Imagen (no hace falta tocarlo salvo que uses otro registro)
MDM_IMAGE=ghcr.io/joaquinrodfer/mydamnmanagement

# Primer administrador. Solo se usa la PRIMERA vez, cuando el índice está
# vacío: después, las altas se hacen desde la app.
ADMIN_USER=joaquin
ADMIN_PASSWORD=cambia-esto-largo-y-raro

# Días de sesión (caducidad deslizante, con tope de 90)
SESSION_DAYS=30

# Carpeta de la bandeja de capturas, relativa al vault
CAPTURE_DIR=Bandeja
EOF
chmod 600 .env

docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps      # api ... (healthy)
curl -s http://127.0.0.1:8000/health
```

`/health` contesta `{"status":"starting",…}` mientras indexa el vault y
`{"status":"ok"}` al terminar. Con el vault vacío es cuestión de segundos; con
12.000 notas, minutos (la app ya responde mientras tanto).

Si el puerto 8000 del contenedor choca con algo del host, cambia en el compose
`"127.0.0.1:8000:8000"` por `"127.0.0.1:8770:8000"` y ajusta el `tailscale serve`.

### Primer acceso

```bash
tailscale up                     # si aún no está en la tailnet
tailscale serve --bg 8000        # TLS en https://<nombre>.<tailnet>.ts.net
```

Abre esa URL, entra con `ADMIN_USER` / `ADMIN_PASSWORD` y **cambia la
contraseña** en el menú del usuario. No uses `tailscale funnel`: eso lo publica
en internet.

En el móvil: abre la misma URL en el navegador y «Añadir a pantalla de inicio».
Funciona como app (PWA) por Tailscale, sin tienda ni instalador.

---

## 4. Backup

Lo único que no se puede reconstruir es el **vault** (las notas). El índice
SQLite se rehace solo al arrancar.

```bash
# Copia en caliente, consistente: tar del volumen a un fichero con fecha
docker run --rm -v mdm-prod_mdm_vault:/v:ro -v /opt/mdm/backups:/b alpine \
  tar czf "/b/vault-$(date +%F).tar.gz" -C /v .
```

Cron diario a las 3:00 (`crontab -e`):

```cron
0 3 * * * docker run --rm -v mdm-prod_mdm_vault:/v:ro -v /opt/mdm/backups:/b alpine tar czf "/b/vault-$(date +\%F).tar.gz" -C /v . && find /opt/mdm/backups -name 'vault-*.tar.gz' -mtime +30 -delete
```

Restaurar:

```bash
docker compose -f docker-compose.prod.yml down
docker run --rm -v mdm-prod_mdm_vault:/v -v /opt/mdm/backups:/b alpine \
  sh -c 'rm -rf /v/* && tar xzf /b/vault-2026-11-10.tar.gz -C /v'
docker compose -f docker-compose.prod.yml up -d
```

El índice se reconstruye del vault al arrancar; si quieres forzarlo, borra el
volumen `mdm-prod_mdm_data` (es derivado, no se pierde nada).

---

## 5. Actualizar y volver atrás

```bash
cd /opt/mdm
sed -i 's/^MDM_TAG=.*/MDM_TAG=v2.3.0/' .env
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps        # (healthy)
```

Volver atrás es lo mismo con la etiqueta anterior:

```bash
sed -i 's/^MDM_TAG=.*/MDM_TAG=v2.2.0/' .env && \
docker compose -f docker-compose.prod.yml pull && \
docker compose -f docker-compose.prod.yml up -d
```

Los volúmenes no se tocan en ninguno de los dos sentidos: el vault es tuyo y las
versiones lo leen igual (el formato `.md` y el frontmatter son estables; cuando
alguna versión cambia algo del formato, lo dice el `CHANGELOG` y trae script).

---

## 6. Cuando algo va mal

| Síntoma | Qué mirar |
|---|---|
| `ps` dice `unhealthy` | `docker inspect --format '{{json .State.Health}}' mdm_api_prod` — el último `output` trae el motivo real |
| `/health` dice `starting` para siempre | el vault es enorme o hay un fichero que no se puede leer: mira `docker compose logs -f api` |
| `pull` da `denied` | falta `docker login ghcr.io` o el token no tiene `read:packages` |
| `pull` da `manifest unknown` | esa etiqueta no se ha publicado: mira Actions → imagen |
| La página carga pero vuelve al login en bucle | estás entrando por `http://` y la cookie va `Secure` por detrás de Tailscale: usa la URL `https://…ts.net` |
| `bind: address already in use` | algo ocupa el 8000 del host: cambia el puerto publicado en el compose |
| El contenedor se reinicia solo | `docker compose logs --tail=80 api`: suele ser un vault ilegible o un `.env` mal escrito |

---

## 7. Notas de diseño que conviene saber

- **Un solo worker.** Cada arranque reindexa el vault y el índice es un SQLite
  con un escritor. Más workers son más reindexados a la vez, no más velocidad.
- **El primer arranque indexa.** Con un vault grande tarda minutos y `/health`
  lo cuenta; no es un fallo.
- **`DOCS_ENABLED=0`** en producción: `/docs` y `/openapi.json` quedan apagados.
- **El puerto no se publica** fuera de `127.0.0.1`: la frontera es Tailscale.
