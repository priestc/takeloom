"""Paths to the ffmpeg/ffprobe binaries used everywhere in takeloom.

FFMPEG/FFPROBE are backed by the static-ffmpeg package, which fetches
self-contained, statically-linked binaries (cached under site-packages)
instead of relying on whatever ffmpeg happens to be on PATH. This avoids
breakage like a Homebrew library upgrade (e.g. x265) leaving the system
ffmpeg linked against a dylib version that no longer exists on disk.

CAMERA_FFMPEG is a separate binary reserved for anything that opens the
physical webcam (device listing, live capture): macOS grants camera access
(TCC) per-binary, and the system ffmpeg is the one the user has already
authorized for camera access on this machine — the unsigned static binary
above gets silently denied and drops the real camera from its avfoundation
device list. Falls back to the static binary if no system ffmpeg is found
(e.g. a fresh machine without Homebrew), matching pre-existing behavior
there rather than failing outright.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from static_ffmpeg import run as _run


def _static_binaries() -> tuple[str, str]:
    """static-ffmpeg's binaries, fetched on first use. Its own fetch call
    takes a lock file inside its package dir every time, even once fetched,
    which fails in an install the user can't write to (a Homebrew test
    sandbox, an apt package under /usr). So: binaries already there (a
    package fetches them at build time) are used as-is, no lock; a
    writable install fetches as usual; a read-only one fetches into the
    user's cache instead."""
    exe_dir = Path(_run.get_platform_dir())
    suffix = ".exe" if sys.platform == "win32" else ""
    if (exe_dir / "installed.crumb").exists():
        return str(exe_dir / f"ffmpeg{suffix}"), str(exe_dir / f"ffprobe{suffix}")
    if os.access(Path(_run.__file__).parent, os.W_OK):
        return _run.get_or_fetch_platform_executables_else_raise()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "takeloom" / "static_ffmpeg"
    return _run._get_or_fetch_platform_executables_else_raise_no_lock(
        download_dir=str(cache / exe_dir.name),
    )


def _system_binaries() -> tuple[str, str] | None:
    """The system's ffmpeg/ffprobe, for a package that depends on its
    distro's ffmpeg instead of shipping the static one (the .deb, see
    packaging/deb/: apt keeps that ffmpeg's libraries consistent, so the
    breakage described above doesn't apply). Opted into with
    TAKELOOM_SYSTEM_FFMPEG=1, which the package's launcher sets."""
    if os.environ.get("TAKELOOM_SYSTEM_FFMPEG") != "1":
        return None
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    return (ffmpeg, ffprobe) if ffmpeg and ffprobe else None


FFMPEG, FFPROBE = _system_binaries() or _static_binaries()

CAMERA_FFMPEG = shutil.which("ffmpeg") or FFMPEG
