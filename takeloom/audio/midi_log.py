"""Captures a session's raw MIDI performance (note/sustain/volume/
expression activity, frame-stamped against the same session-audio
timeline as everything else — see backend.py's _SessionEvent) alongside
the audio Synth.render() already produces from it, and lets
processing/splicer.py slice a completed take's own MIDI out of that
alongside its .flac/.mp4, the same way it slices the audio itself.

The point: a rendered take is one specific synth voice baked into
audio. Keeping the actual MIDI performance too means a take can later
be "revoiced" — re-rendered through a different voice (piano vs organ)
— without re-recording. Nothing in this module renders audio or does
the revoicing itself; it only captures/persists/slices the MIDI data.

Timing: a standard MIDI file's ticks_per_beat is a 16-bit field (max
32767), too small to let one tick equal one audio sample at a real
sample rate (44100/48000/96000...). Instead every file here fixes
_TICKS_PER_BEAT=1000 with a tempo of exactly one beat per second, so
one tick is exactly one millisecond — far finer than needed for a
MIDI performance's timing to read back correctly, and simple to convert
to/from a frame position given only the session's sample_rate (not
carried in the file itself; callers already have it from session_log.
json/StudioConfig, the same source process_session's own audio slicing
already uses).
"""

from __future__ import annotations

import threading
from pathlib import Path

_TICKS_PER_BEAT = 1000
_TEMPO_US_PER_BEAT = 1_000_000  # 1 beat = 1 second => 1 tick = 1 millisecond


def _frame_to_tick(frame: int, sample_rate: int) -> int:
    return round(frame / sample_rate * _TICKS_PER_BEAT)


def _tick_to_frame(tick: int, sample_rate: int) -> int:
    return round(tick * sample_rate / _TICKS_PER_BEAT)


