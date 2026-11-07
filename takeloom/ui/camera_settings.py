"""video-ui's Settings › Cameras: every camera ever seen (its name, Hide)
and where session videos are encoded. Cameras are a set: every connected one
that isn't hidden records its own video, and none is special.

Moved here from the main window's Recording Devices and Studio Setup tabs:
every video/camera setting lives in video-ui now, the main window is for
audio. Saving reloads the config first and changes only these fields, so it
never undoes something saved meanwhile from the main window.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError
from .app_state import AppState

_VIDEO_ENCODING_LABELS = [("local", "This machine"), ("vault", "Vault server")]


class CameraSettingsFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master, padding=16)
        self.app_state = app_state
        self.config_obj = None
        self._inventory: list[dict] = []
        self._camera_vars: dict[str, dict] = {}
        self._forgotten: set[str] = set()
        ttk.Label(self, text="Loading...").grid(row=0, column=0, sticky="w")
        self._load()

    def _load(self) -> None:
        backend = self.app_state.backend

        def worker() -> None:
            try:
                # Cameras before the config: the scan behind camera_inventory()
                # remembers any new camera in it.
                cameras = backend.camera_inventory()
                config = backend.get_config()
                error = None
            except BackendError as e:
                config, cameras, error = None, [], str(e)
            self.after(0, lambda: self._on_loaded(config, cameras, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, config, cameras: list[dict], error: str | None) -> None:
        if not self.winfo_exists():
            return
        for child in self.winfo_children():
            child.destroy()
        if config is None:
            ttk.Label(self, text=error or "Could not load the configuration.", foreground="#b00020").grid(
                row=0, column=0, sticky="w")
            return
        self.config_obj = config
        self._inventory = list(cameras)
        self._forgotten = set()
        self._build()

    def _build(self) -> None:
        row = 0
        ttk.Label(self, text="Cameras", font=("TkDefaultFont", 11, "bold")).grid(row=row, column=0, sticky="w")
        ttk.Button(self, text="Reload", command=self._reload).grid(row=row, column=1, sticky="e")
        row += 1

        table = ttk.Frame(self)
        table.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        row += 1
        self._camera_vars = {}
        cameras = [c for c in self.config_obj.known_cameras if c.key not in self._forgotten]
        if not cameras:
            ttk.Label(table, text="No cameras seen yet. Connect one, or open Takeloom Camera on a phone, "
                                  "then Reload.", foreground="#666666").grid(row=0, column=0, sticky="w")
        else:
            for col, heading in enumerate(("Name", "Detected as", "", "Hide", "")):
                ttk.Label(table, text=heading, foreground="#666666").grid(row=0, column=col, sticky="w", padx=(0, 10))
        connected = {c["key"]: c["connected"] for c in self._inventory}
        for r, camera in enumerate(cameras, start=1):
            name_var = tk.StringVar(value=camera.name)
            hidden_var = tk.BooleanVar(value=camera.hidden)
            self._camera_vars[camera.key] = {"name": name_var, "hidden": hidden_var}
            ttk.Entry(table, textvariable=name_var, width=22).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=2)
            ttk.Label(table, text=camera.detected_name or camera.key).grid(row=r, column=1, sticky="w", padx=(0, 10))
            is_connected = connected.get(camera.key, False)
            ttk.Label(
                table,
                text="● connected" if is_connected else f"last seen {camera.last_seen or 'never'}",
                foreground="#2a7d2a" if is_connected else "#888888",
            ).grid(row=r, column=2, sticky="w", padx=(0, 10))
            ttk.Checkbutton(table, variable=hidden_var).grid(row=r, column=3)
            if not is_connected:
                ttk.Button(table, text="Forget", command=lambda k=camera.key: self._forget(k)).grid(
                    row=r, column=4, sticky="w")
        ttk.Label(
            self,
            text="Every connected camera that isn't hidden is shown and recorded each session, under the "
                 "name given here (blank: the name it reports), and every take can be watched from any of "
                 "them. Hide anything that isn't really a camera, like a screen capture.",
            foreground="#666666", wraplength=620, justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 4))
        row += 1

        ttk.Separator(self, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        ttk.Label(self, text="Session videos", font=("TkDefaultFont", 11, "bold")).grid(row=row, column=0, sticky="w")
        row += 1
        encoding = ttk.Frame(self)
        encoding.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        row += 1
        ttk.Label(encoding, text="Encode session videos on").pack(side="left", padx=(0, 8))
        current = next((label for value, label in _VIDEO_ENCODING_LABELS
                        if value == self.config_obj.video_encoding), _VIDEO_ENCODING_LABELS[0][1])
        self.video_encoding_var = tk.StringVar(value=current)
        ttk.Combobox(encoding, textvariable=self.video_encoding_var,
                     values=[label for _v, label in _VIDEO_ENCODING_LABELS], state="readonly", width=16).pack(side="left")
        ttk.Label(
            self,
            text="Encoding every camera's session video is the heavy part of processing a session. "
                 "\"Vault server\" leaves it to the vault's CPU: for a vault that's a capable computer, "
                 "not a low-powered NAS. Needs \"Remote only\" vault storage (main window › Studio Setup) "
                 "and a vault that can encode (takeloom-vault 0.2 or later, with ffmpeg); otherwise this "
                 "machine encodes.",
            foreground="#666666", wraplength=620, justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 4))
        row += 1

        bar = ttk.Frame(self)
        bar.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        self.status_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.status_var, foreground="#2a6db0").pack(side="left")
        self.save_button = ttk.Button(bar, text="Save", command=self._save)
        self.save_button.pack(side="right")
        self.columnconfigure(1, weight=1)

    def _reload(self) -> None:
        for child in self.winfo_children():
            child.destroy()
        ttk.Label(self, text="Loading...").grid(row=0, column=0, sticky="w")
        self._load()

    def _forget(self, key: str) -> None:
        self._forgotten.add(key)
        self._keep_form_edits()
        for child in self.winfo_children():
            child.destroy()
        self._build()
        self.status_var.set("Forgotten — Save to keep it that way.")

    def _keep_form_edits(self) -> None:
        """The form's current values into config_obj (before a rebuild)."""
        for camera in self.config_obj.known_cameras:
            fields = self._camera_vars.get(camera.key)
            if fields is not None:
                camera.name = fields["name"].get().strip()
                camera.hidden = fields["hidden"].get()

    def _save(self) -> None:
        self._keep_form_edits()
        edits = {c.key: (c.name, c.hidden) for c in self.config_obj.known_cameras}
        forgotten = set(self._forgotten)
        encoding = next((value for value, label in _VIDEO_ENCODING_LABELS
                         if label == self.video_encoding_var.get()), "local")
        backend = self.app_state.backend
        self.save_button.state(["disabled"])

        def worker() -> None:
            try:
                config = backend.get_config()  # fresh: keep what the main window saved meanwhile
                config.known_cameras = [c for c in config.known_cameras if c.key not in forgotten]
                for camera in config.known_cameras:
                    if camera.key in edits:
                        camera.name, camera.hidden = edits[camera.key]
                config.video_encoding = encoding
                errors = config.validate()
                if errors:
                    raise BackendError("\n".join(errors))
                backend.save_config(config)
                error = None
            except BackendError as e:
                config, error = None, str(e)
            self.after(0, lambda: self._on_saved(config, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_saved(self, config, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self.save_button.state(["!disabled"])
        if error:
            messagebox.showerror("Couldn't save", error)
            return
        self.config_obj = config
        self._forgotten = set()
        self.status_var.set("Saved.")
