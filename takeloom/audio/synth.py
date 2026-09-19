"""Polyphonic software synthesizer for MIDI-driven instruments (piano,
organ) — see audio/midi_input.py for the note events that drive it and
audio/engine.py's AudioEngine (its `synth` param) for how its output
replaces the analog mic/DI signal in the recording pipeline, so a
MIDI-driven instrument's take is written/monitored/compressed exactly
like any other instrument's, just generated instead of captured.

Deliberately pure numpy — no soundfont/native synth library. This app
already learned the cost of an extra native/system dependency the hard
way once (see static_ffmpeg_dylib_fix / CLAUDE.md's ffmpeg bundling
story) and bundles its own ffmpeg specifically to avoid that class of
"works today, silently breaks after a library upgrade" failure; pulling
in something like fluidsynth (a native library + a multi-megabyte
soundfont file) would reintroduce exactly that risk for comparatively
little gain. The tradeoff is a simpler, more synthetic timbre than a
sampled instrument — a small bank of additive harmonics per voice, not
a real piano/organ recording — in exchange for zero extra install
footprint and rendering cheap enough to run inline on the realtime
audio callback thread itself, which is what keeps this "zero latency
as possible": there's no separate synth process/thread to hop through,
just numpy math computed fresh for each output block against whatever
notes have arrived by the start of that block.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

SYNTH_VOICES = ["piano", "organ"]
DEFAULT_SYNTH_VOICE = "piano"

_MAX_VOICES = 32  # simultaneous notes before the oldest (by note-on order) is stolen
_FINISHED_THRESHOLD = 1e-4  # peak instantaneous envelope below this = silent enough to drop the voice

# Additive harmonic tables — small, hand-tuned models rather than a
# sampled instrument (see module docstring). Organ: classic drawbar-ish
# ratios (fundamental, octave, twelfth, ...) at roughly equal strength,
# a near-instant attack/release and flat sustain while held — a
# Hammond-like tone. Piano: a handful of harmonics that each decay at
# their own rate (higher harmonics faster), which is what actually reads
# as "piano-ish" here — the tone visibly darkens as a note rings out,
# the same way a struck string's upper partials die out faster than its
# fundamental.
_ORGAN_HARMONICS = np.array([1.0, 2.0, 3.0, 4.0, 6.0, 8.0])
_ORGAN_AMPS = np.array([1.0, 0.7, 0.35, 0.25, 0.15, 0.1])
_ORGAN_ATTACK_S = 0.006
_ORGAN_RELEASE_S = 0.04

_PIANO_HARMONICS = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
_PIANO_AMPS = np.array([1.0, 0.55, 0.3, 0.18, 0.1, 0.06])
_PIANO_DECAY_PER_HARMONIC = np.array([0.7, 1.4, 2.4, 3.6, 5.0, 6.6])  # 1/e decay rate (1/seconds), per harmonic
_PIANO_RELEASE_RATE = 18.0  # extra 1/e decay rate applied once the key is let go (the damper)

_MASTER_GAIN = 0.6  # headroom for several simultaneous voices before Synth.render()'s final clip


def _note_to_freq(note: int) -> float:
    """MIDI note number (0-127, 69 = A4/440Hz) to frequency in Hz."""
    return 440.0 * (2.0 ** ((note - 69) / 12.0))


@dataclass
class _Voice:
    note: int
    freq: float
    velocity: float  # 0..1
    phase: np.ndarray  # per-harmonic running phase (radians), shape (H,)
    age: float = 0.0  # seconds since note_on, updated once per render() block
    released: bool = False
    release_age: float = 0.0  # seconds since note_off, once released
    env_at_release: float = 1.0  # organ only: attack envelope value captured the instant note_off arrived


class Synth:
    """A small polyphonic synth voiced as either "piano" or "organ".

    note_on/note_off/set_sustain are meant to be called from a MIDI
    input's own callback thread (see audio/midi_input.py's MidiInput)
    while render() runs on the realtime audio callback thread (see
    AudioEngine._callback) — both sides share `_lock`, held only for
    plain voice-list bookkeeping, never anything that blocks, so this
    never risks stalling either thread."""

    def __init__(self, sample_rate: int, voice: str = DEFAULT_SYNTH_VOICE) -> None:
        self.sample_rate = sample_rate
        self.voice = voice if voice in SYNTH_VOICES else DEFAULT_SYNTH_VOICE
        self._voices: dict[int, _Voice] = {}  # note -> voice, active or releasing
        self._order: list[int] = []  # notes in note-on order, oldest first — for voice stealing
        self._sustain = False
        self._sustained_notes: set[int] = set()  # held past their own note_off by the sustain pedal
        self._lock = threading.Lock()

    def set_voice(self, voice: str) -> None:
        """Meant to be called before any notes are active (e.g. right
        after construction, picking the instrument's configured voice) —
        switching mid-note would leave an in-flight voice's harmonic
        table mismatched against the new voice's, which render() doesn't
        guard against."""
        if voice in SYNTH_VOICES:
            self.voice = voice

    def note_on(self, note: int, velocity: int) -> None:
        """A velocity of 0 is, per the MIDI spec, a common alternate
        spelling of Note Off — handled the same as an explicit Note Off
        rather than starting a silent voice."""
        if velocity <= 0:
            self.note_off(note)
            return
        harmonics = _ORGAN_HARMONICS if self.voice == "organ" else _PIANO_HARMONICS
        freq = _note_to_freq(note)
        v = _Voice(
            note=note, freq=freq, velocity=min(1.0, velocity / 127.0),
            phase=np.zeros(len(harmonics), dtype=np.float64),
        )
        with self._lock:
            if note in self._voices:
                self._order.remove(note)
            elif len(self._voices) >= _MAX_VOICES:
                oldest = self._order.pop(0)
                del self._voices[oldest]
            self._voices[note] = v
            self._order.append(note)
            self._sustained_notes.discard(note)

    def note_off(self, note: int) -> None:
        with self._lock:
            if self._sustain:
                self._sustained_notes.add(note)
                return
            self._release_locked(note)

    def _release_locked(self, note: int) -> None:
        v = self._voices.get(note)
        if v is None or v.released:
            return
        v.released = True
        if self.voice == "organ":
            v.env_at_release = min(1.0, v.age / _ORGAN_ATTACK_S)

    def set_sustain(self, down: bool) -> None:
        """down=True holds every subsequent note_off without releasing
        the voice (sustain pedal pressed); down=False releases everything
        that was being held that way."""
        with self._lock:
            self._sustain = down
            if not down:
                for note in list(self._sustained_notes):
                    self._release_locked(note)
                self._sustained_notes.clear()

    def all_notes_off(self) -> None:
        with self._lock:
            self._voices.clear()
            self._order.clear()
            self._sustained_notes.clear()

    def render(self, frames: int) -> np.ndarray:
        """Sum every active voice into `frames` samples, advancing each
        voice's phase/envelope and dropping any that have decayed to
        silence. Returns mono float32, shape (frames, 1) — the same
        shape AudioEngine._callback's analog capture path produces, so
        it drops straight into the rest of the pipeline (recording,
        compressor, mixer) unchanged. Always called from the realtime
        audio thread, once per output block."""
        dt = 1.0 / self.sample_rate
        block_t = np.arange(1, frames + 1, dtype=np.float64) * dt  # time-since-block-start, per sample
        out = np.zeros(frames, dtype=np.float64)
        with self._lock:
            if not self._voices:
                return out.astype(np.float32).reshape(-1, 1)
            harmonics = _ORGAN_HARMONICS if self.voice == "organ" else _PIANO_HARMONICS
            amps = _ORGAN_AMPS if self.voice == "organ" else _PIANO_AMPS
            finished = []
            for note, v in self._voices.items():
                sig, done = self._render_voice(v, block_t, harmonics, amps, dt)
                out += sig
                v.age += frames * dt
                if v.released:
                    v.release_age += frames * dt
                if done:
                    finished.append(note)
            for note in finished:
                del self._voices[note]
                if note in self._order:
                    self._order.remove(note)
        out *= _MASTER_GAIN
        np.clip(out, -1.0, 1.0, out=out)
        return out.astype(np.float32).reshape(-1, 1)

    def _render_voice(
        self, v: _Voice, block_t: np.ndarray, harmonics: np.ndarray, amps: np.ndarray, dt: float,
    ) -> tuple[np.ndarray, bool]:
        n = len(block_t)
        omega = 2.0 * np.pi * v.freq * harmonics * dt  # per-harmonic phase increment per sample, shape (H,)
        idx = np.arange(1, n + 1, dtype=np.float64)
        phases = v.phase[:, None] + np.outer(omega, idx)  # shape (H, n)

        if self.voice == "organ":
            age = v.age + block_t  # shape (n,)
            if not v.released:
                env = np.minimum(age / _ORGAN_ATTACK_S, 1.0)
            else:
                rel_age = v.release_age + block_t
                env = np.clip(v.env_at_release * (1.0 - rel_age / _ORGAN_RELEASE_S), 0.0, None)
            env_h = np.broadcast_to(env, (len(harmonics), n))
        else:
            age = v.age + block_t
            env_h = np.exp(-_PIANO_DECAY_PER_HARMONIC[:, None] * age[None, :])
            if v.released:
                rel_age = v.release_age + block_t
                env_h = env_h * np.exp(-_PIANO_RELEASE_RATE * rel_age)[None, :]

        sig_h = (amps[:, None] * env_h) * np.sin(phases)
        sig = sig_h.sum(axis=0) * v.velocity

        v.phase = phases[:, -1] % (2.0 * np.pi)

        if self.voice == "organ":
            done = bool(v.released and (v.release_age + block_t[-1]) >= _ORGAN_RELEASE_S)
        else:
            final_env = float((amps * env_h[:, -1]).max())
            done = final_env < _FINISHED_THRESHOLD

        return sig, done
