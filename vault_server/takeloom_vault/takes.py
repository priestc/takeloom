"""Completed-take videos (and lossless audio), cut from their session on demand.

Takeloom no longer stores a separate .mp4 per take: its session's
session_log.json "takes" snapshot records where each take sits in
session_video.mp4 (a "video ref" — see takeloom/take_video.py for the
format). The first time a take's video is requested it's cut out here with
ffmpeg and kept in a cache directory; an hourly sweep deletes clips that
haven't been requested for a while, or the oldest ones once the cache is
over its size cap. Any clip can always be made again, so evicting is free.

A take's own file in completed_takes/ is lossy (LAME V2 .mp3); only the raw
session audio is kept lossless. original_for() cuts a take's lossless
original back out of its session, for an exported song mix: from
session.flac by sample (the take's "source" ref, when the session still has
it), else from a camera video's Instrument Only (FLAC) track, else a legacy
lossless completed_takes/<stem>.flac. Cached alongside the video clips.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .store import PARTIAL_PREFIX, Vault

SWEEP_INTERVAL_SECONDS = 3600


LEGACY_SESSION_VIDEO = "session_video.mp4"


def camera_refs(info: dict) -> list[dict]:
    """Every camera's ref in a take's video info, each with "camera" and
    "file" (a legacy single ref: one, for session_video.mp4). Same as
    takeloom/take_video.py's camera_refs — keep in step."""
    if isinstance(info.get("cameras"), list):
        return [c for c in info["cameras"] if isinstance(c, dict) and c.get("file")]
    if "start_s" in info:
        return [{"camera": "", "file": LEGACY_SESSION_VIDEO, **info}]
    return []


def choose_camera(info: dict, camera: str | None = None) -> dict | None:
    """`camera`'s ref (by label), else the take's default (the program
    camera when it started), else the first. Same as take_video.py's."""
    refs = camera_refs(info)
    for wanted in (camera, info.get("default")):
        if wanted:
            match = next((r for r in refs if r.get("camera") == wanted), None)
            if match is not None:
                return match
    return refs[0] if refs else None


def find_take_ref(vault: Vault, take_filename: str) -> dict | None:
    """The video info for `take_filename` (every camera's ref: see
    camera_refs), from whichever session's "takes" snapshot lists it, or
    None. A legacy take whose snapshot has no ref
    (processed before refs existed) won't be found; its old per-take .mp4
    in completed_takes/ is served instead, if there is one."""
    for name in vault.session_names():
        data = vault.read_session_log(name) or {}
        for entries in (data.get("takes") or {}).values():
            for entry in entries:
                if entry.get("filename") == take_filename and entry.get("video"):
                    return entry["video"]
    return None


def find_take_entry(vault: Vault, take_filename: str) -> dict | None:
    """`take_filename`'s entry in whichever session's "takes" snapshot
    lists it (its TakeInfo fields, plus "video" and "source" when it has
    them), with that session's name added as "session_dir"; or None."""
    for name in vault.session_names():
        data = vault.read_session_log(name) or {}
        for entries in (data.get("takes") or {}).values():
            for entry in entries:
                if entry.get("filename") == take_filename:
                    return {**entry, "session_dir": name}
    return None


