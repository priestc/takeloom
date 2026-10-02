"""Edit Backing Track dialog: permanently trim a long intro/outro off a
song's backing track, from the Completed Takes tab — see backend.py's
edit_backing_track for the actual operation (every instrument's current
take on the same song gets the identical cut, to stay in sync)."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from ..backend import Backend, BackendError


class EditBackingTrackDialog(tk.Toplevel):
    def __init__(
        self, master: tk.Misc, backend: Backend, take: dict, takes_for_song: list[dict],
        on_trimmed: Callable[[], None],
    ) -> None:
        super().__init__(master)
        self.title("Edit Backing Track")
        self.resizable(False, False)
        self.transient(master)

        self._backend = backend
        self._take = take
        self._on_trimmed = on_trimmed
        self._working = False

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text=take["track_name"], font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        instruments = ", ".join(sorted({t["instrument"] for t in takes_for_song}))
        ttk.Label(
            frame,
            text=f"Trimming the backing track also trims every current take of this song "
                 f"to match ({instruments}), so everything stays in sync.",
            foreground="#666666", wraplength=380, justify="left",
        ).pack(anchor="w", pady=(4, 10))

        sidecar_takes = [t for t in takes_for_song if t.get("has_video") or t.get("has_midi")]
        if sidecar_takes:
            kinds = ", ".join(sorted({"video" if t.get("has_video") else "MIDI" for t in sidecar_takes}))
            ttk.Label(
                frame,
                text=f"Note: {len(sidecar_takes)} take(s) here have a {kinds} file — only their audio "
                     "gets trimmed; the video/MIDI file is left as-is and will no longer line up.",
                foreground="#9a6a00", wraplength=380, justify="left",
            ).pack(anchor="w", pady=(0, 10))

        fields = ttk.Frame(frame)
        fields.pack(fill="x")
        ttk.Label(fields, text="Trim from start (seconds):").grid(row=0, column=0, sticky="w", pady=4)
        self.start_var = tk.StringVar(value="0")
        ttk.Entry(fields, textvariable=self.start_var, width=10).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(fields, text="Trim from end (seconds):").grid(row=1, column=0, sticky="w", pady=4)
        self.end_var = tk.StringVar(value="0")
        ttk.Entry(fields, textvariable=self.end_var, width=10).grid(row=1, column=1, sticky="w", padx=(8, 0))

        ttk.Label(
            frame, text="This cannot be undone.", foreground="#b00020",
        ).pack(anchor="w", pady=(10, 0))

        self.status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_var, foreground="#666666", wraplength=380).pack(
            anchor="w", pady=(8, 0)
        )

        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(12, 0))
        self.cancel_button = ttk.Button(footer, text="Cancel", command=self.destroy)
        self.cancel_button.pack(side="right")
        self.trim_button = ttk.Button(footer, text="Trim", command=self._on_trim)
        self.trim_button.pack(side="right", padx=(0, 8))

    def _on_trim(self) -> None:
        if self._working:
            return
        try:
            trim_start = float(self.start_var.get().strip() or "0")
            trim_end = float(self.end_var.get().strip() or "0")
        except ValueError:
            messagebox.showerror("Invalid trim amount", "Enter trim amounts as a number of seconds.", parent=self)
            return
        if trim_start < 0 or trim_end < 0:
            messagebox.showerror("Invalid trim amount", "Trim amounts can't be negative.", parent=self)
            return
        if trim_start == 0 and trim_end == 0:
            messagebox.showerror("Nothing to trim", "Enter a trim amount for the start and/or end.", parent=self)
            return
        if not messagebox.askyesno(
            "Trim backing track",
            f"Trim {trim_start:g}s from the start and {trim_end:g}s from the end of "
            f"“{self._take['track_name']}” and every current take of it?\n\nThis cannot be undone.",
            parent=self,
        ):
            return

        self._working = True
        self.trim_button.state(["disabled"])
        self.cancel_button.state(["disabled"])
        self.status_var.set("Trimming...")
        backend = self._backend
        filename = self._take["filename"]

        def worker() -> None:
            try:
                result, error = backend.edit_backing_track(filename, trim_start, trim_end), None
            except BackendError as e:
                result, error = None, str(e)
            self.after(0, lambda: self._on_done(result, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_done(self, result: dict | None, error: str | None) -> None:
        self._working = False
        if not self.winfo_exists():
            return
        if error:
            self.status_var.set("")
            self.trim_button.state(["!disabled"])
            self.cancel_button.state(["!disabled"])
            messagebox.showerror("Could not trim", error, parent=self)
            return
        self._on_trimmed()
        self.destroy()
        count = len(result["takes_trimmed"])
        messagebox.showinfo(
            "Trimmed",
            f"Trimmed the backing track and {count} take{'s' if count != 1 else ''} of "
            f"“{result['track_name']}”. New duration: "
            f"{int(result['new_duration_seconds']) // 60}:{int(result['new_duration_seconds']) % 60:02d}.",
        )
