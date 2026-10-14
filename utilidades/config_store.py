"""Lectura/escritura de configuración local (.env)."""

from __future__ import annotations

import os
import re
from pathlib import Path

from app_paths import data_root

ROOT = data_root()
ENV_PATH = ROOT / ".env"

DEFAULTS = {
    "EFFI_SESSION": "",
    "EFFI_BASE_URL": "https://effi.com.co",
    "EFFI_USER": "",
    "EFFI_PASSWORD": "",
    "PANEL_USER": "admin",
    "PANEL_PASSWORD": "admin123",
    "SECRET_KEY": "effi-local-panel-cambia-esto",
}

# Extrae ci_session aunque el usuario pegue un cURL completo.
_CI_SESSION_RE = re.compile(
    r"(?:ci_session=|['\"]ci_session['\"]\s*[:=]\s*['\"])([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

_ENV_KEYS = (
    "EFFI_SESSION",
    "EFFI_BASE_URL",
    "EFFI_USER",
    "EFFI_PASSWORD",
    "PANEL_USER",
    "PANEL_PASSWORD",
    "SECRET_KEY",
)


def extract_ci_session(raw: str) -> str:
    """Acepta solo el valor o un cURL/header Cookie completo."""
    raw = (raw or "").strip()
    if not raw:
        return ""

    if "ci_session=" in raw.lower() or "curl " in raw.lower():
        match = _CI_SESSION_RE.search(raw)
        if match:
            return match.group(1).strip()

    if "\n" in raw or "curl " in raw.lower():
        return ""

    return raw.strip().strip("'\"")


def _parse_env(text: str) -> dict[str, str]:
    data = dict(DEFAULTS)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def load_config() -> dict[str, str]:
    if ENV_PATH.exists():
        cfg = _parse_env(ENV_PATH.read_text(encoding="utf-8"))
        cfg["EFFI_SESSION"] = extract_ci_session(cfg.get("EFFI_SESSION", ""))
        return cfg
    return dict(DEFAULTS)


def save_config(updates: dict[str, str]) -> dict[str, str]:
    current = load_config()
    cleaned = {k: (v or "").strip() for k, v in updates.items()}
    if "EFFI_SESSION" in cleaned:
        cleaned["EFFI_SESSION"] = extract_ci_session(cleaned["EFFI_SESSION"])
    current.update(cleaned)

    lines = [f"{key}={current.get(key, DEFAULTS.get(key, ''))}" for key in _ENV_KEYS]
    lines.append("")
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")

    for key in _ENV_KEYS:
        os.environ[key] = current.get(key, DEFAULTS.get(key, ""))
    return current


def mask_cookie(value: str) -> str:
    value = extract_ci_session(value)
    if not value:
        return "(sin cookie)"
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]} ({len(value)} caracteres)"


def looks_like_placeholder(value: str) -> bool:
    value = extract_ci_session(value).lower()
    if not value:
        return True
    return bool(re.search(r"pega_aqui|redacted|tu_ci_session|xxx|curl ", value))
