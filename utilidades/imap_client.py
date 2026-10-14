"""Prueba de conexión IMAP con mensajes claros."""

from __future__ import annotations

import imaplib
import re
import socket
import ssl


def _quote_carpeta(carpeta: str) -> str:
    """Cita la carpeta IMAP si tiene espacios o caracteres especiales."""
    name = (carpeta or "INBOX").strip() or "INBOX"
    if name.upper() == "INBOX":
        return "INBOX"
    # Ya viene entre comillas
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        return name
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    if re.search(r'[\s"\\(){%*]', name) or name != name.encode("ascii", "ignore").decode():
        return f'"{escaped}"'
    return name


def _texto_exc(exc: BaseException) -> str:
    raw = str(exc)
    # Bytes embebidos: b'Could not parse command' → Could not parse command
    raw = re.sub(r"b'([^']*)'", r"\1", raw)
    raw = re.sub(r'b"([^"]*)"', r"\1", raw)
    return raw.strip()


def _mensaje_amigable(exc: BaseException, *, carpeta: str = "") -> str:
    """Traduce errores típicos de IMAP a mensajes entendibles."""
    t = _texto_exc(exc).lower()

    # Autenticación / contraseña
    if any(
        k in t
        for k in (
            "authenticationfailed",
            "invalid credentials",
            "auth failed",
            "login failed",
            "authenticate failed",
            "username and password not accepted",
            "invalid user name or password",
            "incorrect password",
            "authentication failed",
        )
    ):
        return (
            "Usuario o contraseña incorrectos. "
            "Si usas Gmail, genera una “contraseña de aplicación” "
            "(con verificación en 2 pasos activada)."
        )

    if any(
        k in t
        for k in (
            "application-specific password",
            "app password",
            "web login required",
            "please log in via your web browser",
            "not enabled for imap",
            "imap is disabled",
        )
    ):
        return (
            "Gmail rechazó el acceso. Activa IMAP en la cuenta y usa "
            "una contraseña de aplicación (no la contraseña normal)."
        )

    if "too many" in t or "rate" in t or ("temporarily" in t and "lock" in t):
        return "Demasiados intentos. Espera unos minutos e inténtalo de nuevo."

    # Carpeta
    if any(
        k in t
        for k in (
            "examine",
            "select",
            "could not parse command",
            "unknown mailbox",
            "doesn't exist",
            "does not exist",
            "no such mailbox",
            "mailbox does not exist",
            "nonexistent",
        )
    ):
        nombre = carpeta or "indicada"
        return (
            f"No se pudo abrir la carpeta “{nombre}”. "
            "Revisa el nombre exacto (mayúsculas/espacios) o usa INBOX."
        )

    # Red / host / puerto / SSL
    if any(k in t for k in ("timed out", "timeout", "time out")):
        return (
            "El servidor no respondió a tiempo. "
            "Revisa host, puerto (993) e internet."
        )

    if any(
        k in t
        for k in (
            "name or service not known",
            "getaddrinfo",
            "nodename nor servname",
            "no address associated",
            "temporary failure in name resolution",
        )
    ):
        return "No se encontró el servidor. Revisa el host (ej. imap.gmail.com)."

    if any(
        k in t
        for k in (
            "connection refused",
            "actively refused",
            "errno 10061",
            "errno 111",
        )
    ):
        return (
            "Conexión rechazada. El puerto puede estar mal "
            "(usa 993 con SSL) o el servidor no acepta IMAP."
        )

    if any(
        k in t
        for k in (
            "network is unreachable",
            "no route to host",
            "unreachable",
        )
    ):
        return "Sin ruta al servidor. Revisa tu conexión a internet."

    if any(
        k in t
        for k in (
            "ssl",
            "certificate",
            "handshake",
            "wrong version number",
            "unexpected_eof",
        )
    ):
        return (
            "Error de SSL/TLS. Usa puerto 993 y comprueba que el host sea correcto."
        )

    if "eof" in t or "connection reset" in t or "broken pipe" in t:
        return "El servidor cerró la conexión. Revisa host, puerto y contraseña."

    # Genérico limpio
    corto = _texto_exc(exc)
    if len(corto) > 160:
        corto = corto[:157] + "…"
    if corto:
        return f"No se pudo conectar: {corto}"
    return "No se pudo conectar al correo. Revisa los datos e inténtalo de nuevo."


