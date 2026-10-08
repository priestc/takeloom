"""USB MIDI input: turns incoming Note On/Off, sustain-pedal (CC64),
volume, and expression (CC11) messages into calls against plain
callables — usually a Synth's own note_on/note_off/set_sustain/
set_channel_volume/set_expression (see audio/synth.py and backend.py's
MIDI branch of instrument-engine construction), but a detection scan
(start_auto_detect_instrument/start_detect_all) instead passes
on_note_on alone and ignores note number/velocity entirely: any note at
all is itself the detection.

"Which CC is volume" varies by keyboard — confirmed in practice: an
Alesis QX25's own labeled volume knob sends CC22, a number with no
standard MIDI meaning at all, and there's no way to enumerate every
manufacturer's own default in advance. So this supports two modes, via
the `volume_cc` constructor param (see keyboard_drivers/, which is where
it actually comes from — each supported keyboard model's driver records
which control is its volume; GENERIC_DRIVER, for any other keyboard, has
none):

- Pinned (volume_cc != 0): *only* that exact CC number drives volume.
  Deterministic — once a keyboard's driver records which control its
  volume knob actually sends, every other control on that same keyboard
  is left alone, no matter what else gets touched.
- Auto (volume_cc == 0, a keyboard with no driver yet): any Control Change that isn't sustain (CC64) or the universal
  modulation wheel (CC1 — too well-established a convention on too many
  keyboards to safely repurpose) is treated as volume-equivalent. A
  reasonable guess for a keyboard nobody's configured yet, but not
  reliable once more than one control might get touched — that's what
  writing a driver for it is for.

CC11 (Expression) always gets its own separate, correct handling
(independent, multiplicative — see Synth.set_expression) regardless of
volume_cc, since unlike an arbitrary assignable knob it's an actual
MIDI/GM standard with a defined meaning of its own. Every distinct CC
number seen is still logged once (never spammed), so it's visible
whether a given control is being treated as volume, and which number
each control sends when writing a driver for a new keyboard.

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
import time
from typing import Callable

_NOTE_ON = 0x90
_NOTE_OFF = 0x80
_CONTROL_CHANGE = 0xB0
_SUSTAIN_CC = 64
_SUSTAIN_THRESHOLD = 64  # >= this counts as "pedal down" — the common MIDI-spec convention
_EXPRESSION_CC = 11   # "Expression" — always its own thing, a real MIDI/GM standard, see module docstring
# The modulation wheel — a near-universal, dedicated physical control on
# any keyboard that has pitch/mod wheels at all, distinct from whatever
# assignable knob/slider its "volume" control happens to be wired to.
# Excluded from the volume catch-all specifically because it's too
# well-established a convention (vibrato/modulation depth) to safely
# repurpose just because this app has no other use for it.
_MODULATION_CC = 1


class _SharedPort:
    """One rtmidi port per MIDI device for the whole process, opened the
    first time any MidiInput wants that device and then never closed —
    every MidiInput on that device is just a subscriber to it.

    Why never closed: rtmidi's close_port() holds the GIL while CoreMIDI's
    MIDIPortDispose waits for any in-flight MIDI callback to return, and
    that callback can't return without the GIL — so closing a port while a
    message is arriving deadlocks the entire process (confirmed live:
    auto-detect tearing its scan down as the detecting note was played
    froze `takeloom server`, Ctrl+C included; closing from another thread
    deadlocked the same way). Detection is *triggered* by playing, so that
    overlap is the normal case, not a rare one. Unsubscribing is a plain
    Python tuple swap instead, with no CoreMIDI call at all."""

    def __init__(self, midi_in) -> None:
        self.midi_in = midi_in
        # Replaced wholesale, never mutated, so dispatch() can iterate it
        # from rtmidi's thread without a lock.
        self.subscribers: tuple = ()

    def dispatch(self, event: tuple, _data: object = None) -> None:
        for subscriber in self.subscribers:
            try:
                subscriber._on_message(event)
            except Exception as e:  # one bad handler mustn't starve the others
                print(f"takeloom: MIDI handler for '{subscriber.device_name}' failed: {e}")


_shared_ports: dict[str, _SharedPort] = {}  # keyed by resolved port name
_shared_ports_lock = threading.Lock()


class MidiUnavailableError(Exception):
    """Raised when python-rtmidi isn't installed, or the named MIDI
    input device can't be opened right now (unplugged, claimed by
    another app, etc.). Message is safe to show to the user."""


def _get_ports(midi_in) -> list[str]:
    """midi_in.get_ports(), retried briefly: CoreMIDI drops a just-
    unplugged port from its count before rtmidi asks for each port's
    name, so a call landing in that instant raises InvalidPortError
    (confirmed live, unplugging a Keystation) instead of returning the
    list as it now is."""
    for attempt in range(3):
        try:
            return midi_in.get_ports()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(0.05)
    return []


def match_port(device_name: str, ports: list[str]) -> int | None:
    """Index of `device_name` in `ports` — exact match first, then a
    case-insensitive substring match, since some backends append a
    changing numeric client id to a port's name between launches (e.g.
    "Keystation Mini 32 (0)" one time, "...(1)" the next). Same
    tolerance as audio.devices.resolve_device."""
    for i, name in enumerate(ports):
        if name == device_name:
            return i
    for i, name in enumerate(ports):
        if device_name.lower() in name.lower():
            return i
    return None


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
        ports = _get_ports(midi_in)
        del midi_in
        return ports
    except Exception:
        return []


class MidiInput:
    """One open USB MIDI input port, dispatching Note On/Off/sustain/
    volume/expression to plain callables on rtmidi's own dedicated
    notification thread — never the realtime audio callback thread, and
    never anything that blocks on that thread (Synth.note_on/note_off/
    set_sustain/set_channel_volume/set_expression only ever briefly
    hold a lock), so a keystroke's audio reaches the engine's very next
    output block with no added buffering of its own."""

    def __init__(
        self,
        device_name: str,
        on_note_on: Callable[[int, int], None] | None = None,
        on_note_off: Callable[[int], None] | None = None,
        on_sustain: Callable[[bool], None] | None = None,
        on_volume: Callable[[int], None] | None = None,
        on_expression: Callable[[int], None] | None = None,
        volume_cc: int = 0,
        ignore_ccs: tuple[int, ...] = (),
        on_control_change: Callable[[int, int], None] | None = None,
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
        self._on_expression = on_expression
        # 0 = auto-detect (see module docstring); otherwise only this
        # exact CC number is ever routed to on_volume.
        self._volume_cc = volume_cc
        # Controls handled elsewhere (a keyboard's voice/backing-pitch
        # knobs — see backend.py's _on_control_change), so never treated
        # as volume here, not even in auto mode, and never logged.
        self._ignore_ccs = frozenset(cc for cc in ignore_ccs if cc)
        # Raw hook: every Control Change as (cc, value), and nothing else
        # done with it — no volume/sustain handling, no logging. What
        # backend.py's always-open knob listener uses.
        self._on_control_change = on_control_change
        self._lock = threading.Lock()
        self._closed = False
        # Every distinct CC number seen so far, logged once each (never
        # repeated) so it's visible which controls a given keyboard
        # actually sends — otherwise which CC ends up driving the
        # volume-equivalent catch-all (see _on_message) would be
        # invisible. Not a general-purpose MIDI monitor: only the
        # number/whether-it's-volume distinction is logged, and only
        # once per number, so a controller streaming continuous CC data
        # doesn't flood the console.
        self._logged_unknown_ccs: set[int] = set()

        try:
            ports = _get_ports(rtmidi.MidiIn())
        except Exception as e:
            raise MidiUnavailableError(f"Could not list MIDI devices: {e}") from e
        index = match_port(device_name, ports)
        if index is None:
            raise MidiUnavailableError(
                f"MIDI device '{device_name}' not found. "
                f"Available: {', '.join(ports) if ports else '(none)'}"
            )
        port_name = ports[index]
        with _shared_ports_lock:
            shared = _shared_ports.get(port_name)
            if shared is None:
                midi_in = rtmidi.MidiIn()
                try:
                    midi_in.open_port(index)
                except Exception as e:
                    raise MidiUnavailableError(f"Could not open MIDI device '{device_name}': {e}") from e
                # Sysex/timing-clock/active-sensing messages are irrelevant
                # here and, for timing clock especially, frequent enough to
                # be worth not even delivering to _on_message.
                midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
                shared = _SharedPort(midi_in)
                midi_in.set_callback(shared.dispatch)
                _shared_ports[port_name] = shared
            self._shared = shared
            shared.subscribers = shared.subscribers + (self,)

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
        elif status == _CONTROL_CHANGE and len(message) >= 3:
            cc, value = message[1], message[2]
            if self._on_control_change is not None:
                self._on_control_change(cc, value)
                return
            if cc in self._ignore_ccs:
                return
            # Sustain and Expression are always their own thing,
            # regardless of volume_cc (see module docstring on why
            # Expression never doubles as volume). Otherwise: pinned
            # mode (self._volume_cc set) means *only* that exact CC
            # counts as volume; auto mode (0, the default) means
            # anything that isn't one of the three reserved controls
            # does.
            is_volume = (
                (cc == self._volume_cc) if self._volume_cc
                else cc not in (_SUSTAIN_CC, _MODULATION_CC, _EXPRESSION_CC)
            )
            if cc not in self._logged_unknown_ccs:
                self._logged_unknown_ccs.add(cc)
                suffix = " — treating as volume." if is_volume and cc not in (_SUSTAIN_CC, _EXPRESSION_CC) else "."
                print(f"takeloom: MIDI device '{self.device_name}' sent Control Change {cc} (value {value}){suffix}")
            if cc == _SUSTAIN_CC:
                if self._on_sustain is not None:
                    self._on_sustain(value >= _SUSTAIN_THRESHOLD)
            elif cc == _EXPRESSION_CC:
                if self._on_expression is not None:
                    self._on_expression(value)
            elif is_volume:
                if self._on_volume is not None:
                    self._on_volume(value)
            # else: the modulation wheel (auto mode), or — in pinned
            # mode — some CC other than the one that's actually
            # configured as this device's volume control. Neither is
            # acted on.

    def stop(self) -> None:
        """Alias for close() — lets a MidiInput sit in the same list of
        "streams" as sd.InputStream objects (backend.py's detect-all/
        auto-detect scans, which call .stop() then .close() on every
        entry uniformly)."""
        self.close()

    def close(self) -> None:
        """Stop delivering this device's messages to this MidiInput's
        callbacks. Safe from any thread, including from inside one of
        those callbacks — the underlying port stays open (see
        _SharedPort for why it's never closed), so this never blocks."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        with _shared_ports_lock:
            self._shared.subscribers = tuple(s for s in self._shared.subscribers if s is not self)
