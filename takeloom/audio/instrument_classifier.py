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
range (StudioConfig's freq_min_hz/freq_max_hz, or a default keyed off
its label — see _DEFAULT_RANGES_BY_LABEL — if unset). Every ~1 second
of captured audio, an FFT is taken and
scored against every configured instrument's range by what fraction of
the spectrum's total energy falls inside that band; the highest-scoring
instrument wins. This distinguishes instruments whose fundamentals occupy
clearly different registers (e.g. bass vs. guitar) reasonably well; it
will *not* reliably distinguish two instruments with overlapping ranges
(e.g. two different electric guitars) — that would need real timbral
analysis, not just a frequency band.

Also home to NoteCapture — autocorrelation-based single-note pitch
capture behind Studio Setup's per-instrument "Train" button, for "what's
the actual Hz of the note just played" — answered by estimate_pitch
rather than _energy_in_band.

backend.py's start_detect_all (behind the standalone `takeloom detect-
test` window, ui/detect_test.py) also uses InstrumentClassifier, but one
instance per physical input channel rather than one global instance: a
channel with only one instrument assigned to it needs no classification
at all (any signal on it must be that instrument), and a channel shared
by several instruments (e.g. bass and electric guitar through the same
DI) gets a classifier scoped to just those instruments — narrower, and
much less prone to this module's overlapping-range weakness, than
comparing against every configured instrument regardless of which
channel is actually shared.

Also home to SpectralStatsTracker — a second, independent per-channel
listener start_detect_all runs alongside InstrumentClassifier, behind
detect-test's "Currently Detected" stats panel (min/max frequency heard,
and a rough polyphony count from distinct spectral peaks). Unrelated to
which instrument (if any) has been identified; see analyze_spectrum for
the peak-picking heuristic and its honest limitations.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import INSTRUMENT_LABELS

# Instrument.label -> (low_hz, high_hz) default. Rough fundamental-
# frequency registers, not scientific: enough to tell "clearly low"
# (bass) from "clearly high" (keys) apart, which is the common real
# mix-up. Every entry in INSTRUMENT_LABELS should have one here;
# effective_frequency_range() falls back to _FALLBACK_RANGE for any
# that doesn't (e.g. a label added to one list but not the other).
_DEFAULT_RANGES_BY_LABEL: dict[str, tuple[float, float]] = {
    "acoustic-guitar": (80.0, 1200.0),
    "electric-guitar": (80.0, 1200.0),
    "electric-bass": (40.0, 400.0),
    "electric-bass-fretless": (40.0, 400.0),
    "drums": (60.0, 5000.0),
    "piano": (27.0, 4200.0),
    "organ": (27.0, 4200.0),
}
assert set(_DEFAULT_RANGES_BY_LABEL) == set(INSTRUMENT_LABELS), (
    "_DEFAULT_RANGES_BY_LABEL is out of sync with config.INSTRUMENT_LABELS"
)
_FALLBACK_RANGE = (60.0, 2000.0)

# Below this peak amplitude a block is treated as silence/noise floor and
# excluded from analysis entirely — otherwise room noise between notes
# would constantly drag the classifier toward whatever instrument's range
# happens to overlap low-level hiss. Public (not underscore-prefixed)
# since backend.py's _open_channel_classifier_streams reuses it directly
# to decide when a channel counts as "gone quiet" for detect-test's
# per-input light — same definition of silence throughout, rather than a
# second threshold that could drift out of sync with this one.
SILENCE_THRESHOLD = 0.01

# How much captured audio to accumulate before running one classification
# pass — long enough to average out a single transient/pick attack, short
# enough that the display still feels "live". Lengthened from the original
# 1.0s (see InstrumentClassifier's own docstring for why) so a confirmed
# window is a more confident sample of real playing, not just barely
# enough to average out one attack.
_ANALYSIS_WINDOW_SECONDS = 1.5

# A burst of loud signal has to sustain at least this long before
# InstrumentClassifier trusts it as real playing rather than a transient
# click/pop (e.g. a 1/4" cable being plugged in hot) — see process_block.
# Comfortably longer than any pop (a few ms at most) without being long
# enough to feel like a delay before a real note gets picked up.
_ONSET_CONFIRM_SECONDS = 0.15

# A silence gap has to last at least this long to count as a genuine pause
# (the player stopped, possibly to swap instruments) rather than just the
# ordinary quiet between two notes — see process_block. Below this, an
# already-confirmed onset keeps counting as confirmed; at or above it, the
# next burst of signal has to re-prove itself sustained again too.
_RECONFIRM_SILENCE_SECONDS = 1.5


def effective_frequency_range(instrument) -> tuple[float, float]:
    """The (low_hz, high_hz) range to classify `instrument` against —
    its own configured freq_min_hz/freq_max_hz if both are set, otherwise
    the default for its label (see _DEFAULT_RANGES_BY_LABEL), otherwise
    _FALLBACK_RANGE (no label set, e.g. a config saved before that field
    existed and not yet resaved through Studio Setup)."""
    if instrument.freq_min_hz > 0.0 and instrument.freq_max_hz > instrument.freq_min_hz:
        return (instrument.freq_min_hz, instrument.freq_max_hz)
    return _DEFAULT_RANGES_BY_LABEL.get(instrument.label, _FALLBACK_RANGE)


def _energy_in_band(samples: np.ndarray, sample_rate: int, low_hz: float, high_hz: float) -> float:
    """Fraction of `samples`'s spectral energy falling within
    [low_hz, high_hz] — the scoring primitive behind classify_samples
    (compares every configured instrument's band, picks the best
    match)."""
    windowed = samples * np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(windowed))
    total_energy = float(spectrum.sum())
    if total_energy <= 0.0:
        return 0.0
    freqs = np.fft.rfftfreq(len(samples), d=1.0 / sample_rate)
    mask = (freqs >= low_hz) & (freqs <= high_hz)
    return float(spectrum[mask].sum()) / total_energy


def classify_samples(
    samples: np.ndarray, sample_rate: int, candidates: list[tuple[str, tuple[float, float]]],
) -> tuple[str | None, float]:
    """One-shot best-match classification of a batch of (already
    concatenated, already silence-checked) audio: scores every (name,
    (low_hz, high_hz)) candidate via _energy_in_band and returns the
    highest-scoring name and its score — (None, 0.0) if candidates is
    empty. The shared scoring step behind both InstrumentClassifier's
    realtime per-window pass (_classify, above) and classify_audio_file's
    whole-take analysis (below); pulled out on its own so anything else
    that needs "which of these labeled frequency bands best explains this
    audio" doesn't have to reimplement the comparison loop."""
    best_name, best_score = None, -1.0
    for name, (low_hz, high_hz) in candidates:
        score = _energy_in_band(samples, sample_rate, low_hz, high_hz)
        if score > best_score:
            best_name, best_score = name, score
    return best_name, best_score


def classify_audio_file(path: Path, instruments: list) -> tuple[str | None, float]:
    """Best-guess instrument *label* for an already-recorded audio file,
    by frequency content — used by backend.py's analyze_take (Sessions
    tab's "Analyze" button) to flag a completed take that may have been
    filed under the wrong label (takes are filed by label, not by which
    specific piece of gear played them — see TrackEntry.preferred_takes,
    so a label rather than a specific instrument's full_name is what's
    actually comparable to a take's own stored identity). `instruments`
    is scored the same way InstrumentClassifier scores a live signal:
    each instrument's effective_frequency_range (its own trained/
    hand-set freq_min_hz/freq_max_hz, or its label's default) — but
    candidates are grouped by label here, not kept one-per-instrument, so
    two instruments sharing a label (e.g. two different electric guitars
    on the same channel) don't split votes against each other.

    Reads the whole file in ~1s windows (matching _ANALYSIS_WINDOW_
    SECONDS, the realtime classifier's own window), silence-gates each
    one, classifies it independently, and returns whichever label won
    the most windows — a plurality vote across the take rather than one
    FFT over the entire file, so a long take with a quiet intro or a
    trailing pause doesn't get diluted or misjudged by a single unlucky
    global spectrum. confidence is the fraction of analyzed (non-silent)
    windows that agreed with the winner. Returns (None, 0.0) if
    `instruments` is empty, the file has no non-silent audio, or the
    file can't be read."""
    if not instruments:
        return None, 0.0
    candidates = [(inst.label, effective_frequency_range(inst)) for inst in instruments]

    import soundfile as sf
    try:
        votes: dict[str, int] = {}
        total_windows = 0
        with sf.SoundFile(str(path)) as f:
            window_samples = int(f.samplerate * _ANALYSIS_WINDOW_SECONDS)
            while True:
                block = f.read(window_samples, dtype="float64", always_2d=True)
                if len(block) < 2:
                    break
                mono = block[:, 0]
                if float(np.max(np.abs(mono))) < SILENCE_THRESHOLD:
                    continue
                name, _score = classify_samples(mono, f.samplerate, candidates)
                if name is not None:
                    votes[name] = votes.get(name, 0) + 1
                    total_windows += 1
    except Exception:
        return None, 0.0

    if not votes:
        return None, 0.0
    best_name = max(votes, key=votes.get)
    return best_name, votes[best_name] / total_windows


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
    if float(np.max(np.abs(samples))) < SILENCE_THRESHOLD:
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
    background thread whenever the best-guess instrument changes.

    A brief loud transient — a 1/4" cable pop when an instrument gets
    plugged in hot, a footstep, a stray knock — never reaches process_block
    as part of the real classification window at all: a burst of signal
    has to sustain for _ONSET_CONFIRM_SECONDS before it's trusted as real
    playing rather than a click, and one that doesn't (goes back to
    silence first) is discarded outright. This matters because a sharp
    transient is broadband — spread across every frequency, including
    whatever band happens to score well for the *wrong* instrument — and,
    being a voltage spike rather than a played note, can carry far more
    spectral energy than the actual note that follows it despite lasting a
    tiny fraction as long; left in the window uncorrected, it can dominate
    _energy_in_band's scoring and misclassify the whole window (confirmed
    for real: a bass plugged in hot got misidentified as electric guitar
    from exactly this). Once an onset has been confirmed, ordinary gaps
    between notes don't reset anything — only a gap of at least
    _RECONFIRM_SILENCE_SECONDS (a real pause, not just decay between
    notes) means the next burst has to prove itself sustained again too.

    "Changes" is relative to _last_emitted, which only reset() clears —
    so the *same* instrument being confirmed again in a row never re-
    fires on_detected on its own, deliberately: repeat notes on the one
    instrument someone's already been confirmed playing shouldn't spam
    the callback. But that means a caller that wants "confirmed again
    after a real gap" (e.g. detect-test's per-instrument light, which
    turns back off when its channel goes quiet — see backend.py's
    _open_channel_classifier_streams) must call reset() at that gap
    itself, or the second confirmation is silently suppressed as "no
    change" even though, from that caller's point of view, playing
    stopped and started again in between."""

    def __init__(
        self,
        sample_rate: int,
        instruments: list,
        on_detected: Callable[[str, float], None],
    ) -> None:
        self._sample_rate = sample_rate
        self._ranges = [(inst.full_name, effective_frequency_range(inst)) for inst in instruments]
        self._on_detected = on_detected
        self._window_samples = int(sample_rate * _ANALYSIS_WINDOW_SECONDS)
        self._onset_confirm_samples = int(sample_rate * _ONSET_CONFIRM_SECONDS)
        self._reconfirm_silence_samples = int(sample_rate * _RECONFIRM_SILENCE_SECONDS)
        self._buffer: list[np.ndarray] = []
        self._buffered_samples = 0
        # A burst of signal not yet trusted as real playing — see class
        # docstring. Promoted into self._buffer (and cleared) once it
        # sustains past _onset_confirm_samples; discarded (not promoted)
        # if silence returns first.
        self._pending: list[np.ndarray] = []
        self._pending_samples = 0
        # Whether a sustained onset has already been confirmed since the
        # last reset()/genuine silence gap — once True, ordinary silent
        # blocks between notes no longer force new signal back through
        # the pending/onset-confirmation gate; see process_block.
        self._onset_confirmed = False
        self._silence_run_samples = 0
        self._last_emitted: str | None = None

    def reset(self) -> None:
        """Clear "already reported" state (and any partially-accumulated
        window) so the next detection re-fires on_detected regardless of
        whether it's the same instrument as before — see class docstring."""
        self._buffer = []
        self._buffered_samples = 0
        self._pending = []
        self._pending_samples = 0
        self._onset_confirmed = False
        self._silence_run_samples = 0
        self._last_emitted = None

    def process_block(self, mono: np.ndarray) -> None:
        """Realtime-safe: a peak check, an array copy/append, and a sample
        counter — the actual FFT happens off-thread in _classify() once a
        full window is ready, same "spin a thread, don't block the
        callback" pattern AudioEngine._callback already uses for
        on_song_end (see engine.py)."""
        if not self._ranges:
            return
        block = mono[:, 0] if mono.ndim > 1 else mono
        is_silent = float(np.max(np.abs(block))) < SILENCE_THRESHOLD

        if is_silent:
            self._silence_run_samples += len(block)
            if self._silence_run_samples >= self._reconfirm_silence_samples:
                # A real pause, not just the ordinary quiet between two
                # notes — the next burst of signal needs to re-prove
                # itself sustained too, same as right after reset().
                self._onset_confirmed = False
                self._pending = []
                self._pending_samples = 0
            elif not self._onset_confirmed:
                # Nothing confirmed yet this window, and the burst that
                # was accumulating in _pending just went back to silence
                # without ever sustaining — a click/pop, not real
                # playing. Discard it rather than let it later count
                # toward the real classification window.
                self._pending = []
                self._pending_samples = 0
            return
        self._silence_run_samples = 0

        if not self._onset_confirmed:
            self._pending.append(block.copy())
            self._pending_samples += len(block)
            if self._pending_samples < self._onset_confirm_samples:
                return
            # Sustained long enough to trust — the whole pending burst
            # (from its very start) becomes the beginning of the real
            # classification window.
            self._onset_confirmed = True
            self._buffer.extend(self._pending)
            self._buffered_samples += self._pending_samples
            self._pending = []
            self._pending_samples = 0
        else:
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
        best_name, best_score = classify_samples(samples, self._sample_rate, self._ranges)
        if best_name is not None and best_name != self._last_emitted:
            self._last_emitted = best_name
            self._on_detected(best_name, best_score)


# --- live spectral stats (detect-test's "Currently Detected" panel) ---

# Range analyze_spectrum looks for peaks in at all — well outside it is
# either sub-bass rumble/DC offset or above what any of these instruments
# can plausibly produce as a fundamental, so excluding it up front keeps
# stray peaks there from skewing min/max or inflating the polyphony count.
# Public (not underscore-prefixed): ui/detect_test.py's spectrum
# indicator trims its own display to this exact same "normal musical
# instrument range", so both places share one definition rather than a
# second range that could drift out of sync with this one.
STATS_MIN_HZ = 30.0
STATS_MAX_HZ = 5000.0

# A spectral bin has to reach this fraction of the window's single
# loudest bin to count as a peak at all — filters out noise-floor/
# harmonic-tail bins that are technically local maxima but not
# perceptually a distinct note.
_PEAK_RELATIVE_THRESHOLD = 0.15

# Two peaks closer together than this are merged into one note rather
# than counted separately — otherwise a single (possibly vibrato'd or
# bent) note's energy spreading across a couple of adjacent FFT bins
# would inflate the polyphony count on its own.
_PEAK_MIN_SEPARATION_HZ = 20.0

# A peak within this relative distance of an exact integer multiple of an
# already-accepted fundamental counts as *that fundamental's harmonic*,
# not a second note — see _fundamental_peaks. 3% is roughly half a
# semitone (a semitone is ~5.9%), loose enough to absorb real instruments'
# slightly-inharmonic overtones (piano strings, especially) without
# starting to swallow genuinely different, closely-spaced notes.
_HARMONIC_RELATIVE_TOLERANCE = 0.03

# Harmonics past this multiple of a fundamental are usually too quiet to
# clear _PEAK_RELATIVE_THRESHOLD anyway; capping the search here just
# avoids an unrelated peak coincidentally lining up with some very high
# multiple of an unrelated fundamental and getting wrongly absorbed by it.
_MAX_HARMONIC_NUMBER = 8

# How much captured audio to accumulate before reading the spectrum —
# shorter than InstrumentClassifier's ANALYSIS_WINDOW_SECONDS since this
# is meant to read as "live", not wait for a confident instrument guess.
_STATS_WINDOW_SECONDS = 0.5


def _fundamental_peaks(peak_freqs: list[float]) -> list[float]:
    """Collapse a list of spectral peaks (ascending) down to just the
    ones that aren't explainable as a harmonic of an earlier (lower-
    frequency) one already kept — e.g. a single plucked string's
    fundamental plus its 2nd/3rd/... harmonics collapses to one entry,
    not one per harmonic, which is what actually made polyphony read
    ~20 for a 6-string guitar chord before this existed (every string's
    own overtone series was being counted as extra "notes").

    Ambiguous by nature, not just approximate: a real second note whose
    own fundamental happens to sit near a small-integer multiple of an
    already-kept one (e.g. an octave, or — on a guitar in standard
    tuning — the high E string sitting almost exactly a 4th harmonic
    above the low E string) reads as "just a harmonic" and gets
    absorbed too. Distinguishing "one note's overtone" from "a different
    note at a harmonically-related pitch" from spectral content alone
    needs real multi-pitch estimation, which this doesn't attempt (see
    analyze_spectrum's own docstring) — this trades a rare, narrow
    undercount for fixing a much more common, unbounded overcount."""
    fundamentals: list[float] = []
    for freq in peak_freqs:
        is_harmonic = False
        for fundamental in fundamentals:
            ratio = freq / fundamental
            nearest = round(ratio)
            if 2 <= nearest <= _MAX_HARMONIC_NUMBER and abs(ratio - nearest) / nearest <= _HARMONIC_RELATIVE_TOLERANCE:
                is_harmonic = True
                break
        if not is_harmonic:
            fundamentals.append(freq)
    return fundamentals


def analyze_spectrum(samples: np.ndarray, sample_rate: int) -> tuple[float, float, int, list[float]] | None:
    """Best-effort read on `samples`' frequency content: (min_hz, max_hz,
    polyphony, peak_hz) across every distinct *fundamental* found in
    [STATS_MIN_HZ, STATS_MAX_HZ] (peak_hz lists each one, ascending), or
    None if the block's silent or nothing peaks above the noise floor.

    `polyphony` is a rough "how many separate notes does this look like"
    count — not a rigorous multi-pitch estimate (real polyphonic pitch
    detection needs to reason about which peaks are harmonics of which
    fundamental in a much more principled way than _fundamental_peaks'
    plain integer-ratio check — see _energy_in_band's own module-level
    disclaimer for the same "not real timbral/harmonic analysis" caveat,
    and _fundamental_peaks' for exactly where this approximation can go
    wrong). Two guitar strings a third apart will genuinely read as
    polyphony 2 here; a single note's own overtones won't, both because
    they're typically quieter than _PEAK_RELATIVE_THRESHOLD relative to
    the fundamental and because _fundamental_peaks collapses any that do
    clear it back into the one note they actually belong to."""
    if float(np.max(np.abs(samples))) < SILENCE_THRESHOLD:
        return None
    windowed = samples * np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(len(samples), d=1.0 / sample_rate)

    mask = (freqs >= STATS_MIN_HZ) & (freqs <= STATS_MAX_HZ)
    freqs, spectrum = freqs[mask], spectrum[mask]
    if len(spectrum) < 3:
        return None

    peak_mag = float(spectrum.max())
    if peak_mag <= 0.0:
        return None
    threshold = peak_mag * _PEAK_RELATIVE_THRESHOLD

    # A local maximum (louder than both neighbors) at or above threshold.
    is_peak = (spectrum[1:-1] > spectrum[:-2]) & (spectrum[1:-1] > spectrum[2:]) & (spectrum[1:-1] >= threshold)
    peak_freqs = np.sort(freqs[1:-1][is_peak])
    if len(peak_freqs) == 0:
        return None

    merged = [float(peak_freqs[0])]
    for f in peak_freqs[1:]:
        if f - merged[-1] >= _PEAK_MIN_SEPARATION_HZ:
            merged.append(float(f))

    fundamentals = _fundamental_peaks(merged)
    if not fundamentals:
        return None
    return min(fundamentals), max(fundamentals), len(fundamentals), fundamentals


class SpectralStatsTracker:
    """Feed captured mono blocks in via process_block(); on_stats(min_hz,
    max_hz, polyphony, peak_hz) fires from a background thread roughly
    every _STATS_WINDOW_SECONDS of non-silent audio — see analyze_spectrum
    for what each value means and its limits. Independent of and unrelated
    to InstrumentClassifier — this never identifies an instrument, just
    describes the raw frequency content, and keeps running (there's no
    reset()/_last_emitted-style suppression) for as long as there's
    non-silent audio to read."""

    def __init__(self, sample_rate: int, on_stats: Callable[[float, float, int, list[float]], None]) -> None:
        self._sample_rate = sample_rate
        self._on_stats = on_stats
        self._window_samples = int(sample_rate * _STATS_WINDOW_SECONDS)
        self._buffer: list[np.ndarray] = []
        self._buffered_samples = 0

    def process_block(self, mono: np.ndarray) -> None:
        block = mono[:, 0] if mono.ndim > 1 else mono
        if float(np.max(np.abs(block))) < SILENCE_THRESHOLD:
            return
        self._buffer.append(block.copy())
        self._buffered_samples += len(block)
        if self._buffered_samples < self._window_samples:
            return
        snapshot = self._buffer
        self._buffer = []
        self._buffered_samples = 0
        threading.Thread(target=self._analyze, args=(snapshot,), daemon=True).start()

    def _analyze(self, blocks: list[np.ndarray]) -> None:
        samples = np.concatenate(blocks).astype(np.float64)
        if len(samples) < 2:
            return
        result = analyze_spectrum(samples, self._sample_rate)
        if result is not None:
            self._on_stats(*result)


class NoteCapture:
    """Captures one played note over a fixed duration and estimates its
    fundamental frequency — the "play your highest/lowest note" step of
    Studio Setup's "Train" flow. on_captured(pitch_hz) fires once, from a
    background thread, after duration_seconds of audio has been fed in
    via process_block(); pitch_hz is None if nothing periodic enough was
    found anywhere in the window (e.g. nothing was played).

    Unlike InstrumentClassifier, this doesn't silence-gate individual
    blocks — the window has to fill up regardless of when in it the
    performer actually starts playing, so silence at the start just
    means fewer valid per-chunk pitch estimates once analysis runs, not
    blocks that never counted toward the window at all."""

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
