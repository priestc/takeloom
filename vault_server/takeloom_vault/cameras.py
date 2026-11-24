"""A session's cameras, whichever way its videos are kept, for everything
that plays or cuts them: the session page's player, take clips (takes.py)
and the edit (edit.py).

Two kinds of session:

- **Sources** (session log "camera_sources"; docs/camera-alignment-design.md):
  each camera's best lined-up source — the phone's own recording, untouched,
  or its live feed, or a local camera's file — placed on the session's
  timeline by its alignment: file time t is session time
  file_zero_s + t * (1 + drift). The sound is the session's own:
  session.flac (the instrument) and session_mix.m4a (the mix, a reference).
- **Muxed** (older sessions): each camera's finished video, made at
  processing time with the session's audio muxed in, offset by its sync
  shift (session time T is T + shift in the file).

Either way a Camera says where session time T is in its file (file_time),
and no camera is special: none is "main".
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MIX_REFERENCE = "session_mix.m4a"


@dataclass
class Camera:
    title: str          # what pages show
    label: str          # what take refs and program_camera events name it by
    path: Path
    kind: str           # "phone" | "live" | "local" | "muxed"
    file_zero_s: float = 0.0
    drift: float = 0.0
    shift_s: float = 0.0  # muxed: session time T is T + shift_s in the file
    device: str = ""
    entry: dict = field(default_factory=dict)

    def file_time(self, session_t: float) -> float:
        """Where session time `session_t` is in this camera's file (may be
        negative: before it started)."""
        if self.kind == "muxed":
            return session_t + self.shift_s
        return (session_t - self.file_zero_s) / (1.0 + self.drift)

    @property
    def has_mic(self) -> bool:
        """Its file carries the room's sound (a phone's own recording)."""
        return self.kind == "phone"


def is_sources(data: dict) -> bool:
    return bool(data.get("camera_sources"))


def _safe(name) -> bool:
    return isinstance(name, str) and bool(name) and "/" not in name and "\\" not in name and not name.startswith(".")


def session_cameras(directory: Path, data: dict) -> list[Camera]:
    """Every camera with something to show, in the session log's order."""
    from .web import camera_title
    if not is_sources(data):
        from .edit import time_shift
        from .web import session_videos
        shifts = {"session_video.mp4": time_shift(data.get("video_offset_ms"))}
        devices = {}
        for cam in data.get("extra_cameras") or []:
            if cam.get("final_file"):
                shifts[cam["final_file"]] = time_shift(cam.get("video_offset_ms"))
                devices[cam["final_file"]] = cam.get("device", "")
        return [Camera(title, label, directory / file, "muxed", shift_s=shifts.get(file, 0.0),
                       device=devices.get(file, ""))
                for title, file, label in session_videos(directory, data)]
    cameras = []
    for entry in data.get("extra_cameras") or []:
        best = next((s for s in entry.get("sources") or []
                     if s.get("state") == "aligned" and _safe(s.get("file")) and (directory / s["file"]).is_file()),
                    None)
        if best is None:
            continue
        align = best.get("align") or {}
        label = entry.get("label") or ""
        cameras.append(Camera(
            camera_title(label, entry.get("phone_camera") or {}, data), label or best["file"],
            directory / best["file"], best.get("kind") or "phone",
            file_zero_s=float(align.get("file_zero_s") or 0.0), drift=float(align.get("drift") or 0.0),
            device=entry.get("device", ""), entry=entry,
        ))
    return cameras


def session_audio(directory: Path, data: dict) -> dict:
    """The session's own sound, for a sources session: {"mix": path or None
    (session time 0 = its time 0), "instrument": path or None,
    "instrument_offset_s": where session time 0 is in session.flac}."""
    mix = directory / MIX_REFERENCE
    if not mix.is_file() and (directory / "session_mix.flac").is_file():
        mix = directory / "session_mix.flac"
    instrument = directory / "session.flac"
    sample_rate = data.get("sample_rate") or 48000
    return {
        "mix": mix if mix.is_file() else None,
        "instrument": instrument if instrument.is_file() else None,
        "instrument_offset_s": (data.get("mix_start_frame") or 0) / sample_rate,
    }


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True)
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        return 0.0


def session_length(directory: Path, data: dict, cameras: list[Camera]) -> float:
    """How long the session is on its timeline: its mix (sources), else the
    longest any camera runs."""
    if is_sources(data):
        audio = session_audio(directory, data)
        if audio["mix"] is not None:
            return duration(audio["mix"])
        if audio["instrument"] is not None:
            return max(0.0, duration(audio["instrument"]) - audio["instrument_offset_s"])
    return max((duration(cam.path) - cam.shift_s for cam in cameras), default=0.0)


def mic_input_args(cam: Camera, total: float, sample_rate: int = 48000) -> tuple[list[str], str]:
    """(ffmpeg input arguments, audio filter chain) putting a phone's own
    recording's sound on the session's timeline, `total` s long, silence
    where it wasn't recording — takeloom's phone_camera/sync.py extract_mic:
    keep in step."""
    zero = cam.file_zero_s
    shift = (f"adelay=delays={zero * 1000:.3f}:all=1" if zero >= 0
             else f"atrim=start={-zero:.6f},asetpts=PTS-STARTPTS")
    chain = f"aresample={sample_rate}:async=1000:first_pts=0,{shift},apad,atrim=end={total:.6f}"
    return ["-itsscale", f"{1.0 + cam.drift:.9f}", "-copyts", "-i", str(cam.path)], chain
