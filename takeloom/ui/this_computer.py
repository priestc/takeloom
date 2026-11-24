"""This Computer tab: which takeloom this machine runs, and whether it
follows GitHub's main (development mode, update_check.py). It used to be a
section of the Remote tab, which went with remote mode."""

from __future__ import annotations

import tkinter as tk
from importlib import metadata
from tkinter import ttk

from .. import update_check
from ..backend import BackendError
from .app_state import AppState


class ThisComputerFrame(ttk.Frame):
    def __init__(self, parent: tk.Misc, app_state: AppState) -> None:
        super().__init__(parent)
        self.app_state = app_state
        frame = ttk.Frame(self)
        frame.pack(fill="x", anchor="w", padx=4, pady=4)
        ttk.Label(frame, text="This Computer's Takeloom", font=("TkDefaultFont", 12, "bold")).pack(
            anchor="w", pady=(0, 8)
        )
        kind = update_check.install_kind()
        try:
            version = metadata.version("takeloom")
        except metadata.PackageNotFoundError:
            version = "unknown"
        source = {
            "editable": "an editable install of a working copy",
            "git": "GitHub's main branch, via pipx",
            "pipx-release": "a release, via pipx",
            "packaged": "a package manager",
        }[kind]
        ttk.Label(frame, text=f"Version {version}, installed from {source}.").pack(anchor="w")
        ttk.Label(
            frame, foreground="#666666", wraplength=560, justify="left",
            text="To control another machine (the Mac Mini's takeloom server, say), use the vault's web page: "
                 "its Cameras and Setlists pages reach every recording node connected to the vault.",
        ).pack(anchor="w", pady=(8, 0))

        if not update_check.development_mode_available():
            note = (
                "Always runs its working copy as-is." if kind == "editable"
                else "Updated by whatever installed it."
            )
            ttk.Label(frame, text=note, foreground="#666666").pack(anchor="w", pady=(8, 0))
            return
        try:
            config = app_state.local_backend.get_config()
            enabled = update_check.development_mode_enabled(config)
        except BackendError:
            enabled = False
        self.dev_mode_var = tk.BooleanVar(value=enabled)
        ttk.Checkbutton(
            frame, text="Development mode: follow GitHub's main, updating every time Takeloom opens",
            variable=self.dev_mode_var, command=self._on_toggle_dev_mode,
        ).pack(anchor="w", pady=(12, 0))
        self.dev_mode_status_var = tk.StringVar(value=(
            "Off: stays on this release." if kind == "pipx-release" else "On: updates to the newest commit on each launch."
        ))
        ttk.Label(frame, textvariable=self.dev_mode_status_var, foreground="#666666",
                  wraplength=560, justify="left").pack(anchor="w", pady=(4, 0))

    def _on_toggle_dev_mode(self) -> None:
        on = self.dev_mode_var.get()
        backend = self.app_state.local_backend
        try:
            config = backend.get_config()  # fresh: never a stale copy (another window may have saved)
            config.development_mode = on
            backend.save_config(config)
        except BackendError as e:
            self.dev_mode_status_var.set(f"Couldn't save: {e}")
            return
        on_main = update_check.install_kind() == "git"
        if on == on_main:
            self.dev_mode_status_var.set(
                "On: updates to the newest commit on each launch." if on else "Off: stays on this release."
            )
        elif on:
            self.dev_mode_status_var.set("On: switches to GitHub's main the next time Takeloom opens.")
        else:
            self.dev_mode_status_var.set("Off: switches back to the latest release the next time Takeloom opens.")
