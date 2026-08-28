"""On-screen Stream Deck emulator for the Record tab.

Draws the same buttons — icons, labels, colors — the physical Stream Deck
shows for the recording page (see streamdeck_controller.py: RECORDING_
BUTTONS/RECORDING_VOLUME_BUTTONS/RECORDING_IDLE_BUTTONS/RECORDING_
IDENTIFYING_BUTTONS/RECORDING_IDENTIFIED_BUTTONS and the *_visual()
helpers), and drives clicks through the same key characters RecordingDeck
Driver.handle_key() expects, so an on-screen tile behaves exactly like
pressing the corresponding physical key — one set of layout/behavior
tables, two input devices.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from PIL import ImageTk

from ..streamdeck_controller import (
    RECORDING_BUTTONS,
    RECORDING_IDENTIFIED_BUTTONS,
    RECORDING_IDENTIFYING_BUTTONS,
    RECORDING_IDLE_BUTTONS,
    RECORDING_MONITOR_TOGGLE_KEY_INDEX,
    RECORDING_VOLUME_BUTTONS,
    button_visual,
    monitor_toggle_visual,
    recording_toggle_visual,
    render_button_image,
)
from .platform_style import scaled_px

# Rendered pixel size of each key image — outside ttk's styling system, so
# it needs its own Linux scaling (see platform_style.py) rather than
# picking up the ttk font/padding normalization automatically.
_KEY_SIZE = scaled_px(68)
_COLS = 5
_ALL_BUTTONS: list[tuple] = list(RECORDING_BUTTONS) + list(RECORDING_VOLUME_BUTTONS)
# Every key index that shows up in *any* layout (active session or any of
# the three idle-family ones) needs a widget gridded somewhere; _redraw()
# shows/hides them per-key according to whichever table is current, since
# key 2 in particular is shared between an idle-family layout
# (Re-identify) and the active layout (Next) — the old static "idle vs.
# active" partition this used to use doesn't hold once there are three
# idle-family layouts instead of one.
_IDLE_FAMILY_LAYOUTS: dict[str, list[tuple]] = {
    "idle": RECORDING_IDLE_BUTTONS,
    "identifying": RECORDING_IDENTIFYING_BUTTONS,
    "ready": RECORDING_IDENTIFIED_BUTTONS,
}
_ALL_KEY_INDICES: set[int] = {btn[0] for btn in _ALL_BUTTONS} | {
    btn[0] for buttons in _IDLE_FAMILY_LAYOUTS.values() for btn in buttons
}


class StreamDeckEmulator(ttk.Frame):
    """Grid of on-screen keys mirroring the physical Stream Deck's
    recording-page layout. `on_key` is called with the same key characters
    RecordingDeckDriver.handle_key() expects — the Record tab wires it
    straight to that method (with "r"/"s"/"i"/"p" special-cased to the
    existing Start/Identify/Play/Unpause/Stop button flow) so clicking a
    tile does exactly what pressing the real one would."""

    def __init__(self, master: tk.Misc, on_key: Callable[[str], None]) -> None:
        super().__init__(master)
        self._on_key = on_key
        self._phase = "idle"
        self._video_check_phase = "idle"
        self._identify_state = "idle"
        self._monitoring_mode = "production"
        self._images: dict[int, ImageTk.PhotoImage] = {}
        self._buttons: dict[int, tk.Button] = {}

        for idx in sorted(_ALL_KEY_INDICES):
            row, col = divmod(idx, _COLS)
            # A given key index maps to a different key_char depending on
            # which layout it's currently drawn from — look it up fresh at
            # click time rather than baking one in, so one widget can serve
            # e.g. both "Re-identify" (idle-family) and "Next" (active).
            button = tk.Button(
                self, borderwidth=0, highlightthickness=0, relief="flat", cursor="hand2",
                command=lambda i=idx: self._on_key(self._key_char_for(i)),
            )
            button.grid(row=row, column=col, padx=scaled_px(3), pady=scaled_px(3))
            self._buttons[idx] = button

        self._redraw()

    def _key_char_for(self, idx: int) -> str:
        buttons = self._current_buttons()
        for btn in buttons:
            if btn[0] == idx:
                return btn[3]
        return ""

    def _current_buttons(self) -> list[tuple]:
        if self._phase != "idle":
            return _ALL_BUTTONS
        return _IDLE_FAMILY_LAYOUTS.get(self._identify_state, RECORDING_IDLE_BUTTONS)

    def _set_face(self, idx: int, icon: str | None, label: str | None, color: tuple) -> None:
        photo = ImageTk.PhotoImage(render_button_image(icon, label, color, size=_KEY_SIZE))
        self._images[idx] = photo  # keep a reference — Tk drops images with no live Python reference
        self._buttons[idx].configure(image=photo)

    def _redraw(self) -> None:
        buttons = self._current_buttons()
        used = {btn[0] for btn in buttons}
        for idx, button in self._buttons.items():
            if idx in used:
                button.grid()
            else:
                button.grid_remove()

        if self._phase == "idle":
            for btn in buttons:
                idx, icon, label, _key, _active_state, color, _dim_color = btn
                self._set_face(idx, icon, label, color)
            return

        icon, label, color = recording_toggle_visual(self._phase, self._video_check_phase)
        self._set_face(0, icon, label, color)
        for btn in buttons:
            idx = btn[0]
            if idx in (0, RECORDING_MONITOR_TOGGLE_KEY_INDEX):
                continue
            icon, label, color = button_visual(btn, self._phase)
            self._set_face(idx, icon, label, color)
        icon, label, color = monitor_toggle_visual(self._monitoring_mode)
        self._set_face(RECORDING_MONITOR_TOGGLE_KEY_INDEX, icon, label, color)

    def update_recording_page(
        self, phase: str, video_check_phase: str = "idle", identify_state: str = "idle",
    ) -> None:
        self._phase = phase
        self._video_check_phase = video_check_phase
        self._identify_state = identify_state
        self._redraw()

    def update_monitoring_mode(self, mode: str) -> None:
        self._monitoring_mode = mode
        self._redraw()

    def set_key_enabled(self, idx: int, enabled: bool) -> None:
        """Guard the Record key (idx 0) against double-clicks while a start/
        unpause/stop request is in flight — mirrors the old ttk button's
        .state(["disabled"]) during that same window."""
        self._buttons[idx].configure(state=tk.NORMAL if enabled else tk.DISABLED)
