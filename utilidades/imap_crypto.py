"""Cifrado local de secretos IMAP (Fernet derivado del SECRET_KEY)."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from config_store import load_config


def _fernet() -> Fernet:
    secret = (load_config().get("SECRET_KEY") or "effi-local-panel").encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_secret(raw: str) -> str:
    raw = raw or ""
    if not raw:
        return ""
    return _fernet().encrypt(raw.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        # Compat: valores antiguos en texto plano
        return token
