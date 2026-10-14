"""Escribe tanqueos en un Excel tipo Acpm (append en orden).

Medio montado: agrega filas al final. Saldo / AbonoS quedan para después.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook

from trailero_pdf import format_fecha_slash

HEADERS = [
    "Tanqueo #",
    "Fecha",
    "Placa",
    "CANTIDAD",
    "Precio x Galon",
    "Descuento",
    "Valor Total",
]


def _parse_fecha_cell(fecha: str) -> datetime | str:
    raw = (fecha or "").strip()
    if not raw:
        return ""
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d")
    except ValueError:
        return format_fecha_slash(raw)


def _ultima_fila_datos(ws) -> int:
    """Última fila con algo en columnas A–G (1 = solo encabezado)."""
    last = 1
    for row in range(2, ws.max_row + 1):
        if any(ws.cell(row, c).value not in (None, "") for c in range(1, 8)):
            last = row
    return last


def _siguiente_tanqueo(ws, last_row: int) -> int:
    if last_row < 2:
        return 1
    val = ws.cell(last_row, 1).value
    try:
        return int(val) + 1
    except (TypeError, ValueError):
        return last_row  # fila 2 → tanqueo 1 si no hay número


def append_tanqueos_excel(
    ruta: str,
    hoja: str,
    detalle: list[dict],
) -> dict:
    """
    Agrega al Excel las filas ok del detalle, en orden (siguiente Tanqueo #).

    Retorna {ok, escritos, ruta, hoja, error?, siguiente_desde?}
    """
    path = Path((ruta or "").strip())
    sheet_name = (hoja or "Acpm").strip() or "Acpm"
    rows = [d for d in (detalle or []) if d.get("ok")]

    if not str(path):
        return {"ok": False, "escritos": 0, "error": "Falta la ruta del Excel."}
    if not rows:
        return {
            "ok": True,
            "escritos": 0,
            "ruta": str(path),
            "hoja": sheet_name,
            "error": "",
            "aviso": "No había tanqueos OK para escribir.",
        }

    try:
        if path.exists():
            wb = load_workbook(path)
            if sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
            else:
                ws = wb.create_sheet(sheet_name)
                for col, h in enumerate(HEADERS, start=1):
                    ws.cell(1, col, h)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            wb = Workbook()
            ws = wb.active
            ws.title = sheet_name
            for col, h in enumerate(HEADERS, start=1):
                ws.cell(1, col, h)

        # Asegurar encabezados si la hoja está vacía
        if ws.cell(1, 1).value in (None, ""):
            for col, h in enumerate(HEADERS, start=1):
                ws.cell(1, col, h)

        last = _ultima_fila_datos(ws)
        n = _siguiente_tanqueo(ws, last)
        escritos = 0

        for d in rows:
            last += 1
            ws.cell(last, 1, n)
            ws.cell(last, 2, _parse_fecha_cell(d.get("fecha") or ""))
            ws.cell(last, 3, d.get("placa") or "")
            ws.cell(last, 4, float(d.get("cantidad") or 0))
            ws.cell(last, 5, float(d.get("precio_galon") or 0))
            ws.cell(last, 6, float(d.get("descuento") or 0))
            ws.cell(last, 7, float(d.get("valor_total") or 0))
            # TODO: Saldo (col H) y AbonoS (col I) — calculadora después
            n += 1
            escritos += 1

        wb.save(path)
        return {
            "ok": True,
            "escritos": escritos,
            "ruta": str(path),
            "hoja": sheet_name,
            "error": "",
            "siguiente_tanqueo": n,
        }
    except PermissionError:
        return {
            "ok": False,
            "escritos": 0,
            "ruta": str(path),
            "hoja": sheet_name,
            "error": "No se pudo guardar: el Excel está abierto. Ciérralo e inténtalo de nuevo.",
        }
    except Exception as exc:
        return {
            "ok": False,
            "escritos": 0,
            "ruta": str(path),
            "hoja": sheet_name,
            "error": f"Error al escribir Excel: {exc}",
        }
