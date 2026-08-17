"""tk.Label badge rendering for instrument *labels* — the actual color
table lives in the top-level instrument_colors.py (shared with
streamdeck_controller.py's PIL-rendered touchscreen, which can't depend
on tkinter — see that module's docstring). Used everywhere a label is
shown as its own discrete piece of UI — a take's "filed under" badge, a
Completed Takes row, a Studio Setup Compressor row, a detect-test
instrument row — so the same label reads the same color at a glance
across every tab. Not applied anywhere a label is only a word inside a
longer sentence rather than its own standalone element (e.g. record.py's
"Detected (label — full_name)" status line) — there's no clean way to
color one word out of a plain Label's text.

Always a plain tk.Label badge, never a ttk.Treeview row tag — a Treeview
can only color a whole row via tags, not one cell's text on its own, and
the point of a badge is specifically to color just the label text, not
everything next to it (see completed_takes.py's module docstring, which
used to use Treeview row tags here before switching to plain widgets for
exactly this reason)."""

from __future__ import annotations

import tkinter as tk

from ..instrument_colors import color_for_label

__all__ = ["color_for_label", "make_label_badge"]


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
