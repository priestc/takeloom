"""Lining a Takeloom Camera recording up with the session's audio.

Runs at the start of processing/splicer.py's process_session, for a session
whose session_log.json has a "phone_camera" entry. Its job is to leave
behind what that code already expects from a local camera — a
session_video_raw.mp4 whose video belongs at a known offset from the start
of session_mix.flac — but with that offset measured for this one session
instead of the global, hand-tuned config.video_latency_compensation_ms.
The result goes into the session log as "video_offset_ms" (plus a
"video_sync" record of how it was measured), which process_session uses in
place of the config value.

The phone's uploaded file is used when it arrived (phone_camera.mp4, see
hub.py); otherwise the live feed the server recorded stays, aligned by
clock sync alone.

Alignment of the uploaded file, coarse to fine:

1. Clock sync. The phone pings the server NTP-style and reports its clock
   offset, so its first frame's capture timestamp converts to the server's
   time.monotonic_ns(), which the backend also stamped mix recording's start
   with. Good to a few tens of ms — capture timestamps are exact, but when
   the mix's first sample really left the audio interface isn't.
2. Audio cross-correlation. The phone records its mic into the same file,
   on the same clock as its video. GCC-PHAT between that and the session's
   own audio (the full mix, and the dry instrument — whichever the phone
   actually heard) in a window around the clock estimate pins the file's
   timeline to the mix's to well under a frame.
3. Drift. The phone's clock and the audio interface's sample clock run at
   very slightly different rates (tens of ppm: tens of ms an hour), so
   step 2 runs on many windows across the session and fits a line; its
   slope rescales the video's timestamps (-itsscale, a stream copy, no
   re-encode).

If too few windows agree (a silent session, headphones-only with nothing
acoustic in the room), the clock-sync estimate is used as-is.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from ..ffmpeg_bin import FFMPEG, FFPROBE
from ..utils import atomic_write_text
from .hub import UPLOAD_FILE_NAME, upload_paths

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


def _decode_mono(path: Path, extra_input_args: list[str] | None = None, map_spec: str | None = None,
                 pad_from_zero: bool = False) -> np.ndarray:
    cmd = [FFMPEG, "-v", "error", *(extra_input_args or []), "-i", str(path)]
    if map_spec:
        cmd += ["-map", map_spec]
    cmd += ["-vn", "-ac", "1", "-ar", str(RATE)]
    if pad_from_zero:
        # Sample n <-> file time n/RATE: silence-pad up to the stream's
        # first timestamp rather than starting the output wherever it
        # happens to begin.
        cmd += ["-af", "aresample=async=1:first_pts=0"]
    cmd += ["-f", "f32le", "-"]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg couldn't decode {path.name}: {result.stderr.decode(errors='replace')[-300:]}")
    return np.frombuffer(result.stdout, dtype=np.float32)


def _probe_first_video_pts(path: Path) -> float:
    """The first video frame's presentation time on the file's own raw
    timeline (what -copyts decoding keeps)."""
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
         "-read_intervals", "%+#30", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    times = [float(x) for x in result.stdout.split() if x and x != "N/A"]
    if not times:
        raise RuntimeError(f"no video frames found in {path.name}")
    return min(times)


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


def _normalize_video(src: Path, dest: Path, scale: float) -> bool:
    """Video stream only, timestamps rescaled by `scale` — no re-encode."""
    tmp = dest.with_name(dest.stem + ".normalizing.mp4")
    cmd = [FFMPEG, "-y", "-v", "error"]
    if abs(scale - 1.0) > 1e-9:
        cmd += ["-itsscale", f"{scale:.9f}"]
    cmd += ["-i", str(src), "-map", "0:v:0", "-c", "copy", "-an", str(tmp)]
    if subprocess.run(cmd, capture_output=True).returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    return True


def align_phone_video(session_dir: Path, data: dict, log_path: Path) -> float | None:
    """Make session_video_raw.mp4 the best available phone video and return
    its offset in ms (positive = video starts that long after the mix), also
    saving it into the session log. None if nothing usable."""
    offset_ms = _align_camera(session_dir, data, data.get("phone_camera") or {}, UPLOAD_FILE_NAME,
                              session_dir / "session_video_raw.mp4", data)
    if offset_ms is not None:
        atomic_write_text(log_path, json.dumps(data, indent=2))
    return offset_ms


def align_extra_cameras(session_dir: Path, data: dict, log_path: Path, local_offset_ms: float) -> None:
    """Fill in "video_offset_ms" on each of the session log's
    "extra_cameras" entries (see backend.py's session_cameras): measured,
    for a phone, exactly as align_phone_video does the main camera (its
    raw file replaced by its uploaded recording, when that arrived);
    `local_offset_ms` (the hand-tuned camera offset) for a local camera."""
    for entry in data.get("extra_cameras", []):
        if entry.get("video_offset_ms") is not None:
            continue  # already aligned by an earlier, interrupted run
        phone = entry.get("phone_camera")
        if phone and entry.get("upload_file"):
            entry["video_offset_ms"] = _align_camera(
                session_dir, entry, phone, entry["upload_file"], session_dir / entry["file"], data,
            )
        else:
            entry["video_offset_ms"] = local_offset_ms
    atomic_write_text(log_path, json.dumps(data, indent=2))


def _align_camera(
    session_dir: Path, record: dict, phone: dict, upload_file: str, raw: Path, data: dict,
) -> float | None:
    """Make `raw` the best available video from one phone and return its
    offset in ms, recording how it was measured into `record`
    ("video_offset_ms"/"video_sync"). None if nothing usable."""
    uploaded, _part, meta_path = upload_paths(session_dir, upload_file)
    mix_flac = session_dir / "session_mix.flac"
    session_flac = session_dir / "session.flac"
    sample_rate = data.get("sample_rate") or 48000
    mix_start_frame = data.get("mix_start_frame", 0)

    sync: dict | None = None
    if uploaded.exists() and mix_flac.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            meta = {}
        try:
            sync = _align_uploaded(uploaded, meta, phone, mix_flac, session_flac, sample_rate, mix_start_frame, raw)
        except Exception as e:  # never lose the session over this: fall back to the live feed
            sync = None
            record["video_sync_error"] = str(e)
        if sync is not None:
            uploaded.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)

    if sync is None and "video_sync" in record and record["video_sync"].get("source") == "phone upload":
        return record.get("video_offset_ms")  # already aligned by an earlier, interrupted run

    if sync is None and raw.exists():
        live_start = _clock_video_start(phone, phone.get("first_live_pts_ns"),
                                        phone.get("first_live_clock_offset_ns") or phone.get("clock_offset_ns"))
        if live_start is not None:
            sync = {"source": "live feed", "method": "clock", "offset_ms": round(live_start * 1000, 3)}

    if sync is None:
        return None
    record["video_offset_ms"] = sync["offset_ms"]
    record["video_sync"] = sync
    return sync["offset_ms"]


def _align_uploaded(
    uploaded: Path, meta: dict, phone: dict, mix_flac: Path, session_flac: Path,
    sample_rate: int, mix_start_frame: int, raw: Path,
) -> dict | None:
    video_start_pts = meta.get("video_start_pts_ns") or phone.get("phone_video_start_pts_ns")
    clock_offset = meta.get("clock_offset_ns")
    if clock_offset is None:
        clock_offset = phone.get("clock_offset_ns")
    clock_v = _clock_video_start(phone, video_start_pts, clock_offset)

    first_video_pts = _probe_first_video_pts(uploaded)
    phone_audio = _decode_mono(uploaded, ["-copyts"], "0:a:0", pad_from_zero=True)
    references = [("mix", _decode_mono(mix_flac), 0.0)]
    if session_flac.exists():
        # session.flac runs on its own timeline; the mix started
        # mix_start_frame samples into it.
        references.append(("instrument", _decode_mono(session_flac), -mix_start_frame / sample_rate))

    if clock_v is not None:
        fit = measure_alignment(phone_audio, references, clock_v - first_video_pts, SEARCH_S)
    else:
        fit = measure_alignment(phone_audio, references, -first_video_pts, NO_CLOCK_SEARCH_S)

    if fit is not None:
        scale = 1.0 + fit["drift"]
        video_start = fit["a0_s"] + first_video_pts * scale
        result = {
            "source": "phone upload", "method": "audio",
            "offset_ms": round(video_start * 1000, 3),
            "drift_ppm": round(fit["drift"] * 1e6, 2),
            "windows_matched": fit["inliers"], "windows_tried": fit["windows"],
            "residual_ms": fit["residual_ms"],
        }
        if clock_v is not None:
            result["clock_offset_ms"] = round(clock_v * 1000, 3)
    elif clock_v is not None:
        scale = 1.0
        result = {"source": "phone upload", "method": "clock", "offset_ms": round(clock_v * 1000, 3)}
    else:
        return None
    if not _normalize_video(uploaded, raw, scale):
        return None
    return result
