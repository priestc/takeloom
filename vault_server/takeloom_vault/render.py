"""Session video encodes that takeloom left for this machine to make.

With takeloom's "Encode session videos on: Vault server" setting, the
studio machine processes a session (files its takes) but doesn't encode the
session's videos: it uploads their inputs (raw videos, audio, watermark
PNGs) with a render_jobs.json describing each encode, and this makes them
here, on what's assumed to be the more capable machine. The format is
takeloom/video/render_jobs.py's: keep the two in step.

    {"version": 1,
     "jobs": [{"output": "session_video.mp4",
               "spec": {"video", "mix", "instrument", "output": file names,
                        "video_delay_s", "audio_delay_s": seconds,
                        "watermarks": [{"png", "start", "end"}]},
               "done": false}],
     "cleanup": ["session_video_raw.mp4", ...],
     "error": "..."}

A job is a description, never a command line: the ffmpeg command is built
here (mux_args, a copy of takeloom/video/capture.py's), and every name in
it must be a plain file name in the session's own directory (no paths, no
leading "." or "-"), so a job file can't make this run anything else or
read or write anything outside that session.

Each job writes its spec's output (OUTPUT + ".encoding.mp4"), renamed to
OUTPUT once ffmpeg succeeds and the job marked done, so a restart picks up
where it left off. When every job is done the cleanup files and
render_jobs.json are deleted. A failure is recorded in "error", the inputs
are kept, and the session isn't retried until the server restarts.

One session at a time, on one background thread, with ffmpeg at low
priority (nice) so the web UI and uploads stay responsive. Queued when a
session's session_log.json arrives (the last file of an upload), and at
startup for any session still pending.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from .store import Vault

JOBS_FILE = "render_jobs.json"
ENCODING_SUFFIX = ".encoding.mp4"
# Letters (any language: takeloom's camera file names keep non-ASCII
# letters), digits, and a few separators; never a path or an option.
_NAME = re.compile(r"\w[\w .+()'-]*")


class JobError(Exception):
    pass


def can_encode() -> bool:
    """ffmpeg is here, with the libx264 encoder the jobs use."""
    if shutil.which("ffmpeg") is None:
        return False
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "libx264" in out


def _file_name(value) -> str:
    """A plain file name in the session directory, or JobError."""
    if not isinstance(value, str) or not _NAME.fullmatch(value) or ".." in value or len(value) > 200:
        raise JobError(f"not a plain file name: {value!r}")
    return value


def _seconds(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value < 1e6:
        raise JobError(f"not a time in seconds: {value!r}")
    return float(value)


def checked_spec(job: dict) -> dict:
    """The job's spec with every field checked, or JobError."""
    spec = job.get("spec")
    if not isinstance(spec, dict):
        raise JobError("no spec")
    output = _file_name(job.get("output"))
    checked = {
        "video": _file_name(spec.get("video")),
        "mix": _file_name(spec.get("mix")),
        "instrument": _file_name(spec.get("instrument")),
        "output": _file_name(spec.get("output")),
        "video_delay_s": _seconds(spec.get("video_delay_s", 0)),
        "audio_delay_s": _seconds(spec.get("audio_delay_s", 0)),
        "watermarks": [],
    }
    if checked["output"] != output + ENCODING_SUFFIX:
        raise JobError(f"spec writes {checked['output']!r}, not {output + ENCODING_SUFFIX!r}")
    marks = spec.get("watermarks") or []
    if not isinstance(marks, list) or len(marks) > 500:
        raise JobError("bad watermarks")
    for wm in marks:
        if not isinstance(wm, dict):
            raise JobError("bad watermark")
        end = wm.get("end")
        checked["watermarks"].append({
            "png": _file_name(wm.get("png")),
            "start": _seconds(wm.get("start", 0)),
            "end": _seconds(end) if end is not None else None,
        })
    return checked


