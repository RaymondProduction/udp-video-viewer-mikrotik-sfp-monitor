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