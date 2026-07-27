# wisnee-deploy

[![Release](https://github.com/getwisnee/wisnee-deploy/actions/workflows/release.yml/badge.svg)](https://github.com/getwisnee/wisnee-deploy/actions/workflows/release.yml)

Orquestación del stack Wisnee en un VPS (Ubuntu Server 24.04+). Las imágenes se
bajan de **GHCR** (no se buildea en el server). Dos entornos: **prod** y **demo**
(este último con seed). Un CLI en **Python** (`./wisnee`) + **Ansible** generan
los `.env` con secrets autogenerados y manejan el sistema; **docker compose** es
la capa de runtime.

## Instalación rápida (en el VPS)

El **mismo** flujo en cada máquina nueva (prod o demo): clonar + `init`.

```bash
# 1. Crear el droplet con tu llave SSH (Ubuntu 24.04). Recomendado: 4 GB para prod, 2 GB para demo.
# 2. Apuntar el DNS del dominio a la IP del droplet (necesario ANTES del init: certbot lo valida).
# 3. En el server (como root):
git clone https://github.com/getwisnee/wisnee-deploy.git /opt/wisnee
cd /opt/wisnee
sudo ./wisnee init        # pregunta dominio, email, GHCR, tag, etc. y levanta todo
```

En el `init`, dos respuestas a tener claras:

- **Tag de imágenes**: `edge` para demo (rolling, última `main`) o una versión
  de release `vX.Y.Z` para prod (inmutable, coherente, con rollback). Ver
  "Qué tag elegir" abajo y "Versionado / Releases".
- **Token de GHCR**: el de la máquina es de **solo lectura** (`read:packages`)
  porque el VPS solo hace `pull`. ⚠️ NO uses acá el PAT con `write:packages`:
  ese es **solo** para el secret `GHCR_PAT` del workflow de Release.

`init` hace: prompts → **verifica que las imágenes del tag existan** →
**autogenera todos los secrets** → render de `env/*` y nginx → Ansible (docker,
swap, UFW, fail2ban, SSH key-only) → `docker login` + `pull` → migrate → `up` →
emite el certificado TLS → escribe `/tmp/wisnee-credentials.txt` (con la URL de
setup + `INIT_TOKEN`).

### Qué tag elegir

| Tag | Qué es | Cuándo |
|-----|--------|--------|
| `vX.Y.Z` | Pin **inmutable**: los 6 artefactos a la misma versión. Permite rollback exacto. | **Prod** |
| `edge` | Última `main` de cada repo de app (rolling, se mueve solo). | Demo |
| `latest` | Última release **ESTABLE**. | Prod, una vez que existan estables |

> ⚠️ **`latest` solo lo mueven los releases estables.** Un prerelease
> (`-beta`, `-rc`) **no** lo toca: mientras el producto esté en beta, `latest`
> no apunta a ninguna versión y el `pull` falla con `... latest: not found`.
> Es el default del prompt en prod por diseño (para cuando haya estables), así
> que **en beta hay que escribir la versión a mano** (ej. `v2.0.0-beta.24`).
> `init` y `update --tag` lo verifican contra GHCR antes de tocar la máquina y
> abortan con la lista de imágenes faltantes si el tag no existe.

> Cada instalación es **independiente y autocontenida**: genera sus propios
> secrets, su propia BD y su propio certificado. Levantar una segunda máquina no
> comparte nada con la primera; alcanzan los 3 pasos de arriba.

## Comandos

```bash
sudo ./wisnee init [--force] [--skip-provision] [--no-harden]
./wisnee update        # baja imágenes nuevas, migra y recrea (actualizaciones)
./wisnee domain <fqdn> [--yes]  # cambia el dominio: re-renderiza nginx, re-emite el cert y recrea
./wisnee status        # docker compose ps
./wisnee logs [svc]    # logs en vivo
./wisnee seed [--yes]  # (solo Demo) resetea la BD y siembra datos
./wisnee cert          # emite/renueva el certificado (recuperación)
./wisnee backup        # pg_dump → backups/
./wisnee credentials   # muestra el credentials.txt
```

> Requiere `python3` (viene en Ubuntu) y, para `init`, permisos de root.

### Init desatendido (cloud-init / panel)

`./wisnee init --unattended` no hace preguntas: lee la config de variables de
entorno. Es lo que usa el **panel** al aprovisionar un droplet (vía cloud-init).

| Variable | Requerida | Default | Notas |
|----------|-----------|---------|-------|
| `WISNEE_ENV` | no | `prod` | `prod` \| `demo` |
| `WISNEE_DOMAIN` | **sí** | — | FQDN, ej. `cliente.wisnee.com` |
| `WISNEE_EMAIL` | **sí** | — | Para Let's Encrypt |
| `WISNEE_TIMEZONE` | no | `America/Lima` | IANA |
| `WISNEE_CURRENCY` | no | `PEN` | ISO 4217 |
| `WISNEE_TAG` | no | `latest` (prod) / `edge` (demo) | Tag de imágenes |
| `GHCR_USER` | **sí** | — | Usuario de GitHub (pull GHCR) |
| `GHCR_TOKEN` | **sí** | — | PAT `read:packages` |
| `WISNEE_INIT_TOKEN` | no | autogenerado | Pre-fija el `INIT_TOKEN` para que el orquestador conozca el token de setup |
| `WISNEE_MATERIS_LICENSE_KEY` | no | — | Clave de licencia Materis (por suscripción). Vacía = "sin licencia" (el CRM solo informa, no bloquea) |
| `WISNEE_WG_PORT` | no | `51820` | Solo prod |
| `WISNEE_WG_ENDPOINT` | no | `<domain>:<port>` | Solo prod |

```bash
export WISNEE_DOMAIN=cliente.wisnee.com WISNEE_EMAIL=ops@wisnee.com \
       GHCR_USER=getwisnee GHCR_TOKEN=ghp_xxx WISNEE_TAG=v2.0.0-beta.2 \
       WISNEE_INIT_TOKEN=$(openssl rand -hex 24)
sudo -E ./wisnee init --unattended
```

Es idempotente: si el droplet ya está configurado, `--unattended` termina con
éxito sin tocar nada (clave para que cloud-init pueda re-correr).

## Estructura

```
compose/
  docker-compose.yml        base: db, migrate(one-shot), server, web(SPA), proxy(nginx), certbot
  docker-compose.prod.yml   overlay: + wa-bridge, mk-bridge, vpn-hub, chat-node-assets(one-shot)
  docker-compose.demo.yml   overlay: + seed(one-shot, APP_ENV=demo)
  .env.example              variables de compose (TAG, DOMAIN, APP_ENV, WG/SSTP ports)
nginx/
  default.conf.template     reverse proxy (render ${DOMAIN}); same-origin SPA + /api + WS
env/
  db.env / server.env / wa-bridge.env / mk-bridge.env / vpn-hub.env   (los genera el orquestador)
```

Solo el `proxy` se publica (80/443). Además, `mk-bridge` expone el UDP del WG
reverso (Mikrotiks CGNAT con RouterOS 7) y `vpn-hub` el TCP `1443` del SSTP
(RouterOS 6). Postgres, el server y la API HTTP de los bridges quedan en la red
interna `wisnee`. `chat-node-assets` es un one-shot que copia el instalador del
Chat Node para Windows al volumen que sirve el server.

> El Postgres **no se expone** a propósito. Para inspeccionarlo o migrar datos
> desde tu local, tunelizalo por SSH (no abras el puerto a internet): en el
> server `docker run --rm -d --network wisnee -p 127.0.0.1:55432:5432
> alpine/socat tcp-listen:5432,fork,reuseaddr tcp-connect:db:5432`, y desde tu
> máquina `ssh -N -L <puertoLocalLibre>:127.0.0.1:55432 root@<dominio>`.

## Secrets (autogenerados, deben coincidir)

- `db.env:POSTGRES_*` ↔ `server.env:DB_*`
- `wa-bridge.env:INTERNAL_SECRET` ↔ `server.env:WA_BRIDGE_SECRET`
- `mk-bridge.env:INTERNAL_SECRET` ↔ `server.env:MK_BRIDGE_SECRET`
- `server.env`: `SECRET_KEY`, `INIT_TOKEN`, `FISCAL_ENCRYPTION_KEY` (nadie los escribe a mano)

## Actualizar variables del server (post-install)

Los `env/*.env` viven en el droplet (`/opt/wisnee/env/`, modo 600) y `update`
**no los re-renderiza** (preserva los secrets). Para cambiar o agregar una
variable del backend (p. ej. la clave de licencia de Materis) en un droplet ya
desplegado:

```bash
cd /opt/wisnee
nano env/server.env            # editá/añadí la variable (ej. MATERIS_LICENSE_KEY=XXXX-...)
docker compose -f compose/docker-compose.yml -f compose/docker-compose.prod.yml up -d server
```

El server lee el env al arrancar, así que recrear solo ese contenedor toma el
cambio (no hace falta `init` ni tocar la BD). Para las variables nuevas que el
stack incorpora con el tiempo (Materis incluido), `./wisnee update` las **siembra
con su default** sin pisar lo que ya cargaste (ver `reconcile_service_envs`): así
un `git pull` + `update` deja en `server.env` las líneas `MATERIS_URL`,
`MATERIS_PROJECT_SLUG` y `MATERIS_LICENSE_KEY=` (vacía), listas para completar la
clave.

## Levantar

```bash
# render del nginx (solo $DOMAIN):
envsubst '$DOMAIN' < nginx/default.conf.template > nginx/default.conf

# Producción
docker compose -f compose/docker-compose.yml -f compose/docker-compose.prod.yml up -d

# Demo (core-only + seed)
APP_ENV=demo docker compose -f compose/docker-compose.yml -f compose/docker-compose.demo.yml up -d
```

`migrate` corre las migraciones y termina; `server` espera a que complete. El
seed **resetea la BD** y solo corre con `APP_ENV=demo` (el contenedor aborta si no).
Además, `APP_ENV=demo` enciende el **modo demostración** (`DEMO_MODE`): la app
bloquea acciones sensibles del login compartido (p. ej. editar/eliminar usuarios
o cambiar contraseñas) para que nadie deje la demo sin acceso.

## TLS (bootstrap del certificado)

nginx no arranca si el `ssl_certificate` no existe → se evita el huevo-y-gallina
con un cert dummy y luego se reemplaza por el real (lo automatiza el orquestador):

```bash
DOMAIN=tu-dominio.com
# 1) cert self-signed temporal para que nginx levante
docker run --rm -v wisnee_certbot-conf:/etc/letsencrypt alpine/openssl req -x509 -nodes \
  -days 1 -newkey rsa:2048 \
  -keyout /etc/letsencrypt/live/$DOMAIN/privkey.pem \
  -out   /etc/letsencrypt/live/$DOMAIN/fullchain.pem -subj "/CN=$DOMAIN"
# 2) up del proxy → 3) emitir el real por webroot:
docker compose ... run --rm certbot certonly --webroot -w /var/www/certbot -d $DOMAIN --email TU_EMAIL --agree-tos -n
# 4) recargar nginx:
docker compose ... exec proxy nginx -s reload
```

El servicio `certbot` del compose renueva en loop cada 12 h.

## Versionado / Releases (tren de releases)

Una sola versión de producto para los 6 artefactos, sin recompilar lo que no
cambió. El modelo:

1. **Cada push a `main`** de un repo de app publica su imagen `:edge`
   (+ `:sha-xxxx`) en GHCR, con caché → rápido. Solo se reconstruye el repo
   que cambió (un commit de frontend NO recompila el server).
2. **Cortar un release**: en `wisnee-deploy` → Actions → **Release** →
   *Run workflow* con la versión (`v2.0.0-beta.2`). Ese workflow **fotografía**
   los `:edge` actuales de los 6 artefactos (server, web, wa-bridge,
   wa-bridge-win-dist, mk-bridge, vpn-hub) en un tag de versión **inmutable y
   compartido** (`docker buildx imagetools create`, sin recompilar). Todos los
   servicios quedan a la misma versión.
3. **Desplegar / rollback** por un solo número:

```bash
./wisnee update --tag v2.0.0-beta.2   # mueve los 6 artefactos a esa versión
./wisnee update --tag v2.0.0-beta.1   # rollback exacto
./wisnee update                       # re-aplica los tags vigentes (p. ej. demo en edge)
```

La versión del release coincide con el `VERSION` del footer del login —
bumpealo al cortar el release, no antes.

> **Secret requerido**: el workflow Release necesita `GHCR_PAT` (Settings →
> Secrets → Actions de `wisnee-deploy`): un PAT classic del owner con
> `write:packages` + `read:packages` (el token de solo lectura del deploy no
> alcanza para retaggear).
>
> **Bootstrap** (primera vez): los `:edge` tienen que existir. Corré una vez el
> workflow "Release image" (o pusheá a main) en cada repo de app para sembrar
> los `:edge`, y recién ahí cortá el primer Release.
>
> **Demo** puede vivir en `TAG=edge` (siempre la última main): un `./wisnee
> update` la pone al día sin cortar release.

### Escape hatch: tag por servicio

Para un hotfix que NO toca el protocolo front/back (un fix de nginx/SEO, etc.)
podés mover un solo servicio sin tocar los demás:

```bash
./wisnee update --web v2.0.0-beta.2   # solo el frontend; server/bridges quedan igual
```

`--server`, `--web`, `--wa`, `--mk` persisten `SERVER_TAG`/`WEB_TAG`/… en
`compose/.env` (ganan sobre `TAG`). **Usalo con cuidado**: desincronizar
`web` y `server` puede disparar la pantalla `old_version`.

Manual equivalente:

```bash
docker compose ... pull          # baja las imágenes de los tags vigentes
docker compose ... up -d         # recrea solo lo cambiado (migrate corre antes del server)
docker image prune -f
```
