#!/bin/sh
# Render the logo (docs/logo/logo.html) to transparent PNGs, with headless
# Chrome, trimmed to the mark: the README's, the vault's YouTube watermark,
# and the app icon (its T scrap). Needs Google Chrome and a Python with Pillow
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
# The app icon (takeloom/data/icon.png): the T scrap alone on a dark
# rounded tile, laid out on macOS's icon grid (an 824 tile in a 1024 canvas)
# so it sits the same size as other apps' icons in the Dock.
shot "s=700&only=0" "$tmp/t.png" 1400 1400
"$PY" - "$tmp/t.png" takeloom/data/icon.png <<'PYEOF'
import sys
from PIL import Image, ImageDraw
S, TILE, RADIUS = 1024, 824, 185
icon = Image.new("RGBA", (S, S))
mask = Image.new("L", (S, S))
ImageDraw.Draw(mask).rounded_rectangle([(S - TILE) // 2] * 2 + [(S + TILE) // 2 - 1] * 2, RADIUS, fill=255)
icon.paste((17, 17, 17, 255), mask=mask)
t = Image.open(sys.argv[1]).convert("RGBA")
t.thumbnail((TILE, round(TILE * 0.7)), Image.LANCZOS)
icon.alpha_composite(t, ((S - t.width) // 2, (S - t.height) // 2))
icon.resize((512, 512), Image.LANCZOS).save(sys.argv[2], optimize=True)
print(sys.argv[2], "(512, 512)")
PYEOF
rm -rf "$tmp"
