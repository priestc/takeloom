"""Sync clicks: short chirps played through the speakers so the phones'
mics always hear something to line their video up with.

Takeloom Camera videos are lined up by matching each phone's mic against
the session's audio (phone_camera/sync.py). When nothing in the room makes
a sound (a MIDI keyboard or a DI'd bass heard only in headphones) there's
nothing to match, and the video falls back to clock sync: tens of ms off,
with the phones' clock drift uncorrected. With the speaker/headphone split
(engine.py's speaker_channels), the speakers carry only the instrument,
and AudioEngine adds one of these whenever they've been quiet a while.
Each click's position on the session's timeline is logged
("sync_clicks" in session_log.json), so sync.py rebuilds the exact signal
(reference()) and matches against it. Nothing recorded is altered.

A click is a linear chirp, 200 -> 3300 Hz over 60 ms with a Hann taper:
inside the band sync.py correlates in (100-3500 Hz), with a single sharp
autocorrelation peak. It's defined in continuous time, so render() gives
the same signal at any sample rate (48 kHz to play, 8 kHz to match).
"""

from __future__ import annotations

import numpy as np

VERSION = 1
SECONDS = 0.06
F0, F1 = 200.0, 3300.0
LEVEL = 0.3  # peak, of full scale
# The speakers quiet this long (seconds) gets a click; another follows
# every GAP_S while they stay quiet. The first comes FIRST_S into the session.
GAP_S = 20.0
FIRST_S = 2.0
QUIET_PEAK = 1e-3


def render(sample_rate: int) -> np.ndarray:
    """One click, mono float32, at `sample_rate`."""
    n = int(round(SECONDS * sample_rate))
    t = np.arange(n, dtype=np.float64) / sample_rate
    phase = 2 * np.pi * (F0 * t + (F1 - F0) * t * t / (2 * SECONDS))
    taper = 0.5 - 0.5 * np.cos(2 * np.pi * t / SECONDS)
    return (LEVEL * taper * np.sin(phase)).astype(np.float32)


def reference(times_s: list[float], duration_s: float, sample_rate: int) -> np.ndarray:
    """Silence `duration_s` long with a click at each of `times_s` — what
    the speakers played, for sync.py to match the phones' mics against."""
    out = np.zeros(int(duration_s * sample_rate) + 1, dtype=np.float32)
    click = render(sample_rate)
    for t in times_s:
        start = int(round(t * sample_rate))
        if start < 0 or start >= len(out):
            continue
        end = min(len(out), start + len(click))
        out[start:end] += click[:end - start]
    return out


class ClickScheduler:
    """Decides, block by block on the audio thread, when to start a click
    (see the module docstring). No allocation per block beyond slicing."""

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.click = render(sample_rate)
        self.gap = int(GAP_S * sample_rate)
        self.reset()

    def reset(self) -> None:
        self.quiet = self.gap - int(FIRST_S * self.sample_rate)
        self.playing = -1  # next sample of the click being played; -1: none
        self.frames: list[int] = []  # session-timeline frame of each click's first sample

    def process(self, speaker: np.ndarray, block_start_frame: int) -> np.ndarray | None:
        """`speaker`: this block's speaker feed (frames, 1), before any
        click. Returns the click samples to add to it (same shape), or None."""
        frames = len(speaker)
        if self.playing < 0:
            if float(np.max(np.abs(speaker))) > QUIET_PEAK:
                self.quiet = 0
                return None
            self.quiet += frames
            if self.quiet < self.gap:
                return None
            self.quiet = 0
            self.playing = 0
            self.frames.append(block_start_frame)
        out = np.zeros((frames, 1), dtype=np.float32)
        n = min(frames, len(self.click) - self.playing)
        out[:n, 0] = self.click[self.playing:self.playing + n]
        self.playing += n
        if self.playing >= len(self.click):
            self.playing = -1
        return out
