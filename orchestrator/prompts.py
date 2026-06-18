"""Preguntas interactivas del `init`. Los secrets NO se preguntan: se autogeneran."""

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
        "tag": tag,
        "ghcr_user": ghcr_user,
        "ghcr_token": ghcr_token,
        "wg_port": wg_port,
        "wg_endpoint": wg_endpoint,
    }
