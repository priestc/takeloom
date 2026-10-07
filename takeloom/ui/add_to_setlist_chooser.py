"""First step of the Record tab's "Add to Setlist" button: pick whether
you're adding one song (AddToSetlistDialog — file, YouTube, inspiration
track) or a set of songs (SongSetDialog — a fixed
list each session draws from)."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from ..backend import Backend
from .add_to_setlist_dialog import AddToSetlistDialog
from .song_set_dialog import SongSetDialog


class AddToSetlistChooser(tk.Toplevel):
    def __init__(
        self, master: tk.Misc, backend: Backend, project_name: str, on_track_added: Callable[[], None],
    ) -> None:
        super().__init__(master)
        self.title("Add to Setlist")
        self.resizable(False, False)
        self.transient(master)
        self._master = master
        self._backend = backend
        self._project_name = project_name
        self._on_track_added = on_track_added

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Adding to: {project_name}", font=("TkDefaultFont", 11, "bold")).pack(
            anchor="w", pady=(0, 12)
        )

        for label, description, command in (
            ("Single song", "One song: from a file, a YouTube URL, or the inspiration server.", self._on_single),
            ("Set of songs", "A list of songs you pick — one at a time, or every song matching a "
                             "filter. Each session draws one song at random from the set.", self._on_set),
        ):
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=4)
            ttk.Button(row, text=label, width=14, command=command).pack(side="left", anchor="n")
            ttk.Label(row, text=description, foreground="#666666", wraplength=300, justify="left").pack(
                side="left", padx=(10, 0)
            )

        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Button(footer, text="Cancel", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda _e: self.destroy())

    def _on_single(self) -> None:
        self.destroy()
        AddToSetlistDialog(self._master, self._backend, self._project_name, self._on_track_added)

    def _on_set(self) -> None:
        self.destroy()
        backend, project_name = self._backend, self._project_name
        SongSetDialog(
            self._master, backend, title=f"Add Song Set — {project_name}",
            commit=lambda name, songs: backend.add_song_set_slot(project_name, name, songs),
            on_saved=lambda _name, _songs: self._on_track_added(),
            save_label="Add to Setlist",
        )
