"""Sessions tab: browse past recording sessions and their takes.

Two read-only, independently-usable actions per take — see backend.py's
list_sessions/get_session_detail/analyze_take/play_take for exactly what
each one touches:

1. "Analyze" runs the take's actual recorded audio through the
   frequency-based instrument classifier (audio/instrument_classifier.py)
   and reports which configured instrument it most resembles — never
   touches anything itself.
2. "▶ Play" opens the take in the OS's default player. Downloads it from
   the backup server first if it isn't already on local disk (see
   backend.py's ensure_take_local) — a take shown here doesn't have to be
   any project's *current* preferred one (see get_session_detail), so
   this can't assume next_untaken_track_index-style "still local" the
   way an active session's own playback can. Also the only one of the two
   that behaves genuinely differently over a Remote connection: the
   server resolves/downloads the file on its own end and streams the
   bytes back in chunks to actually play on the machine looking at this
   tab, not the studio's.

(This tab used to also offer "Correct instrument" and "Reassign", for
retitling a session/re-filing a take recorded under the wrong instrument —
removed once the underlying mis-filing causes were fixed at the source;
backend.py's correct_session_instrument/reassign_take still exist and work
if that's ever needed again, just nothing in this UI calls them anymore.)

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
            text="Past recording sessions. Select one to see each track's take status, play a "
                 "take, or analyze it.",
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
        self._build_detail(session_dir, detail)

    def _build_detail(self, session_dir: str, detail: dict) -> None:
        ttk.Label(
            self.detail_frame, text=f"Recorded as: {detail.get('instrument', '')}",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w", pady=(0, 10))

        project_name = detail.get("project", "")
        for track in detail.get("tracks", []):
            self._build_track_row(session_dir, project_name, track)

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

    def _build_track_row(self, session_dir: str, project_name: str, track: dict) -> None:
        # A grid (not pack) per track: column 0 holds the track name and,
        # below it, nothing for each take row — grid sizes column 0 to
        # its widest cell automatically, so take rows still line up under
        # the (now unclipped, however long) track name without having to
        # guess a fixed character width that either truncates a long title
        # (the bug this replaced) or wastes space on a short one.
        track_frame = ttk.Frame(self.detail_frame)
        track_frame.pack(fill="x", pady=(6, 0))
        ttk.Label(
            track_frame, text=track["track_name"], anchor="w", font=("TkDefaultFont", 10, "bold"),
        ).grid(row=0, column=0, sticky="w")

        takes = track.get("takes", [])
        status_text, status_color = self._STATUS_STYLES.get(track.get("status", "not recorded"), ("", "#888888"))
        if track.get("status") == "completed" and len(takes) > 1:
            status_text = f"{status_text} ({len(takes)} takes)"
        ttk.Label(track_frame, text=status_text, foreground=status_color).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )

        if not takes:
            return

        # A track normally has one take per instrument that's actually
        # recorded it — one row per (instrument, take) currently on file,
        # so a track with takes under more than one instrument doesn't
        # hide any of them.
        for i, take in enumerate(takes, start=1):
            self._build_take_row(track_frame, i, session_dir, project_name, track["track_name"], take)

    def _build_take_row(
        self, track_frame: ttk.Frame, grid_row: int, session_dir: str, project_name: str, track_name: str,
        take: dict,
    ) -> None:
        old_instrument = take["instrument"]  # a label — takes are filed by label
        # Gridded into track_frame's column 1 (see _build_track_row) so it
        # lines up under the status text, not the track name in column 0;
        # its own contents are still packed, same as before — grid/pack
        # can mix freely as long as they're never both used directly on
        # the same parent's children.
        row = ttk.Frame(track_frame)
        row.grid(row=grid_row, column=1, sticky="w", pady=1)
        # Label called out as its own colored badge, not just bracketed
        # into the filename text below it — the filename usually repeats
        # it too, but this is what actually answers "what was this filed
        # under" at a glance, including for a take pulled in from a
        # filter-slot draw's shared inspiration-take index — and the
        # same color for a given label everywhere it's shown (see
        # instrument_colors.py) makes it a fast visual scan across rows.
        make_label_badge(row, old_instrument).pack(side="left", padx=(0, 6))
        ttk.Label(row, text=take["filename"], foreground="#666666").pack(side="left", padx=(0, 8))
        self._build_play_controls(row, project_name, take["filename"], old_instrument)
        self._build_analyze_controls(row, session_dir, track_name, old_instrument)

    def _build_play_controls(self, row: ttk.Frame, project_name: str, filename: str, label: str) -> None:
        # Read-only, same spirit as Analyze — opens the take in the OS's
        # default player (backend.py's play_take) rather than anything
        # this tab renders itself. Works identically pointed at local
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

    def _build_analyze_controls(self, row: ttk.Frame, session_dir: str, track_name: str, instrument: str) -> None:
        # Read-only (see backend.py's analyze_take) — its result just
        # updates analyze_var in place, never touches anything on disk.
        analyze_var = tk.StringVar(value="")
        ttk.Button(
            row, text="Analyze",
            command=lambda: self._on_analyze_take(session_dir, track_name, instrument, analyze_var),
        ).pack(side="left", padx=(10, 0))
        ttk.Label(row, textvariable=analyze_var, foreground="#2a6db0").pack(side="left", padx=(6, 0))

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

    def _on_analyze_take(
        self, session_dir: str, track_name: str, instrument_name: str, result_var: tk.StringVar,
    ) -> None:
        result_var.set("Analyzing...")
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.analyze_take(session_dir, track_name, instrument_name),
            lambda result, error: self._on_analyze_result(instrument_name, result_var, result, error),
        )

    def _on_analyze_result(
        self, old_instrument: str, result_var: tk.StringVar, result: dict | None, error: str | None,
    ) -> None:
        if not self.winfo_exists():
            return  # a different session was selected (or the tab left) before this reply arrived
        if error:
            result_var.set("")
            messagebox.showerror("Could not analyze take", error)
            return
        guess = (result or {}).get("guess")
        confidence = (result or {}).get("confidence") or 0.0
        if guess is None:
            result_var.set("Couldn't tell — too quiet or unreadable.")
        elif guess == old_instrument:
            result_var.set(f"Matches: {guess} ({confidence:.0%})")
        else:
            result_var.set(f"Sounds more like: {guess} ({confidence:.0%})")
