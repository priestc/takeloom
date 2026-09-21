"""USB MIDI input: turns incoming Note On/Off, sustain-pedal (CC64), and
channel-volume (CC7) messages into calls against plain callables —
usually a Synth's own note_on/note_off/set_sustain/set_channel_volume
(see audio/synth.py and backend.py's MIDI branch of instrument-engine
construction), but a detection scan (start_auto_detect_instrument/
start_detect_all) instead passes on_note_on alone and ignores note
number/velocity entirely: any note at all is itself the detection.

Uses python-rtmidi directly (not e.g. `mido`'s higher-level wrapper)
because a callback-driven port — no polling loop of our own — is what
actually keeps a struck note's latency down to "however fast the OS and
its driver deliver it" rather than however often something happens to
poll, the same "no extra hop" reasoning behind Synth.render() being
called straight from AudioEngine's own audio callback rather than
through some intermediate queue/thread.
"""

from __future__ import annotations

import threading
from typing import Callable

_NOTE_ON = 0x90
_NOTE_OFF = 0x80
_CONTROL_CHANGE = 0xB0
_SUSTAIN_CC = 64
_SUSTAIN_THRESHOLD = 64  # >= this counts as "pedal down" — the common MIDI-spec convention
_VOLUME_CC = 7  # "Channel Volume" — what a keyboard's own physical volume slider/fader sends


class MidiUnavailableError(Exception):
    """Raised when python-rtmidi isn't installed, or the named MIDI
    input device can't be opened right now (unplugged, claimed by
    another app, etc.). Message is safe to show to the user."""


def list_midi_devices() -> list[str]:
    """Every currently visible MIDI input port name. Re-enumerated fresh
    on each call (a throwaway rtmidi.MidiIn is opened just to ask, then
    dropped) — same "no persistent handle needed just to look" shape as
    audio.devices.resolve_device. Returns [] if python-rtmidi isn't
    installed or nothing is visible right now, never raises — mirrors
    LocalBackend.list_audio_devices/list_cameras' own best-effort
    convention, since this backs the same kind of "reload devices"
    dropdown in Studio Setup."""
    try:
        import rtmidi
    except Exception:
        return []
    try:
        midi_in = rtmidi.MidiIn()
        ports = midi_in.get_ports()
        del midi_in
        return ports
    except Exception:
        return []


class MidiInput:
    """One open USB MIDI input port, dispatching Note On/Off/sustain/
    volume to plain callables on rtmidi's own dedicated notification
    thread — never the realtime audio callback thread, and never
    anything that blocks on that thread (Synth.note_on/note_off/
    set_sustain/set_channel_volume only ever briefly
    hold a lock), so a keystroke's audio reaches the engine's very next
    output block with no added buffering of its own."""

    def __init__(
        self,
        device_name: str,
        on_note_on: Callable[[int, int], None] | None = None,
        on_note_off: Callable[[int], None] | None = None,
        on_sustain: Callable[[bool], None] | None = None,
        on_volume: Callable[[int], None] | None = None,
    ) -> None:
        try:
            import rtmidi
        except ImportError as e:
            raise MidiUnavailableError(
                "MIDI support requires the 'python-rtmidi' package, which isn't installed."
            ) from e
        self.device_name = device_name
        self._on_note_on = on_note_on
        self._on_note_off = on_note_off
        self._on_sustain = on_sustain
        self._on_volume = on_volume
        self._lock = threading.Lock()
        self._closed = False

        self._midi_in = rtmidi.MidiIn()
        ports = self._midi_in.get_ports()
        index = None
        for i, name in enumerate(ports):
            if name == device_name:
                index = i
                break
        if index is None:
            # Partial-match fallback, same tolerance as audio.devices.
            # resolve_device — useful since some backends append a
            # changing numeric client id to a port's name between
            # launches (e.g. "Keystation Mini 32 (0)" one time, "...(1)"
            # the next).
            for i, name in enumerate(ports):
                if device_name.lower() in name.lower():
                    index = i
                    break
        if index is None:
            raise MidiUnavailableError(
                f"MIDI device '{device_name}' not found. "
                f"Available: {', '.join(ports) if ports else '(none)'}"
            )
        try:
            self._midi_in.open_port(index)
        except Exception as e:
            raise MidiUnavailableError(f"Could not open MIDI device '{device_name}': {e}") from e
        # Sysex/timing-clock/active-sensing messages are irrelevant here
        # and, for timing clock especially, frequent enough to be worth
        # not even delivering to _on_message.
        self._midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
        self._midi_in.set_callback(self._on_message)

    def start(self) -> None:
        """No-op beyond construction — rtmidi's callback is already live
        the moment __init__ succeeds. Exists so callers (backend.py) can
        treat MidiInput the same shape as the sd.InputStream objects it's
        often opened alongside (construct, then .start(), later .stop()/
        .close())."""

    def _on_message(self, event: tuple, _data: object = None) -> None:
        message, _delta_time = event
        if not message:
            return
        status = message[0] & 0xF0
        if status == _NOTE_ON and len(message) >= 3:
            note, velocity = message[1], message[2]
            if velocity == 0:
                # A zero-velocity Note On is a common alternate spelling
                # of Note Off (running-status-friendly on some
                # controllers) — treat it as one.
                if self._on_note_off is not None:
                    self._on_note_off(note)
            elif self._on_note_on is not None:
                self._on_note_on(note, velocity)
        elif status == _NOTE_OFF and len(message) >= 3:
            if self._on_note_off is not None:
                self._on_note_off(message[1])
        elif status == _CONTROL_CHANGE and len(message) >= 3 and message[1] == _SUSTAIN_CC:
            if self._on_sustain is not None:
                self._on_sustain(message[2] >= _SUSTAIN_THRESHOLD)
        elif status == _CONTROL_CHANGE and len(message) >= 3 and message[1] == _VOLUME_CC:
            if self._on_volume is not None:
                self._on_volume(message[2])

    def stop(self) -> None:
        """Alias for close() — lets a MidiInput sit in the same list of
        "streams" as sd.InputStream objects (backend.py's detect-all/
        auto-detect scans, which call .stop() then .close() on every
        entry uniformly)."""
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._midi_in.cancel_callback()
        except Exception:
            pass
        try:
            self._midi_in.close_port()
        except Exception:
            pass
