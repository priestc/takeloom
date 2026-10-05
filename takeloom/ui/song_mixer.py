"""Completed Takes tab's mixer panel (the left-hand pane): every take on one
song — plus its backing track — each on its own channel strip with a
volume slider plus Mute/Solo, played together live through an
AudioPlayerBar (ui/audio_player.py) — a slider move is heard immediately,
no re-render.

"Save mix" writes the strips' volumes/mutes into the vault as
mixes/<song>.json (backend.save_song_mix — vault.py's save_song_mix; a
plain RPC over Remote, written by the studio machine like every other
vault file). Loading a song applies its saved mix automatically. Settings
are keyed by instrument label, not take filename, so a mix survives a
take being re-recorded; Solo is a listening aid only and never saved.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable

from ..backend import BackendError
from .app_state import AppState
from .audio_player import AudioPlayerBar
from .backing_trim_editor import BackingTrimEditor
from .instrument_colors import make_label_badge

_MAX_GAIN = 2.0  # slider top = 200%
# Saved-mix key for the backing track's strip — alongside instrument labels
# in mixes/<song>.json's "volumes"/"muted", which can never be this.
_BACKING_KEY = "backing track"


class SongMixer(ttk.Frame):
    def __init__(self, master: tk.Misc, app_state: AppState, on_trim_saved: Callable[[], None]) -> None:
        super().__init__(master)
        self.app_state = app_state
        self._on_trim_saved = on_trim_saved
        self.track_name: str | None = None
        self.trim: tuple[float, float] = (0.0, 0.0)  # the loaded song's trim, see CompletedTakesFrame._on_loaded
        self._load_token = 0
        # One per loaded take: {"key", "label", "gain_var", "mute_var", "solo_var", "pct_var"}
        self._strips: list[dict] = []
        self._saved_state: tuple | None = None  # (volumes, muted) last saved/loaded — for "unsaved changes"
        self._base_status = ""  # status shown when there are no unsaved changes

        # Minimum width — the pane's starting size in the PanedWindow: room
        # for a usable seek bar and several channel strips. Draggable wider.
        ttk.Frame(self, width=460, height=1).pack(anchor="w")
        self._title_var = tk.StringVar(value="Mixer")
        ttk.Label(self, textvariable=self._title_var, font=("TkDefaultFont", 12, "bold"), wraplength=380).pack(
            anchor="w"
        )
        self.player = AudioPlayerBar(self, show_title=False)
        self.player.pack(fill="x", pady=(4, 10))

        self._strips_frame = ttk.Frame(self)
        self._strips_frame.pack(fill="x")
        self._placeholder = ttk.Label(
            self._strips_frame, text="Click a song on the right to load its takes here.", foreground="#666666",
        )
        self._placeholder.pack(anchor="w")

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(10, 0))
        self._save_button = ttk.Button(buttons, text="Save mix", command=self._on_save)
        self._save_button.pack(side="left")
        self._reset_button = ttk.Button(buttons, text="Reset to 100%", command=self._on_reset)
        self._reset_button.pack(side="left", padx=(6, 0))
        self._status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._status_var, foreground="#666666", wraplength=400, justify="left").pack(
            anchor="w", pady=(6, 0)
        )
        self._set_buttons_enabled(False)

        self.trim_editor = BackingTrimEditor(
            self, lambda: self.app_state.backend, self.player, on_saved=self._on_trim_editor_saved,
        )
        self.trim_editor.pack(fill="x", pady=(16, 0))

    # --- loading ---

    def load_song(self, track_name: str, takes: list[dict], play_project: str, autoplay: bool = True) -> None:
        """Fetch a local playable copy of every take in `takes` (one song's
        Completed Takes rows) plus its saved mix, then build a strip per
        take and start playing. A take that can't be made available is
        skipped (the rest of the song still loads) and noted in the status."""
        self._load_token += 1
        token = self._load_token
        self.track_name = track_name
        self.trim = (takes[0].get("trim_start_seconds", 0.0), takes[0].get("trim_end_seconds", 0.0)) if takes else (0.0, 0.0)
        self._title_var.set(track_name)
        self._status_var.set("Loading takes...")
        self.trim_editor.clear()
        self._set_buttons_enabled(False)
        backend = self.app_state.backend

        def worker() -> None:
            loaded: list[tuple[dict, str]] = []
            skipped: list[str] = []
            for take in takes:
                try:
                    path = backend.get_take_playback_path(play_project, take["filename"], take["instrument"])
                    loaded.append((take, path))
                except BackendError as e:
                    skipped.append(f"{take['instrument']}: {e}")
            backing_path = None
            if takes:
                try:
                    backing_path = backend.get_backing_playback_path(takes[0]["filename"])
                except BackendError as e:
                    skipped.append(f"backing track: {e}")
            try:
                mix, mix_error = backend.get_song_mix(track_name), None
            except BackendError as e:
                mix, mix_error = None, str(e)
            self.after(0, lambda: self._on_song_loaded(
                token, track_name, loaded, backing_path, skipped, mix, mix_error, autoplay,
            ))

        threading.Thread(target=worker, daemon=True).start()

    def _on_song_loaded(
        self, token: int, track_name: str, loaded: list[tuple[dict, str]], backing_path: str | None,
        skipped: list[str], mix: dict | None, mix_error: str | None, autoplay: bool,
    ) -> None:
        if token != self._load_token or not self.winfo_exists():
            return
        for child in self._strips_frame.winfo_children():
            child.destroy()
        self._strips = []
        if not loaded:
            self._status_var.set("None of this song's takes are available right now.")
            if skipped:
                self._status_var.set("None of this song's takes are available: " + "; ".join(skipped))
            return

        volumes = (mix or {}).get("volumes", {})
        muted = set((mix or {}).get("muted", []))
        labels = [take["instrument"] for take, _ in loaded]
        for i, (take, _path) in enumerate(loaded):
            # Normally one take per label per song; if a label repeats (the
            # same song filed in two places with different takes), number
            # them so each strip still saves its own setting.
            key = take["instrument"] if labels.count(take["instrument"]) == 1 else f"{take['instrument']} #{i + 1}"
            self._build_strip(
                i + 1, key, lambda parent, label=take["instrument"]: make_label_badge(
                    parent, label, font_size=8, padx=4, pady=1,
                ),
                f"take {take['take_number']}", volumes.get(key, 1.0), key in muted,
            )
        if backing_path is not None:
            # Leftmost on screen (column 0), but last in self._strips and
            # the player's track list — the player resamples everything to
            # the *first* file's rate, which should be a take's (the
            # session rate), not whatever the backing download came as.
            self._build_strip(
                0, _BACKING_KEY, lambda parent: tk.Label(
                    parent, text="backing", bg="#444444", fg="white",
                    font=("TkDefaultFont", 8, "bold"), padx=4, pady=1,
                ),
                "track", volumes.get(_BACKING_KEY, 1.0), _BACKING_KEY in muted,
            )
        self._saved_state = self._current_state() if mix else None

        notes = []
        if mix:
            notes.append(f"Saved mix applied ({time.strftime('%b %-d, %-I:%M %p', time.localtime(mix['saved_at']))})"
                         if mix.get("saved_at") else "Saved mix applied")
        elif mix_error:
            notes.append(f"Couldn't read saved mix: {mix_error}")
        else:
            notes.append("No saved mix yet")
        if skipped:
            notes.append("skipped " + "; ".join(skipped))
        self._base_status = " — ".join(notes)
        self._status_var.set(self._base_status)
        self._set_buttons_enabled(True)

        loaded_takes = [take for take, _path in loaded]
        paths = [path for _take, path in loaded] + ([backing_path] if backing_path is not None else [])
        self.player.load_tracks(
            paths, track_name, gains=self._effective_gains(),
            trim_start_seconds=self.trim[0], trim_end_seconds=self.trim[1], autoplay=autoplay,
            on_loaded=lambda full: self._on_audio_loaded(token, track_name, loaded_takes, full),
        )

    def _on_audio_loaded(self, token: int, track_name: str, takes: list[dict], full_length: float) -> None:
        if token == self._load_token:
            self.trim_editor.set_song(track_name, takes, full_length)

    def _on_trim_editor_saved(self, trim_start: float, trim_end: float) -> None:
        # The player is already playing the new window (the editor moved
        # it live), so just record it — CompletedTakesFrame's reload then
        # sees the loaded song's trim already matches and doesn't reload it.
        self.trim = (trim_start, trim_end)
        self._on_trim_saved()

    def _build_strip(
        self, column: int, key: str, make_badge: Callable[[tk.Misc], tk.Widget], caption: str,
        gain: float, muted: bool,
    ) -> None:
        strip = ttk.Frame(self._strips_frame)
        strip.grid(row=0, column=column, sticky="ns", padx=(0, 14))
        self._strips_frame.rowconfigure(0, weight=1)

        pct_var = tk.StringVar()
        gain_var = tk.DoubleVar(value=min(max(gain, 0.0), _MAX_GAIN))
        mute_var = tk.BooleanVar(value=muted)
        solo_var = tk.BooleanVar(value=False)
        entry = {"key": key, "gain_var": gain_var, "mute_var": mute_var, "solo_var": solo_var, "pct_var": pct_var}

        ttk.Label(strip, textvariable=pct_var, width=5, anchor="center").pack()
        scale = ttk.Scale(
            strip, from_=_MAX_GAIN, to=0.0, orient="vertical", variable=gain_var, length=180,
            command=lambda _v: self._on_strip_changed(),
        )
        scale.pack(pady=(2, 4))
        # Double-click snaps back to 100% — a fader's usual "unity" shortcut.
        scale.bind("<Double-Button-1>", lambda _e: (gain_var.set(1.0), self._on_strip_changed()))
        ttk.Checkbutton(strip, text="Mute", variable=mute_var, command=self._on_strip_changed).pack(anchor="w")
        ttk.Checkbutton(strip, text="Solo", variable=solo_var, command=self._on_strip_changed).pack(anchor="w")
        make_badge(strip).pack(pady=(4, 0))
        ttk.Label(strip, text=caption, foreground="#666666").pack()

        self._strips.append(entry)
        self._update_pct(entry)

    # --- strip state ---

    def _update_pct(self, entry: dict) -> None:
        entry["pct_var"].set(f"{round(entry['gain_var'].get() * 100)}%")

    def _effective_gains(self) -> list[float]:
        any_solo = any(s["solo_var"].get() for s in self._strips)
        gains = []
        for s in self._strips:
            silent = s["mute_var"].get() or (any_solo and not s["solo_var"].get())
            gains.append(0.0 if silent else float(s["gain_var"].get()))
        return gains

    def _current_state(self) -> tuple:
        volumes = tuple(sorted((s["key"], round(s["gain_var"].get(), 3)) for s in self._strips))
        muted = tuple(sorted(s["key"] for s in self._strips if s["mute_var"].get()))
        return volumes, muted

    def _on_strip_changed(self) -> None:
        for s in self._strips:
            self._update_pct(s)
        self.player.set_gains(self._effective_gains())
        dirty = self._current_state() != self._saved_state
        self._status_var.set("Unsaved changes" if dirty else self._base_status)

    def _on_reset(self) -> None:
        for s in self._strips:
            s["gain_var"].set(1.0)
            s["mute_var"].set(False)
            s["solo_var"].set(False)
        self._on_strip_changed()

    # --- save ---

    def _on_save(self) -> None:
        if not self.track_name or not self._strips:
            return
        track_name = self.track_name
        token = self._load_token
        volumes = {s["key"]: round(float(s["gain_var"].get()), 3) for s in self._strips}
        muted = [s["key"] for s in self._strips if s["mute_var"].get()]
        state = self._current_state()
        self._status_var.set("Saving...")
        self._save_button.state(["disabled"])
        backend = self.app_state.backend

        def worker() -> None:
            try:
                result, error = backend.save_song_mix(track_name, volumes, muted), None
            except BackendError as e:
                result, error = None, str(e)
            self.after(0, lambda: self._on_saved(token, state, result, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_saved(self, token: int, state: tuple, result: dict | None, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self._save_button.state(["!disabled"])
        if token != self._load_token:
            return  # a different song got loaded meanwhile; the save itself still happened
        if error or result is None:
            self._status_var.set(f"Could not save: {error}")
            return
        self._saved_state = state
        self._base_status = f"Mix saved {time.strftime('%-I:%M %p')}"
        self._on_strip_changed()

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for button in (self._save_button, self._reset_button):
            button.state(["!disabled"] if enabled else ["disabled"])
