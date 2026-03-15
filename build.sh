pyinstaller \
  --noconfirm \
  --windowed \
  --name udp_video_viewer \
  --collect-submodules gi \
  --hidden-import=gi \
  --hidden-import=gi.repository.Gtk \
  --hidden-import=gi.repository.Gst \
  --hidden-import=gi.repository.GLib \ 
  main.py

./dist/udp_video_viewer/udp_video_viewer \
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