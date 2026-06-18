"""Renderiza los env/*.env, compose/.env y nginx/default.conf a partir de las
respuestas del operador + los secrets autogenerados."""

import os
from pathlib import Path

from . import config, secretgen


def _write_env(path: Path, data: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{k}={v}" for k, v in data.items()) + "\n"
    path.write_text(body, encoding="utf-8")
    os.chmod(path, mode)


def render(answers: dict, secrets: dict) -> None:
    domain = answers["domain"]
    base_url = f"https://{domain}"

    # TZ a nivel de proceso: alinea crons, formateo y `now()` con la zona de la
    # operación. Default America/Lima si no se preguntó (instalaciones viejas).
    timezone = answers.get("timezone") or "America/Lima"
    # Moneda de la instalación (ISO 4217). Define símbolo y formato de todos los
    # montos. Default PEN si no se preguntó (instalaciones viejas).
    currency = (answers.get("currency") or "PEN").upper()

    _write_env(config.ENV_DIR / "db.env", {
        "POSTGRES_USER": config.DB_USER,
        "POSTGRES_PASSWORD": secrets["DB_PASSWORD"],
        "POSTGRES_DB": config.DB_NAME,
        # Zona del contenedor de Postgres (logs y `now()`/`current_date`).
        "TZ": timezone,
    })

    _write_env(config.ENV_DIR / "server.env", {
        "SERVER_PORT": config.SERVER_PORT,
        "HTTPS": "true",
        "PUBLIC_BASE_URL": base_url,
        "CORS_ORIGIN": base_url,
        "DB_HOST": "db",
        "DB_PORT": "5432",
        "DB_USERNAME": config.DB_USER,
        "DB_PASSWORD": secrets["DB_PASSWORD"],
        "DB_NAME": config.DB_NAME,
        # Zona horaria de la instalación. Al ir como TZ del proceso, todo el
        # server (crons, períodos, formateo, logs) corre en esta zona. La lee
        # `config/date-tools` en el backend.
        "TZ": timezone,
        # Moneda de la instalación (ISO 4217). La lee `config/currency` en el
        # backend para el símbolo y el formato de todos los montos.
        "CURRENCY": currency,
        "SECRET_KEY": secrets["SECRET_KEY"],
        "INIT_TOKEN": secrets["INIT_TOKEN"],
        "FISCAL_ENCRYPTION_KEY": secrets["FISCAL_ENCRYPTION_KEY"],
        "WA_BRIDGE_URL": "http://wa-bridge:4100",
        "WA_BRIDGE_SECRET": secrets["WA_BRIDGE_SECRET"],
        "MK_BRIDGE_URL": "http://mk-bridge:4200",
        "MK_BRIDGE_SECRET": secrets["MK_BRIDGE_SECRET"],
        "VPN_HUB_URL": "http://vpn-hub:4300",
        "VPN_HUB_SECRET": secrets["VPN_HUB_SECRET"],
        # Demo: el frontend deshabilita acciones sensibles (vincular WhatsApp,
        # conectar/añadir Mikrotiks) y muestra un aviso. Solo true en demo.
        "DEMO_MODE": "true" if answers["env"] == "demo" else "false",
    })

    _write_env(config.ENV_DIR / "wa-bridge.env", {
        "PORT": "4100",
        "INTERNAL_SECRET": secrets["WA_BRIDGE_SECRET"],
        "WEBHOOK_URL": "http://server:4000/api/whatsapp/webhook/message",
    })

    _write_env(config.ENV_DIR / "mk-bridge.env", {
        "PORT": "4200",
        "INTERNAL_SECRET": secrets["MK_BRIDGE_SECRET"],
        "DRY": "false",
        "WG_CONFIG_DIR": "/etc/wireguard",
        "WG_REVERSE_LISTEN_PORT": answers["wg_port"],
        "WG_REVERSE_PUBLIC_ENDPOINT": answers["wg_endpoint"],
    })

    # vpn-hub: concentrador SSTP (Mikrotiks bajo CGNAT con RouterOS 6). El
    # SSTP escucha en 1443 (el 443 lo usa el proxy). SSTP_PUBLIC_HOST es el
    # dominio: resuelve al mismo VPS, y el sstp-client del Mikrotik se conecta
    # a <dominio>:1443. El pool de gestión usa el default del servicio.
    _write_env(config.ENV_DIR / "vpn-hub.env", {
        "PORT": "4300",
        "INTERNAL_SECRET": secrets["VPN_HUB_SECRET"],
        "DRY": "false",
        "SSTP_PUBLIC_HOST": domain,
        "SSTP_LISTEN_PORT": "1443",
    })

    # No es secreto, pero lo dejamos 600 por consistencia.
    _write_env(config.COMPOSE_ENV, {
        "TAG": answers["tag"],
        "DOMAIN": domain,
        "APP_ENV": answers["env"],
        "WG_REVERSE_LISTEN_PORT": answers["wg_port"],
        # Puerto TCP público del SSTP (el 443 lo usa el proxy). El compose lo
        # publica como ${SSTP_LISTEN_PORT:-1443}.
        "SSTP_LISTEN_PORT": "1443",
        # No lo usa compose; lo guardamos para el comando `cert` (recuperación).
        "CERTBOT_EMAIL": answers["email"],
    })

    render_nginx(domain)


def reconcile_service_envs() -> list:
    """Completa los env/*.env de los servicios bridge que falten y agrega en
    server.env los *_URL/*_SECRET que falten, generando SOLO los secrets
    ausentes y SIN tocar los existentes (preserva DB_PASSWORD, SECRET_KEY, etc.,
    o sea: ni la BD ni las sesiones del cliente se rompen).

    Pensado para droplets instalados antes de que el stack incorporara mk-bridge
    o vpn-hub: ahí el `git pull` trae un compose que ya referencia
    env/vpn-hub.env, pero ese archivo (y su secret) nunca se generó y el
    `up -d` aborta con 'env file ... not found'. Idempotente: en un droplet al
    día no escribe nada. Devuelve la lista de archivos creados/actualizados.
    """
    created = []
    server_path = config.ENV_DIR / "server.env"
    server = config.read_env_file(server_path)
    if not server:
        return created  # sin configurar; cmd_update ya valida antes que llegue acá

    compose = config.read_env_file(config.COMPOSE_ENV)
    domain = compose.get("DOMAIN", "")
    wg_port = compose.get("WG_REVERSE_LISTEN_PORT", "51820")
    server_changed = False

    def ensure_secret(key):
        nonlocal server_changed
        if not server.get(key):
            server[key] = secretgen.token(32)
            server_changed = True
        return server[key]

    def ensure_url(key, value):
        nonlocal server_changed
        if not server.get(key):
            server[key] = value
            server_changed = True

    # wa-bridge (suele existir; se respeta si ya está)
    wa_secret = ensure_secret("WA_BRIDGE_SECRET")
    ensure_url("WA_BRIDGE_URL", "http://wa-bridge:4100")
    wa_env = config.ENV_DIR / "wa-bridge.env"
    if not wa_env.exists():
        _write_env(wa_env, {
            "PORT": "4100",
            "INTERNAL_SECRET": wa_secret,
            "WEBHOOK_URL": "http://server:4000/api/whatsapp/webhook/message",
        })
        created.append("wa-bridge.env")

    # mk-bridge
    mk_secret = ensure_secret("MK_BRIDGE_SECRET")
    ensure_url("MK_BRIDGE_URL", "http://mk-bridge:4200")
    mk_env = config.ENV_DIR / "mk-bridge.env"
    if not mk_env.exists():
        _write_env(mk_env, {
            "PORT": "4200",
            "INTERNAL_SECRET": mk_secret,
            "DRY": "false",
            "WG_CONFIG_DIR": "/etc/wireguard",
            "WG_REVERSE_LISTEN_PORT": wg_port,
            "WG_REVERSE_PUBLIC_ENDPOINT": f"{domain}:{wg_port}" if domain else "",
        })
        created.append("mk-bridge.env")

    # vpn-hub
    vpn_secret = ensure_secret("VPN_HUB_SECRET")
    ensure_url("VPN_HUB_URL", "http://vpn-hub:4300")
    vpn_env = config.ENV_DIR / "vpn-hub.env"
    if not vpn_env.exists():
        _write_env(vpn_env, {
            "PORT": "4300",
            "INTERNAL_SECRET": vpn_secret,
            "DRY": "false",
            "SSTP_PUBLIC_HOST": domain,
            "SSTP_LISTEN_PORT": "1443",
        })
        created.append("vpn-hub.env")

    # DEMO_MODE deriva del entorno (APP_ENV de compose/.env). Lo agregamos solo
    # si falta, para que droplets instalados antes de esta feature lo tomen en
    # el próximo update; no pisamos un valor ya presente.
    if "DEMO_MODE" not in server:
        server["DEMO_MODE"] = (
            "true" if compose.get("APP_ENV") == "demo" else "false"
        )
        server_changed = True

    # TZ: droplets instalados antes de que la zona fuera config de proceso no la
    # tienen. La sembramos con el default (America/Lima) para no cambiarles el
    # comportamiento; quien quiera otra zona edita server.env (y db.env) y
    # reinicia. No pisamos un valor ya presente.
    if "TZ" not in server:
        server["TZ"] = "America/Lima"
        server_changed = True

    # CURRENCY: droplets instalados antes del soporte multi-moneda no la tienen.
    # La sembramos con el default (PEN) para no cambiarles el comportamiento;
    # quien quiera otra moneda edita server.env y reinicia. No pisamos un valor
    # ya presente.
    if "CURRENCY" not in server:
        server["CURRENCY"] = "PEN"
        server_changed = True

    if server_changed:
        config.write_env_file(server_path, server)
        created.append("server.env")

    return created


def render_nginx(domain: str) -> None:
    """Renderiza nginx/default.conf desde el template sustituyendo solo
    ${DOMAIN} (las demás $vars son de nginx). Se llama en init y en update
    para que cambios del template se apliquen sin re-init."""
    template = config.NGINX_TEMPLATE.read_text(encoding="utf-8")
    config.NGINX_CONF.write_text(
        template.replace("${DOMAIN}", domain), encoding="utf-8"
    )


def repoint_domain(new_domain: str) -> dict:
    """Reapunta el deployment a un dominio nuevo. Actualiza todo lo que deriva
    del dominio web, SIN tocar secrets ni la BD:
      - compose/.env: DOMAIN.
      - server.env: PUBLIC_BASE_URL y CORS_ORIGIN (sin esto el front en el
        dominio nuevo se bloquea por CORS y el link del Chat Node queda viejo).
      - vpn-hub.env: SSTP_PUBLIC_HOST — solo si seguía igual al dominio viejo
        (no pisar un host puesto a mano).
      - mk-bridge.env: WG_REVERSE_PUBLIC_ENDPOINT — el endpoint WG es
        independiente (puede ser una IP cruda), así que solo se reapunta si su
        host era el dominio viejo.
    Re-renderiza nginx. NO toca certbot ni contenedores (eso lo hace el comando
    `domain`). Devuelve {old, changed:[servicios cuyo env cambió y hay que
    recrear]}.
    """
    base_url = f"https://{new_domain}"
    compose = config.read_env_file(config.COMPOSE_ENV)
    old_domain = compose.get("DOMAIN", "")
    changed = []

    compose["DOMAIN"] = new_domain
    config.write_env_file(config.COMPOSE_ENV, compose)

    server_path = config.ENV_DIR / "server.env"
    server = config.read_env_file(server_path)
    if server:
        server["PUBLIC_BASE_URL"] = base_url
        server["CORS_ORIGIN"] = base_url
        config.write_env_file(server_path, server)
        changed.append("server")

    vpn_path = config.ENV_DIR / "vpn-hub.env"
    vpn = config.read_env_file(vpn_path)
    if vpn and vpn.get("SSTP_PUBLIC_HOST", "") == old_domain:
        vpn["SSTP_PUBLIC_HOST"] = new_domain
        config.write_env_file(vpn_path, vpn)
        changed.append("vpn-hub")

    mk_path = config.ENV_DIR / "mk-bridge.env"
    mk = config.read_env_file(mk_path)
    if mk:
        ep = mk.get("WG_REVERSE_PUBLIC_ENDPOINT", "")
        host, sep, port = ep.rpartition(":")
        if ep == old_domain:  # endpoint sin puerto
            mk["WG_REVERSE_PUBLIC_ENDPOINT"] = new_domain
            config.write_env_file(mk_path, mk)
            changed.append("mk-bridge")
        elif sep and host == old_domain:  # host:puerto
            mk["WG_REVERSE_PUBLIC_ENDPOINT"] = f"{new_domain}:{port}"
            config.write_env_file(mk_path, mk)
            changed.append("mk-bridge")

    render_nginx(new_domain)
    return {"old": old_domain, "changed": changed}
