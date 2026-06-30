#!/usr/bin/env bash
set -euo pipefail

APP_NAME="ground_station"
APP_TITLE="Ground Station"
ENTRY_SCRIPT="main.py"
ICON_FILE="icon.png"
PLACEHOLDER_FILE="placeholder.png"
FALLBACK_PLACEHOLDER="placeholder.png"
FONT_FILE="font_btfl_hd.png"
DESKTOP_FILE="${APP_NAME}.desktop"
APPDIR="AppDir"
VENV_DIR=".venv"

echo "==> Cleaning previous build artifacts..."
rm -rf build dist "$APPDIR" *.spec

echo "==> Checking required files..."
if [ ! -f "$ENTRY_SCRIPT" ]; then
    echo "ERROR: Entry script '$ENTRY_SCRIPT' not found"
    exit 1
fi

if [ ! -f "$ICON_FILE" ]; then
    echo "WARNING: Icon file '$ICON_FILE' not found"
fi

if [ -f "$PLACEHOLDER_FILE" ]; then
    ACTUAL_PLACEHOLDER_FILE="$PLACEHOLDER_FILE"
elif [ -f "$FALLBACK_PLACEHOLDER" ]; then
    ACTUAL_PLACEHOLDER_FILE="$FALLBACK_PLACEHOLDER"
    echo "WARNING: '$PLACEHOLDER_FILE' not found, using '$FALLBACK_PLACEHOLDER'"
else
    ACTUAL_PLACEHOLDER_FILE=""
    echo "WARNING: No placeholder image found ('$PLACEHOLDER_FILE' or '$FALLBACK_PLACEHOLDER')"
fi

echo "==> Recreating virtual environment..."
rm -rf "$VENV_DIR"
python3 -m venv "$VENV_DIR" --system-site-packages

echo "==> Activating virtual environment..."
source "$VENV_DIR/bin/activate"

echo "==> Installing Python dependencies..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install --upgrade pyinstaller

PYINSTALLER_ARGS=(
  --noconfirm
  --clean
  --windowed
  --name "$APP_NAME"
)

if [ -f "$ICON_FILE" ]; then
  PYINSTALLER_ARGS+=(--add-data "${ICON_FILE}:.")
fi

if [ -n "$ACTUAL_PLACEHOLDER_FILE" ]; then
  PYINSTALLER_ARGS+=(--add-data "${ACTUAL_PLACEHOLDER_FILE}:.")
fi

if [ -f "$FONT_FILE" ]; then
  PYINSTALLER_ARGS+=(--add-data "${FONT_FILE}:.")
fi

echo "==> Building with PyInstaller..."
python -m PyInstaller "${PYINSTALLER_ARGS[@]}" "$ENTRY_SCRIPT"

echo "==> Preparing AppDir..."
mkdir -p "$APPDIR/usr/bin"
cp -r "dist/$APP_NAME/"* "$APPDIR/usr/bin/"

echo "==> Writing AppRun..."
cat > "$APPDIR/AppRun" <<EOF
#!/bin/sh
HERE="\$(dirname "\$(readlink -f "\$0")")"
export PATH="\$HERE/usr/bin:\$PATH"
exec "\$HERE/usr/bin/${APP_NAME}" "\$@"
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
StartupNotify=true
EOF

echo "==> Copying icon..."
if [ -f "$ICON_FILE" ]; then
    cp "$ICON_FILE" "$APPDIR/${APP_NAME}.png"
    cp "$ICON_FILE" "$APPDIR/.DirIcon"
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