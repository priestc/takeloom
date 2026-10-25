"""Build Takeloom.dmg: Takeloom.app beside an Applications shortcut, for the
usual drag-to-install. The .app installs takeloom itself with pipx on first
open (see takeloom/launcher.py), so this image is all a new Mac needs.

Run on a Mac, from the repo root:  python3 scripts/build_macos_dmg.py [output.dmg]
(default dist/Takeloom.dmg). Needs Pillow (any takeloom venv has it).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from takeloom.launcher import build_macos_dmg  # noqa: E402

output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dist") / "Takeloom.dmg"
print(f"Built {build_macos_dmg(output)}")