def instrument_track_offset(video: Path, sample_rate: int) -> int:
    """Where session.flac's frame 0 sits in `video`'s Instrument Only track
    (its second audio stream), in samples: that track's first timestamp.
    A camera muxed with its audio delayed (a negative sync offset) has it
    late by the delay — rounded by the muxer to whole ms, which is why a
    take is found in a camera video from its frames plus this, never from
    its video ref's seconds (those assume the unrounded delay). Same as
    takeloom/take_original.py's — keep in step."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:1", "-show_entries", "stream=start_pts,time_base",
         "-of", "json", str(video)], capture_output=True, text=True,
    )
    stream = (json.loads(result.stdout or "{}").get("streams") or [{}])[0]
    num, _, den = str(stream.get("time_base") or "1/1").partition("/")
    return round(int(stream.get("start_pts") or 0) * int(num) * sample_rate / int(den or 1))


def exact_video_cut_cmd(video: Path, offset: int, start_frame: int, end_frame: int, sample_rate: int) -> list[str]:
    """ffmpeg (minus the output path) cutting session.flac's frames
    [start_frame, end_frame) out of `video`'s Instrument Only track, to the
    sample. Seeked to a whole second a little before — after which ffmpeg
    counts samples from that point in the file, where the track is late by
    `offset` — or, for a take in the session's first seconds, not seeked at
    all, when ffmpeg counts them from the track's own first sample. Same as
    takeloom/take_original.py's exact_video_cut_args — keep in step."""
    n = end_frame - start_frame
    seek = max(0, (start_frame + offset) // sample_rate - 2)
    first = start_frame + offset - seek * sample_rate if seek else start_frame
    return [
        "ffmpeg", "-y", "-v", "error", *(["-ss", str(seek)] if seek else []), "-i", str(video),
        "-map", "0:a:1", "-af", f"atrim=start_sample={first}:end_sample={first + n},asetpts=PTS-STARTPTS",
        "-c:a", "flac",
    ]


def original_cmd(entry: dict, vault: Vault, use_legacy: bool = True) -> tuple[list[str] | Path, str] | None:
    """How to get `entry`'s take losslessly — an ffmpeg command (minus the
    output path) cutting it out of its session, or the path of a file that
    already is it — plus a description of where from; or None if nothing
    lossless is left. In order: session.flac at the take's frames (its
    "source" ref); a legacy .flac take file (exact, as it was cut from
    session.flac; skipped without `use_legacy`, which is how takeloom's
    migration checks a video cut against it); a camera video's Instrument
    Only track, at the take's frames (exact), else at its video ref (from
    before takes had frames: can be off by under a ms). A retuned MIDI
    take's session audio is at the wrong pitch, so only its legacy file
    will do (takeloom re-renders a newer one from its MIDI)."""
    source = entry.get("source") or {}
    session = str(source.get("session") or entry.get("session_dir") or "")
    try:
        directory = vault.session_dir(session) if session else None
    except Exception:
        directory = None
    frames = "start_frame" in source and "end_frame" in source
    sample_rate = int(source.get("sample_rate") or 48000)
    if not source.get("retuned") and directory is not None and frames and (directory / "session.flac").is_file():
        start, end = int(source["start_frame"]), int(source["end_frame"])
        return ([
            "ffmpeg", "-y", "-v", "error", "-i", str(directory / "session.flac"),
            "-af", f"atrim=start_sample={start}:end_sample={end},asetpts=PTS-STARTPTS",
            "-c:a", "flac",
        ], f"{session}/session.flac")
    filename = str(entry.get("filename") or "")
    legacy = vault.root / "completed_takes" / (Path(filename).stem + ".flac")
    if use_legacy and filename and legacy.is_file():
        return legacy, f"completed_takes/{legacy.name}"
    if source.get("retuned"):
        return None
    info = entry.get("video") or {}
    video_session = str(info.get("session") or session)
    for ref in camera_refs(info):
        video_file = str(ref["file"])
        if "/" in video_file or "\\" in video_file or video_file.startswith(".") or not video_session:
            continue
        try:
            video = vault.session_dir(video_session) / video_file
        except Exception:
            continue
        if not video.is_file():
            continue
        if frames:
            offset = instrument_track_offset(video, sample_rate)
            return (exact_video_cut_cmd(video, offset, int(source["start_frame"]), int(source["end_frame"]),
                                        sample_rate), f"{video_session}/{video_file}")
        return ([
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{max(0.0, ref['instrument_start_s']):.6f}", "-i", str(video),
            "-map", "0:a:1", "-t", f"{ref['duration_s']:.6f}", "-c:a", "flac",
        ], f"{video_session}/{video_file}")
    return None


def clip_cmd(session_video: Path, ref: dict, out_path: Path) -> list[str]:
    """Same command as takeloom/take_video.py's clip_cmd — keep in step."""
    return [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{max(0.0, ref['start_s']):.6f}", "-i", str(session_video),
        "-ss", f"{max(0.0, ref['instrument_start_s']):.6f}", "-i", str(session_video),
        "-map", "0:v:0", "-map", "0:a:0", "-map", "1:a:1",
        "-t", f"{ref['duration_s']:.6f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a:0", "aac", "-c:a:1", "libmp3lame", "-q:a:1", "2",
        "-metadata:s:a:0", "title=Mix", "-metadata:s:a:1", "title=Instrument Only",
        "-disposition:a:0", "0", "-disposition:a:1", "default",
        "-movflags", "+faststart",
        str(out_path),
    ]


class TakeVideoError(Exception):
    pass


class TakeVideoCache:
    def __init__(
        self, vault: Vault, cache_dir: Path, max_age_days: float, max_bytes: int,
        log: Callable[[str], None],
    ) -> None:
        self.vault = vault
        self.dir = cache_dir
        self.max_age_seconds = max_age_days * 86400
        self.max_bytes = max_bytes
        self._log = log
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, take_filename: str, camera: str = "") -> Path:
        """A ready-to-serve video of `take_filename` from `camera` (a label;
        "": the take's default, the program camera when it started): its
        legacy per-take .mp4 if the vault still has one, else a cached clip,
        cut first if needed. Raises TakeVideoError if neither is possible.
        Concurrent requests for the same clip wait for one cut rather than
        each starting their own."""
        if "/" in take_filename or "\\" in take_filename or take_filename.startswith("."):
            raise TakeVideoError(f"Invalid take name {take_filename!r}")
        legacy = self.vault.root / "completed_takes" / (Path(take_filename).stem + ".mp4")
        if legacy.is_file():
            return legacy

        info = find_take_ref(self.vault, take_filename)
        ref = choose_camera(info, camera) if info is not None else None
        if ref is None:
            raise TakeVideoError(f"No video is recorded for take {take_filename!r}.")
        video_file = str(ref["file"])
        if "/" in video_file or "\\" in video_file or video_file.startswith("."):
            raise TakeVideoError(f"Bad video file {video_file!r} for take {take_filename!r}.")
        ref = {**ref, "session": info["session"]}
        session_video = self.vault.session_dir(info["session"]) / video_file
        if not session_video.is_file():
            if (session_video.parent / "render_jobs.json").is_file():
                raise TakeVideoError(f"The session video for {take_filename!r} is still being encoded "
                                     f"on the vault ({ref['session']}); try again in a few minutes.")
            raise TakeVideoError(f"Session video for {take_filename!r} is missing ({ref['session']}).")
        # Keyed by the ref and the session video's size/mtime too, not just
        # the name: a corrected ref or a rebuilt session video must never
        # keep serving a clip cut from what was there before.
        st = session_video.stat()
        key = hashlib.sha256(json.dumps(
            [take_filename, ref, st.st_size, st.st_mtime_ns], sort_keys=True,
        ).encode()).hexdigest()[:24]
        out = self.dir / f"{key}.mp4"

        with self._lock_for(key):
            if out.exists():
                out.touch()  # last-used time, for eviction
                return out
            tmp = self.dir / f"{PARTIAL_PREFIX}{key}.mp4"
            started = time.monotonic()
            result = subprocess.run(clip_cmd(session_video, ref, tmp), capture_output=True, text=True)
            if result.returncode != 0 or not tmp.exists():
                tmp.unlink(missing_ok=True)
                raise TakeVideoError(
                    f"ffmpeg couldn't cut {take_filename!r} from {ref['session']}: "
                    f"{(result.stderr or '').strip()[-300:] or 'exit code ' + str(result.returncode)}"
                )
            tmp.replace(out)
            self._log(f"Cut take video {take_filename!r} from {ref['session']} "
                      f"({ref['duration_s']:.0f}s of video in {time.monotonic() - started:.0f}s)")
            return out

    def original_for(self, take_filename: str, use_legacy: bool = True) -> Path:
        """A lossless FLAC of `take_filename` as recorded (see original_cmd),
        cut on first request and cached. Raises TakeVideoError if nothing
        lossless is left of it."""
        if "/" in take_filename or "\\" in take_filename or take_filename.startswith("."):
            raise TakeVideoError(f"Invalid take name {take_filename!r}")
        entry = find_take_entry(self.vault, take_filename) or {"filename": take_filename}
        found = original_cmd(entry, self.vault, use_legacy)
        if found is None:
            raise TakeVideoError(f"No lossless original is left for take {take_filename!r}.")
        cmd, where = found
        if isinstance(cmd, Path):
            return cmd
        src = Path(cmd[cmd.index("-i") + 1])
        st = src.stat()
        key = hashlib.sha256(json.dumps(
            ["original", take_filename, cmd, st.st_size, st.st_mtime_ns], sort_keys=True,
        ).encode()).hexdigest()[:24]
        out = self.dir / f"{key}.flac"
        with self._lock_for(key):
            if out.exists():
                out.touch()
                return out
            tmp = self.dir / f"{PARTIAL_PREFIX}{key}.flac"
            result = subprocess.run([*cmd, str(tmp)], capture_output=True, text=True)
            if result.returncode != 0 or not tmp.exists():
                tmp.unlink(missing_ok=True)
                raise TakeVideoError(
                    f"ffmpeg couldn't cut the original of {take_filename!r} from {where}: "
                    f"{(result.stderr or '').strip()[-300:] or 'exit code ' + str(result.returncode)}"
                )
            tmp.replace(out)
            self._log(f"Cut lossless original of {take_filename!r} from {where}")
            return out

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_lock:
            return self._locks.setdefault(key, threading.Lock())

    # --- eviction ---

    def start_sweeper(self) -> None:
        def loop() -> None:
            while True:
                try:
                    self.sweep()
                except Exception as e:  # never let the sweeper thread die
                    self._log(f"Take-video cache sweep failed: {type(e).__name__}: {e}")
                time.sleep(SWEEP_INTERVAL_SECONDS)
        threading.Thread(target=loop, daemon=True, name="take-video-cache-sweeper").start()

    def sweep(self) -> None:
        now = time.time()
        files = []
        for f in self.dir.iterdir():
            if not f.is_file():
                continue
            st = f.stat()
            if f.name.startswith(PARTIAL_PREFIX):
                if now - st.st_mtime > 6 * 3600:  # left behind by a crash mid-cut
                    f.unlink(missing_ok=True)
                continue
            files.append((st.st_mtime, st.st_size, f))
        files.sort()  # least recently used first
        total = sum(size for _, size, _ in files)
        removed = 0
        for mtime, size, f in files:
            if now - mtime > self.max_age_seconds or total > self.max_bytes:
                f.unlink(missing_ok=True)
                total -= size
                removed += 1
        if removed:
            self._log(f"Take-video cache: evicted {removed} clip(s); {total / 1e9:.1f} GB left")
