"""Audio filters applied to the instrument input signal, inside AudioEngine's
real-time callback — see engine.py's use of Compressor. Kept dependency-free
(numpy only, no scipy) as it must run inline with the sd.Stream callback."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class CompressorSettings:
    """Feed-forward dynamic-range compressor settings, in the classic
    threshold/ratio/attack/release/makeup-gain shape."""
    enabled: bool = False
    threshold_db: float = -24.0
    ratio: float = 4.0       # 1.0 = no compression, higher = more
    attack_ms: float = 10.0
    release_ms: float = 150.0
    makeup_gain_db: float = 0.0


# Starting points for common instrument sources, not gospel — attack/release
# in particular are genre/performer-dependent. Picked to be reasonable
# defaults a performer can nudge from rather than a "correct" setting:
# faster attack/higher ratio for percussive, transient-heavy sources (drums,
# bass, picked electric), gentler and slower for sustained/dynamic ones
# (acoustic, piano) so the compressor smooths without visibly pumping.
COMPRESSOR_PRESETS: dict[str, CompressorSettings] = {
    "Acoustic Guitar": CompressorSettings(
        enabled=True, threshold_db=-20.0, ratio=3.0, attack_ms=15.0, release_ms=150.0, makeup_gain_db=3.0,
    ),
    "Electric Guitar": CompressorSettings(
        enabled=True, threshold_db=-18.0, ratio=3.0, attack_ms=5.0, release_ms=100.0, makeup_gain_db=2.0,
    ),
    "Bass Guitar": CompressorSettings(
        enabled=True, threshold_db=-22.0, ratio=5.0, attack_ms=20.0, release_ms=200.0, makeup_gain_db=4.0,
    ),
    "Drums": CompressorSettings(
        enabled=True, threshold_db=-16.0, ratio=4.0, attack_ms=8.0, release_ms=120.0, makeup_gain_db=3.0,
    ),
    "Piano": CompressorSettings(
        enabled=True, threshold_db=-20.0, ratio=2.5, attack_ms=20.0, release_ms=250.0, makeup_gain_db=2.0,
    ),
}


def _time_constant_coeff(time_ms: float, sample_rate: int) -> float:
    """One-pole envelope-follower coefficient for a given attack/release time."""
    if time_ms <= 0:
        return 0.0
    return math.exp(-1.0 / (sample_rate * (time_ms / 1000.0)))


class Compressor:
    """Mono feed-forward compressor with a one-pole attack/release envelope
    follower, processed one block at a time inside the audio callback.

    Runs sample-by-sample (the envelope at sample N depends on sample N-1),
    which is inherently sequential — but blocks are small (the configured
    audio buffer_size, at most a couple thousand samples) so this stays well
    within the callback's real-time deadline.
    """

    def __init__(self, sample_rate: int, settings: CompressorSettings | None = None) -> None:
        self.sample_rate = sample_rate
        self.settings = settings or CompressorSettings()
        self._envelope_db = -120.0  # runs continuously across blocks for a smooth envelope

    def process(self, mono: np.ndarray) -> np.ndarray:
        """mono: float32 array of shape (frames, 1). Returns a same-shaped array."""
        s = self.settings
        if not s.enabled:
            return mono

        attack_coeff = _time_constant_coeff(s.attack_ms, self.sample_rate)
        release_coeff = _time_constant_coeff(s.release_ms, self.sample_rate)
        makeup = 10.0 ** (s.makeup_gain_db / 20.0)
        threshold_db = s.threshold_db
        ratio = max(s.ratio, 1.0)

        out = np.empty_like(mono)
        envelope_db = self._envelope_db
        for i in range(mono.shape[0]):
            sample = float(mono[i, 0])
            level_db = 20.0 * math.log10(max(abs(sample), 1e-9))
            coeff = attack_coeff if level_db > envelope_db else release_coeff
            envelope_db = coeff * envelope_db + (1.0 - coeff) * level_db
            if envelope_db > threshold_db:
                gain_db = (threshold_db - envelope_db) * (1.0 - 1.0 / ratio)
            else:
                gain_db = 0.0
            out[i, 0] = sample * (10.0 ** (gain_db / 20.0)) * makeup
        self._envelope_db = envelope_db
        # Makeup gain can push a sample past 0dBFS even after gain reduction —
        # clip rather than let it overflow into the recorded file/monitor mix.
        np.clip(out, -1.0, 1.0, out=out)
        return out


def apply_compressor(data: np.ndarray, sample_rate: int, settings: CompressorSettings) -> np.ndarray:
    """Run every channel of `data` (shape (N, channels)) through a fresh
    Compressor, independently per channel — for offline (whole-file-at-
    once, non-realtime) use, unlike AudioEngine's own per-block streaming
    use of Compressor.process(). Feeding an entire array through one
    process() call is mathematically identical to many small sequential
    calls (same recursive envelope update either way), so this is exact,
    not an approximation.

    A stereo array that started life as a mono take duplicated into two
    identical channels (see mixer.py's Mixer.add_source) still comes out
    with both channels identical, since Compressor.process is a pure
    function of its input and gets the same input on both channels here.

    Used by Mixer.add_source (an "other instrument's take" layered into a
    live session's monitor mix) and backend.py's play_take (a take opened
    from the Sessions/Completed Takes tab) — the two places a previously-
    recorded take actually gets listened to "from within takeloom" per
    that label's own settings, as opposed to the take's file on disk,
    which AudioEngine always writes raw/uncompressed regardless of this.
    Returns `data` unchanged if settings.enabled is False — no reason to
    allocate a copy nobody asked for."""
    if not settings.enabled:
        return data
    out = np.empty_like(data)
    for ch in range(data.shape[1]):
        out[:, ch:ch + 1] = Compressor(sample_rate, settings).process(data[:, ch:ch + 1])
    return out