class MidiEventLog:
    """Thread-safe accumulator for one session's raw MIDI events, fed
    from MidiInput's callbacks (backend.py's _build_engine_for_
    instrument) on rtmidi's own notification thread — append() is
    called from there, events()/write from the main/processing thread
    later, hence the lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[tuple[int, str, dict]] = []

    def append(self, frame: int, kind: str, **fields) -> None:
        with self._lock:
            self._events.append((frame, kind, fields))

    def events(self) -> list[tuple[int, str, dict]]:
        with self._lock:
            return list(self._events)

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._events)


def _event_to_message(kind: str, fields: dict):
    import mido
    if kind == "note_on":
        return mido.Message("note_on", note=fields["note"], velocity=fields["velocity"])
    if kind == "note_off":
        return mido.Message("note_off", note=fields["note"], velocity=0)
    if kind == "sustain":
        return mido.Message("control_change", control=64, value=127 if fields["down"] else 0)
    if kind == "volume":
        return mido.Message("control_change", control=7, value=fields["value"])
    if kind == "expression":
        return mido.Message("control_change", control=11, value=fields["value"])
    raise ValueError(f"Unknown MIDI log event kind: {kind!r}")


def write_midi_file(events: list[tuple[int, str, dict]], sample_rate: int, path: Path) -> None:
    """Write `events` (frame, kind, fields — see MidiEventLog.append) as
    a Type 0 Standard MIDI File at `path`, oldest first regardless of
    the order passed in."""
    import mido
    mid = mido.MidiFile(type=0, ticks_per_beat=_TICKS_PER_BEAT)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=_TEMPO_US_PER_BEAT, time=0))
    last_tick = 0
    for frame, kind, fields in sorted(events, key=lambda e: e[0]):
        tick = _frame_to_tick(frame, sample_rate)
        msg = _event_to_message(kind, fields)
        msg.time = max(0, tick - last_tick)
        track.append(msg)
        last_tick = tick
    path.parent.mkdir(parents=True, exist_ok=True)
    mid.save(str(path))


def _read_midi_events(path: Path, sample_rate: int) -> list[tuple[int, str, dict]]:
    """The inverse of write_midi_file: `path`'s messages back as (frame,
    kind, fields), oldest first. Ignores anything not written by
    write_midi_file above (meta messages, other CCs) — this only ever
    reads a file this module itself wrote."""
    import mido
    mid = mido.MidiFile(str(path))
    events: list[tuple[int, str, dict]] = []
    tick = 0
    for msg in mid.tracks[0]:
        tick += msg.time
        if msg.is_meta:
            continue
        frame = _tick_to_frame(tick, sample_rate)
        if msg.type == "note_on" and msg.velocity > 0:
            events.append((frame, "note_on", {"note": msg.note, "velocity": msg.velocity}))
        elif msg.type in ("note_on", "note_off"):
            events.append((frame, "note_off", {"note": msg.note}))
        elif msg.type == "control_change" and msg.control == 64:
            events.append((frame, "sustain", {"down": msg.value >= 64}))
        elif msg.type == "control_change" and msg.control == 7:
            events.append((frame, "volume", {"value": msg.value}))
        elif msg.type == "control_change" and msg.control == 11:
            events.append((frame, "expression", {"value": msg.value}))
    return events


def slice_midi_file(src_path: Path, start_frame: int, end_frame: int, sample_rate: int, out_path: Path) -> None:
    """Cut src_path's [start_frame, end_frame) range (session-audio
    frames, same units/timeline as processing/splicer.py's own audio
    slicing) into a new, self-contained MIDI file at out_path, rebased
    to start at frame 0.

    "Self-contained" is the part naive slicing would get wrong: any note
    already held, or sustain/volume/expression already set, before
    start_frame is replayed at relative tick 0 so the segment doesn't
    open on unexpected silence or a stuck default; any note still held
    at end_frame gets an explicit note_off there so the segment doesn't
    leave a note ringing forever when played/rendered on its own.
    """
    events = _read_midi_events(src_path, sample_rate)

    held_notes: dict[int, int] = {}
    sustain_down = False
    volume: int | None = None
    expression: int | None = None
    held_at_start = held_notes
    sustain_at_start = sustain_down
    volume_at_start = volume
    expression_at_start = expression
    snapshot_taken = False
    in_range: list[tuple[int, str, dict]] = []

    for frame, kind, fields in events:
        if not snapshot_taken and frame >= start_frame:
            held_at_start = dict(held_notes)
            sustain_at_start = sustain_down
            volume_at_start = volume
            expression_at_start = expression
            snapshot_taken = True
        if kind == "note_on":
            held_notes[fields["note"]] = fields["velocity"]
        elif kind == "note_off":
            held_notes.pop(fields["note"], None)
        elif kind == "sustain":
            sustain_down = fields["down"]
        elif kind == "volume":
            volume = fields["value"]
        elif kind == "expression":
            expression = fields["value"]
        if start_frame <= frame < end_frame:
            in_range.append((frame - start_frame, kind, fields))
    if not snapshot_taken:
        # Every logged event (if any) was before start_frame — carry
        # whatever state that leaves us in, same as the mid-loop case.
        held_at_start = dict(held_notes)
        sustain_at_start = sustain_down
        volume_at_start = volume
        expression_at_start = expression

    out_events: list[tuple[int, str, dict]] = []
    if volume_at_start is not None:
        out_events.append((0, "volume", {"value": volume_at_start}))
    if expression_at_start is not None:
        out_events.append((0, "expression", {"value": expression_at_start}))
    if sustain_at_start:
        out_events.append((0, "sustain", {"down": True}))
    for note, velocity in held_at_start.items():
        out_events.append((0, "note_on", {"note": note, "velocity": velocity}))
    out_events.extend(in_range)

    still_held = dict(held_at_start)
    for _, kind, fields in in_range:
        if kind == "note_on":
            still_held[fields["note"]] = fields["velocity"]
        elif kind == "note_off":
            still_held.pop(fields["note"], None)
    end_rel = end_frame - start_frame
    for note in still_held:
        out_events.append((end_rel, "note_off", {"note": note}))

    write_midi_file(out_events, sample_rate, out_path)
