#!/usr/bin/env bash
# Adds "Race Overlay" to your app menu (and "Open With" for videos).
# Re-run it if you move the project folder.
set -e
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
chmod +x "$DIR/launch.sh"
mkdir -p "$APPS" "$ICONS"
cp "$DIR/raceoverlay.svg" "$ICONS/raceoverlay.svg"
cat > "$APPS/raceoverlay.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Race Overlay
GenericName=Telemetry Video Overlay
Comment=Sync racing telemetry to GoPro video and render gauge overlays
Exec="$DIR/launch.sh" %f
Icon=raceoverlay
Terminal=false
Categories=AudioVideo;Video;
MimeType=video/mp4;video/quicktime;video/x-matroska;
StartupWMClass=raceoverlay
DESKTOP
update-desktop-database "$APPS" >/dev/null 2>&1 || true
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
if [ -d "$HOME/Desktop" ] && [ "${1:-}" = "--desktop" ]; then
    cp "$APPS/raceoverlay.desktop" "$HOME/Desktop/"
    chmod +x "$HOME/Desktop/raceoverlay.desktop"
fi
echo "Installed. Look for 'Race Overlay' in your app menu."
echo "(Add --desktop to also drop an icon on the desktop.)"
