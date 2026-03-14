# UDP Video Viewer + MikroTik SFP Monitor

Python-програма для:
- перегляду UDP відео (`raw` або `rtp`)
- показу SFP telemetry з MikroTik поверх відео
- роботи з MikroTik через SSH

## Що потрібно

### Системні пакети Ubuntu

Встановити системні залежності:

```bash
sudo apt update
sudo apt install -y \
  python3 \
  python3-venv \
  python3-pip \
  python3-gi \
  python3-gi-cairo \
  python3-gst-1.0 \
  gir1.2-gtk-3.0 \
  gir1.2-gstreamer-1.0 \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  gstreamer1.0-gtk3# udp-video-viewer-mikrotik-sfp-monitor
# udp-video-viewer-mikrotik-sfp-monitor
