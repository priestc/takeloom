"""Lining up a session's camera recordings, here on the vault.

docs/camera-alignment-design.md, stage 2. A phone uploads its own recording
of a session straight into the session's folder here; this lines it up with
the session's audio — everything it needs is already here: the recording,
session.flac, the session_mix.m4a reference, and the sync clicks' times in
the session log — and writes the result into that camera's "sources" in
session_log.json (see takeloom's phone_camera/sources.py for the format).
No network, nothing on the studio machine: it can run mid-session there.

The math is a copy of takeloom's phone_camera/sync.py (measure_alignment
and what it uses) and audio/sync_click.py (render, reference): KEEP IN STEP.
takeloom's tests run both on the same session and require the same result.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Callable

import numpy as np

from .store import Vault

RATE = 8000  # Hz; plenty for timing to a fraction of a ms after interpolation
WINDOW_S = 8.0
SEARCH_S = 1.0  # +/- around the clock estimate
NO_CLOCK_SEARCH_S = 8.0
MAX_WINDOWS = 24
MIN_PEAK_Z = 8.0  # correlation peak height over the noise floor, in robust std devs
INLIER_S = 0.003
MIN_INLIERS = 3
MIN_DRIFT_SPAN_S = 300.0  # shorter than this, a slope estimate is mostly noise
MAX_DRIFT = 300e-6

# audio/sync_click.py's
CLICK_VERSION = 1
CLICK_SECONDS = 0.06
F0, F1 = 200.0, 3300.0
CLICK_LEVEL = 0.3  # peak, of full scale


def _bandlimited_spectrum(x: np.ndarray, n: int) -> np.ndarray:
    spec = np.fft.rfft(x, n)
    freqs = np.fft.rfftfreq(n, 1.0 / RATE)
    spec[(freqs < 100) | (freqs > 3500)] = 0
    return spec


def gcc_phat_lag(segment: np.ndarray, reference: np.ndarray) -> tuple[float, float]:
    """Where `segment` best fits inside `reference` (len(reference) >=
    len(segment)): (offset in samples, sub-sample interpolated; peak height
    in robust standard deviations above the correlation's noise floor)."""
    n = 1 << int(np.ceil(np.log2(len(reference) + len(segment))))
    cross = _bandlimited_spectrum(reference, n) * np.conj(_bandlimited_spectrum(segment, n))
    cross /= np.abs(cross) + 1e-12  # PHAT: whiten, keep only phase
    cc = np.fft.irfft(cross, n)[: len(reference) - len(segment) + 1]
    k = int(np.argmax(cc))
    median = np.median(cc)
    spread = 1.4826 * np.median(np.abs(cc - median)) + 1e-12
    z = float((cc[k] - median) / spread)
    frac = 0.0
    if 0 < k < len(cc) - 1:
        a, b, c = cc[k - 1], cc[k], cc[k + 1]
        denom = a - 2 * b + c
        if denom != 0:
            frac = float(np.clip(0.5 * (a - c) / denom, -0.5, 0.5))
    return k + frac, z


def _window_starts(phone: np.ndarray) -> list[int]:
    """Start samples of the loudest non-overlapping windows, spread out:
    loudness so there's something to correlate, spread so the drift fit
    has span."""
    w = int(WINDOW_S * RATE)
    n_blocks = len(phone) // w
    if n_blocks == 0:
        return []
    rms = np.sqrt(np.mean(phone[: n_blocks * w].reshape(n_blocks, w) ** 2, axis=1))
    loud = [i for i in np.argsort(rms)[::-1] if rms[i] > 1e-4]
    # Best block per tenth of the session first, then the rest by loudness.
    picked: list[int] = []
    for decile in range(10):
        lo, hi = n_blocks * decile / 10, n_blocks * (decile + 1) / 10
        best = next((i for i in loud if lo <= i < hi), None)
        if best is not None:
            picked.append(best)
    for i in loud:
        if len(picked) >= MAX_WINDOWS:
            break
        if i not in picked:
            picked.append(i)
    return sorted(int(i) * w for i in picked[:MAX_WINDOWS])


def measure_alignment(
    phone_audio: np.ndarray, references: list[tuple[str, np.ndarray, float]],
    file_t0_guess_s: float, search_s: float,
) -> dict | None:
    """Fit mix_time = a0 + (1 + drift) * file_time from many windows.

    references: (name, mono RATE audio, mix time of its sample 0). Returns
    {"a0_s", "drift", "inliers", "windows", "residual_ms"} or None if fewer
    than MIN_INLIERS windows agree."""
    w = int(WINDOW_S * RATE)
    m = int(search_s * RATE)
    points: list[tuple[float, float, float]] = []  # (file time, a = mix time - file time, z)
    for start in _window_starts(phone_audio):
        segment = phone_audio[start:start + w]
        file_time = start / RATE
        best = None
        for _name, ref, ref_t0 in references:
            expected = int(round((file_t0_guess_s + file_time - ref_t0) * RATE))
            lo = expected - m
            if lo < 0 or lo + w + 2 * m > len(ref):
                continue
            window = ref[lo:lo + w + 2 * m]
            if np.sqrt(np.mean(window ** 2)) < 1e-5:
                continue
            k, z = gcc_phat_lag(segment, window)
            if z >= MIN_PEAK_Z and (best is None or z > best[2]):
                best = (file_time, ref_t0 + (lo + k) / RATE - file_time, z)
        if best is not None:
            points.append(best)
    if len(points) < MIN_INLIERS:
        return None

    t = np.array([p[0] for p in points])
    a = np.array([p[1] for p in points])
    center = float(np.median(a))
    inliers = np.abs(a - center) < max(INLIER_S, MAX_DRIFT * (t.max() - t.min()))
    drift = 0.0
    a0 = center
    for _ in range(3):
        if inliers.sum() < MIN_INLIERS:
            return None
        ti, ai = t[inliers], a[inliers]
        if ti.max() - ti.min() >= MIN_DRIFT_SPAN_S:
            drift, a0 = np.polyfit(ti, ai, 1)
            drift = float(np.clip(drift, -MAX_DRIFT, MAX_DRIFT))
            a0 = float(np.median(ai - drift * ti))
        else:
            drift, a0 = 0.0, float(np.median(ai))
        inliers = np.abs(a - (a0 + drift * t)) < INLIER_S
    if inliers.sum() < MIN_INLIERS:
        return None
    residual = a[inliers] - (a0 + drift * t[inliers])
    return {
        "a0_s": a0, "drift": drift, "inliers": int(inliers.sum()), "windows": len(points),
        "residual_ms": round(float(np.sqrt(np.mean(residual ** 2))) * 1000, 3),
    }


def _clock_video_start(phone: dict, video_start_pts_ns: int | None, clock_offset_ns: int | None) -> float | None:
    """Mix time (s) of the phone recording's first frame, by clock sync."""
    mix_start = phone.get("mix_start_server_ns")
    if video_start_pts_ns is None or clock_offset_ns is None or mix_start is None:
        return None
    return (video_start_pts_ns + clock_offset_ns - mix_start) / 1e9


def click_render(sample_rate: int) -> np.ndarray:
    """One click, mono float32, at `sample_rate`."""
    n = int(round(CLICK_SECONDS * sample_rate))
    t = np.arange(n, dtype=np.float64) / sample_rate
    phase = 2 * np.pi * (F0 * t + (F1 - F0) * t * t / (2 * CLICK_SECONDS))
    taper = 0.5 - 0.5 * np.cos(2 * np.pi * t / CLICK_SECONDS)
    return (CLICK_LEVEL * taper * np.sin(phase)).astype(np.float32)


def click_reference(times_s: list[float], duration_s: float, sample_rate: int) -> np.ndarray:
    """Silence `duration_s` long with a click at each of `times_s` — what
    the speakers played, for sync.py to match the phones' mics against."""
    out = np.zeros(int(duration_s * sample_rate) + 1, dtype=np.float32)
    click = click_render(sample_rate)
    for t in times_s:
        start = int(round(t * sample_rate))
        if start < 0 or start >= len(out):
            continue
        end = min(len(out), start + len(click))
        out[start:end] += click[:end - start]
    return out



# --- reading files (system ffmpeg; sync.py's _decode_mono / _probe_first_video_pts) ---

def _decode_mono(path: Path, extra_input_args: list[str] | None = None, map_spec: str | None = None,
                 pad_from_zero: bool = False) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", *(extra_input_args or []), "-i", str(path)]
    if map_spec:
        cmd += ["-map", map_spec]
    cmd += ["-vn", "-ac", "1", "-ar", str(RATE)]
    if pad_from_zero:
        cmd += ["-af", "aresample=async=1:first_pts=0"]
    cmd += ["-f", "f32le", "-"]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg couldn't decode {path.name}: {result.stderr.decode(errors='replace')[-300:]}")
    return np.frombuffer(result.stdout, dtype=np.float32)


def _probe_first_video_pts(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
         "-read_intervals", "%+#30", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    times = [float(x) for x in result.stdout.split() if x and x != "N/A"]
    if not times:
        raise RuntimeError(f"no video frames found in {path.name}")
    return min(times)


def click_times(data: dict) -> list[float]:
    """sync.py's _click_times."""
    clicks = data.get("sync_clicks") or {}
    if clicks.get("version") != CLICK_VERSION:
        return []
    sample_rate = data.get("sample_rate") or 48000
    mix_start = data.get("mix_start_frame") or 0
    return [(f - mix_start) / sample_rate for f in clicks.get("frames") or [] if f >= mix_start]


def mix_audio(directory: Path) -> Path | None:
    """The session's mix: the session_mix.m4a reference, or (an older
    session) session_mix.flac."""
    for name in ("session_mix.m4a", "session_mix.flac"):
        if (directory / name).is_file():
            return directory / name
    return None


def measure_recording(recording: Path, meta: dict, phone: dict, directory: Path, data: dict) -> dict | None:
    """sync.py's measure_recording, against this session's own audio here:
    {"method", "file_zero_s", "drift", ...}, or None if neither the audio
    nor the clock could place it."""
    sample_rate = int(data.get("sample_rate") or 48000)
    mix_start_frame = int(data.get("mix_start_frame") or 0)
    video_start_pts = meta.get("video_start_pts_ns") or phone.get("phone_video_start_pts_ns")
    clock_offset = meta.get("clock_offset_ns")
    if clock_offset is None:
        clock_offset = phone.get("clock_offset_ns")
    clock_v = _clock_video_start(phone, video_start_pts, clock_offset)

    first_video_pts = _probe_first_video_pts(recording)
    phone_audio = _decode_mono(recording, ["-copyts"], "0:a:0", pad_from_zero=True)
    references = []
    mix = mix_audio(directory)
    if mix is not None:
        references.append(("mix", _decode_mono(mix), 0.0))
    if (directory / "session.flac").is_file():
        references.append(("instrument", _decode_mono(directory / "session.flac"), -mix_start_frame / sample_rate))
    clicks = click_times(data)
    if clicks and references:
        length = max(len(ref) / RATE + t0 for _name, ref, t0 in references)
        references.append(("clicks", click_reference(clicks, length, RATE), 0.0))

    fit = None
    if references:
        if clock_v is not None:
            fit = measure_alignment(phone_audio, references, clock_v - first_video_pts, SEARCH_S)
        else:
            fit = measure_alignment(phone_audio, references, -first_video_pts, NO_CLOCK_SEARCH_S)
    if fit is not None:
        result = {
            "method": "audio", "file_zero_s": fit["a0_s"], "drift": fit["drift"],
            "drift_ppm": round(fit["drift"] * 1e6, 2),
            "windows_matched": fit["inliers"], "windows_tried": fit["windows"],
            "residual_ms": fit["residual_ms"],
        }
        if clock_v is not None:
            result["clock_offset_ms"] = round(clock_v * 1000, 3)
        return result
    if clock_v is not None:
        return {"method": "clock", "file_zero_s": clock_v - first_video_pts, "drift": 0.0}
    return None


# --- the session log's camera sources ---

_STATE_RANK = {"waiting": 0, "arrived": 1, "failed": 1, "aligned": 2}


def merge_sources(incoming: dict, existing: dict | None) -> dict:
    """The studio machine sends its session_log.json again on every upload
    retry, and that copy doesn't know what this vault has done since (a
    recording arrived here, got lined up, its live feed deleted). Keep the
    further-along state of each camera's phone source, and drop sources
    this vault has already dropped."""
    if not existing:
        return incoming
    old = {e.get("upload_file") or e.get("file"): e for e in existing.get("extra_cameras") or []}
    for entry in incoming.get("extra_cameras") or []:
        before = old.get(entry.get("upload_file") or entry.get("file"))
        if before is None or "sources" not in entry or "sources" not in before:
            continue
        mine = {s.get("file"): s for s in before["sources"]}
        merged = []
        for source in entry["sources"]:
            here = mine.get(source.get("file"))
            if here is None and source.get("kind") == "live" and before.get("sources") is not None \
                    and not any(s.get("kind") == "live" for s in before["sources"]):
                continue  # this vault deleted it (its phone's recording arrived)
            if here is not None and _STATE_RANK.get(here.get("state"), 0) > _STATE_RANK.get(source.get("state"), 0):
                source = here
            merged.append(source)
        entry["sources"] = merged
    return incoming


class Aligner:
    """Lines up every phone recording in a session that's here in full and
    not lined up yet (see the module docstring). One session at a time, on
    its own thread, at low priority. Queued at startup, when a recording's
    upload completes (arrived), and when a session's log arrives."""

    def __init__(self, vault: Vault, log: Callable[[str], None]) -> None:
        self.vault = vault
        self.log = log
        self.lock = threading.Lock()  # around every read-modify-write of a session log
        self._queue: queue.Queue[str] = queue.Queue()
        self._queued: set[str] = set()
        self._qlock = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._run, name="camera-align", daemon=True).start()
        for name in self.vault.session_names():
            data = self.vault.read_session_log(name) or {}
            if self._pending(self.vault.session_dir(name), data):
                self.enqueue(name)

    def enqueue(self, name: str) -> None:
        with self._qlock:
            if name in self._queued:
                return
            self._queued.add(name)
        self._queue.put(name)

    @staticmethod
    def _phone_sources(data: dict):
        for entry in data.get("extra_cameras") or []:
            for source in entry.get("sources") or []:
                if source.get("kind") == "phone":
                    yield entry, source

    def _pending(self, directory: Path, data: dict) -> bool:
        return any(s.get("state") in ("waiting", "arrived") and (directory / s["file"]).is_file()
                   for _e, s in self._phone_sources(data))

    def arrived(self, name: str, file: str) -> None:
        """A phone's recording of session `name` has just arrived in full:
        its live feed goes (it's superseded), and it's queued to line up.
        If the session's log isn't here yet, that happens when it arrives."""
        directory = self.vault.session_dir(name)
        with self.lock:
            data = self.vault.read_session_log(name)
            if data is None:
                return
            changed = False
            for entry in data.get("extra_cameras") or []:
                # A live feed arriving after its phone's own recording (the
                # studio machine's session upload, sent again): not wanted.
                phone = next((s for s in entry.get("sources") or [] if s.get("kind") == "phone"), None)
                if entry.get("file") == file and phone is not None and phone.get("state") != "waiting" \
                        and not any(s.get("file") == file for s in entry.get("sources") or []):
                    (directory / file).unlink(missing_ok=True)
                    self.log(f"Deleted {name}/{file}: the phone's own recording is already here")
            for entry, source in self._phone_sources(data):
                if source.get("file") == file and source.get("state") == "waiting":
                    source["state"] = "arrived"
                    changed = True
                if source.get("file") == file:
                    changed |= self._drop_live(entry, directory)
            if changed:
                self.vault.write_session_log(name, data)
        self.enqueue(name)

    def _drop_live(self, entry: dict, directory: Path) -> bool:
        live = [s for s in entry.get("sources") or [] if s.get("kind") == "live"]
        for source in live:
            name = str(source.get("file") or "")
            if name and "/" not in name and not name.startswith("."):
                (directory / name).unlink(missing_ok=True)
                self.log(f"Deleted {directory.name}/{name}: the phone's own recording is here")
        if live:
            entry["sources"] = [s for s in entry["sources"] if s.get("kind") != "live"]
        return bool(live)

    def _run(self) -> None:
        try:
            os.nice(10)  # this thread (and its ffmpegs) only: Linux nice is per thread
        except (AttributeError, OSError):
            pass
        while True:
            name = self._queue.get()
            with self._qlock:
                self._queued.discard(name)
            try:
                self._align_session(name)
            except Exception as e:  # noqa: BLE001 — never let one session stop the worker
                self.log(f"Lining up {name}'s cameras failed: {type(e).__name__}: {e}")

    def _align_session(self, name: str) -> None:
        directory = self.vault.session_dir(name)
        data = self.vault.read_session_log(name) or {}
        todo = [(e.get("label") or e.get("upload_file"), s["file"]) for e, s in self._phone_sources(data)
                if s.get("state") in ("waiting", "arrived") and (directory / s["file"]).is_file()]
        for label, file in todo:
            recording = directory / file
            meta_path = directory / (Path(file).stem + ".upload.json")
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, ValueError):
                meta = {}
            entry = next(e for e, s in self._phone_sources(data) if s["file"] == file)
            try:
                result = measure_recording(recording, meta, entry.get("phone_camera") or {}, directory, data)
                error = None if result else "no audio match and no clock sync"
            except Exception as e:  # noqa: BLE001
                result, error = None, f"{type(e).__name__}: {e}"
            with self.lock:
                fresh = self.vault.read_session_log(name) or data
                for entry, source in self._phone_sources(fresh):
                    if source.get("file") != file:
                        continue
                    if result:
                        source.update(state="aligned", align=result)
                        source.pop("error", None)
                    else:
                        source.update(state="failed", error=error)
                    self._drop_live(entry, directory)
                self.vault.write_session_log(name, fresh)
                data = fresh
            if result:
                self.log(f"Lined up {name}'s {label} by {result['method']}"
                         + (f" ({result['windows_matched']} matches, drift {result['drift_ppm']} ppm)"
                            if result["method"] == "audio" else ""))
            else:
                self.log(f"Couldn't line up {name}'s {label}: {error}")
