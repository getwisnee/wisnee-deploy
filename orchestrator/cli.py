"""CLI del orquestador. Comandos: init, update, domain, seed, logs, status,
backup, cert, credentials."""

import argparse
import datetime
import os
import re
import subprocess
import sys

from . import config, credentials, prompts, render, runner, secretgen


def _die(msg: str, code: int = 1):
    print(f"\n✖ {msg}", file=sys.stderr)
    sys.exit(code)


def _require_configured() -> dict:
    cfg = config.read_env_file(config.COMPOSE_ENV)
    if not cfg or not (config.ENV_DIR / "server.env").exists():
        _die("No está configurado todavía. Corré primero: sudo ./wisnee init")
    return cfg


# ---- comandos ----

def cmd_init(args):
    unattended = getattr(args, "unattended", False)

    if (config.ENV_DIR / "server.env").exists() and not args.force:
        if unattended:
            # cloud-init puede re-correr: ya configurado = idempotente, exit 0.
            print("✔ Ya configurado (existe env/server.env); nada que hacer.")
            return
        _die("Ya está configurado (existe env/server.env). Para actualizar usá "
             "`./wisnee update`. Para reconfigurar desde cero, `init --force` "
             "(¡regenera secrets y rompe la BD existente!).")

    answers = prompts.answers_from_env() if unattended else prompts.ask_init()
    secrets = secretgen.generate()
    # En desatendido el INIT_TOKEN puede venir pre-fijado (WISNEE_INIT_TOKEN) para
    # que el orquestador (panel) lo conozca y pueda llamar al setup del CRM sin
    # leer el credentials.txt del server.
    if unattended:
        forced_token = (os.environ.get("WISNEE_INIT_TOKEN") or "").strip()
        if forced_token:
            secrets["INIT_TOKEN"] = forced_token

    print("\n→ Generando configuración y secrets…")
    render.render(answers, secrets)

    if not args.skip_provision:
        print("\n→ Provisioning del sistema (Ansible)…")
        runner.ensure_ansible()
        runner.ansible_provision({
            "ghcr_username": answers["ghcr_user"],
            "ghcr_token": answers["ghcr_token"],
            "harden_ssh": not args.no_harden,
        })

    if answers["ghcr_token"]:
        try:
            runner.docker_login(answers["ghcr_user"], answers["ghcr_token"])
        except subprocess.CalledProcessError:
            _die(
                "Falló el login a GHCR. El token debe ser un PAT *classic* de "
                f"GitHub con scope read:packages, sin vencer, del usuario "
                f"'{answers['ghcr_user']}'. Probalo a mano:\n"
                f"  docker login ghcr.io -u {answers['ghcr_user']}\n"
                "Luego reintentá: ./wisnee init --force"
            )

    env = answers["env"]
    print("\n→ Bajando imágenes de GHCR…")
    runner.compose(["pull"], env)

    print("\n→ Levantando el stack…")
    runner.dummy_cert(answers["domain"])
    runner.compose(["up", "-d"], env)

    print("\n→ Emitiendo certificado TLS…")
    try:
        runner.certbot_issue(answers["domain"], answers["email"], env)
        runner.nginx_reload(env)
    except subprocess.CalledProcessError as e:
        print(f"\n⚠ No se pudo emitir el certificado ({e}).")
        print(f"  Verificá que el DNS de {answers['domain']} apunte a este "
              f"server y reintentá: ./wisnee cert")
        runner.dummy_cert(answers["domain"])  # que nginx siga arriba

    path = credentials.write(answers, secrets)
    print(f"\n✔ Listo. Credenciales en: {path}")
    print(f"  Setup:  https://{answers['domain']}/?token={secrets['INIT_TOKEN']}")


_TAG_VARS = {"tag": "TAG", "server": "SERVER_TAG", "web": "WEB_TAG",
             "wa": "WA_TAG", "mk": "MK_TAG"}


