#!/bin/zsh
# Builds desktop/build/Jarvis.app (menu-bar app, ad-hoc signed). Usage: ./desktop/build.sh
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
APP="$ROOT/build/Jarvis.app"

echo "→ compiling"
swift build -c release --package-path JarvisMac >/dev/null
BIN="$(swift build -c release --package-path JarvisMac --show-bin-path)/JarvisMac"

echo "→ assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
touch "$ROOT/build/.metadata_never_index"  # Spotlight must not list the build output as a second Jarvis
cp "$BIN" "$APP/Contents/MacOS/Jarvis"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.example.jarvis</string>
  <key>CFBundleName</key><string>Jarvis</string>
  <key>CFBundleDisplayName</key><string>Jarvis</string>
  <key>CFBundleExecutable</key><string>Jarvis</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Jarvis слушает ваши голосовые команды и слово «Джарвис».</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>Речь распознаётся прямо на этом Mac, чтобы понимать команды.</string>
</dict>
</plist>
PLIST

echo "→ icon"
ICONSET="$ROOT/build/AppIcon.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
swift make_icon.swift "$ROOT/../docs/assets/jarvis_logo.png" "$ROOT/build/icon_1024.png"
for size in 16 32 128 256 512; do
  sips -z $size $size "$ROOT/build/icon_1024.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) "$ROOT/build/icon_1024.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

echo "→ signing (ad-hoc)"
codesign --force --deep --sign - --identifier com.example.jarvis "$APP" >/dev/null
echo "✓ $APP"
