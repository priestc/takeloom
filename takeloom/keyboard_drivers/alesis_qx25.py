"""Alesis QX25 — 25 keys, 8 knobs (K1-K8), 8 pads."""

from . import ROLE_BACKING_PITCH, ROLE_VOICE, ROLE_VOLUME, Control, KeyboardDriver

# UNVERIFIED: K1/K2 are assumed to follow the QX25's factory knob numbering
# (K1-K8 = CC 20-27), consistent with the one knob confirmed so far (CC 22,
# used as volume). Confirm by moving each knob with `takeloom server`
# running — every new CC number is logged the first time it's seen — and
# correct the numbers here if they differ.
DRIVER = KeyboardDriver(
    name="Alesis QX25",
    port_names=("QX25",),
    controls=(
        Control("K1", "knob", cc=20, role=ROLE_VOICE,
                notes="Left half of its travel = piano, right half = organ."),
        Control("K2", "knob", cc=21, role=ROLE_BACKING_PITCH,
                notes="Backing track tuning: full travel = -50..+50 cents, centre = untouched."),
        # Confirmed in practice (logged as CC 22).
        Control("K3", "knob", cc=22, role=ROLE_VOLUME),
        Control("K4", "knob", cc=23),
        Control("K5", "knob", cc=24),
        Control("K6", "knob", cc=25),
        Control("K7", "knob", cc=26),
        Control("K8", "knob", cc=27),
        Control("Pads 1-8", "pad", notes="Send notes, not CCs — played like keys."),
        Control("Modulation", "wheel", cc=1, notes="Not used."),
        Control("Pitch bend", "wheel", notes="Pitch Bend messages, not a CC. Not used."),
        Control("Sustain", "pedal", cc=64, notes="Standard sustain jack — always handled (audio/midi_input.py)."),
        Control("Octave − / +", "button", notes="Handled inside the keyboard; sends nothing."),
        Control("Transport buttons", "button", notes="Not used."),
    ),
)
