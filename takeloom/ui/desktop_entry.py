"""Linux desktop integration: the taskbar/dock icon and name.

Linux docks (GNOME Shell, KDE Plasma, ...) don't take an app's icon or name
from the window itself (iconphoto is mostly ignored there). They match the
window's WM_CLASS against an installed .desktop entry, and with no match
fall back to a generic icon labelled with the raw class: Tk's default "tk".

So the root window is created with className=WM_CLASS_NAME (WM_CLASS
"takeloom"/"Takeloom"), and ensure_desktop_entry() installs a matching
~/.local/share/applications/takeloom.desktop plus the icon, on every
launch — rewritten only when its content changed, so a pipx reinstall that
moves the executable fixes itself the next time the app opens.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Tk capitalizes the first letter for the class half of WM_CLASS:
# ("takeloom", "Takeloom") — what StartupWMClass below must match.
WM_CLASS_NAME = "takeloom"

ICON_PATH = Path(__file__).resolve().parent.parent / "data" / "icon.png"


def ensure_desktop_entry() -> None:
    """Best-effort; a no-op off Linux. Never raises — a missing dock icon
    must never stop the app from opening."""
    if not sys.platform.startswith("linux") or os.environ.get("FLATPAK_ID"):
        # A Flatpak brings its own desktop entry, and its paths only exist
        # inside the sandbox.
        return
    try:
        install_desktop_entry()
    except Exception:
        pass


def desktop_file_path() -> Path:
    return Path.home() / ".local" / "share" / "applications" / "takeloom.desktop"


def install_desktop_entry() -> None:
    """Write the .desktop entry and icon (see the module docstring); also
    `takeloom install-launcher`'s Linux half (launcher.py)."""
    data_home = Path.home() / ".local" / "share"
    icon_dest = data_home / "icons" / "hicolor" / "512x512" / "apps" / "takeloom.png"
    if not icon_dest.exists() or icon_dest.read_bytes() != ICON_PATH.read_bytes():
        icon_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ICON_PATH, icon_dest)

    executable = shutil.which("takeloom") or str(Path(sys.argv[0]).resolve())
    entry = "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Takeloom",
        "Comment=Music recording session manager",
        f"Exec={executable} ui",
        # Absolute, not the theme name: a fresh icon in hicolor isn't
        # always picked up until the icon cache is rebuilt.
        f"Icon={icon_dest}",
        "Terminal=false",
        "Categories=AudioVideo;Audio;Recorder;",
        f"StartupWMClass={WM_CLASS_NAME.capitalize()}",
        "",
    ])
    desktop_file = desktop_file_path()
    if desktop_file.exists() and desktop_file.read_text() == entry:
        return
    desktop_file.parent.mkdir(parents=True, exist_ok=True)
    desktop_file.write_text(entry)
    if shutil.which("update-desktop-database"):
        subprocess.run(
            ["update-desktop-database", str(desktop_file.parent)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
