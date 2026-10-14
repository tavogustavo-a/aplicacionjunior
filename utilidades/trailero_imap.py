"""Busca correos IMAP por fechas y descarga PDFs adjuntos."""

from __future__ import annotations

import email
import imaplib
import io
import re
import zipfile
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.policy import default as email_policy_default
from email.utils import parsedate_to_datetime
from typing import Callable

from imap_client import _mensaje_amigable, _quote_carpeta
from trailero_pdf import fecha_sort_key, parse_factura_pdf

ProgressCb = Callable[[dict], None]


def _imap_date(d: datetime) -> str:
    # IMAP exige meses en inglés (01-Jan-2025)
    months = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    return f"{d.day:02d}-{months[d.month - 1]}-{d.year}"


def _parse_hora(hora: str) -> tuple[int, int]:
    raw = (hora or "00:00").strip()
    try:
        parts = raw.split(":")
        h = max(0, min(23, int(parts[0])))
        m = max(0, min(59, int(parts[1]))) if len(parts) > 1 else 0
        return h, m
    except (TypeError, ValueError, IndexError):
        return 0, 0


def _parse_email_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _decode_mime(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _filename_of(part: Message) -> str:
    name = part.get_filename()
    if name:
        return _decode_mime(name)
    # Content-Disposition / Content-Type name= / filename*=
    for header in ("Content-Disposition", "Content-Type"):
        raw = part.get(header) or ""
        m = re.search(
            r"(?:filename|name)\*\s*=\s*(?:UTF-8''|utf-8'')([^;]+)",
            raw,
            re.I,
        )
        if m:
            try:
                from urllib.parse import unquote

                return unquote(m.group(1).strip().strip('"'))
            except Exception:
                pass
        m = re.search(r'(?:filename|name)\s*=\s*"([^"]+)"', raw, re.I)
        if m:
            return _decode_mime(m.group(1))
        m = re.search(r"(?:filename|name)\s*=\s*([^;\s]+)", raw, re.I)
        if m:
            return _decode_mime(m.group(1).strip().strip("'\""))
    return ""


def _looks_like_pdf_bytes(data: bytes | None) -> bool:
    if not data or len(data) < 5:
        return False
    # BOM / espacios antes de %PDF
    head = data[:32].lstrip()
    return head.startswith(b"%PDF")


def _is_pdf_part(part: Message, payload: bytes | None = None) -> bool:
    ctype = (part.get_content_type() or "").lower()
    fname = _filename_of(part).lower()
    subtype = (part.get_content_subtype() or "").lower()
    if fname.endswith(".pdf") or ".pdf" in fname:
        return True
    if ctype in ("application/pdf", "application/x-pdf") or subtype == "pdf":
        return True
    if "pdf" in ctype:
        return True
    if payload is not None and _looks_like_pdf_bytes(payload):
        # Algunos clientes mandan application/octet-stream sin nombre .pdf
        disp = (part.get_content_disposition() or "").lower()
        if disp in ("attachment", "inline") or fname or ctype.startswith("application/"):
            return True
    return False


def _list_adjuntos_info(msg: Message) -> list[str]:
    """Nombres/tipos de adjuntos (para mensajes de error más claros)."""
    infos: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ctype = (part.get_content_type() or "").lower()
        if ctype.startswith("text/") and not part.get_filename():
            continue
        if ctype in ("text/plain", "text/html"):
            continue
        fname = _filename_of(part) or "(sin nombre)"
        infos.append(f"{fname} [{ctype}]")
    return infos


def _is_zip_part(part: Message, payload: bytes | None = None) -> bool:
    ctype = (part.get_content_type() or "").lower()
    fname = _filename_of(part).lower()
    if fname.endswith(".zip") or ".zip" in fname:
        return True
    if ctype in ("application/zip", "application/x-zip-compressed", "multipart/x-zip"):
        return True
    if payload and len(payload) >= 4 and payload[:2] == b"PK":
        return True
    return False


def _pdfs_from_zip(zip_bytes: bytes, zip_name: str = "") -> list[tuple[str, bytes]]:
    """Extrae PDFs desde un ZIP (facturas DIAN suelen venir así)."""
    out: list[tuple[str, bytes]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                base = info.filename.replace("\\", "/").split("/")[-1]
                if not base or base.startswith("."):
                    continue
                try:
                    data = zf.read(info)
                except Exception:
                    continue
                lower = base.lower()
                if lower.endswith(".pdf") or _looks_like_pdf_bytes(data):
                    label = base if lower.endswith(".pdf") else f"{base}.pdf"
                    if zip_name:
                        label = f"{zip_name} / {label}"
                    out.append((label, data))
    except zipfile.BadZipFile:
        return []
    except Exception:
        return []
    return out


def _iter_pdf_attachments(msg: Message) -> list[tuple[str, bytes]]:
    found: list[tuple[str, bytes]] = []
    seen: set[tuple[str, int]] = set()

    def _add(name: str, payload: bytes) -> None:
        key = (name.lower(), len(payload))
        if key in seen:
            return
        seen.add(key)
        found.append((name, payload))

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ctype = (part.get_content_type() or "").lower()
        if ctype in ("text/plain", "text/html") and not part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if not payload:
            continue

        if _is_pdf_part(part, payload):
            name = _filename_of(part) or "adjunto.pdf"
            if not name.lower().endswith(".pdf"):
                name = f"{name}.pdf" if name != "adjunto.pdf" else name
            _add(name, payload)
            continue

        if _is_zip_part(part, payload):
            zip_name = _filename_of(part) or "adjunto.zip"
            for pdf_name, pdf_bytes in _pdfs_from_zip(payload, zip_name):
                _add(pdf_name, pdf_bytes)

    return found


def _mensaje_sin_pdf(asunto: str, msg: Message) -> str:
    infos = _list_adjuntos_info(msg)
    if not infos:
        return "Sin PDF adjunto (el correo no trae archivos)."
    tiene_zip = any(".zip" in (i or "").lower() for i in infos)
    muestra = ", ".join(infos[:4])
    if len(infos) > 4:
        muestra += f" (+{len(infos) - 4} más)"
    if tiene_zip:
        return (
            "No se encontró PDF dentro del ZIP. "
            f"Archivos vistos: {muestra}"
        )
    return f"Sin PDF adjunto. Archivos vistos: {muestra}"


def ejecutar_trailero(
    *,
    host: str,
    port: int,
    usuario: str,
    password: str,
    carpeta: str,
    desde: str,
    hasta: str,
    hora_desde: str = "00:00",
    usar_ssl: bool = True,
    progress_cb: ProgressCb | None = None,
    timeout: float = 30.0,
) -> dict:
    """
    Busca correos entre desde/hasta (inclusivo), desde la hora indicada,
    lee PDFs y arma el listado.
    """

    def emit(ev: dict) -> None:
        if progress_cb:
            progress_cb(ev)

    try:
        d0 = datetime.strptime(desde[:10], "%Y-%m-%d")
        d1 = datetime.strptime(hasta[:10], "%Y-%m-%d")
    except ValueError:
        return {
            "ok": False,
            "error": "Fechas inválidas. Usa el calendario (AAAA-MM-DD).",
            "code": "fechas",
        }
    if d1 < d0:
        return {
            "ok": False,
            "error": "La fecha Hasta no puede ser anterior a Desde.",
            "code": "fechas",
        }

    hh, mm = _parse_hora(hora_desde)
    hora_norm = f"{hh:02d}:{mm:02d}"
    inicio_dt = d0.replace(hour=hh, minute=mm, second=0, microsecond=0)
    fin_dt = d1.replace(hour=23, minute=59, second=59, microsecond=0)

    carpeta_ui = (carpeta or "INBOX").strip() or "INBOX"
    carpeta_cmd = _quote_carpeta(carpeta_ui)
    # BEFORE es exclusivo → día siguiente a hasta
    before = d1 + timedelta(days=1)

    emit(
        {
            "type": "status",
            "phase": "conectar",
            "message": f"Conectando a {host}…",
        }
    )

    client = None
    filas: list[dict] = []
    sin_pdf: list[dict] = []
    total_msgs = 0
    omitidos_hora = 0

    try:
        if usar_ssl:
            client = imaplib.IMAP4_SSL(host, int(port), timeout=timeout)
        else:
            client = imaplib.IMAP4(host, int(port), timeout=timeout)
        client.login(usuario, password)
        typ, _ = client.select(carpeta_cmd, readonly=True)
        if typ != "OK":
            return {
                "ok": False,
                "error": f"No se pudo abrir la carpeta “{carpeta_ui}”.",
                "code": "carpeta",
            }

        criteria = f'(SINCE {_imap_date(d0)} BEFORE {_imap_date(before)})'
        emit(
            {
                "type": "status",
                "phase": "buscar",
                "message": (
                    f"Buscando correos del {desde} {hora_norm} al {hasta}…"
                ),
            }
        )
        typ, data = client.search(None, criteria)
        if typ != "OK":
            return {
                "ok": False,
                "error": "La búsqueda IMAP falló. Revisa las fechas y la carpeta.",
                "code": "buscar",
            }

        ids = (data[0] or b"").split()
        total_msgs = len(ids)
        emit(
            {
                "type": "status",
                "phase": "buscar",
                "message": f"Encontrados {total_msgs} correo(s). Revisando adjuntos…",
            }
        )

        pdf_jobs: list[dict] = []
        for i, mid in enumerate(ids, start=1):
            emit(
                {
                    "type": "status",
                    "phase": "correos",
                    "message": f"Leyendo correo {i} de {total_msgs}…",
                }
            )
            typ, msg_data = client.fetch(mid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                sin_pdf.append(
                    {
                        "ok": False,
                        "asunto": "",
                        "error": "No se pudo descargar el correo",
                    }
                )
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            try:
                msg = email.message_from_bytes(raw, policy=email_policy_default)
            except Exception:
                msg = email.message_from_bytes(raw)
            asunto = _decode_mime(msg.get("Subject"))
            fecha_hdr = _decode_mime(msg.get("Date"))
            mail_dt = _parse_email_dt(fecha_hdr)
            if mail_dt is not None and (mail_dt < inicio_dt or mail_dt > fin_dt):
                # Fuera del rango horario/fecha: se ignora (no es error)
                omitidos_hora += 1
                continue
            pdfs = _iter_pdf_attachments(msg)
            if not pdfs:
                sin_pdf.append(
                    {
                        "ok": False,
                        "asunto": asunto,
                        "correo_fecha": fecha_hdr,
                        "error": _mensaje_sin_pdf(asunto, msg),
                        "archivo": "",
                    }
                )
                continue
            for nombre, blob in pdfs:
                pdf_jobs.append(
                    {
                        "asunto": asunto,
                        "correo_fecha": fecha_hdr,
                        "archivo": nombre,
                        "bytes": blob,
                    }
                )

        total_pdfs = len(pdf_jobs)
        for i, job in enumerate(pdf_jobs, start=1):
            emit(
                {
                    "type": "pdf_progress",
                    "phase": "pdf",
                    "done": i,
                    "total": max(total_pdfs, 1),
                    "faltan": max(total_pdfs - i, 0),
                    "factura": job.get("archivo") or "",
                    "message": (
                        f"Leyendo PDF {i} de {total_pdfs}"
                        + (f" · {job.get('archivo')}" if job.get("archivo") else "")
                    ),
                }
            )
            parsed = parse_factura_pdf(
                job["bytes"], nombre_archivo=job.get("archivo") or ""
            )
            fila = {
                **parsed,
                "asunto": job.get("asunto") or "",
                "correo_fecha": job.get("correo_fecha") or "",
            }
            filas.append(fila)

        # Errores de correos sin PDF
        for e in sin_pdf:
            filas.append(
                {
                    "ok": False,
                    "factura": "",
                    "fecha": "",
                    "placa": "",
                    "cantidad": None,
                    "precio_galon": None,
                    "descuento": None,
                    "valor_total": None,
                    "archivo": e.get("archivo") or "",
                    "asunto": e.get("asunto") or "",
                    "correo_fecha": e.get("correo_fecha") or "",
                    "error": e.get("error") or "No leído",
                }
            )

    except imaplib.IMAP4.error as exc:
        return {
            "ok": False,
            "error": _mensaje_amigable(exc, carpeta=carpeta_ui),
            "code": "imap",
        }
    except OSError as exc:
        return {
            "ok": False,
            "error": _mensaje_amigable(exc, carpeta=carpeta_ui),
            "code": "red",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": _mensaje_amigable(exc, carpeta=carpeta_ui),
            "code": "error",
        }
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass

    # Orden: OK por fecha ascendente, luego errores al final
    ok_rows = [f for f in filas if f.get("ok")]
    bad_rows = [f for f in filas if not f.get("ok")]
    ok_rows.sort(key=lambda r: (fecha_sort_key(r.get("fecha") or ""), r.get("placa") or ""))

    detalle: list[dict] = []
    for i, r in enumerate(ok_rows, start=1):
        detalle.append(
            {
                "tanqueo": i,
                "fecha": r.get("fecha") or "",
                "placa": r.get("placa") or "",
                "cantidad": r.get("cantidad"),
                "precio_galon": r.get("precio_galon"),
                "descuento": r.get("descuento"),
                "valor_total": r.get("valor_total"),
                "factura": r.get("factura") or "",
                "archivo": r.get("archivo") or "",
                "asunto": r.get("asunto") or "",
                "ok": True,
                "error": "",
            }
        )
    for r in bad_rows:
        detalle.append(
            {
                "tanqueo": None,
                "fecha": r.get("fecha") or "",
                "placa": r.get("placa") or "",
                "cantidad": r.get("cantidad"),
                "precio_galon": r.get("precio_galon"),
                "descuento": r.get("descuento"),
                "valor_total": r.get("valor_total"),
                "factura": r.get("factura") or "",
                "archivo": r.get("archivo") or "",
                "asunto": r.get("asunto") or "",
                "ok": False,
                "error": r.get("error") or "No se pudo leer",
            }
        )

    total_general = sum(float(d.get("valor_total") or 0) for d in detalle if d.get("ok"))
    pdfs_ok = sum(1 for d in detalle if d.get("ok"))
    pdfs_error = sum(1 for d in detalle if not d.get("ok"))

    return {
        "ok": True,
        "desde": desde[:10],
        "hasta": hasta[:10],
        "hora_desde": hora_norm,
        "imap_usuario": usuario,
        "carpeta": carpeta_ui,
        "correos_total": total_msgs,
        "omitidos_hora": omitidos_hora,
        "pdfs_ok": pdfs_ok,
        "pdfs_error": pdfs_error,
        "tanqueos_total": pdfs_ok,
        "total_general": total_general,
        "detalle": detalle,
    }
