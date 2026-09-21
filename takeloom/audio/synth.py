"""Polyphonic synthesizer for MIDI-driven instruments (piano, organ) —
see audio/midi_input.py for the note events that drive it and audio/
engine.py's AudioEngine (its `synth` param) for how its output replaces
the analog mic/DI signal in the recording pipeline, so a MIDI-driven
instrument's take is written/monitored/compressed exactly like any
other instrument's, just generated instead of captured.

`Synth` (below) is the public façade every other module imports and
constructs; it never exposes which of two implementations is actually
doing the rendering:

- `_FluidSynthVoice`: real sampled piano/organ audio via FluidSynth (the
  `fluidsynth` PyPI package, a ctypes binding to the `libfluidsynth`
  native library) playing a bundled SoundFont — see soundfont.py for
  where that file comes from. Dramatically more realistic than pure
  synthesis, at the cost of a native-library dependency this app
  otherwise avoids (see ffmpeg_bin.py's own docstring for the class of
  problem that avoidance is about) — acceptable here specifically
  because it only ever has to work on the one machine that actually
  runs sessions (see CLAUDE.md's studio hardware notes), not something
  distributed to other people's machines.
- `_AdditiveSynth`: the original pure-numpy small bank of hand-tuned
  harmonics per voice — synthetic-sounding but zero extra dependency,
  cheap enough to run inline on the realtime audio callback thread.
  Used automatically whenever FluidSynth or its SoundFont isn't
  available (library not installed, no network for the one-time
  SoundFont download, etc.) — see Synth.__init__ — so a machine without
  FluidSynth set up still records a MIDI take, just with a more
  synthetic tone, rather than refusing to record at all.

Either way, rendering happens inline on the realtime audio callback
thread itself (FluidSynth's own C rendering is easily fast enough —
sub-millisecond per block, see its own docstring), not through a
separate synth process/thread, which is what keeps this "zero latency
as possible": no extra hop, just this block's own buffer_size/
sample_rate latency, the same as any other instrument going through
this engine.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SYNTH_VOICES = ["piano", "organ"]
DEFAULT_SYNTH_VOICE = "piano"

# General MIDI bank-0 program numbers, within the bundled SoundFont, for
# each voice — 0 = Acoustic Grand Piano, 16 = Drawbar Organ (the closest
# GM equivalent to _AdditiveSynth's Hammond-ish organ model).
_GM_PROGRAM_BY_VOICE = {"piano": 0, "organ": 16}

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


class _AdditiveSynth:
    """The pure-numpy fallback voice — see module docstring. A small
    polyphonic synth voiced as either "piano" or "organ".

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
        # 0.0-1.0, driven by the MIDI keyboard's own physical volume
        # slider (CC7, "Channel Volume" — see audio/midi_input.py) if it
        # has one. Starts at full: most keyboards don't resend their
        # slider's current position on connect, and a silently-quiet
        # instrument would be a far more confusing default than "as loud
        # as everything else until the slider is actually touched."
        self._channel_volume = 1.0
        self._lock = threading.Lock()

    def set_channel_volume(self, value: int) -> None:
        """MIDI Control Change 7 — applied to the actual generated
        signal (not just the live monitor feed the way AudioEngine's own
        instrument_volume dial is, see engine.py), the same way easing
        off a real instrument's own volume knob would naturally result
        in a quieter microphone capture: this is part of the
        performance, so it's reflected in the take itself, not just
        what's heard live. 0-127, MIDI's own standard range — squared
        (not linear) to match _FluidSynthVoice's own CC7 curve
        (measured empirically: FluidSynth scales its output by
        (value/127)**2, a perceptual/loudness curve rather than a flat
        amplitude one), so a keyboard's slider feels the same regardless
        of which backend happens to be rendering."""
        with self._lock:
            self._channel_volume = (max(0, min(127, value)) / 127.0) ** 2

    def set_voice(self, voice: str) -> None:
        """Live voice switch — e.g. the Record page's "Sound" picker
        (backend.py's set_synth_voice), which is meant to work while
        notes are actively playing, not just before the first one. Safe
        to call from any thread: guarded by the same lock render() holds
        for its own self.voice reads, so a change can never land mid-
        block and mix harmonics from one voice with envelope math from
        the other.

        Both current voices' harmonic tables are the same length (see
        SYNTH_VOICES' arrays), so switching never crashes an in-flight
        note — but it does immediately reinterpret every *currently
        ringing* note's envelope under the new voice's math too (there's
        no per-voice "which sound was this struck under" memory), which
        can produce a brief, audible discontinuity on a note that's
        still sustaining at the moment of the switch. A newly struck
        note is unaffected either way."""
        with self._lock:
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
            channel_volume = self._channel_volume
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
        out *= _MASTER_GAIN * channel_volume
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


