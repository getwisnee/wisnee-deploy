"""Consulta a GHCR (Docker Registry v2 API) por HTTP, SIN depender de docker.

Existe para que `init` pueda validar el tag ANTES de provisionar la máquina: en
un droplet limpio todavía no hay docker (lo instala Ansible), así que no se puede
usar `docker manifest inspect`. Con esto el error de "tag inexistente" aparece a
los segundos, en vez de después de instalar Docker, swap, UFW y fail2ban.

Criterio: solo se afirma que una imagen FALTA ante un 404 explícito. Todo lo
demás es DESCONOCIDO, no "está" — un falso positivo bloquearía una instalación
válida, pero decir "verificado" sin haber podido mirar es peor: el operador
confía en un chequeo que no ocurrió. Por eso el estado es de tres valores y el
CLI informa cuántas no se pudieron verificar.

Ojo con los permisos: ante un token SIN `read:packages`, GHCR responde 403 a
todo (no filtra si el repo existe), así que no se puede verificar nada. Con el
PAT correcto —el que el propio `init` exige— responde 404 para lo inexistente.
"""

import base64
import json
import urllib.error
import urllib.request

REGISTRY = "ghcr.io"
ORG = "getwisnee"

TIMEOUT = 15

# Imágenes propias que baja cada entorno. Las públicas (postgres, nginx,
# certbot) no se verifican: no dependen de nuestros releases.
CORE_IMAGES = ["wisnee-server", "wisnee"]
PROD_IMAGES = ["wa-bridge", "wa-bridge-win-dist", "mk-bridge", "vpn-hub"]

_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def expected_images(env_name: str, tag: str) -> list:
    """(repo, tag) que el compose de este entorno va a intentar bajar."""
    images = [(name, tag) for name in CORE_IMAGES]
    if env_name == "prod":
        images += [(name, tag) for name in PROD_IMAGES]
    elif env_name == "demo":
        # El seed vive en el mismo repo del server, con sufijo.
        images.append(("wisnee-server", f"{tag}-seed"))
    return images


def _pull_token(repo: str, user: str, token: str) -> str:
    """Token de solo-pull para un repo. Con credenciales alcanza a los privados."""
    url = (f"https://{REGISTRY}/token?service={REGISTRY}"
           f"&scope=repository:{ORG}/{repo}:pull")
    req = urllib.request.Request(url)
    if user and token:
        basic = base64.b64encode(f"{user}:{token}".encode()).decode()
        req.add_header("Authorization", f"Basic {basic}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp).get("token", "")


def image_status(repo: str, tag: str, user: str, token: str):
    """`True` = está, `False` = NO está (404 explícito), `None` = no se pudo
    averiguar (sin red, sin permisos, registry caído)."""
    try:
        bearer = _pull_token(repo, user, token)
    except (urllib.error.URLError, ValueError, OSError):
        return None

    req = urllib.request.Request(
        f"https://{REGISTRY}/v2/{ORG}/{repo}/manifests/{tag}", method="HEAD")
    req.add_header("Authorization", f"Bearer {bearer}")
    req.add_header("Accept", _ACCEPT)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT):
            return True
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except (urllib.error.URLError, OSError):
        return None


def check_images(env_name: str, tag: str, user: str, token: str):
    """(faltantes, no_verificadas) para este entorno+tag, como 'repo:tag'."""
    missing, unknown = [], []
    for repo, t in expected_images(env_name, tag):
        status = image_status(repo, t, user, token)
        if status is False:
            missing.append(f"{repo}:{t}")
        elif status is None:
            unknown.append(f"{repo}:{t}")
    return missing, unknown
