"""Studio Setup screen: studio identity, input labels, and instrument
assignment.

Mirrors the fields configured by the `takeloom setup-studio` CLI command,
plus the input-label and instrument sections of `setup-recording-devices`
and `setup-instruments` — grouped here because instruments are just names
attached to input labels, so editing both together avoids bouncing between
tabs. Sample rate/buffer/output device/camera/Stream Deck selection stays
on the Recording Devices tab (see recording_devices.py). Reads/writes
whichever machine's config `app_state.backend` currently points at — local
by default, or a connected remote instance's config in remote mode.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..backend import BackendError
from ..config import INSTRUMENT_LABELS, Instrument, InputLabel, StudioConfig
from .app_state import AppState

FIELDS = [
    ("studio_name", "Studio name"),
    ("studio_location", "Studio location"),
    ("studio_musician", "Studio musician (default performer)"),
    ("inspiration_server", "Inspiration server URL"),
    ("inspiration_api_key", "Inspiration API key"),
]

# Rendered in their own "Vault" section (see _build_vault_section) rather
# than the plain FIELDS loop above, since local_vault gets a folder-picker
# button alongside its Entry and the whole group needs its own heading —
# _on_save still writes both back from self._vars, same as any FIELDS entry.
VAULT_FIELDS = [
    ("session_vault_path", "Local vault"),
    ("backup_server", "Remote vault (user@host:/path)"),
]

_VAULT_MODE_LABELS = [("local", "Local only"), ("remote", "Remote only"), ("both", "Both")]


class _InputRow(ttk.Frame):
    """One editable input-label row: label text, device choice, channel number."""

    def __init__(
        self,
        master: tk.Misc,
        input_devices: list[dict],
        on_remove,
        label: str = "",
        device: str = "",
        channel: int = 1,
    ) -> None:
        super().__init__(master)
        self.input_devices = input_devices
        self._on_remove = on_remove

        self.label_var = tk.StringVar(value=label)
        self.device_var = tk.StringVar(value=device)
        self.channel_var = tk.IntVar(value=channel or 1)

        ttk.Entry(self, textvariable=self.label_var, width=18).grid(row=0, column=0, padx=(0, 6))

        self.device_box = ttk.Combobox(self, textvariable=self.device_var, width=28)
        self.device_box.grid(row=0, column=1, padx=(0, 6))
        self.device_box.bind("<<ComboboxSelected>>", lambda _e: self._update_channel_range())

        self.channel_spin = ttk.Spinbox(self, from_=1, to=64, textvariable=self.channel_var, width=4)
        self.channel_spin.grid(row=0, column=2, padx=(0, 6))

        ttk.Button(self, text="Remove", command=lambda: self._on_remove(self)).grid(row=0, column=3)

        self.set_input_devices(input_devices)

    def set_input_devices(self, input_devices: list[dict]) -> None:
        """Refresh the device dropdown's choices, e.g. after a device reload."""
        self.input_devices = input_devices
        device_names = [d["name"] for d in input_devices]
        self.device_box.configure(values=device_names, state="readonly" if device_names else "normal")
        self._update_channel_range()

    def _update_channel_range(self) -> None:
        dev = next((d for d in self.input_devices if d["name"] == self.device_var.get()), None)
        max_ch = dev["max_input_channels"] if dev else 64
        self.channel_spin.configure(to=max(1, max_ch))
        if max_ch and self.channel_var.get() > max_ch:
            self.channel_var.set(max_ch)

    def to_input_label(self) -> InputLabel | None:
        label = self.label_var.get().strip()
        device = self.device_var.get().strip()
        if not label or not device:
            return None
        return InputLabel(label=label, device=device, channel=self.channel_var.get())


