"""Recording Devices screen: sample rate, buffer, output device, Stream
Deck. (Cameras are in video-ui's Settings tab: ui/camera_settings.py.)

Mirrors the fields configured by the `takeloom setup-recording-devices` CLI
command (input labels excepted — see studio_setup.py, grouped there with
instruments instead). Reads/writes whichever machine's config
`app_state.backend` currently points at, and queries that same machine's
audio devices.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError
from ..config import StudioConfig, VALID_BUFFER_SIZES, VALID_SAMPLE_RATES
from .app_state import AppState


class RecordingDevicesFrame(ttk.Frame):
    """Form for sample rate, buffer, output device, and Stream Deck."""

    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None
        self.devices: list[dict] = []
        self.output_devices: list[dict] = []
        self._streamdeck_choices: list[tuple[str, str]] = []

        ttk.Label(self, text="Loading...").pack(anchor="w")
        self._initial_load()

    # --- loading ---

    def _initial_load(self) -> None:
        backend = self.app_state.backend

        def worker() -> None:
            try:
                config = backend.get_config()
                devices = backend.list_audio_devices()
                streamdecks = backend.list_streamdecks()
                error = None
            except BackendError as e:
                config, devices, streamdecks, error = None, [], [], str(e)
            self.after(0, lambda: self._on_initial_loaded(config, devices, streamdecks, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_initial_loaded(
        self, config: StudioConfig | None, devices: list[dict],
        streamdecks: list[tuple[str, str]], error: str | None,
    ) -> None:
        if not self.winfo_exists():
            return
        for child in self.winfo_children():
            child.destroy()
        if error or config is None:
            ttk.Label(self, text=error or "Could not load configuration.", foreground="#b00020").pack(anchor="w")
            return
        self.config_obj = config
        self._set_devices(devices, streamdecks)
        self._build()

    def _set_devices(self, devices: list[dict], streamdecks: list[tuple[str, str]]) -> None:
        self.devices = devices
        self.output_devices = [d for d in devices if d["max_output_channels"] > 0]
        # A device that's configured but momentarily not detected (unplugged,
        # or racing another process for it — see streamdeck_controller.py's
        # _open_by_id) must still appear as a choice here, selected, using
        # its last-saved label. Otherwise the dropdown falls back to "None"
        # (recording_devices.py's "current display" lookups below) and
        # Save would then read that "None" back and permanently clear the
        # saved device_id/label, even though the user touched nothing.
        self._streamdeck_choices = [("", "None")] + self._with_configured_device(
            streamdecks, self.config_obj.streamdeck_id if self.config_obj else "",
            self.config_obj.streamdeck_label if self.config_obj else "",
        )

    @staticmethod
    def _with_configured_device(
        detected: list[tuple[str, str]], configured_id: str, configured_label: str,
    ) -> list[tuple[str, str]]:
        if not configured_id or any(dev_id == configured_id for dev_id, _ in detected):
            return list(detected)
        return list(detected) + [(configured_id, configured_label or configured_id)]

    def _build(self) -> None:
        row = 0
        self.reload_button = ttk.Button(self, text="Reload Devices", command=self._on_reload_devices)
        self.reload_button.grid(row=row, column=0, sticky="w", pady=(0, 10))
        row += 1

        if not self.devices:
            ttk.Label(
                self, text="Could not query audio devices (sounddevice unavailable).",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 8))
            row += 1

        ttk.Label(self, text="Sample rate").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.sample_rate_var = tk.StringVar(value=str(self.config_obj.sample_rate))
        ttk.Combobox(
            self, textvariable=self.sample_rate_var,
            values=[str(r) for r in VALID_SAMPLE_RATES], state="readonly", width=10,
        ).grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Label(self, text="Buffer size").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.buffer_size_var = tk.StringVar(value=str(self.config_obj.buffer_size))
        ttk.Combobox(
            self, textvariable=self.buffer_size_var,
            values=[str(b) for b in VALID_BUFFER_SIZES], state="readonly", width=10,
        ).grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Label(self, text="Output device").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.output_device_var = tk.StringVar(value=self.config_obj.output_device)
        self.output_device_box = ttk.Combobox(self, textvariable=self.output_device_var, width=34)
        self.output_device_box.grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Label(self, text="Output channels").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.output_channels_var = tk.IntVar(value=self.config_obj.output_channels)
        ttk.Spinbox(self, from_=1, to=32, textvariable=self.output_channels_var, width=6).grid(
            row=row, column=1, sticky="w"
        )
        row += 1

        ttk.Label(self, text="Latency compensation (ms)").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        default_comp = self.config_obj.latency_compensation_ms
        if default_comp == 0.0:
            default_comp = round(self.config_obj.buffer_size / self.config_obj.sample_rate * 1000, 1)
        self.latency_var = tk.StringVar(value=str(default_comp))
        ttk.Entry(self, textvariable=self.latency_var, width=10).grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Separator(self, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1

        ttk.Label(self, text="Stream Deck", font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        ttk.Label(self, text="Stream Deck device").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        current_streamdeck_display = next(
            (display for dev_id, display in self._streamdeck_choices if dev_id == self.config_obj.streamdeck_id),
            "None",
        )
        self.streamdeck_var = tk.StringVar(value=current_streamdeck_display)
        self.streamdeck_box = ttk.Combobox(self, textvariable=self.streamdeck_var, state="readonly", width=34)
        self.streamdeck_box.grid(row=row, column=1, sticky="w")
        row += 1
        ttk.Label(
            self, text="Only connects to a Stream Deck if one is selected here.",
            foreground="#666666",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        self.columnconfigure(1, weight=1)

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, foreground="#2a7d2a").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )
        row += 1

        button_row = ttk.Frame(self)
        button_row.grid(row=row, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.save_button = ttk.Button(button_row, text="Save", command=self._on_save)
        self.save_button.pack(side="right")

        self._refresh_device_choices()

    def _refresh_device_choices(self) -> None:
        """Push the current device lists into all dropdowns' choices."""
        output_names = [d["name"] for d in self.output_devices]
        self.output_device_box.configure(values=output_names, state="readonly" if output_names else "normal")

        streamdeck_display = [display for _dev_id, display in self._streamdeck_choices]
        self.streamdeck_box.configure(values=streamdeck_display)

    # --- reload devices ---

    def _on_reload_devices(self) -> None:
        """Re-query audio devices without discarding unsaved edits.

        Rebuilds the form from scratch on fresh widgets rather than
        reconfiguring the existing comboboxes' values in place — ttk's
        combobox can leave its dropdown listbox stale/blank when `values`
        is changed on a widget that's already been rendered or clicked.
        """
        self._apply_form_to_config()
        self.reload_button.state(["disabled"])
        backend = self.app_state.backend

        def worker() -> None:
            try:
                devices = backend.list_audio_devices()
                streamdecks = backend.list_streamdecks()
                error = None
            except BackendError as e:
                devices, streamdecks, error = [], [], str(e)
            self.after(0, lambda: self._on_reloaded(devices, streamdecks, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_reloaded(
        self, devices: list[dict], streamdecks: list[tuple[str, str]], error: str | None,
    ) -> None:
        if not self.winfo_exists():
            return
        if error:
            messagebox.showerror("Reload failed", error)
            self.reload_button.state(["!disabled"])
            return
        self._set_devices(devices, streamdecks)
        for child in self.winfo_children():
            child.destroy()
        self._build()
        self.status_var.set(f"Devices reloaded ({len(self.devices)} found).")

    def _apply_form_to_config(self) -> None:
        """Copy current (possibly unsaved) form values back into config_obj,
        so a device reload or tab switch doesn't discard in-progress edits."""
        try:
            self.config_obj.sample_rate = int(self.sample_rate_var.get())
        except ValueError:
            pass
        try:
            self.config_obj.buffer_size = int(self.buffer_size_var.get())
        except ValueError:
            pass
        self.config_obj.output_device = self.output_device_var.get().strip()
        try:
            self.config_obj.output_channels = int(self.output_channels_var.get())
        except (ValueError, tk.TclError):
            pass
        try:
            self.config_obj.latency_compensation_ms = float(self.latency_var.get())
        except ValueError:
            pass

        streamdeck_display = self.streamdeck_var.get()
        streamdeck_id, streamdeck_label = next(
            ((dev_id, display) for dev_id, display in self._streamdeck_choices if display == streamdeck_display),
            (self.config_obj.streamdeck_id, self.config_obj.streamdeck_label),
        )
        self.config_obj.streamdeck_id = streamdeck_id
        self.config_obj.streamdeck_label = streamdeck_label

    def _on_save(self) -> None:
        try:
            int(self.sample_rate_var.get())
            int(self.buffer_size_var.get())
            int(self.output_channels_var.get())
            float(self.latency_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror(
                "Invalid configuration",
                "Sample rate, buffer size, output channels, and latency must be numbers.",
            )
            return

        self._apply_form_to_config()

        errors = self.config_obj.validate()
        if errors:
            messagebox.showerror("Invalid configuration", "\n".join(errors))
            return

        backend = self.app_state.backend
        config = self.config_obj
        self.save_button.state(["disabled"])

        def worker() -> None:
            try:
                # video-ui's settings as they are now, not as this tab loaded them.
                from ..config import VIDEO_SETTINGS_FIELDS, copy_fields
                copy_fields(config, backend.get_config(), VIDEO_SETTINGS_FIELDS)
                backend.save_config(config)
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
