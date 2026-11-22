"""A completed take's lossless original, for an exported song mix.

A take's own file in completed_takes/ is lossy (LAME V2 .mp3, see
audio/formats.py's LAME_ARGS); only a session's raw recording is kept
lossless. So an exported mix (Backend.export_song_mix) never mixes the
.mp3s: each take is cut back out of its session, losslessly, from the first
of these that exists:

1. a retuned MIDI take (TakeInfo.source "retuned"): its session audio is at
   the wrong pitch, so it's rendered again from its .mid, the same way
   processing made it;
2. session.flac, at the take's span (TakeInfo.source's frames), when the
   session is still on this machine;
3. a legacy lossless take file (completed_takes/<stem>.flac, from before
   takes were lossy: exact, being a copy cut from session.flac);
4. a camera video's Instrument Only track (FLAC, on session.flac's own
   timeline), when the session is still on this machine: at the take's
   frames plus that track's own start offset (exact_video_cut_args), else,
   for a take from before takes had frames, at its video ref (take_video.py;
   can be off by under a ms, the muxer having rounded the camera's delay);
5. the remote vault, which does 2-4 on its end (GET /api/takes/<f>/original:
   vault_server/takeloom_vault/takes.py's original_for).

The take's source ref comes from its TakeInfo, else from whichever session
log's "takes" snapshot lists it (the same lookup the vault does).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import StudioConfig
from .take_video import camera_refs, read_take_video_ref
from .vault import remote_vault, vault_root


class NoOriginalError(Exception):
    pass


def _snapshot_entry(root: Path, filename: str) -> dict | None:
    sessions = root / "sessions"
    if not sessions.is_dir():
        return None
    for log_path in sessions.glob("*/session_log.json"):
        try:
            data = json.loads(log_path.read_text())
        except (OSError, ValueError):
            continue
        for entries in (data.get("takes") or {}).values():
            for entry in entries:
                if entry.get("filename") == filename:
                    return {**entry, "session_dir": log_path.parent.name}
    return None


def _cut_session_flac(session_flac: Path, start: int, end: int, out_path: Path) -> None:
    import soundfile as sf
    from .processing.splicer import _copy_flac_segment
    with sf.SoundFile(str(session_flac)) as src:
        _copy_flac_segment(src, max(0, start), min(end, len(src)), out_path)


def instrument_track_offset(video: str, sample_rate: int, input_args: list[str] | None = None) -> int:
    """Where session.flac's frame 0 sits in `video`'s Instrument Only track,
    in samples: that track's first timestamp (late by the camera's audio
    delay, rounded to whole ms by the muxer). `video` may be a URL, with
    `input_args` (e.g. ffprobe -headers) ahead of it. Same as
    vault_server/takeloom_vault/takes.py's — keep in step."""
    from .ffmpeg_bin import FFPROBE
    result = subprocess.run(
        [FFPROBE, "-v", "error", *(input_args or []), "-select_streams", "a:1",
         "-show_entries", "stream=start_pts,time_base", "-of", "json", video], capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise NoOriginalError(f"Couldn't read the Instrument Only track of {video}: {result.stderr.strip()[-200:]}")
    stream = (json.loads(result.stdout or "{}").get("streams") or [{}])[0]
    num, _, den = str(stream.get("time_base") or "1/1").partition("/")
    return round(int(stream.get("start_pts") or 0) * int(num) * sample_rate / int(den or 1))


def exact_video_cut_args(video: str, offset: int, start_frame: int, end_frame: int, sample_rate: int,
                         input_args: list[str] | None = None) -> list[str]:
    """ffmpeg arguments (no binary, no output) cutting session.flac's frames
    [start_frame, end_frame) out of `video`'s Instrument Only track, to the
    sample (checked against real camera videos). Seeked to a whole second a
    little before — after which ffmpeg counts samples from that point in the
    file, where the track is late by `offset` — or, for a take in the
    session's first seconds, not seeked at all, when ffmpeg counts them from
    the track's own first sample. Same as vault_server/takeloom_vault/
    takes.py's exact_video_cut_cmd — keep in step."""
    n = end_frame - start_frame
    seek = max(0, (start_frame + offset) // sample_rate - 2)
    first = start_frame + offset - seek * sample_rate if seek else start_frame
    return [
        "-y", "-v", "error", *(["-ss", str(seek)] if seek else []), *(input_args or []), "-i", video,
        "-map", "0:a:1", "-af", f"atrim=start_sample={first}:end_sample={first + n},asetpts=PTS-STARTPTS",
        "-c:a", "flac",
    ]


def _cut_instrument_track(video: Path, ref: dict, out_path: Path) -> bool:
    from .ffmpeg_bin import FFMPEG
    result = subprocess.run([
        FFMPEG, "-y", "-v", "error", "-ss", f"{max(0.0, ref['instrument_start_s']):.6f}", "-i", str(video),
        "-map", "0:a:1", "-t", f"{ref['duration_s']:.6f}", "-c:a", "flac", str(out_path),
    ], capture_output=True)
    return result.returncode == 0 and out_path.exists()


def lossless_take_path(
    config: StudioConfig, filename: str, label: str, source: dict | None, out_dir: Path,
) -> Path:
    """A lossless copy of take `filename` (filed under instrument `label`)
    in `out_dir` — or a legacy lossless take file where it already is. See
    the module docstring for where it comes from. Raises NoOriginalError if
    nowhere has one."""
    root = vault_root(config)
    completed = root / "completed_takes"
    stem = Path(filename).stem
    out_path = out_dir / f"{stem}.original.flac"
    entry = None
    if not source:
        entry = _snapshot_entry(root, filename)
        source = (entry or {}).get("source")
    source = source or {}
    client = remote_vault(config)

    if source.get("retuned"):
        mid = completed / f"{stem}.mid"
        if not mid.exists() and client is not None:
            client.download_file(f"completed_takes/{mid.name}", mid)
        if mid.exists():
            from .audio.midi_log import render_midi_file
            render_midi_file(mid, label, int(source["sample_rate"]),
                             int(source["end_frame"]) - int(source["start_frame"]), out_path)
            return out_path
        raise NoOriginalError(f"'{filename}' was retuned, and its MIDI ({mid.name}) is nowhere to be found.")

    session = source.get("session") or (entry or {}).get("session_dir")
    frames = "start_frame" in source and "end_frame" in source
    if session and frames:
        session_flac = root / "sessions" / session / "session.flac"
        if session_flac.exists():
            _cut_session_flac(session_flac, int(source["start_frame"]), int(source["end_frame"]), out_path)
            return out_path

    legacy = completed / f"{stem}.flac"
    if legacy.exists():
        return legacy

    info = read_take_video_ref(completed / filename) or (entry or {}).get("video") or {}
    for ref in camera_refs(info):
        video = root / "sessions" / str(info.get("session") or session or "") / str(ref["file"])
        if not video.is_file():
            continue
        if frames:
            from .ffmpeg_bin import FFMPEG
            sr = int(source.get("sample_rate") or 48000)
            args = exact_video_cut_args(str(video), instrument_track_offset(str(video), sr),
                                        int(source["start_frame"]), int(source["end_frame"]), sr)
            if subprocess.run([FFMPEG, *args, str(out_path)], capture_output=True).returncode == 0:
                return out_path
        elif _cut_instrument_track(video, ref, out_path):
            return out_path

    if client is not None:
        if client.download_take_original(filename, out_path):
            return out_path
        raise NoOriginalError(f"The vault at {client.url} has no lossless original of '{filename}'.")
    raise NoOriginalError(f"No lossless original of '{filename}' is on this machine, and no remote vault is paired.")
