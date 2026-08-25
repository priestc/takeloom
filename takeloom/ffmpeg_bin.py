"""Path to the ffmpeg/ffprobe binaries used everywhere in takeloom.

Backed by the static-ffmpeg package, which fetches self-contained,
statically-linked binaries (cached under site-packages) instead of relying
on whatever ffmpeg happens to be on PATH. This avoids breakage like a
Homebrew library upgrade (e.g. x265) leaving the system ffmpeg linked
against a dylib version that no longer exists on disk.
"""

from __future__ import annotations

from static_ffmpeg import run as _run

FFMPEG, FFPROBE = _run.get_or_fetch_platform_executables_else_raise()
