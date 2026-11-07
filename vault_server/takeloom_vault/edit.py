"""A session's edited video: its cameras cut together into one final file.

The session page's timeline editor (web.py) saves a plan, edit.json in the
session's folder:

    {"priority": [0, 2, 1], "cuts": [[0.0, 0], [42.5, 2], [47.5, 0], ...]}

"priority" ranks the cameras: the first is the main camera, on screen
most of the time and the source of the sound; the others are cut to now
and then, the lower the priority the less often (auto_cuts). "cuts" is a
list of (time on the session's timeline in seconds, camera), each camera
shown from its cut to the next. Cameras are indexes into
web.session_videos' list, resolved here (a plan never names a file).
Rendering makes edit.mp4: each stretch of picture from its camera's own
session video, and the audio, unbroken start to finish, from the main
camera, so a cut never breaks the sound.

All of a session's camera videos are on the same timeline, apart from a few
tens of ms: a camera with a negative sync offset had its audio delayed by
that much when it was muxed (takeloom's mux_spec), so session time T sits at
T + max(0, -offset) in that file (time_shift). Stretches are snapped to
whole frames (FPS) and each is encoded to exactly its frame count, so the
picture never drifts from the sound however many cuts there are.

Each stretch is its own short ffmpeg encode (frame-accurate seek, scaled to
the main camera's size), then they're joined without re-encoding and the
audio added. One render at a time, on a background thread, at low priority,
like render.py's session encodes.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .store import Vault

PLAN_FILE = "edit.json"
OUTPUT_FILE = "edit.mp4"
FPS = 30
MAX_CUTS = 5000


def time_shift(entry_offset_ms) -> float:
    """Where session time 0 falls in a camera's video (see the docstring)."""
    try:
        return max(0.0, -float(entry_offset_ms or 0.0) / 1000.0)
    except (TypeError, ValueError):
        return 0.0


def camera_files(directory: Path, data: dict) -> list[tuple[str, Path, float]]:
    """(title, file, time shift) per camera, in session_videos' order."""
    from .web import session_videos
    shifts = {"session_video.mp4": time_shift(data.get("video_offset_ms"))}
    for cam in data.get("extra_cameras") or []:
        if cam.get("final_file"):
            shifts[cam["final_file"]] = time_shift(cam.get("video_offset_ms"))
    return [(title, directory / file, shifts.get(file, 0.0)) for title, file, _label in session_videos(directory, data)]


