"""Completed-take videos, cut from their session's video on demand.

Takeloom no longer stores a separate .mp4 per take: its session's
session_log.json "takes" snapshot records where each take sits in
session_video.mp4 (a "video ref" — see takeloom/take_video.py for the
format). The first time a take's video is requested it's cut out here with
ffmpeg and kept in a cache directory; an hourly sweep deletes clips that
haven't been requested for a while, or the oldest ones once the cache is
over its size cap. Any clip can always be made again, so evicting is free.
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


def clip_cmd(session_video: Path, ref: dict, out_path: Path) -> list[str]:
    """Same command as takeloom/take_video.py's clip_cmd — keep in step."""
    return [
        "ffmpeg", "-y", "-v", "error",
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
