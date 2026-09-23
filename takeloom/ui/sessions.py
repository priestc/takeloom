"""Sessions tab: browse past recording sessions and their takes.

Per take, "▶ Play" opens it in the OS's default player — see backend.py's
list_sessions/get_session_detail/play_take. Downloads it from the backup
server first if it isn't already on local disk (see backend.py's
ensure_take_local) — a take shown here doesn't have to be any project's
*current* preferred one (see get_session_detail), so this can't assume
next_untaken_track_index-style "still local" the way an active session's
own playback can. Also behaves genuinely differently over a Remote
connection: the server resolves/downloads the file on its own end and
streams the bytes back in chunks to actually play on the machine looking
at this tab, not the studio's.

(This tab used to also offer "Correct instrument", "Reassign", and
"Analyze", for retitling a session/re-filing a take recorded under the
wrong instrument — removed once the underlying mis-filing causes were
fixed at the source; backend.py's correct_session_instrument/
reassign_take/analyze_take still exist and work if that's ever needed
again, just nothing in this UI calls them anymore.)

Everything here goes through app_state.backend, same as every other tab —
works identically pointed at local hardware or a Remote connection.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError
from ..config import StudioConfig
from .app_state import AppState
from .instrument_colors import make_label_badge


class SessionsFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None
        self._sessions: list[dict] = []
        self._selected_session_dir: str | None = None

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
        self._run_backend(
            lambda: (backend.get_config(), backend.list_sessions()),
            lambda result, error: self._on_loaded(*(result or (None, [])), error),
        )

    def _on_loaded(self, config: StudioConfig | None, sessions: list[dict], error: str | None) -> None:
        if not self.winfo_exists():
            return
        for child in self.winfo_children():
            child.destroy()
        if error or config is None:
            ttk.Label(self, text=error or "Could not load configuration.", foreground="#b00020").pack(anchor="w")
            return
        self.config_obj = config
        self._sessions = sessions
        self._build()

    # --- build ---

    def _build(self) -> None:
        ttk.Label(self, text="Sessions", font=("TkDefaultFont", 14, "bold")).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            self,
            text="Past recording sessions. Select one to see each track's take status, or play a take.",
            foreground="#666666", wraplength=760, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="x")
        self.tree = ttk.Treeview(
            tree_frame, columns=("date", "project", "instrument", "status"),
            show="headings", height=10, selectmode="browse",
        )
        self.tree.heading("date", text="Date")
        self.tree.heading("project", text="Project")
        self.tree.heading("instrument", text="Instrument")
        self.tree.heading("status", text="Status")
        self.tree.column("date", width=150)
        self.tree.column("project", width=140)
        self.tree.column("instrument", width=100)
        self.tree.column("status", width=360)
        for session in self._sessions:
            self.tree.insert("", "end", iid=session["session_dir"], values=(
                session["date"], session["project"], session["instrument"], session.get("status_summary", ""),
            ))
        self.tree.pack(side="left", fill="x", expand=True)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select_session)

        if not self._sessions:
            ttk.Label(self, text="No past sessions found.", foreground="#666666").pack(anchor="w", pady=(12, 0))

        self.detail_frame = ttk.Frame(self)
        self.detail_frame.pack(fill="both", expand=True, pady=(16, 0))

    # --- session selection / detail ---

    def _on_select_session(self, _event: object) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        session_dir = selection[0]
        self._selected_session_dir = session_dir
        for child in self.detail_frame.winfo_children():
            child.destroy()
        ttk.Label(self.detail_frame, text="Loading session...").pack(anchor="w")
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.get_session_detail(session_dir),
            lambda detail, error: self._on_detail_loaded(session_dir, detail, error),
        )

    def _on_detail_loaded(self, session_dir: str, detail: dict | None, error: str | None) -> None:
        if not self.winfo_exists() or self._selected_session_dir != session_dir:
            return  # a different session was selected before this reply arrived
        for child in self.detail_frame.winfo_children():
            child.destroy()
        if error or detail is None:
            ttk.Label(self.detail_frame, text=error or "Could not load session.", foreground="#b00020").pack(
                anchor="w"
            )
            return
        self._build_detail(detail)

    def _build_detail(self, detail: dict) -> None:
        ttk.Label(
            self.detail_frame, text=f"Recorded as: {detail.get('instrument', '')}",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w", pady=(0, 10))

        # One grid shared by every track and take row, not a separate grid
        # per track — column 0 (track names) and column 1 (status text/
        # take badges) each size to their widest cell across the *whole*
        # session, so every badge lines up under the others regardless of
        # how long any given track's own title is.
        tracks_frame = ttk.Frame(self.detail_frame)
        tracks_frame.pack(fill="x")
        project_name = detail.get("project", "")
        grid_row = 0
        for track in detail.get("tracks", []):
            grid_row = self._build_track_row(tracks_frame, grid_row, project_name, track)

    # Display text + color for each of get_session_detail's per-track
    # `status` values (see backend.py's _track_take_status for what each
    # one means and when it's reported).
    _STATUS_STYLES = {
        "completed": ("Completed", "#2a7d2a"),
        "skipped": ("Skipped", "#888888"),
        "stopped early": ("Stopped early", "#b06a00"),
        "recorded, not filed": ("Recorded, but never filed — take processing likely failed", "#b00020"),
        "not recorded": ("Not recorded", "#888888"),
        "pending": ("Pending processing", "#2a6db0"),
    }

    def _build_track_row(self, tracks_frame: ttk.Frame, grid_row: int, project_name: str, track: dict) -> int:
        """Places this track (and each of its take rows) into tracks_frame's
        shared grid starting at grid_row; returns the next free grid_row."""
        ttk.Label(
            tracks_frame, text=track["track_name"], anchor="w", font=("TkDefaultFont", 10, "bold"),
        ).grid(row=grid_row, column=0, sticky="w", pady=(6, 0))

        takes = track.get("takes", [])
        status_text, status_color = self._STATUS_STYLES.get(track.get("status", "not recorded"), ("", "#888888"))
        if track.get("status") == "completed" and len(takes) > 1:
            status_text = f"{status_text} ({len(takes)} takes)"
        ttk.Label(tracks_frame, text=status_text, foreground=status_color).grid(
            row=grid_row, column=1, sticky="w", padx=(8, 0), pady=(6, 0)
        )
        grid_row += 1

        # A track normally has one take per instrument that's actually
        # recorded it — one row per (instrument, take) currently on file,
        # so a track with takes under more than one instrument doesn't
        # hide any of them.
        for take in takes:
            self._build_take_row(tracks_frame, grid_row, project_name, take)
            grid_row += 1
        return grid_row

    def _build_take_row(self, tracks_frame: ttk.Frame, grid_row: int, project_name: str, take: dict) -> None:
        old_instrument = take["instrument"]  # a label — takes are filed by label
        # Gridded into tracks_frame's column 1 (see _build_track_row) so
        # it lines up under every other track's own take rows, not just
        # this track's own name in column 0; its own contents are still
        # packed, same as before — grid/pack can mix freely as long as
        # they're never both used directly on the same parent's children.
        row = ttk.Frame(tracks_frame)
        row.grid(row=grid_row, column=1, sticky="w", pady=1)
        # Label called out as its own colored badge — this is what
        # actually answers "what was this filed under" at a glance,
        # including for a take pulled in from a filter-slot draw's shared
        # inspiration-take index — and the same color for a given label
        # everywhere it's shown (see instrument_colors.py) makes it a
        # fast visual scan across rows.
        make_label_badge(row, old_instrument).pack(side="left", padx=(0, 6))
        # Just "Take N", not the take's full filename — the badge already
        # gives the label, the track name is right there in column 0, and
        # the rest of the filename (source tag, inspiration id) is noise
        # nobody reads here; the actual file only matters to "▶ Play".
        ttk.Label(row, text=f"Take {take['take_number']}", foreground="#666666").pack(side="left", padx=(0, 8))
        self._build_play_controls(row, project_name, take["filename"], old_instrument)

    def _build_play_controls(self, row: ttk.Frame, project_name: str, filename: str, label: str) -> None:
        # Read-only — opens the take in the OS's default player
        # (backend.py's play_take) rather than anything this tab renders
        # itself. Works identically pointed at local
        # hardware or a Remote connection: locally it just needs the file
        # to exist (downloading it from the backup server first if
        # "remote" vault mode already pruned it — see ensure_take_local),
        # remotely the server does that same local-availability step on
        # its own end and streams the bytes back to actually play here.
        # `label` (the take's own instrument label — see the bold label
        # right next to this in _build_take_row) is what play_take looks
        # its compressor settings up by; the take file itself is always
        # raw on disk regardless.
        play_var = tk.StringVar(value="")
        ttk.Button(
            row, text="▶ Play",
            command=lambda: self._on_play_take(project_name, filename, label, play_var),
        ).pack(side="left", padx=(0, 4))
        ttk.Label(row, textvariable=play_var, foreground="#666666").pack(side="left", padx=(0, 4))

    # --- actions ---

    def _on_play_take(self, project_name: str, filename: str, label: str, status_var: tk.StringVar) -> None:
        # "Loading..." matters most over a Remote connection, where this
        # can mean a real wait — downloading the take from the backup
        # server to the studio machine, then streaming it here (see
        # backend.py's play_take/ensure_take_local) — but costs nothing
        # to show locally either, where it's normally near-instant.
        status_var.set("Loading...")
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.play_take(project_name, filename, label),
            lambda _result, error: self._on_play_take_result(status_var, error),
        )

    def _on_play_take_result(self, status_var: tk.StringVar, error: str | None) -> None:
        if not self.winfo_exists():
            return
        status_var.set("")
        if error:
            messagebox.showerror("Could not play take", error)
