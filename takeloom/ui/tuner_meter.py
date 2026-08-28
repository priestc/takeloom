"""TunerMeter: the Record tab's Tk-native tuner needle, live throughout the
whole auto-detect identify cycle ("identifying" *and* "ready" — see ui/
record.py's _handle_tuner_status) — shows the note currently being heard
and how sharp/flat it is against the playing instrument's configured
string tuning (see audio/pitch.py). Tk equivalent of streamdeck_
controller.py's touchscreen _draw_tuner_needle, mirroring its background-
track/zone-band/needle layout so the two read as the same widget whether
you're looking at the screen or the physical deck.
"""

from __future__ import annotations

import tkinter as tk


class TunerMeter(tk.Canvas):
    _BG = "#1a1a1a"
    _TRACK_BG = "#202020"
    _GREEN = "#00d282"
    _AMBER = "#e6a000"
    _RED = "#d7463c"
    _ZONE_RED = "#462321"
    _ZONE_AMBER = "#46370f"
    _ZONE_GREEN = "#0f4128"
    _END_MARK = "#8c8c8c"
    _CENTER_MARK = "#ebebeb"
    _IDLE_TEXT_COLOR = "#888888"
    _IN_TUNE_CENTS = 5.0
    _WARN_CENTS = 25.0
    _DISPLAY_RANGE_CENTS = 50.0

    def __init__(self, master: tk.Misc, height: int = 46) -> None:
        super().__init__(master, height=height, background=self._BG, highlightthickness=0)
        self._note: str | None = None
        self._cents = 0.0
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_reading(self, note: str, cents: float) -> None:
        """`cents` is expected to already be smoothed by the caller (see
        ui/record.py's _tuner_smoother) — this just draws whatever it's
        given."""
        self._note = note
        self._cents = cents
        self._redraw()

    def clear(self) -> None:
        """Back to "nothing heard yet" — a fresh identify cycle just
        started (see ui/record.py's _begin_identify/_redo_identify), or
        the cycle just ended (a session opened, or this frame reattached
        to a different backend)."""
        self._note = None
        self._redraw()

    def _needle_color(self) -> str:
        if abs(self._cents) <= self._IN_TUNE_CENTS:
            return self._GREEN
        if abs(self._cents) <= self._WARN_CENTS:
            return self._AMBER
        return self._RED

    def _redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1:
            return
        cy = height // 2

        if self._note is None:
            self.create_text(
                width // 2, cy, text="Listening for a note...",
                fill=self._IDLE_TEXT_COLOR, font=("TkDefaultFont", 10),
            )
            return

        color = self._needle_color()
        self.create_text(36, cy, text=self._note, fill=color, font=("TkDefaultFont", 16, "bold"))

        scale_left, scale_right = 72, width - 10
        if scale_right <= scale_left:
            return
        scale_mid = (scale_left + scale_right) // 2
        half_width = scale_right - scale_mid

        def x_at(cents_value: float) -> int:
            frac = max(-1.0, min(1.0, cents_value / self._DISPLAY_RANGE_CENTS))
            return scale_mid + int(frac * half_width)

        # Background track, so the needle reads against something instead
        # of bare canvas.
        track_half_h = 12
        self.create_rectangle(
            scale_left, cy - track_half_h, scale_right, cy + track_half_h,
            fill=self._TRACK_BG, outline="",
        )

        # Red/amber/green zone bands inside the track.
        band_half_h = 8
        green_l, green_r = x_at(-self._IN_TUNE_CENTS), x_at(self._IN_TUNE_CENTS)
        warn_l, warn_r = x_at(-self._WARN_CENTS), x_at(self._WARN_CENTS)
        self.create_rectangle(scale_left, cy - band_half_h, warn_l, cy + band_half_h, fill=self._ZONE_RED, outline="")
        self.create_rectangle(warn_l, cy - band_half_h, green_l, cy + band_half_h, fill=self._ZONE_AMBER, outline="")
        self.create_rectangle(green_l, cy - band_half_h, green_r, cy + band_half_h, fill=self._ZONE_GREEN, outline="")
        self.create_rectangle(green_r, cy - band_half_h, warn_r, cy + band_half_h, fill=self._ZONE_AMBER, outline="")
        self.create_rectangle(warn_r, cy - band_half_h, scale_right, cy + band_half_h, fill=self._ZONE_RED, outline="")

        # End markers (±_DISPLAY_RANGE_CENTS) and a taller/brighter center
        # mark (dead in tune — the "aim for here" reference).
        self.create_line(scale_left, cy - track_half_h, scale_left, cy + track_half_h, fill=self._END_MARK, width=2)
        self.create_line(scale_right, cy - track_half_h, scale_right, cy + track_half_h, fill=self._END_MARK, width=2)
        self.create_line(
            scale_mid, cy - track_half_h - 5, scale_mid, cy + track_half_h + 5, fill=self._CENTER_MARK, width=3,
        )

        needle_x = x_at(self._cents)
        self.create_line(needle_x, cy - track_half_h - 7, needle_x, cy + track_half_h + 7, fill=color, width=4)
