"""Interfaz web local: login, usuarios SQLite, cookie EFFI y búsqueda."""

from __future__ import annotations

import json
import os
import threading
import webbrowser
from dataclasses import asdict
from datetime import timedelta
from functools import wraps
from pathlib import Path
from queue import Empty, Queue

from flask import (
    Flask,
    Response,
    copy_current_request_context,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    stream_with_context,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from app_paths import data_root, debug_dump, is_frozen, resource_root
from sqlalchemy import text

from config_store import (
    extract_ci_session,
    load_config,
    looks_like_placeholder,
    mask_cookie,
    save_config,
)
from effi_clientes import EffiClientes, EffiError
from effi_login import entrar as entrar_effi_cuenta
from effi_ventas import MEDIOS, ORDEN_TODOS, SUCURSALES_UI, EffiVentas, resumen_a_dict
from ventas_export import (
    export_pdf as ventas_export_pdf,
    export_txt as ventas_export_txt,
    export_xlsx as ventas_export_xlsx,
)
from trailero_export import (
    export_pdf as trailero_export_pdf,
    export_txt as trailero_export_txt,
    export_xlsx as trailero_export_xlsx,
)
from trailero_excel_write import append_tanqueos_excel
from trailero_imap import ejecutar_trailero
from models import (
    ImapConfig,
    TraileroExcelConfig,
    TraileroRegistro,
    User,
    VentasRegistro,
    db,
)
from imap_client import probar_imap
from imap_crypto import decrypt_secret, encrypt_secret
from security import (
    MAX_LOGIN_ATTEMPTS,
    clear_failed_login,
    csrf_protect,
    ensure_csrf_token,
    is_user_locked,
    lock_remaining_minutes,
    register_failed_login,
    validate_password,
    validate_username,
)

ROOT = data_root()
RES_ROOT = resource_root()
DB_PATH = ROOT / "instance" / "panel.db"

app = Flask(
    __name__,
    template_folder=str(RES_ROOT / "templates"),
    static_folder=str(RES_ROOT / "static"),
)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH.as_posix()}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Panel local: mantener sesión varios meses (hasta pulsar Salir).
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=180)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB

cfg0 = load_config()
app.secret_key = cfg0.get("SECRET_KEY") or "effi-local-panel"

# Solo en el servidor, detrás de nginx (junior.tuyaho.com). El .exe local no lo activa.
if os.environ.get("BEHIND_PROXY", "").strip() == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["PREFERRED_URL_SCHEME"] = "https"

db.init_app(app)


def seed_admin() -> None:
    """Crea el primer usuario desde .env si la tabla está vacía."""
    if User.query.count() > 0:
        return
    cfg = load_config()
    user = User(
        username=cfg.get("PANEL_USER") or "admin",
        enabled=True,
        failed_attempts=0,
    )
    user.set_password(cfg.get("PANEL_PASSWORD") or "admin123")
    db.session.add(user)
    db.session.commit()


def ensure_schema() -> None:
    """Agrega columnas nuevas si la BD ya existía."""
    with db.engine.begin() as conn:
        cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()
        }
        if "failed_attempts" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE users ADD COLUMN failed_attempts "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            )
        if "blocked_until" not in cols:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN blocked_until DATETIME")
            )

        reg_cols = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(ventas_registros)")
            ).fetchall()
        }
        if reg_cols and "payload_json" not in reg_cols:
            conn.execute(
                text("ALTER TABLE ventas_registros ADD COLUMN payload_json TEXT")
            )

        imap_cols = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(imap_configs)")
            ).fetchall()
        }
        if imap_cols and "carpeta" not in imap_cols:
            conn.execute(
                text(
                    "ALTER TABLE imap_configs ADD COLUMN carpeta "
                    "VARCHAR(255) NOT NULL DEFAULT 'INBOX'"
                )
            )

        trailero_cols = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(trailero_registros)")
            ).fetchall()
        }
        if trailero_cols and "hora_desde" not in trailero_cols:
            conn.execute(
                text(
                    "ALTER TABLE trailero_registros ADD COLUMN hora_desde "
                    "VARCHAR(8) NOT NULL DEFAULT '00:00'"
                )
            )


MAX_REGISTROS = 10


def recortar_registros(model, limite: int = MAX_REGISTROS) -> None:
    """Deja solo los más recientes. El siguiente reemplaza al más antiguo."""
    sobrantes = model.query.order_by(model.id.desc()).offset(limite).all()
    if not sobrantes:
        return
    for row in sobrantes:
        db.session.delete(row)
    db.session.commit()


_TABLAS_HISTORIAL = {
    "ventas_registros": "ventas_export",
    "trailero_registros": "trailero_export",
}


def _olvidar_export(clave: str, rid: int | None = None) -> None:
    """Quita de la sesión la copia del registro que ya no está en la base."""
    actual = session.get(clave)
    if rid is None or (isinstance(actual, dict) and actual.get("registro_id") == rid):
        session.pop(clave, None)


def vaciar_registros(model, clave_sesion: str) -> int:
    """Borra todas las filas del historial, también las que no se ven en la lista."""
    tabla = model.__tablename__
    if tabla not in _TABLAS_HISTORIAL or _TABLAS_HISTORIAL[tabla] != clave_sesion:
        raise ValueError("tabla")
    antes = db.session.query(model).count()
    db.session.query(model).delete(synchronize_session=False)
    db.session.commit()
    quedan = db.session.execute(text(f"SELECT COUNT(*) FROM {tabla}")).scalar() or 0
    if quedan:
        db.session.execute(text(f"DELETE FROM {tabla}"))
        db.session.commit()
    _olvidar_export(clave_sesion)
    return int(antes)


