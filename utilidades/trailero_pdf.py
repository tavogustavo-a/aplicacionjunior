"""Parsea facturas PDF de Estación El Trailero (BIOMAX)."""

from __future__ import annotations

import io
import re
from datetime import datetime

from pypdf import PdfReader


def _parse_numero_co(raw: str, *, es_dinero: bool = False) -> float | None:
    """
    Convierte números estilo CO.
    - 11.590,01 → 11590.01
    - 23.608 (cantidad) → 23.608
    - 11.590 (dinero solo con puntos de miles) → 11590
    """
    s = (raw or "").strip().replace(" ", "").replace("$", "")
    if not s:
        return None
    try:
        if "," in s and "." in s:
            # miles con punto, decimal con coma
            return float(s.replace(".", "").replace(",", "."))
        if "," in s:
            return float(s.replace(",", "."))
        # Solo punto(s)
        if es_dinero and re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
            # 11.590 / 1.234.567 → separador de miles
            return float(s.replace(".", ""))
        return float(s)
    except ValueError:
        return None


def _extract_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts: list[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


def parse_factura_pdf(pdf_bytes: bytes, *, nombre_archivo: str = "") -> dict:
    """
    Extrae campos de tanqueo desde el PDF.
    Retorna dict con ok=True/False y campos:
    factura, fecha, placa, cantidad, precio_galon, descuento, valor_total, error
    """
    out = {
        "ok": False,
        "factura": "",
        "fecha": "",
        "placa": "",
        "cantidad": None,
        "precio_galon": None,
        "descuento": None,
        "valor_total": None,
        "archivo": nombre_archivo or "",
        "error": "",
    }
    if not pdf_bytes:
        out["error"] = "PDF vacío"
        return out

    try:
        text = _extract_text(pdf_bytes)
    except Exception as exc:
        out["error"] = f"No se pudo leer el PDF: {exc}"
        return out

    if not text.strip():
        out["error"] = "El PDF no tiene texto legible"
        return out

    # Normalizar acentos rotos frecuentes de extracción
    norm = (
        text.replace("\xa0", " ")
        .replace("Ó", "O")
        .replace("ó", "o")
        .replace("Í", "I")
        .replace("í", "i")
        .replace("É", "E")
        .replace("é", "e")
        .replace("Á", "A")
        .replace("á", "a")
        .replace("Ú", "U")
        .replace("ú", "u")
        .replace("Ñ", "N")
        .replace("ñ", "n")
    )

    m_fact = re.search(r"\b(FEVE\d+)\b", norm, re.I)
    if m_fact:
        out["factura"] = m_fact.group(1).upper()

    m_fecha = re.search(
        r"Fecha\s+Emisi[^\n:]*:\s*(\d{4}-\d{2}-\d{2})",
        norm,
        re.I,
    )
    if not m_fecha:
        m_fecha = re.search(
            r"Fecha\s+Validaci[^\n:]*:\s*(\d{4}-\d{2}-\d{2})",
            norm,
            re.I,
        )
    if m_fecha:
        out["fecha"] = m_fecha.group(1)

    m_placa = re.search(
        r"PLACA\s+VEHICULO\(S\):\s*([A-Z0-9\-]+)",
        norm,
        re.I,
    )
    if not m_placa:
        m_placa = re.search(r"PLACA[^:\n]*:\s*([A-Z0-9\-]+)", norm, re.I)
    if m_placa:
        out["placa"] = m_placa.group(1).upper().strip()

    # Línea de producto: DIESEL o CORRIENTE, luego cant · um · precio · dcto
    m_producto = re.search(
        r"(?:DIESEL|CORRIENTE)\s+([\d.,]+)\s+\S+\s+([\d.,]+)\s+([\d.,]+)",
        norm,
        re.I,
    )
    if m_producto:
        out["cantidad"] = _parse_numero_co(m_producto.group(1))
        out["precio_galon"] = _parse_numero_co(m_producto.group(2), es_dinero=True)
        out["descuento"] = _parse_numero_co(m_producto.group(3), es_dinero=True)

    m_total = re.search(r"Total\s+COP:\s*([\d.,]+)", norm, re.I)
    if m_total:
        out["valor_total"] = _parse_numero_co(m_total.group(1), es_dinero=True)

    faltan = []
    if not out["fecha"]:
        faltan.append("fecha")
    if not out["placa"]:
        faltan.append("placa")
    if out["cantidad"] is None:
        faltan.append("cantidad")
    if out["precio_galon"] is None:
        faltan.append("precio")
    if out["descuento"] is None:
        faltan.append("descuento")
    if out["valor_total"] is None:
        faltan.append("valor total")

    if faltan:
        out["error"] = "No se pudieron leer: " + ", ".join(faltan)
        return out

    out["ok"] = True
    return out


def fecha_sort_key(fecha: str) -> datetime:
    try:
        return datetime.strptime(fecha[:10], "%Y-%m-%d")
    except ValueError:
        # También aceptar D/M/YYYY
        try:
            parts = (fecha or "").strip().split("/")
            if len(parts) == 3:
                d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
                return datetime(y, m, d)
        except ValueError:
            pass
        return datetime.max


def format_fecha_slash(fecha: str) -> str:
    """YYYY-MM-DD → D/MM/YYYY (como el Excel Acpm)."""
    raw = (fecha or "").strip()
    if not raw:
        return ""
    if "/" in raw and "-" not in raw[:10]:
        return raw
    try:
        dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        return f"{dt.day}/{dt.month:02d}/{dt.year}"
    except ValueError:
        return raw.replace("-", "/")
