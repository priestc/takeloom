"""Alesis QX25 — 25 keys; knobs K1-K8, slider S1, P1-P4, Pitch and Mod wheels.
Control names are the labels printed on the keyboard."""

from . import ROLE_BACKING_PITCH, ROLE_VOICE, ROLE_VOLUME, Control, KeyboardDriver

# Confirmed on the hardware, everything on MIDI channel 1: K1-K8 send CC
# 14-21 in order, S1 sends CC 22, P1-P4 send notes 48-51, Mod sends CC 1,
# Pitch sends pitch bend.
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
        Control("S1", "slider", cc=22, role=ROLE_VOLUME),
        Control("P1", "pad", notes="Sends note 48 (C3), velocity-sensitive."),
        Control("P2", "pad", notes="Sends note 49 (C#3), velocity-sensitive."),
        Control("P3", "pad", notes="Sends note 50 (D3), velocity-sensitive."),
        Control("P4", "pad", notes="Sends note 51 (D#3), velocity-sensitive."),
        Control("Pitch", "wheel", notes="Pitch Bend messages (confirmed), not a CC. Not used."),
        Control("Mod", "wheel", cc=1, notes="CC 1 (confirmed); never treated as volume. Not used."),
        Control("Sustain", "pedal", cc=64, notes="Pedal input — always handled (audio/midi_input.py)."),
    ),
)
