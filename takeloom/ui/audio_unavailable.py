"""The "can't list audio devices" notice shown by Recording Devices and
Studio Setup's Input Labels when the device list comes back empty: says why
(see portaudio_support.py) and, where possible, offers to install PortAudio.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from .. import portaudio_support

ERROR_COLOR = "#b00020"


def build_audio_unavailable_notice(
    parent: tk.Misc, on_installed: Callable[[], None],
) -> ttk.Frame:
    """A frame (not yet placed) explaining the empty device list. With a
    local backend missing PortAudio on Debian/Ubuntu, it carries an
    "Install PortAudio" button that calls `on_installed` once it succeeds
    (normally the screen's own Reload Devices)."""
    frame = ttk.Frame(parent)
    ttk.Label(
        frame, text=portaudio_support.describe_problem(), foreground=ERROR_COLOR,
        wraplength=520, justify="left",
    ).pack(anchor="w")

    if portaudio_support.portaudio_missing() and portaudio_support.can_auto_install():
        status_var = tk.StringVar()
        button = ttk.Button(frame, text="Install PortAudio")
        button.pack(anchor="w", pady=(6, 0))
        ttk.Label(frame, textvariable=status_var).pack(anchor="w")

        def on_done(ok: bool, message: str) -> None:
            if not frame.winfo_exists():
                return
            if ok:
                on_installed()
                return
            status_var.set("")
            button.state(["!disabled"])
            messagebox.showerror("Install PortAudio", message, parent=frame)

        def on_click() -> None:
            button.state(["disabled"])
            status_var.set("Installing libportaudio2 (enter your password in the prompt)...")

            def worker() -> None:
                ok, message = portaudio_support.install_portaudio()
                try:
                    frame.after(0, lambda: on_done(ok, message))
                except (tk.TclError, RuntimeError):
                    pass  # screen closed/rebuilt while installing

            threading.Thread(target=worker, daemon=True).start()

        button.configure(command=on_click)
    return frame
