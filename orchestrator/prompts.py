"""Preguntas interactivas del `init`. Los secrets NO se preguntan: se autogeneran.

También expone `answers_from_env()`: la misma estructura de respuestas pero leída
de variables de entorno, para el modo DESATENDIDO (`init --unattended`) que usan
cloud-init / CI cuando el panel aprovisiona un droplet sin intervención humana."""

import os
from getpass import getpass


def _ask(label, default=None, required=True):
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"  {label}{suffix}: ").strip()
        if not value and default is not None:
            return default
        if value or not required:
            return value
        print("    (requerido)")


def ask_init() -> dict:
    print("\n== Configuración de la instalación de Wisnee ==\n")

    env = ""
    while env not in ("prod", "demo"):
        env = _ask("Entorno (prod/demo)", "prod").lower()

    domain = _ask("Dominio público (ej. panel.tu-isp.com)")
    email = _ask("Email para Let's Encrypt (avisos de expiración)")
    # Zona horaria de la operación (IANA). Define la hora de los crons, el
    # cálculo de períodos/vencimientos y el display de todas las fechas. Se fija
    # como TZ del proceso (server + Postgres). Cambiarla luego = editar el env y
    # reiniciar; no es algo que el operador toque a diario.
    timezone = _ask(
        "Zona horaria de la operación (IANA, ej. America/Lima, America/Santiago)",
        "America/Lima",
    )
    # Moneda de la instalación (ISO 4217). Define el símbolo y el formato de TODOS
    # los montos (UI, recibos, reportes). Va como CURRENCY del proceso, como la
    # zona horaria. Soportadas: PEN, USD, MXN, COP, ARS, CLP, BOB, PYG, UYU, VES,
    # GTQ, HNL, NIO, CRC, DOP, PAB, CUP, EUR. Cambiarla luego solo cambia cómo se
    # muestran los montos (no convierte valores ya guardados).
    currency = _ask(
        "Moneda (ISO 4217, ej. PEN, USD, MXN, COP, CLP, ARS)",
        "PEN",
    ).upper()
    # Licencia de Materis (opcional). El CRM consulta su estado contra Materis y
    # SOLO INFORMA: sin clave corre como "sin licencia" y no se bloquea nada. La
    # clave es por suscripción (la entrega Materis); se puede dejar vacía acá y
    # cargarla luego en env/server.env. URL y slug usan sus defaults de prod.
    materis_license_key = _ask(
        "Clave de licencia Materis (opcional, Enter para omitir)",
        "", required=False,
    )
    # `edge` = última main (rolling, ideal para demo). `vX.Y.Z` = release
    # inmutable y coherente (recomendado para prod). Cambiable luego con
    # `./wisnee update --tag <tag>`.
    tag = _ask("Tag de imágenes (edge=rolling / vX.Y.Z=release)",
               "edge" if env == "demo" else "latest")

    print("\n  -- Acceso a GHCR (imágenes privadas) --")
    ghcr_user = _ask("Usuario de GitHub")
    ghcr_token = getpass("  Token GHCR con read:packages (oculto): ").strip()

    wg_port, wg_endpoint = "51820", ""
    if env == "prod":
        print("\n  -- WireGuard reverso (Mikrotiks bajo CGNAT) --")
        wg_port = _ask("Puerto UDP del WireGuard reverso", "51820")
        wg_endpoint = _ask(
            "Endpoint público del bridge (host:port que pondrán los Mikrotiks)",
            f"{domain}:{wg_port}",
        )

    return {
        "env": env,
        "domain": domain,
        "email": email,
        "timezone": timezone,
        "currency": currency,
        "materis_license_key": materis_license_key,
        "tag": tag,
        "ghcr_user": ghcr_user,
        "ghcr_token": ghcr_token,
        "wg_port": wg_port,
        "wg_endpoint": wg_endpoint,
    }


def answers_from_env() -> dict:
    """Mismas respuestas que `ask_init()` pero desde variables de entorno (modo
    desatendido). Las requeridas sin valor abortan con exit != 0 (cloud-init lo
    deja en su log). Variables:

      WISNEE_ENV (prod|demo, default prod), WISNEE_DOMAIN*, WISNEE_EMAIL*,
      WISNEE_TIMEZONE (default America/Lima), WISNEE_CURRENCY (default PEN),
      WISNEE_TAG (default edge en demo / latest en prod), GHCR_USER*, GHCR_TOKEN*,
      WISNEE_WG_PORT (default 51820), WISNEE_WG_ENDPOINT (default <domain>:<port>).
      (* = requerida)
    """

    def need(name: str) -> str:
        value = (os.environ.get(name) or "").strip()
        if not value:
            raise SystemExit(f"\n✖ Falta la variable de entorno requerida: {name}")
        return value

    def opt(name: str, default: str = "") -> str:
        value = (os.environ.get(name) or "").strip()
        return value if value else default

    env = opt("WISNEE_ENV", "prod").lower()
    if env not in ("prod", "demo"):
        raise SystemExit("\n✖ WISNEE_ENV debe ser 'prod' o 'demo'.")

    domain = need("WISNEE_DOMAIN").lower().rstrip(".")
    email = need("WISNEE_EMAIL")
    timezone = opt("WISNEE_TIMEZONE", "America/Lima")
    currency = opt("WISNEE_CURRENCY", "PEN").upper()
    # Licencia de Materis (opcional, por suscripción). La inyecta el panel al
    # aprovisionar; vacía = "sin licencia" (solo informa, no bloquea).
    materis_license_key = opt("WISNEE_MATERIS_LICENSE_KEY", "")
    tag = opt("WISNEE_TAG", "edge" if env == "demo" else "latest")
    ghcr_user = need("GHCR_USER")
    ghcr_token = need("GHCR_TOKEN")

    wg_port, wg_endpoint = "51820", ""
    if env == "prod":
        wg_port = opt("WISNEE_WG_PORT", "51820")
        wg_endpoint = opt("WISNEE_WG_ENDPOINT", f"{domain}:{wg_port}")

    return {
        "env": env,
        "domain": domain,
        "email": email,
        "timezone": timezone,
        "currency": currency,
        "materis_license_key": materis_license_key,
        "tag": tag,
        "ghcr_user": ghcr_user,
        "ghcr_token": ghcr_token,
        "wg_port": wg_port,
        "wg_endpoint": wg_endpoint,
    }
