"""Resumen de ventas por sucursal y medios de pago (facturas EFFI)."""

from __future__ import annotations

import io
import re
import time
from copy import copy
from dataclasses import asdict, dataclass, field
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

from effi_clientes import EffiError

FACTURAS_PATH = "/app/factura_v"
MAX_PAGES = 40
# Lotes de PDFs: lee todos, de a 120, con pausa breve entre lotes.
PDF_BATCH_SIZE = 120
PDF_BATCH_PAUSE_SEC = 0.35
PAGE_SIZE_HINT = 10  # filas típicas por página en factura_v
CUENTA_BANCOLOMBIA_ESPERADA = "72300006159"


def _normalize_fechas_effi(desde: str, hasta: str) -> tuple[str, str]:
    """EFFI ignora el día si hasta va solo como YYYY-MM-DD (queda en 00:00:00)."""
    desde = (desde or "").strip()
    hasta = (hasta or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", desde):
        desde = f"{desde} 00:00:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", hasta):
        hasta = f"{hasta} 23:59:59"
    return desde, hasta


def _parse_money(raw: str) -> float | None:
    """Parsea montos EFFI tipo 275000.0000, $275,000 o $231,092.44."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("$", "").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        pass
    # 1.234.567,89 (europeo) o 1,234,567.89 (US)
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        s = s.replace(",", ".") if len(parts[-1]) <= 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None

# Código panel → id EFFI
SUCURSALES = {
    "PER": {"id": "1", "nombre": "Pereira", "label": "1 - Principal"},
    "BOG": {"id": "3", "nombre": "Bogotá", "label": "3 - BOGOTA"},
    "MED": {"id": "2", "nombre": "Medellín", "label": "2 - MEDELLIN"},
}

# Orden fijo cuando se consulta TODOS
ORDEN_TODOS = ("PER", "MED", "BOG")

# Incluye TODOS para la UI del panel
SUCURSALES_UI = {
    **SUCURSALES,
    "TODOS": {"id": "", "nombre": "Todas", "label": "PER + MED + BOG"},
}

# Bucket panel → ids t_medio_pago en EFFI
MEDIOS = [
    ("efectivo", "Efectivo", (1,)),
    ("tarjeta", "Tarjeta crédito y débito", (2, 3)),
    ("sistecredito", "Sistecrédito", (11,)),
    ("addi", "Addi", (10,)),
    ("transferencias", "Transferencias", (5,)),
    ("pago_electronico", "Pago electrónico", (6,)),
    ("anticipo", "Anticipo", (9,)),
]

MEDIO_POR_ID = {
    1: ("efectivo", "Efectivo"),
    2: ("tarjeta", "Tarjeta crédito y débito"),
    3: ("tarjeta", "Tarjeta crédito y débito"),
    5: ("transferencias", "Transferencias"),
    6: ("pago_electronico", "Pago electrónico"),
    9: ("anticipo", "Anticipo"),
    10: ("addi", "Addi"),
    11: ("sistecredito", "Sistecrédito"),
}

# Subetiqueta (como Creación / Entrega) para medios con pasarela
PASARELA_POR_MEDIO = {
    "pago_electronico": "Mercado Pago",
}


@dataclass
class MedioTotal:
    key: str
    nombre: str
    total: float = 0.0
    facturas: int = 0


@dataclass
class FacturaDetalle:
    factura: str
    fecha: str = ""
    cliente: str = ""
    medio_key: str = ""
    medio_nombre: str = ""
    total: float = 0.0
    cuenta: str = ""
    banco: str = ""
    cuenta_alerta: bool = False
    pdf_url: str = ""
    numeracion: str = ""
    sucursal: str = ""
    sucursal_nombre: str = ""
    pasarela: str = ""  # ej. Mercado Pago bajo Pago electrónico


@dataclass
class ResumenSucursalBloque:
    codigo: str
    nombre: str
    medios: list[MedioTotal] = field(default_factory=list)
    total_general: float = 0.0
    facturas_total: int = 0


@dataclass
class ResumenVentas:
    codigo: str
    sucursal_id: str
    sucursal_nombre: str
    desde: str
    hasta: str
    medios: list[MedioTotal] = field(default_factory=list)
    total_general: float = 0.0
    facturas_total: int = 0
    detalle: list[FacturaDetalle] = field(default_factory=list)
    por_sucursal: list[ResumenSucursalBloque] = field(default_factory=list)
    cuenta_esperada: str = CUENTA_BANCOLOMBIA_ESPERADA


class EffiVentas:
    def __init__(self, session_cookie: str, base_url: str = "https://effi.com.co"):
        if not session_cookie:
            raise EffiError("SIN_COOKIE", "Falta la cookie ci_session.")
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.cookies.set("ci_session", session_cookie, domain="effi.com.co")
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/150.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "es",
                "Referer": f"{self.base_url}{FACTURAS_PATH}",
            }
        )

    def resumen_sucursal(
        self,
        codigo: str,
        desde: str,
        hasta: str,
        guardar_html: Optional[str] = None,
        leer_pdf_transferencias: bool = True,
        medios_filtro: Optional[list[str]] = None,
        progress_cb=None,
    ) -> ResumenVentas:
        codigo = (codigo or "").strip().upper()
        if codigo not in SUCURSALES and codigo != "TODOS":
            raise EffiError("SUCURSAL", f"Sucursal inválida: {codigo}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", desde or ""):
            raise EffiError("FECHA", "Fecha desde inválida (YYYY-MM-DD).")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", hasta or ""):
            raise EffiError("FECHA", "Fecha hasta inválida (YYYY-MM-DD).")
        if desde > hasta:
            raise EffiError("FECHA", "La fecha desde no puede ser mayor que hasta.")

        keys_validos = {key for key, _, _ in MEDIOS}
        if medios_filtro:
            seleccion = [k for k in medios_filtro if k in keys_validos]
        else:
            seleccion = [key for key, _, _ in MEDIOS]
        if not seleccion:
            raise EffiError("MEDIOS", "Selecciona al menos un medio de pago.")

        # La búsqueda siempre consulta/muestra los 5 medios; los marcados
        # son los que suman y salen en el listado individual.
        if codigo == "TODOS":
            targets = [(c, SUCURSALES[c]) for c in ORDEN_TODOS]
            sucursal_id = ""
            sucursal_nombre = "Todas"
        else:
            targets = [(codigo, SUCURSALES[codigo])]
            sucursal_id = SUCURSALES[codigo]["id"]
            sucursal_nombre = SUCURSALES[codigo]["nombre"]

        detalle_all: list[FacturaDetalle] = []
        seen: set[str] = set()
        medios_map = {
            key: MedioTotal(key=key, nombre=nombre) for key, nombre, _ in MEDIOS
        }
        # Totales por sucursal (orden PER → MED → BOG en TODOS)
        por_suc_map: dict[str, dict[str, MedioTotal]] = {
            code: {
                key: MedioTotal(key=key, nombre=nombre) for key, nombre, _ in MEDIOS
            }
            for code, _ in targets
        }

        if progress_cb:
            progress_cb(
                {
                    "type": "status",
                    "phase": "facturas",
                    "message": "Consultando facturas en EFFI…",
                }
            )

        for t_idx, (_code, meta) in enumerate(targets):
            for idx, (key, nombre, ids) in enumerate(MEDIOS):
                if progress_cb:
                    label = meta.get("nombre") or _code
                    progress_cb(
                        {
                            "type": "status",
                            "phase": "facturas",
                            "message": f"Consultando {nombre} · {label}…",
                        }
                    )
                filas = self._listar_medios(
                    sucursal_id=meta["id"],
                    desde=desde,
                    hasta=hasta,
                    medio_ids=ids,
                    medio_key=key,
                    medio_nombre=nombre,
                    guardar_html=(
                        guardar_html if t_idx == 0 and idx == 0 else None
                    ),
                )
                for fila in filas:
                    if fila.factura in seen:
                        continue
                    seen.add(fila.factura)
                    fila.sucursal = _code
                    fila.sucursal_nombre = meta.get("nombre") or _code
                    if not fila.pasarela:
                        fila.pasarela = PASARELA_POR_MEDIO.get(key, "")
                    detalle_all.append(fila)
                    medios_map[key].total += fila.total
                    medios_map[key].facturas += 1
                    por_suc_map[_code][key].total += fila.total
                    por_suc_map[_code][key].facturas += 1

        if leer_pdf_transferencias:
            detalle_all = self._enriquecer_desde_pdf(
                detalle_all, progress_cb=progress_cb
            )
            # Tras partir facturas multi-medio, recalcular totales desde filas.
            medios_map = {
                key: MedioTotal(key=key, nombre=nombre) for key, nombre, _ in MEDIOS
            }
            por_suc_map = {
                code: {
                    key: MedioTotal(key=key, nombre=nombre)
                    for key, nombre, _ in MEDIOS
                }
                for code, _ in targets
            }
            for fila in detalle_all:
                if fila.medio_key not in medios_map:
                    continue
                medios_map[fila.medio_key].total += fila.total
                medios_map[fila.medio_key].facturas += 1
                if fila.sucursal in por_suc_map:
                    por_suc_map[fila.sucursal][fila.medio_key].total += fila.total
                    por_suc_map[fila.sucursal][fila.medio_key].facturas += 1

        medios = []
        for key, nombre, _ in MEDIOS:
            m = medios_map[key]
            m.total = round(m.total, 2)
            medios.append(m)

        # Individuales + total: solo los medios marcados
        detalle = [f for f in detalle_all if f.medio_key in seleccion]
        total_general = round(
            sum(medios_map[k].total for k in seleccion),
            2,
        )
        facturas_total = sum(medios_map[k].facturas for k in seleccion)

        por_sucursal: list[ResumenSucursalBloque] = []
        for code, meta in targets:
            medios_suc = []
            for key, nombre, _ in MEDIOS:
                m = por_suc_map[code][key]
                m.total = round(m.total, 2)
                medios_suc.append(m)
            tot_suc = round(sum(por_suc_map[code][k].total for k in seleccion), 2)
            fac_suc = sum(por_suc_map[code][k].facturas for k in seleccion)
            por_sucursal.append(
                ResumenSucursalBloque(
                    codigo=code,
                    nombre=meta.get("nombre") or code,
                    medios=medios_suc,
                    total_general=tot_suc,
                    facturas_total=fac_suc,
                )
            )

        # Orden: Pereira → Medellín → Bogotá; dentro de cada una, fecha desc.
        orden_idx = {c: i for i, c in enumerate(ORDEN_TODOS)}
        detalle_ordenado: list[FacturaDetalle] = []
        if codigo == "TODOS":
            for code in ORDEN_TODOS:
                grupo = [f for f in detalle if f.sucursal == code]
                grupo.sort(key=lambda f: (f.fecha, f.factura), reverse=True)
                detalle_ordenado.extend(grupo)
            # Cualquier otra (por si acaso)
            otros = [f for f in detalle if f.sucursal not in orden_idx]
            otros.sort(key=lambda f: (f.fecha, f.factura), reverse=True)
            detalle_ordenado.extend(otros)
            detalle = detalle_ordenado
        else:
            detalle.sort(key=lambda f: (f.fecha, f.factura), reverse=True)

        return ResumenVentas(
            codigo=codigo,
            sucursal_id=sucursal_id,
            sucursal_nombre=sucursal_nombre,
            desde=desde,
            hasta=hasta,
            medios=medios,
            total_general=round(total_general, 2),
            facturas_total=facturas_total,
            detalle=detalle,
            por_sucursal=por_sucursal if codigo == "TODOS" else [],
        )

    def _listar_medios(
        self,
        sucursal_id: str,
        desde: str,
        hasta: str,
        medio_ids: tuple[int, ...],
        medio_key: str,
        medio_nombre: str,
        guardar_html: Optional[str] = None,
    ) -> list[FacturaDetalle]:
        out: list[FacturaDetalle] = []
        seen: set[str] = set()

        for medio_id in medio_ids:
            pagina = 1
            total_paginas = 1
            while pagina <= total_paginas and pagina <= MAX_PAGES:
                html = self._get_listado(
                    sucursal_id=sucursal_id,
                    desde=desde,
                    hasta=hasta,
                    medio_id=medio_id,
                    pagina=pagina,
                    guardar_html=(
                        guardar_html
                        if pagina == 1 and medio_id == medio_ids[0]
                        else None
                    ),
                )
                page_items = self._parse_filas(html, medio_key, medio_nombre)
                # Con filtro t_medio_pago EFFI a veces deja la paginación
                # del listado general: cortamos en vacío o página incompleta.
                if not page_items:
                    break
                for fila in page_items:
                    if fila.factura in seen:
                        continue
                    seen.add(fila.factura)
                    out.append(fila)
                if len(page_items) < PAGE_SIZE_HINT:
                    break
                total_paginas = min(self._parse_total_paginas(html, pagina), MAX_PAGES)
                pagina += 1

        return out

    def _aplicar_medio_pdf(self, fila: FacturaDetalle, medio: dict) -> FacturaDetalle:
        """Copia la fila con datos de un medio parseado del PDF."""
        out = copy(fila)
        out.medio_key = medio.get("medio_key") or out.medio_key
        out.medio_nombre = medio.get("medio_nombre") or out.medio_nombre
        if medio.get("total") is not None:
            out.total = round(float(medio["total"]), 2)
        out.banco = medio.get("banco") or ""
        out.cuenta = medio.get("cuenta") or ""
        out.pasarela = medio.get("pasarela") or PASARELA_POR_MEDIO.get(
            out.medio_key, ""
        )
        out.cuenta_alerta = False
        if out.medio_key == "transferencias":
            if out.cuenta and out.cuenta != CUENTA_BANCOLOMBIA_ESPERADA:
                out.cuenta_alerta = True
            elif out.banco and "bancolombia" in out.banco.lower() and not out.cuenta:
                out.cuenta_alerta = True
        return out

    def _enriquecer_desde_pdf(
        self, detalle: list[FacturaDetalle], progress_cb=None
    ) -> list[FacturaDetalle]:
        """Lee Ver PDF; si hay 2+ medios, parte la factura en filas seguidas."""
        out: list[FacturaDetalle] = []
        pendientes = [f for f in detalle if f.pdf_url]
        total = len(pendientes)
        pdf_idx = 0
        if progress_cb:
            progress_cb(
                {
                    "type": "pdf_progress",
                    "phase": "pdf",
                    "done": 0,
                    "total": total,
                    "faltan": total,
                    "factura": "",
                    "message": (
                        f"Sin PDF para leer."
                        if total == 0
                        else f"Leyendo PDF 0 de {total}…"
                    ),
                }
            )

        for fila in detalle:
            if not fila.pdf_url:
                out.append(fila)
                continue
            if pdf_idx > 0 and pdf_idx % PDF_BATCH_SIZE == 0:
                time.sleep(PDF_BATCH_PAUSE_SEC)
            try:
                info = self._leer_pdf_pago(fila.pdf_url)
            except EffiError:
                info = {}
            except Exception:
                info = {}
            pdf_idx += 1

            if info.get("numeracion"):
                fila.numeracion = info["numeracion"]

            medios_pdf = info.get("medios") or []
            if len(medios_pdf) >= 2:
                for medio in medios_pdf:
                    out.append(self._aplicar_medio_pdf(fila, medio))
            else:
                if medios_pdf:
                    fila = self._aplicar_medio_pdf(fila, medios_pdf[0])
                else:
                    if info.get("pasarela"):
                        fila.pasarela = info["pasarela"]
                    elif fila.medio_key in PASARELA_POR_MEDIO and not fila.pasarela:
                        fila.pasarela = PASARELA_POR_MEDIO[fila.medio_key]
                    if fila.medio_key == "transferencias":
                        if info.get("medio_nombre"):
                            fila.medio_nombre = info["medio_nombre"]
                        fila.banco = info.get("banco", "") or fila.banco
                        fila.cuenta = info.get("cuenta", "") or fila.cuenta
                        if fila.cuenta and fila.cuenta != CUENTA_BANCOLOMBIA_ESPERADA:
                            fila.cuenta_alerta = True
                        elif (
                            fila.banco
                            and "bancolombia" in fila.banco.lower()
                            and not fila.cuenta
                        ):
                            fila.cuenta_alerta = True
                    else:
                        if info.get("banco"):
                            fila.banco = info["banco"]
                        if info.get("cuenta"):
                            fila.cuenta = info["cuenta"]
                out.append(fila)

            if progress_cb:
                progress_cb(
                    {
                        "type": "pdf_progress",
                        "phase": "pdf",
                        "done": pdf_idx,
                        "total": total,
                        "faltan": max(total - pdf_idx, 0),
                        "factura": fila.factura or "",
                        "message": f"Leyendo PDF {pdf_idx} de {total}…",
                    }
                )
        return out

    def _leer_pdf_pago(self, pdf_url: str) -> dict:
        url = pdf_url
        if url.startswith("/"):
            url = urljoin(self.base_url + "/", url.lstrip("/"))
        try:
            resp = self.session.get(url, timeout=45)
        except requests.Timeout as exc:
            raise EffiError("TIMEOUT", "EFFI tardó al descargar el PDF.") from exc
        except requests.RequestException as exc:
            raise EffiError("RED", f"Error al descargar PDF: {exc}") from exc

        if resp.status_code >= 400:
            raise EffiError("HTTP", f"PDF HTTP {resp.status_code}")
        if not resp.content.startswith(b"%PDF"):
            raise EffiError("PDF", "La respuesta no es un PDF válido.")

        return parse_pdf_pago(resp.content)

    def _get_listado(
        self,
        sucursal_id: str,
        desde: str,
        hasta: str,
        medio_id: int,
        pagina: int,
        guardar_html: Optional[str] = None,
    ) -> str:
        desde_q, hasta_q = _normalize_fechas_effi(desde, hasta)
        url = urljoin(self.base_url + "/", FACTURAS_PATH.lstrip("/"))
        params: dict[str, str | int] = {
            "desde": desde_q,
            "hasta": hasta_q,
            "sucursal": sucursal_id,
            "t_medio_pago": medio_id,
            "vigente": 1,
        }
        if pagina > 1:
            params["page"] = pagina

        try:
            resp = self.session.get(url, params=params, timeout=45)
        except requests.Timeout as exc:
            raise EffiError("TIMEOUT", "EFFI tardó demasiado al consultar facturas.") from exc
        except requests.ConnectionError as exc:
            raise EffiError("SIN_RED", "No se pudo conectar con EFFI para ventas.") from exc
        except requests.RequestException as exc:
            raise EffiError("RED", f"Error de red: {exc}") from exc

        if resp.status_code >= 400:
            raise EffiError("HTTP", f"EFFI respondió HTTP {resp.status_code} en facturas.")

        html_l = resp.text.lower()
        if 'id="login"' in html_l or "effi erp | ingreso" in html_l:
            raise EffiError(
                "SESION",
                "Sesión inválida o expirada al consultar ventas. Actualiza la cookie.",
            )

        if guardar_html:
            with open(guardar_html, "w", encoding="utf-8") as f:
                f.write(resp.text)
        return resp.text

    def _parse_filas(
        self, html: str, medio_key: str, medio_nombre: str
    ) -> list[FacturaDetalle]:
        soup = BeautifulSoup(html, "lxml")
        items: list[FacturaDetalle] = []
        seen_facturas: set[str] = set()

        for a in soup.select("a.ver-trazabilidad"):
            monto = _parse_money(a.get("data-total") or "")
            factura = (a.get("data-prefijo_id") or "").strip()
            if not factura:
                pref = (a.get("data-prefijo") or "").strip()
                iid = (a.get("data-id") or "").strip()
                factura = f"{pref}-{iid}" if pref and iid else iid
            if not factura or factura in seen_facturas:
                continue

            tr = a.find_parent("tr")
            pdf_url = ""
            if tr:
                if monto is None:
                    # Fallback: columna Totales → TOTAL NETO
                    for td in tr.find_all("td"):
                        t = td.get_text(" ", strip=True)
                        m_neto = re.search(
                            r"TOTAL\s*NETO\s*:\s*\$?\s*([\d.,]+)",
                            t,
                            re.I,
                        )
                        if m_neto:
                            monto = _parse_money(m_neto.group(1))
                            if monto is not None:
                                break
                for link in tr.select('a[href*="consultar_pdf"]'):
                    href = link.get("href") or ""
                    texto = link.get_text(" ", strip=True).lower()
                    if "manifiesto" in href.lower() or "manifiesto" in texto:
                        continue
                    if "consultar_pdf" in href and (
                        "ver pdf" in texto or "consultar_pdf?" in href
                    ):
                        pdf_url = href
                        break
                if not pdf_url:
                    for link in tr.select('a[href*="consultar_pdf?"]'):
                        href = link.get("href") or ""
                        if "manifiesto" not in href.lower():
                            pdf_url = href
                            break

            if monto is None:
                continue

            fecha = (
                (a.get("data-fecha_trans") or "").strip()
                or (a.get("data-fecha_entrega") or "").strip()
            )
            if not fecha and tr:
                tds = tr.find_all("td")
                if len(tds) > 1:
                    celda = tds[1].get_text(" ", strip=True)
                    m_fecha = re.search(
                        r"(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?)",
                        celda,
                    )
                    if m_fecha:
                        fecha = m_fecha.group(1)

            seen_facturas.add(factura)
            items.append(
                FacturaDetalle(
                    factura=factura,
                    fecha=fecha,
                    cliente=(a.get("data-nombre_cliente") or "").strip(),
                    medio_key=medio_key,
                    medio_nombre=medio_nombre,
                    total=round(monto, 2),
                    pdf_url=pdf_url,
                    pasarela=PASARELA_POR_MEDIO.get(medio_key, ""),
                )
            )
        return items

    def _parse_total_paginas(self, html: str, pagina_actual: int) -> int:
        soup = BeautifulSoup(html, "lxml")
        pages: list[int] = []
        for a in soup.select("#paginacion a[data-page]"):
            try:
                pages.append(int(a.get("data-page")))
            except (TypeError, ValueError):
                continue
        if pages:
            return max(1, max(pages))

        text = soup.get_text(" ", strip=True)
        m = re.search(r"(\d+)\s+facturas?\s+de\s+venta\s+encontradas", text, re.I)
        if m:
            n = int(m.group(1))
            links = len(soup.select("a.ver-trazabilidad[data-total]"))
            por_pagina = links or 50
            return max(1, (n + por_pagina - 1) // por_pagina)
        return max(1, pagina_actual)


def _clasificar_medio_pdf(label: str) -> tuple[str, str, str]:
    """Devuelve (medio_key, medio_nombre, pasarela) según el texto del PDF."""
    raw = (label or "").strip()
    low = re.sub(r"\s+", " ", raw).lower()
    if low.startswith("anticipo"):
        # Conserva ref. tipo Anticipo MED167
        nombre = re.sub(r"\s+", " ", raw).strip() or "Anticipo"
        return "anticipo", nombre, ""
    if "transferencia" in low:
        return "transferencias", "Transferencia bancaria", ""
    if "mercado" in low and "pago" in low:
        return "pago_electronico", "Pago electrónico", "Mercado Pago"
    if "electr" in low:
        return "pago_electronico", "Pago electrónico", "Mercado Pago"
    if "sistecr" in low:
        return "sistecredito", "Sistecrédito", ""
    if "addi" in low:
        return "addi", "Addi", ""
    if "tarjeta" in low or "crédit" in low or "credit" in low or "débito" in low:
        return "tarjeta", "Tarjeta crédito y débito", ""
    if "efectivo" in low:
        return "efectivo", "Efectivo", ""
    # Fallback: no mapeado → se ignora en multi
    return "", raw, ""


def _parse_medios_desde_texto(text: str) -> list[dict]:
    """Parte la sección MEDIOS DE PAGO en N entradas con su valor."""
    m_sec = re.search(
        r"MEDIOS\s+DE\s+PAGO\s*:?\s*(.*?)(?="
        r"DETALLE\s+DE\s+IMPUESTOS|Observaci[oó]n|CUFE|"
        r"Firma\s+y\s+sello|Documento\s+electr[oó]nico|$)",
        text,
        re.I | re.S,
    )
    section = m_sec.group(1) if m_sec else text

    label_re = (
        r"(Anticipo(?:\s+[A-Z]{2,5}-?\d+)?|"
        r"Transferencia\s+bancaria|"
        r"Pago\s+electr[oó]nico|"
        r"Mercado\s*Pago|"
        r"Efectivo|"
        r"Tarjeta(?:\s+cr[eé]dito\s+y\s+d[eé]bito)?|"
        r"Sistecr[eé]dito|"
        r"Addi)"
    )
    pattern = re.compile(
        label_re
        + r"(.*?)"
        + r"Valor\s*:\s*\$?\s*([\d.,]+)",
        re.I | re.S,
    )

    medios: list[dict] = []
    for m in pattern.finditer(section):
        label = m.group(1).strip()
        detalle = m.group(2) or ""
        monto = _parse_money(m.group(3))
        if monto is None:
            continue
        key, nombre, pasarela = _clasificar_medio_pdf(label)
        if not key:
            # Mercado Pago solo como etiqueta
            if "mercado" in label.lower():
                key, nombre, pasarela = (
                    "pago_electronico",
                    "Pago electrónico",
                    "Mercado Pago",
                )
            else:
                continue

        banco = ""
        cuenta = ""
        m_bco = re.search(
            r"(BANCOLOMBIA|DAVIVIENDA|BANCO DE BOGOT[AÁ]|NEQUI|BBVA)\s*#?\s*(\d{6,})",
            detalle,
            re.I,
        )
        if m_bco:
            banco = m_bco.group(1).strip().upper()
            cuenta = m_bco.group(2).strip()
        else:
            m_cta = re.search(r"Cuenta destino:[^\d\n]*(\d{6,})", detalle, re.I)
            if m_cta:
                cuenta = m_cta.group(1).strip()
            if re.search(r"BANCOLOMBIA", detalle, re.I):
                banco = "BANCOLOMBIA"

        if key == "pago_electronico" and not pasarela:
            pasarela = "Mercado Pago"

        medios.append(
            {
                "medio_key": key,
                "medio_nombre": nombre,
                "total": round(monto, 2),
                "banco": banco,
                "cuenta": cuenta,
                "pasarela": pasarela,
            }
        )
    return medios


def parse_pdf_pago(pdf_bytes: bytes) -> dict:
    """Extrae numeración y uno o varios medios de pago (con montos) del PDF."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    text_n = re.sub(r"[ \t]+", " ", text)

    numeracion = ""
    m_num = re.search(r"No\.\s*:\s*([A-Z]{2,5}-\d+)", text_n, re.I)
    if m_num:
        numeracion = m_num.group(1).strip().upper()

    medios = _parse_medios_desde_texto(text)
    if not medios:
        # Reintento sobre texto aplanado (pypdf a veces junta líneas)
        medios = _parse_medios_desde_texto(text_n)

    medio_nombre = ""
    banco = ""
    cuenta = ""
    pasarela = ""
    if medios:
        primero = medios[0]
        medio_nombre = primero["medio_nombre"]
        banco = primero.get("banco") or ""
        cuenta = primero.get("cuenta") or ""
        pasarela = primero.get("pasarela") or ""
    else:
        m_medio = re.search(
            r"MEDIOS DE PAGO:\s*\n?\s*([^\n]+?)(?:\s*-\s*Cuenta|\s*-\s*Valor|$)",
            text_n,
            re.I,
        )
        if m_medio:
            medio_nombre = m_medio.group(1).strip(" -\t")
        if not medio_nombre and re.search(r"Transferencia bancaria", text_n, re.I):
            medio_nombre = "Transferencia bancaria"
        if re.search(r"Mercado\s*Pago", text_n, re.I):
            pasarela = "Mercado Pago"
        m_bco = re.search(
            r"(BANCOLOMBIA|DAVIVIENDA|BANCO DE BOGOT[AÁ]|NEQUI|BBVA)\s*#?\s*(\d{6,})",
            text_n,
            re.I,
        )
        if m_bco:
            banco = m_bco.group(1).strip().upper()
            cuenta = m_bco.group(2).strip()

    return {
        "medio_nombre": medio_nombre,
        "banco": banco,
        "cuenta": cuenta,
        "numeracion": numeracion,
        "pasarela": pasarela,
        "medios": medios,
        "texto": text_n,
    }


def resumen_a_dict(resumen: ResumenVentas) -> dict:
    return {
        "ok": True,
        "codigo": resumen.codigo,
        "sucursal_nombre": resumen.sucursal_nombre,
        "desde": resumen.desde,
        "hasta": resumen.hasta,
        "total_general": resumen.total_general,
        "facturas_total": resumen.facturas_total,
        "cuenta_esperada": resumen.cuenta_esperada,
        "medios": [asdict(m) for m in resumen.medios],
        "medios_keys": [m.key for m in resumen.medios],
        "por_sucursal": [asdict(b) for b in resumen.por_sucursal],
        "detalle": [asdict(d) for d in resumen.detalle],
    }