class _InstrumentRow:
    """One editable instrument row, gridded directly into the shared table
    so its columns line up exactly with the header above.

    Train/Stop test drive the realtime pitch calibration in audio/
    instrument_classifier.py (via Backend.start_instrument_train/
    stop_instrument_test) — this class only renders the row and reports
    clicks upward via on_train/on_stop; StudioSetupFrame owns actually
    talking to the backend and routing "instrument_test_status" events
    back to whichever row is currently under test (see its _testing_row).

    There's no per-row Detect button — see StudioSetupFrame's single
    top-of-section "Detect" button and its "detect_all_status" handling,
    which calls this row's set_status("Detected!") directly by matching
    on instrument full name (there's no separate "name" field — full_name
    is the instrument's only identifier, see config.py's Instrument)."""

    def __init__(
        self,
        table: ttk.Frame,
        row: int,
        input_label_names: list[str],
        on_remove,
        on_train,
        on_stop,
        input_label: str = "",
        full_name: str = "",
        label: str = "",
        musician: str = "",
        freq_min_hz: float = 0.0,
        freq_max_hz: float = 0.0,
    ) -> None:
        self._on_remove = on_remove

        self.input_label_var = tk.StringVar(value=input_label or (input_label_names[0] if input_label_names else ""))
        self.full_name_var = tk.StringVar(value=full_name)
        self.label_var = tk.StringVar(value=label or INSTRUMENT_LABELS[0])
        self.musician_var = tk.StringVar(value=musician)
        self.freq_min_var = tk.StringVar(value=(f"{freq_min_hz:g}" if freq_min_hz else ""))
        self.freq_max_var = tk.StringVar(value=(f"{freq_max_hz:g}" if freq_max_hz else ""))
        self.status_var = tk.StringVar(value="")

        self.train_button = ttk.Button(table, text="Train", command=lambda: on_train(self))
        self.stop_button = ttk.Button(table, text="Stop test", command=lambda: on_stop(self))
        self.stop_button.state(["disabled"])

        self.widgets = [
            ttk.Entry(table, textvariable=self.full_name_var, width=20),
            ttk.Combobox(table, textvariable=self.label_var, values=INSTRUMENT_LABELS, state="readonly", width=16),
            ttk.Combobox(table, textvariable=self.input_label_var, values=input_label_names, state="readonly", width=12),
            ttk.Entry(table, textvariable=self.musician_var, width=10),
            ttk.Entry(table, textvariable=self.freq_min_var, width=6),
            ttk.Entry(table, textvariable=self.freq_max_var, width=6),
            self.train_button,
            self.stop_button,
            ttk.Button(table, text="Remove", command=lambda: self._on_remove(self)),
        ]
        self.status_label = ttk.Label(table, textvariable=self.status_var, foreground="#2a6db0", wraplength=220)
        for col, widget in enumerate(self.widgets):
            widget.grid(row=row, column=col, sticky="w", padx=(0, 4), pady=2)
        self.status_label.grid(row=row, column=len(self.widgets), sticky="w", padx=(6, 0), pady=2)

    def set_row(self, row: int) -> None:
        for widget in self.widgets:
            widget.grid(row=row)
        self.status_label.grid(row=row)

    def destroy(self) -> None:
        for widget in self.widgets:
            widget.destroy()
        self.status_label.destroy()

    def to_instrument(self) -> Instrument | None:
        full_name = self.full_name_var.get().strip()
        input_label = self.input_label_var.get().strip()
        if not full_name or not input_label:
            return None
        return Instrument(
            input_label=input_label,
            full_name=full_name,
            label=self.label_var.get().strip(),
            musician=self.musician_var.get().strip(),
            freq_min_hz=self._parse_hz(self.freq_min_var),
            freq_max_hz=self._parse_hz(self.freq_max_var),
        )

    @staticmethod
    def _parse_hz(var: tk.StringVar) -> float:
        text = var.get().strip()
        try:
            return float(text) if text else 0.0
        except ValueError:
            return 0.0

    # --- detect/train test state, driven by StudioSetupFrame ---

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def set_train_enabled(self, enabled: bool) -> None:
        self.train_button.state(["!disabled" if enabled else "disabled"])

    def set_stop_enabled(self, enabled: bool) -> None:
        self.stop_button.state(["!disabled" if enabled else "disabled"])

    def apply_trained_range(self, freq_min_hz: float, freq_max_hz: float) -> None:
        self.freq_min_var.set(f"{freq_min_hz:g}")
        self.freq_max_var.set(f"{freq_max_hz:g}")


