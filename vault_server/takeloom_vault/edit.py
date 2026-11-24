"""A session's edited video: its cameras cut together into one final file.

The session page's timeline editor (web.py) saves a plan, edit.json in the
session's folder:

    {"shares": [1, 1, 2], "audio": "processed", "cuts": [[0.0, 0], [30.0, 2], [60.0, 1], ...]}

"shares" is how much of the time each camera is on screen, relative to the
others (equal by default); no camera is special. "cuts" is a list of (time
on the session's timeline in seconds, camera), each camera shown from its
cut to the next. Cameras are indexes into cameras.session_cameras' list,
resolved here (a plan never names a file). A plan from before shares (a
"priority" list, or a "primary") is read as equal shares.

Rendering makes edit.mp4: each stretch of picture from its camera's own
file (cameras.py places session time in it: a phone's original by its
alignment, or an older session's muxed video by its sync shift), and the
sound unbroken from start to finish, so a cut never breaks it. "audio"
picks the sound: "processed" is the session's own (the mix, plus the
instrument alone as a second track); "atmospheric" is the phones' own mics
together, summed and limited, with the mix kept second. A stretch whose
camera has no picture there (it started late or stopped early) shows
whichever camera does instead. The frame is the largest camera's size.

Stretches are snapped to whole frames (FPS) and each is encoded to exactly
its frame count, so the picture never drifts from the sound however many
cuts there are. edit.mp4 is clean: the footer (footer.py) is only for
YouTube, so publishing renders the same plan again, from the same sources
(one encode, never an encode of edit.mp4), with it drawn in, naming each
stretch's camera, as edit_youtube.mp4 (render_for_youtube).

Each stretch is its own short ffmpeg encode (frame-accurate seek, scaled to
the frame size), then they're joined without re-encoding and the sound
added. One render at a time, on a background thread, at low priority,
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

from . import footer
from .store import Vault

PLAN_FILE = "edit.json"
OUTPUT_FILE = "edit.mp4"
YOUTUBE_FILE = "edit_youtube.mp4"  # edit.mp4 with the footer, made to publish (render_for_youtube)
FPS = 30
MAX_CUTS = 5000
_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Helvetica.ttc")


def can_draw_text() -> bool:
    """ffmpeg here has drawtext (built with libfreetype), for the footer."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return " drawtext " in out


