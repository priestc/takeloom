"""Completed Takes tab: browse every completed take across the whole vault
— not scoped to one session or project, unlike the Sessions tab (see
backend.py's list_completed_takes for exactly what's gathered and why).

Two panes: on the left a mixer (ui/song_mixer.py), on the right the song
list. Clicking a song loads all of its takes into the mixer — a volume
slider (plus Mute/Solo) per instrument, played together live — where
"Save mix" stores those settings in the vault and they're re-applied
whenever that song is loaded again. Below the mixer, a backing-track trim
editor (ui/backing_trim_editor.py) crops the song's intro/outro.

Takes are grouped by song, one grid row per track name with three
columns: Name (behind a collapse/expand triangle), Backing source
(Inspiration / YouTube / Direct upload — TrackEntry.source_label()), and
Instruments — every instrument that has a take on it as a row of colored
badges (instrument_colors.py). Once expanded, one line per take appears
below it with its take number and date. takes["track_name"] is already
the group key list_completed_takes sorts by, so grouping here is just a
consecutive-run split, not a re-sort.

Built from plain widgets in a scrollable canvas rather than a
ttk.Treeview: a Treeview can only color a whole row via tags, not one
cell's text on its own, and the point of a label's badge is specifically
to color just the label text, not everything next to it — same reasoning
extends to needing an inline row of badges on the header itself, which a
Treeview's own tree/heading columns can't produce either.

A title filter narrows by track name (whole song groups shown/hidden
together); "Filter by this project" further narrows to only songs
currently in the loaded project's own setlist (config.last_selected_
project) — the same project the Record tab has open. An ordinary track
matches by its own name directly; a song set slot has no single fixed song
of its own, so it matches by every song in its set (see project.py's
TrackEntry.song_set).
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from itertools import groupby
from tkinter import messagebox, ttk

from ..backend import BackendError
from ..config import StudioConfig
from ..inspiration import build_inspiration_track_entry
from .app_state import AppState
from .instrument_colors import make_label_badge
from .song_mixer import SongMixer


def _format_time_ago(recorded_at: float | None) -> str:
    """"3h ago"/"5d ago"-style relative time, or "—" when recorded_at is
    None (the take file isn't on local disk right now — see backend.py's
    list_completed_takes docstring for why there's nothing better to
    show in that case)."""
    if recorded_at is None:
        return "—"
    delta = time.time() - recorded_at
    if delta < 60:
        return "just now"
    minutes = delta / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 30:
        return f"{int(days)}d ago"
    months = days / 30.44
    if months < 12:
        return f"{int(months)}mo ago"
    years = days / 365.25
    return f"{int(years)}y ago"


_PACK_BATCH = 15  # song rows shown per event-loop turn — see _pack_in_batches

# backend list_completed_takes' "backing_source" (TrackEntry.source_label())
# -> what the grid's "Backing source" column shows. Missing (an older
# server over Remote) shows "—".
_BACKING_SOURCE_NAMES = {"inspiration": "Inspiration", "youtube": "YouTube", "upload": "Direct upload"}


class CompletedTakesFrame(ttk.Frame):
    """Persistent tab (see ui/app.py's TABS): built once on first visit and
    kept, so coming back to it is instant and whatever the mixer is
    playing keeps going. The data it shows is therefore a cache — "⟳
    Reload" refetches it (and clears the backend's cached playback files,
    see Backend.clear_playback_cache), as does connecting to/disconnecting
    from a Remote studio or saving a backing-track trim."""

    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None
        self._takes: list[dict] = []
        self._play_project: str = ""  # any project name — only used to resolve the (shared) vault path
        self._current_project: str = ""
        self._current_track_names: set[str] = set()
        self._expanded: set[str] = set()  # track_names currently showing their per-take detail lines
        # track_name -> {"frame", "toggle", "detail" (built on first expand), "takes"}
        self._songs: dict[str, dict] = {}
        self._pack_generation = 0  # see _pack_in_batches
        self._current_backend = app_state.backend

        self._build()
        self.app_state.add_listener(self._on_app_state_changed)
        self.bind("<Destroy>", self._on_destroy)
        self._load()

    def _on_app_state_changed(self) -> None:
        if self.app_state.backend is not self._current_backend:
            self._current_backend = self.app_state.backend
            self.after(0, self._load)

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self:
            self.app_state.remove_listener(self._on_app_state_changed)

    # --- loading ---

    def _run_backend(self, fn, on_done) -> None:
        def worker() -> None:
            try:
                result, error = fn(), None
            except BackendError as e:
                result, error = None, str(e)
            self.after(0, lambda: on_done(result, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_reload(self) -> None:
        self._load(clear_playback_cache=True)

    def _load(self, clear_playback_cache: bool = False) -> None:
        backend = self.app_state.backend
        self._reload_button.state(["disabled"])
        self._load_status_var.set("Loading...")

        def fetch():
            if clear_playback_cache:
                backend.clear_playback_cache()
            config = backend.get_config()
            takes = backend.list_completed_takes()
            projects = backend.list_projects()
            current_project = config.last_selected_project if config.last_selected_project in projects else ""
            current_track_names: set[str] = set()
            if current_project:
                setlist = backend.get_setlist(current_project)
                for t in setlist.get("tracks", []):
                    if t.get("is_inspiration_filter"):
                        for match in t.get("song_set", []):
                            current_track_names.add(build_inspiration_track_entry(match).name)
                    else:
                        current_track_names.add(t["name"])
            play_project = current_project or (projects[0] if projects else "")
            return config, takes, current_project, current_track_names, play_project

        self._run_backend(fetch, self._on_loaded)

    def _on_loaded(self, result: tuple | None, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self._reload_button.state(["!disabled"])
        if error or result is None:
            self._load_status_var.set(error or "Could not load completed takes.")
            return
        config, takes, current_project, current_track_names, play_project = result
        self.config_obj = config
        self._takes = takes
        self._current_project = current_project
        self._current_track_names = current_track_names
        self._play_project = play_project
        self._load_status_var.set(f"Loaded {time.strftime('%-I:%M %p')}")

        self._project_check.configure(
            text=f"Filter by this project ({current_project})" if current_project
            else "Filter by this project (none loaded)",
        )
        self._project_check.state(["!disabled"] if current_project else ["disabled"])
        if not current_project:
            self.project_only_var.set(False)
        self._build_songs()

        # The song in the mixer had its backing-track trim edited since it
        # was loaded — reload it so the takes line up with the new trim,
        # without auto-starting playback.
        loaded = self._songs.get(self.mixer.track_name or "")
        if loaded is not None:
            takes = loaded["takes"]
            trim = (takes[0].get("trim_start_seconds", 0.0), takes[0].get("trim_end_seconds", 0.0))
            if trim != self.mixer.trim:
                self._load_into_mixer(self.mixer.track_name, autoplay=False)

    # --- build ---

    def _build(self) -> None:
        """The page's fixed chrome — built once; only the song list below
        it is rebuilt on (re)load (see _build_songs)."""
        title_row = ttk.Frame(self)
        title_row.pack(fill="x", pady=(0, 4))
        ttk.Label(title_row, text="Completed Takes", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        self._reload_button = ttk.Button(title_row, text="⟳ Reload", command=self._on_reload)
        self._reload_button.pack(side="left", padx=(12, 6))
        self._load_status_var = tk.StringVar(value="")
        ttk.Label(title_row, textvariable=self._load_status_var, foreground="#666666").pack(side="left")
        ttk.Label(
            self,
            text="Every completed take across the whole vault, not just one project or session — grouped by song. "
            "Click a song to load its takes into the mixer.",
            foreground="#666666", wraplength=900, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)
        self.mixer = SongMixer(panes, self.app_state, on_trim_saved=self._load)
        right = ttk.Frame(panes)
        panes.add(self.mixer, weight=0)
        panes.add(right, weight=1)

        filter_row = ttk.Frame(right)
        filter_row.pack(fill="x", pady=(0, 8))
        ttk.Label(filter_row, text="Filter by title:").pack(side="left")
        self.title_var = tk.StringVar(value="")
        self.title_var.trace_add("write", lambda *_a: self._apply_filter())
        ttk.Entry(filter_row, textvariable=self.title_var, width=30).pack(side="left", padx=(6, 16))

        self.project_only_var = tk.BooleanVar(value=False)
        self._project_check = ttk.Checkbutton(
            filter_row, text="Filter by this project", variable=self.project_only_var, command=self._apply_filter,
        )
        self._project_check.pack(side="left")
        self._project_check.state(["disabled"])

        self._build_scroll_container(right)

    def _build_scroll_container(self, parent: ttk.Frame) -> None:
        """A scrollable canvas for the song/take list — plain widgets, not
        a Treeview (see module docstring for why), so it can grow past
        the window's height the same way Studio Setup's own content does
        (see studio_setup.py's _build_scroll_container, which this
        mirrors)."""
        canvas = tk.Canvas(parent, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.content = ttk.Frame(canvas)
        content_window = canvas.create_window((0, 0), window=self.content, anchor="nw")

        def _on_content_configure(_event: object) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event: object) -> None:
            canvas.itemconfigure(content_window, width=event.width)

        self.content.bind("<Configure>", _on_content_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        self._canvas = canvas
        self._bind_mousewheel(canvas)

    def _on_mousewheel(self, event: object) -> None:
        self._canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")  # type: ignore[attr-defined]

    def _bind_mousewheel(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._on_mousewheel)
        for child in widget.winfo_children():
            self._bind_mousewheel(child)

    def _build_songs(self) -> None:
        """(Re)create one group per song from self._takes. Expanding and
        filtering afterwards only show/hide these existing widgets —
        destroying and recreating the whole list on every click/keystroke
        is what made this tab slow (Tk relayout of hundreds of widgets)."""
        for child in self.content.winfo_children():
            child.destroy()
        self._songs = {}
        # self._takes is already sorted by track_name (see backend.py's
        # list_completed_takes), so groupby's usual "only groups
        # consecutive runs" caveat doesn't apply here — every take for a
        # given song is already adjacent.
        self._build_column_headings()
        for track_name, group in groupby(self._takes, key=lambda t: t["track_name"]):
            self._build_song_group(track_name, list(group))
        self._bind_mousewheel(self.content)
        self._apply_filter()

    # --- filtering ---

    def _apply_filter(self) -> None:
        title = self.title_var.get().strip().lower()
        project_only = self.project_only_var.get()
        for song in self._songs.values():
            self._show_song(song, False)
        visible = [
            song for track_name, song in self._songs.items()
            if (not title or title in track_name.lower())
            and (not project_only or track_name in self._current_track_names)
        ]
        self._canvas.yview_moveto(0)
        self._pack_generation += 1
        self._pack_in_batches(visible, self._pack_generation)

    def _pack_in_batches(self, songs: list[dict], generation: int) -> None:
        """Show `songs` a batch at a time, yielding to the event loop in
        between. Mapping a widget for the first time inside the canvas
        costs ~1ms on macOS and a full list is hundreds of them — done in
        one go that froze the window for seconds. This way the first
        screenful shows immediately and the rest fills in behind it. A
        newer _apply_filter (bumping _pack_generation) abandons this run."""
        if generation != self._pack_generation or not self.winfo_exists():
            return
        for song in songs[:_PACK_BATCH]:
            self._show_song(song, True)
        if len(songs) > _PACK_BATCH:
            self.after(1, lambda: self._pack_in_batches(songs[_PACK_BATCH:], generation))

    def _show_song(self, song: dict, visible: bool) -> None:
        """Grid in (or remove) one song's row of cells — and its detail
        row too, if it's expanded. Each widget's grid options were set
        once at build time, so a bare grid() puts it back where it was."""
        for cell in song["cells"]:
            cell.grid() if visible else cell.grid_remove()
        if song["detail"] is not None:
            song["detail"].grid() if visible and song["name"] in self._expanded else song["detail"].grid_remove()

    def _build_column_headings(self) -> None:
        for column, text in enumerate(("Name", "Backing source", "Instruments")):
            ttk.Label(self.content, text=text, foreground="#666666", font=("TkDefaultFont", 10, "bold")).grid(
                row=0, column=column, sticky="w", padx=(0 if column else 22, 16), pady=(0, 4),
            )
        ttk.Separator(self.content, orient="horizontal").grid(row=1, column=0, columnspan=3, sticky="ew")
        self.content.columnconfigure(2, weight=1)

    def _build_song_group(self, track_name: str, takes_for_song: list[dict]) -> None:
        # Two grid rows per song: the song's own cells, then its (initially
        # unbuilt) per-take detail row spanning all three columns.
        row = 2 + 2 * len(self._songs)

        name_cell = ttk.Frame(self.content, cursor="hand2")
        name_cell.grid(row=row, column=0, sticky="w", pady=(6, 2), padx=(0, 16))
        toggle = tk.Label(name_cell, text="\N{BLACK RIGHT-POINTING TRIANGLE}", font=("TkDefaultFont", 9), cursor="hand2")
        toggle.pack(side="left", padx=(0, 6))
        title = ttk.Label(
            name_cell, text=track_name, font=("TkDefaultFont", 11, "bold"), cursor="hand2", wraplength=380,
        )
        title.pack(side="left")

        source = _BACKING_SOURCE_NAMES.get(takes_for_song[0].get("backing_source", ""), "—")
        source_cell = ttk.Label(self.content, text=source, foreground="#444444", cursor="hand2")
        source_cell.grid(row=row, column=1, sticky="w", pady=(6, 2), padx=(0, 16))

        # Every instrument that has a take on this song — a fast "who's
        # covered this one" glance without having to expand it, same color
        # per label as everywhere else (see instrument_colors.py).
        instruments_cell = ttk.Frame(self.content, cursor="hand2")
        instruments_cell.grid(row=row, column=2, sticky="w", pady=(6, 2))
        badges = []
        for take in takes_for_song:
            badge = make_label_badge(instruments_cell, take["instrument"], font_size=8, padx=4, pady=0)
            badge.pack(side="left", padx=(0, 4))
            badges.append(badge)

        # Triangle expands/collapses; anywhere else on the row loads the
        # song into the mixer.
        toggle.bind("<Button-1>", lambda _e, name=track_name: self._toggle_expanded(name))
        load = lambda _e, name=track_name: self._load_into_mixer(name)  # noqa: E731
        for widget in (name_cell, title, source_cell, instruments_cell, *badges):
            widget.bind("<Button-1>", load)

        cells = [name_cell, source_cell, instruments_cell]
        for cell in cells:
            cell.grid_remove()  # shown by _apply_filter
        self._songs[track_name] = {
            "name": track_name, "row": row, "cells": cells, "toggle": toggle, "title": title,
            "detail": None, "takes": takes_for_song,
        }
        if track_name == self.mixer.track_name:
            title.configure(foreground="#2a6db0")
        if track_name in self._expanded:
            self._set_expanded(track_name, True)

    def _toggle_expanded(self, track_name: str) -> None:
        self._set_expanded(track_name, track_name not in self._expanded)

    def _set_expanded(self, track_name: str, expanded: bool) -> None:
        song = self._songs.get(track_name)
        if song is None:
            return
        if expanded:
            self._expanded.add(track_name)
            if song["detail"] is None:
                song["detail"] = ttk.Frame(self.content)
                song["detail"].grid(row=song["row"] + 1, column=0, columnspan=3, sticky="w")
                for take in song["takes"]:
                    self._build_take_row(song["detail"], take)
                self._bind_mousewheel(song["detail"])
                song["detail"].grid_remove()
            if song["cells"][0].winfo_manager():  # only if the song itself is currently shown
                song["detail"].grid()
        else:
            self._expanded.discard(track_name)
            if song["detail"] is not None:
                song["detail"].grid_remove()
        song["toggle"].configure(
            text="\N{BLACK DOWN-POINTING TRIANGLE}" if expanded else "\N{BLACK RIGHT-POINTING TRIANGLE}"
        )

    def _build_take_row(self, parent: ttk.Frame, take: dict) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=(28, 0), pady=1)
        make_label_badge(row, take["instrument"]).pack(side="left", padx=(0, 8))
        ttk.Label(row, text=f"take {take['take_number']}", width=10).pack(side="left")
        ttk.Label(row, text=_format_time_ago(take.get("recorded_at")), foreground="#666666", width=10).pack(
            side="left"
        )
        ttk.Label(row, text="video" if take["has_video"] else "", foreground="#666666", width=6).pack(side="left")

    # --- mixer ---

    def _load_into_mixer(self, track_name: str, autoplay: bool = True) -> None:
        song = self._songs.get(track_name)
        if song is None:
            return
        if not self._play_project:
            messagebox.showerror("Could not load song", "No project available to locate the vault.")
            return
        self.mixer.load_song(track_name, song["takes"], self._play_project, autoplay=autoplay)
        self._highlight_selected()

    def _highlight_selected(self) -> None:
        for name, song in self._songs.items():
            song["title"].configure(foreground="#2a6db0" if name == self.mixer.track_name else "")
