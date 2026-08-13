"""SetlistRow: one row in the Record tab's Setlist list — see record.py.

Plain tk widgets (not ttk): ttk's native macOS Aqua theme mostly ignores
background-color styling on ttk Frame/Label, but classic tk widgets respect
.configure(bg=..., fg=...) directly, which is what selection highlighting
here depends on.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

_BG = "#ffffff"
_BG_SELECTED = "#2f6fed"
_FG = "#000000"
_FG_SELECTED = "#ffffff"
_STATS_FG = "#666666"
_STATS_FG_SELECTED = "#dbe6ff"
_SEPARATOR = "#e0e0e0"


class SetlistRow(tk.Frame):
    """A single setlist entry: a large title line (track name, duration,
    and — for the currently detected instrument — a take checkmark), a
    smaller grey stats line below it (per-installed-label take status for
    an ordinary track; match count, and per-label "next up" picks before
    autodetect knows what's being recorded, for an inspiration filter
    slot — see record.py's _track_stats), and an optional highlight line
    below that in slightly bigger text — the filter slot's "next up" pick
    for specifically the currently-detected instrument, once autodetect
    knows one, called out on its own instead of having to be picked out
    of the full per-label list above. Empty/absent for an ordinary track,
    or before autodetect has locked onto anything.

    `index` is this row's position in the setlist at the time it was
    built — record.py rebuilds every row from scratch on each refresh
    (_refresh_setlist), so it's always current. Press/motion/release/
    right-click are forwarded to record.py: press and right-click with
    this index, motion with the raw event, since a drag started on one
    row keeps receiving motion events even once the pointer has moved
    over another (record.py hit-tests the whole row list by y position
    to figure out which row a drag is currently over — see
    RecordFrame._row_index_at_y)."""

    def __init__(
        self,
        master: tk.Misc,
        index: int,
        on_press: Callable[[int], None],
        on_motion: Callable[[tk.Event], None],
        on_release: Callable[[], None],
        on_right_click: Callable[[int, tk.Event], None],
    ) -> None:
        super().__init__(master, bg=_BG, cursor="hand2")
        self.index = index

        self.title_label = tk.Label(
            self, font=("TkDefaultFont", 20, "bold"), bg=_BG, fg=_FG,
            anchor="w", justify="left", wraplength=1,
        )
        self.title_label.pack(fill="x", padx=10, pady=(8, 0), anchor="w")

        self.stats_label = tk.Label(
            self, font=("TkDefaultFont", 10), bg=_BG, fg=_STATS_FG,
            anchor="w", justify="left", wraplength=1,
        )
        self.stats_label.pack(fill="x", padx=10, pady=(1, 0), anchor="w")

        self.highlight_label = tk.Label(
            self, font=("TkDefaultFont", 12, "bold"), bg=_BG, fg=_STATS_FG,
            anchor="w", justify="left", wraplength=1,
        )
        # Not packed here — set_content() packs/unpacks it on demand, since
        # most rows (every ordinary track, and a filter slot before
        # autodetect knows what's being recorded) have nothing to show here
        # and shouldn't reserve blank space for it.

        separator = tk.Frame(self, height=1, bg=_SEPARATOR)
        separator.pack(fill="x", side="bottom")

        for widget in (self, self.title_label, self.stats_label, self.highlight_label):
            widget.bind("<ButtonPress-1>", lambda _e, i=index: on_press(i))
            widget.bind("<B1-Motion>", on_motion)
            widget.bind("<ButtonRelease-1>", lambda _e: on_release())
            widget.bind("<Button-3>", lambda e, i=index: on_right_click(i, e))
            widget.bind("<Button-2>", lambda e, i=index: on_right_click(i, e))
            widget.bind("<Control-Button-1>", lambda e, i=index: on_right_click(i, e))

        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event: object) -> None:
        width = max(1, event.width - 20)  # type: ignore[attr-defined]
        self.title_label.configure(wraplength=width)
        self.stats_label.configure(wraplength=width)
        self.highlight_label.configure(wraplength=width)

    def set_content(self, title: str, stats: str, highlight: str = "") -> None:
        self.title_label.configure(text=title)
        self.stats_label.configure(text=stats)
        if highlight:
            self.highlight_label.configure(text=highlight)
            self.highlight_label.pack(fill="x", padx=10, pady=(0, 8), anchor="w")
            self.stats_label.pack_configure(pady=(1, 0))
        else:
            self.highlight_label.pack_forget()
            # stats_label was packed assuming a highlight line follows it
            # (top padding only, see __init__) — without one, it's the
            # last line in the row and needs the bottom padding itself.
            self.stats_label.pack_configure(pady=(1, 8))

    def set_selected(self, selected: bool) -> None:
        bg = _BG_SELECTED if selected else _BG
        fg = _FG_SELECTED if selected else _FG
        stats_fg = _STATS_FG_SELECTED if selected else _STATS_FG
        self.configure(bg=bg)
        self.title_label.configure(bg=bg, fg=fg)
        self.stats_label.configure(bg=bg, fg=stats_fg)
        self.highlight_label.configure(bg=bg, fg=stats_fg)
