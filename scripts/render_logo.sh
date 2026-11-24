#!/bin/sh
# Render the logo (docs/logo/logo.html) to transparent PNGs, with headless
# Chrome, trimmed to the mark: the README's, the vault's YouTube watermark,
# and the app icons, takeloom's and the Camera app's (its T scrap). Needs Google Chrome and a Python with Pillow
# (takeloom's own venv has it). Run from anywhere; network needed for the
# Google Fonts.
set -e
cd "$(dirname "$0")/.."
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
PY="${PY:-$HOME/.local/pipx/venvs/takeloom/bin/python}"
page="file://$PWD/docs/logo/logo.html"
tmp=$(mktemp -d)
shot() {  # shot <query> <out.png> <window width> <window height>
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --default-background-color=00000000 --virtual-time-budget=8000 \
    --window-size="$3,$4" --screenshot="$tmp/raw.png" "$page?$1" >/dev/null 2>&1
  "$PY" - "$tmp/raw.png" "$2" <<'PYEOF'
import sys
from PIL import Image
im = Image.open(sys.argv[1]).convert("RGBA")
box = im.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
im = im.crop(box)
pad = max(2, im.height // 40)
out = Image.new("RGBA", (im.width + 2 * pad, im.height + 2 * pad))
out.paste(im, (pad, pad))
out.save(sys.argv[2], optimize=True)
print(sys.argv[2], out.size)
PYEOF
}
shot "s=240" docs/logo/takeloom-logo.png 2400 700
"$PY" -c "import sys; from PIL import Image; im = Image.open(sys.argv[1]); im.thumbnail((960, 960), Image.LANCZOS); im.save(sys.argv[2], optimize=True)" docs/logo/takeloom-logo.png vault_server/takeloom_vault/logo.png
# The app icons, all the T scrap alone on the same dark tile (#111):
# - takeloom/data/icon.png: a rounded tile on macOS's icon grid (an 824 tile
#   in a 1024 canvas), so it sits the same size as other apps' in the Dock.
# - the Camera app's iOS icon: one opaque 1024 square (iOS rounds it).
# - the Camera app's Android adaptive-icon foreground: a 108dp canvas, the
#   scrap inside the middle 66dp circle every launcher's mask keeps; the
#   tile color is res/values/ic_launcher_background.xml.
shot "s=700&only=0" "$tmp/t.png" 1400 1400
"$PY" - "$tmp/t.png" <<'PYEOF'
import sys
from PIL import Image, ImageDraw
TILE_COLOR = (17, 17, 17, 255)
t = Image.open(sys.argv[1]).convert("RGBA")

def scrap(width, height):
    s = t.copy()
    s.thumbnail((width, height), Image.LANCZOS)
    return s

def centered(canvas, s):
    canvas.alpha_composite(s, ((canvas.width - s.width) // 2, (canvas.height - s.height) // 2))
    return canvas

S, TILE, RADIUS = 1024, 824, 185
mask = Image.new("L", (S, S))
ImageDraw.Draw(mask).rounded_rectangle([(S - TILE) // 2] * 2 + [(S + TILE) // 2 - 1] * 2, RADIUS, fill=255)
icon = Image.new("RGBA", (S, S))
icon.paste(TILE_COLOR, mask=mask)
out = "takeloom/data/icon.png"
centered(icon, scrap(TILE, round(TILE * 0.7))).resize((512, 512), Image.LANCZOS).save(out, optimize=True)
print(out)

out = "camera_app/ios/TakeloomCamera/Assets.xcassets/AppIcon.appiconset/icon-1024.png"
centered(Image.new("RGBA", (S, S), TILE_COLOR), scrap(S, round(S * 0.7))).convert("RGB").save(out, optimize=True)
print(out)

FG = 432
safe = FG * 66 / 108  # the circle's diameter
h = safe / (1 + (t.width / t.height) ** 2) ** 0.5  # the scrap's diagonal fits it
out = "camera_app/android/app/src/main/res/drawable-nodpi/ic_launcher_foreground.png"
centered(Image.new("RGBA", (FG, FG)), scrap(FG, int(h))).save(out, optimize=True)
print(out)
PYEOF
rm -rf "$tmp"