def footer_filters(entries: list[list], start: float, end: float, camera: str, height: int,
                   work: Path, tag: str) -> str:
    """The drawbox/drawtext filters for one stretch [start, end) of the
    session: the footer strip (1/24 of the height, translucent black, as it
    always was) and, over it, each footer text that applies in the stretch
    with the camera's name, timed from the stretch's own start. Texts go
    through files (textfile, no expansion), so nothing in a song title can
    upset the filter; paths are relative to `work`, ffmpeg's working dir."""
    bar = max(28, height // 24)
    size = max(13, bar - 12)
    font = next((f"fontfile={f}" for f in _FONTS if Path(f).exists()), "font=Sans")
    parts = [f"drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black@0.43:t=fill"]
    for j, (at, text) in enumerate(entries):
        until = entries[j + 1][0] if j + 1 < len(entries) else math.inf
        a, b = max(at, start), min(until, end)
        if b <= a:
            continue
        name = f"{tag}_{j}.txt"
        (work / name).write_text(footer.with_camera(text, camera))
        parts.append(
            f"drawtext={font}:textfile={name}:expansion=none:fontsize={size}:fontcolor=white@0.92:"
            f"x=14:y=h-{bar}+({bar}-th)/2:enable='between(t,{a - start:.3f},{b - start:.3f})'"
        )
    return ",".join(parts)


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


def mic_files(directory: Path, data: dict) -> list[tuple[str, Path]]:
    """(camera title, file) of each phone mic track the session has
    (the camera entries' "mic_file", when it's there)."""
    from .web import camera_title
    found = []
    for cam in data.get("extra_cameras") or []:
        name = cam.get("mic_file") or ""
        if name and "/" not in name and not name.startswith(".") and (directory / name).is_file():
            found.append((camera_title(cam.get("label") or "", cam.get("phone_camera") or {}, data), directory / name))
    return found


def usable(camera: int, start: float, end: float) -> bool:
    """Whether a camera's shot is worth cutting to from start to end on the
    session's timeline. Always, for now: the place for a check that the
    performer is actually in frame (a model running here, looking at that
    camera's video), so auto_cuts skips an angle they've walked out of."""
    return True


def auto_cuts(total: float, shares: list[float], every: float,
              usable: Callable[[int, float, float], bool] = usable) -> list[list]:
    """A cut every `every` s, each stretch going to a camera by its share
    of the time (smooth weighted round-robin: interleaved evenly, never
    bunched), skipping any camera not `usable` for that stretch; runs of one
    camera are merged. No camera is a base the others cut away from."""
    cameras = [cam for cam, share in enumerate(shares) if share > 0] or list(range(len(shares)))
    if not cameras:
        return [[0.0, 0]]
    weights = {cam: max(shares[cam], 0.0) or 1.0 for cam in cameras}
    total_weight = sum(weights.values())
    credit = {cam: 0.0 for cam in cameras}
    step = every if every > 0 else max(total, 1.0)
    cuts: list[list] = []
    t = 0.0
    while t < total or not cuts:
        for cam in cameras:
            credit[cam] += weights[cam]
        candidates = [cam for cam in cameras if usable(cam, t, min(t + step, total))] or cameras
        pick = max(candidates, key=lambda cam: credit[cam])
        credit[pick] -= total_weight
        if not cuts or cuts[-1][1] != pick:
            cuts.append([round(t, 3), pick])
        t += step
    return cuts


def checked_shares(shares, cameras: int) -> list[float]:
    """A non-negative share per camera. Raises ValueError."""
    if (not isinstance(shares, list) or len(shares) != cameras
            or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0
                   for x in shares)):
        raise ValueError("give every camera a share of the time (0 or more)")
    return [float(x) for x in shares]


def checked_plan(plan: dict, cameras: int) -> dict:
    """The plan, validated: {"shares": [...], "audio", "cuts": [[t, camera],
    ...]} sorted, starting at 0, cameras in range. A plan from before shares
    gets equal ones. Raises ValueError."""
    if "shares" in plan:
        shares = checked_shares(plan.get("shares"), cameras)
    else:
        shares = [1.0] * cameras
    audio = plan.get("audio") or "processed"
    if audio == "acoustic":
        audio = "atmospheric"  # its first name
    if audio not in ("processed", "atmospheric"):
        raise ValueError("audio must be processed or atmospheric")
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
    return {"shares": shares, "audio": audio, "cuts": cuts}


