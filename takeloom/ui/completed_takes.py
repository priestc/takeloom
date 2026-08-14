"""Completed Takes tab: browse every completed take across the whole vault
— not scoped to one session or project, unlike the Sessions tab (see
backend.py's list_completed_takes for exactly what's gathered and why).

Takes are grouped by song: one header row per track name — a collapse/
expand triangle, the title, then every instrument that has a take on it
as a row of colored badges (instrument_colors.py) right there on the
header, plus a "Play all" button that mixes every one of that song's
takes together (backend.py's play_song_takes) — and, once expanded, one
line per take below it with its own date and Play button. takes["track_
name"] is already the group key list_completed_takes sorts by, so
grouping here is just a consecutive-run split, not a re-sort.

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
matches by its own name directly; a filter slot has no single fixed song
of its own, so it matches by every song currently in its cached_matches
(see project.py's TrackEntry and backend.py's get_filter_slot_previews)
— the same cached list the Record tab's Setlist panel shows "next up"
from, not a fresh inspiration-server query. A filter slot never yet
opened in the Record tab (empty cache) contributes no songs here, same
as it shows nothing "next up" there either.
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


class CompletedTakesFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None
        self._takes: list[dict] = []
        self._play_project: str = ""  # any project name — only used to resolve the (shared) vault path
        self._current_project: str = ""
        self._current_track_names: set[str] = set()
        self._expanded: set[str] = set()  # track_names currently showing their per-take detail lines

        ttk.Label(self, text="Loading...").pack(anchor="w")
        self._load()

    # --- loading ---

    def _run_backend(self, fn, on_done) -> None:
        def worker() -> None:
            try:
                result, error = fn(), None
            except BackendError as e:
                result, error = None, str(e)
            self.after(0, lambda: on_done(result, error))

        threading.Thread(target=worker, daemon=True).start()

    def _load(self) -> None:
        backend = self.app_state.backend

        def fetch():
            config = backend.get_config()
            takes = backend.list_completed_takes()
            projects = backend.list_projects()
            current_project = config.last_selected_project if config.last_selected_project in projects else ""
            current_track_names: set[str] = set()
            if current_project:
                setlist = backend.get_setlist(current_project)
                for t in setlist.get("tracks", []):
                    if t.get("is_inspiration_filter"):
                        for match in t.get("cached_matches", []):
                            current_track_names.add(build_inspiration_track_entry(match).name)
                    else:
                        current_track_names.add(t["name"])
            play_project = current_project or (projects[0] if projects else "")
            return config, takes, current_project, current_track_names, play_project

        self._run_backend(fetch, self._on_loaded)

    def _on_loaded(self, result: tuple | None, error: str | None) -> None:
        if not self.winfo_exists():
            return
        for child in self.winfo_children():
            child.destroy()
        if error or result is None:
            ttk.Label(self, text=error or "Could not load completed takes.", foreground="#b00020").pack(anchor="w")
            return
        config, takes, current_project, current_track_names, play_project = result
        self.config_obj = config
        self._takes = takes
        self._current_project = current_project
        self._current_track_names = current_track_names
        self._play_project = play_project
        self._build()

    # --- build ---

    def _build(self) -> None:
        ttk.Label(self, text="Completed Takes", font=("TkDefaultFont", 14, "bold")).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            self,
            text="Every completed take across the whole vault, not just one project or session — grouped by song.",
            foreground="#666666", wraplength=760, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        filter_row = ttk.Frame(self)
        filter_row.pack(fill="x", pady=(0, 8))
        ttk.Label(filter_row, text="Filter by title:").pack(side="left")
        self.title_var = tk.StringVar(value="")
        self.title_var.trace_add("write", lambda *_a: self._apply_filter())
        ttk.Entry(filter_row, textvariable=self.title_var, width=30).pack(side="left", padx=(6, 16))

        self.project_only_var = tk.BooleanVar(value=False)
        project_check = ttk.Checkbutton(
            filter_row,
            text=f"Filter by this project ({self._current_project})" if self._current_project
            else "Filter by this project (none loaded)",
            variable=self.project_only_var, command=self._apply_filter,
        )
        project_check.pack(side="left")
        if not self._current_project:
            project_check.state(["disabled"])

        self._build_scroll_container()
        self._apply_filter()

    def _build_scroll_container(self) -> None:
        """A scrollable canvas for the song/take list — plain widgets, not
        a Treeview (see module docstring for why), so it can grow past
        the window's height the same way Studio Setup's own content does
        (see studio_setup.py's _build_scroll_container, which this
        mirrors)."""
        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
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

    # --- filtering ---

    def _apply_filter(self) -> None:
        title = self.title_var.get().strip().lower()
        project_only = self.project_only_var.get()
        filtered = [
            take for take in self._takes
            if (not title or title in take["track_name"].lower())
            and (not project_only or take["track_name"] in self._current_track_names)
        ]

        for child in self.content.winfo_children():
            child.destroy()

        # self._takes is already sorted by track_name (see backend.py's
        # list_completed_takes) and filtering above preserves order, so
        # groupby's usual "only groups consecutive runs" caveat doesn't
        # apply here — every take for a given song is already adjacent.
        for track_name, group in groupby(filtered, key=lambda t: t["track_name"]):
            self._build_song_header(track_name, list(group))

        self._bind_mousewheel(self._canvas)

    def _build_song_header(self, track_name: str, takes_for_song: list[dict]) -> None:
        expanded = track_name in self._expanded
        header = ttk.Frame(self.content)
        header.pack(fill="x", pady=(10, 1))

        toggle = tk.Label(
            header, text=("\N{BLACK DOWN-POINTING TRIANGLE}" if expanded else "\N{BLACK RIGHT-POINTING TRIANGLE}"),
            font=("TkDefaultFont", 9), cursor="hand2",
        )
        toggle.pack(side="left", padx=(0, 6))
        title = ttk.Label(header, text=track_name, font=("TkDefaultFont", 11, "bold"), cursor="hand2")
        title.pack(side="left", padx=(0, 8))
        for widget in (toggle, title):
            widget.bind("<Button-1>", lambda _e, name=track_name: self._toggle_expanded(name))

        # Every instrument that has a take on this song, right on the
        # header — a fast "who's covered this one" glance without having
        # to expand it, same color per label as everywhere else (see
        # instrument_colors.py).
        for take in takes_for_song:
            make_label_badge(header, take["instrument"], font_size=8, padx=4, pady=0).pack(side="left", padx=(0, 4))

        status_var = tk.StringVar(value="")
        ttk.Button(
            header, text="▶ Play all", command=lambda: self._on_play_song(track_name, takes_for_song, status_var),
        ).pack(side="left", padx=(10, 4))
        ttk.Label(header, textvariable=status_var, foreground="#666666").pack(side="left")

        if expanded:
            for take in takes_for_song:
                self._build_take_row(take)

    def _toggle_expanded(self, track_name: str) -> None:
        if track_name in self._expanded:
            self._expanded.discard(track_name)
        else:
            self._expanded.add(track_name)
        self._apply_filter()

    def _build_take_row(self, take: dict) -> None:
        row = ttk.Frame(self.content)
        row.pack(fill="x", padx=(28, 0), pady=1)
        make_label_badge(row, take["instrument"]).pack(side="left", padx=(0, 8))
        ttk.Label(row, text=f"take {take['take_number']}", width=10).pack(side="left")
        ttk.Label(row, text=_format_time_ago(take.get("recorded_at")), foreground="#666666", width=10).pack(
            side="left"
        )
        ttk.Label(row, text="video" if take["has_video"] else "", foreground="#666666", width=6).pack(side="left")
        self._build_play_controls(row, take)

    def _build_play_controls(self, row: ttk.Frame, take: dict) -> None:
        status_var = tk.StringVar(value="")
        ttk.Button(row, text="▶ Play", command=lambda: self._on_play_take(take, status_var)).pack(
            side="left", padx=(8, 4)
        )
        ttk.Label(row, textvariable=status_var, foreground="#666666").pack(side="left")

    # --- play ---

    def _on_play_take(self, take: dict, status_var: tk.StringVar) -> None:
        if not self._play_project:
            status_var.set("")
            messagebox.showerror("Could not play take", "No project available to locate the vault.")
            return
        status_var.set("Loading...")
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.play_take(self._play_project, take["filename"], take["instrument"]),
            lambda _result, error: self._on_play_result(status_var, error),
        )

    def _on_play_song(self, track_name: str, takes_for_song: list[dict], status_var: tk.StringVar) -> None:
        if not self._play_project:
            status_var.set("")
            messagebox.showerror("Could not play song", "No project available to locate the vault.")
            return
        status_var.set("Mixing...")
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.play_song_takes(self._play_project, takes_for_song),
            lambda _result, error: self._on_play_result(status_var, error),
        )

    def _on_play_result(self, status_var: tk.StringVar, error: str | None) -> None:
        if not self.winfo_exists():
            return
        status_var.set("")
        if error:
            messagebox.showerror("Could not play", error)
