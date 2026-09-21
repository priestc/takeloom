"""Note-name/cents math for the Record tab's tuner (see StreamDeckController's
touchscreen needle and ui/tuner_meter.py's Tk equivalent) — pure arithmetic,
no numpy/audio dependency, so it's safe to import from UI-only contexts
(Studio Setup's tuning field, a Remote client with no local audio at all)
as well as backend.py, which is the only caller that ever has a real Hz
reading to feed it (see audio/instrument_classifier.py's TunerTracker).

Equal temperament, A4 = 440Hz throughout — no support for alternate
tunings/temperaments; a "tuning" here just means "which specific pitches
count as in-tune" (e.g. drop-D), not a different definition of the octave.
"""

from __future__ import annotations

import math
import re

_A4_HZ = 440.0
_A4_SEMITONES_FROM_C0 = 57  # C0, A0, ... C4, ..., A4 is the 57th semitone above C0
_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Accepts a trailing "b" (flat) on input even though hz_to_note_name only
# ever produces sharps — "Bb2" is a more natural thing for someone to type
# into Studio Setup's tuning field than "A#2", so parsing (not display)
# supports both spellings of every accidental.
_FLAT_TO_SHARP = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
_NOTE_RE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d+)$")


def parse_note(text: str) -> float | None:
    """"E2" (or "Bb2", "F#3", lowercase either way) -> Hz. None if `text`
    isn't a recognizable note name — the caller decides what "couldn't
    parse this one" should mean (parse_tuning below just drops it;
    Studio Setup could instead choose to flag it, but doesn't today, same
    tolerance StudioConfig.validate() already extends to freq_min/max_hz's
    text entries elsewhere)."""
    match = _NOTE_RE.match(text.strip())
    if match is None:
        return None
    letter, accidental, octave_str = match.groups()
    name = letter.upper() + accidental.lower()
    if accidental == "b":
        name = _FLAT_TO_SHARP.get(letter.upper() + "b")
        if name is None:
            return None
    try:
        octave = int(octave_str)
    except ValueError:
        return None
    semitone_index = _NOTE_NAMES.index(name)
    semitones_from_c0 = octave * 12 + semitone_index
    return _A4_HZ * (2.0 ** ((semitones_from_c0 - _A4_SEMITONES_FROM_C0) / 12.0))


def hz_to_note_name(freq_hz: float) -> str:
    """Nearest equal-tempered note name (scientific pitch notation, e.g.
    "E2") for freq_hz — always spelled with sharps, never flats (see the
    module docstring)."""
    semitones_from_a4 = round(12.0 * math.log2(freq_hz / _A4_HZ))
    semitones_from_c0 = _A4_SEMITONES_FROM_C0 + semitones_from_a4
    octave, semitone_index = divmod(semitones_from_c0, 12)
    return f"{_NOTE_NAMES[semitone_index]}{octave}"


def cents_between(freq_hz: float, target_hz: float) -> float:
    """How far freq_hz is from target_hz, in cents (100ths of a
    semitone) — negative is flat, positive is sharp."""
    return 1200.0 * math.log2(freq_hz / target_hz)


def parse_tuning(text: str) -> list[str]:
    """A Studio Setup tuning field's raw text (space/comma separated, e.g.
    "E2 A2 D3 G3 B3 E4") -> the note names that actually parsed, low to
    high as written, silently dropping anything that doesn't (a typo
    shouldn't block saving the rest of the form) — see parse_note."""
    tokens = [t for t in re.split(r"[\s,]+", text.strip()) if t]
    return [t for t in tokens if parse_note(t) is not None]


def format_tuning(tuning: list[str]) -> str:
    """Inverse of parse_tuning, for pre-filling the Studio Setup field."""
    return " ".join(tuning)


# Instrument.label -> default tuning, for stringed instruments only —
# mirrors instrument_classifier.py's _DEFAULT_RANGES_BY_LABEL (used the
# same way: only when the instrument's own `tuning` is unset). Unfretted/
# unpitched-in-this-sense labels (drums) or instruments where "tuning" as
# a small fixed note set doesn't really apply (midi-keyboard) have none —
# nearest_target falls back to the nearest chromatic note for those.
DEFAULT_TUNING_BY_LABEL: dict[str, list[str]] = {
    "electric-guitar": ["E2", "A2", "D3", "G3", "B3", "E4"],
    "acoustic-guitar": ["E2", "A2", "D3", "G3", "B3", "E4"],
    "electric-bass": ["E1", "A1", "D2", "G2"],
    "electric-bass-fretless": ["E1", "A1", "D2", "G2"],
}

