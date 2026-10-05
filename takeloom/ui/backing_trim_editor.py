"""Backing-track trim editor — the bottom of the Completed Takes tab's left
pane, under the mixer (ui/song_mixer.py). Non-destructively crops a long
intro/outro off the loaded song: move the start later or the end earlier
(or back again) and the mixer's player hears the new window immediately
(AudioPlayerBar.set_window — it keeps the full, untrimmed takes loaded).
"Save trim" stores it via backend.edit_backing_track, which every take of
the song and every future session loading it then plays back through. No
audio file is ever changed.

Replaces the old "Edit backing track..." dialog on each take row.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable

from ..backend import Backend, BackendError
from .audio_player import AudioPlayerBar

_NUDGE_SECONDS = 0.5
_MIN_LENGTH_SECONDS = 1.0  # never let start and end meet
_PREVIEW_END_SECONDS = 5.0  # "Preview end" plays this much before the end point


def _fmt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


class BackingTrimEditor(ttk.Frame):
    def __init__(
        self, master: tk.Misc, get_backend: Callable[[], Backend], player: AudioPlayerBar,
        on_saved: Callable[[float, float], None],
    ) -> None:
        super().__init__(master)
        self._get_backend = get_backend
        self._player = player
        self._on_saved_cb = on_saved
        self._take_filename = ""
        self._track_name = ""
        self._full = 0.0
        self._saved = (0.0, 0.0)  # (start, end) — absolute seconds, as last saved/loaded
        self._prev = (0.0, 0.0)  # (start, end) as of the last _changed — tells which handle moved
        self._nudge_buttons: list[ttk.Button] = []
        self._token = 0

        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=(0, 8))
        ttk.Label(self, text="Backing track", font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        ttk.Label(
            self, text="Crop a long intro/outro. Non-destructive — applies to every take of this song and any "
                       "future session that loads it.",
            foreground="#666666", wraplength=420, justify="left",
        ).pack(anchor="w", pady=(2, 6))

        self.start_var = tk.DoubleVar(value=0.0)
        self.end_var = tk.DoubleVar(value=0.0)
        self._start_label_var = tk.StringVar(value="Start")
        self._end_label_var = tk.StringVar(value="End")
        self._start_scale = self._build_row(self._start_label_var, self.start_var, self._nudge_start)
        self._end_scale = self._build_row(self._end_label_var, self.end_var, self._nudge_end)

        previews = ttk.Frame(self)
        previews.pack(fill="x", pady=(4, 0))
        self._preview_start = ttk.Button(previews, text="▶ Preview start", command=self._on_preview_start)
        self._preview_start.pack(side="left")
        self._preview_end = ttk.Button(previews, text="▶ Preview end", command=self._on_preview_end)
        self._preview_end.pack(side="left", padx=(6, 0))

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(8, 0))
        self._save_button = ttk.Button(buttons, text="Save trim", command=self._on_save)
        self._save_button.pack(side="left")
        self._revert_button = ttk.Button(buttons, text="Revert", command=self._on_revert)
        self._revert_button.pack(side="left", padx=(6, 0))
        self._clear_button = ttk.Button(buttons, text="No trim", command=self._on_clear)
        self._clear_button.pack(side="left", padx=(6, 0))

        self._note_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._note_var, foreground="#9a6a00", wraplength=420, justify="left").pack(
            anchor="w", pady=(6, 0)
        )
        self._status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._status_var, foreground="#666666", wraplength=420, justify="left").pack(
            anchor="w", pady=(2, 0)
        )
        self._set_enabled(False)

    def _build_row(self, label_var: tk.StringVar, var: tk.DoubleVar, nudge: Callable[[float], None]) -> ttk.Scale:
        ttk.Label(self, textvariable=label_var).pack(anchor="w", pady=(4, 0))
        row = ttk.Frame(self)
        row.pack(fill="x")
        back = ttk.Button(row, text="◀", width=2, command=lambda: nudge(-_NUDGE_SECONDS))
        back.pack(side="left")
        scale = ttk.Scale(row, from_=0.0, to=1.0, variable=var, orient="horizontal", command=lambda _v: self._changed())
        scale.pack(side="left", fill="x", expand=True, padx=4)
        forward = ttk.Button(row, text="▶", width=2, command=lambda: nudge(_NUDGE_SECONDS))
        forward.pack(side="left")
        self._nudge_buttons += [back, forward]
        return scale

    # --- song ---

    def clear(self) -> None:
        self._token += 1
        self._take_filename = ""
        self._status_var.set("")
        self._note_var.set("")
        self._set_enabled(False)

    def set_song(self, track_name: str, takes: list[dict], full_length_seconds: float) -> None:
        """Show `track_name`'s current trim. `full_length_seconds` is the
        loaded takes' untrimmed length — used only if the backend doesn't
        know the backing track's own (backing_duration_seconds)."""
        self._token += 1
        self._track_name = track_name
        self._take_filename = takes[0]["filename"]
        self._full = takes[0].get("backing_duration_seconds") or full_length_seconds
        trim_start = takes[0].get("trim_start_seconds", 0.0)
        trim_end = takes[0].get("trim_end_seconds", 0.0)
        self._saved = (trim_start, max(self._full - trim_end, trim_start + _MIN_LENGTH_SECONDS))
        for scale in (self._start_scale, self._end_scale):
            scale.configure(to=self._full)
        self.start_var.set(self._saved[0])
        self.end_var.set(self._saved[1])
        self._prev = self._saved

        sidecar = [t for t in takes if t.get("has_video") or t.get("has_midi")]
        if sidecar:
            kinds = ", ".join(sorted({"video" if t.get("has_video") else "MIDI" for t in sidecar}))
            self._note_var.set(
                f"Note: {len(sidecar)} take(s) have a {kinds} file — only audio playback reflects the trim, "
                "so reviewing the video/MIDI directly still includes the untrimmed intro/outro."
            )
        else:
            self._note_var.set("")
        self._status_var.set("")
        self._set_enabled(True)
        self._changed()

    # --- editing ---

    def _changed(self) -> None:
        start, end = self.start_var.get(), self.end_var.get()
        # Keep at least _MIN_LENGTH_SECONDS between the two; whichever
        # handle is being dragged pushes against the other, not through it.
        if end - start < _MIN_LENGTH_SECONDS:
            if start != self._prev[0]:
                start = max(0.0, end - _MIN_LENGTH_SECONDS)
                self.start_var.set(start)
            else:
                end = min(self._full, start + _MIN_LENGTH_SECONDS)
                self.end_var.set(end)
        self._prev = (start, end)
        self._start_label_var.set(f"Start  {_fmt(start)}")
        self._end_label_var.set(f"End  {_fmt(end)}  (−{_fmt(self._full - end)})")
        self._player.set_window(start, end)
        dirty = (round(start, 2), round(end, 2)) != (round(self._saved[0], 2), round(self._saved[1], 2))
        if dirty:
            self._status_var.set(f"Unsaved — plays {_fmt(end - start)} of {_fmt(self._full)}")
        elif not self._status_var.get().startswith("Saved"):
            self._status_var.set(f"Plays {_fmt(end - start)} of {_fmt(self._full)}")

    def _nudge_start(self, delta: float) -> None:
        self.start_var.set(min(max(self.start_var.get() + delta, 0.0), self._full))
        self._changed()

    def _nudge_end(self, delta: float) -> None:
        self.end_var.set(min(max(self.end_var.get() + delta, 0.0), self._full))
        self._changed()

    def _on_preview_start(self) -> None:
        self._player.play_from(self.start_var.get())

    def _on_preview_end(self) -> None:
        self._player.play_from(max(self.start_var.get(), self.end_var.get() - _PREVIEW_END_SECONDS))

    def _on_revert(self) -> None:
        self.start_var.set(self._saved[0])
        self.end_var.set(self._saved[1])
        self._prev = self._saved
        self._changed()

    def _on_clear(self) -> None:
        self.start_var.set(0.0)
        self.end_var.set(self._full)
        self._prev = (0.0, self._full)
        self._changed()

    # --- save ---

    def _on_save(self) -> None:
        if not self._take_filename:
            return
        start = round(self.start_var.get(), 2)
        end = round(self.end_var.get(), 2)
        trim_end = round(max(self._full - end, 0.0), 2)
        token = self._token
        filename = self._take_filename
        backend = self._get_backend()
        self._save_button.state(["disabled"])
        self._status_var.set("Saving...")

        def worker() -> None:
            try:
                result, error = backend.edit_backing_track(filename, start, trim_end), None
            except BackendError as e:
                result, error = None, str(e)
            self.after(0, lambda: self._on_saved(token, start, end, trim_end, result, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_saved(
        self, token: int, start: float, end: float, trim_end: float, result: dict | None, error: str | None,
    ) -> None:
        if not self.winfo_exists():
            return
        self._save_button.state(["!disabled"])
        if token != self._token:
            return  # another song was loaded meanwhile; the save itself still happened
        if error or result is None:
            self._status_var.set(f"Could not save trim: {error}")
            return
        self._saved = (start, end)
        count = len(result["affected_takes"])
        self._status_var.set(
            f"Saved — now plays {_fmt(result['new_duration_seconds'])}; "
            f"{count} take{'s' if count != 1 else ''} follow this trim."
        )
        self._on_saved_cb(start, trim_end)

    def _set_enabled(self, enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        for w in (self._start_scale, self._end_scale, self._preview_start, self._preview_end,
                  self._save_button, self._revert_button, self._clear_button, *self._nudge_buttons):
            w.state(state)
