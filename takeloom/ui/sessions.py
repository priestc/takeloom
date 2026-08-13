"""Sessions tab: browse past recording sessions and correct the instrument
they were recorded under, for the case where the "Instrument" dropdown on
the Record tab was left on the wrong one (e.g. a bass take recorded while
still set to "electric").

Four separate, independently-usable actions per session — see backend.py's
list_sessions/get_session_detail/correct_session_instrument/reassign_take/
analyze_take/play_take for exactly what each one touches and why they're
kept apart:

1. "Correct instrument" fixes the session's own historical record
   (session_log.json) — always safe, no ambiguity.
2. "Reassign" re-files one specific track's take file under a different
   instrument — shown per-track, with a confirmation dialog naming the
   exact file before anything on disk moves. Re-keys the take in the
   project's setlist.json for an ordinary track, or in the shared
   vault-wide inspiration-take index for one drawn from an inspiration
   filter slot (see backend.py's reassign_take docstring).
3. "Analyze" is read-only: runs the take's actual recorded audio through
   the frequency-based instrument classifier (audio/instrument_
   classifier.py) and reports which configured instrument it most
   resembles, right next to the "Reassign" row for the same take — a hint
   for whether Reassign is actually warranted, not a replacement for it
   (it never touches anything itself).
4. "▶ Play" opens the take in the OS's default player. Downloads it from
   the backup server first if it isn't already on local disk (see
   backend.py's ensure_take_local) — a take shown here doesn't have to be
   any project's *current* preferred one (see get_session_detail), so
   this can't assume next_untaken_track_index-style "still local" the
   way an active session's own playback can. Also the only one of the
   four that behaves genuinely differently over a Remote connection: the
   server resolves/downloads the file on its own end and streams the
   bytes back in chunks to actually play on the machine looking at this
   tab, not the studio's.

Everything here goes through app_state.backend, same as every other tab —
works identically pointed at local hardware or a Remote connection.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError
from ..config import INSTRUMENT_LABELS, StudioConfig
from .app_state import AppState


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
            text="Past recording sessions. Select one to correct its instrument, or reassign a "
                 "specific take to a different instrument.",
            foreground="#666666", wraplength=760, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="x")
        self.tree = ttk.Treeview(
            tree_frame, columns=("date", "project", "instrument", "tracks"),
            show="headings", height=10, selectmode="browse",
        )
        self.tree.heading("date", text="Date")
        self.tree.heading("project", text="Project")
        self.tree.heading("instrument", text="Instrument")
        self.tree.heading("tracks", text="Tracks")
        self.tree.column("date", width=150)
        self.tree.column("project", width=140)
        self.tree.column("instrument", width=100)
        self.tree.column("tracks", width=360)
        for session in self._sessions:
            self.tree.insert("", "end", iid=session["session_dir"], values=(
                session["date"], session["project"], session["instrument"], ", ".join(session["track_names"]),
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

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, foreground="#2a7d2a", wraplength=760).pack(
            anchor="w", pady=(8, 0)
        )

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
        # Two different vocabularies for two different corrections:
        # "Correct instrument" fixes session_log.json's own record of
        # which *specific* instrument (full_name) played the session, for
        # record-keeping — "Reassign" re-files a take, which is filed by
        # *label* (see backend.py's reassign_take/TrackEntry.
        # preferred_takes), not by which specific piece of gear played it.
        instrument_names = [inst.full_name for inst in self.config_obj.instruments]
        instrument_labels = list(INSTRUMENT_LABELS)
        current_instrument = detail.get("instrument", "")

        header = ttk.Frame(self.detail_frame)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text=f"Recorded as: {current_instrument}", font=("TkDefaultFont", 11, "bold")).pack(
            side="left"
        )
        ttk.Label(header, text="  Correct to:").pack(side="left", padx=(12, 4))
        correct_var = tk.StringVar(value=current_instrument)
        ttk.Combobox(
            header, textvariable=correct_var, values=instrument_names, state="readonly", width=18,
        ).pack(side="left")
        ttk.Button(
            header, text="Correct instrument",
            command=lambda: self._on_correct_instrument(session_dir, correct_var.get()),
        ).pack(side="left", padx=(8, 0))

        ttk.Label(
            self.detail_frame,
            text="\"Correct instrument\" only fixes this session's own record — it doesn't move any "
                 "take file. Use \"Reassign\" below for a specific track's take.",
            foreground="#666666", wraplength=760, justify="left",
        ).pack(anchor="w", pady=(0, 10))

        project_name = detail.get("project", "")
        for track in detail.get("tracks", []):
            self._build_track_row(session_dir, project_name, track, instrument_labels)

    def _build_track_row(
        self, session_dir: str, project_name: str, track: dict, instrument_labels: list[str],
    ) -> None:
        header_row = ttk.Frame(self.detail_frame)
        header_row.pack(fill="x", pady=(6, 0))
        ttk.Label(header_row, text=track["track_name"], width=28, anchor="w", font=("TkDefaultFont", 10, "bold")).pack(
            side="left"
        )

        is_filter_draw = track["is_filter_draw"]

        takes = track.get("takes", [])
        if not takes:
            if not is_filter_draw:
                ttk.Label(header_row, text="no take currently filed for this track", foreground="#888888").pack(
                    side="left"
                )
            return

        # A track normally has one take per instrument that's actually
        # recorded it — one row per (instrument, take) currently on file,
        # each independently reassignable, including one drawn from a
        # filter slot (reassign_take resolves it via the shared vault-wide
        # inspiration-take index in that case — see its docstring), so a
        # track with takes under more than one instrument doesn't hide any
        # of them.
        for take in takes:
            self._build_take_row(session_dir, project_name, track["track_name"], take, instrument_labels)

    def _build_take_row(
        self, session_dir: str, project_name: str, track_name: str, take: dict, instrument_labels: list[str],
    ) -> None:
        old_instrument = take["instrument"]  # a label — takes are filed by label, see reassign_take's docstring
        row = ttk.Frame(self.detail_frame)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text="", width=28).pack(side="left")  # aligns under the track name above
        # Label called out as its own bold label, not just bracketed into
        # the filename text below it — the filename usually repeats it
        # too, but this is what actually answers "what was this filed
        # under" at a glance, including for a take pulled in from a
        # filter-slot draw's shared inspiration-take index.
        ttk.Label(row, text=old_instrument, font=("TkDefaultFont", 9, "bold")).pack(side="left", padx=(0, 6))
        ttk.Label(row, text=take["filename"], foreground="#666666").pack(side="left", padx=(0, 8))
        self._build_play_controls(row, project_name, take["filename"])
        # Default the target to whatever label isn't this take's own —
        # the common case (this whole tab exists for) is a 2-label
        # mix-up, so this is usually already the right answer.
        other_labels = [label for label in instrument_labels if label != old_instrument]
        reassign_var = tk.StringVar(value=(other_labels[0] if other_labels else old_instrument))
        ttk.Combobox(row, textvariable=reassign_var, values=instrument_labels, state="readonly", width=16).pack(
            side="left"
        )
        ttk.Button(
            row, text="Reassign",
            command=lambda: self._on_reassign_take(
                session_dir, track_name, old_instrument, reassign_var, take["filename"],
            ),
        ).pack(side="left", padx=(6, 0))
        self._build_analyze_controls(row, session_dir, track_name, old_instrument)

    def _build_play_controls(self, row: ttk.Frame, project_name: str, filename: str) -> None:
        # Read-only, same spirit as Analyze — opens the take in the OS's
        # default player (backend.py's play_take) rather than anything
        # this tab renders itself. Works identically pointed at local
        # hardware or a Remote connection: locally it just needs the file
        # to exist (downloading it from the backup server first if
        # "remote" vault mode already pruned it — see ensure_take_local),
        # remotely the server does that same local-availability step on
        # its own end and streams the bytes back to actually play here.
        play_var = tk.StringVar(value="")
        ttk.Button(
            row, text="▶ Play",
            command=lambda: self._on_play_take(project_name, filename, play_var),
        ).pack(side="left", padx=(0, 4))
        ttk.Label(row, textvariable=play_var, foreground="#666666").pack(side="left", padx=(0, 4))

    def _build_analyze_controls(self, row: ttk.Frame, session_dir: str, track_name: str, instrument: str) -> None:
        # Read-only (see backend.py's analyze_take) — its result just
        # updates analyze_var in place, no _refresh_after_change/rebuild,
        # so it survives sitting next to Reassign without the two
        # interfering with each other.
        analyze_var = tk.StringVar(value="")
        ttk.Button(
            row, text="Analyze",
            command=lambda: self._on_analyze_take(session_dir, track_name, instrument, analyze_var),
        ).pack(side="left", padx=(10, 0))
        ttk.Label(row, textvariable=analyze_var, foreground="#2a6db0").pack(side="left", padx=(6, 0))

    # --- actions ---

    def _on_play_take(self, project_name: str, filename: str, status_var: tk.StringVar) -> None:
        # "Loading..." matters most over a Remote connection, where this
        # can mean a real wait — downloading the take from the backup
        # server to the studio machine, then streaming it here (see
        # backend.py's play_take/ensure_take_local) — but costs nothing
        # to show locally either, where it's normally near-instant.
        status_var.set("Loading...")
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.play_take(project_name, filename),
            lambda _result, error: self._on_play_take_result(status_var, error),
        )

    def _on_play_take_result(self, status_var: tk.StringVar, error: str | None) -> None:
        if not self.winfo_exists():
            return
        status_var.set("")
        if error:
            messagebox.showerror("Could not play take", error)

    def _on_correct_instrument(self, session_dir: str, new_instrument: str) -> None:
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.correct_session_instrument(session_dir, new_instrument),
            lambda _result, error: self._on_correct_instrument_result(session_dir, new_instrument, error),
        )

    def _on_correct_instrument_result(self, session_dir: str, new_instrument: str, error: str | None) -> None:
        if error:
            messagebox.showerror("Could not correct instrument", error)
            return
        self.status_var.set(f"Session corrected to '{new_instrument}'.")
        self._refresh_after_change(session_dir)

    def _on_reassign_take(
        self, session_dir: str, track_name: str, old_instrument: str, new_instrument_var: tk.StringVar,
        filename: str,
    ) -> None:
        new_instrument = new_instrument_var.get()
        if new_instrument == old_instrument:
            messagebox.showerror("Cannot reassign", "Pick a different instrument to reassign to.")
            return
        if not messagebox.askyesno(
            "Reassign take",
            f"Reassign '{filename}' from '{old_instrument}' to '{new_instrument}'?\n\n"
            "The take file will be renamed and re-filed under the new instrument.",
        ):
            return
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.reassign_take(session_dir, track_name, old_instrument, new_instrument),
            lambda _result, error: self._on_reassign_result(session_dir, track_name, new_instrument, error),
        )

    def _on_reassign_result(self, session_dir: str, track_name: str, new_instrument: str, error: str | None) -> None:
        if error:
            messagebox.showerror("Could not reassign take", error)
            return
        self.status_var.set(f"'{track_name}' reassigned to '{new_instrument}'.")
        self._refresh_after_change(session_dir)

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

    def _refresh_after_change(self, session_dir: str) -> None:
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.get_session_detail(session_dir),
            lambda detail, error: self._on_detail_loaded(session_dir, detail, error),
        )
