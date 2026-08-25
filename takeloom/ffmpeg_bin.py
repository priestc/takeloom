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

import shutil

from static_ffmpeg import run as _run

FFMPEG, FFPROBE = _run.get_or_fetch_platform_executables_else_raise()

CAMERA_FFMPEG = shutil.which("ffmpeg") or FFMPEG
