"""M-Audio Keystation 61es — 61 keys, no knobs."""

from . import ROLE_VOLUME, Control, KeyboardDriver

DRIVER = KeyboardDriver(
    name="M-Audio Keystation 61es",
    port_names=("Keystation 61es",),
    controls=(
        # Confirmed from takeloom's own log ("sent Control Change 7").
        Control("Volume", "fader", cc=7, role=ROLE_VOLUME),
        Control("Modulation", "wheel", cc=1, notes="Confirmed (CC 1 logged). Not used."),
        Control("Pitch bend", "wheel", notes="Pitch Bend messages, not a CC. Not used."),
        Control("Sustain", "pedal", cc=64, notes="Standard sustain jack — always handled (audio/midi_input.py)."),
        Control("Octave − / +", "button", notes="Handled inside the keyboard; sends nothing."),
        Control("Advanced", "button", notes="The keyboard's own edit mode; sends nothing takeloom uses."),
    ),
)
# No voice control: the Stream Deck shows its Voice key for this keyboard.
