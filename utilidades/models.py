"""Modelos SQLite del panel."""

from __future__ import annotations

import json
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    failed_attempts = db.Column(db.Integer, default=0, nullable=False)
    blocked_until = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def set_password(self, raw: str) -> None:
        self.password = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password, raw)


class VentasRegistro(db.Model):
    """Historial de búsquedas del modo ventas."""

    __tablename__ = "ventas_registros"

    id = db.Column(db.Integer, primary_key=True)
    sucursal = db.Column(db.String(10), nullable=False, index=True)
    sucursal_nombre = db.Column(db.String(80), nullable=False, default="")
    desde = db.Column(db.String(10), nullable=False)
    hasta = db.Column(db.String(10), nullable=False)
    facturas_total = db.Column(db.Integer, default=0, nullable=False)
    total_general = db.Column(db.Float, default=0.0, nullable=False)
    usuario = db.Column(db.String(80), nullable=False, default="")
    payload_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def set_payload(self, payload: dict | None) -> None:
        if not payload:
            self.payload_json = None
            return
        self.payload_json = json.dumps(payload, ensure_ascii=False)

    def get_payload(self) -> dict | None:
        if not self.payload_json:
            return None
        try:
            data = json.loads(self.payload_json)
        except (TypeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sucursal": self.sucursal,
            "sucursal_nombre": self.sucursal_nombre,
            "desde": self.desde,
            "hasta": self.hasta,
            "facturas_total": self.facturas_total,
            "total_general": self.total_general,
            "usuario": self.usuario,
            "tiene_export": bool(self.payload_json),
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M")
            if self.created_at
            else "",
        }


class ImapConfig(db.Model):
    """Configuración IMAP para Servicio Trailero."""

    __tablename__ = "imap_configs"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False, default="")
    host = db.Column(db.String(255), nullable=False)
    port = db.Column(db.Integer, nullable=False, default=993)
    usuario = db.Column(db.String(255), nullable=False)
    password_enc = db.Column(db.Text, nullable=False, default="")
    carpeta = db.Column(db.String(255), nullable=False, default="INBOX")
    usar_ssl = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "host": self.host,
            "port": self.port,
            "usuario": self.usuario,
            "carpeta": self.carpeta or "INBOX",
            "usar_ssl": bool(self.usar_ssl),
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M")
            if self.created_at
            else "",
        }


class TraileroRegistro(db.Model):
    """Historial de ejecuciones del Servicio Trailero."""

    __tablename__ = "trailero_registros"

    id = db.Column(db.Integer, primary_key=True)
    desde = db.Column(db.String(10), nullable=False)
    hasta = db.Column(db.String(10), nullable=False)
    hora_desde = db.Column(db.String(8), nullable=False, default="00:00")
    imap_usuario = db.Column(db.String(255), nullable=False, default="")
    tanqueos_total = db.Column(db.Integer, default=0, nullable=False)
    pdfs_error = db.Column(db.Integer, default=0, nullable=False)
    total_general = db.Column(db.Float, default=0.0, nullable=False)
    usuario = db.Column(db.String(80), nullable=False, default="")
    payload_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def set_payload(self, payload: dict | None) -> None:
        if not payload:
            self.payload_json = None
            return
        self.payload_json = json.dumps(payload, ensure_ascii=False)

    def get_payload(self) -> dict | None:
        if not self.payload_json:
            return None
        try:
            data = json.loads(self.payload_json)
        except (TypeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def to_dict(self) -> dict:
        hora = (self.hora_desde or "").strip()
        if not hora:
            payload = self.get_payload() or {}
            hora = (payload.get("hora_desde") or "00:00").strip()
        return {
            "id": self.id,
            "desde": self.desde,
            "hasta": self.hasta,
            "hora_desde": hora or "00:00",
            "imap_usuario": self.imap_usuario,
            "tanqueos_total": self.tanqueos_total,
            "pdfs_error": self.pdfs_error,
            "total_general": self.total_general,
            "usuario": self.usuario,
            "tiene_export": bool(self.payload_json),
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M")
            if self.created_at
            else "",
        }


class TraileroExcelConfig(db.Model):
    """Ruta/hoja del Excel Acpm donde se escriben los tanqueos al Ejecutar."""

    __tablename__ = "trailero_excel_config"

    id = db.Column(db.Integer, primary_key=True)
    ruta = db.Column(db.String(500), nullable=False, default="")
    hoja = db.Column(db.String(120), nullable=False, default="Acpm")
    activo = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ruta": self.ruta or "",
            "hoja": self.hoja or "Acpm",
            "activo": bool(self.activo),
        }