with app.app_context():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db.create_all()
    ensure_schema()
    seed_admin()
    recortar_registros(VentasRegistro)
    recortar_registros(TraileroRegistro)


@app.context_processor
def inject_security():
    return {"csrf_token": ensure_csrf_token()}


@app.after_request
def set_security_headers(response):
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # No forzar no-store en static/offline/sw: el service worker necesita cachearlos.
    path = request.path or ""
    if (
        not path.startswith("/static/")
        and path not in ("/sw.js", "/offline")
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


# Rutas que solo aceptan POST: no se pueden usar como "next" tras login.
_POST_ONLY_PREFIXES = (
    "/guardar-config",
    "/probar-conexion",
    "/usuarios/crear",
    "/logout",
)


def _safe_next_url(raw: str | None) -> str:
    nxt = (raw or "").strip()
    if not nxt.startswith("/") or nxt.startswith("//"):
        return url_for("index")
    if any(nxt == p or nxt.startswith(p + "/") for p in _POST_ONLY_PREFIXES):
        return url_for("index")
    if nxt.startswith("/usuarios/") and (
        nxt.endswith("/toggle") or nxt.endswith("/eliminar") or "/editar" in nxt
    ):
        # editar GET sí es válido; toggle/eliminar no
        if nxt.endswith("/toggle") or nxt.endswith("/eliminar"):
            return url_for("usuarios")
    return nxt


def admin_username() -> str:
    return (load_config().get("PANEL_USER") or "admin").strip()


def is_admin_user(user: User | None) -> bool:
    if not user:
        return False
    return user.username == admin_username()


def current_user() -> User | None:
    return db.session.get(User, session.get("user_id"))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            # Tras login solo tiene sentido volver a páginas GET.
            nxt = request.path if request.method == "GET" else url_for("index")
            return redirect(url_for("login", next=_safe_next_url(nxt)))
        user = current_user()
        if not user or not user.enabled:
            session.clear()
            flash("Usuario desactivado o inexistente.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not is_admin_user(user):
            flash("Solo el administrador puede gestionar usuarios.", "error")
            return redirect(url_for("index"))
        return view(*args, **kwargs)

    return wrapped


def _credenciales_effi(cfg: dict) -> tuple[str, str]:
    return (cfg.get("EFFI_USER") or "").strip(), cfg.get("EFFI_PASSWORD") or ""


def _tiene_acceso_effi(cfg: dict) -> bool:
    cookie = cfg.get("EFFI_SESSION", "")
    if cookie and not looks_like_placeholder(cookie):
        return True
    user, password = _credenciales_effi(cfg)
    return bool(user and password)


def _renovar_sesion_effi(cfg: dict, base_url: str) -> str:
    user, password = _credenciales_effi(cfg)
    if not user or not password:
        raise EffiError(
            "SIN_CLAVE",
            "La sesión de EFFI no sirve y no hay usuario y contraseña guardados. "
            "Escríbelos arriba y pulsa Entrar y guardar, o pega la cookie a mano.",
        )
    cookie = entrar_effi_cuenta(user, password, base_url)
    save_config(
        {
            "EFFI_SESSION": cookie,
            "EFFI_BASE_URL": base_url.rstrip("/"),
        }
    )
    return cookie


def _con_effi(cfg: dict, accion, base_url: str | None = None):
    """Ejecuta accion(cookie). Si la sesión caducó, entra de nuevo una sola vez."""
    base = (base_url or cfg.get("EFFI_BASE_URL") or "https://effi.com.co").rstrip("/")
    cookie = cfg.get("EFFI_SESSION") or ""
    renovada = False
    if looks_like_placeholder(cookie):
        cookie = _renovar_sesion_effi(cfg, base)
        renovada = True
    try:
        return accion(cookie), renovada
    except EffiError as exc:
        if exc.code != "SESION" or renovada:
            if exc.code == "SESION" and renovada:
                raise EffiError(
                    "SESION",
                    "Se entró de nuevo, pero EFFI volvió a rechazar la sesión.",
                ) from exc
            raise
        cookie = _renovar_sesion_effi(cfg, base)
        return accion(cookie), True


def _panel_ctx(**extra):
    cfg = load_config()
    cookie = cfg.get("EFFI_SESSION", "")
    user = current_user()
    effi_user, effi_password = _credenciales_effi(cfg)
    data = {
        "base_url": cfg.get("EFFI_BASE_URL", "https://effi.com.co"),
        "cookie_masked": mask_cookie(cookie),
        "cookie_value": "" if looks_like_placeholder(cookie) else cookie,
        "cookie_ok": _tiene_acceso_effi(cfg),
        "effi_user": effi_user,
        "effi_pass_ok": bool(effi_password),
        "usuario": session.get("usuario", ""),
        "user_id": session.get("user_id"),
        "is_admin": is_admin_user(user),
        "admin_name": admin_username(),
        "error_code": None,
        "pagina": 1,
        "total_paginas": 1,
        "total": 0,
        "resultados": None,
        "busqueda": "",
        "sucursales": [
            {"codigo": code, **SUCURSALES_UI[code]}
            for code in (*ORDEN_TODOS, "TODOS")
            if code in SUCURSALES_UI
        ],
        "medios_pago": [
            {"key": key, "nombre": nombre} for key, nombre, _ in MEDIOS
        ],
    }
    data.update(extra)
    return data



@app.route("/offline")
def offline_page():
    return render_template("offline.html", home_url=url_for("index"))


@app.route("/sw.js")
def service_worker():
    resp = send_from_directory(
        app.static_folder, "sw.js", mimetype="application/javascript"
    )
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/login", methods=["GET", "POST"])
@csrf_protect
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = request.form.get("usuario", "").strip()[:80]
        password = request.form.get("password", "")[:200]
        user = User.query.filter_by(username=username).first()

        if user and is_user_locked(user):
            mins = lock_remaining_minutes(user)
            error = f"Cuenta bloqueada por intentos fallidos. Espera {mins} min."
        elif user and not user.enabled:
            error = "Usuario desactivado (OFF)."
        elif user and user.check_password(password):
            clear_failed_login(user)
            db.session.commit()
            # Evita fijación de sesión
            session.clear()
            session.permanent = True
            ensure_csrf_token()
            session["logged_in"] = True
            session["usuario"] = user.username
            session["user_id"] = user.id
            flash("Sesión iniciada.", "ok")
            return redirect(_safe_next_url(request.args.get("next")))
        else:
            if user:
                register_failed_login(user)
                db.session.commit()
                if is_user_locked(user):
                    mins = lock_remaining_minutes(user)
                    error = f"Demasiados intentos. Cuenta bloqueada {mins} min."
                else:
                    left = max(0, MAX_LOGIN_ATTEMPTS - int(user.failed_attempts or 0))
                    error = "Usuario o contraseña incorrectos."
                    if left <= 2:
                        error += f" Te quedan {left} intento(s)."
            else:
                error = "Usuario o contraseña incorrectos."

    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
@csrf_protect
def logout():
    session.clear()
    flash("Sesión cerrada.", "ok")
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@login_required
def index():
    return render_template("index.html", **_panel_ctx(error=None))


@app.route("/guardar-config", methods=["POST"])
@login_required
@csrf_protect
def guardar_config():
    raw_cookie = request.form.get("effi_session", "").strip()[:20000]
    cookie = extract_ci_session(raw_cookie)
    base_url = request.form.get("effi_base_url", "").strip() or "https://effi.com.co"
    if not base_url.startswith("https://"):
        flash("La URL base debe usar HTTPS.", "error")
        return redirect(url_for("index"))


    if not cookie:
        flash(
            "No se encontró ci_session. Pega solo el valor, o un cURL que contenga "
            "ci_session=....",
            "error",
        )
        return redirect(url_for("index"))

    save_config(
        {
            "EFFI_SESSION": cookie,
            "EFFI_BASE_URL": base_url.rstrip("/"),
        }
    )
    if raw_cookie != cookie:
        flash(
            f"Se extrajo ci_session del texto pegado: {cookie[:4]}...{cookie[-4:]}",
            "ok",
        )
    else:
        flash("Cookie guardada.", "ok")
    return redirect(url_for("index"))


@app.route("/entrar-effi", methods=["POST"])
@login_required
@csrf_protect
def entrar_effi():
    """Entra a EFFI con usuario y contraseña, guarda la cookie y las credenciales."""
    cfg = load_config()
    email = request.form.get("effi_user", "").strip()[:255]
    password = request.form.get("effi_password", "")[:200]
    if not password:
        password = cfg.get("EFFI_PASSWORD") or ""
    base_url = (
        request.form.get("effi_base_url", "").strip()
        or cfg.get("EFFI_BASE_URL", "https://effi.com.co")
    )
    if not base_url.startswith("https://"):
        flash("La URL base debe usar HTTPS.", "error")
        return redirect(url_for("index"))
    if not email or not password:
        flash("Escribe el usuario y la contraseña de EFFI.", "error")
        return redirect(url_for("index"))

    try:
        cookie = entrar_effi_cuenta(email, password, base_url)
    except EffiError as exc:
        flash(exc.message, "error")
        return redirect(url_for("index"))
    except Exception as exc:
        flash(f"No se pudo entrar a EFFI: {exc}", "error")
        return redirect(url_for("index"))

    save_config(
        {
            "EFFI_USER": email,
            "EFFI_PASSWORD": password,
            "EFFI_SESSION": cookie,
            "EFFI_BASE_URL": base_url.rstrip("/"),
        }
    )
    flash(
        "Sesión de EFFI guardada. Si la cookie caduca, entra sola con este usuario.",
        "ok",
    )
    return redirect(url_for("index"))


@app.route("/probar-conexion", methods=["POST"])
@login_required
@csrf_protect
def probar_conexion():
    cfg = load_config()
    raw_cookie = request.form.get("effi_session", "").strip()[:20000]
    cookie = extract_ci_session(raw_cookie) or cfg.get("EFFI_SESSION", "")
    base_url = (
        request.form.get("effi_base_url", "").strip()
        or cfg.get("EFFI_BASE_URL", "https://effi.com.co")
    )

    def _probar(cookie_activa: str) -> str:
        api = EffiClientes(session_cookie=cookie_activa, base_url=base_url)
        return api.probar_conexion()

    try:
        if cookie and not looks_like_placeholder(cookie):
            try:
                flash(_probar(cookie), "ok")
                return redirect(url_for("index"))
            except EffiError as exc:
                if exc.code != "SESION":
                    flash(exc.message, "error")
                    return redirect(url_for("index"))
        mensaje, renovada = _con_effi(cfg, _probar, base_url)
        if renovada:
            mensaje = f"{mensaje} La sesión se renovó y quedó guardada."
        flash(mensaje, "ok")
    except EffiError as exc:
        flash(exc.message, "error")
    except Exception as exc:
        flash(f"Error al probar: {exc}", "error")

    return redirect(url_for("index"))


@app.route("/buscar", methods=["GET"])
@login_required
def buscar():
    """Búsqueda por GET para poder refrescar sin el aviso de reenvío de formulario."""
    cfg = load_config()
    base_url = cfg.get("EFFI_BASE_URL", "https://effi.com.co")
    nombre = request.args.get("nombre", "").strip()[:120]
    try:
        pagina = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        pagina = 1

    resultados = []
    error = None
    error_code = None
    total = 0
    total_paginas = 1
    pagina_actual = pagina

    if not _tiene_acceso_effi(cfg):
        error_code = "SIN_COOKIE"
        error = (
            "Falta la sesión de EFFI. Escribe usuario y contraseña y pulsa "
            "Entrar y guardar, o pega la cookie a mano."
        )
    else:
        try:
            def _buscar(cookie_activa: str):
                api = EffiClientes(session_cookie=cookie_activa, base_url=base_url)
                return api.buscar(
                    nombre=nombre,
                    pagina=pagina,
                    guardar_html=debug_dump("ultimo_clientes.html"),
                )

            resultado, renovada = _con_effi(cfg, _buscar, base_url)
            if renovada:
                flash("La cookie había caducado. Se entró de nuevo y quedó guardada.", "ok")
            resultados = [asdict(c) for c in resultado.clientes]
            total = resultado.total
            total_paginas = resultado.total_paginas
            pagina_actual = resultado.pagina
            if not resultados:
                if nombre:
                    error_code = "SIN_RESULTADOS"
                    error = (
                        f"No se encontraron clientes con “{nombre}”, "
                        "o EFFI no devolvió filas legibles."
                    )
                else:
                    error_code = "SIN_RESULTADOS"
                    error = (
                        "No se encontraron clientes (búsqueda vacía = todos). "
                        "Revisa la cookie o utilidades/ultimo_clientes.html."
                    )
        except EffiError as exc:
            error_code = exc.code
            error = exc.message
        except Exception as exc:
            error_code = "DESCONOCIDO"
            error = f"Error inesperado: {exc}"

    return render_template(
        "index.html",
        **_panel_ctx(
            resultados=resultados,
            busqueda=nombre,
            error=error,
            error_code=error_code,
            pagina=pagina_actual,
            total_paginas=total_paginas,
            total=total,
        ),
    )


@app.route("/api/ventas/resumen", methods=["GET"])
@login_required
def api_ventas_resumen():
    """Suma facturas por sucursal/medios y lee PDF (numeración + cuenta).

    Con stream=1 (por defecto) envía NDJSON con progreso de lectura de PDFs.
    """
    cfg = load_config()
    base_url = cfg.get("EFFI_BASE_URL", "https://effi.com.co")
    codigo = request.args.get("sucursal", "").strip().upper()
    desde = request.args.get("desde", "").strip()
    hasta = request.args.get("hasta", "").strip()
    medios_raw = request.args.get("medios", "").strip()
    medios_filtro = [
        m.strip() for m in medios_raw.split(",") if m.strip()
    ] if medios_raw else None
    use_stream = request.args.get("stream", "1").strip() != "0"

    if not _tiene_acceso_effi(cfg):
        return jsonify(
            {
                "ok": False,
                "code": "SIN_COOKIE",
                "error": "Falta la sesión de EFFI. Guarda usuario y contraseña, o pega la cookie.",
            }
        ), 400

    usuario = session.get("usuario") or ""

    def _build_and_save(progress_cb=None):
        def _resumen(cookie_activa: str):
            api = EffiVentas(session_cookie=cookie_activa, base_url=base_url)
            return api.resumen_sucursal(
                codigo=codigo,
                desde=desde,
                hasta=hasta,
                guardar_html=debug_dump("ultimo_factura_v.html"),
                leer_pdf_transferencias=True,
                medios_filtro=medios_filtro,
                progress_cb=progress_cb,
            )
        resumen, _renovada = _con_effi(cfg, _resumen, base_url)
        payload = resumen_a_dict(resumen)
        export_payload = dict(payload)
        export_payload["detalle"] = [
            {k: v for k, v in row.items() if k != "pdf_url"}
            for row in (payload.get("detalle") or [])
        ]
        reg = VentasRegistro(
            sucursal=payload.get("codigo") or codigo,
            sucursal_nombre=payload.get("sucursal_nombre") or "",
            desde=payload.get("desde") or desde,
            hasta=payload.get("hasta") or hasta,
            facturas_total=int(payload.get("facturas_total") or 0),
            total_general=float(payload.get("total_general") or 0),
            usuario=usuario,
        )
        reg.set_payload(export_payload)
        db.session.add(reg)
        db.session.commit()
        recortar_registros(VentasRegistro)
        payload["registro_id"] = reg.id
        return payload, export_payload

    if not use_stream:
        try:
            payload, export_payload = _build_and_save()
            session["ventas_export"] = export_payload
            return jsonify(payload)
        except EffiError as exc:
            return jsonify({"ok": False, "code": exc.code, "error": exc.message}), 400
        except Exception as exc:
            return jsonify({"ok": False, "code": "DESCONOCIDO", "error": str(exc)}), 500

    q: Queue = Queue()

    @copy_current_request_context
    def worker():
        try:
            def progress_cb(ev):
                q.put(ev)

            payload, export_payload = _build_and_save(progress_cb=progress_cb)
            q.put({"type": "done", "payload": payload, "export_payload": export_payload})
        except EffiError as exc:
            q.put({"type": "error", "ok": False, "code": exc.code, "error": exc.message})
        except Exception as exc:
            q.put({"type": "error", "ok": False, "code": "DESCONOCIDO", "error": str(exc)})

    threading.Thread(target=worker, daemon=True).start()

    @stream_with_context
    def generate():
        export_for_session = None
        while True:
            try:
                ev = q.get(timeout=300)
            except Empty:
                yield json.dumps(
                    {
                        "type": "error",
                        "ok": False,
                        "code": "TIMEOUT",
                        "error": "La consulta tardó demasiado.",
                    },
                    ensure_ascii=False,
                ) + "\n"
                break
            kind = ev.get("type")
            if kind == "done":
                export_for_session = ev.get("export_payload")
                if export_for_session is not None:
                    session["ventas_export"] = export_for_session
                    session.modified = True
                out = {"type": "done", "payload": ev.get("payload")}
                yield json.dumps(out, ensure_ascii=False) + "\n"
                break
            if kind == "error":
                yield json.dumps(ev, ensure_ascii=False) + "\n"
                break
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _export_response(payload: dict, formato: str) -> Response:
    formato = (formato or "").strip().lower()
    codigo = payload.get("codigo") or "VEN"
    desde = payload.get("desde") or ""
    hasta = payload.get("hasta") or ""
    base_name = f"ventas_{codigo}_{desde}_{hasta}".replace(":", "")

    if formato in ("txt", "notepad", "texto"):
        return Response(
            ventas_export_txt(payload),
            mimetype="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{base_name}.txt"'
            },
        )
    if formato in ("xlsx", "excel"):
        return Response(
            ventas_export_xlsx(payload),
            mimetype=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": f'attachment; filename="{base_name}.xlsx"'
            },
        )
    if formato == "pdf":
        return Response(
            ventas_export_pdf(payload),
            mimetype="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{base_name}.pdf"'
            },
        )
    raise ValueError("FORMATO")


