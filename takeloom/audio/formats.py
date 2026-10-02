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


def trim_audio_file(path: Path, trim_start_seconds: float, trim_end_seconds: float) -> float:
    """Permanently cut `trim_start_seconds` off the beginning and
    `trim_end_seconds` off the end of the audio/video file at `path`, in
    place (written to a temp file alongside it, then swapped in — so a
    crash or failed encode mid-trim never leaves `path` half-written).
    Returns the file's new duration in seconds.

    A native soundfile format (WAV/FLAC — what every take file, and some
    backing tracks, actually are) is trimmed directly via soundfile:
    sample-accurate, lossless, and avoids round-tripping through ffmpeg
    for the common case. Anything else (a compressed-audio or video
    backing track) goes through ffmpeg instead, re-encoding rather than
    stream-copying — the same "-ss before -i, then re-encode" shape
    video/capture.py already uses elsewhere in this codebase, which cuts
    on the exact requested timestamp instead of snapping to the nearest
    keyframe the way a copy-mode cut can."""
    duration = get_duration(path)
    new_duration = duration - trim_start_seconds - trim_end_seconds
    if new_duration <= 0:
        raise ValueError(
            f"Trim amount ({trim_start_seconds + trim_end_seconds:.1f}s) leaves nothing of "
            f"{path.name} ({duration:.1f}s long)."
        )

    tmp_path = path.with_name(f".{path.stem}.trimtmp{path.suffix}")
    if path.suffix.lower() in _FFMPEG_EXTS:
        cmd = [
            FFMPEG, "-y",
            "-ss", f"{trim_start_seconds:.6f}", "-i", str(path),
            "-t", f"{new_duration:.6f}",
            "-v", "quiet", str(tmp_path),
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg failed to trim {path}: {result.stderr.decode()}")
    else:
        with sf.SoundFile(str(path)) as src:
            sr = src.samplerate
            start_frame = round(trim_start_seconds * sr)
            end_frame = len(src) - round(trim_end_seconds * sr)
            with sf.SoundFile(
                str(tmp_path), mode="w", samplerate=sr, channels=src.channels,
                format=src.format, subtype=src.subtype,
            ) as out:
                src.seek(start_frame)
                remaining = end_frame - start_frame
                while remaining > 0:
                    block = src.read(min(remaining, 1 << 20), dtype="float32", always_2d=True)
                    if len(block) == 0:
                        break
                    out.write(block)
                    remaining -= len(block)

    tmp_path.replace(path)
    return get_duration(path)


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
