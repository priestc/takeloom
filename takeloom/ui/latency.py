"""Latency page: record a short instrument+camera test clip against a
continuous metronome click, watch it back, and dial in the audio/video
offsets used to correct AV sync on real takes.

Camera-based calibration is local-only (see backend.LocalBackend/RemoteBackend)
— when app_state.backend is a RemoteBackend, this tab shows a static notice
instead of the controls. Persistent (like Record/Remote): it can hold a live
audio stream + ffmpeg camera capture mid-test, which a tab-switch rebuild
would kill outright.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError
from ..config import StudioConfig
from .app_state import AppState


class LatencyFrame(ttk.Frame):
    """Instrument/camera picker, start/stop test recording, and offset fields."""

    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None
        self._cameras: list[tuple[str, str]] = []
        self._phase = "idle"  # "idle" | "recording"

        self._current_backend = None
        self.app_state.add_listener(self._on_app_state_changed)
        self.bind("<Destroy>", self._on_destroy)

        ttk.Label(self, text="Loading...").pack(anchor="w")
        self._attach_backend()

    # --- backend attach / (re)load ---

    def _attach_backend(self) -> None:
        if self._current_backend is not None:
            self._current_backend.off_event(self._on_backend_event)
        self._current_backend = self.app_state.backend
        self._current_backend.on_event(self._on_backend_event)
        self._phase = "idle"
        self._load()

    def _on_app_state_changed(self) -> None:
        if self.app_state.backend is not self._current_backend:
            self._attach_backend()

    def _load(self) -> None:
        backend = self.app_state.backend
        if backend.is_remote():
            self.after(0, self._build_remote_notice)
            return

        def worker() -> None:
            try:
                config = backend.get_config()
                cameras = backend.list_cameras()
                error = None
            except BackendError as e:
                config, cameras, error = None, [], str(e)
            self.after(0, lambda: self._on_loaded(config, cameras, error))

        threading.Thread(target=worker, daemon=True).start()

    def _build_remote_notice(self) -> None:
        if not self.winfo_exists():
            return
        for child in self.winfo_children():
            child.destroy()
        ttk.Label(
            self, text="Camera latency measurement only works on the local instance.",
            foreground="#b00020",
        ).pack(anchor="w")

    def _on_loaded(self, config: StudioConfig | None, cameras: list[tuple[str, str]], error: str | None) -> None:
        if not self.winfo_exists():
            return
        for child in self.winfo_children():
            child.destroy()
        if error or config is None:
            ttk.Label(self, text=error or "Could not load configuration.", foreground="#b00020").pack(anchor="w")
            return
        self.config_obj = config
        self._cameras = cameras
        self._build()

    # --- build ---

    def _build(self) -> None:
        row = 0
        ttk.Label(self, text="Measure Latency", font=("TkDefaultFont", 14, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        row += 1
        ttk.Label(
            self,
            text=(
                "Pick an instrument and camera, then Start. A steady click plays for a while — "
                "clap or hit your instrument along with it. Stop opens the recorded clip with "
                "today's saved offsets already applied. Adjust an offset below, Save, and run the "
                "test again to check — repeat until the clip looks and sounds right."
            ),
            wraplength=560, foreground="#666666",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 12))
        row += 1

        instrument_names = [inst.full_name for inst in self.config_obj.instruments]
        default_instrument = self.config_obj.last_selected_instrument
        if default_instrument not in instrument_names:
            default_instrument = instrument_names[0] if instrument_names else ""
        ttk.Label(self, text="Instrument").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.instrument_var = tk.StringVar(value=default_instrument)
        self.instrument_combo = ttk.Combobox(
            self, textvariable=self.instrument_var, values=instrument_names, state="readonly", width=28,
        )
        self.instrument_combo.grid(row=row, column=1, sticky="w")
        self.instrument_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_start_button_state())
        row += 1

        camera_names = [display for _dev_id, display in self._cameras]
        default_camera = next(
            (display for dev_id, display in self._cameras if dev_id == self.config_obj.camera_device),
            camera_names[0] if camera_names else "",
        )
        ttk.Label(self, text="Camera").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.camera_var = tk.StringVar(value=default_camera)
        self.camera_combo = ttk.Combobox(
            self, textvariable=self.camera_var, values=camera_names, state="readonly", width=28,
        )
        self.camera_combo.grid(row=row, column=1, sticky="w")
        self.camera_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_start_button_state())
        row += 1

        self.metronome_var = tk.BooleanVar(value=True)
        self.metronome_check = ttk.Checkbutton(
            self, text="Play metronome click", variable=self.metronome_var,
        )
        self.metronome_check.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        row += 1

        if not instrument_names:
            ttk.Label(
                self, text="No instruments configured. Set them up on the Studio Setup tab first.",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1
        if not camera_names:
            ttk.Label(
                self, text="No cameras found. Configure one on the Recording Devices tab first.",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, foreground="#2a7d2a", wraplength=440).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        row += 1

        self.test_button = ttk.Button(self, text="Start Test", command=self._on_toggle_test)
        self.test_button.grid(row=row, column=0, sticky="w", pady=(4, 12))
        row += 1

        ttk.Separator(self, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1

        ttk.Label(self, text="Offsets", font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        ttk.Label(self, text="Audio device offset (ms)").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.audio_offset_var = tk.StringVar(value=str(self.config_obj.latency_compensation_ms))
        ttk.Entry(self, textvariable=self.audio_offset_var, width=10).grid(row=row, column=1, sticky="w")
        row += 1
        ttk.Label(
            self,
            text=(
                "This one won't change the test clip if you rerun it — it only affects overdub "
                "monitoring during a real Record session, not this solo recording. To find the "
                "right value, compare your hit against the click in this clip's audio: increase "
                "if your hit sounds like it lands after the click; decrease toward 0 if it "
                "already lines up."
            ),
            wraplength=440, foreground="#666666", font=("TkDefaultFont", 9),
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=(0, 8), pady=(0, 8))
        row += 1

        ttk.Label(self, text="Video device offset (ms)").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.video_offset_var = tk.StringVar(value=str(self.config_obj.video_latency_compensation_ms))
        ttk.Entry(self, textvariable=self.video_offset_var, width=10).grid(row=row, column=1, sticky="w")
        row += 1
        ttk.Label(
            self,
            text=(
                "This one does change the test clip — Save, then run the test again to check. If "
                "you hear the hit before you see it (video lags), decrease this — it can go "
                "negative. If you see the hit before you hear it (video is ahead), increase this."
            ),
            wraplength=440, foreground="#666666", font=("TkDefaultFont", 9),
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=(0, 8), pady=(0, 8))
        row += 1

        self.columnconfigure(1, weight=1)

        self.save_status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.save_status_var, foreground="#2a7d2a").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        row += 1

        button_row = ttk.Frame(self)
        button_row.grid(row=row, column=0, columnspan=2, sticky="e", pady=(8, 0))
        self.save_button = ttk.Button(button_row, text="Save Offsets", command=self._on_save_offsets)
        self.save_button.pack(side="right")

        self._update_start_button_state()

    def _update_start_button_state(self) -> None:
        if not hasattr(self, "test_button"):
            return
        if self._phase != "idle":
            self.test_button.state(["!disabled"])
            return
        ready = bool(self.instrument_var.get()) and bool(self.camera_var.get())
        self.test_button.state(["!disabled"] if ready else ["disabled"])

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "readonly" if enabled else "disabled"
        self.instrument_combo.configure(state=state)
        self.camera_combo.configure(state=state)
        self.metronome_check.configure(state="normal" if enabled else "disabled")

    def _selected_camera_device(self) -> str:
        display = self.camera_var.get()
        return next((dev_id for dev_id, disp in self._cameras if disp == display), "")

    # --- start/stop test ---

    def _on_toggle_test(self) -> None:
        if self._phase == "idle":
            self._start_test()
        else:
            self._stop_test()

    def _start_test(self) -> None:
        instrument_name = self.instrument_var.get()
        camera_device = self._selected_camera_device()
        if not instrument_name or not camera_device:
            messagebox.showerror("Cannot start", "Select an instrument and a camera first.")
            return

        play_metronome = self.metronome_var.get()
        self.test_button.state(["disabled"])
        self._set_controls_enabled(False)
        self.status_var.set("Starting...")
        backend = self.app_state.backend

        def worker() -> None:
            try:
                backend.start_latency_test(instrument_name, camera_device, play_metronome)
                error = None
            except BackendError as e:
                error = str(e)
            self.after(0, lambda: self._on_start_result(error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_start_result(self, error: str | None) -> None:
        # On success, the "latency_test_status" event already updated the
        # button/state via _handle_backend_event — nothing left to do here.
        if error:
            messagebox.showerror("Cannot start", error)
            self._set_controls_enabled(True)
            self.status_var.set("")
            self._update_start_button_state()

    def _stop_test(self) -> None:
        self.test_button.state(["disabled"])
        self.status_var.set("Finishing up...")
        backend = self.app_state.backend

        def worker() -> None:
            try:
                backend.stop_latency_test()
                error = None
            except BackendError as e:
                error = str(e)
            self.after(0, lambda: self._on_stop_result(error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_stop_result(self, error: str | None) -> None:
        if error:
            messagebox.showerror("Stop failed", error)
            self.test_button.state(["!disabled"])
        # On success, the "latency_test_status" event already updated
        # phase/status back to idle — nothing left to do here.

    # --- backend events ---

    def _on_backend_event(self, event: str, data: dict) -> None:
        self.after(0, lambda: self._handle_backend_event(event, data))

    def _handle_backend_event(self, event: str, data: dict) -> None:
        if event != "latency_test_status":
            return
        if "status" in data:
            self.status_var.set(data["status"])
        if "phase" in data:
            self._phase = data["phase"]
            self.app_state.recording_active = self._phase == "recording"
            self.test_button.configure(text="Stop Test" if self._phase == "recording" else "Start Test")
            self.test_button.state(["!disabled"])
            self._set_controls_enabled(self._phase == "idle")
            self._update_start_button_state()

    # --- offsets ---

    def _on_save_offsets(self) -> None:
        try:
            audio_ms = float(self.audio_offset_var.get())
            video_ms = float(self.video_offset_var.get())
        except ValueError:
            messagebox.showerror("Invalid offset", "Offsets must be numbers.")
            return

        self.config_obj.latency_compensation_ms = audio_ms
        self.config_obj.video_latency_compensation_ms = video_ms

        backend = self.app_state.backend
        config = self.config_obj
        self.save_button.state(["disabled"])

        def worker() -> None:
            try:
                backend.save_config(config)
                error = None
            except BackendError as e:
                error = str(e)
            self.after(0, lambda: self._on_offsets_saved(error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_offsets_saved(self, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self.save_button.state(["!disabled"])
        if error:
            messagebox.showerror("Save failed", error)
            return
        self.save_status_var.set("Offsets saved.")

    def _on_destroy(self, _event: object) -> None:
        if self._current_backend is not None:
            self._current_backend.off_event(self._on_backend_event)
        self.app_state.remove_listener(self._on_app_state_changed)
