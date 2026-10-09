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


# Onset envelope resolution (frames per hop) used to steer splices away
# from transients — see onset_envelope / SplicePitchShifter._plan_splice.
ONSET_HOP = 256
# Splices are planned at most this far ahead, so a very gentle correction
# (splices minutes apart) doesn't scan minutes of envelope at once.
PLAN_HORIZON_SECONDS = 2.0
# Shortest jump a splice makes, as a fraction of the offset bound — an
# early splice (planned well before the bound) jumps less.
MIN_JUMP_FRACTION = 0.5
# Candidate splice points scoring within this much (onset dB) of the best
# are treated as equally clean, and the latest of them wins.
PLAN_TOLERANCE_DB = 1.0


def onset_envelope(data: np.ndarray) -> np.ndarray:
    """Per-ONSET_HOP transient strength of `data` (frames, channels): the
    rise in log energy of the first difference (high-frequency weighted,
    so drum hits and picked attacks stand out over sustained tone) from
    the previous couple of hops. Whole-file numpy — ~0.1 s for a song,
    so never call it from the audio callback (see SplicePitchShifter.
    prepare)."""
    mono = data.mean(axis=1) if data.ndim == 2 else data
    hp = np.diff(mono, prepend=mono[:1]).astype(np.float32)
    n = len(hp) // ONSET_HOP
    if n < 3:
        return np.zeros(max(n, 1), dtype=np.float32)
    energy = np.sqrt(np.mean(hp[:n * ONSET_HOP].reshape(n, ONSET_HOP) ** 2, axis=1)) + 1e-5
    level = 20.0 * np.log10(energy)
    previous = np.maximum(np.concatenate([[level[0]], level[:-1]]), np.concatenate([level[:2], level[:-2]]))
    return np.maximum(0.0, level - previous).astype(np.float32)


