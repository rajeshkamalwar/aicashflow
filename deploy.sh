#!/bin/bash
# ─── AI Cashflow — VPS deployment script ───────────────────────────────────
# Run this on the VPS after uploading the project files.
# Usage: bash deploy.sh

set -e

APP_DIR="/home/aicashflow.pro/htdocs"
PYTHON="python3.11"
SERVICE="aicashflow"

echo "==> Setting up Python virtual environment..."
cd "$APP_DIR"
$PYTHON -m venv .venv
source .venv/bin/activate

echo "==> Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Creating config directory if missing..."
mkdir -p config
if [ ! -f config/tenant.local.json ]; then
  cp config/tenant.example.json config/tenant.local.json
  echo "    -> Created tenant.local.json from example. Edit it with your settings."
fi

echo "==> Creating data directory..."
mkdir -p data uploads

echo "==> Writing systemd service..."
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=AI Cashflow FastAPI App
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}/src
Environment="PATH=${APP_DIR}/.venv/bin"
ExecStart=${APP_DIR}/.venv/bin/uvicorn ai_cashflow.api.app:app --host 127.0.0.1 --port 8001
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "==> Enabling and starting service..."
systemctl daemon-reload
systemctl enable $SERVICE
systemctl restart $SERVICE
systemctl status $SERVICE --no-pager

echo ""
echo "✓ Deployment complete."
echo "  App running at http://127.0.0.1:8001"
echo "  Point CloudPanel site to port 8001 and enable SSL."