def cmd_update(args):
    cfg = _require_configured()

    # Overrides de tag por servicio: persistirlos en compose/.env antes de
    # pull/up. Sin overrides, recrea con los tags actuales (comportamiento previo).
    overrides = {_TAG_VARS[k]: getattr(args, k) for k in _TAG_VARS
                 if getattr(args, k, None)}
    # Un `--tag` global expresa "todos los servicios a esta versión": limpia los
    # pines por-servicio (WEB_TAG/SERVER_TAG/...) que hayan quedado de un deploy
    # `--web`/`--server` previo, salvo los que se vuelvan a pasar explícito ahora.
    # Sin esto, el compose `${WEB_TAG:-${TAG:-latest}}` deja el pin viejo ganando
    # sobre `--tag` y un servicio se queda atrás en la versión anterior.
    if args.tag:
        for var in ("SERVER_TAG", "WEB_TAG", "WA_TAG", "MK_TAG"):
            if var not in overrides:
                cfg.pop(var, None)
    if overrides or args.tag:
        cfg.update(overrides)
        config.write_env_file(config.COMPOSE_ENV, cfg)
        print("→ Tags fijados: " + ", ".join(f"{k}={v}" for k, v in cfg.items()
                                              if k in _TAG_VARS.values()))

    env = cfg.get("APP_ENV", "prod")

    # Reconciliar env/secrets de servicios que el stack agregó después de la
    # instalación de este droplet (mk-bridge, vpn-hub): crea los env faltantes
    # sin tocar los secrets/datos existentes. Sin esto, un droplet viejo aborta
    # el `up -d` con 'env file env/vpn-hub.env not found'.
    created = render.reconcile_service_envs()
    if created:
        print("→ Config de servicios reconciliada: " + ", ".join(created))

    # Re-renderizar nginx por si cambió el template (p. ej. sub_filter de
    # og:image). Es idempotente y barato.
    domain = cfg.get("DOMAIN")
    if domain:
        render.render_nginx(domain)

    # Poda ANTES del pull: en un droplet chico las versiones viejas llenan el
    # disco y el pull falla con "no space left on device". Borrar imágenes sin
    # usar libera ese espacio sin tocar volúmenes ni la versión en ejecución.
    print("→ Liberando espacio (imágenes sin usar)…")
    runner.docker_prune()
    print("→ Bajando imágenes nuevas…")
    runner.compose(["pull"], env)
    print("→ Aplicando (migrate one-shot corre antes del server)…")
    runner.compose(["up", "-d"], env)

    # Aplicar el nginx re-renderizado (el bind mount ya está; basta un reload).
    if domain:
        try:
            runner.nginx_reload(env)
        except subprocess.CalledProcessError:
            pass  # el proxy se recrea con up -d si hiciera falta

    print("✔ Actualizado.")


def cmd_seed(args):
    cfg = _require_configured()
    env = cfg.get("APP_ENV", "")
    if env != "demo":
        _die("El seed solo corre en entorno Demo (APP_ENV=demo). Resetea la BD.")
    if not args.yes:
        ans = input("Esto BORRA la base y siembra datos demo. ¿Seguir? (escribí 'demo'): ")
        if ans.strip() != "demo":
            _die("Cancelado.")
    # SEED_FORCE=1: el comando explícito SÍ resetea. El seed automático del
    # `up -d` corre sin esta variable → es seed-once (no borra si ya hay datos),
    # para que un `update` no destruya lo creado a mano en la demo.
    runner.compose(["run", "--rm", "-e", "SEED_FORCE=1", "seed"], env)
    print("✔ Seed aplicado.")


def cmd_logs(args):
    cfg = _require_configured()
    env = cfg.get("APP_ENV", "prod")
    runner.compose(["logs", "-f", "--tail", "200"] + args.service, env)


def cmd_status(args):
    cfg = _require_configured()
    runner.compose(["ps"], cfg.get("APP_ENV", "prod"))


def cmd_cert(args):
    cfg = _require_configured()
    domain = cfg.get("DOMAIN")
    email = cfg.get("CERTBOT_EMAIL", "")
    env = cfg.get("APP_ENV", "prod")
    runner.dummy_cert(domain)
    runner.compose(["up", "-d", "proxy"], env)
    runner.certbot_issue(domain, email, env)
    runner.nginx_reload(env)
    print("✔ Certificado emitido/renovado.")


# FQDN simple (incluye subdominios tipo cliente.wisnee.com). Sin esquema ni path.
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9](-*[a-z0-9])*\.)+[a-z]{2,}$"
)


def cmd_domain(args):
    """Cambia el dominio del deployment de punta a punta: actualiza los env que
    derivan del dominio (PUBLIC_BASE_URL/CORS, SSTP, WG), re-renderiza nginx,
    re-emite el certificado TLS y recrea los servicios afectados. Un cliente por
    deploy: pensado para mover un cliente a su `cliente.wisnee.com`."""
    cfg = _require_configured()
    new = args.domain.strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(new):
        _die(f"Dominio inválido: '{new}'. Usá un FQDN, ej. cliente.wisnee.com "
             "(sin http:// ni barra final).")

    old = cfg.get("DOMAIN", "")
    if new == old:
        _die(f"El dominio ya es {new}. Para re-emitir el certificado: ./wisnee cert")

    email = cfg.get("CERTBOT_EMAIL", "")
    env = cfg.get("APP_ENV", "prod")

    if not args.yes:
        print(f"\nCambiar dominio:  {old or '(ninguno)'}  →  {new}")
        print(f"  El A record de {new} TIENE que apuntar ya a ESTE server; si no,")
        print("  la emisión del certificado (validación HTTP) va a fallar.")
        ans = input("  Para confirmar, escribí el dominio nuevo: ")
        if ans.strip().lower() != new:
            _die("Cancelado.")

    print("\n→ Actualizando configuración (env + nginx)…")
    info = render.repoint_domain(new)

    print("→ Placeholder TLS + recreando proxy…")
    runner.dummy_cert(new)
    runner.compose(["up", "-d", "--force-recreate", "proxy"], env)

    print("→ Emitiendo certificado real…")
    try:
        runner.certbot_issue(new, email, env)
        runner.nginx_reload(env)
    except subprocess.CalledProcessError as e:
        print(f"\n⚠ No se pudo emitir el certificado ({e}).")
        print(f"  Verificá el DNS de {new} y reintentá: ./wisnee cert")
        print("  (el sitio sigue arriba con un certificado temporal.)")

    # Recrear los servicios que leen el dominio por env (compose no recrea solo
    # cuando cambia el contenido de un env_file). server siempre; los bridges
    # solo en prod, donde existen.
    recreate = ["server"]
    if env == "prod":
        recreate += [s for s in ("vpn-hub", "mk-bridge") if s in info["changed"]]
    print(f"→ Recreando servicios: {', '.join(recreate)}…")
    runner.compose(["up", "-d", "--force-recreate"] + recreate, env)

    print(f"\n✔ Dominio actualizado a {new}.")
    print(f"  Probá: https://{new}/")
    if old:
        print(f"  (El cert viejo de {old} queda guardado; el auto-renew lo "
              "ignorará una vez que el DNS deje de apuntar acá.)")


