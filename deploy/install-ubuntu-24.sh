#!/usr/bin/env bash
# Jalankan di VPS Ubuntu 24.04 sebagai user yang punya sudo (mis. sembapos).
# Usage: bash deploy/install-ubuntu-24.sh
#
# Sebelumnya: upload/clone proyek ke /home/sembapos/sembako-kuningan
#             atau edit APP_DIR di bawah.

set -euo pipefail

APP_USER="${SUDO_USER:-$USER}"
if [[ "$APP_USER" == "root" ]]; then
  APP_USER="sembapos"
fi

APP_DIR="/home/${APP_USER}/sembako-kuningan"
DB_NAME="sembako"
DB_USER="sembako_app"

echo "==> Paket sistem (Python venv, PostgreSQL, Nginx, build deps untuk wheel)"
sudo apt-get update
sudo apt-get install -y python3.12-venv python3-pip postgresql postgresql-contrib nginx \
  build-essential libpq-dev

echo "==> PostgreSQL: buat role & database (ganti password di prompt manual setelah ini jika perlu)"
sudo -u postgres psql -v ON_ERROR_STOP=1 -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 \
  || sudo -u postgres psql -v ON_ERROR_STOP=1 -c "CREATE USER ${DB_USER} WITH PASSWORD 'GANTI_PASSWORD_INI';"
sudo -u postgres psql -v ON_ERROR_STOP=1 -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
  || sudo -u postgres psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"

echo "==> Virtualenv & pip"
[[ -d "${APP_DIR}" ]] || { echo "Folder tidak ada: ${APP_DIR}. Clone/upload proyek dulu."; exit 1; }
sudo chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}" || true
sudo -u "${APP_USER}" bash <<EOF
set -e
cd "${APP_DIR}"
python3.12 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
[[ -f .env ]] || { cp .env.example .env 2>/dev/null || true; echo "Buat file .env dan isi SECRET_KEY + DATABASE_URL"; }
EOF

echo ""
echo "=== Manual (penting) ==="
echo "1) Edit ${APP_DIR}/.env :"
echo "   SECRET_KEY=\$(openssl rand -hex 32)"
echo "   DATABASE_URL=postgresql://${DB_USER}:PASSWORD@127.0.0.1:5432/${DB_NAME}"
echo "2) sudo -u ${APP_USER} -H bash -c 'cd ${APP_DIR} && ./venv/bin/flask db upgrade'  # export FLASK_APP=wsgi:app atau cd +"
echo ""
echo "   Contoh:"
echo "   sudo -u ${APP_USER} -H bash -c 'cd ${APP_DIR} && export FLASK_APP=wsgi:app && ./venv/bin/flask db upgrade'"
echo ""
echo "3) systemd:"
echo "   sudo cp ${APP_DIR}/deploy/sembako.service /etc/systemd/system/sembako.service"
echo "   sudo sed -i \"s|/home/sembapos|/home/${APP_USER}|g\" /etc/systemd/system/sembako.service"
echo "   sudo systemctl daemon-reload && sudo systemctl enable --now sembako"
echo ""
echo "4) nginx:"
echo "   sudo cp ${APP_DIR}/deploy/nginx-sembako.conf /etc/nginx/sites-available/sembako"
echo "   sudo sed -i \"s|/home/sembapos|/home/${APP_USER}|g\" /etc/nginx/sites-available/sembako"
echo "   sudo ln -sf /etc/nginx/sites-available/sembako /etc/nginx/sites-enabled/"
echo "   sudo rm -f /etc/nginx/sites-enabled/default"
echo "   sudo nginx -t && sudo systemctl reload nginx"
echo ""
echo "5) Firewall (opsional): sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable"
echo ""
echo "Ganti password PostgreSQL untuk ${DB_USER}:"
echo "  sudo -u postgres psql -c \"ALTER USER ${DB_USER} WITH PASSWORD 'password_baru';\""
echo "  lalu sesuaikan DATABASE_URL di .env"
