"""Entra a EFFI con usuario y contraseña y devuelve la cookie ci_session."""

from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup

from effi_clientes import CLIENTES_PATH, EffiError

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)


def entrar(email: str, password: str, base_url: str = "https://effi.com.co") -> str:
    """Inicia sesión en EFFI y devuelve ci_session.

    Lanza EffiError con un mensaje concreto si la clave, el usuario,
    la red o la respuesta de EFFI no dejan entrar.
    """
    email = (email or "").strip()
    password = password or ""
    base = (base_url or "https://effi.com.co").rstrip("/")
    if not email or not password:
        raise EffiError("SIN_CLAVE", "Falta el usuario o la contraseña de EFFI.")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es",
        }
    )

    try:
        page = session.get(f"{base}/ingreso", timeout=30)
    except requests.Timeout as exc:
        raise EffiError(
            "TIMEOUT",
            "EFFI tardó demasiado al abrir el ingreso.",
        ) from exc
    except requests.ConnectionError as exc:
        raise EffiError(
            "SIN_RED",
            "No se pudo conectar con EFFI para entrar. Revisa tu internet.",
        ) from exc
    except requests.RequestException as exc:
        raise EffiError("RED", f"Error de red al entrar a EFFI: {exc}") from exc

    token = _token(page.text)
    if not token:
        raise EffiError(
            "LOGIN",
            "EFFI no entregó el formulario de ingreso. Inténtalo de nuevo en un momento.",
        )

    resp = _post_login(session, base, email, password, token, extra=None)
    resp = _elegir_empresa_si_pide(session, resp, base, email, password)

    aviso = _aviso(resp.text)
    if _es_login(resp):
        if aviso:
            raise EffiError(_clasificar(aviso), aviso)
        raise EffiError(
            "LOGIN",
            "EFFI no dejó entrar y no explicó el motivo. Revisa usuario y contraseña.",
        )

    try:
        check = session.get(f"{base}{CLIENTES_PATH}", timeout=30)
    except requests.Timeout as exc:
        raise EffiError(
            "TIMEOUT",
            "EFFI aceptó el ingreso, pero tardó al confirmar la sesión.",
        ) from exc
    except requests.RequestException as exc:
        raise EffiError("RED", f"Error de red al confirmar la sesión: {exc}") from exc

    if _es_login(check):
        aviso = _aviso(check.text) or _aviso(resp.text)
        if aviso:
            raise EffiError(_clasificar(aviso), aviso)
        raise EffiError(
            "LOGIN",
            "EFFI no dejó una sesión válida después de entrar.",
        )

    cookie = session.cookies.get("ci_session") or ""
    if not cookie:
        raise EffiError("LOGIN", "EFFI no devolvió la cookie de sesión.")
    return cookie


def _post_login(session, base, email, password, token, extra):
    data = {
        "email": email,
        "password": password,
        "token": token,
        "email_no_verificado": "",
        "password_encrypt": "",
    }
    if extra:
        data.update(extra)
    try:
        return session.post(
            f"{base}/ingreso/validar_usuario",
            data=data,
            timeout=30,
            allow_redirects=True,
        )
    except requests.Timeout as exc:
        raise EffiError("TIMEOUT", "EFFI tardó demasiado al validar el ingreso.") from exc
    except requests.ConnectionError as exc:
        raise EffiError(
            "SIN_RED",
            "Se perdió la conexión con EFFI durante el ingreso.",
        ) from exc
    except requests.RequestException as exc:
        raise EffiError("RED", f"Error de red al validar el ingreso: {exc}") from exc


def _elegir_empresa_si_pide(session, resp, base, email, password):
    """Si EFFI pide empresa y solo hay una, la elige. Si hay varias, avisa."""
    if not _es_login(resp):
        return resp
    soup = BeautifulSoup(resp.text, "lxml")
    elegido = None
    opciones: list[tuple[str, str]] = []
    nombre_campo = ""
    for select in soup.select("select"):
        blob = f"{select.get('name') or ''} {select.get('id') or ''}".lower()
        if "empresa" not in blob:
            continue
        encontradas = []
        for opt in select.select("option"):
            valor = (opt.get("value") or "").strip()
            etiqueta = opt.get_text(" ", strip=True)
            if not valor or valor in {"0", "-1"}:
                continue
            encontradas.append((valor, etiqueta or valor))
        if encontradas:
            nombre_campo = select.get("name") or ""
            opciones = encontradas
            break
    if not nombre_campo:
        return resp
    if len(opciones) > 1:
        nombres = ", ".join(etiqueta for _, etiqueta in opciones[:8])
        raise EffiError(
            "EMPRESA",
            "EFFI pidió elegir empresa y hay varias: "
            f"{nombres}. Entra una vez en el navegador o pega la cookie.",
        )
    elegido = opciones[0][0]
    token = _token(resp.text)
    if not token:
        raise EffiError("LOGIN", "EFFI pidió la empresa, pero no entregó un formulario válido.")
    return _post_login(
        session,
        base,
        email,
        password,
        token,
        extra={nombre_campo: elegido},
    )


def _token(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    field = soup.select_one('form#login input[name="token"]')
    if field and field.get("value"):
        return field["value"].strip()
    match = re.search(r'name="token"\s+value="([^"]+)"', html)
    return match.group(1).strip() if match else ""


def _aviso(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for sel in ("#validacion_alert", ".alert", ".callout-danger", ".text-danger"):
        node = soup.select_one(sel)
        if not node:
            continue
        text = node.get_text(" ", strip=True)
        if text:
            return text[:400]
    return ""


def _es_login(resp: requests.Response) -> bool:
    url = (resp.url or "").lower()
    html = (resp.text or "").lower()
    if any(x in url for x in ("/ingreso", "validar_usuario")):
        if 'id="login"' in html or 'name="password"' in html:
            return True
    markers = (
        'id="login"',
        "effi erp | ingreso",
        "validar_usuario",
        "email de usuario",
    )
    return sum(1 for marker in markers if marker in html) >= 2


def _clasificar(aviso: str) -> str:
    text = aviso.lower()
    if any(x in text for x in ("captcha", "recaptcha", "robot", "no soy un robot")):
        return "CAPTCHA"
    if any(x in text for x in ("contraseña", "contrasena", "clave")) and any(
        x in text for x in ("incorrect", "inválid", "invalid", "no es", "erróne", "errone")
    ):
        return "CLAVE"
    if any(x in text for x in ("no coincide", "no existe", "ninguna cuenta", "no está registr")):
        return "USUARIO"
    if "contraseña" in text or "contrasena" in text or "clave incorrecta" in text:
        return "CLAVE"
    return "LOGIN"
