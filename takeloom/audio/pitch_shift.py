"""Small, timing-locked pitch shifting for backing tracks — the QX25's K2
knob "tune the backing track" control (see backend.py's set_backing_pitch).

Only ever asked for tiny shifts — MAX_PITCH_CENTS either way, a quarter-
tone — to correct a backing track that's slightly sharp or flat. That
narrow job has one hard requirement general-purpose pitch shifters fail:
the shifted track must stay sample-locked to the original's timeline,
because every take recorded over it is filed against that timeline.
Rubber Band (via pedalboard.time_stretch) was tried first and measured
drifting up to ±0.19% — a quarter-second over a four-minute song — by an
erratic, uncompensatable amount that changes with every shift setting;
its low-quality mode holds time but leaves anything below ~400 Hz
unshifted.

So this is a splicing shifter instead, which keeps timing by
construction: output frame n plays the source at n + offset, where offset
creeps by (ratio - 1) per frame — pitch exact, since that's just reading
slightly fast or slow — and whenever it would wander past MAX_OFFSET_SECONDS
either way, the read point jumps back (or forward) by about twice that,
at whichever exact distance makes the waveforms line up best, with a
short crossfade. At most ±50 cents that's one splice every ~1.4 s, far
less often for a gentler correction; the timeline never drifts more than
MAX_OFFSET_SECONDS from the original, and never accumulates.

Runs per audio block from AudioEngine's callback (through Mixer), and
offline over a whole file for Completed Takes playback (render())."""

from __future__ import annotations

import math

import numpy as np

MAX_PITCH_CENTS = 50.0
MAX_OFFSET_SECONDS = 0.020
CROSSFADE_SECONDS = 0.012


def _read(data: np.ndarray, positions: np.ndarray) -> np.ndarray:
    """`data` (frames, channels) sampled at fractional `positions` with
    4-point cubic Hermite interpolation; silence outside the array."""
    n = len(data)
    i0 = np.floor(positions).astype(np.int64)
    frac = (positions - i0).astype(np.float32)[:, None]

    def tap(k: int) -> np.ndarray:
        idx = i0 + k
        valid = (idx >= 0) & (idx < n)
        out = data[np.clip(idx, 0, max(0, n - 1))]
        if not valid.all():
            out = out * valid[:, None]
        return out

    xm1, x0, x1, x2 = tap(-1), tap(0), tap(1), tap(2)
    c1 = 0.5 * (x1 - xm1)
    c2 = xm1 - 2.5 * x0 + 2.0 * x1 - 0.5 * x2
    c3 = 0.5 * (x2 - xm1) + 1.5 * (x0 - x1)
    return (((c3 * frac + c2) * frac + c1) * frac + x0).astype(np.float32)


def cents_to_ratio(cents: float) -> float:
    return 2.0 ** (cents / 1200.0)


class SplicePitchShifter:
    """Stateful — one per source being shifted. process() must be called
    for consecutive windows to stay seamless; a call at any other position
    (a seek, a restart) resets it, which is equally seamless since a fresh
    shifter starts exactly in sync with the source."""

    def __init__(self, sample_rate: int) -> None:
        self.max_offset = max(1, int(MAX_OFFSET_SECONDS * sample_rate))
        self.crossfade = max(1, int(CROSSFADE_SECONDS * sample_rate))
        self.reset()

    def reset(self) -> None:
        self._offset = 0.0
        self._fade_from: float | None = None  # the old tap's offset, mid-crossfade
        self._fade_done = 0
        self._next_pos: int | None = None

    def process(self, data: np.ndarray, pos: int, frames: int, cents: float) -> np.ndarray:
        """Frames [pos, pos+frames) of `data` (frames, channels), shifted by
        `cents` — which may change from one call to the next (a knob being
        turned), taking effect immediately."""
        if pos != self._next_pos:
            self.reset()
        self._next_pos = pos + frames
        cents = max(-MAX_PITCH_CENTS, min(MAX_PITCH_CENTS, cents))
        if cents == 0.0 and self._offset == 0.0 and self._fade_from is None:
            return _read(data, np.arange(pos, pos + frames, dtype=np.float64))

        step = cents_to_ratio(cents) - 1.0
        out = np.empty((frames, data.shape[1]), dtype=np.float32)
        i = 0
        while i < frames:
            if self._fade_from is None:
                if cents == 0.0:
                    # Back to unshifted: fade straight onto the original
                    # timeline rather than staying a few ms off it.
                    self._start_fade(-self._offset)
                    continue
                room = (math.copysign(self.max_offset, step) - self._offset) / step
                if room <= 0:
                    self._splice(data, pos + i, step)
                    continue
                n = min(frames - i, max(1, math.ceil(room)))
                k = np.arange(n, dtype=np.float64)
                out[i:i + n] = _read(data, pos + i + k + self._offset + step * k)
                self._offset += step * n
            else:
                n = min(frames - i, self.crossfade - self._fade_done)
                k = np.arange(n, dtype=np.float64)
                base = pos + i + k
                new = _read(data, base + self._offset + step * k)
                old = _read(data, base + self._fade_from + step * k)
                w = ((self._fade_done + k + 1) / self.crossfade).astype(np.float32)[:, None]
                out[i:i + n] = old + (new - old) * w
                self._offset += step * n
                self._fade_from += step * n
                self._fade_done += n
                if self._fade_done >= self.crossfade:
                    self._fade_from = None
            i += n
        return out

    def _start_fade(self, jump: float) -> None:
        self._fade_from = self._offset
        self._offset += jump
        self._fade_done = 0

    def _splice(self, data: np.ndarray, abs_pos: int, step: float) -> None:
        """Jump the read point about 2×max_offset back (reading fast) or
        forward (reading slow), at the exact distance whose waveform best
        matches what's playing now, then crossfade onto it."""
        lo, hi = int(1.5 * self.max_offset), int(2.5 * self.max_offset)
        direction = -1 if step > 0 else 1
        q = int(round(abs_pos + self._offset))
        c = self.crossfade
        n = len(data)
        jump = 2 * self.max_offset
        if direction < 0:
            region_start, region_end = q - hi, q - lo + c
        else:
            region_start, region_end = q + lo, q + hi + c
        if region_start >= 0 and region_end <= n and q + c <= n:
            mono = data.mean(axis=1) if data.ndim == 2 else data
            current = mono[q:q + c].astype(np.float64)
            region = mono[region_start:region_end].astype(np.float64)
            if np.any(current):
                corr = np.correlate(region, current, mode="valid")
                sq = np.concatenate([[0.0], np.cumsum(region * region)])
                energy = sq[c:] - sq[:-c]
                t = int(np.argmax(corr / np.sqrt(energy + 1e-12)))
                jump = (hi - t) if direction < 0 else (lo + t)
        self._start_fade(direction * jump)


def render(data: np.ndarray, sample_rate: int, cents: float, block: int = 65536) -> np.ndarray:
    """The whole of `data` shifted by `cents`, offline — what a session's
    live playback of the same track sounds like with the knob left there."""
    if cents == 0.0:
        return data
    shifter = SplicePitchShifter(sample_rate)
    return np.concatenate(
        [shifter.process(data, pos, min(block, len(data) - pos), cents) for pos in range(0, len(data), block)]
    )
