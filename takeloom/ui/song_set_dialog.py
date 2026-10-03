"""Song Set dialog: build (or edit) a setlist "song set" slot — a fixed,
hand-picked list of inspiration-server songs that each session draws one
song from, the same way an inspiration filter slot does (see
TrackEntry.song_set in project.py). Songs go in one at a time by artist/
title, or in bulk from an inspiration filter — either way the result is a
plain list the user can prune before saving, not a live filter."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from ..backend import Backend, BackendError
from ..inspiration import derive_filter_label
from ..utils import format_duration
from .add_to_setlist_dialog import _AutocompleteEntry
from .filter_fields import FilterCriteriaFields

# Adding more matches than this from one filter asks first — an artist- or
# genre-wide filter can match hundreds of songs.
_BULK_CONFIRM_THRESHOLD = 100


class SongSetDialog(tk.Toplevel):
    """`commit(name, songs)`, if given, runs on a background thread when
    Save is pressed (e.g. the backend call that actually adds the slot);
    a BackendError from it is shown here and leaves the dialog open with
    the list intact. `on_saved(name, songs)` then runs on the main thread
    just before the dialog closes. `name` may be "" — the caller picks a
    default."""

    def __init__(
        self, master: tk.Misc, backend: Backend, title: str,
        on_saved: Callable[[str, list[dict]], None],
        commit: Callable[[str, list[dict]], None] | None = None,
        initial_name: str = "", initial_songs: list[dict] | None = None,
        save_label: str = "Save",
    ) -> None:
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self._backend = backend
        self._on_saved = on_saved
        self._commit = commit
        self._songs: list[dict] = [dict(s) for s in (initial_songs or [])]
        self._working = False
        self._artist_cache: dict[str, list[dict]] = {}  # see _artist_tracks
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        name_row = ttk.Frame(frame)
        name_row.pack(fill="x", pady=(0, 8))
        ttk.Label(name_row, text="Name").pack(side="left", padx=(0, 8))
        self.name_var = tk.StringVar(value=initial_name)
        ttk.Entry(name_row, textvariable=self.name_var, width=40).pack(side="left", fill="x", expand=True)

        ttk.Label(
            frame, text="Each session draws one song from this set, like an inspiration filter — "
                        "preferring a song another instrument already has a take for, so parts can layer.",
            foreground="#666666", wraplength=520, justify="left",
        ).pack(anchor="w", pady=(0, 8))

        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True)
        columns = ("artist", "title", "year", "length")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=10, selectmode="extended")
        for col, heading, width in (
            ("artist", "Artist", 170), ("title", "Title", 220), ("year", "Year", 60), ("length", "Length", 70),
        ):
            self.tree.heading(col, text=heading)
            self.tree.column(col, width=width, anchor="w")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Delete>", lambda _e: self._remove_selected())
        self.tree.bind("<BackSpace>", lambda _e: self._remove_selected())

        list_actions = ttk.Frame(frame)
        list_actions.pack(fill="x", pady=(4, 10))
        self.count_var = tk.StringVar()
        ttk.Label(list_actions, textvariable=self.count_var).pack(side="left")
        ttk.Button(list_actions, text="Clear", command=self._clear).pack(side="right")
        ttk.Button(list_actions, text="Remove selected", command=self._remove_selected).pack(side="right", padx=(0, 6))

        self.notebook = ttk.Notebook(frame)
        self.notebook.pack(fill="x")
        self._build_single_tab()
        self._build_filter_tab()

        self.status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_var, foreground="#2a7d2a", wraplength=520).pack(
            anchor="w", pady=(6, 0)
        )

        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(12, 0))
        self.cancel_button = ttk.Button(footer, text="Cancel", command=self._on_close)
        self.cancel_button.pack(side="right")
        self.save_button = ttk.Button(footer, text=save_label, command=self._on_save)
        self.save_button.pack(side="right", padx=(0, 8))

        self._refresh_list()

    # --- add one song ---

    def _build_single_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Add a song")

        def fetch_artists(text: str) -> list[tuple[str, None]]:
            return [(name, None) for name in self._backend.search_inspiration_artists(text)]

        def fetch_titles(text: str) -> list[tuple[str, dict | None]]:
            artist = self.artist_field.get().strip()
            needle = text.strip().lower()
            # With an artist entered, suggest from that artist's own songs
            # first, matched locally on any substring: the server's title
            # autocomplete needs 2+ characters and only knows the exact
            # artist spelling picked, so a chosen spelling with only a song
            # or two filed under it (e.g. "Lil' Jimmie Dickens" vs. the
            # 100+ under "Little Jimmy Dickens", which the artist filter
            # search does find) left the Title box suggesting nothing.
            tracks = [t for t in self._artist_tracks(artist) if needle in (t.get("title") or "").lower()]
            tracks.sort(key=lambda t: not (t.get("title") or "").lower().startswith(needle))  # prefix matches first
            have = {t.get("id") for t in tracks}
            tracks += [
                t for t in self._backend.search_inspiration_titles(text, artist=artist)
                if t.get("id") not in have or "id" not in t
            ]
            out = []
            for t in tracks:
                year = t.get("year")
                display = f"{t.get('title', '')} ({year})" if year else t.get("title", "")
                if artist and t.get("artist") and t["artist"].lower() != artist.lower():
                    display += f" — {t['artist']}"
                out.append((display, t if "id" in t else None))
            return out

        ttk.Label(tab, text="Artist").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.artist_field = _AutocompleteEntry(tab, fetch=fetch_artists)
        self.artist_field.grid(row=0, column=1, sticky="ew")
        ttk.Label(tab, text="Title").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.title_field = _AutocompleteEntry(tab, fetch=fetch_titles)
        self.title_field.grid(row=1, column=1, sticky="ew")
        tab.columnconfigure(1, weight=1)
        self.add_song_button = ttk.Button(tab, text="Add song", command=self._on_add_song)
        self.add_song_button.grid(row=2, column=1, sticky="e", pady=(6, 0))
        self.artist_field.bind_return(lambda _e: self._on_add_song())
        self.title_field.bind_return(lambda _e: self._on_add_song())

    def _artist_tracks(self, artist: str) -> list[dict]:
        """Every library track the artist filter search finds for
        `artist`, fetched once per artist per dialog (called from the
        autocomplete's worker thread, never the Tk thread). [] with no
        artist, or if the search fails — suggestions just fall back to the
        server's own title autocomplete."""
        if not artist:
            return []
        key = artist.lower()
        if key not in self._artist_cache:
            try:
                self._artist_cache[key] = self._backend.search_inspiration_by_filter({"artist": artist}, all_matches=True)
            except BackendError:
                return []
        return self._artist_cache[key]

    def _on_add_song(self) -> None:
        if self._working:
            return
        artist = self.artist_field.get().strip()
        title = self.title_field.get().strip()
        selected = self.title_field.selected_payload
        if isinstance(selected, dict) and selected.get("id"):
            # Picked off the Title autocomplete — already the exact track.
            self._append_songs([selected])
            self._reset_single_fields()
            return
        if not title:
            messagebox.showerror("Cannot add", "Enter a song title (and ideally the artist).", parent=self)
            return
        backend = self._backend
        self._run(
            "Looking up song...",
            lambda: backend.find_inspiration_track(artist, title),
            lambda track: (self._append_songs([track]), self._reset_single_fields()),
        )

    def _reset_single_fields(self) -> None:
        # Keep the artist — entering several songs by the same artist in a
        # row is the common case.
        self.title_field.set("")
        self.title_field.selected_payload = None
        self.title_field.focus_set()

    # --- add from a filter ---

    def _build_filter_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Add from filter")
        self.filter_fields = FilterCriteriaFields(tab, self._backend, dialog_parent=self, start_row=0)
        self.add_matches_button = ttk.Button(tab, text="Add matching songs", command=self._on_add_matches)
        self.add_matches_button.grid(row=self.filter_fields.next_row, column=1, sticky="e", pady=(6, 0))
        self.filter_fields.bind_return(lambda _e: self._on_add_matches())

    def _on_add_matches(self) -> None:
        if self._working:
            return
        criteria = self.filter_fields.get_criteria()
        if criteria is None:
            return
        backend = self._backend

        def on_found(tracks: list[dict]) -> None:
            if not tracks:
                messagebox.showinfo("No matches", "No songs match that filter.", parent=self)
                return
            if len(tracks) > _BULK_CONFIRM_THRESHOLD and not messagebox.askyesno(
                "Add all?", f"{len(tracks)} songs match that filter. Add all of them?", parent=self,
            ):
                return
            if not self.name_var.get().strip() and not self._songs:
                self.name_var.set(derive_filter_label(criteria))
            self._append_songs(tracks)

        self._run("Searching...", lambda: backend.search_inspiration_by_filter(criteria, all_matches=True), on_found)

    # --- list ---

    def _append_songs(self, tracks: list[dict]) -> None:
        if not self.winfo_exists():
            return
        have = {s.get("id") for s in self._songs}
        added = 0
        for t in tracks:
            if t.get("id") and t["id"] not in have:
                self._songs.append(dict(t))
                have.add(t["id"])
                added += 1
        skipped = len(tracks) - added
        msg = f"Added {added} song{'s' if added != 1 else ''}."
        if skipped:
            msg += f" ({skipped} already in the set.)"
        self.status_var.set(msg)
        self._refresh_list()

    def _refresh_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self._songs):
            duration = s.get("duration")
            self.tree.insert("", "end", iid=str(i), values=(
                s.get("artist", ""), s.get("title", ""), s.get("year", "") or "",
                format_duration(float(duration)) if duration else "",
            ))
        n = len(self._songs)
        self.count_var.set(f"{n} song{'s' if n != 1 else ''} in set")

    def _remove_selected(self) -> None:
        selected = {int(iid) for iid in self.tree.selection()}
        if not selected:
            return
        self._songs = [s for i, s in enumerate(self._songs) if i not in selected]
        self._refresh_list()

    def _clear(self) -> None:
        if self._songs and messagebox.askyesno("Clear set", "Remove every song from this set?", parent=self):
            self._songs = []
            self._refresh_list()

    # --- save / plumbing ---

    def _on_save(self) -> None:
        if self._working:
            return
        if not self._songs:
            messagebox.showerror("Empty set", "Add at least one song to the set.", parent=self)
            return
        name = self.name_var.get().strip()
        songs = list(self._songs)

        def done(_result: object) -> None:
            self._on_saved(name, songs)
            self.destroy()

        commit = self._commit
        if commit is None:
            done(None)
            return
        self._run("Saving...", lambda: commit(name, songs), done)

    def _run(self, status: str, call: Callable[[], object], on_success: Callable[[object], None]) -> None:
        """Run a (possibly slow, possibly remote) backend call off the Tk
        thread, with every action button disabled meanwhile."""
        self._set_working(True, status)

        def worker() -> None:
            try:
                result = call()
            except BackendError as e:
                self.after(0, lambda err=str(e): self._finish(None, err, on_success))
                return
            self.after(0, lambda: self._finish(result, None, on_success))

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, result: object, error: str | None, on_success: Callable[[object], None]) -> None:
        if not self.winfo_exists():
            return
        self._set_working(False, "")
        if error:
            messagebox.showerror("Error", error, parent=self)
            return
        on_success(result)

    def _set_working(self, working: bool, status: str) -> None:
        self._working = working
        state = ["disabled"] if working else ["!disabled"]
        for b in (self.save_button, self.cancel_button, self.add_song_button, self.add_matches_button):
            b.state(state)
        if working or status:
            self.status_var.set(status)

    def _on_close(self) -> None:
        if not self._working:
            self.destroy()