@app.route("/api/ventas/registros", methods=["GET"])
@login_required
def api_ventas_registros():
    """Historial de búsquedas de ventas con paginación AJAX."""
    codigo = request.args.get("sucursal", "").strip().upper()
    per_raw = (request.args.get("per_page") or request.args.get("limit") or "10").strip().lower()
    try:
        pagina = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        pagina = 1

    q = VentasRegistro.query
    if codigo and codigo != "ALL":
        q = q.filter_by(sucursal=codigo)
    total = q.count()

    if per_raw in ("todos", "all", "0"):
        per_page = total or 1
        show_all = True
    else:
        try:
            per_page = int(per_raw)
        except (TypeError, ValueError):
            per_page = 5
        if per_page not in (5, 10):
            per_page = 10
        show_all = False

    total_paginas = max(1, (total + per_page - 1) // per_page) if total else 1
    if pagina > total_paginas:
        pagina = total_paginas

    query = q.order_by(VentasRegistro.id.desc())
    if show_all:
        rows = query.all()
        pagina = 1
        total_paginas = 1
        per_label = "todos"
    else:
        rows = query.offset((pagina - 1) * per_page).limit(per_page).all()
        per_label = str(per_page)

    return jsonify(
        {
            "ok": True,
            "registros": [r.to_dict() for r in rows],
            "pagina": pagina,
            "per_page": per_label,
            "total": total,
            "total_paginas": total_paginas,
        }
    )


@app.route("/api/ventas/registros", methods=["DELETE"])
@login_required
@csrf_protect
def api_ventas_registros_eliminar_todos():
    n = vaciar_registros(VentasRegistro, "ventas_export")
    return jsonify({"ok": True, "eliminados": n})


@app.route("/api/ventas/registros/<int:rid>", methods=["DELETE"])
@login_required
@csrf_protect
def api_ventas_registro_eliminar(rid: int):
    reg = db.session.get(VentasRegistro, rid)
    if not reg:
        return jsonify({"ok": False, "code": "NO_ENCONTRADO", "error": "Registro no existe."}), 404
    db.session.delete(reg)
    db.session.commit()
    _olvidar_export("ventas_export", rid)
    return jsonify({"ok": True})


@app.route("/api/ventas/registros/<int:rid>/exportar/<formato>", methods=["GET"])
@login_required
def api_ventas_registro_exportar(rid: int, formato: str):
    """Exporta un registro guardado: txt | xlsx | pdf."""
    reg = db.session.get(VentasRegistro, rid)
    if not reg:
        return jsonify({"ok": False, "code": "NO_ENCONTRADO", "error": "Registro no existe."}), 404
    payload = reg.get_payload()
    if not payload or not payload.get("ok"):
        return jsonify(
            {
                "ok": False,
                "code": "SIN_DATOS",
                "error": "Este registro no tiene datos para descargar (haz una búsqueda nueva).",
            }
        ), 400
    try:
        return _export_response(payload, formato)
    except ValueError:
        return jsonify({"ok": False, "code": "FORMATO", "error": "Usa txt, xlsx o pdf."}), 400


@app.route("/api/ventas/exportar/<formato>", methods=["GET"])
@login_required
def api_ventas_exportar(formato: str):
    """Exporta el último resumen de ventas: txt | xlsx | pdf."""
    payload = session.get("ventas_export")
    if not payload or not payload.get("ok"):
        return jsonify(
            {
                "ok": False,
                "code": "SIN_DATOS",
                "error": "Primero busca y suma ventas en el modal.",
            }
        ), 400
    try:
        return _export_response(payload, formato)
    except ValueError:
        return jsonify({"ok": False, "code": "FORMATO", "error": "Usa txt, xlsx o pdf."}), 400


@app.errorhandler(400)
def handle_400(err):
    flash(getattr(err, "description", None) or "Solicitud inválida.", "error")
    if session.get("logged_in"):
        return redirect(url_for("index"))
    return redirect(url_for("login"))


@app.errorhandler(404)
def handle_404(_err):
    return render_template("404.html", home_url=url_for("index")), 404


@app.errorhandler(500)
def handle_500(_err):
    return render_template("500.html", home_url=url_for("index")), 500



# ---------- Usuarios ----------


def _usuarios_page(edit_user=None):
    users = User.query.order_by(User.id.asc()).all()
    return render_template(
        "usuarios.html",
        users=users,
        usuario=session.get("usuario", ""),
        user_id=session.get("user_id"),
        is_admin=True,
        admin_name=admin_username(),
        edit_user=edit_user,
    )


def _trailero_excel_config() -> TraileroExcelConfig:
    cfg = TraileroExcelConfig.query.order_by(TraileroExcelConfig.id.asc()).first()
    if cfg is None:
        cfg = TraileroExcelConfig(ruta="", hoja="Acpm", activo=False)
        db.session.add(cfg)
        db.session.commit()
    return cfg


@app.route("/servicio-trailero", methods=["GET"])
@login_required
def servicio_trailero():
    configs = ImapConfig.query.order_by(ImapConfig.id.desc()).all()
    excel_cfg = _trailero_excel_config()
    return render_template(
        "servicio_trailero.html",
        usuario=session.get("usuario", ""),
        user_id=session.get("user_id"),
        is_admin=is_admin_user(current_user()),
        imap_configs=configs,
        excel_config=excel_cfg,
    )


@app.route("/servicio-trailero/excel-config", methods=["POST"])
@login_required
@csrf_protect
def trailero_excel_config_guardar():
    cfg = _trailero_excel_config()
    cfg.ruta = (request.form.get("ruta") or "").strip()[:500]
    cfg.hoja = (request.form.get("hoja") or "Acpm").strip()[:120] or "Acpm"
    cfg.activo = (request.form.get("activo") or "") in ("1", "on", "true", "True")
    db.session.commit()
    flash("Configuración Excel guardada.", "ok")
    return redirect(url_for("servicio_trailero"))


def _imap_from_form(form, *, require_password: bool) -> tuple[dict | None, str | None]:
    host = (form.get("host") or "").strip()[:255]
    usuario = (form.get("usuario") or "").strip()[:255]
    password = form.get("password") or ""
    carpeta = (form.get("carpeta") or "INBOX").strip()[:255] or "INBOX"
    try:
        port = int(form.get("port") or 993)
    except (TypeError, ValueError):
        return None, "Puerto inválido."
    if not host:
        return None, "Indica el servidor IMAP (host)."
    if not usuario:
        return None, "Indica el usuario de correo."
    if require_password and not password.strip():
        return None, "Indica la contraseña."
    if port < 1 or port > 65535:
        return None, "Puerto fuera de rango."
    return {
        "host": host,
        "port": port,
        "usuario": usuario,
        "password": password,
        "carpeta": carpeta,
        "usar_ssl": True,
    }, None


@app.route("/servicio-trailero/imap/crear", methods=["POST"])
@login_required
@csrf_protect
def imap_crear():
    data, err = _imap_from_form(request.form, require_password=True)
    if err:
        flash(err, "error")
        return redirect(url_for("servicio_trailero"))
    cfg = ImapConfig(
        nombre=data["usuario"],
        host=data["host"],
        port=data["port"],
        usuario=data["usuario"],
        password_enc=encrypt_secret(data["password"]),
        carpeta=data["carpeta"],
        usar_ssl=data["usar_ssl"],
    )
    db.session.add(cfg)
    db.session.commit()
    flash(f"Configuración IMAP “{cfg.usuario}” creada.", "ok")
    return redirect(url_for("servicio_trailero"))


@app.route("/servicio-trailero/imap/<int:cid>/editar", methods=["POST"])
@login_required
@csrf_protect
def imap_editar(cid: int):
    cfg = db.get_or_404(ImapConfig, cid)
    data, err = _imap_from_form(request.form, require_password=False)
    if err:
        flash(err, "error")
        return redirect(url_for("servicio_trailero"))

    cfg.nombre = data["usuario"]
    cfg.host = data["host"]
    cfg.port = data["port"]
    cfg.usuario = data["usuario"]
    cfg.carpeta = data["carpeta"]
    cfg.usar_ssl = data["usar_ssl"]
    if data["password"].strip():
        cfg.password_enc = encrypt_secret(data["password"])
    db.session.commit()
    flash(f"Configuración IMAP “{cfg.usuario}” actualizada.", "ok")
    return redirect(url_for("servicio_trailero"))


@app.route("/servicio-trailero/imap/<int:cid>/eliminar", methods=["POST"])
@login_required
@csrf_protect
def imap_eliminar(cid: int):
    cfg = db.get_or_404(ImapConfig, cid)
    label = cfg.usuario
    db.session.delete(cfg)
    db.session.commit()
    flash(f"Configuración IMAP “{label}” eliminada.", "ok")
    return redirect(url_for("servicio_trailero"))


@app.route("/servicio-trailero/imap/<int:cid>/probar", methods=["POST"])
@login_required
@csrf_protect
def imap_probar(cid: int):
    cfg = db.get_or_404(ImapConfig, cid)
    password = decrypt_secret(cfg.password_enc or "")
    ok, msg = probar_imap(
        host=cfg.host,
        port=cfg.port,
        usuario=cfg.usuario,
        password=password,
        carpeta=cfg.carpeta or "INBOX",
        usar_ssl=True,
    )
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("servicio_trailero"))


