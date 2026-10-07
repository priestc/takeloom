"""Inspiration filter criteria fields (artist/genre/year range/length
range) — the song set builder's "Add from filter" tab (ui/song_set_
dialog.py).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from ..backend import Backend


class FilterCriteriaFields:
    """Builds Artist/Genre/Year range/Length range widgets into `parent`
    starting at grid row `start_row`. `get_criteria()` validates and
    returns a filter_criteria dict (or None, having shown an error
    dialog itself) in exactly the shape inspiration.search_tracks_by_
    filter expects. `dialog_parent`
    is the Toplevel error dialogs should be modal to — not necessarily
    `parent` itself, which is just a content frame."""

    def __init__(self, parent: tk.Misc, backend: Backend, dialog_parent: tk.Misc, start_row: int = 0) -> None:
        self._backend = backend
        self._dialog_parent = dialog_parent

        from .add_to_setlist_dialog import _AutocompleteEntry

        def fetch_filter_artists(text: str) -> list[tuple[str, None]]:
            return [(name, None) for name in self._backend.search_inspiration_artists(text)]

        row = start_row
        ttk.Label(parent, text="Artist").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.artist_field = _AutocompleteEntry(parent, fetch=fetch_filter_artists)
        self.artist_field.grid(row=row, column=1, sticky="ew")
        row += 1

        ttk.Label(parent, text="Genre").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.genre_var = tk.StringVar()
        self.genre_entry = ttk.Entry(parent, textvariable=self.genre_var, width=30)
        self.genre_entry.grid(row=row, column=1, sticky="ew")
        row += 1

        year_row = ttk.Frame(parent)
        year_row.grid(row=row, column=1, sticky="w")
        ttk.Label(parent, text="Year range").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.year_min_var = tk.StringVar()
        self.year_max_var = tk.StringVar()
        self.year_min_entry = ttk.Entry(year_row, textvariable=self.year_min_var, width=8)
        self.year_min_entry.pack(side="left")
        ttk.Label(year_row, text=" to ").pack(side="left")
        self.year_max_entry = ttk.Entry(year_row, textvariable=self.year_max_var, width=8)
        self.year_max_entry.pack(side="left")
        row += 1

        length_row = ttk.Frame(parent)
        length_row.grid(row=row, column=1, sticky="w")
        ttk.Label(parent, text="Length range (min)").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.length_min_var = tk.StringVar()
        self.length_max_var = tk.StringVar()
        self.length_min_entry = ttk.Entry(length_row, textvariable=self.length_min_var, width=8)
        self.length_min_entry.pack(side="left")
        ttk.Label(length_row, text=" to ").pack(side="left")
        self.length_max_entry = ttk.Entry(length_row, textvariable=self.length_max_var, width=8)
        self.length_max_entry.pack(side="left")
        row += 1

        parent.columnconfigure(1, weight=1)
        self.next_row = row

    def bind_return(self, callback: Callable[[object], None]) -> None:
        self.artist_field.bind_return(callback)
        self.genre_entry.bind("<Return>", callback)
        self.year_min_entry.bind("<Return>", callback)
        self.year_max_entry.bind("<Return>", callback)
        self.length_min_entry.bind("<Return>", callback)
        self.length_max_entry.bind("<Return>", callback)

    def set_criteria(self, criteria: dict) -> None:
        self.artist_field.set(criteria.get("artist", ""))
        self.genre_var.set(criteria.get("genre", ""))
        self.year_min_var.set(str(criteria["year_min"]) if "year_min" in criteria else "")
        self.year_max_var.set(str(criteria["year_max"]) if "year_max" in criteria else "")
        length_min = criteria.get("duration_min")
        length_max = criteria.get("duration_max")
        self.length_min_var.set(f"{length_min / 60:g}" if length_min is not None else "")
        self.length_max_var.set(f"{length_max / 60:g}" if length_max is not None else "")

    def get_criteria(self) -> dict | None:
        """Validate the fields and return a filter_criteria dict, or None
        after showing an error dialog if something's invalid/empty."""
        filter_artist = self.artist_field.get().strip()
        filter_genre = self.genre_var.get().strip()
        year_min_text = self.year_min_var.get().strip()
        year_max_text = self.year_max_var.get().strip()
        length_min_text = self.length_min_var.get().strip()
        length_max_text = self.length_max_var.get().strip()

        year_min = year_max = None
        for text, field_name in ((year_min_text, "minimum year"), (year_max_text, "maximum year")):
            if text and not text.isdigit():
                messagebox.showerror("Cannot add", f"Enter a numeric {field_name} (e.g. 1975).", parent=self._dialog_parent)
                return None
        if year_min_text:
            year_min = int(year_min_text)
        if year_max_text:
            year_max = int(year_max_text)
        if year_min is not None and year_max is not None and year_min > year_max:
            messagebox.showerror("Cannot add", "Minimum year can't be after maximum year.", parent=self._dialog_parent)
            return None

        length_min = length_max = None
        for text, field_name in ((length_min_text, "minimum length"), (length_max_text, "maximum length")):
            if not text:
                continue
            try:
                minutes = float(text)
                if minutes < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Cannot add", f"Enter a numeric {field_name} in minutes (e.g. 3.5).", parent=self._dialog_parent,
                )
                return None
        if length_min_text:
            length_min = round(float(length_min_text) * 60)
        if length_max_text:
            length_max = round(float(length_max_text) * 60)
        if length_min is not None and length_max is not None and length_min > length_max:
            messagebox.showerror("Cannot add", "Minimum length can't be more than maximum length.", parent=self._dialog_parent)
            return None

        if (
            not filter_artist and not filter_genre and year_min is None and year_max is None
            and length_min is None and length_max is None
        ):
            messagebox.showerror(
                "Cannot add", "Enter an artist, genre, year range, and/or length range to filter by.",
                parent=self._dialog_parent,
            )
            return None

        filter_criteria: dict = {}
        if filter_artist:
            filter_criteria["artist"] = filter_artist
        if filter_genre:
            filter_criteria["genre"] = filter_genre
        if year_min is not None:
            filter_criteria["year_min"] = year_min
        if year_max is not None:
            filter_criteria["year_max"] = year_max
        if length_min is not None:
            filter_criteria["duration_min"] = length_min
        if length_max is not None:
            filter_criteria["duration_max"] = length_max
        return filter_criteria
