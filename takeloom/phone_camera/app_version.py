"""The Takeloom Camera app's version, as this takeloom knows it.

camera_app/VERSION is the apps' one version number: Android's build reads
it, and `takeloom make-release --camera` writes it into the Xcode project
when it tags a camera release (camera-vX.Y.Z). It only changes when the
camera app does, independently of takeloom's own version.

takeloom ships that same file (force-included into the wheel as
takeloom/data/camera_app_VERSION, see pyproject.toml), so it knows the
newest camera release as of its own build without asking the network, and
can tell a connected phone it's out of date. An editable install reads the
working copy's file directly.
"""

from __future__ import annotations

from pathlib import Path

_PACKAGED = Path(__file__).resolve().parent.parent / "data" / "camera_app_VERSION"
_CHECKOUT = Path(__file__).resolve().parent.parent.parent / "camera_app" / "VERSION"


def latest_camera_app_version() -> str | None:
    for path in (_PACKAGED, _CHECKOUT):
        try:
            return path.read_text().strip() or None
        except OSError:
            continue
    return None


def parse_version(version: str) -> tuple[int, ...] | None:
    """"1.2.3" -> (1, 2, 3); "1.0" -> (1, 0, 0). None if not numeric."""
    try:
        parts = tuple(int(p) for p in version.strip().split("."))
    except ValueError:
        return None
    return parts + (0,) * (3 - len(parts)) if 0 < len(parts) <= 3 else None


def version_status(app_version: str) -> dict:
    """What the UI shows for a phone running `app_version`: its version,
    the newest known release, and whether it's behind that."""
    latest = latest_camera_app_version()
    running, newest = parse_version(app_version or ""), parse_version(latest or "")
    return {
        "version": app_version or "",
        "latest": latest or "",
        "outdated": bool(running and newest and running < newest),
    }