def _trailero_export_response(payload: dict, formato: str) -> Response:
    formato = (formato or "").strip().lower()
    desde = payload.get("desde") or ""
    hasta = payload.get("hasta") or ""
    base_name = f"trailero_{desde}_{hasta}".replace(":", "")

    if formato in ("txt", "notepad", "texto"):
        return Response(
            trailero_export_txt(payload),
            mimetype="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{base_name}.txt"'
            },
        )
    if formato in ("xlsx", "excel"):
        return Response(
            trailero_export_xlsx(payload),
            mimetype=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": f'attachment; filename="{base_name}.xlsx"'
            },
        )
    if formato == "pdf":
        return Response(
            trailero_export_pdf(payload),
            mimetype="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{base_name}.pdf"'
            },
        )
    return jsonify({"ok": False, "error": "Formato no soportado."}), 400


@app.route("/api/trailero/ejecutar", methods=["GET"])
@login_required
def api_trailero_ejecutar():
    desde = (request.args.get("desde") or "").strip()[:10]
    hasta = (request.args.get("hasta") or "").strip()[:10]
    hora_desde = (request.args.get("hora_desde") or "00:00").strip()[:8] or "00:00"
    use_stream = request.args.get("stream", "1") != "0"
    imap_id = request.args.get("imap_id", type=int)

    if not desde or not hasta:
        return jsonify({"ok": False, "error": "Indica fechas Desde y Hasta."}), 400

    cfg = None
    if imap_id:
        cfg = db.session.get(ImapConfig, imap_id)
    if cfg is None:
        cfg = ImapConfig.query.order_by(ImapConfig.id.desc()).first()
    if cfg is None:
        return jsonify(
            {
                "ok": False,
                "error": "No hay configuración IMAP. Crea una primero.",
            }
        ), 400

    password = decrypt_secret(cfg.password_enc or "")
    user_label = session.get("usuario") or ""

    def _build_and_save(progress_cb=None):
        payload = ejecutar_trailero(
            host=cfg.host,
            port=cfg.port,
            usuario=cfg.usuario,
            password=password,
            carpeta=cfg.carpeta or "INBOX",
            desde=desde,
            hasta=hasta,
            hora_desde=hora_desde,
            usar_ssl=True,
            progress_cb=progress_cb,
        )
        if not payload.get("ok"):
            raise RuntimeError(payload.get("error") or "Error al ejecutar Trailero")

        # Escribir en Excel Acpm si la config está activa (medio montado).
        excel_cfg = TraileroExcelConfig.query.order_by(
            TraileroExcelConfig.id.asc()
        ).first()
        if excel_cfg and excel_cfg.activo and (excel_cfg.ruta or "").strip():
            if progress_cb:
                progress_cb(
                    {
                        "type": "status",
                        "message": "Escribiendo tanqueos en Excel…",
                    }
                )
            excel_result = append_tanqueos_excel(
                excel_cfg.ruta,
                excel_cfg.hoja or "Acpm",
                payload.get("detalle") or [],
            )
            payload["excel_write"] = excel_result
        else:
            payload["excel_write"] = {
                "ok": True,
                "escritos": 0,
                "omitido": True,
                "error": "",
            }

        reg = TraileroRegistro(
            desde=payload.get("desde") or desde,
            hasta=payload.get("hasta") or hasta,
            hora_desde=payload.get("hora_desde") or hora_desde or "00:00",
            imap_usuario=payload.get("imap_usuario") or cfg.usuario,
            tanqueos_total=int(payload.get("tanqueos_total") or 0),
            pdfs_error=int(payload.get("pdfs_error") or 0),
            total_general=float(payload.get("total_general") or 0),
            usuario=user_label,
        )
        reg.set_payload(payload)
        db.session.add(reg)
        db.session.commit()
        recortar_registros(TraileroRegistro)
        payload["registro_id"] = reg.id
        return payload

    if not use_stream:
        try:
            payload = _build_and_save()
            session["trailero_export"] = payload
            return jsonify(payload)
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    q: Queue = Queue()

    @copy_current_request_context
    def worker():
        try:
            def progress_cb(ev):
                q.put(ev)

            payload = _build_and_save(progress_cb=progress_cb)
            q.put({"type": "done", "payload": payload})
        except RuntimeError as exc:
            q.put({"type": "error", "ok": False, "error": str(exc)})
        except Exception as exc:
            q.put({"type": "error", "ok": False, "error": str(exc)})

    threading.Thread(target=worker, daemon=True).start()

    @stream_with_context
    def generate():
        while True:
            try:
                ev = q.get(timeout=600)
            except Empty:
                yield json.dumps(
                    {
                        "type": "error",
                        "ok": False,
                        "error": "La ejecución tardó demasiado.",
                    },
                    ensure_ascii=False,
                ) + "\n"
                break
            kind = ev.get("type")
            if kind == "done":
                payload = ev.get("payload")
                if payload:
                    session["trailero_export"] = payload
                    session.modified = True
                yield json.dumps(
                    {"type": "done", "payload": payload}, ensure_ascii=False
                ) + "\n"
                break
            if kind == "error":
                yield json.dumps(ev, ensure_ascii=False) + "\n"
                break
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/trailero/registros", methods=["GET"])
@login_required
def api_trailero_registros():
    pagina = max(1, request.args.get("page", 1, type=int) or 1)
    per_raw = (request.args.get("per_page") or "10").strip().lower()
    q = TraileroRegistro.query
    total = q.count()

    if per_raw in ("todos", "all", "0"):
        per_page = total or 1
        show_all = True
    else:
        try:
            per_page = int(per_raw)
        except (TypeError, ValueError):
            per_page = 5
        if per_page not in (5, 10):
            per_page = 10
        show_all = False

    total_paginas = max(1, (total + per_page - 1) // per_page) if total else 1
    if pagina > total_paginas:
        pagina = total_paginas

    query = q.order_by(TraileroRegistro.id.desc())
    if show_all:
        rows = query.all()
        pagina = 1
        total_paginas = 1
        per_label = "todos"
    else:
        rows = query.offset((pagina - 1) * per_page).limit(per_page).all()
        per_label = str(per_page)

    return jsonify(
        {
            "ok": True,
            "registros": [r.to_dict() for r in rows],
            "pagina": pagina,
            "per_page": per_label,
            "total": total,
            "total_paginas": total_paginas,
        }
    )


@app.route("/api/trailero/registros", methods=["DELETE"])
@login_required
@csrf_protect
def api_trailero_registros_eliminar_todos():
    n = vaciar_registros(TraileroRegistro, "trailero_export")
    return jsonify({"ok": True, "eliminados": n})


@app.route("/api/trailero/registros/<int:rid>", methods=["DELETE"])
@login_required
@csrf_protect
def api_trailero_registro_eliminar(rid: int):
    reg = db.session.get(TraileroRegistro, rid)
    if not reg:
        return jsonify({"ok": False, "code": "NO_ENCONTRADO", "error": "Registro no existe."}), 404
    db.session.delete(reg)
    db.session.commit()
    _olvidar_export("trailero_export", rid)
    return jsonify({"ok": True})


@app.route("/api/trailero/registros/<int:rid>/exportar/<formato>", methods=["GET"])
@login_required
def api_trailero_registro_exportar(rid: int, formato: str):
    reg = db.get_or_404(TraileroRegistro, rid)
    payload = reg.get_payload()
    if not payload:
        return jsonify({"ok": False, "error": "Este registro no tiene datos para exportar."}), 404
    return _trailero_export_response(payload, formato)


@app.route("/api/trailero/exportar/<formato>", methods=["GET"])
@login_required
def api_trailero_exportar(formato: str):
    payload = session.get("trailero_export")
    if not payload:
        return jsonify({"ok": False, "error": "No hay resultado reciente para exportar."}), 404
    return _trailero_export_response(payload, formato)


@app.route("/usuarios", methods=["GET"])
@login_required
@admin_required
def usuarios():
    return _usuarios_page()


@app.route("/usuarios/crear", methods=["POST"])
@login_required
@admin_required
@csrf_protect
def usuarios_crear():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    user_err = validate_username(username)
    if user_err:
        flash(user_err, "error")
        return redirect(url_for("usuarios"))

    pass_err = validate_password(password, required=True)
    if pass_err:
        flash(pass_err, "error")
        return redirect(url_for("usuarios"))

    if username == admin_username():
        flash("Ese nombre está reservado para el administrador.", "error")
        return redirect(url_for("usuarios"))

    if User.query.filter_by(username=username).first():
        flash("Ese usuario ya existe.", "error")
        return redirect(url_for("usuarios"))

    user = User(username=username, enabled=True, failed_attempts=0)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash(f"Usuario “{username}” creado.", "ok")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/<int:uid>/editar", methods=["GET", "POST"])
@login_required
@admin_required
@csrf_protect
def usuarios_editar(uid: int):
    # La edición es por modal; GET solo vuelve al listado.
    if request.method == "GET":
        return redirect(url_for("usuarios"))

    user = db.get_or_404(User, uid)
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    password_actual = request.form.get("password_actual", "")
    target_is_admin = is_admin_user(user)

    if target_is_admin:
        username = user.username
        if password:
            if not password_actual:
                flash("Para cambiar la contraseña del admin, escribe la contraseña actual.", "error")
                return redirect(url_for("usuarios"))
            if not user.check_password(password_actual):
                flash("La contraseña actual del admin no es correcta.", "error")
                return redirect(url_for("usuarios"))
    else:
        user_err = validate_username(username)
        if user_err:
            flash(user_err, "error")
            return redirect(url_for("usuarios"))
        if username == admin_username():
            flash("Ese nombre está reservado para el administrador.", "error")
            return redirect(url_for("usuarios"))

    pass_err = validate_password(password, required=False)
    if pass_err:
        flash(pass_err, "error")
        return redirect(url_for("usuarios"))

    exists = User.query.filter(User.username == username, User.id != uid).first()
    if exists:
        flash("Ese nombre de usuario ya está en uso.", "error")
        return redirect(url_for("usuarios"))

    user.username = username
    if password:
        user.set_password(password)
    db.session.commit()

    if session.get("user_id") == user.id:
        session["usuario"] = user.username

    flash("Usuario actualizado.", "ok")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/<int:uid>/toggle", methods=["POST"])
@login_required
@admin_required
@csrf_protect
def usuarios_toggle(uid: int):
    user = db.get_or_404(User, uid)

    if is_admin_user(user):
        flash("El administrador no se puede apagar.", "error")
        return redirect(url_for("usuarios"))

    if user.id == session.get("user_id") and user.enabled:
        flash("No puedes apagarte a ti mismo.", "error")
        return redirect(url_for("usuarios"))

    enabled_count = User.query.filter_by(enabled=True).count()
    if user.enabled and enabled_count <= 1:
        flash("Debe quedar al menos un usuario activo.", "error")
        return redirect(url_for("usuarios"))

    user.enabled = not user.enabled
    db.session.commit()
    estado = "activado (ON)" if user.enabled else "apagado (OFF)"
    flash(f"Usuario “{user.username}” {estado}.", "ok")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/<int:uid>/eliminar", methods=["POST"])
@login_required
@admin_required
@csrf_protect
def usuarios_eliminar(uid: int):
    user = db.get_or_404(User, uid)

    if is_admin_user(user):
        flash("El administrador no se puede eliminar.", "error")
        return redirect(url_for("usuarios"))

    if user.id == session.get("user_id"):
        flash("No puedes eliminarte a ti mismo.", "error")
        return redirect(url_for("usuarios"))

    if User.query.count() <= 1:
        flash("No puedes eliminar el único usuario.", "error")
        return redirect(url_for("usuarios"))

    nombre = user.username
    db.session.delete(user)
    db.session.commit()
    flash(f"Usuario “{nombre}” eliminado.", "ok")
    return redirect(url_for("usuarios"))


if __name__ == "__main__":
    # Desarrollo: servidor + navegador. El .exe usa desktop_app.py (ventana app).
    import os
    import time

    host = "127.0.0.1"
    port = 5000
    url = f"http://{host}:{port}"
    if is_frozen():
        from desktop_app import main as desktop_main

        desktop_main()
    else:
        print(f"aplicacionjunior en {url}")
        print(f"Datos locales: {ROOT}")

        def _open() -> None:
            time.sleep(0.9)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()
        app.run(host=host, port=port, debug=True, use_reloader=False)