class SplicePitchShifter:
    """Stateful — one per source being shifted. process() must be called
    for consecutive windows to stay seamless; a call at any other position
    (a seek, a restart) resets it, which is equally seamless since a fresh
    shifter starts exactly in sync with the source.

    Where each splice lands matters more than how it's crossfaded: a splice
    re-plays (reading fast) or skips (reading slow) a few tens of ms of
    audio, and if a drum hit falls in that stretch it's heard twice or not
    at all — an audible stutter every splice. So once prepare() has given
    it the track's onset envelope, each splice is *planned*: anywhere from
    when the offset crosses zero to when it would hit the bound, at the
    moment whose re-played/skipped stretch has the weakest transient in
    it, with the jump sized to land back inside the bound. Without an
    envelope (not prepared yet, or the data was swapped) it falls back to
    splicing right at the bound."""

    def __init__(self, sample_rate: int) -> None:
        self.max_offset = max(1, int(MAX_OFFSET_SECONDS * sample_rate))
        self.crossfade = max(1, int(CROSSFADE_SECONDS * sample_rate))
        self.min_jump = max(1, int(MIN_JUMP_FRACTION * self.max_offset))
        self.horizon = int(PLAN_HORIZON_SECONDS * sample_rate)
        self._onsets: np.ndarray | None = None
        self._onsets_for: np.ndarray | None = None
        self.reset()

    def reset(self) -> None:
        self._offset = 0.0
        self._fade_from: float | None = None  # the old tap's offset, mid-crossfade
        self._fade_done = 0
        self._next_pos: int | None = None
        self._plan_at: int | None = None  # output frame of the next planned splice
        self._plan_step = 0.0

    def prepare(self, data: np.ndarray) -> None:
        """Compute `data`'s onset envelope for splice planning. Slow-ish
        (whole file) — call it off the audio thread; process() picks it
        up once it's there, for that exact array only."""
        onsets = onset_envelope(data)
        self._onsets, self._onsets_for = onsets, data

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
        onsets = self._onsets if self._onsets_for is data else None
        out = np.empty((frames, data.shape[1]), dtype=np.float32)
        i = 0
        while i < frames:
            if self._fade_from is None:
                if cents == 0.0:
                    if self._offset == 0.0:
                        out[i:] = _read(data, np.arange(pos + i, pos + frames, dtype=np.float64))
                        break
                    # Back to unshifted: fade straight onto the original
                    # timeline rather than staying a few ms off it.
                    self._start_fade(-self._offset)
                    continue
                abs_pos = pos + i
                room = (math.copysign(self.max_offset, step) - self._offset) / step
                if room <= 0 or (self._plan_at is not None and abs_pos >= self._plan_at):
                    self._splice(data, abs_pos, step)
                    continue
                if self._plan_at is None or step != self._plan_step:
                    self._plan_at = self._plan_splice(onsets, abs_pos, step, room)
                    self._plan_step = step
                n = min(frames - i, max(1, math.ceil(room)))
                if self._plan_at is not None:
                    n = min(n, max(1, self._plan_at - abs_pos))
                k = np.arange(n, dtype=np.float64)
                out[i:i + n] = _read(data, abs_pos + k + self._offset + step * k)
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

    def _plan_splice(self, onsets: np.ndarray | None, abs_pos: int, step: float, room: float) -> int | None:
        """Output frame at which to splice next, or None to splice at the
        bound (no envelope, or the bound is still beyond PLAN_HORIZON —
        replanned on a later block). Candidates run from when the offset
        has crossed zero heading toward the bound (any earlier and the
        jump couldn't land back inside the bound) up to the bound itself,
        every ONSET_HOP frames; each is scored by the strongest onset in
        the stretch of source it would re-play (reading fast) or skip
        (reading slow), crossfade included. Anything within
        PLAN_TOLERANCE_DB of the best counts as a tie, and ties go to the
        latest candidate — a later splice jumps further, so fewer of them."""
        if onsets is None or room > self.horizon:
            return None
        earliest = max(0.0, -self._offset / step)
        if earliest >= room:
            return None
        d = np.arange(math.ceil(earliest), math.floor(room) + 1, ONSET_HOP, dtype=np.int64)
        if len(d) == 0:
            return None
        o = self._offset + step * d
        q = abs_pos + d + o  # source read position at each candidate
        hi = np.abs(o) + self.max_offset  # biggest jump that lands inside the bound
        if step > 0:
            start, end = q - hi, q + self.crossfade
        else:
            start, end = q, q + hi + self.crossfade
        a = np.clip((start // ONSET_HOP).astype(np.int64), 0, len(onsets) - 1)
        b = np.clip((end // ONSET_HOP).astype(np.int64) + 1, 1, len(onsets))
        # Max over each [a, b) — widths are all within a hop or two, so a
        # fixed-width sliding max over the widest is close enough and
        # stays vectorized.
        width = int(np.max(b - a))
        padded = np.concatenate([onsets, np.zeros(width, dtype=onsets.dtype)])
        windows = np.lib.stride_tricks.sliding_window_view(padded, width)
        cost = windows[a].max(axis=1)
        good = np.nonzero(cost <= cost.min() + PLAN_TOLERANCE_DB)[0]
        return abs_pos + int(d[good[-1]])

    def _start_fade(self, jump: float) -> None:
        self._fade_from = self._offset
        self._offset += jump
        self._fade_done = 0
        self._plan_at = None

    def _splice(self, data: np.ndarray, abs_pos: int, step: float) -> None:
        """Jump the read point back (reading fast) or forward (reading
        slow) by however much lands it back inside the bound — at the
        exact distance in that range whose waveform best matches what's
        playing now — then crossfade onto it."""
        direction = -1 if step > 0 else 1
        # How far past zero the offset has drifted toward the bound; the
        # jump must cover at least that, and at most that plus the bound.
        drift = max(0.0, -direction * self._offset)
        lo = max(self.min_jump, int(math.ceil(drift)))
        hi = max(lo + 1, int(drift + self.max_offset))
        q = int(round(abs_pos + self._offset))
        c = self.crossfade
        n = len(data)
        jump = (lo + hi) // 2
        if direction < 0:
            region_start, region_end = q - hi, q - lo + c
        else:
            region_start, region_end = q + lo, q + hi + c
        if region_start >= 0 and region_end <= n and q + c <= n:
            # Mix down only the two slices compared — never the whole
            # file, which on a full song took ~60 ms, blowing the audio
            # callback's budget at every splice (choppy playback).
            def mono(a: np.ndarray) -> np.ndarray:
                return (a.mean(axis=1) if a.ndim == 2 else a).astype(np.float64)
            current = mono(data[q:q + c])
            region = mono(data[region_start:region_end])
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
    shifter.prepare(data)
    return np.concatenate(
        [shifter.process(data, pos, min(block, len(data) - pos), cents) for pos in range(0, len(data), block)]
    )