class StudioSetupFrame(ttk.Frame):
    """Form for studio identity, input labels, and instrument assignment."""

    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None
        self.input_devices: list[dict] = []
        self.table: ttk.Frame | None = None
        self._vars: dict[str, tk.StringVar] = {}
        self._input_rows: list[_InputRow] = []
        self._instrument_rows: list[_InstrumentRow] = []
        self._testing_row: _InstrumentRow | None = None
        self._detect_all_active = False

        # Subscribed for the tab's whole lifetime (it's non-persistent —
        # rebuilt from scratch on every visit, but __init__ itself only
        # runs once per visit — see app.py's rebuild()) so "instrument_
        # test_status"/"detect_all_status" events reach whichever row
        # started a Train test (or every row, for detect-all), and so a
        # test/detect-all run still going when the tab's torn down gets
        # stopped rather than left holding the audio hardware — see
        # _on_destroy.
        self.app_state.backend.on_event(self._on_backend_event)
        self.bind("<Destroy>", self._on_destroy)

        self._build_scroll_container()
        ttk.Label(self.content, text="Loading...").pack(anchor="w")
        self._load()

    def _build_scroll_container(self) -> None:
        """Studio Setup's content (studio identity fields, vault section,
        input labels, and the instrument table) easily runs taller than
        the window now — wraps it all in a scrollable canvas rather than
        letting it clip. self.content (a plain ttk.Frame) is what every
        _build_*/_on_loaded method below actually grids/packs onto, not
        self directly; self itself just holds the canvas + scrollbar."""
        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.content = ttk.Frame(canvas)
        content_window = canvas.create_window((0, 0), window=self.content, anchor="nw")

        def _on_content_configure(_event: object) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event: object) -> None:
            # Stretches self.content to the canvas's own width so widgets
            # using sticky="ew"/columnconfigure(weight=1) still fill the
            # window horizontally — only the vertical extent scrolls.
            canvas.itemconfigure(content_window, width=event.width)

        self.content.bind("<Configure>", _on_content_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(event: object) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        # Only bound while the pointer is actually over this tab's canvas
        # (bind_all/unbind_all on Enter/Leave), so scrolling here doesn't
        # hijack the mouse wheel anywhere else in the app.
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

    def _on_destroy(self, _event: object) -> None:
        self.app_state.backend.off_event(self._on_backend_event)
        if self._testing_row is not None:
            try:
                self.app_state.backend.stop_instrument_test()
            except BackendError:
                pass
        if self._detect_all_active:
            try:
                self.app_state.backend.stop_detect_all()
            except BackendError:
                pass

    # --- backend events (instrument_test_status / detect_all_status) ---

    def _on_backend_event(self, event: str, data: dict) -> None:
        self.after(0, lambda: self._handle_backend_event(event, data))

    def _handle_backend_event(self, event: str, data: dict) -> None:
        if not self.winfo_exists():
            return
        if event == "instrument_test_status":
            self._handle_instrument_test_status(data)
        elif event == "detect_all_status":
            self._handle_detect_all_status(data)

    def _handle_instrument_test_status(self, data: dict) -> None:
        row = self._testing_row
        if row is None:
            return
        row.set_status(data.get("status", ""))
        phase = data.get("phase")
        if phase == "trained":
            freq_min, freq_max = data.get("freq_min_hz"), data.get("freq_max_hz")
            if freq_min is not None and freq_max is not None:
                row.apply_trained_range(freq_min, freq_max)
            self._finish_instrument_test()
        elif phase == "idle":
            self._finish_instrument_test()
        # "train_high"/"train_low" — test still running, just a
        # status-text update; nothing else to do.

    def _finish_instrument_test(self) -> None:
        if self._testing_row is not None:
            self._testing_row.set_stop_enabled(False)
        self._testing_row = None
        for row in self._instrument_rows:
            row.set_train_enabled(True)
        self.detect_all_button.state(["!disabled"])

    def _handle_detect_all_status(self, data: dict) -> None:
        phase = data.get("phase")
        if phase == "started":
            for row in self._instrument_rows:
                row.set_status("")
            self.detect_all_status_var.set(data.get("status", ""))
        elif phase == "detected":
            name = (data.get("instrument") or "").strip().lower()
            for row in self._instrument_rows:
                if row.full_name_var.get().strip().lower() == name:
                    row.set_status("Detected!")
                    break
        elif phase == "stopped":
            self._finish_detect_all()

    def _finish_detect_all(self) -> None:
        self._detect_all_active = False
        self.detect_all_button.configure(text="Detect")
        self.detect_all_status_var.set("")
        for row in self._instrument_rows:
            row.set_train_enabled(True)

    # --- loading ---

    def _load(self) -> None:
        backend = self.app_state.backend

        def worker() -> None:
            try:
                config = backend.get_config()
                devices = backend.list_audio_devices()
                error = None
            except BackendError as e:
                config, devices, error = None, [], str(e)
            self.after(0, lambda: self._on_loaded(config, devices, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, config: StudioConfig | None, devices: list[dict], error: str | None) -> None:
        if not self.winfo_exists():
            return  # tab was switched away (and rebuilt/destroyed) before this load finished
        for child in self.content.winfo_children():
            child.destroy()
        self._input_rows = []
        self._instrument_rows = []
        if error or config is None:
            ttk.Label(
                self.content, text=error or "Could not load configuration.", foreground="#b00020",
            ).pack(anchor="w")
            return
        self.config_obj = config
        self.input_devices = [d for d in devices if d["max_input_channels"] > 0]
        self._build()

    # --- build ---

    def _build(self) -> None:
        ttk.Label(self.content, text="Studio Setup", font=("TkDefaultFont", 14, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        row = 1
        for attr, label in FIELDS:
            ttk.Label(self.content, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            var = tk.StringVar(value=getattr(self.config_obj, attr))
            show = "*" if attr == "inspiration_api_key" else ""
            entry = ttk.Entry(self.content, textvariable=var, width=42, show=show)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            self._vars[attr] = var
            row += 1

        self.content.columnconfigure(1, weight=1)

        row = self._build_vault_section(row)
        row = self._build_input_labels(row)
        row = self._build_instruments(row)

        self.status_var = tk.StringVar(value="")
        ttk.Label(self.content, textvariable=self.status_var, foreground="#2a7d2a").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )
        row += 1

        button_row = ttk.Frame(self.content)
        button_row.grid(row=row, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.save_button = ttk.Button(button_row, text="Save", command=self._on_save)
        self.save_button.pack(side="right")

    def _build_vault_section(self, row: int) -> int:
        ttk.Separator(self.content, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1

        ttk.Label(self.content, text="Vault", font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        for attr, label in VAULT_FIELDS:
            ttk.Label(self.content, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            var = tk.StringVar(value=getattr(self.config_obj, attr))
            self._vars[attr] = var
            if attr == "session_vault_path":
                path_row = ttk.Frame(self.content)
                path_row.grid(row=row, column=1, sticky="ew")
                path_row.columnconfigure(0, weight=1)
                ttk.Entry(path_row, textvariable=var).grid(row=0, column=0, sticky="ew")
                ttk.Button(path_row, text="Browse...", command=self._on_browse_local_vault).grid(
                    row=0, column=1, padx=(6, 0)
                )
            else:
                ttk.Entry(self.content, textvariable=var, width=42).grid(row=row, column=1, sticky="ew")
            row += 1

        ttk.Label(self.content, text="Vault storage").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.vault_mode_var = tk.StringVar(value=self._current_vault_mode_label())
        ttk.Combobox(
            self.content, textvariable=self.vault_mode_var, values=[label for _v, label in _VAULT_MODE_LABELS],
            state="readonly", width=16,
        ).grid(row=row, column=1, sticky="w", pady=4)
        row += 1
        ttk.Label(
            self.content,
            text="Where recorded sessions (the continuous audio/video, not the setlist itself) are stored. "
                 "\"Remote only\" pushes each session to the remote vault above and removes the local copy "
                 "once that's verified; \"Both\" pushes but keeps the local copy too.",
            foreground="#666666", wraplength=440, justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 10))
        row += 1
        return row

    def _on_browse_local_vault(self) -> None:
        initial = self._vars["session_vault_path"].get().strip()
        chosen = filedialog.askdirectory(
            title="Choose Local Vault Folder", initialdir=initial or str(Path.home()), parent=self,
        )
        if chosen:
            self._vars["session_vault_path"].set(chosen)

    def _build_input_labels(self, row: int) -> int:
        ttk.Separator(self.content, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1

        header = ttk.Frame(self.content)
        header.grid(row=row, column=0, columnspan=2, sticky="ew")
        ttk.Label(header, text="Input Labels", font=("TkDefaultFont", 11, "bold")).pack(side="left")
        ttk.Button(header, text="Reload Devices", command=self._on_reload_devices).pack(side="right")
        row += 1

        if not self.input_devices:
            ttk.Label(
                self.content, text="Could not query audio devices (sounddevice unavailable).",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1

        self.rows_container = ttk.Frame(self.content)
        self.rows_container.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        row += 1

        for il in self.config_obj.input_labels:
            self._add_input_row(label=il.label, device=il.device, channel=il.channel)
        if not self.config_obj.input_labels:
            self._add_input_row()

        ttk.Button(self.content, text="+ Add Input", command=self._add_input_row).grid(
            row=row, column=0, sticky="w", pady=(4, 10)
        )
        row += 1
        return row

    def _add_input_row(self, label: str = "", device: str = "", channel: int = 1) -> None:
        row_widget = _InputRow(
            self.rows_container, self.input_devices, self._remove_input_row,
            label=label, device=device, channel=channel,
        )
        row_widget.pack(fill="x", pady=2)
        self._input_rows.append(row_widget)

    def _remove_input_row(self, row_widget: _InputRow) -> None:
        row_widget.destroy()
        self._input_rows.remove(row_widget)

    def _on_reload_devices(self) -> None:
        backend = self.app_state.backend

        def worker() -> None:
            try:
                devices = backend.list_audio_devices()
                error = None
            except BackendError as e:
                devices, error = [], str(e)
            self.after(0, lambda: self._on_devices_reloaded(devices, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_devices_reloaded(self, devices: list[dict], error: str | None) -> None:
        if not self.winfo_exists():
            return
        if error:
            messagebox.showerror("Reload failed", error)
            return
        self.input_devices = [d for d in devices if d["max_input_channels"] > 0]
        for row_widget in self._input_rows:
            row_widget.set_input_devices(self.input_devices)
        self.status_var.set(f"Devices reloaded ({len(self.input_devices)} inputs found).")

    def _build_instruments(self, row: int) -> int:
        ttk.Separator(self.content, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1

        header = ttk.Frame(self.content)
        header.grid(row=row, column=0, columnspan=2, sticky="ew")
        ttk.Label(header, text="Instruments", font=("TkDefaultFont", 11, "bold")).pack(side="left")
        self.detect_all_button = ttk.Button(header, text="Detect", command=self._on_toggle_detect_all)
        self.detect_all_button.pack(side="right")
        row += 1

        input_label_names = self._current_input_label_names()
        if not input_label_names:
            ttk.Label(
                self.content, text="Add an input label above before assigning instruments.",
                foreground="#666666",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1
            return row

        ttk.Label(
            self.content,
            text="Label is the instrument's type (e.g. \"electric-guitar\") — different instruments "
                 "sharing a label (a Stratocaster and a Telecaster, say) count as satisfying the same "
                 "song's need for a take, so recording one with either won't have the setlist offer "
                 "it again for the other. Min/Max Hz is the frequency range the Record tab's live "
                 "instrument detector compares against — leave blank to fall back to a default for "
                 "the instrument's label. \"Train\" sets it automatically from two notes you play. "
                 "\"Detect\" above listens on every instrument's own input at once and marks each "
                 "row \"Detected!\" as you play it — handy for confirming cabling before a session.",
            foreground="#666666", wraplength=760, justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 6))
        row += 1

        self.detect_all_status_var = tk.StringVar(value="")
        ttk.Label(
            self.content, textvariable=self.detect_all_status_var,
            foreground="#2a6db0", wraplength=760, justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))
        row += 1

        self.table = ttk.Frame(self.content)
        self.table.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        row += 1

        for col, text in enumerate(
            ["Full name", "Label", "Input", "Musician", "Min Hz", "Max Hz", "", "", "", "Status"]
        ):
            ttk.Label(self.table, text=text).grid(row=0, column=col, sticky="w", padx=(0, 6))

        for inst in self.config_obj.instruments:
            self._add_instrument_row(
                input_label_names, input_label=inst.input_label,
                full_name=inst.full_name, label=inst.label, musician=inst.musician,
                freq_min_hz=inst.freq_min_hz, freq_max_hz=inst.freq_max_hz,
            )
        if not self.config_obj.instruments:
            self._add_instrument_row(input_label_names)

        ttk.Button(
            self.content, text="+ Add Instrument",
            command=lambda: self._add_instrument_row(self._current_input_label_names()),
        ).grid(row=row, column=0, sticky="w", pady=(8, 10))
        row += 1
        return row

    def _current_input_label_names(self) -> list[str]:
        return [name for row in self._input_rows if (name := row.label_var.get().strip())]

    def _add_instrument_row(
        self, input_label_names: list[str], input_label: str = "",
        full_name: str = "", label: str = "", musician: str = "",
        freq_min_hz: float = 0.0, freq_max_hz: float = 0.0,
    ) -> None:
        if self.table is None:
            return
        row_index = len(self._instrument_rows) + 1  # row 0 is the header
        row = _InstrumentRow(
            self.table, row_index, input_label_names, self._remove_instrument_row,
            on_train=self._on_train_instrument, on_stop=self._on_stop_instrument_test,
            input_label=input_label, full_name=full_name, label=label, musician=musician,
            freq_min_hz=freq_min_hz, freq_max_hz=freq_max_hz,
        )
        self._instrument_rows.append(row)

    def _remove_instrument_row(self, row: _InstrumentRow) -> None:
        row.destroy()
        self._instrument_rows.remove(row)
        for idx, remaining in enumerate(self._instrument_rows, start=1):
            remaining.set_row(idx)

    # --- save ---

    def _current_vault_mode_label(self) -> str:
        for value, label in _VAULT_MODE_LABELS:
            if value == self.config_obj.session_vault_mode:
                return label
        return _VAULT_MODE_LABELS[0][1]  # "Local only" — the safe default for an unrecognized config value

    def _on_save(self, on_success=None) -> None:
        """`on_success`, if given, runs after a successful save — used by
        Detect/Train (see _on_detect_instrument/_on_train_instrument) to
        make sure the instrument the backend is about to test against
        (looked up from the *persisted* config, not this form's in-memory
        state) actually reflects whatever's currently in the row, rather
        than testing stale settings from the last save."""
        for attr, _label in FIELDS:
            setattr(self.config_obj, attr, self._vars[attr].get())
        for attr, _label in VAULT_FIELDS:
            setattr(self.config_obj, attr, self._vars[attr].get())
        for value, label in _VAULT_MODE_LABELS:
            if label == self.vault_mode_var.get():
                self.config_obj.session_vault_mode = value
                break
        self.config_obj.input_labels = [il for row in self._input_rows if (il := row.to_input_label())]
        self.config_obj.instruments = [inst for row in self._instrument_rows if (inst := row.to_instrument())]

        errors = self.config_obj.validate()
        if errors:
            messagebox.showerror("Invalid configuration", "\n".join(errors))
            return

        backend = self.app_state.backend
        config = self.config_obj
        self.save_button.state(["disabled"])

        def worker() -> None:
            try:
                backend.save_config(config)
                error = None
            except BackendError as e:
                error = str(e)
            self.after(0, lambda: self._on_saved(error, on_success))

        threading.Thread(target=worker, daemon=True).start()

    def _on_saved(self, error: str | None, on_success=None) -> None:
        if not self.winfo_exists():
            return
        self.save_button.state(["!disabled"])
        if error:
            messagebox.showerror("Save failed", error)
            return
        target = f"remote ({self.app_state.remote_name})" if self.app_state.backend.is_remote() else "local config"
        self.status_var.set(f"Saved to {target}")
        if on_success:
            on_success()

    # --- instrument train ---

    def _on_train_instrument(self, row: _InstrumentRow) -> None:
        if self._testing_row is not None or self._detect_all_active:
            return  # a test is already running (buttons are disabled, but guard anyway)
        inst = row.to_instrument()
        if inst is None:
            messagebox.showerror("Cannot test", "Enter a full name and input for this instrument first.")
            return
        self._on_save(on_success=lambda: self._begin_instrument_test(row, inst.full_name))

    def _begin_instrument_test(self, row: _InstrumentRow, instrument_name: str) -> None:
        self._testing_row = row
        for r in self._instrument_rows:
            r.set_train_enabled(False)
        self.detect_all_button.state(["disabled"])
        row.set_stop_enabled(True)
        row.set_status("Starting...")
        backend = self.app_state.backend

        def worker() -> None:
            try:
                backend.start_instrument_train(instrument_name)
                error = None
            except BackendError as e:
                error = str(e)
            self.after(0, lambda: self._on_instrument_test_start_result(error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_instrument_test_start_result(self, error: str | None) -> None:
        if not self.winfo_exists():
            return
        if error:
            messagebox.showerror("Cannot start test", error)
            self._finish_instrument_test()

    def _on_stop_instrument_test(self, _row: _InstrumentRow) -> None:
        # Fire-and-forget: the resulting "instrument_test_status" (phase
        # "idle") event arrives through the normal subscription and is
        # what actually clears _testing_row / re-enables the other rows —
        # see _handle_instrument_test_status.
        backend = self.app_state.backend
        threading.Thread(target=backend.stop_instrument_test, daemon=True).start()

    # --- detect-all ---

    def _on_toggle_detect_all(self) -> None:
        if self._detect_all_active:
            # Fire-and-forget: the resulting "detect_all_status" (phase
            # "stopped") event arrives through the normal subscription and
            # is what actually clears _detect_all_active / re-enables the
            # rows — see _handle_detect_all_status/_finish_detect_all.
            backend = self.app_state.backend
            threading.Thread(target=backend.stop_detect_all, daemon=True).start()
            return
        if self._testing_row is not None:
            return  # a per-row Train test is running (button is disabled, but guard anyway)
        self._on_save(on_success=self._begin_detect_all)

    def _begin_detect_all(self) -> None:
        self._detect_all_active = True
        self.detect_all_button.configure(text="Stop Detecting")
        for row in self._instrument_rows:
            row.set_train_enabled(False)
            row.set_status("")
        self.detect_all_status_var.set("Starting...")
        backend = self.app_state.backend

        def worker() -> None:
            try:
                backend.start_detect_all()
                error = None
            except BackendError as e:
                error = str(e)
            self.after(0, lambda: self._on_detect_all_start_result(error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_detect_all_start_result(self, error: str | None) -> None:
        if not self.winfo_exists():
            return
        if error:
            messagebox.showerror("Cannot start detect", error)
            self._finish_detect_all()
