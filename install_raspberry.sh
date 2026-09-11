#!/bin/bash
set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Installerar Intesa dashboard i $APP_DIR"
sudo apt update
sudo apt install -y python3 python3-pip python3-venv
cd "$APP_DIR"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo "Skapar systemd-service..."
sudo tee /etc/systemd/system/intesa-dashboard.service >/dev/null <<EOF
[Unit]
Description=Intesa Sanpaolo Swing Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/intesa.env
Environment=SERVER_MODE=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable intesa-dashboard
sudo systemctl restart intesa-dashboard
echo
echo "Klart. Kontrollera med:"
echo "  sudo systemctl status intesa-dashboard"
echo "Öppna från nätverket:"
echo "  http://<RASPBERRY-IP>:5000"