def probar_imap(
    host: str,
    port: int,
    usuario: str,
    password: str,
    carpeta: str = "INBOX",
    usar_ssl: bool = True,
    timeout: float = 12.0,
) -> tuple[bool, str]:
    host = (host or "").strip()
    usuario = (usuario or "").strip()
    password = password or ""
    carpeta_ui = (carpeta or "INBOX").strip() or "INBOX"
    carpeta_cmd = _quote_carpeta(carpeta_ui)

    if not host:
        return False, "Indica el servidor IMAP (ej. imap.gmail.com)."
    if not usuario:
        return False, "Indica el usuario o correo."
    if not password:
        return False, "Indica la contraseña o clave de aplicación."
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False, "El puerto debe ser un número (ej. 993)."
    if port < 1 or port > 65535:
        return False, "Puerto fuera de rango. Usa 993 para Gmail/SSL."

    client = None
    try:
        if usar_ssl:
            client = imaplib.IMAP4_SSL(host, port, timeout=timeout)
        else:
            client = imaplib.IMAP4(host, port, timeout=timeout)

        try:
            typ, _ = client.login(usuario, password)
        except imaplib.IMAP4.error as exc:
            return False, _mensaje_amigable(exc, carpeta=carpeta_ui)
        if typ != "OK":
            return False, (
                "Usuario o contraseña incorrectos. "
                "Si usas Gmail, prueba con una contraseña de aplicación."
            )

        try:
            typ, data = client.select(carpeta_cmd, readonly=True)
        except imaplib.IMAP4.error as exc:
            return False, _mensaje_amigable(exc, carpeta=carpeta_ui)

        if typ != "OK":
            detalle = ""
            if data:
                detalle = _texto_exc(Exception(data[0] if data else ""))
            # Intentar listar carpetas para orientar al usuario
            sugerencia = ""
            try:
                sty, folders = client.list()
                if sty == "OK" and folders:
                    nombres = []
                    for item in folders[:12]:
                        if not item:
                            continue
                        line = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
                        # Último token suele ser el nombre (posible "nombre con espacios")
                        m = re.search(r' "([^"]+)"\s*$| (\S+)\s*$', line)
                        if m:
                            nombres.append(m.group(1) or m.group(2))
                    if nombres:
                        muestra = ", ".join(nombres[:6])
                        sugerencia = f" Carpetas disponibles: {muestra}."
            except Exception:
                sugerencia = ""
            base = (
                f"No se pudo abrir la carpeta “{carpeta_ui}”. "
                "Revisa el nombre exacto o usa INBOX."
            )
            if detalle and "parse" in detalle.lower():
                base = (
                    f"La carpeta “{carpeta_ui}” no es válida para IMAP "
                    "(espacios o caracteres). Prueba INBOX o el nombre exacto."
                )
            return False, base + sugerencia

        try:
            n = int(data[0]) if data else 0
            return True, f"Conexión correcta · “{carpeta_ui}” con {n} mensaje(s)."
        except (TypeError, ValueError):
            return True, f"Conexión correcta · carpeta “{carpeta_ui}”."

    except socket.timeout:
        return False, (
            "El servidor no respondió a tiempo. "
            "Revisa host, puerto (993) e internet."
        )
    except socket.gaierror:
        return False, "No se encontró el servidor. Revisa el host (ej. imap.gmail.com)."
    except ssl.SSLError as exc:
        return False, _mensaje_amigable(exc, carpeta=carpeta_ui)
    except imaplib.IMAP4.error as exc:
        return False, _mensaje_amigable(exc, carpeta=carpeta_ui)
    except OSError as exc:
        return False, _mensaje_amigable(exc, carpeta=carpeta_ui)
    except Exception as exc:
        return False, _mensaje_amigable(exc, carpeta=carpeta_ui)
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass
