#!/bin/bash
# Se ejecuta ya dentro del usuario junior, en /home/junior.
# En esa carpeta tienen que estar este script y el archivo .env.
# Uso: bash instalar.sh
set -euo pipefail

REPO="https://github.com/tavogustavo-a/aplicacionjunior.git"
DOMINIO="junior.tuyaho.com"
IP_ESPERADA="173.212.218.51"
APP_DIR="$HOME/aplicacionjunior"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_SRC="$SCRIPT_DIR/.env"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "No lo ejecutes como root. Entra con: su - junior"
  exit 1
fi

if [[ ! -f "$ENV_SRC" ]]; then
  echo "Falta $ENV_SRC"
  echo "Deja el .env en la misma carpeta que instalar.sh"
  exit 1
fi

python3 - "$ENV_SRC" <<'PY'
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8")
data = {}
for line in text.splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    data[key.strip()] = value.strip().strip("\"'")
faltan = [k for k in ("PANEL_USER", "PANEL_PASSWORD", "SECRET_KEY") if not data.get(k)]
if faltan:
    sys.exit("El .env no tiene: " + ", ".join(faltan))
clave = data["SECRET_KEY"]
if clave in {"effi-local-panel-cambia-esto", "PEGA_AQUI_UNA_CLAVE_LARGA"} or len(clave) < 32:
    sys.exit("SECRET_KEY sigue siendo un ejemplo o es demasiado corta.")
print("Panel:", data["PANEL_USER"])
PY

resuelta="$(getent ahostsv4 "$DOMINIO" | awk '{print $1; exit}')"
if [[ "$resuelta" != "$IP_ESPERADA" ]]; then
  echo "$DOMINIO responde '$resuelta' y tiene que ser $IP_ESPERADA"
  echo "Espera a que el registro de Namecheap se propague y vuelve a ejecutar."
  exit 1
fi

echo "Va a pedir la contraseña de junior una vez."
sudo -v

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-venv python3-dev build-essential \
  libffi-dev libxml2-dev libxslt1-dev zlib1g-dev \
  git nginx certbot python3-certbot-nginx

if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" pull --ff-only origin main
else
  git clone "$REPO" "$APP_DIR"
fi

install -m 600 "$ENV_SRC" "$APP_DIR/.env"
mkdir -p "$APP_DIR/instance"
chmod 700 "$APP_DIR/instance"
chmod 711 "$HOME"

python3 -m venv "$APP_DIR/venv"
# shellcheck disable=SC1091
source "$APP_DIR/venv/bin/activate"
pip install --upgrade pip
grep -v -i '^pywebview' "$APP_DIR/utilidades/requirements.txt" > /tmp/req-junior.txt
pip install -r /tmp/req-junior.txt
pip install gunicorn
deactivate

sudo tee /etc/systemd/system/aplicacionjunior.service >/dev/null <<EOF
[Unit]
Description=aplicacionjunior
After=network.target

[Service]
User=$USER
Group=$USER
WorkingDirectory=$APP_DIR/utilidades
Environment="PATH=$APP_DIR/venv/bin"
Environment="BEHIND_PROXY=1"
ExecStart=$APP_DIR/venv/bin/gunicorn --workers 1 --bind 127.0.0.1:8001 --timeout 3600 --forwarded-allow-ips=127.0.0.1 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/nginx/sites-available/aplicacionjunior.conf >/dev/null <<EOF
server {
    listen 80;
    server_name $DOMINIO;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/aplicacionjunior.conf /etc/nginx/sites-enabled/aplicacionjunior.conf
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable aplicacionjunior
sudo systemctl restart aplicacionjunior
sudo systemctl reload nginx

if ! curl -fsS -o /dev/null -I http://127.0.0.1:8001/login; then
  echo "La aplicación no respondió en el puerto 8001."
  sudo systemctl status aplicacionjunior --no-pager || true
  exit 1
fi

sudo certbot --nginx -d "$DOMINIO" --email admin@tuyaho.com --agree-tos --no-eff-email -n --redirect
sudo nginx -t
sudo systemctl reload nginx

echo "Listo: https://$DOMINIO"
