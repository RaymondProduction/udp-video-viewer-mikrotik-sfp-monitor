#!/usr/bin/env bash
set -euo pipefail

APP_NAME="prince_ground_station"
APP_TITLE="Prince Ground Station"
ENTRY_SCRIPT="main.py"
ICON_FILE="prince_ground_station.png"
DESKTOP_FILE="${APP_NAME}.desktop"
APPDIR="AppDir"
VENV_DIR=".venv"

echo "==> Cleaning previous build artifacts..."
rm -rf build dist "$APPDIR" *.spec

echo "==> Creating virtual environment if needed..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR" --system-site-packages
fi

echo "==> Activating virtual environment..."
source "$VENV_DIR/bin/activate"

echo "==> Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Building with PyInstaller..."
pyinstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "$APP_NAME" \
  "$ENTRY_SCRIPT"

echo "==> Preparing AppDir..."
mkdir -p "$APPDIR/usr/bin"
cp -r "dist/$APP_NAME/"* "$APPDIR/usr/bin/"

echo "==> Writing AppRun..."
cat > "$APPDIR/AppRun" <<EOF
#!/bin/sh
HERE="\$(dirname "\$(readlink -f "\$0")")"
exec "\$HERE/usr/bin/$APP_NAME" "\$@"
EOF
chmod +x "$APPDIR/AppRun"

echo "==> Writing desktop file..."
cat > "$APPDIR/$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_TITLE
Exec=$APP_NAME
Icon=${APP_NAME}
Categories=Utility;Network;Video;
Terminal=false
EOF

echo "==> Copying icon..."
if [ -f "$ICON_FILE" ]; then
    cp "$ICON_FILE" "$APPDIR/${APP_NAME}.png"
    cp "$ICON_FILE" "$APPDIR/.DirIcon"
else
    echo "WARNING: Icon file '$ICON_FILE' not found"
fi

echo "==> Downloading appimagetool if needed..."
if [ ! -f appimagetool.AppImage ]; then
    wget -O appimagetool.AppImage \
      https://github.com/AppImage/appimagetool/releases/latest/download/appimagetool-x86_64.AppImage
    chmod +x appimagetool.AppImage
fi

echo "==> Building AppImage..."
ARCH=x86_64 ./appimagetool.AppImage "$APPDIR"

echo "==> Done."
ls -lh *.AppImage