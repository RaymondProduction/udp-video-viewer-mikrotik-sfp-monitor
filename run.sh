#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  echo "Створюю venv..."
  if [[ "$OSTYPE" == "darwin"* ]]; then
    /opt/homebrew/bin/python3.13 -m venv venv --system-site-packages
  else
    python3 -m venv venv --system-site-packages
  fi
fi

source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

#python -m pip install -r requirements.txt

export DYLD_LIBRARY_PATH="/opt/homebrew/lib:${DYLD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="/opt/homebrew/lib/girepository-1.0:${GI_TYPELIB_PATH}"
export GST_PLUGIN_PATH="/opt/homebrew/lib/gstreamer-1.0"

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