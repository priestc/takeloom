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
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

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


def configure_label_tags(tree: ttk.Treeview, font_size: int = 10) -> None:
    """Register one Treeview tag per known label (tag name == the label
    itself), styled the same badge way (colored background, bold white
    text) — unlike a plain tk.Label, a Treeview row's color can only be
    set via tag_configure, applied to the whole row rather than one
    cell. Confirmed (unlike ttk.Frame/Label background styling — see
    make_label_badge) that Treeview tag backgrounds *do* render under
    Aqua. Call once after creating `tree`; callers then pass
    tags=(label,) to insert() for any row that should be colored by
    label — an unrecognized tag (nothing configured) just falls back to
    the Treeview's normal, unstyled row."""
    for label, color in _LABEL_COLORS.items():
        tree.tag_configure(label, background=color, foreground="white", font=("TkDefaultFont", font_size, "bold"))
    tree.tag_configure(
        "_unknown_label", background=_FALLBACK_COLOR, foreground="white", font=("TkDefaultFont", font_size, "bold"),
    )


def label_tag(label: str) -> str:
    """The tag name to pass to a Treeview row's tags=(...,) for `label`
    — configure_label_tags() must have been called on that tree first.
    Falls back to the shared "unknown label" tag for anything outside
    today's INSTRUMENT_LABELS, same reasoning as color_for_label."""
    return label if label in _LABEL_COLORS else "_unknown_label"
