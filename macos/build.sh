#!/bin/zsh
# Build "Chief of Staff.app". Usage:
#   ./build.sh            build into macos/dist/
#   ./build.sh --install  build + copy to /Applications + launch
set -e
cd "$(dirname "$0")"

APP="dist/Chief of Staff.app"
rm -rf dist
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "→ compiling…"
swiftc -O -o "$APP/Contents/MacOS/chief" Sources/main.swift \
  -framework Cocoa -framework WebKit -framework UserNotifications

cp Info.plist "$APP/Contents/Info.plist"

echo "→ rendering icon…"
swift Sources/MakeIcon.swift dist/icon-1024.png >/dev/null
ICONSET=dist/AppIcon.iconset
mkdir -p "$ICONSET"
for s in 16 32 64 128 256 512; do
  sips -z $s $s dist/icon-1024.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d dist/icon-1024.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

echo "→ signing (ad-hoc)…"
codesign --force --deep -s - "$APP"

echo "✓ built $APP"

if [[ "$1" == "--install" ]]; then
  echo "→ installing to /Applications…"
  rm -rf "/Applications/Chief of Staff.app"
  cp -R "$APP" /Applications/
  open "/Applications/Chief of Staff.app"
  echo "✓ installed + launched"
fi
