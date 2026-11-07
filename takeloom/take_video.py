"""A completed take's video, cut from its session's video on demand.

A take used to get its own .mp4 at processing time, a second full copy of
footage its session's session_video.mp4 already holds. Now processing
records only *where* the take sits in that session video (a "video ref"),
and the clip is cut out when someone actually wants to watch it:

- on the vault server (vault_server/takeloom_vault/takes.py), for sessions
  that live there, behind GET /api/takes/<filename>/video, into a cache it
  evicts on its own; or
- locally, by Backend.get_take_video_path via clip_take_video below, for a
  session still on this machine.

A take's video info is stored in two places: in the session's
session_log.json "takes" snapshot (where the vault server finds it), and as
a tiny sidecar next to the take's audio, completed_takes/<stem>.video.json
(so this machine knows a take has video, and which session to cut it from,
without reading every session log). Both hold the same dict. Every camera
recorded the take (cameras are a set: each one makes its own whole-session
video), so there's a ref per camera, and the one that was on the program
feed when the take started is the one to show first:

    {"session": <session dir name>, "default": <camera label>,
     "cameras": [{"camera": <label>, "file": <video in the session dir>,
                  "start_s": float, "instrument_start_s": float, "duration_s": float}, ...]}

A take processed before cameras were a set has a single ref for its
session's session_video.mp4 instead — {"session", "start_s",
"instrument_start_s", "duration_s"} — which camera_refs() reads as one
camera.

start_s is where the take starts in that video's video and Mix tracks;
instrument_start_s is the same moment in its Instrument Only track, which
runs on session.flac's own timeline rather than the mix's (see
mux_video_audio). Both already include that camera's latency offset,
applied when its video was muxed, so a clip is a plain seek.

Older takes may still have a real <stem>.mp4; that's preferred when present.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .utils import atomic_write_text

SIDECAR_SUFFIX = ".video.json"


def take_video_ref(
    session_dir_name: str, start_frame: int, end_frame: int, mix_start_frame: int, sample_rate: int,
    video_offset_ms: float,
) -> dict:
    """The ref for a take spanning [start_frame, end_frame) of session.flac.
    Mirrors mux_video_audio's offset handling: a positive offset delays the
    video stream (audio timeline unchanged); a negative one delays both
    audio tracks by the same amount, which shifts everything by that much."""
    audio_delay_s = max(0.0, -video_offset_ms / 1000.0)
    return {
        "session": session_dir_name,
        "start_s": round((start_frame - mix_start_frame) / sample_rate + audio_delay_s, 6),
        "instrument_start_s": round(start_frame / sample_rate + audio_delay_s, 6),
        "duration_s": round((end_frame - start_frame) / sample_rate, 6),
    }


LEGACY_SESSION_VIDEO = "session_video.mp4"


def camera_refs(info: dict) -> list[dict]:
    """Every camera's ref in a take's video info, each with "camera" and
    "file" (a legacy single ref: one, for session_video.mp4)."""
    if isinstance(info.get("cameras"), list):
        return [c for c in info["cameras"] if isinstance(c, dict) and c.get("file")]
    if "start_s" in info:
        return [{"camera": "", "file": LEGACY_SESSION_VIDEO, **info}]
    return []


def choose_camera(info: dict, camera: str | None = None) -> dict | None:
    """The ref to show: `camera`'s (by label), else the take's default
    (the program camera when it started), else the first."""
    refs = camera_refs(info)
    for wanted in (camera, info.get("default")):
        if wanted:
            match = next((r for r in refs if r.get("camera") == wanted), None)
            if match is not None:
                return match
    return refs[0] if refs else None


def sidecar_path(take_audio_path: Path) -> Path:
    return take_audio_path.with_name(take_audio_path.stem + SIDECAR_SUFFIX)


def write_take_video_ref(take_audio_path: Path, ref: dict) -> None:
    atomic_write_text(sidecar_path(take_audio_path), json.dumps(ref, indent=2))


def read_take_video_ref(take_audio_path: Path) -> dict | None:
    try:
        return json.loads(sidecar_path(take_audio_path).read_text())
    except (OSError, ValueError):
        return None


def take_has_video(take_audio_path: Path) -> bool:
    """A legacy per-take .mp4, or a ref to cut one from its session video."""
    return take_audio_path.with_suffix(".mp4").exists() or sidecar_path(take_audio_path).exists()


def clip_cmd(ffmpeg: str, session_video: Path, ref: dict, out_path: Path) -> list[str]:
    """The ffmpeg command that cuts `ref`'s take out of `session_video`.
    Re-encodes the video (input seeking + re-encode is frame-accurate; a
    stream copy could only start on a keyframe) and keeps both audio tracks
    in the same Mix (AAC) + Instrument Only (FLAC) layout a session video
    has. The vault server runs this same command — keep
    vault_server/takeloom_vault/takes.py's copy in step with it."""
    return [
        ffmpeg, "-y", "-v", "error",
        "-ss", f"{max(0.0, ref['start_s']):.6f}", "-i", str(session_video),
        "-ss", f"{max(0.0, ref['instrument_start_s']):.6f}", "-i", str(session_video),
        "-map", "0:v:0", "-map", "0:a:0", "-map", "1:a:1",
        "-t", f"{ref['duration_s']:.6f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a:0", "aac", "-c:a:1", "flac",
        "-metadata:s:a:0", "title=Mix", "-metadata:s:a:1", "title=Instrument Only",
        "-disposition:a:0", "0", "-disposition:a:1", "default",
        "-movflags", "+faststart",
        str(out_path),
    ]


def clip_take_video(session_video: Path, ref: dict, out_path: Path) -> bool:
    from .ffmpeg_bin import FFMPEG
    tmp = out_path.with_name(out_path.name + ".partial.mp4")
    result = subprocess.run(clip_cmd(FFMPEG, session_video, ref, tmp), capture_output=True)
    if result.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(out_path)
    return True
