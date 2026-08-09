"""Sessions tab: browse past recording sessions and correct the instrument
they were recorded under, for the case where the "Instrument" dropdown on
the Record tab was left on the wrong one (e.g. a bass take recorded while
still set to "electric").

Two separate, independently-usable actions per session — see backend.py's
list_sessions/get_session_detail/correct_session_instrument/reassign_take
for exactly what each one touches and why they're kept apart:

1. "Correct instrument" fixes the session's own historical record
   (session_log.json) — always safe, no ambiguity.
2. "Reassign" re-files one specific track's take file + setlist.json entry
   under a different instrument — shown per-track, with a confirmation
   dialog naming the exact file before anything on disk moves. Not offered
   for an inspiration filter-slot draw (see backend.py's reassign_take
   docstring for why that case can't be done reliably).

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
        instrument_names = [inst.name for inst in self.config_obj.instruments]
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

        for track in detail.get("tracks", []):
            self._build_track_row(session_dir, track, instrument_names)

    def _build_track_row(self, session_dir: str, track: dict, instrument_names: list[str]) -> None:
        header_row = ttk.Frame(self.detail_frame)
        header_row.pack(fill="x", pady=(6, 0))
        ttk.Label(header_row, text=track["track_name"], width=28, anchor="w", font=("TkDefaultFont", 10, "bold")).pack(
            side="left"
        )

        if track["is_filter_draw"]:
            ttk.Label(
                header_row, text="drawn from an inspiration filter — not reassignable here", foreground="#888888",
            ).pack(side="left")
            return

        takes = track.get("takes", [])
        if not takes:
            ttk.Label(header_row, text="no take currently filed for this track", foreground="#888888").pack(
                side="left"
            )
            return

        # A track normally has one take per instrument that's actually
        # recorded it — one row per (instrument, take) currently on file,
        # each independently reassignable, so a track with takes under
        # more than one instrument doesn't hide any of them.
        for take in takes:
            self._build_take_row(session_dir, track["track_name"], take, instrument_names)

    def _build_take_row(self, session_dir: str, track_name: str, take: dict, instrument_names: list[str]) -> None:
        old_instrument = take["instrument"]
        row = ttk.Frame(self.detail_frame)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text="", width=28).pack(side="left")  # aligns under the track name above
        ttk.Label(row, text=f"[{old_instrument}] {take['filename']}", foreground="#666666").pack(
            side="left", padx=(0, 8)
        )
        # Default the target to whatever instrument isn't this take's own —
        # the common case (this whole tab exists for) is a 2-instrument
        # mix-up, so this is usually already the right answer.
        other_names = [name for name in instrument_names if name != old_instrument]
        reassign_var = tk.StringVar(value=(other_names[0] if other_names else old_instrument))
        ttk.Combobox(row, textvariable=reassign_var, values=instrument_names, state="readonly", width=14).pack(
            side="left"
        )
        ttk.Button(
            row, text="Reassign",
            command=lambda: self._on_reassign_take(
                session_dir, track_name, old_instrument, reassign_var, take["filename"],
            ),
        ).pack(side="left", padx=(6, 0))

    # --- actions ---

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

    def _refresh_after_change(self, session_dir: str) -> None:
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.get_session_detail(session_dir),
            lambda detail, error: self._on_detail_loaded(session_dir, detail, error),
        )
