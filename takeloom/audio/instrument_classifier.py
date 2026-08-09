"""Best-effort realtime instrument classifier: guesses which configured
Instrument is currently playing from the frequency content of the live
input signal, so the Record tab can show a "here's what we think you're
playing" indicator — see ui/record.py's detected_instrument_var. This is
a development/testing aid to calibrate against real playing by ear, not
a finished production feature; it may go away once/if the underlying
detection quality is good enough to just trust silently.

Deliberately dependency-free (numpy only, no scipy/librosa/aubio) so it
imposes no new requirement — same constraint audio/filters.py already
follows for the same reason (this and the compressor both run in or near
the realtime sd.Stream callback).

The heuristic: each Instrument has an expected fundamental-frequency
range (StudioConfig's freq_min_hz/freq_max_hz, or a name-keyword default
below if unset). Every ~1 second of captured audio, an FFT is taken and
scored against every configured instrument's range by what fraction of
the spectrum's total energy falls inside that band; the highest-scoring
instrument wins. This distinguishes instruments whose fundamentals occupy
clearly different registers (e.g. bass vs. guitar) reasonably well; it
will *not* reliably distinguish two instruments with overlapping ranges
(e.g. two different electric guitars) — that would need real timbral
analysis, not just a frequency band.
"""

from __future__ import annotations

import threading
from typing import Callable

import numpy as np

# (keyword to match against Instrument.name/full_name, (low_hz, high_hz))
# checked in order — first match wins. Rough fundamental-frequency
# registers, not scientific: enough to tell "clearly low" (bass) from
# "clearly high" (vocals/keys) apart, which is the common real mix-up.
_DEFAULT_RANGES_BY_KEYWORD: list[tuple[str, tuple[float, float]]] = [
    ("bass", (40.0, 400.0)),
    ("kick", (40.0, 200.0)),
    ("drum", (60.0, 5000.0)),
    ("guitar", (80.0, 1200.0)),
    ("vox", (100.0, 1100.0)),
    ("vocal", (100.0, 1100.0)),
    ("sing", (100.0, 1100.0)),
    ("piano", (27.0, 4200.0)),
    ("keys", (27.0, 4200.0)),
    ("synth", (27.0, 4200.0)),
]
_FALLBACK_RANGE = (60.0, 2000.0)

# Below this peak amplitude a block is treated as silence/noise floor and
# excluded from analysis entirely — otherwise room noise between notes
# would constantly drag the classifier toward whatever instrument's range
# happens to overlap low-level hiss.
_SILENCE_THRESHOLD = 0.01

# How much captured audio to accumulate before running one classification
# pass — long enough to average out a single transient/pick attack, short
# enough that the display still feels "live".
_ANALYSIS_WINDOW_SECONDS = 1.0


def effective_frequency_range(instrument) -> tuple[float, float]:
    """The (low_hz, high_hz) range to classify `instrument` against —
    its own configured freq_min_hz/freq_max_hz if both are set, otherwise
    a name-keyword default (see _DEFAULT_RANGES_BY_KEYWORD), otherwise
    _FALLBACK_RANGE."""
    if instrument.freq_min_hz > 0.0 and instrument.freq_max_hz > instrument.freq_min_hz:
        return (instrument.freq_min_hz, instrument.freq_max_hz)
    haystack = f"{instrument.name} {instrument.full_name}".lower()
    for keyword, rng in _DEFAULT_RANGES_BY_KEYWORD:
        if keyword in haystack:
            return rng
    return _FALLBACK_RANGE


class InstrumentClassifier:
    """Feed captured mono input blocks in via process_block() (safe to call
    from the realtime audio callback thread — see AudioEngine._callback's
    set_instrument_sink hook); on_detected(name, confidence) fires from a
    background thread whenever the best-guess instrument changes."""

    def __init__(
        self,
        sample_rate: int,
        instruments: list,
        on_detected: Callable[[str, float], None],
    ) -> None:
        self._sample_rate = sample_rate
        self._ranges = [(inst.name, effective_frequency_range(inst)) for inst in instruments]
        self._on_detected = on_detected
        self._window_samples = int(sample_rate * _ANALYSIS_WINDOW_SECONDS)
        self._buffer: list[np.ndarray] = []
        self._buffered_samples = 0
        self._last_emitted: str | None = None

    def process_block(self, mono: np.ndarray) -> None:
        """Realtime-safe: a peak check, an array copy/append, and a sample
        counter — the actual FFT happens off-thread in _classify() once a
        full window is ready, same "spin a thread, don't block the
        callback" pattern AudioEngine._callback already uses for
        on_song_end (see engine.py)."""
        if not self._ranges:
            return
        block = mono[:, 0] if mono.ndim > 1 else mono
        if float(np.max(np.abs(block))) < _SILENCE_THRESHOLD:
            return
        self._buffer.append(block.copy())
        self._buffered_samples += len(block)
        if self._buffered_samples < self._window_samples:
            return
        snapshot = self._buffer
        self._buffer = []
        self._buffered_samples = 0
        threading.Thread(target=self._classify, args=(snapshot,), daemon=True).start()

    def _classify(self, blocks: list[np.ndarray]) -> None:
        samples = np.concatenate(blocks).astype(np.float64)
        if len(samples) < 2:
            return
        windowed = samples * np.hanning(len(samples))
        spectrum = np.abs(np.fft.rfft(windowed))
        total_energy = float(spectrum.sum())
        if total_energy <= 0.0:
            return
        freqs = np.fft.rfftfreq(len(samples), d=1.0 / self._sample_rate)

        best_name, best_score = None, -1.0
        for name, (low_hz, high_hz) in self._ranges:
            mask = (freqs >= low_hz) & (freqs <= high_hz)
            score = float(spectrum[mask].sum()) / total_energy
            if score > best_score:
                best_name, best_score = name, score

        if best_name is not None and best_name != self._last_emitted:
            self._last_emitted = best_name
            self._on_detected(best_name, best_score)
