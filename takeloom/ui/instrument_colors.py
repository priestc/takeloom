"""Shared color coding for instrument *labels* (INSTRUMENT_LABELS — see
config.py), used everywhere a label is shown as its own discrete piece of
UI — a take's "filed under" badge, a Completed Takes row, a Studio Setup
Compressor row, a detect-test instrument row — so the same label reads
the same color at a glance across every tab. Two instruments sharing a
label (a Stratocaster and a Telecaster both "electric-guitar", say) share
this same color too — see config.py's own INSTRUMENT_LABELS docstring
for why label, not full_name, is the meaningful unit here. Not applied
anywhere a label is only a word inside a longer sentence rather than its
own standalone element (e.g. record.py's "Detected (label — full_name)"
status line) — there's no clean way to color one word out of a plain
Label's text.

Always a plain tk.Label badge (make_label_badge below), never a
ttk.Treeview row tag — a Treeview can only color a whole row via tags,
not one cell's text on its own, and the point of a badge is specifically
to color just the label text, not everything next to it (see
completed_takes.py's module docstring, which used to use Treeview row
tags here before switching to plain widgets for exactly this reason)."""

from __future__ import annotations

import tkinter as tk

# One fixed color per current label (config.py's INSTRUMENT_LABELS) —
# same color family for labels of the same rough instrument type (the
# two guitars share green, the two basses share blue), distinct shades
# so they're still tellable apart from each other.
_LABEL_COLORS: dict[str, str] = {
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
_FALLBACK_COLOR = "#616161"


def color_for_label(label: str) -> str:
    return _LABEL_COLORS.get(label, _FALLBACK_COLOR)


def make_label_badge(master: tk.Misc, label: str, font_size: int = 9, padx: int = 6, pady: int = 1) -> tk.Label:
    """A colored "pill" for `label`: solid background in that label's own
    color, bold white text. Plain tk.Label, not ttk — ttk widgets mostly
    ignore background-color styling under macOS's Aqua theme (see
    setlist_row.py's module docstring for the same issue elsewhere in
    this codebase), which a badge whose entire point is its background
    color can't tolerate."""
    return tk.Label(
        master, text=label, font=("TkDefaultFont", font_size, "bold"),
        bg=color_for_label(label), fg="white", padx=padx, pady=pady,
    )
