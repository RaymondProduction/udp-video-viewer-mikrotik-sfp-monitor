#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  echo "Створюю venv..."
  python3 -m venv venv --system-site-packages
fi

source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

#python -m pip install -r requirements.txt

  python main.py \
  --port 5600 \
  --mode rtp \
  --always-on-top \
  --mikrotik-host 192.168.121.1 \
  --mikrotik-user admin \
  --mikrotik-password "" \
  --mikrotik-interface sfp1 \
  --serial-baudrate 420000 \
  --bridge-remote-host 192.168.121.50 \
  --bridge-remote-port 9000 \
  --bridge-hex
#python test.py