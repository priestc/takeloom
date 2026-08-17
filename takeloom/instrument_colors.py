"""Shared color coding for instrument *labels* (INSTRUMENT_LABELS — see
config.py) — the actual color table lives here, dependency-free (no
tkinter, no PIL), so it can be used both by ui/instrument_colors.py's
tk.Label badges (Sessions/Completed Takes/Studio Setup/detect-test — see
that module) and by streamdeck_controller.py's PIL-rendered touchscreen,
which has to stay usable in headless `takeloom server` with no Tk runtime
at all.

Two instruments sharing a label (a Stratocaster and a Telecaster both
"electric-guitar", say) share this same color too — see config.py's own
INSTRUMENT_LABELS docstring for why label, not full_name, is the
meaningful unit here.
"""

from __future__ import annotations

# One fixed color per current label (config.py's INSTRUMENT_LABELS) —
# same color family for labels of the same rough instrument type (the
# two guitars share green, the two basses share blue), distinct shades
# so they're still tellable apart from each other.
LABEL_COLORS: dict[str, str] = {
    "piano": "#C62828",
    "organ": "#6A1B9A",
    "acoustic-guitar": "#2E7D32",
    "electric-guitar": "#43A047",
    "electric-bass": "#1565C0",
    "electric-bass-fretless": "#0D47A1",
    "drums": "#E65100",
}
# An unrecognized/legacy label (predates today's INSTRUMENT_LABELS
# vocabulary, or was hand-edited in) — never a real current choice, but
# take data can outlive a label's removal, so this keeps that case
# visibly distinct (flat gray) rather than crashing or guessing.
FALLBACK_COLOR = "#616161"


def color_for_label(label: str) -> str:
    """`label`'s color as a "#rrggbb" hex string — tk.Label's bg=
    already takes this form directly (ui/instrument_colors.py); PIL
    drawing calls need hex_to_rgb() below instead."""
    return LABEL_COLORS.get(label, FALLBACK_COLOR)


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """"#rrggbb" -> (r, g, b), 0-255 each — what PIL's ImageDraw fill=/
    outline= wants (streamdeck_controller.py), unlike tk.Label's bg=,
    which takes the hex string as-is."""
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
