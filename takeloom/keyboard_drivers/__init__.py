"""MIDI keyboard drivers: the technical details of each physical control on
each supported keyboard model — what MIDI message it sends, and which
takeloom feature (if any) it drives.

Every keyboard wires its controls differently, and there's no way to ask a
keyboard what its knobs mean: the Alesis QX25's volume knob sends CC 22, a
number with no standard MIDI meaning, while the Keystation 61es's volume
fader sends the standard CC 7. So each supported model gets a driver here,
matched to an Instrument by its MIDI port name (config.Instrument.
midi_device) — see driver_for(). A keyboard with no driver still works
through GENERIC_DRIVER: notes, sustain and expression always do, and
volume falls back to audio/midi_input.py's "any unclaimed CC" guess.

Adding a keyboard is a developer task, not a setup one — there's
deliberately no UI for any of this:

1. Plug it in, start `takeloom server`, and move each control once —
   audio/midi_input.py logs every Control Change number the first time
   it's seen.
2. Add a module next to these describing every control (KeyboardDriver
   below), marking which ones takeloom should act on via `role`.
3. Add it to DRIVERS.
"""

from __future__ import annotations

from dataclasses import dataclass

# What a control can be used for in takeloom. A control with no role is
# still listed in its driver (so the driver is a complete description of
# the hardware), it just isn't acted on.
ROLE_VOLUME = "volume"                # the synth's volume (see Synth.set_channel_volume)
ROLE_VOICE = "voice"                  # picks the synth voice by knob position
ROLE_BACKING_PITCH = "backing_pitch"  # tunes the session's backing track (backend.set_backing_pitch)


@dataclass(frozen=True)
class Control:
    """One physical control. `cc` is the MIDI Control Change number it
    sends, or None for a control that sends something else (notes, pitch
    bend) or nothing at all (a button the keyboard handles internally,
    like octave shift)."""
    name: str
    kind: str  # "knob" | "fader" | "wheel" | "button" | "pad" | "pedal"
    cc: int | None = None
    role: str | None = None
    notes: str = ""


@dataclass(frozen=True)
class KeyboardDriver:
    name: str
    # Matched case-insensitively as substrings of the MIDI port name, which
    # is what config.Instrument.midi_device holds.
    port_names: tuple[str, ...]
    controls: tuple[Control, ...] = ()

    def cc_for(self, role: str) -> int:
        """The CC number of the control with `role`, or 0 if this keyboard
        has none — the same "0 = not available" every caller already
        checks for."""
        for control in self.controls:
            if control.role == role and control.cc is not None:
                return control.cc
        return 0

    @property
    def volume_cc(self) -> int:
        """0 = no known volume control: audio/midi_input.py then guesses."""
        return self.cc_for(ROLE_VOLUME)

    @property
    def voice_cc(self) -> int:
        """0 = no voice control on the keyboard itself, so the Stream Deck
        shows its own Voice key instead (backend.get_voice_switch)."""
        return self.cc_for(ROLE_VOICE)

    @property
    def backing_pitch_cc(self) -> int:
        return self.cc_for(ROLE_BACKING_PITCH)

    @property
    def handled_ccs(self) -> tuple[int, ...]:
        """CCs handled outside the synth's own MIDI input (voice, backing
        pitch) — never to be mistaken for volume there."""
        return tuple(cc for cc in (self.voice_cc, self.backing_pitch_cc) if cc)


GENERIC_DRIVER = KeyboardDriver(name="Generic MIDI keyboard", port_names=())

from .alesis_qx25 import DRIVER as _ALESIS_QX25  # noqa: E402
from .m_audio_keystation_61es import DRIVER as _M_AUDIO_KEYSTATION_61ES  # noqa: E402

DRIVERS: tuple[KeyboardDriver, ...] = (
    _ALESIS_QX25,
    _M_AUDIO_KEYSTATION_61ES,
)


def driver_for(midi_device: str) -> KeyboardDriver:
    """The driver for a keyboard by its MIDI port name, or GENERIC_DRIVER."""
    name = midi_device.lower()
    for driver in DRIVERS:
        if any(port.lower() in name for port in driver.port_names):
            return driver
    return GENERIC_DRIVER
