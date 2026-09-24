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
            tree_frame, columns=("date", "duration", "project", "instrument", "status"),
            show="headings", height=10, selectmode="browse",
        )
        self.tree.heading("date", text="Date")
        self.tree.heading("duration", text="Duration")
        self.tree.heading("project", text="Project")
        self.tree.heading("instrument", text="Instrument")
        self.tree.heading("status", text="Status")
        self.tree.column("date", width=150)
        self.tree.column("duration", width=80, anchor="e")
        self.tree.column("project", width=140)
        self.tree.column("instrument", width=100)
        self.tree.column("status", width=280)
        for session in self._sessions:
            self.tree.insert("", "end", iid=session["session_dir"], values=(
                session["date"], session.get("duration", ""), session["project"], session["instrument"],
                session.get("status_summary", ""),
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
        # Big — the session's start time, spelled out (day of week, date,
        # time of day — see backend.py's _format_session_datetime), the
        # one fact everything else here is organized under.
        ttk.Label(
            self.detail_frame, text=detail.get("date_display") or detail.get("date", ""),
            font=("TkDefaultFont", 18, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        # Everything else about the session, completionist — one line per
        # fact, skipped entirely (not shown blank) when this particular
        # session log doesn't have it.
        info_frame = ttk.Frame(self.detail_frame)
        info_frame.pack(anchor="w", pady=(0, 4))
        instrument = detail.get("instrument", "")
        if instrument:
            label = detail.get("instrument_label", "")
            text = f"Recorded as: {instrument} ({label})" if label else f"Recorded as: {instrument}"
            ttk.Label(info_frame, text=text, font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        for line in self._session_info_lines(detail):
            ttk.Label(info_frame, text=line, foreground="#444444").pack(anchor="w")

        vault_tags = detail.get("vault_tags", [])
        tags_row = ttk.Frame(self.detail_frame)
        tags_row.pack(anchor="w", pady=(4, 12))
        ttk.Label(tags_row, text="In vault: ", foreground="#444444").pack(side="left")
        if vault_tags:
            for tag in vault_tags:
                ttk.Label(
                    tags_row, text=f"[{tag}]", foreground="#2a6db0", font=("TkDefaultFont", 10, "bold"),
                ).pack(side="left", padx=(0, 6))
        else:
            ttk.Label(tags_row, text="none of its own raw files locally", foreground="#888888").pack(side="left")

        if not detail.get("processed"):
            self._build_process_pending_row(detail)

        # One grid shared by every track — column 0 (track names) and
        # column 1 (status text) each size to their widest cell across
        # the *whole* session, so every status/take lines up under the
        # others regardless of how long any given track's own title is.
        tracks_frame = ttk.Frame(self.detail_frame)
        tracks_frame.pack(fill="x")
        project_name = detail.get("project", "")
        for grid_row, track in enumerate(detail.get("tracks", [])):
            self._build_track_row(tracks_frame, grid_row, project_name, track)

    @staticmethod
    def _session_info_lines(detail: dict) -> list[str]:
        """Every other fact get_session_detail has about the session,
        each its own line, skipped when this session log doesn't have
        it — used right under the big date/instrument header."""
        lines = []
        if detail.get("musician"):
            lines.append(f"Musician: {detail['musician']}")
        studio_name = detail.get("studio_name", "")
        studio_location = detail.get("studio_location", "")
        if studio_name or studio_location:
            studio = " — ".join(p for p in (studio_name, studio_location) if p)
            lines.append(f"Studio: {studio}")
        if detail.get("project"):
            lines.append(f"Project: {detail['project']}")
        if detail.get("duration"):
            lines.append(f"Duration: {detail['duration']}")
        if detail.get("sample_rate"):
            lines.append(f"Sample rate: {detail['sample_rate']} Hz")
        if detail.get("has_video"):
            lines.append("Video: recorded")
        return lines

    def _build_process_pending_row(self, detail: dict) -> None:
        """A "Process now" button for a session get_session_detail says
        hasn't been through process_session yet — the normal "just
        hasn't gotten to it" case, and also how a session recovered from
        a crash (the app killed, or power lost, mid-recording — see
        backend.py's process_pending_session/processing/splicer.py's
        total_frames) actually gets turned into real takes, since
        nothing else ever retries it on its own."""
        row = ttk.Frame(self.detail_frame)
        row.pack(anchor="w", pady=(0, 12))
        ttk.Button(
            row, text="Process now", command=self._on_process_pending,
        ).pack(side="left")
        self.process_status_var = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.process_status_var, foreground="#666666").pack(
            side="left", padx=(8, 0)
        )

    def _on_process_pending(self) -> None:
        session_dir = self._selected_session_dir
        if session_dir is None:
            return
        self.process_status_var.set("Processing...")
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.process_pending_session(session_dir),
            lambda result, error: self._on_process_pending_result(session_dir, result, error),
        )

    def _on_process_pending_result(self, session_dir: str, result: str | None, error: str | None) -> None:
        if not self.winfo_exists() or self._selected_session_dir != session_dir:
            return  # a different session was selected before this reply arrived
        if error:
            self.process_status_var.set("")
            messagebox.showerror("Could not process session", error)
            return
        # Refreshes the whole detail pane, same as _on_detail_loaded would
        # for a fresh selection — the button disappears once `processed`
        # comes back true, and every track/take row now reflects whatever
        # process_session actually found.
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.get_session_detail(session_dir),
            lambda detail, err: self._on_detail_loaded(session_dir, detail, err),
        )
        # And the top table's own Duration/Status columns for this same
        # session — updates the one row in place rather than rebuilding
        # the whole tree, so the current selection/scroll position isn't
        # disturbed for what was, from the table's point of view, a
        # one-row change.
        self._run_backend(
            lambda: backend.list_sessions(),
            lambda sessions, err: self._on_sessions_refreshed(sessions, err),
        )

    def _on_sessions_refreshed(self, sessions: list[dict] | None, error: str | None) -> None:
        if error or sessions is None or not self.winfo_exists() or not hasattr(self, "tree"):
            return
        for session in sessions:
            iid = session["session_dir"]
            if self.tree.exists(iid):
                self.tree.item(iid, values=(
                    session["date"], session.get("duration", ""), session["project"], session["instrument"],
                    session.get("status_summary", ""),
                ))

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

    _NAME_COLOR = "#000000"
    _NAME_COLOR_ABANDONED = "#a0a0a0"

    def _build_track_row(self, tracks_frame: ttk.Frame, grid_row: int, project_name: str, track: dict) -> None:
        """One song, one grid row in tracks_frame: name (column 0),
        status (column 1), every take it has, if any (column 2, all on
        this same row rather than rows of their own — see the module
        docstring's "one line per song")."""
        status = track.get("status", "not recorded")
        # Greyed out, name included — a skipped or stopped-early song
        # never became a real take, so nothing about it (not even its
        # title) is worth calling out the same as one that was actually
        # completed. The status word itself keeps its own color either
        # way, so the two are still tellable apart.
        abandoned = status in ("skipped", "stopped early")
        name_color = self._NAME_COLOR_ABANDONED if abandoned else self._NAME_COLOR
        ttk.Label(
            tracks_frame, text=track["track_name"], anchor="w", font=("TkDefaultFont", 10, "bold"),
            foreground=name_color,
        ).grid(row=grid_row, column=0, sticky="w", pady=3)

        status_text, status_color = self._STATUS_STYLES.get(status, ("", "#888888"))
        takes = track.get("takes", [])
        if status == "completed" and len(takes) > 1:
            status_text = f"{status_text} ({len(takes)} takes)"
        ttk.Label(tracks_frame, text=status_text, foreground=status_color).grid(
            row=grid_row, column=1, sticky="w", padx=(8, 0), pady=3
        )

        if takes:
            takes_row = ttk.Frame(tracks_frame)
            takes_row.grid(row=grid_row, column=2, sticky="w", padx=(8, 0), pady=3)
            # A track normally has one take per instrument that's
            # actually recorded it — everything for one played more than
            # once in the same session still fits on this one line, not
            # hiding any of them.
            for take in takes:
                self._build_take_controls(takes_row, project_name, take)

    def _build_take_controls(self, row: ttk.Frame, project_name: str, take: dict) -> None:
        old_instrument = take["instrument"]  # a label — takes are filed by label
        # Its own sub-frame per take, packed left within takes_row — more
        # than one take on the same line still reads as separate groups
        # rather than one run-on badge/number/button sequence.
        cell = ttk.Frame(row)
        cell.pack(side="left", padx=(0, 14))
        # Label called out as its own colored badge — this is what
        # actually answers "what was this filed under" at a glance,
        # including for a take pulled in from a filter-slot draw's shared
        # inspiration-take index — and the same color for a given label
        # everywhere it's shown (see instrument_colors.py) makes it a
        # fast visual scan across rows.
        make_label_badge(cell, old_instrument).pack(side="left", padx=(0, 6))
        # Just "Take N", not the take's full filename — the badge already
        # gives the label, the track name is right there in column 0, and
        # the rest of the filename (source tag, inspiration id) is noise
        # nobody reads here; the actual file only matters to "▶ Play".
        ttk.Label(cell, text=f"Take {take['take_number']}", foreground="#666666").pack(side="left", padx=(0, 8))
        self._build_play_controls(cell, project_name, take["filename"], old_instrument)

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
