"""Edit Backing Track dialog: non-destructively crop a long intro/outro
off a song's backing track, from the Completed Takes tab — see backend.
py's edit_backing_track for the actual operation. No audio file is ever
touched; every instrument's current take, and every future session that
loads this song, plays back the same virtual window instead."""

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
            text="Non-destructive: no audio file is changed. Cropping the backing track also crops "
                 f"every current take of this song to match ({instruments}), and any future session "
                 "that loads this song plays the cropped version too.",
            foreground="#666666", wraplength=380, justify="left",
        ).pack(anchor="w", pady=(4, 10))

        sidecar_takes = [t for t in takes_for_song if t.get("has_video") or t.get("has_midi")]
        if sidecar_takes:
            kinds = ", ".join(sorted({"video" if t.get("has_video") else "MIDI" for t in sidecar_takes}))
            ttk.Label(
                frame,
                text=f"Note: {len(sidecar_takes)} take(s) here have a {kinds} file — only audio playback "
                     "reflects this crop for now, so reviewing the video/MIDI directly still includes "
                     "the untrimmed intro/outro.",
                foreground="#9a6a00", wraplength=380, justify="left",
            ).pack(anchor="w", pady=(0, 10))

        fields = ttk.Frame(frame)
        fields.pack(fill="x")
        ttk.Label(fields, text="Trim from start (seconds):").grid(row=0, column=0, sticky="w", pady=4)
        self.start_var = tk.StringVar(value=_format_seconds(take.get("trim_start_seconds", 0.0)))
        ttk.Entry(fields, textvariable=self.start_var, width=10).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(fields, text="Trim from end (seconds):").grid(row=1, column=0, sticky="w", pady=4)
        self.end_var = tk.StringVar(value=_format_seconds(take.get("trim_end_seconds", 0.0)))
        ttk.Entry(fields, textvariable=self.end_var, width=10).grid(row=1, column=1, sticky="w", padx=(8, 0))

        if take.get("trim_start_seconds") or take.get("trim_end_seconds"):
            ttk.Label(
                frame, text="Already trimmed — shown above. Change the values and click Save to adjust, "
                            "or set both to 0 to remove the trim entirely.",
                foreground="#666666", wraplength=380, justify="left",
            ).pack(anchor="w", pady=(8, 0))

        self.status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_var, foreground="#666666", wraplength=380).pack(
            anchor="w", pady=(8, 0)
        )

        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(12, 0))
        self.cancel_button = ttk.Button(footer, text="Cancel", command=self.destroy)
        self.cancel_button.pack(side="right")
        self.trim_button = ttk.Button(footer, text="Save", command=self._on_trim)
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

        self._working = True
        self.trim_button.state(["disabled"])
        self.cancel_button.state(["disabled"])
        self.status_var.set("Saving...")
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
            messagebox.showerror("Could not edit backing track", error, parent=self)
            return
        self._on_trimmed()
        self.destroy()
        count = len(result["affected_takes"])
        new_duration = result["new_duration_seconds"]
        messagebox.showinfo(
            "Saved",
            f"“{result['track_name']}” now plays back at {int(new_duration) // 60}:{int(new_duration) % 60:02d} "
            f"— {count} take{'s' if count != 1 else ''} will reflect this the next time "
            "it's played or recorded.",
        )


def _format_seconds(value: float) -> str:
    return "0" if not value else f"{value:g}"