class _FluidSynthVoice:
    """Real sampled piano/organ via FluidSynth + a bundled SoundFont —
    see module docstring and soundfont.py for where the file comes from.
    try_create() is the only way to get one: it returns None (never
    raises) on any failure — the `fluidsynth` package not installed, no
    libfluidsynth on this machine, the SoundFont not fetchable — so
    Synth.__init__ falls back to _AdditiveSynth transparently instead of
    refusing to construct a MIDI instrument's engine at all.

    Thread safety mirrors _AdditiveSynth's: note_on/note_off/
    set_sustain/set_voice are meant to be called from a MIDI input's own
    callback thread (or, for set_voice, the Record page's backend
    thread) while render() runs on the realtime audio thread — all
    guarded by the same lock, held only for plain FluidSynth API calls,
    never anything that blocks."""

    # FluidSynth's own default gain (0.2) is tuned for many simultaneous
    # MIDI channels at once; this app only ever plays one voice through
    # one channel, so there's room to turn it up. 1.3 was picked by
    # measuring an unusually loud case — a six-note, full-velocity chord
    # (both hands, fortissimo) — which peaks around 0.96 at this gain;
    # ordinary playing sits well below that. render()'s own clip is
    # still the final safety net regardless.
    _GAIN = 1.3

    @classmethod
    def try_create(cls, sample_rate: int, voice: str) -> "_FluidSynthVoice | None":
        try:
            import fluidsynth
        except ImportError:
            return None
        from .soundfont import ensure_soundfont
        soundfont_path = ensure_soundfont()
        if soundfont_path is None:
            return None
        try:
            return cls(fluidsynth, sample_rate, voice, soundfont_path)
        except Exception as e:
            print(f"takeloom: FluidSynth unavailable ({e}) — using the built-in synth voice instead.")
            return None

    def __init__(self, fluidsynth_module, sample_rate: int, voice: str, soundfont_path: Path) -> None:
        self._fs = fluidsynth_module.Synth(gain=self._GAIN, samplerate=float(sample_rate))
        sfid = self._fs.sfload(str(soundfont_path))
        if sfid == -1:
            raise RuntimeError(f"fluidsynth could not load soundfont {soundfont_path}")
        self._sfid = sfid
        self._channel = 0  # this app only ever plays one voice at a time — no need for more
        self._lock = threading.Lock()
        self.set_voice(voice)

    def set_voice(self, voice: str) -> None:
        program = _GM_PROGRAM_BY_VOICE.get(voice, _GM_PROGRAM_BY_VOICE[DEFAULT_SYNTH_VOICE])
        with self._lock:
            self._fs.program_select(self._channel, self._sfid, 0, program)

    def note_on(self, note: int, velocity: int) -> None:
        """Same velocity-0-is-a-note-off tolerance as _AdditiveSynth's
        own note_on — belt and suspenders alongside whatever FluidSynth
        itself already does internally with a zero velocity."""
        if velocity <= 0:
            self.note_off(note)
            return
        with self._lock:
            self._fs.noteon(self._channel, note, velocity)

    def note_off(self, note: int) -> None:
        with self._lock:
            self._fs.noteoff(self._channel, note)

    def set_sustain(self, down: bool) -> None:
        with self._lock:
            self._fs.cc(self._channel, 64, 127 if down else 0)  # CC64 = sustain pedal

    def set_channel_volume(self, value: int) -> None:
        """Forwarded straight to FluidSynth's own native CC7 handling —
        unlike _AdditiveSynth, which has to implement the gain scaling
        itself, FluidSynth already applies Channel Volume internally to
        whatever it renders next, so there's no extra math here at all."""
        with self._lock:
            self._fs.cc(self._channel, 7, max(0, min(127, value)))  # CC7 = channel volume

    def all_notes_off(self) -> None:
        """Unlike _AdditiveSynth's hard, instant cut, FluidSynth's own
        all_notes_off triggers each voice's normal release/decay tail
        (and any reverb) rather than silencing them on the spot — a
        graceful release, not a kill switch. Nothing in this codebase
        currently calls this method, so the difference has no effect
        today; noted here so it isn't a surprise if something starts
        relying on it for an instant cutoff later."""
        with self._lock:
            self._fs.all_notes_off(self._channel)

    def render(self, frames: int) -> np.ndarray:
        """Same (frames, 1) mono float32 shape _AdditiveSynth.render()
        returns — FluidSynth itself only renders interleaved stereo, so
        this downmixes by averaging L/R (most GM patches, including the
        piano/organ ones used here, aren't hard-panned, so this loses
        essentially nothing) and rescales from get_samples()'s int16
        range to float32 [-1, 1]."""
        with self._lock:
            samples = self._fs.get_samples(frames)  # interleaved stereo int16, length 2*frames
        stereo = samples.reshape(-1, 2).astype(np.float32) / 32768.0
        mono = stereo.mean(axis=1, keepdims=True)
        np.clip(mono, -1.0, 1.0, out=mono)
        return mono


class Synth:
    """Public façade every other module imports and constructs — see the
    module docstring for what actually renders behind it (FluidSynth +
    a bundled SoundFont when available, else the built-in additive
    synth) and why callers never need to know which. Constructing one
    picks the implementation once, up front; there's no live switch
    between them mid-session."""

    def __init__(self, sample_rate: int, voice: str = DEFAULT_SYNTH_VOICE) -> None:
        self.sample_rate = sample_rate
        self.voice = voice if voice in SYNTH_VOICES else DEFAULT_SYNTH_VOICE
        impl = _FluidSynthVoice.try_create(sample_rate, self.voice)
        self._impl = impl if impl is not None else _AdditiveSynth(sample_rate, self.voice)

    def set_voice(self, voice: str) -> None:
        if voice in SYNTH_VOICES:
            self.voice = voice
            self._impl.set_voice(voice)

    def note_on(self, note: int, velocity: int) -> None:
        self._impl.note_on(note, velocity)

    def note_off(self, note: int) -> None:
        self._impl.note_off(note)

    def set_sustain(self, down: bool) -> None:
        self._impl.set_sustain(down)

    def set_channel_volume(self, value: int) -> None:
        self._impl.set_channel_volume(value)

    def all_notes_off(self) -> None:
        self._impl.all_notes_off()

    def render(self, frames: int) -> np.ndarray:
        return self._impl.render(frames)
