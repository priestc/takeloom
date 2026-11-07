"""Streaming tab: turn on live streaming to YouTube, set the stream key and
quality, and (optionally) connect a YouTube account so each session's
stream gets a real title.

When enabled, every session automatically streams live for its whole
duration — starting the moment recording starts and ending the moment it
stops (see backend.py's _begin_session_locked/_end_session and
takeloom/streaming.py). Reads/writes whichever machine's config
app_state.backend currently points at, same as Studio Setup.

The "Connect YouTube Account" OAuth flow (takeloom/youtube_api.py) is the
one exception to that local-or-remote-transparent pattern: it opens a
browser and waits for a redirect on a local loopback port, so it always
runs against *this* machine — right here in the UI layer, never proxied
through Backend/RemoteBackend. The resulting refresh token still gets
saved into whichever config app_state.backend points at, same as
everything else on this tab."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError
from ..config import StudioConfig
from ..streaming import STREAM_QUALITY_PRESETS
from ..youtube_api import YouTubeAPIError, revoke, run_oauth_flow
from .app_state import AppState

_VISIBILITY_LABELS = [("public", "Public"), ("unlisted", "Unlisted"), ("private", "Private")]


class StreamingFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None

        ttk.Label(self, text="Loading...").pack(anchor="w")
        self._load()

    # --- loading ---

    def _load(self) -> None:
        backend = self.app_state.backend

        def worker() -> None:
            try:
                config = backend.get_config()
                error = None
            except BackendError as e:
                config, error = None, str(e)
            self.after(0, lambda: self._on_loaded(config, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, config: StudioConfig | None, error: str | None) -> None:
        if not self.winfo_exists():
            return
        for child in self.winfo_children():
            child.destroy()
        if error or config is None:
            ttk.Label(self, text=error or "Could not load configuration.", foreground="#b00020").pack(anchor="w")
            return
        self.config_obj = config
        self._build()

    # --- build ---

    def _build(self) -> None:
        ttk.Label(self, text="Streaming", font=("TkDefaultFont", 14, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        ttk.Label(
            self,
            text="When enabled, every session streams live to YouTube for its whole duration — the "
                 "stream starts the moment Record is pressed and ends the moment the session stops. "
                 "Requires a connected camera (see Settings › Cameras).",
            foreground="#666666", wraplength=560, justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))

        self.enabled_var = tk.BooleanVar(value=self.config_obj.streaming_enabled)
        ttk.Checkbutton(
            self, text="Stream every session live to YouTube", variable=self.enabled_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(self, text="YouTube stream key").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        self.key_var = tk.StringVar(value=self.config_obj.youtube_stream_key)
        ttk.Entry(self, textvariable=self.key_var, width=48, show="*").grid(row=3, column=1, sticky="ew", pady=4)
        self.columnconfigure(1, weight=1)

        ttk.Label(
            self, text="Find this under “Go Live” in YouTube Studio (studio.youtube.com).",
            foreground="#666666",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 16))

        ttk.Label(self, text="Stream quality").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=4)
        self._quality_labels = [label for label, _width, _bitrate in STREAM_QUALITY_PRESETS]
        self.quality_var = tk.StringVar(value=self._current_quality_label())
        ttk.Combobox(
            self, textvariable=self.quality_var, values=self._quality_labels, state="readonly", width=32,
        ).grid(row=5, column=1, sticky="w", pady=4)

        ttk.Label(
            self, text="A higher bitrate looks better but needs more upload bandwidth — YouTube's own "
                 "recommended ranges are used here.",
            foreground="#666666",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 16))

        row = self._build_oauth_section(7)

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, foreground="#2a7d2a").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        row += 1

        button_row = ttk.Frame(self)
        button_row.grid(row=row, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.save_button = ttk.Button(button_row, text="Save", command=self._on_save)
        self.save_button.pack(side="right")

    def _current_quality_label(self) -> str:
        """The preset label matching the config's saved width/bitrate, or
        the first preset if the config predates this dropdown (its
        defaults match STREAM_QUALITY_PRESETS[0] exactly) or was hand-edited
        to a combination that isn't one of the presets."""
        for label, width, bitrate in STREAM_QUALITY_PRESETS:
            if width == self.config_obj.streaming_video_width and bitrate == self.config_obj.streaming_bitrate_kbps:
                return label
        return STREAM_QUALITY_PRESETS[0][0]

    # --- YouTube account (title automation) ---

    def _build_oauth_section(self, row: int) -> int:
        ttk.Separator(self, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1

        ttk.Label(self, text="YouTube Account (optional — for automatic titles)", font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1
        ttk.Label(
            self,
            text="Connect a YouTube account to have each session's stream titled automatically with the "
                 "studio, musician, project, and date. RTMP (the stream key above) can't carry a title on its "
                 "own, so this needs its own Google sign-in: create an OAuth Client ID (type \"Desktop app\") in "
                 "the Google Cloud Console for a project with the YouTube Data API v3 enabled, then paste its "
                 "Client ID and Secret below.",
            foreground="#666666", wraplength=560, justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 10))
        row += 1

        ttk.Label(self, text="OAuth Client ID").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.client_id_var = tk.StringVar(value=self.config_obj.youtube_oauth_client_id)
        ttk.Entry(self, textvariable=self.client_id_var, width=48).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1

        ttk.Label(self, text="OAuth Client Secret").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.client_secret_var = tk.StringVar(value=self.config_obj.youtube_oauth_client_secret)
        ttk.Entry(self, textvariable=self.client_secret_var, width=48, show="*").grid(row=row, column=1, sticky="ew", pady=4)
        row += 1

        ttk.Label(self, text="Broadcast visibility").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.visibility_var = tk.StringVar(value=self._current_visibility_label())
        ttk.Combobox(
            self, textvariable=self.visibility_var, values=[label for _v, label in _VISIBILITY_LABELS],
            state="readonly", width=16,
        ).grid(row=row, column=1, sticky="w", pady=4)
        row += 1

        ttk.Label(self, text="Title template").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.title_template_var = tk.StringVar(value=self.config_obj.youtube_title_template)
        ttk.Entry(self, textvariable=self.title_template_var, width=48).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1

        ttk.Label(self, text="Description template").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.description_template_var = tk.StringVar(value=self.config_obj.youtube_description_template)
        ttk.Entry(self, textvariable=self.description_template_var, width=48).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1

        ttk.Label(
            self,
            text="Placeholders: {date}, {studio}, {studio-location}, {musician}, {project}, {instrument name}.",
            foreground="#666666",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 10))
        row += 1

        connect_row = ttk.Frame(self)
        connect_row.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.oauth_status_var = tk.StringVar(value=self._connection_status_text())
        ttk.Label(connect_row, textvariable=self.oauth_status_var).pack(side="left", padx=(0, 12))
        self.connect_button = ttk.Button(connect_row, text=self._connect_button_text(), command=self._on_connect_clicked)
        self.connect_button.pack(side="left")
        row += 1

        return row

    def _current_visibility_label(self) -> str:
        for value, label in _VISIBILITY_LABELS:
            if value == self.config_obj.youtube_broadcast_visibility:
                return label
        return _VISIBILITY_LABELS[1][1]  # "Unlisted" — the safe default if config has an unrecognized value

    def _is_connected(self) -> bool:
        return bool(self.config_obj.youtube_oauth_refresh_token)

    def _connection_status_text(self) -> str:
        return "Connected to YouTube." if self._is_connected() else "Not connected — streams won't be auto-titled."

    def _connect_button_text(self) -> str:
        return "Disconnect" if self._is_connected() else "Connect YouTube Account"

    def _on_connect_clicked(self) -> None:
        if self._is_connected():
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        client_id = self.client_id_var.get().strip()
        client_secret = self.client_secret_var.get().strip()
        if not client_id or not client_secret:
            messagebox.showerror("Missing credentials", "Enter both the OAuth Client ID and Client Secret first.")
            return

        self.connect_button.state(["disabled"])
        self.oauth_status_var.set("Waiting for authorization in your browser...")

        def worker() -> None:
            try:
                refresh_token = run_oauth_flow(client_id, client_secret)
                error = None
            except YouTubeAPIError as e:
                refresh_token, error = None, str(e)
            self.after(0, lambda: self._on_connected(client_id, client_secret, refresh_token, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_connected(self, client_id: str, client_secret: str, refresh_token: str | None, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self.connect_button.state(["!disabled"])
        if error:
            self.oauth_status_var.set(self._connection_status_text())
            messagebox.showerror("Could not connect YouTube account", error)
            return

        # Persisted immediately (not deferred to the main Save button) so a
        # successful authorization is never lost by navigating away without
        # remembering to hit Save separately.
        self.config_obj.youtube_oauth_client_id = client_id
        self.config_obj.youtube_oauth_client_secret = client_secret
        self.config_obj.youtube_oauth_refresh_token = refresh_token
        self._save_silently(on_done=lambda: self._after_connection_state_change())

    def _disconnect(self) -> None:
        refresh_token = self.config_obj.youtube_oauth_refresh_token
        self.connect_button.state(["disabled"])

        def worker() -> None:
            if refresh_token:
                revoke(refresh_token)  # best-effort; never raises
            self.after(0, self._on_disconnected)

        threading.Thread(target=worker, daemon=True).start()

    def _on_disconnected(self) -> None:
        if not self.winfo_exists():
            return
        self.config_obj.youtube_oauth_refresh_token = ""
        self._save_silently(on_done=lambda: self._after_connection_state_change())

    def _after_connection_state_change(self) -> None:
        if not self.winfo_exists():
            return
        self.connect_button.state(["!disabled"])
        self.connect_button.configure(text=self._connect_button_text())
        self.oauth_status_var.set(self._connection_status_text())

    def _save_silently(self, on_done) -> None:
        """Save self.config_obj as it currently stands, without touching
        the visible Save button/status line — used by the connect/
        disconnect flows, which have their own status feedback."""
        backend = self.app_state.backend
        config = self.config_obj

        def worker() -> None:
            try:
                from ..config import STREAMING_FIELDS, copy_fields
                backend.save_config(copy_fields(backend.get_config(), config, STREAMING_FIELDS))
            except BackendError as e:
                self.after(0, lambda: messagebox.showerror("Save failed", str(e)))
                return
            self.after(0, on_done)

        threading.Thread(target=worker, daemon=True).start()

    # --- save ---

    def _on_save(self) -> None:
        self.config_obj.streaming_enabled = self.enabled_var.get()
        self.config_obj.youtube_stream_key = self.key_var.get().strip()
        for label, width, bitrate in STREAM_QUALITY_PRESETS:
            if label == self.quality_var.get():
                self.config_obj.streaming_video_width = width
                self.config_obj.streaming_bitrate_kbps = bitrate
                break
        self.config_obj.youtube_oauth_client_id = self.client_id_var.get().strip()
        self.config_obj.youtube_oauth_client_secret = self.client_secret_var.get().strip()
        for value, label in _VISIBILITY_LABELS:
            if label == self.visibility_var.get():
                self.config_obj.youtube_broadcast_visibility = value
                break
        self.config_obj.youtube_title_template = self.title_template_var.get()
        self.config_obj.youtube_description_template = self.description_template_var.get()

        errors = self.config_obj.validate()
        if errors:
            messagebox.showerror("Invalid configuration", "\n".join(errors))
            return

        backend = self.app_state.backend
        config = self.config_obj
        self.save_button.state(["disabled"])

        def worker() -> None:
            try:
                from ..config import STREAMING_FIELDS, copy_fields
                backend.save_config(copy_fields(backend.get_config(), config, STREAMING_FIELDS))
                error = None
            except BackendError as e:
                error = str(e)
            self.after(0, lambda: self._on_saved(error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_saved(self, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self.save_button.state(["!disabled"])
        if error:
            messagebox.showerror("Save failed", error)
            return
        target = f"remote ({self.app_state.remote_name})" if self.app_state.backend.is_remote() else "local config"
        self.status_var.set(f"Saved to {target}")
