"""Alesis QX25 — 25 keys; knobs K1-K8, S1, P1-P4, Pitch and Mod wheels.
Control names are the labels printed on the keyboard."""

from . import ROLE_BACKING_PITCH, ROLE_VOICE, ROLE_VOLUME, Control, KeyboardDriver

# CC numbers confirmed on the hardware, MIDI channel 1: K1-K8 send CC 14-21
# in order, and S1 sends CC 22.
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
        Control("S1", "knob", cc=22, role=ROLE_VOLUME),
        Control("P1", "pad", notes="Message not measured yet. Not used."),
        Control("P2", "pad", notes="Message not measured yet. Not used."),
        Control("P3", "pad", notes="Message not measured yet. Not used."),
        Control("P4", "pad", notes="Message not measured yet. Not used."),
        Control("Pitch", "wheel", notes="Pitch Bend messages, not a CC. Not used."),
        Control("Mod", "wheel", cc=1, notes="Standard modulation CC; never treated as volume. Not used."),
        Control("Sustain", "pedal", cc=64, notes="Pedal input — always handled (audio/midi_input.py)."),
    ),
)
