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

Also home to the two single-purpose test tools behind Studio Setup's
per-instrument "Detect"/"Train" buttons (see backend.py's
start_instrument_detect/start_instrument_train): InstrumentDetector
(the single-instrument version of the classifier above, for "does audio
on this channel land in this instrument's own range") and NoteCapture
(autocorrelation-based single-note pitch capture, for "what's the actual
Hz of the note just played" — a different question from classification,
answered by estimate_pitch rather than _energy_in_band).
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


def _energy_in_band(samples: np.ndarray, sample_rate: int, low_hz: float, high_hz: float) -> float:
    """Fraction of `samples`'s spectral energy falling within
    [low_hz, high_hz] — the shared scoring primitive behind both
    InstrumentClassifier (compares every configured instrument's band,
    picks the best match) and InstrumentDetector (checks one instrument's
    own band against a pass/fail threshold)."""
    windowed = samples * np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(windowed))
    total_energy = float(spectrum.sum())
    if total_energy <= 0.0:
        return 0.0
    freqs = np.fft.rfftfreq(len(samples), d=1.0 / sample_rate)
    mask = (freqs >= low_hz) & (freqs <= high_hz)
    return float(spectrum[mask].sum()) / total_energy


# Autocorrelation peak must retain at least this fraction of the signal's
# zero-lag energy to count as "periodic enough to be a note" — below this,
# it's more likely noise/silence/an indistinct pluck than a held pitch.
_PITCH_CONFIDENCE_THRESHOLD = 0.3


def estimate_pitch(samples: np.ndarray, sample_rate: int, fmin: float = 30.0, fmax: float = 2000.0) -> float | None:
    """Best-effort fundamental-frequency estimate for one (assumed
    monophonic) played note, via normalized autocorrelation — used by
    NoteCapture for Studio Setup's "Train" flow ("play your highest/
    lowest note" -> an actual Hz value). Returns None below the silence
    threshold, or when no confident periodicity is found in [fmin, fmax]
    (e.g. nothing was played, or what came through wasn't tonal)."""
    samples = samples.astype(np.float64)
    samples = samples - samples.mean()
    if float(np.max(np.abs(samples))) < _SILENCE_THRESHOLD:
        return None
    corr = np.correlate(samples, samples, mode="full")
    corr = corr[len(corr) // 2:]  # keep zero and positive lags only
    if corr[0] <= 0:
        return None
    min_lag = max(1, int(sample_rate / fmax))
    max_lag = min(int(sample_rate / fmin), len(corr) - 1)
    if min_lag >= max_lag:
        return None
    # The correlation naturally decays from its lag-0 peak just from the
    # waveform's own smoothness, independent of periodicity — for a low
    # note that decay can still be higher at a short lag than the true
    # period's peak (e.g. an 80Hz tone's lag-24 point outranks its actual
    # 600-sample period). Skip past that initial slope to the first local
    # minimum before searching for the real peak, same trick real pitch
    # trackers (e.g. YIN) use.
    search_start = min_lag
    for lag in range(min_lag, max_lag):
        if corr[lag + 1] > corr[lag]:
            search_start = lag
            break
    else:
        search_start = min_lag
    segment = corr[search_start:max_lag + 1]
    peak_offset = int(np.argmax(segment))
    peak_lag = search_start + peak_offset
    if corr[peak_lag] / corr[0] < _PITCH_CONFIDENCE_THRESHOLD:
        return None
    return sample_rate / peak_lag


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
        best_name, best_score = None, -1.0
        for name, (low_hz, high_hz) in self._ranges:
            score = _energy_in_band(samples, self._sample_rate, low_hz, high_hz)
            if score > best_score:
                best_name, best_score = name, score

        if best_name is not None and best_name != self._last_emitted:
            self._last_emitted = best_name
            self._on_detected(best_name, best_score)


class InstrumentDetector:
    """Single-instrument version of InstrumentClassifier for Studio
    Setup's "Detect" test: feed captured blocks via process_block();
    on_detected(confidence) fires once, from a background thread, the
    first time a ~1s window's energy is concentrated enough in
    `freq_range` to call it a match (score >= `threshold`). Does nothing
    further after that — the caller (backend.py's start_instrument_
    detect) tears the whole test down on the first firing."""

    def __init__(
        self, sample_rate: int, freq_range: tuple[float, float],
        on_detected: Callable[[float], None], threshold: float = 0.5,
    ) -> None:
        self._sample_rate = sample_rate
        self._freq_range = freq_range
        self._on_detected = on_detected
        self._threshold = threshold
        self._window_samples = int(sample_rate * _ANALYSIS_WINDOW_SECONDS)
        self._buffer: list[np.ndarray] = []
        self._buffered_samples = 0
        self._done = False

    def process_block(self, mono: np.ndarray) -> None:
        if self._done:
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
        threading.Thread(target=self._check, args=(snapshot,), daemon=True).start()

    def _check(self, blocks: list[np.ndarray]) -> None:
        if self._done:
            return
        samples = np.concatenate(blocks).astype(np.float64)
        if len(samples) < 2:
            return
        score = _energy_in_band(samples, self._sample_rate, *self._freq_range)
        if score >= self._threshold:
            self._done = True
            self._on_detected(score)


class NoteCapture:
    """Captures one played note over a fixed duration and estimates its
    fundamental frequency — the "play your highest/lowest note" step of
    Studio Setup's "Train" flow. on_captured(pitch_hz) fires once, from a
    background thread, after duration_seconds of audio has been fed in
    via process_block(); pitch_hz is None if nothing periodic enough was
    found anywhere in the window (e.g. nothing was played).

    Unlike InstrumentClassifier/InstrumentDetector, this doesn't silence-
    gate individual blocks — the window has to fill up regardless of
    when in it the performer actually starts playing, so silence at the
    start just means fewer valid per-chunk pitch estimates once analysis
    runs, not blocks that never counted toward the window at all."""

    def __init__(self, sample_rate: int, duration_seconds: float, on_captured: Callable[[float | None], None]) -> None:
        self._sample_rate = sample_rate
        self._on_captured = on_captured
        self._window_samples = int(sample_rate * duration_seconds)
        self._buffer: list[np.ndarray] = []
        self._buffered_samples = 0
        self._done = False

    def process_block(self, mono: np.ndarray) -> None:
        if self._done:
            return
        block = mono[:, 0] if mono.ndim > 1 else mono
        self._buffer.append(block.copy())
        self._buffered_samples += len(block)
        if self._buffered_samples < self._window_samples:
            return
        self._done = True
        snapshot = self._buffer
        self._buffer = []
        threading.Thread(target=self._finish, args=(snapshot,), daemon=True).start()

    def _finish(self, blocks: list[np.ndarray]) -> None:
        samples = np.concatenate(blocks).astype(np.float64)
        # Per-chunk estimates (rather than one estimate over the whole
        # window) and take the median — robust against the performer
        # taking a moment to start, natural vibrato, or a brief pick/bow
        # transient skewing a single long autocorrelation.
        chunk_size = max(1, int(self._sample_rate * 0.25))
        estimates = [
            pitch for start in range(0, len(samples) - chunk_size + 1, chunk_size)
            if (pitch := estimate_pitch(samples[start:start + chunk_size], self._sample_rate)) is not None
        ]
        self._on_captured(float(np.median(estimates)) if estimates else None)
