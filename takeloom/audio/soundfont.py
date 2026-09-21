"""Fetches and caches the SoundFont FluidSynth-backed Synth voices render
from (see synth.py's _FluidSynthVoice) — real sampled piano/organ audio,
not the built-in additive synth's pure-math approximation.

There's no PyPI package that bundles a soundfont the way static-ffmpeg
bundles ffmpeg binaries (see ffmpeg_bin.py's own docstring for that
pattern) and a ~32MB binary has no business living in this repo's git
history forever, so this follows the same *shape* of solution by hand:
fetch once, cache under the user's home directory, and never re-fetch
once a good copy is already there. ensure_soundfont() never raises —
every failure (no network, an interrupted download, a full disk) just
means callers fall back to the additive synth instead of a session
refusing to record.

GeneralUser GS (https://www.schristiancollins.com/generaluser) is the
specific SoundFont used: General MIDI-compatible, includes real sampled
acoustic piano and drawbar organ patches, and — per its author's own
LICENSE.txt (mirrored alongside the file at the URL below) — is
explicitly free to use and redistribute in software projects, private
or commercial, with no fee or restriction. That license also asks
anyone hosting it not to deep-link the author's own download files but
to serve their own copy instead, which is exactly what caching a local
copy here does.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

_SOUNDFONT_URL = "https://raw.githubusercontent.com/mrbumpy409/GeneralUser-GS/main/GeneralUser-GS.sf2"
_SOUNDFONT_DIR = Path.home() / ".takeloom" / "soundfonts"
_SOUNDFONT_PATH = _SOUNDFONT_DIR / "GeneralUser-GS.sf2"
# The real file is ~32MB — anything drastically smaller than this means
# a previous download was interrupted/truncated (or the URL started
# serving an HTML error page instead), and should be re-fetched rather
# than trusted as-is.
_MIN_VALID_BYTES = 20_000_000


def ensure_soundfont() -> Path | None:
    """Local path to a usable GeneralUser GS.sf2, downloading it once to
    ~/.takeloom/soundfonts/ if it isn't already cached there. Returns
    None (never raises) on any failure. Safe — if slow — to call from
    whichever thread first constructs a MIDI instrument's Synth; that's
    normally the background thread start_monitoring() already runs on
    at app/server startup (see AppState.__init__), which is built to
    tolerate exactly this kind of one-time slow I/O without blocking
    anything the operator is waiting on. Every launch after the first
    successful download returns instantly (a plain existence + size
    check), since the cached file is never re-verified byte-for-byte."""
    try:
        if _SOUNDFONT_PATH.exists() and _SOUNDFONT_PATH.stat().st_size >= _MIN_VALID_BYTES:
            return _SOUNDFONT_PATH
        _SOUNDFONT_DIR.mkdir(parents=True, exist_ok=True)
        # Downloaded under a temp name and only renamed into place once
        # complete, so a download that dies partway through (network
        # drop, disk full) never leaves a truncated file that the size
        # check above would then wrongly accept as valid on next launch.
        tmp_path = _SOUNDFONT_PATH.with_suffix(".sf2.part")
        urllib.request.urlretrieve(_SOUNDFONT_URL, tmp_path)
        if tmp_path.stat().st_size < _MIN_VALID_BYTES:
            tmp_path.unlink(missing_ok=True)
            return None
        tmp_path.replace(_SOUNDFONT_PATH)
        return _SOUNDFONT_PATH
    except Exception:
        return None
