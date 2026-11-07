"""Session video encodes left for the vault server to run.

Encoding a session's videos (one per camera, each re-encoded to burn in
the watermark) is the heavy part of processing a session. With
StudioConfig.video_encoding "vault",
processing/splicer.py does everything else on this machine and, instead
of encoding, describes each encode in the session directory as
render_jobs.json, beside their inputs (raw videos, audio, watermark PNGs).
The session uploads as usual, and the vault server makes them on its own
CPU (vault_server/takeloom_vault/render.py, which reads this same format),
then deletes the inputs listed in "cleanup".

A job is a description (video/capture.py's mux_spec: file names in the
session directory, offsets, watermark timings), never a command line: the
vault builds the ffmpeg command itself and only accepts plain file names,
so a job file can't get it to run anything else or touch other files.

    {"version": 1,
     "jobs": [{"output": "session_video.mp4",
               "spec": {...mux_spec, writing "session_video.mp4.encoding.mp4"...},
               "done": false}],
     "cleanup": ["session_video_raw.mp4", "session_mix.flac", ...],
     "error": "..."}   (only after a failed run)

Each job writes OUTPUT + ".encoding.mp4" and is renamed to OUTPUT only once
ffmpeg succeeds, so a half-written video never takes the final name. If the
upload fails, this machine runs the jobs itself (run_locally), so a session
is never left unencoded on a machine that kept the only copy.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

JOBS_FILE = "render_jobs.json"
ENCODING_SUFFIX = ".encoding.mp4"


def encoding_name(output: str) -> str:
    return output + ENCODING_SUFFIX


def write_jobs(session_dir: Path, jobs: list[dict], cleanup: list[str]) -> None:
    from ..utils import atomic_write_text
    data = {"version": 1, "jobs": [{**job, "done": False} for job in jobs], "cleanup": cleanup}
    atomic_write_text(session_dir / JOBS_FILE, json.dumps(data, indent=2))


def pending(session_dir: Path) -> bool:
    return (session_dir / JOBS_FILE).exists()


def run_locally(session_dir: Path) -> bool:
    """Run the jobs here instead (the upload to the vault failed). True if
    every one succeeded; the jobs file and cleanup files are then gone."""
    from ..ffmpeg_bin import FFMPEG
    from ..utils import atomic_write_text
    from .capture import mux_args
    path = session_dir / JOBS_FILE
    data = json.loads(path.read_text())
    for job in data["jobs"]:
        if job.get("done"):
            continue
        result = subprocess.run([FFMPEG, *mux_args(job["spec"])], cwd=session_dir,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        temp = session_dir / encoding_name(job["output"])
        if result.returncode != 0 or not temp.exists():
            temp.unlink(missing_ok=True)
            data["error"] = f"ffmpeg failed making {job['output']}"
            atomic_write_text(path, json.dumps(data, indent=2))
            return False
        temp.replace(session_dir / job["output"])
        job["done"] = True
        atomic_write_text(path, json.dumps(data, indent=2))
    for name in data.get("cleanup", []):
        (session_dir / name).unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    return True
