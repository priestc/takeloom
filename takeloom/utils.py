"""Timestamps, take numbering, and path helpers."""

from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path
from typing import Iterable


def timestamp_now() -> float:
    """Return monotonic time for audio-accurate measurements."""
    return time.monotonic()


def wall_timestamp() -> str:
    """Return human-readable wall-clock timestamp for logging."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: float) -> str:
    """Format seconds as MM:SS."""
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def format_duration_hms(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    h, remainder = divmod(int(seconds), 3600)
    m, s = divmod(remainder, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def sanitize_filename(name: str) -> str:
    """Remove characters unsafe for filenames."""
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


_YOUTUBE_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{6,15})\]|_([A-Za-z0-9_-]{6,15})$")
# "inspiration_" prefix from inspiration.py's backing_track naming
# (f"inspiration_{track_id}.{fmt}") and add_backing_track's collision-
# safe upload prefix (secrets.token_hex(4), 8 hex chars) — see below.
_INSPIRATION_STEM_RE = re.compile(r"^inspiration_(\d+)$")
_UPLOAD_PREFIX_RE = re.compile(r"^([0-9a-f]{8})_")


def _backing_track_id(source: str, backing_track: str) -> str:
    """A short identifier for `backing_track`, for the source tag in
    take_filename() — the actual video/track ID rather than the whole
    filename, per the naming schemes in youtube.py/inspiration.py/
    project.py's add_backing_track(). Falls back to the file's stem
    (still short in practice) if nothing more specific is recognized."""
    stem = Path(backing_track).stem if backing_track else "unknown"
    if source == "inspiration":
        m = _INSPIRATION_STEM_RE.match(stem)
        if m:
            return m.group(1)
    elif source == "youtube":
        m = _YOUTUBE_ID_RE.search(stem)
        if m:
            return m.group(1) or m.group(2)
    elif source == "upload":
        m = _UPLOAD_PREFIX_RE.match(stem)
        if m:
            return m.group(1)
    return stem


def take_filename(
    track_name: str, instrument: str, take_number: int, source: str, backing_track: str, ext: str = "flac",
) -> str:
    """Generate take filename: 'track - instrument - takeN [source:id].ext'.
    `source` is TrackEntry.source_label() — "upload", "youtube", or
    "inspiration" — and `backing_track` its backing_track filename (the
    exact file this take was recorded against); backing tracks are
    shared vault-wide now (see vault.py), so two entries can share a
    display name while pointing at different audio, and the filename
    should make which one — and where it came from — explicit rather
    than relying on the take being found next to the right backing track
    by chance."""
    safe_track = sanitize_filename(track_name)
    safe_inst = sanitize_filename(instrument)
    tag_id = sanitize_filename(_backing_track_id(source, backing_track))
    return f"{safe_track} - {safe_inst} - take{take_number} [{source}:{tag_id}].{ext}"


def next_take_number(
    completed_dir: Path, track_name: str, instrument: str, other_names: Iterable[str] = (),
) -> int:
    """Find the next available take number for a track/instrument combo:
    one past the highest among completed_dir's files and `other_names`
    (filenames elsewhere — the remote vault's completed_takes/, under
    "Remote only" storage, where a take's audio no longer stays here, so
    the number is never reused for a different take)."""
    prefix = f"{sanitize_filename(track_name)} - {sanitize_filename(instrument)} - take"
    max_num = 0
    stems = [f.stem for f in completed_dir.iterdir()] if completed_dir.exists() else []
    stems += [Path(name).stem for name in other_names]
    for stem in stems:
        if stem.startswith(prefix.rstrip("take")):
            match = re.search(r"take(\d+)", stem)
            if match:
                max_num = max(max_num, int(match.group(1)))
    return max_num + 1


def ensure_dir(path: Path) -> Path:
    """Create directory if it doesn't exist, return the path."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str) -> None:
    """Write text via temp-file-then-rename, so a concurrent reader on
    another thread/process never observes a truncated/partial file (as a
    plain path.write_text() — which truncates before writing — can produce).
    Uses mkstemp for a guaranteed-unique name, so concurrent writer threads
    in the same process can't collide on the same temp file either."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def frames_to_seconds(frames: int, sample_rate: int) -> float:
    return frames / sample_rate


def seconds_to_frames(seconds: float, sample_rate: int) -> int:
    return int(seconds * sample_rate)