def probe(path: Path) -> tuple[float, int, int]:
    """(duration s, width, height) of a video."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
         "-of", "json", str(path)], capture_output=True, text=True, check=True,
    ).stdout
    info = json.loads(out)
    stream = (info.get("streams") or [{}])[0]
    return float(info["format"]["duration"]), int(stream["width"]), int(stream["height"])


def checked_priority(priority, cameras: int) -> list[int]:
    """Every camera exactly once, highest priority first. Raises ValueError."""
    if (not isinstance(priority, list) or any(isinstance(c, bool) or not isinstance(c, int) for c in priority)
            or sorted(priority) != list(range(cameras))):
        raise ValueError("the camera priority must list every camera once")
    return list(priority)


def usable(camera: int, start: float, end: float) -> bool:
    """Whether a camera's shot is worth cutting to from start to end on the
    session's timeline. Always, for now: the place for a check that the
    performer is actually in frame (a model running here, looking at that
    camera's video), so auto_cuts skips an angle they've walked out of."""
    return True


def auto_cuts(total: float, priority: list[int], every: float, length: float,
              usable: Callable[[int, float, float], bool] = usable) -> list[list]:
    """The main camera (priority[0]) throughout, cutting to another camera
    for `length` s every `every` s. Which one: the others share the
    cut-ins by priority, the lowest getting the fewest: weights n, n-1, ...,
    1 for n others, interleaved evenly (smooth weighted round-robin) rather
    than bunched, skipping any camera not `usable` for that stretch (none
    usable: that cut-in is skipped). Nothing in the last few seconds."""
    main, others = priority[0], priority[1:]
    cuts: list[list] = [[0.0, main]]
    if not others or every <= 0 or length <= 0:
        return cuts
    weights = {cam: len(others) - rank for rank, cam in enumerate(others)}
    credit = {cam: 0 for cam in others}
    total_weight = sum(weights.values())
    t = float(every)
    while t + length < total - 5:
        for cam in others:
            credit[cam] += weights[cam]
        candidates = [cam for cam in others if usable(cam, t, t + length)]
        if candidates:
            pick = max(candidates, key=lambda cam: credit[cam])
            credit[pick] -= total_weight
            cuts += [[round(t, 3), pick], [round(t + length, 3), main]]
        t += every
    return cuts


def session_length(directory: Path, data: dict, main: int) -> float:
    """The session's length on its timeline: the main camera's video, less
    its sync shift, in whole frames."""
    cameras = camera_files(directory, data)
    _title, path, shift = cameras[main]
    duration, _w, _h = probe(path)
    return math.floor((duration - shift) * FPS) / FPS


def checked_plan(plan: dict, cameras: int) -> dict:
    """The plan, validated: {"priority": [...], "primary": priority[0],
    "cuts": [[t, camera], ...]} sorted, starting at 0, cameras in range.
    A plan from before priorities ({"primary"}) gets one: the main camera,
    then the rest in order. Raises ValueError."""
    if "priority" in plan:
        priority = checked_priority(plan.get("priority"), cameras)
    else:
        primary = plan.get("primary")
        if not isinstance(primary, int) or isinstance(primary, bool) or not 0 <= primary < cameras:
            raise ValueError("pick the main camera")
        priority = [primary] + [c for c in range(cameras) if c != primary]
    primary = priority[0]
    raw = plan.get("cuts")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_CUTS:
        raise ValueError("the timeline is empty")
    cuts = []
    for item in raw:
        if not (isinstance(item, list) and len(item) == 2):
            raise ValueError("bad cut")
        t, cam = item
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0:
            raise ValueError("bad cut time")
        if isinstance(cam, bool) or not isinstance(cam, int) or not 0 <= cam < cameras:
            raise ValueError("bad cut camera")
        cuts.append([round(float(t), 3), cam])
    cuts.sort(key=lambda c: c[0])
    cuts[0][0] = 0.0
    return {"priority": priority, "primary": primary, "cuts": cuts}


class EditRenderer:
    def __init__(self, vault: Vault, log: Callable[[str], None], available: bool) -> None:
        self.vault = vault
        self.log = log
        self.available = available
        self._lock = threading.Lock()
        self._status: dict[str, dict] = {}  # session -> {"state", "progress", "error"}
        self._wake = threading.Event()
        self._queue: list[str] = []
        if available:
            threading.Thread(target=self._run, name="session-edit", daemon=True).start()

    def status(self, name: str) -> dict:
        with self._lock:
            status = dict(self._status.get(name) or {})
        if not status:
            done = (self.vault.session_dir(name) / OUTPUT_FILE).is_file()
            status = {"state": "done" if done else "none"}
        return status

    def save_and_render(self, name: str, plan: dict) -> dict:
        directory = self.vault.session_dir(name)
        data = self.vault.read_session_log(name) or {}
        plan = checked_plan(plan, len(camera_files(directory, data)))
        tmp = directory / (PLAN_FILE + ".tmp")
        tmp.write_text(json.dumps(plan, indent=2))
        os.replace(tmp, directory / PLAN_FILE)
        if not self.available:
            raise ValueError("this vault can't render (it needs ffmpeg with libx264)")
        with self._lock:
            if self._status.get(name, {}).get("state") not in ("queued", "rendering"):
                self._status[name] = {"state": "queued"}
                self._queue.append(name)
        self._wake.set()
        return self.status(name)

    def _run(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                name = self._queue.pop(0) if self._queue else None
                if not self._queue:
                    self._wake.clear()
            if name is None:
                continue
            try:
                self._render(name)
                with self._lock:
                    self._status.pop(name, None)  # done: edit.mp4 says so
            except Exception as e:  # noqa: BLE001 — never let one session stop the worker
                self.log(f"Rendering {name}'s edit failed: {e}")
                with self._lock:
                    self._status[name] = {"state": "failed", "error": str(e)}

    def _set(self, name: str, **status) -> None:
        with self._lock:
            self._status[name] = status

    def _render(self, name: str) -> None:
        directory = self.vault.session_dir(name)
        data = self.vault.read_session_log(name) or {}
        cameras = camera_files(directory, data)
        plan = checked_plan(json.loads((directory / PLAN_FILE).read_text()), len(cameras))
        _title, main_file, main_shift = cameras[plan["primary"]]
        main_duration, width, height = probe(main_file)
        total = math.floor((main_duration - main_shift) * FPS) / FPS

        # Stretches snapped to whole frames; zero-length ones dropped and
        # runs of one camera merged.
        bounds = [min(total, round(t * FPS) / FPS) for t, _cam in plan["cuts"]] + [total]
        stretches: list[list] = []
        for (start, end), (_t, cam) in zip(zip(bounds, bounds[1:]), plan["cuts"]):
            if end <= start:
                continue
            if stretches and stretches[-1][2] == cam and abs(stretches[-1][1] - start) < 1e-9:
                stretches[-1][1] = end
            else:
                stretches.append([start, end, cam])

        work = directory / ".edit-work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        nice = ["nice", "-n", "10"] if shutil.which("nice") else []
        started = time.monotonic()
        self.log(f"Rendering {name}'s edit: {len(stretches)} stretches, {total:.0f}s")
        try:
            names = []
            for i, (start, end, cam) in enumerate(stretches):
                self._set(name, state="rendering", progress=round(start / total, 3) if total else 0)
                frames = round((end - start) * FPS)
                part = work / f"part{i:05d}.mp4"
                if not self._encode_stretch(nice, cameras[cam][1], start + cameras[cam][2], frames, width, height, part):
                    # That camera has nothing there (it started late or ended
                    # early): the main camera instead, so the timing holds.
                    if not self._encode_stretch(nice, main_file, start + main_shift, frames, width, height, part):
                        raise RuntimeError(f"couldn't render {start:.1f}-{end:.1f}s")
                names.append(part.name)
            (work / "parts.txt").write_text("".join(f"file '{n}'\n" for n in names))
            self._set(name, state="rendering", progress=1.0)
            out = directory / (OUTPUT_FILE + ".partial.mp4")
            # The joined picture, and the main camera's two audio tracks
            # (Mix, Instrument Only), unbroken from session time 0.
            cmd = [*nice, "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "parts.txt",
                   "-ss", f"{main_shift:.6f}", "-i", str(main_file),
                   "-map", "0:v:0", "-map", "1:a:0", "-map", "1:a:1?",
                   "-c:v", "copy", "-c:a:0", "aac", "-b:a:0", "192k", "-c:a:1", "flac",
                   "-metadata:s:a:0", "title=Mix", "-metadata:s:a:1", "title=Instrument Only",
                   "-disposition:a:0", "default", "-disposition:a:1", "0",
                   "-t", f"{total:.6f}", "-movflags", "+faststart", str(out)]
            result = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
            if result.returncode != 0 or not out.exists():
                out.unlink(missing_ok=True)
                raise RuntimeError(f"joining failed: {(result.stderr or '').strip()[-300:]}")
            os.replace(out, directory / OUTPUT_FILE)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self.log(f"Rendered {name}'s edit ({total:.0f}s of video in {time.monotonic() - started:.0f}s)")

    @staticmethod
    def _encode_stretch(nice: list[str], src: Path, seek: float, frames: int, width: int, height: int,
                        out: Path) -> bool:
        """Exactly `frames` frames of `src` from `seek`, at FPS and the main
        camera's size (the last frame held if the source runs out)."""
        vf = (f"fps={FPS},scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,tpad=stop_mode=clone:stop_duration=3600")
        cmd = [*nice, "ffmpeg", "-v", "error", "-y", "-ss", f"{seek:.6f}", "-i", str(src),
               "-map", "0:v:0", "-vf", vf, "-frames:v", str(frames),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-video_track_timescale", "90000", "-an", str(out)]
        result = subprocess.run(cmd, capture_output=True)
        return result.returncode == 0 and out.exists() and out.stat().st_size > 0
