"""YouTube video download for backing tracks, via the yt-dlp CLI binary.

Downloads the full video (not just its audio), so it's kept alongside the
backing track for whatever else it's useful for. Playback/mixing pulls just
the audio stream back out of it via ffmpeg (see takeloom/audio/formats.py),
the same way any other video-file backing track is handled."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from .utils import sanitize_filename

YOUTUBE_URL_RE = re.compile(r"(youtube\.com/|youtu\.be/)", re.IGNORECASE)

# Matches yt-dlp's --newline progress lines, e.g.:
#   [download]   5.5% of  679.44MiB at    5.98MiB/s ETA 01:47
_PROGRESS_RE = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")

# (percent 0-100, or None for a non-progress status line; the raw line text)
ProgressCallback = Callable[[float | None, str], None]


class YouTubeDownloadError(Exception):
    """Raised when a YouTube URL can't be resolved or downloaded via yt-dlp."""


def is_youtube_url(text: str) -> bool:
    return bool(YOUTUBE_URL_RE.search(text.strip()))


def download_youtube_video(
    url: str, dest_dir: Path, video_format: str = "mp4", on_progress: ProgressCallback | None = None,
) -> tuple[Path, str, float]:
    """Download a YouTube video (video + audio, not audio-only) into dest_dir
    via yt-dlp, merging into a single container.

    If given, on_progress(percent, line) is called for every line of the
    download step's live output — percent is set when the line reports a
    download percentage, None for other status lines (fetching, merging, ...).

    Returns (file_path, title, duration_seconds).
    """
    if shutil.which("yt-dlp") is None:
        raise YouTubeDownloadError(
            "yt-dlp isn't installed. Install it (e.g. `brew install yt-dlp` or "
            "`pip install yt-dlp`) to add YouTube backing tracks."
        )

    try:
        info_result = subprocess.run(
            ["yt-dlp", "-j", "--no-playlist", url],
            capture_output=True, text=True, check=True,
        )
        info = json.loads(info_result.stdout)
    except subprocess.CalledProcessError as e:
        raise YouTubeDownloadError(f"Could not read video info for {url}: {e.stderr.strip() or e}") from e
    except json.JSONDecodeError as e:
        raise YouTubeDownloadError(f"Could not read video info for {url}: {e}") from e

    title = info.get("title") or info.get("id") or "Untitled"
    duration = float(info.get("duration") or 0)
    video_id = info.get("id") or "unknown"
    # yt-dlp's own %(title)s can contain characters unsafe on disk; build our
    # own filename from the sanitized title plus the video ID for uniqueness.
    safe_name = sanitize_filename(title) or "track"
    dest_stem = f"{safe_name}_{video_id}"
    output_template = str(dest_dir / f"{dest_stem}.%(ext)s")

    proc = subprocess.Popen(
        [
            "yt-dlp", "--no-playlist", "-f", "bv*+ba/b",
            "--merge-output-format", video_format,
            "--newline", "--progress",
            "-o", output_template, url,
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    lines: list[str] = []
    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.rstrip("\n")
        lines.append(line)
        if on_progress:
            match = _PROGRESS_RE.search(line)
            percent = float(match.group(1)) if match else None
            on_progress(percent, line)
    proc.wait()
    if proc.returncode != 0:
        last_line = next((l for l in reversed(lines) if l.strip()), "yt-dlp failed")
        raise YouTubeDownloadError(f"Download failed for {url}: {last_line}")

    dest_path = dest_dir / f"{dest_stem}.{video_format}"
    if not dest_path.exists():
        raise YouTubeDownloadError("yt-dlp finished but the expected output file is missing.")
    return dest_path, title, duration
