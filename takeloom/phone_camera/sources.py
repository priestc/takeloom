"""A session's cameras as sources, each lined up on its own.

docs/camera-alignment-design.md, stage 1. A camera entry in the session
log ("extra_cameras", one per camera) gets a "sources" list: what's been
recorded for that camera, best first, each lined up independently once it's
there. Nothing is made from them here (no camera video, no mic file):
everything else is derived on demand from the source and its alignment.

    "sources": [
      {"file": "camera2_samsung_phone.mp4", "kind": "phone",
       "state": "waiting" | "arrived" | "aligned" | "failed",
       "align": {"file_zero_s": 0.8002, "drift": 4.99e-05,
                 "method": "audio" | "clock", "windows_matched": 24, ...}},
      {"file": "camera2_samsung_raw.mp4", "kind": "live", "state": "aligned",
       "align": {"file_zero_s": ..., "drift": 0.0, "method": "clock"}}]

The alignment: the file's time t (its raw timestamps, as -copyts decoding
sees them) is session time file_zero_s + t * (1 + drift), session time 0
being the mix's first sample (session.flac frame mix_start_frame).

"waiting": not here yet; "arrived": here in full, not lined up yet.

- "phone": the phone's own recording, archived untouched. Best once lined up.
- "live": the stream-quality feed the server recorded, lined up by clock sync
  alone. A placeholder, deleted the moment the phone's recording has arrived
  in full.
- "local": a camera attached to this machine, placed by the hand-tuned
  latency offset (config.video_latency_compensation_ms).
"""

from __future__ import annotations

import json
from pathlib import Path

from .hub import upload_paths
from .sync import _click_times, _clock_video_start, _probe_first_video_pts, measure_recording

VERSION = 1  # the session log's "camera_sources"


def _clock_align(phone: dict, first_pts_ns, clock_offset_ns, file: Path) -> dict | None:
    start = _clock_video_start(phone, first_pts_ns, clock_offset_ns)
    if start is None or not file.exists():
        return None
    try:
        first = _probe_first_video_pts(file)
    except RuntimeError:
        return None
    return {"file_zero_s": start - first, "drift": 0.0, "method": "clock"}


def live_source(entry: dict, session_dir: Path) -> dict | None:
    """The live feed this server recorded for a phone camera, lined up by
    clock sync, or None if there's no such file."""
    file = entry.get("file") or ""
    if not file or not (session_dir / file).exists():
        return None
    phone = entry.get("phone_camera") or {}
    align = _clock_align(phone, phone.get("first_live_pts_ns"),
                         phone.get("first_live_clock_offset_ns") or phone.get("clock_offset_ns"),
                         session_dir / file)
    return {"file": file, "kind": "live", "state": "aligned" if align else "failed",
            **({"align": align} if align else {"error": "no clock sync to place it by"})}


def local_source(entry: dict, session_dir: Path, offset_ms: float) -> dict | None:
    file = entry.get("file") or ""
    path = session_dir / file
    if not file or not path.exists():
        return None
    try:
        first = _probe_first_video_pts(path)
    except RuntimeError:
        return {"file": file, "kind": "local", "state": "failed", "error": "no video frames"}
    return {"file": file, "kind": "local", "state": "aligned",
            "align": {"file_zero_s": offset_ms / 1000.0 - first, "drift": 0.0, "method": "latency setting"}}


def phone_source(entry: dict, session_dir: Path, data: dict, mix_audio: Path) -> dict:
    """The phone's own recording: lined up if it has arrived in full here,
    else waiting for it."""
    file = entry["upload_file"]
    uploaded, _part, meta_path = upload_paths(session_dir, file)
    source = {"file": file, "kind": "phone", "state": "waiting"}
    if not uploaded.exists():
        return source
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        meta = {}
    try:
        result = measure_recording(
            uploaded, meta, entry.get("phone_camera") or {}, mix_audio, session_dir / "session.flac",
            int(data.get("sample_rate") or 48000), int(data.get("mix_start_frame") or 0),
            clicks=_click_times(data),
        )
    except Exception as e:  # noqa: BLE001 — never lose the session over this
        return {**source, "state": "failed", "error": str(e)}
    if result is None:
        return {**source, "state": "failed", "error": "no audio match and no clock sync"}
    align = {k: v for k, v in result.items() if k not in ("source", "offset_ms")}
    return {**source, "state": "aligned", "align": align}


def build_sources(data: dict, session_dir: Path, mix_audio: Path, local_offset_ms: float) -> None:
    """Fill in every camera entry's "sources" (see the module docstring),
    lining up whatever's here. A phone's live feed is deleted as soon as its
    own recording is here in full. Marks the log "camera_sources"."""
    for entry in data.get("extra_cameras") or []:
        if entry.get("phone_camera") and entry.get("upload_file"):
            phone = phone_source(entry, session_dir, data, mix_audio)
            sources = [phone]
            if phone["state"] == "waiting":
                live = live_source(entry, session_dir)
                if live is not None:
                    sources.append(live)
            elif entry.get("file"):
                (session_dir / entry["file"]).unlink(missing_ok=True)  # superseded
            entry["sources"] = sources
        else:
            local = local_source(entry, session_dir, local_offset_ms)
            entry["sources"] = [local] if local else []
    data["camera_sources"] = VERSION


def best_source(entry: dict) -> dict | None:
    """The source a camera shows: its first lined-up one."""
    return next((s for s in entry.get("sources") or [] if s.get("state") == "aligned"), None)


def camera_state(entry: dict) -> str:
    """What the vault shows for a camera: "phone" (its own recording, lined
    up), "lining up" (arrived, not lined up yet), "live" (stream quality
    only), "local", or "none"."""
    sources = entry.get("sources") or []
    phone = next((s for s in sources if s.get("kind") == "phone"), None)
    best = best_source(entry)
    if best is not None:
        return best["kind"]
    if phone is not None and phone.get("state") == "arrived":
        return "lining up"
    return "none"