def cmd_backup(args):
    cfg = _require_configured()
    env = cfg.get("APP_ENV", "prod")
    db = config.read_env_file(config.ENV_DIR / "db.env")
    user = db.get("POSTGRES_USER", config.DB_USER)
    name = db.get("POSTGRES_DB", config.DB_NAME)
    config.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = config.BACKUPS_DIR / f"wisnee-{ts}.sql"
    argv = runner.compose_argv(env) + ["exec", "-T", "db", "pg_dump", "-U", user, name]
    print(f"→ pg_dump → {out}")
    with open(out, "wb") as fh:
        subprocess.run(argv, stdout=fh, check=True)
    print(f"✔ Backup: {out}")


def cmd_credentials(args):
    if config.CREDENTIALS_PATH.exists():
        print(config.CREDENTIALS_PATH.read_text(encoding="utf-8"))
    else:
        _die(f"No existe {config.CREDENTIALS_PATH} (¿se reinició el server? /tmp se borra).")


def build_parser():
    p = argparse.ArgumentParser(prog="wisnee", description="Orquestador de despliegue de Wisnee")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("init", help="Configura y levanta el stack por primera vez")
    pi.add_argument("--force", action="store_true", help="Reconfigurar (REGENERA secrets)")
    pi.add_argument("--skip-provision", action="store_true", help="No correr Ansible (sistema ya provisto)")
    pi.add_argument("--no-harden", action="store_true", help="No endurecer SSH a key-only")
    pi.add_argument("--unattended", action="store_true",
                    help="Sin prompts: lee la config de variables de entorno "
                         "(WISNEE_DOMAIN, WISNEE_EMAIL, GHCR_USER/TOKEN, …). "
                         "Para cloud-init/CI.")
    pi.set_defaults(func=cmd_init)

    pu = sub.add_parser("update", help="Baja imágenes nuevas, migra y recrea")
    pu.add_argument("--tag", help="Fija el TAG global de todos los servicios")
    pu.add_argument("--server", help="Fija solo el tag de wisnee-server")
    pu.add_argument("--web", help="Fija solo el tag del frontend (wisnee)")
    pu.add_argument("--wa", help="Fija solo el tag de wa-bridge")
    pu.add_argument("--mk", help="Fija solo el tag de mk-bridge")
    pu.set_defaults(func=cmd_update)

    pd = sub.add_parser("domain", help="Cambia el dominio del deployment (re-renderiza nginx y re-emite el cert)")
    pd.add_argument("domain", help="Nuevo dominio o subdominio (ej. cliente.wisnee.com)")
    pd.add_argument("--yes", action="store_true", help="Sin confirmación")
    pd.set_defaults(func=cmd_domain)

    ps = sub.add_parser("seed", help="(Demo) resetea la BD y siembra datos demo")
    ps.add_argument("--yes", action="store_true", help="Sin confirmación")
    ps.set_defaults(func=cmd_seed)

    pl = sub.add_parser("logs", help="Sigue los logs")
    pl.add_argument("service", nargs="*", help="Servicio(s) (vacío = todos)")
    pl.set_defaults(func=cmd_logs)

    sub.add_parser("status", help="Estado de los servicios").set_defaults(func=cmd_status)
    sub.add_parser("cert", help="Emitir/renovar el certificado TLS").set_defaults(func=cmd_cert)
    sub.add_parser("backup", help="pg_dump de la base").set_defaults(func=cmd_backup)
    sub.add_parser("credentials", help="Muestra el credentials.txt").set_defaults(func=cmd_credentials)

    return p


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except subprocess.CalledProcessError as e:
        _die(f"Falló un comando (exit {e.returncode}).")
    except KeyboardInterrupt:
        _die("Cancelado.", 130)
