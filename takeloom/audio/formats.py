"""Audio format handling: decode MP3/M4A/video via ffmpeg, read/write FLAC/WAV."""

from __future__ import annotations

import subprocess
import numpy as np
import soundfile as sf
from pathlib import Path

from takeloom.ffmpeg_bin import FFMPEG, FFPROBE

# Anything soundfile can't read natively goes through ffmpeg/ffprobe instead:
# compressed audio containers, and video containers (a video backing track's
# audio stream is extracted from it the same way).
_FFMPEG_EXTS = (
    ".mp3", ".m4a", ".aac", ".ogg", ".opus",
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi",
)

# Every file type read_audio()/get_duration() can handle — native (soundfile)
# formats plus everything decoded via ffmpeg above. Used wherever the app
# needs to recognize "this can be used as a backing track."
SUPPORTED_EXTS = frozenset({".wav", ".flac"} | set(_FFMPEG_EXTS))


def read_audio(path: Path, sample_rate: int | None = None) -> tuple[np.ndarray, int]:
    """Read an audio file, returning (float32 array, sample_rate).

    Supports FLAC, WAV natively. Compressed audio and video files (their
    audio stream) decoded via ffmpeg.
    Output is always float32. Mono files returned as (N,1), stereo as (N,2).
    """
    suffix = path.suffix.lower()
    if suffix in _FFMPEG_EXTS:
        return _decode_with_ffmpeg(path, sample_rate)
    # Native soundfile formats
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if sample_rate and sr != sample_rate:
        data, sr = _decode_with_ffmpeg(path, sample_rate)
    return data, sr


def _decode_with_ffmpeg(path: Path, target_sr: int | None = None) -> tuple[np.ndarray, int]:
    """Decode any audio file to raw PCM float32 via ffmpeg subprocess."""
    sr = target_sr or 48000
    cmd = [
        FFMPEG, "-i", str(path),
        "-f", "f32le",
        "-acodec", "pcm_f32le",
        "-ar", str(sr),
        "-ac", "2",  # always output stereo
        "-v", "quiet",
        "-"
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to decode {path}: {result.stderr.decode()}")
    data = np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, 2)
    return data, sr


def write_flac(path: Path, data: np.ndarray, sample_rate: int) -> None:
    """Write audio data to FLAC file."""
    sf.write(str(path), data, sample_rate, format="FLAC", subtype="PCM_16")


def get_duration(path: Path) -> float:
    """Get duration of an audio (or video) file in seconds."""
    suffix = path.suffix.lower()
    if suffix in _FFMPEG_EXTS:
        cmd = [
            FFPROBE, "-i", str(path),
            "-show_entries", "format=duration",
            "-v", "quiet", "-of", "csv=p=0"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
        return 0.0
    info = sf.info(str(path))
    return info.duration
