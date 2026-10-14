"""Rutas de la app: desarrollo vs .exe (PyInstaller)."""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _source_dir() -> Path:
    return Path(__file__).resolve().parent


def data_root() -> Path:
    """Carpeta escribible: al lado del .exe, en la raíz del proyecto."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    source = _source_dir()
    if source.name == "utilidades":
        return source.parent
    return source


def resource_root() -> Path:
    """Templates/static empaquetados (MEIPASS) o la carpeta del código."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return _source_dir()


def debug_dump(name: str) -> str | None:
    """Ruta de un volcado HTML de depuración dentro de utilidades.

    El .exe no escribe estos archivos: solo sirven al desarrollar.
    """
    if is_frozen():
        return None
    folder = data_root() / "utilidades"
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder / name)