class EditRenderer:
    def __init__(self, vault: Vault, log: Callable[[str], None], available: bool) -> None:
        self.vault = vault
        self.log = log
        self.available = available
        self.draws_text = available and can_draw_text()
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
        from .cameras import session_cameras
        plan = checked_plan(plan, len(session_cameras(directory, data)))
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

    def render_for_youtube(self, name: str, on_progress: Callable[[float], None]) -> Path:
        """The edit with the footer drawn in (see the module docstring), as
        YOUTUBE_FILE, rendered now on the calling thread. Just edit.mp4 for a
        session whose videos carry their own footer already, or if this
        ffmpeg can't draw text."""
        directory = self.vault.session_dir(name)
        data = self.vault.read_session_log(name) or {}
        if not footer.wanted(data):
            return directory / OUTPUT_FILE
        if not self.draws_text:
            self.log(f"{name}'s YouTube video: no footer (this ffmpeg has no drawtext)")
            return directory / OUTPUT_FILE
        self._render(name, with_footer=True, output=YOUTUBE_FILE, on_progress=on_progress)
        return directory / YOUTUBE_FILE

    def _render(self, name: str, with_footer: bool = False, output: str = OUTPUT_FILE,
                on_progress: Callable[[float], None] | None = None) -> None:
        progress = on_progress or (lambda fraction: self._set(name, state="rendering", progress=fraction))
        directory = self.vault.session_dir(name)
        data = self.vault.read_session_log(name) or {}
        from .cameras import is_sources, mic_input_args, session_audio, session_cameras, session_length
        cameras = session_cameras(directory, data)
        plan = checked_plan(json.loads((directory / PLAN_FILE).read_text()), len(cameras))
        # The frame: the largest camera's.
        sizes = [probe(cam.path) for cam in cameras]
        width, height = max(((w, h) for _d, w, h in sizes), key=lambda wh: wh[0] * wh[1])
        total = math.floor(session_length(directory, data, cameras) * FPS) / FPS
        # A stretch whose camera has no picture there: the others, most
        # screen time first.
        fallback = sorted(range(len(cameras)), key=lambda c: -plan["shares"][c])

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

        work = directory / (".edit-work-youtube" if with_footer else ".edit-work")
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        entries = footer.schedule(data) if with_footer and footer.wanted(data) and self.draws_text else None
        nice = ["nice", "-n", "10"] if shutil.which("nice") else []
        started = time.monotonic()
        what = "YouTube video (with the footer)" if entries is not None else "edit"
        self.log(f"Rendering {name}'s {what}: {len(stretches)} stretches, {total:.0f}s")
        try:
            names = []
            for i, (start, end, cam) in enumerate(stretches):
                progress(round(start / total, 3) if total else 0)
                frames = round((end - start) * FPS)
                part = work / f"part{i:05d}.mp4"

                def overlay(camera: int) -> str:
                    if entries is None:
                        return ""
                    return footer_filters(entries, start, end, cameras[camera].title, height, work, f"t{i:05d}")

                for candidate in [cam] + [c for c in fallback if c != cam]:
                    seek = cameras[candidate].file_time(start)
                    if seek >= 0 and self._encode_stretch(nice, cameras[candidate].path, seek, frames, width, height,
                                                          part, overlay(candidate), work):
                        break
                else:
                    # No camera has started yet (the very start of a session):
                    # this one, black until it does.
                    seek = cameras[cam].file_time(start)
                    if not self._encode_stretch(nice, cameras[cam].path, seek, frames, width, height, part,
                                                overlay(cam), work):
                        raise RuntimeError(f"no camera has picture at {start:.1f}-{end:.1f}s")
                names.append(part.name)
            (work / "parts.txt").write_text("".join(f"file '{n}'\n" for n in names))
            progress(1.0)
            out = directory / (output + ".partial.mp4")
            cmd = [*nice, "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "parts.txt"]
            if is_sources(data):
                audio = session_audio(directory, data)
                if audio["mix"] is None:
                    raise RuntimeError("this session has no mix reference (session_mix.m4a)")
                cmd += ["-i", str(audio["mix"])]
                mics = [c for c in cameras if c.has_mic] if plan["audio"] == "atmospheric" else []
                if plan["audio"] == "atmospheric" and not mics:
                    raise RuntimeError("this session has no phone recordings for atmospheric audio")
                if mics:
                    chains = []
                    for k, mic in enumerate(mics):
                        args, chain = mic_input_args(mic, total)
                        cmd += args
                        chains.append(f"[{2 + k}:a]{chain}[m{k}]")
                    mixed = "".join(f"[m{k}]" for k in range(len(mics)))
                    cmd += ["-filter_complex",
                            ";".join(chains) + f";{mixed}amix=inputs={len(mics)}:normalize=0,alimiter=limit=0.95[ac]",
                            "-map", "0:v:0", "-map", "[ac]", "-map", "1:a:0",
                            "-c:v", "copy", "-c:a:0", "aac", "-b:a:0", "192k", "-c:a:1", "aac", "-b:a:1", "192k",
                            "-metadata:s:a:0", "title=Atmospheric", "-metadata:s:a:1", "title=Mix"]
                else:
                    if audio["instrument"] is not None:
                        cmd += ["-ss", f"{audio['instrument_offset_s']:.6f}", "-i", str(audio["instrument"])]
                    cmd += ["-map", "0:v:0", "-map", "1:a:0"] + (["-map", "2:a:0"] if audio["instrument"] else [])
                    cmd += ["-c:v", "copy", "-c:a:0", "aac", "-b:a:0", "192k", "-c:a:1", "libmp3lame", "-q:a:1", "2",
                            "-metadata:s:a:0", "title=Mix", "-metadata:s:a:1", "title=Instrument Only"]
            else:
                # An older session: every camera video carries the same two
                # tracks (Mix, Instrument Only); the first camera's will do.
                sound = cameras[0]
                cmd += ["-ss", f"{sound.shift_s:.6f}", "-i", str(sound.path)]
                mics = mic_files(directory, data) if plan["audio"] == "atmospheric" else []
                if plan["audio"] == "atmospheric" and not mics:
                    raise RuntimeError("this session has no phone mic tracks for atmospheric audio")
                if mics:
                    for _title, path in mics:
                        cmd += ["-i", str(path)]
                    inputs = "".join(f"[{2 + i}:a]" for i in range(len(mics)))
                    cmd += ["-filter_complex", f"{inputs}amix=inputs={len(mics)}:normalize=0,alimiter=limit=0.95[ac]",
                            "-map", "0:v:0", "-map", "[ac]", "-map", "1:a:0",
                            "-c:v", "copy", "-c:a:0", "aac", "-b:a:0", "192k", "-c:a:1", "aac", "-b:a:1", "192k",
                            "-metadata:s:a:0", "title=Atmospheric", "-metadata:s:a:1", "title=Mix"]
                else:
                    cmd += ["-map", "0:v:0", "-map", "1:a:0", "-map", "1:a:1?",
                            "-c:v", "copy", "-c:a:0", "aac", "-b:a:0", "192k", "-c:a:1", "libmp3lame", "-q:a:1", "2",
                            "-metadata:s:a:0", "title=Mix", "-metadata:s:a:1", "title=Instrument Only"]
            cmd += ["-disposition:a:0", "default", "-disposition:a:1", "0",
                    "-t", f"{total:.6f}", "-movflags", "+faststart", str(out)]
            result = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
            if result.returncode != 0 or not out.exists():
                out.unlink(missing_ok=True)
                raise RuntimeError(f"joining failed: {(result.stderr or '').strip()[-300:]}")
            os.replace(out, directory / output)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self.log(f"Rendered {name}'s {what} ({total:.0f}s of video in {time.monotonic() - started:.0f}s)")

    @staticmethod
    def _encode_stretch(nice: list[str], src: Path, seek: float, frames: int, width: int, height: int,
                        out: Path, overlay: str = "", work: Path | None = None) -> bool:
        """Exactly `frames` frames of `src` from `seek`, at FPS and the
        edit's frame size (the last frame held if the source runs out), with
        `overlay`'s filters (the footer) on top; run in `work`."""
        lead = max(0.0, -seek)  # starts before the camera did: black until it's there
        vf = (f"fps={FPS},scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
              + (f"tpad=start_duration={lead:.6f}:start_mode=add:color=black," if lead else "")
              + "tpad=stop_mode=clone:stop_duration=3600")
        seek = max(0.0, seek)
        if overlay:
            vf += "," + overlay
        cmd = [*nice, "ffmpeg", "-v", "error", "-y", "-ss", f"{seek:.6f}", "-i", str(src),
               "-map", "0:v:0", "-vf", vf, "-frames:v", str(frames),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-video_track_timescale", "90000", "-an", str(out)]
        result = subprocess.run(cmd, capture_output=True, cwd=work)
        return result.returncode == 0 and out.exists() and out.stat().st_size > 0
