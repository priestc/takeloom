"""Alesis QX25 — 25 keys, 8 knobs (K1-K8), a volume knob, 8 pads."""

from . import ROLE_BACKING_PITCH, ROLE_VOICE, ROLE_VOLUME, Control, KeyboardDriver

# CC numbers confirmed on the hardware, MIDI channel 1: K1-K8 send CC 14-21
# in order, and the separate volume knob sends CC 22.
DRIVER = KeyboardDriver(
    name="Alesis QX25",
    port_names=("QX25",),
    controls=(
        Control("K1", "knob", cc=14, role=ROLE_VOICE,
                notes="Left half of its travel = piano, right half = organ."),
        Control("K2", "knob", cc=15, role=ROLE_BACKING_PITCH,
                notes="Backing track tuning: full travel = -50..+50 cents, centre = untouched."),
        Control("K3", "knob", cc=16),
        Control("K4", "knob", cc=17),
        Control("K5", "knob", cc=18),
        Control("K6", "knob", cc=19),
        Control("K7", "knob", cc=20),
        Control("K8", "knob", cc=21),
        Control("Volume", "knob", cc=22, role=ROLE_VOLUME),
        Control("Pads 1-8", "pad", notes="Send notes, not CCs — played like keys."),
        Control("Modulation", "wheel", cc=1, notes="Not used."),
        Control("Pitch bend", "wheel", notes="Pitch Bend messages, not a CC. Not used."),
        Control("Sustain", "pedal", cc=64, notes="Standard sustain jack — always handled (audio/midi_input.py)."),
        Control("Octave − / +", "button", notes="Handled inside the keyboard; sends nothing."),
        Control("Transport buttons", "button", notes="Not used."),
    ),
)