def mux_args(spec: dict) -> list[str]:
    """Same as takeloom/video/capture.py's mux_args — keep in step."""
    cmd = ["-y"]
    if spec["video_delay_s"]:
        cmd += ["-itsoffset", f"{spec['video_delay_s']:.6f}"]
    cmd += ["-i", spec["video"]]
    if spec["audio_delay_s"]:
        cmd += ["-itsoffset", f"{spec['audio_delay_s']:.6f}"]
    cmd += ["-i", spec["mix"]]
    if spec["audio_delay_s"]:
        cmd += ["-itsoffset", f"{spec['audio_delay_s']:.6f}"]
    cmd += ["-i", spec["instrument"]]
    watermarks = spec["watermarks"]
    if watermarks:
        chain, label = [], "0:v"
        for i, wm in enumerate(watermarks):
            cmd += ["-i", wm["png"]]
            enable = f"gte(t,{wm['start']:.3f})" + (f"*lt(t,{wm['end']:.3f})" if wm["end"] is not None else "")
            out = "outv" if i == len(watermarks) - 1 else f"wm{i}"
            chain.append(f"[{label}][{3 + i}:v]overlay=0:main_h-overlay_h:enable='{enable}'[{out}]")
            label = out
        cmd += [
            "-filter_complex", ";".join(chain),
            "-map", "[outv]", "-map", "1:a", "-map", "2:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        ]
    else:
        cmd += ["-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "copy"]
    cmd += [
        "-c:a:0", "aac",
        "-c:a:1", "flac",
        "-metadata:s:a:0", "title=Mix",
        "-metadata:s:a:1", "title=Instrument Only",
        "-disposition:a:0", "0",
        "-disposition:a:1", "default",
        "-shortest",
        "-movflags", "+faststart",
        spec["output"],
    ]
    return cmd


class Renderer:
    def __init__(self, vault: Vault, log: Callable[[str], None]) -> None:
        self.vault = vault
        self.log = log
        self.available = can_encode()
        self._queue: queue.Queue[str] = queue.Queue()
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self._current: str | None = None

    def start(self) -> None:
        if not self.available:
            self.log("Session video encoding: not available here (needs ffmpeg with libx264).")
            return
        threading.Thread(target=self._run, name="session-render", daemon=True).start()
        for name in self.vault.session_names():
            if self.pending(name):
                self.enqueue(name)

    def pending(self, name: str) -> bool:
        return (self.vault.session_dir(name) / JOBS_FILE).is_file()

    def status(self, name: str) -> str | None:
        """"encoding", "queued", "failed: ...", "waiting", or None (nothing to do)."""
        path = self.vault.session_dir(name) / JOBS_FILE
        if not path.is_file():
            return None
        with self._lock:
            if self._current == name:
                return "encoding"
            if name in self._queued:
                return "queued"
        try:
            error = json.loads(path.read_text()).get("error")
        except (OSError, ValueError):
            error = "unreadable render_jobs.json"
        return f"failed: {error}" if error else "waiting"

    def enqueue(self, name: str) -> None:
        if not self.available or not self.pending(name):
            return
        with self._lock:
            if name in self._queued or self._current == name:
                return
            self._queued.add(name)
        self._queue.put(name)

    def _run(self) -> None:
        while True:
            name = self._queue.get()
            with self._lock:
                self._queued.discard(name)
                self._current = name
            try:
                self._render(name)
            except Exception as e:  # noqa: BLE001 — never let one session stop the worker
                self.log(f"Encoding {name}'s videos failed: {e}")
            finally:
                with self._lock:
                    self._current = None

    def _render(self, name: str) -> None:
        session_dir = self.vault.session_dir(name)
        path = session_dir / JOBS_FILE
        data = json.loads(path.read_text())
        jobs = data.get("jobs") or []
        try:
            specs = [None if job.get("done") else checked_spec(job) for job in jobs]
            cleanup = [_file_name(f) for f in data.get("cleanup") or []]
        except (JobError, AttributeError) as e:
            data["error"] = f"rejected render_jobs.json: {e}"
            _write(path, data)
            self.log(f"Rejected {name}'s render_jobs.json: {e}")
            return
        self.log(f"Encoding {sum(s is not None for s in specs)} video(s) for session {name}...")
        nice = ["nice", "-n", "10"] if shutil.which("nice") else []
        for job, spec in zip(jobs, specs):
            if spec is None:
                continue
            output = job["output"]
            temp = session_dir / spec["output"]
            result = subprocess.run([*nice, "ffmpeg", "-v", "error", *mux_args(spec)], cwd=session_dir,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if result.returncode != 0 or not temp.exists():
                temp.unlink(missing_ok=True)
                detail = (result.stderr or "").strip().splitlines()[-1:] or ["no output"]
                data["error"] = f"ffmpeg failed making {output}: {detail[0]}"
                _write(path, data)
                self.log(f"Encoding {name}/{output} failed: {detail[0]}")
                return
            os.replace(temp, session_dir / output)
            job["done"] = True
            _write(path, data)
            self.log(f"Encoded {name}/{output}")
        for file in cleanup:
            (session_dir / file).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        self.log(f"Session {name}'s videos are done.")


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)
