#!/bin/bash
set -e
cd "$(dirname "$0")"
echo "=== Intesa Sanpaolo Dashboard - Raspberry Pi ==="
python3 --version
python3 -m pip install --user -r requirements.txt
echo "Startar på http://0.0.0.0:5000"
export SERVER_MODE=1
python3 app.py
