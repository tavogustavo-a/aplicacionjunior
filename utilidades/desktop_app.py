"""Ventana de aplicación (sin CMD ni navegador externo)."""

from __future__ import annotations

import base64
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from app_paths import data_root, is_frozen


def _free_port(host: str = "127.0.0.1", start: int = 5000) -> int:
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    return start


def _wait_ready(url: str, tries: int = 60) -> bool:
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=0.4) as resp:
                if resp.status < 500:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.1)
    return False


def _log_error(root, message: str) -> None:
    try:
        path = root / "panel_error.log"
        path.write_text(message, encoding="utf-8")
    except OSError:
        pass


def _safe_filename(name: str) -> str:
    name = (name or "archivo").strip().replace("\\", "_").replace("/", "_")
    name = re.sub(r'[<>:"|?*]+', "_", name)
    return name[:180] or "archivo"


class DesktopApi:
    """Bridge JS → Python para guardar exportaciones en la app de escritorio."""

    def save_file(self, filename: str, content_b64: str) -> dict:
        import webview

        try:
            data = base64.b64decode(content_b64, validate=False)
        except Exception as exc:
            return {"ok": False, "error": f"Base64 inválido: {exc}"}

        filename = _safe_filename(filename)
        windows = webview.windows
        if not windows:
            return {"ok": False, "error": "Ventana no disponible."}

        dialog_type = (
            webview.FileDialog.SAVE
            if hasattr(webview, "FileDialog")
            else webview.SAVE_DIALOG
        )
        result = windows[0].create_file_dialog(
            dialog_type,
            directory=str(Path.home() / "Downloads"),
            save_filename=filename,
        )
        if not result:
            return {"ok": False, "error": "Cancelado."}

        path = result[0] if isinstance(result, (list, tuple)) else result
        Path(path).write_bytes(data)
        return {"ok": True, "path": str(path)}


def main() -> None:
    root = data_root()
    if is_frozen():
        os.chdir(root)

    try:
        host = "127.0.0.1"
        port = _free_port(host)
        url = f"http://{host}:{port}/"

        # Importar Flask app después de chdir (rutas .env / BD).
        from app import app

        def run_server() -> None:
            app.run(
                host=host,
                port=port,
                debug=False,
                use_reloader=False,
                threaded=True,
            )

        threading.Thread(target=run_server, daemon=True).start()
        if not _wait_ready(url):
            raise RuntimeError("El panel no arrancó a tiempo.")

        import webview

        # Sin esto WebView2 cancela en silencio Content-Disposition / blob downloads.
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True

        api = DesktopApi()
        storage = root / "webview_data"
        storage.mkdir(parents=True, exist_ok=True)

        webview.create_window(
            title="aplicacionjunior",
            url=url,
            width=1200,
            height=840,
            min_size=(920, 640),
            confirm_close=False,
            js_api=api,
        )
        # private_mode=False: conserva cookies/sesión entre aperturas.
        webview.start(
            private_mode=False,
            storage_path=str(storage),
        )
    except Exception as exc:
        import traceback

        _log_error(root, f"{exc}\n\n{traceback.format_exc()}")
        raise


if __name__ == "__main__":
    main()
