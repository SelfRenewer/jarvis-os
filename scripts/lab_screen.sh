#!/bin/zsh
# Jarvis Lab: the screen of the lab's MacBook = the user's main screen (16.10, the user: «возьми за экран скрин моей
# главной»): the current wallpaper (in the current light/dark appearance) + the Dock with the user's own apps and icons.
# Built from files, not a screen capture — screencapture would ask for screen-recording permission (a pending prompt
# can hang LaunchServices, R23) — so no window and nothing private gets in. The page draws the menu bar with a clock.
# Output (git-ignored, 0600): desktop/system-site/data/lab-screen.jpg, served only to a signed-in page
# (GET /api/lab/screen). Run again after changing the wallpaper or the Dock. Needs ImageMagick (`magick`).
set -euo pipefail
cd "${0:A:h}/.."
out=desktop/system-site/data/lab-screen.jpg
W=2048 H=1324 # the 3456 × 2234 panel at 0.5926
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
command -v magick >/dev/null || { echo "Нужен ImageMagick (magick)."; exit 1; }

# file:///a%20b → /a b
decode() { local p=${1#file://}; p=${p%/}; printf '%b' "${p//\%/\\x}"; }

store="$HOME/Library/Application Support/com.apple.wallpaper/Store/Index.plist"
plutil -extract AllSpacesAndDisplays.Linked.Content.Choices.0.Configuration raw -o - "$store" | base64 -d > "$work/conf.plist"
url=$(plutil -extract url.relative raw -o - "$work/conf.plist" 2>/dev/null || true)
wall=$(decode "$url")
[[ -n "$url" && -f "$wall" ]] || { echo "Обои — не файл-картинка (динамические или из коллекции): $url"; exit 1; }
# A light/dark wallpaper (a HEIC with two frames, like Apple's «hello»): the frame of the current appearance.
frame=0
[[ "$(defaults read -g AppleInterfaceStyle 2>/dev/null || true)" == Dark ]] && frame=1
cat > "$work/frame.swift" <<'SWIFT'
import Foundation
import ImageIO
import UniformTypeIdentifiers
let a = CommandLine.arguments // <image> <frame> <out.jpg>
guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: a[1]) as CFURL, nil) else { exit(1) }
let i = CGImageSourceGetCount(src) == 2 ? Int(a[2]) ?? 0 : 0 // more frames = a time-of-day wallpaper: the first one
guard let img = CGImageSourceCreateImageAtIndex(src, i, nil),
      let dst = CGImageDestinationCreateWithURL(URL(fileURLWithPath: a[3]) as CFURL, UTType.jpeg.identifier as CFString, 1, nil)
else { exit(1) }
CGImageDestinationAddImage(dst, img, [kCGImageDestinationLossyCompressionQuality: 0.9] as CFDictionary)
exit(CGImageDestinationFinalize(dst) ? 0 : 1)
SWIFT
swift "$work/frame.swift" "$wall" "$frame" "$work/wall.jpg" 2>/dev/null || sips -s format jpeg "$wall" --out "$work/wall.jpg" >/dev/null
magick "$work/wall.jpg" -resize "${W}x${H}^" -gravity center -extent "${W}x${H}" "$work/screen.png"

# The Dock: Finder, the user's apps in their order, the Bin. Icons at the Dock's own size (tilesize, points).
apps=(/System/Library/CoreServices/Finder.app)
while IFS= read -r u; do apps+=("$(decode "$u")"); done < <(
  defaults read com.apple.dock persistent-apps 2>/dev/null | grep -o '"_CFURLString" = "[^"]*"' | sed 's/.*= "//; s/"$//')
tile=$(defaults read com.apple.dock tilesize 2>/dev/null || echo 64)
size=$(( tile * 2 * W / 3456 ))
icons=()
for app in "${apps[@]}"; do
  res="$app/Contents/Resources"
  name=$(defaults read "$app/Contents/Info" CFBundleIconFile 2>/dev/null || true)
  icns=""
  for c in "$res/$name" "$res/$name.icns" "$res/AppIcon.icns"; do [[ -f "$c" ]] && { icns=$c; break; }; done
  [[ -n "$icns" ]] || continue # an icon only in Assets.car: left out
  png="$work/icon-${#icons}.png"
  sips -s format png -Z $(( size * 2 )) "$icns" --out "$png" >/dev/null 2>&1 && icons+=("$png")
done
bin=/System/Library/CoreServices/Dock.app/Contents/Resources/s-trashempty@2x.png
[[ -f "$bin" ]] && icons+=("$bin")

gap=$(( size / 9 )) pad=$(( size / 7 ))
n=${#icons}
dw=$(( n * size + (n - 1) * gap + 2 * pad )) dh=$(( size + 2 * pad ))
glass='rgba(246,246,250,0.30)' edge='rgba(255,255,255,0.35)'
(( frame )) && glass='rgba(36,34,52,0.50)' edge='rgba(255,255,255,0.18)' # the dark Dock
args=(-size "${dw}x${dh}" xc:none -fill "$glass" -stroke "$edge"
  -draw "roundrectangle 0,0 $(( dw - 1 )),$(( dh - 1 )) $(( dh * 3 / 10 )),$(( dh * 3 / 10 ))")
for i in {1..$n}; do
  args+=( \( "${icons[$i]}" -resize "${size}x${size}" \) -geometry "+$(( pad + (i - 1) * (size + gap) ))+$pad" -composite )
done
magick "${args[@]}" "$work/dock.png"
magick "$work/screen.png" "$work/dock.png" -gravity south -geometry +0+$(( size / 8 )) -composite -quality 88 "$work/screen.jpg"

mkdir -p "${out:h}"
install -m 600 "$work/screen.jpg" "$out"
echo "Экран для лаборатории: $out (обои «${wall:t:r}», в Dock $n значков). Обновите страницу лаборатории."
