"""Utilidades de seguridad del panel."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import abort, request, session

MAX_LOGIN_ATTEMPTS = 5
LOCK_MINUTES = 15


def ensure_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["csrf_token"] = token
    return token


def validate_csrf() -> None:
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    if not sent or not expected or not secrets.compare_digest(str(sent), str(expected)):
        abort(400, description="Token CSRF inválido. Recarga la página e inténtalo de nuevo.")


def csrf_protect(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            validate_csrf()
        return view(*args, **kwargs)

    return wrapped


def validate_username(username: str) -> str | None:
    username = (username or "").strip()
    if not username:
        return "El usuario no puede estar vacío."
    if len(username) > 80:
        return "El usuario es demasiado largo."
    return None


def validate_password(password: str, *, required: bool = True) -> str | None:
    if not password:
        return "La contraseña es obligatoria." if required else None
    if len(password) > 200:
        return "La contraseña es demasiado larga."
    return None


def is_user_locked(user) -> bool:
    until = getattr(user, "blocked_until", None)
    if not until:
        return False
    return until > datetime.utcnow()


def register_failed_login(user) -> None:
    user.failed_attempts = int(user.failed_attempts or 0) + 1
    if user.failed_attempts >= MAX_LOGIN_ATTEMPTS:
        user.blocked_until = datetime.utcnow() + timedelta(minutes=LOCK_MINUTES)
        user.failed_attempts = 0


def clear_failed_login(user) -> None:
    user.failed_attempts = 0
    user.blocked_until = None


def lock_remaining_minutes(user) -> int:
    until = getattr(user, "blocked_until", None)
    if not until:
        return 0
    delta = until - datetime.utcnow()
    return max(1, int(delta.total_seconds() // 60) + 1)
