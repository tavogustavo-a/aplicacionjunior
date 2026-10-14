"""
Cliente mínimo para consultar Clientes en EFFI ERP.

La búsqueda usa GET /app/tercero/cliente?nombre=...
y autentica con la cookie de sesión ci_session (CodeIgniter).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("EFFI_BASE_URL", "https://effi.com.co").rstrip("/")
CLIENTES_PATH = "/app/tercero/cliente"


class EffiError(Exception):
    """Error distinguible al hablar con EFFI."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


PAGE_SIZE = 50


@dataclass
class Cliente:
    nombre: str
    nit: str = ""
    telefono: str = ""
    direccion: str = ""
    email: str = ""


@dataclass
class BusquedaResultado:
    clientes: list[Cliente]
    pagina: int = 1
    total_paginas: int = 1
    total: int = 0
    por_pagina: int = PAGE_SIZE


class EffiClientes:
    def __init__(self, session_cookie: Optional[str] = None, base_url: str = BASE_URL):
        cookie = session_cookie or os.getenv("EFFI_SESSION")
        if not cookie:
            raise EffiError(
                "SIN_COOKIE",
                "Falta la cookie ci_session. Pégala y guárdala antes de buscar.",
            )

        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.cookies.set("ci_session", cookie, domain="effi.com.co")
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/150.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Accept-Language": "es",
                "Referer": f"{self.base_url}{CLIENTES_PATH}",
            }
        )

    def _get_clientes(self, nombre: str = "", pagina: int = 1) -> requests.Response:
        url = urljoin(self.base_url + "/", CLIENTES_PATH.lstrip("/"))
        params: dict[str, str | int] = {}
        if nombre:
            params["nombre"] = nombre
        # EFFI: page=1 se omite; page>=2 usa ?page=N
        if pagina > 1:
            params["page"] = pagina

        try:
            resp = self.session.get(url, params=params, timeout=30)
        except requests.Timeout as exc:
            raise EffiError(
                "TIMEOUT",
                "EFFI tardó demasiado en responder. Revisa tu internet e inténtalo otra vez.",
            ) from exc
        except requests.ConnectionError as exc:
            raise EffiError(
                "SIN_RED",
                "No se pudo conectar con EFFI. Revisa tu internet o si effi.com.co está caído.",
            ) from exc
        except requests.RequestException as exc:
            raise EffiError(
                "RED",
                f"Error de red al consultar EFFI: {exc}",
            ) from exc

        if resp.status_code == 404:
            raise EffiError(
                "NO_ENCONTRADO",
                "EFFI respondió 404: no se encontró la página de clientes.",
            )
        if resp.status_code in (401, 403):
            raise EffiError(
                "SIN_PERMISO",
                "EFFI rechazó el acceso (401/403). La sesión puede haber caducado.",
            )
        if resp.status_code >= 500:
            raise EffiError(
                "EFFI_CAIDO",
                f"EFFI falló con error {resp.status_code}. Intenta más tarde.",
            )
        if resp.status_code >= 400:
            raise EffiError(
                "HTTP",
                f"EFFI respondió con error HTTP {resp.status_code}.",
            )

        if self._es_pagina_login(resp):
            raise EffiError(
                "SESION",
                "Sesión inválida o expirada. EFFI mostró el login. "
                "Copia de nuevo la ci_session desde Chrome y guárdala.",
            )
        return resp

    def probar_conexion(self) -> str:
        """Verifica que la cookie abre Clientes en EFFI. Devuelve mensaje OK."""
        resp = self._get_clientes()
        if "cliente" in resp.url.lower() or "tercero" in resp.url.lower():
            return "Conexión correcta: EFFI aceptó la cookie y cargó Clientes."
        if len(resp.text) > 500 and not self._es_pagina_login(resp):
            return "Conexión correcta: EFFI respondió con sesión activa."
        raise EffiError(
            "RESPUESTA",
            "EFFI respondió, pero la página no parece la de Clientes. Revisa la cookie.",
        )

    def buscar(
        self,
        nombre: str = "",
        pagina: int = 1,
        guardar_html: Optional[str] = None,
    ) -> BusquedaResultado:
        """Busca clientes por nombre. EFFI entrega ~50 por página (?page=N)."""
        pagina = max(1, int(pagina or 1))
        resp = self._get_clientes(nombre=nombre, pagina=pagina)

        if guardar_html:
            with open(guardar_html, "w", encoding="utf-8") as f:
                f.write(resp.text)

        clientes = self._parsear_tabla(resp.text)
        total, total_paginas, pagina_actual = self._parsear_paginacion(
            resp.text, fallback_count=len(clientes), pagina=pagina
        )
        return BusquedaResultado(
            clientes=clientes,
            pagina=pagina_actual,
            total_paginas=total_paginas,
            total=total,
            por_pagina=PAGE_SIZE,
        )

    def _parsear_paginacion(
        self, html: str, fallback_count: int, pagina: int
    ) -> tuple[int, int, int]:
        soup = BeautifulSoup(html, "lxml")
        total = fallback_count
        m = re.search(
            r"(\d+)\s+resultados?\s+encontrados",
            soup.get_text(" ", strip=True),
            flags=re.IGNORECASE,
        )
        if m:
            total = int(m.group(1))

        pages = []
        for a in soup.select("#paginacion a[data-page]"):
            try:
                pages.append(int(a.get("data-page")))
            except (TypeError, ValueError):
                continue

        total_paginas = max(pages) if pages else max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        active = soup.select_one("#paginacion li.active a[data-page]")
        if active and active.get("data-page"):
            try:
                pagina = int(active.get("data-page"))
            except ValueError:
                pass

        if total == 0 and fallback_count == 0:
            return 0, 1, 1
        return total, max(1, total_paginas), max(1, pagina)

    def _es_pagina_login(self, resp: requests.Response) -> bool:
        url = resp.url.lower()
        html = resp.text.lower()
        if any(x in url for x in ("/ingreso", "/login", "validar_usuario")):
            return True
        markers = (
            'id="login"',
            "effi erp | ingreso",
            "validar_usuario",
            'name="password"',
            "email de usuario",
        )
        return sum(1 for m in markers if m in html) >= 2


    def _parsear_tabla(self, html: str) -> list[Cliente]:
        """
        EFFI arma la tabla así (por fila):
        0 acciones | 1 foto | 2 cliente | 3 teléfono | 4 dirección | 5 email | ...

        Lo más limpio son los data-* de los enlaces .modificar / .ver-detalles.
        """
        soup = BeautifulSoup(html, "lxml")
        clientes: list[Cliente] = []

        tabla = None
        for candidate in soup.select("table.table"):
            head = candidate.get_text(" ", strip=True).lower()
            if "cliente" in head and (
                "teléfono" in head or "telefono" in head or "email" in head
            ):
                tabla = candidate
                break
        if tabla is None:
            tabla = soup.select_one("div.table-responsive table") or soup.find("table")
        if not tabla:
            return clientes

        for tr in tabla.select("tbody tr"):
            link = tr.select_one("a.modificar") or tr.select_one("a.ver-detalles")
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue

            nombre = self._extraer_nombre_completo(link, tds)
            nit = ""
            telefono = ""
            email = ""
            direccion = ""

            if link:
                nit = (link.get("data-numero") or "").strip()
                telefono = (
                    link.get("data-celular")
                    or link.get("data-telefono1")
                    or link.get("data-whatsapp")
                    or ""
                ).strip()
                email = (link.get("data-email") or "").strip()

            if not nit and len(tds) > 2:
                bloque = tds[2].get_text(" ", strip=True)
                nit_match = re.search(
                    r"(?:NIT|CC|CE|Nit)[.\s]*([0-9.\-]+)",
                    bloque,
                    flags=re.IGNORECASE,
                )
                nit = nit_match.group(1) if nit_match else ""

            if len(tds) > 3 and not telefono:
                telefono = re.sub(
                    r"^(Celular|Teléfono|WhatsApp)\s*:\s*",
                    "",
                    tds[3].get_text(" ", strip=True),
                    flags=re.IGNORECASE,
                ).strip()

            if len(tds) > 4:
                direccion = re.sub(
                    r"^[•·\-\s]+", "", tds[4].get_text(" ", strip=True)
                ).strip()

            if len(tds) > 5 and not email:
                email = tds[5].get_text(" ", strip=True)

            if not nombre and not nit:
                continue

            if "ver más detalles" in nombre.lower() or "modificar" in nombre.lower():
                continue

            clientes.append(
                Cliente(
                    nombre=nombre,
                    nit=nit,
                    telefono=telefono,
                    direccion=direccion,
                    email=email,
                )
            )

        return clientes

    def _extraer_nombre_completo(self, link, tds) -> str:
        """Prioriza el nombre visible en la columna Cliente (nombre completo)."""
        from_attr = ""
        if link is not None:
            from_attr = (
                link.get("data-nombre")
                or link.get("data-nombre_cliente")
                or ""
            ).strip()

        from_cell = ""
        if len(tds) > 2:
            lines = [
                ln.strip()
                for ln in tds[2].get_text("\n", strip=True).split("\n")
                if ln.strip()
            ]
            for ln in lines:
                low = ln.lower()
                if re.match(r"^(cc|nit|ce|tercero)\b", low):
                    continue
                if re.match(r"^[\d.\-\s]+$", ln):
                    continue
                from_cell = ln
                break

        # El de la celda suele ser el nombre completo mostrado en EFFI
        if len(from_cell) >= len(from_attr):
            return from_cell or from_attr
        return from_attr or from_cell


def main() -> int:
    nombre = " ".join(sys.argv[1:]).strip() or "alva"
    guardar = os.getenv(
        "EFFI_SAVE_HTML",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ultimo_clientes.html"),
    )
    os.makedirs(os.path.dirname(guardar) or ".", exist_ok=True)

    try:
        api = EffiClientes()
        resultado = api.buscar(nombre=nombre, guardar_html=guardar)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Búsqueda: {nombre!r}")
    print(
        f"Página {resultado.pagina}/{resultado.total_paginas} · "
        f"{len(resultado.clientes)} en esta página · total {resultado.total}"
    )
    print(f"HTML guardado en: {guardar}")
    print("-" * 60)

    for i, c in enumerate(resultado.clientes[:30], start=1):
        print(f"{i}. {c.nombre} | {c.nit} | {c.telefono} | {c.email}")

    if len(resultado.clientes) > 30:
        print(f"... y {len(resultado.clientes) - 30} más")

    if not resultado.clientes:
        print(
            "No se parsearon filas. Revisa el HTML guardado para ajustar "
            "el selector de la tabla (puede variar según tu vista)."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
