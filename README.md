# UDP Video Viewer + MikroTik SFP Monitor

A Python application for:

- viewing UDP video (`raw` or `rtp`)
- displaying SFP telemetry from MikroTik on top of the video
- communicating with MikroTik via SSH
- optionally running a UDP ↔ Serial bridge for controller/control link communication

## Requirements

### Ubuntu system packages

Install the required system dependencies:

```bash
sudo apt update
sudo apt install -y \
  python3 \
  python3-venv \
  python3-pip \
  python3-gi \
  python3-gi-cairo \
  python3-gst-1.0 \
  python3-serial \
  python3-paramiko \
  iproute2 \
  gir1.2-gtk-3.0 \
  gir1.2-gstreamer-1.0 \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  gstreamer1.0-gtk3

### macOS (Homebrew)

Install the required system dependencies via [Homebrew](https://brew.sh):

```bash
brew install python@3.13 gtk+3 gstreamer gst-plugins-base gst-plugins-good \
             gst-plugins-bad gst-plugins-ugly gobject-introspection pygobject3 \
             gst-libav gtk-mac-integration
```

> **Примітка:** `pygobject3` надає модуль `gi` для Python 3.13.
> Стандартний macOS Python (3.9) не підтримується — використовуй саме `python3.13` від Homebrew.

#### Створення та запуск venv (macOS)

```bash
# Видалити старий venv (якщо є)
rm -rf venv

# Створити venv на основі Python 3.13 з доступом до системних пакетів Homebrew
python3.13 -m venv venv --system-site-packages

# Активувати та встановити залежності
source venv/bin/activate
pip install -r requirements.txt
```

Або просто запустити `./run.sh` — скрипт робить все це автоматично.

#### Що було виправлено для macOS

1. **`ModuleNotFoundError: No module named 'gi'`** — venv використовував Python 3.9 (системний macOS),
   для якого `pygobject3` від Homebrew не встановлений. Виправлення: пересоздати venv через `python3.13`.

2. **`Failed to load shared library 'libgobject-2.0.0.dylib'`** — GStreamer plugin scanner не міг
   знайти бібліотеки Homebrew, бо `/opt/homebrew/lib` не входить у стандартний шлях пошуку dylib.
   Виправлення: додано в `run.sh` наступні змінні середовища:

```bash
export DYLD_LIBRARY_PATH="/opt/homebrew/lib:${DYLD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="/opt/homebrew/lib/girepository-1.0:${GI_TYPELIB_PATH}"
export GST_PLUGIN_PATH="/opt/homebrew/lib/gstreamer-1.0"
```