# (display name, tuning) quick-pick presets offered by Studio Setup's
# Tuning dropdown (see ui/studio_setup.py's _InstrumentRow), keyed by
# Instrument.label — a curated common-tunings list, not exhaustive; the
# field stays a free-editable combobox precisely so anything not listed
# here can still be typed by hand. Each label's first entry is always the
# same tuning as DEFAULT_TUNING_BY_LABEL, so "leave it blank" and "pick
# the first preset" mean the same thing. Bass has no separate 4-string/
# 5-string label (see config.INSTRUMENT_LABELS) — both string counts'
# presets are offered together under "electric-bass"/"electric-bass-
# fretless" rather than needing a label split just for this.
TUNING_PRESETS_BY_LABEL: dict[str, list[tuple[str, list[str]]]] = {
    "electric-guitar": [
        ("Standard (E A D G B E)", ["E2", "A2", "D3", "G3", "B3", "E4"]),
        ("Drop D (D A D G B E)", ["D2", "A2", "D3", "G3", "B3", "E4"]),
        ("Half Step Down (Eb Ab Db Gb Bb Eb)", ["Eb2", "Ab2", "Db3", "Gb3", "Bb3", "Eb4"]),
        ("Drop C# (C# G# C# F# A# D#)", ["C#2", "G#2", "C#3", "F#3", "A#3", "D#4"]),
        ("Open G (D G D G B D)", ["D2", "G2", "D3", "G3", "B3", "D4"]),
        ("Open D (D A D F# A D)", ["D2", "A2", "D3", "F#3", "A3", "D4"]),
        ("Open E (E B E G# B E)", ["E2", "B2", "E3", "G#3", "B3", "E4"]),
        ("DADGAD (D A D G A D)", ["D2", "A2", "D3", "G3", "A3", "D4"]),
    ],
    "electric-bass": [
        ("Standard 4-String (E A D G)", ["E1", "A1", "D2", "G2"]),
        ("Drop D 4-String (D A D G)", ["D1", "A1", "D2", "G2"]),
        ("Half Step Down 4-String (Eb Ab Db Gb)", ["Eb1", "Ab1", "Db2", "Gb2"]),
        ("Standard 5-String (B E A D G)", ["B0", "E1", "A1", "D2", "G2"]),
        ("Standard 5-String, High C (E A D G C)", ["E1", "A1", "D2", "G2", "C3"]),
    ],
}
TUNING_PRESETS_BY_LABEL["acoustic-guitar"] = TUNING_PRESETS_BY_LABEL["electric-guitar"]
TUNING_PRESETS_BY_LABEL["electric-bass-fretless"] = TUNING_PRESETS_BY_LABEL["electric-bass"]


def effective_tuning(instrument) -> list[str]:
    """The tuning to display targets from for `instrument` (any object
    with .tuning/.label — i.e. a config.Instrument, not imported by name
    here to avoid a config.py <-> audio.pitch import cycle) — its own
    config.Instrument.tuning if set, otherwise DEFAULT_TUNING_BY_LABEL for
    its label, otherwise empty (nearest_target then falls back to the
    nearest chromatic note). Mirrors instrument_classifier.py's
    effective_frequency_range."""
    if instrument.tuning:
        return instrument.tuning
    return DEFAULT_TUNING_BY_LABEL.get(instrument.label, [])


def nearest_target(freq_hz: float, tuning: list[str]) -> tuple[str, float, float]:
    """(note_name, target_hz, cents_off) for freq_hz — snapped to the
    closest note in `tuning` if any of it parses, otherwise the nearest
    chromatic semitone. cents_off is freq_hz relative to target_hz (see
    cents_between); a well-tuned string reads close to 0."""
    targets = [(name, hz) for name in tuning if (hz := parse_note(name)) is not None]
    if not targets:
        name = hz_to_note_name(freq_hz)
        target_hz = parse_note(name)
        assert target_hz is not None  # hz_to_note_name always produces a parseable name
        return name, target_hz, cents_between(freq_hz, target_hz)
    name, target_hz = min(targets, key=lambda nt: abs(cents_between(freq_hz, nt[1])))
    return name, target_hz, cents_between(freq_hz, target_hz)


class TunerSmoother:
    """Exponential moving average over consecutive nearest_target() cents
    readings, so the tuner needle doesn't visibly jitter between
    individual ~quarter-second pitch estimates (each one's its own
    independent autocorrelation pass — see audio/instrument_classifier.py's
    TunerTracker — so raw readings for an actually-steady note still wobble
    a few cents from one to the next). Resets instantly (no smoothing lag)
    the moment the resolved note itself changes, e.g. moving to a
    different string, rather than sliding the needle through the gap
    between two unrelated pitches as if it were one continuous bend.

    One instance per independent display (RecordingDeckDriver and ui/
    record.py each keep their own — same duplication as detected_
    instrument/identify_state, since they're tracking backend events on
    separate subscriptions and shouldn't smooth across each other's gaps
    in what they've each actually received)."""

    def __init__(self, alpha: float = 0.35) -> None:
        self._alpha = alpha
        self._note: str | None = None
        self._smoothed_cents = 0.0

    def update(self, note: str, cents: float) -> float:
        if note != self._note:
            self._note = note
            self._smoothed_cents = cents
        else:
            self._smoothed_cents += self._alpha * (cents - self._smoothed_cents)
        return self._smoothed_cents

    def reset(self) -> None:
        """Called whenever a fresh identify cycle starts (see
        RecordingDeckDriver._begin_identify/_redo_identify and their ui/
        record.py equivalents) — otherwise the *next* cycle's first
        reading would start already smoothed toward whatever note the
        *previous* cycle's take happened to end on."""
        self._note = None
        self._smoothed_cents = 0.0
