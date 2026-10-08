"""M-Audio Keystation 61es — 61 keys; a Volume slider, Pitch and Mod
wheels. Control names are the labels printed on the keyboard."""

from . import ROLE_VOLUME, Control, KeyboardDriver

DRIVER = KeyboardDriver(
    name="M-Audio Keystation 61es",
    port_names=("Keystation 61es",),
    controls=(
        # Confirmed from takeloom's own log ("sent Control Change 7").
        Control("Volume", "slider", cc=7, role=ROLE_VOLUME),
        Control("Pitch", "wheel", notes="Pitch Bend messages, not a CC. Not used."),
        Control("Mod", "wheel", cc=1, notes="Confirmed (CC 1 logged). Never treated as volume. Not used."),
        Control("Sustain", "pedal", cc=64, notes="Pedal input — always handled (audio/midi_input.py)."),
    ),
)
# No voice control: the Stream Deck shows its Voice key for this keyboard.
