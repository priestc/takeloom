"""Completed Takes tab: browse every completed take across the whole vault
— not scoped to one session or project, unlike the Sessions tab (see
backend.py's list_completed_takes for exactly what's gathered and why).

A title filter narrows by track name; "Filter by this project" further
narrows to only takes whose track name is currently in the loaded
project's own setlist (config.last_selected_project) — the same project
the Record tab has open. An ordinary track matches by its own name
directly; a filter slot has no single fixed song of its own, so it
matches by every song currently in its cached_matches (see project.py's
TrackEntry and backend.py's get_filter_slot_previews) — the same cached
list the Record tab's Setlist panel shows "next up" from, not a fresh
inspiration-server query. A filter slot never yet opened in the Record
tab (empty cache) contributes no songs here, same as it shows nothing
"next up" there either.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError
from ..config import StudioConfig
from ..inspiration import build_inspiration_track_entry
from .app_state import AppState


class CompletedTakesFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None
        self._takes: list[dict] = []
        self._play_project: str = ""  # any project name — only used to resolve the (shared) vault path
        self._current_project: str = ""
        self._current_track_names: set[str] = set()

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
            text="Every completed take across the whole vault, not just one project or session.",
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

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            tree_frame, columns=("track", "instrument", "take", "video"),
            show="headings", height=18, selectmode="browse",
        )
        self.tree.heading("track", text="Track")
        self.tree.heading("instrument", text="Instrument")
        self.tree.heading("take", text="Take #")
        self.tree.heading("video", text="Video")
        self.tree.column("track", width=420)
        self.tree.column("instrument", width=140)
        self.tree.column("take", width=60, anchor="center")
        self.tree.column("video", width=60, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self._on_play_selected())

        play_row = ttk.Frame(self)
        play_row.pack(fill="x", pady=(8, 0))
        ttk.Button(play_row, text="▶ Play selected", command=self._on_play_selected).pack(side="left")
        self.status_var = tk.StringVar(value="")
        ttk.Label(play_row, textvariable=self.status_var, foreground="#666666").pack(side="left", padx=(8, 0))

        self._filtered: list[dict] = []
        self._apply_filter()

    # --- filtering ---

    def _apply_filter(self) -> None:
        title = self.title_var.get().strip().lower()
        project_only = self.project_only_var.get()
        self._filtered = [
            take for take in self._takes
            if (not title or title in take["track_name"].lower())
            and (not project_only or take["track_name"] in self._current_track_names)
        ]
        self.tree.delete(*self.tree.get_children())
        for i, take in enumerate(self._filtered):
            self.tree.insert("", "end", iid=str(i), values=(
                take["track_name"], take["instrument"], take["take_number"], "Yes" if take["has_video"] else "",
            ))

    # --- play ---

    def _on_play_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        take = self._filtered[int(selection[0])]
        if not self._play_project:
            messagebox.showerror("Could not play take", "No project available to locate the vault.")
            return
        self.status_var.set("Loading...")
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.play_take(self._play_project, take["filename"]),
            self._on_play_result,
        )

    def _on_play_result(self, _result: object, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self.status_var.set("")
        if error:
            messagebox.showerror("Could not play take", error)
