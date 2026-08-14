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

The Instruments table's Detect-all and per-row Train buttons, and the
Min/Max Hz fields they drove (Backend.start_detect_all/start_instrument_
train/stop_instrument_test), are deliberately pulled out of this tab for
now — not removed from the backend, just not wired up here. Train is
coming back later; until then Min/Max Hz just round-trip whatever's
already in config unchanged (see _InstrumentRow), with no UI to view or
edit them.
"""

from __future__ import annotations

import threading
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..audio.filters import COMPRESSOR_PRESETS, CompressorSettings
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

    Min/Max Hz aren't rendered here — there's no UI to set them right now
    (see the module docstring's note on Detect/Train being pulled out),
    but freq_min_var/freq_max_var still round-trip whatever's already in
    config unchanged (set from the loaded Instrument, read back by
    to_instrument()) rather than silently dropping it on save."""

    def __init__(
        self,
        table: ttk.Frame,
        row: int,
        input_label_names: list[str],
        on_remove,
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

        self.widgets = [
            ttk.Entry(table, textvariable=self.full_name_var, width=20),
            ttk.Combobox(table, textvariable=self.label_var, values=INSTRUMENT_LABELS, state="readonly", width=16),
            ttk.Combobox(table, textvariable=self.input_label_var, values=input_label_names, state="readonly", width=12),
            ttk.Entry(table, textvariable=self.musician_var, width=10),
            ttk.Button(table, text="Remove", command=lambda: self._on_remove(self)),
        ]
        for col, widget in enumerate(self.widgets):
            widget.grid(row=row, column=col, sticky="w", padx=(0, 4), pady=2)

    def set_row(self, row: int) -> None:
        for widget in self.widgets:
            widget.grid(row=row)

    def destroy(self) -> None:
        for widget in self.widgets:
            widget.destroy()

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


class _CompressorRow:
    """One instrument label's compressor settings row, in the Compressor
    table — always one row per INSTRUMENT_LABELS entry, all 7, regardless
    of how many (if any) currently-configured instruments actually use
    that label. Keyed by label (not by which specific Instrument/
    full_name), matching how takes are actually filed and played back —
    see config.py's StudioConfig.compressor_for_label and backend.py's
    play_take: two instruments sharing a label share these same settings.
    A recorded take file itself is always raw on disk no matter what's
    set here — this only ever shapes what's actually heard, live or on
    playback (see AudioEngine._callback/Mixer.add_source/backend.py's
    play_take).

    attack_ms/release_ms aren't exposed as their own controls (yet) —
    carried through unchanged from whatever's already in config, same as
    the Record-tab version this replaced, so picking a preset (which does
    set them) doesn't get silently overwritten by fields with no control
    of their own."""

    def __init__(self, table: ttk.Frame, row: int, label: str, settings: CompressorSettings) -> None:
        self.label = label
        self._attack_ms = settings.attack_ms
        self._release_ms = settings.release_ms

        self.enabled_var = tk.BooleanVar(value=settings.enabled)
        self.preset_var = tk.StringVar(value="")
        self.threshold_var = tk.DoubleVar(value=settings.threshold_db)
        self.ratio_var = tk.DoubleVar(value=settings.ratio)
        self.makeup_var = tk.DoubleVar(value=settings.makeup_gain_db)

        ttk.Label(table, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)

        self.enabled_check = ttk.Checkbutton(table, variable=self.enabled_var, command=self._on_enabled_change)
        self.enabled_check.grid(row=row, column=1, padx=(0, 8))

        self.preset_combo = ttk.Combobox(
            table, textvariable=self.preset_var, values=list(COMPRESSOR_PRESETS.keys()),
            state="readonly", width=16,
        )
        self.preset_combo.grid(row=row, column=2, padx=(0, 8))
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)

        self.threshold_spin = ttk.Spinbox(
            table, from_=-60, to=0, increment=1, width=6, textvariable=self.threshold_var,
        )
        self.threshold_spin.grid(row=row, column=3, padx=(0, 6))

        self.ratio_spin = ttk.Spinbox(table, from_=1, to=20, increment=0.5, width=6, textvariable=self.ratio_var)
        self.ratio_spin.grid(row=row, column=4, padx=(0, 6))

        self.makeup_spin = ttk.Spinbox(table, from_=0, to=24, increment=0.5, width=6, textvariable=self.makeup_var)
        self.makeup_spin.grid(row=row, column=5, padx=(0, 6))

        self._set_controls_enabled(settings.enabled)

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.threshold_spin.configure(state=state)
        self.ratio_spin.configure(state=state)
        self.makeup_spin.configure(state=state)

    def _on_enabled_change(self) -> None:
        self._set_controls_enabled(self.enabled_var.get())

    def _on_preset_selected(self, _event: object = None) -> None:
        preset = COMPRESSOR_PRESETS.get(self.preset_var.get())
        if preset is None:
            return
        # Picking a preset both fills in the numbers and turns the
        # compressor on — selecting one only to have it silently stay
        # disabled would be a confusing dead click.
        self.enabled_var.set(preset.enabled)
        self.threshold_var.set(preset.threshold_db)
        self.ratio_var.set(preset.ratio)
        self.makeup_var.set(preset.makeup_gain_db)
        self._attack_ms = preset.attack_ms
        self._release_ms = preset.release_ms
        self._set_controls_enabled(preset.enabled)

    def to_settings(self) -> CompressorSettings:
        try:
            threshold_db = self.threshold_var.get()
            ratio = self.ratio_var.get()
            makeup_gain_db = self.makeup_var.get()
        except tk.TclError:
            # A spinbox is mid-edit with invalid/empty text — fall back to
            # whatever was last valid rather than raising out of Save.
            threshold_db, ratio, makeup_gain_db = -24.0, 4.0, 0.0
        return CompressorSettings(
            enabled=self.enabled_var.get(), threshold_db=threshold_db, ratio=ratio,
            attack_ms=self._attack_ms, release_ms=self._release_ms, makeup_gain_db=makeup_gain_db,
        )


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
        self._compressor_rows: list[_CompressorRow] = []

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
        self directly; self itself just holds the footer (built first, so
        it claims its space at the bottom before the canvas expands into
        whatever's left) and the canvas + scrollbar."""
        self._build_footer()

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

        self._canvas = canvas
        self._bind_mousewheel(canvas)

    def _on_mousewheel(self, event: object) -> None:
        # event.delta isn't pre-scaled to multiples of 120 on macOS the
        # way it is on Windows — it's a small raw value (often -1..-3/
        # 1..3 per notch, sometimes a couple dozen for a fast trackpad
        # swipe), so the once-conventional "divide by 120" rounds every
        # ordinary scroll down to 0 units, i.e. no visible movement at
        # all. Just move a fixed one unit per event instead, signed by
        # delta's direction.
        self._canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _bind_mousewheel(self, widget: tk.Misc) -> None:
        """Bind the scroll handler directly on `widget` and, recursively,
        every one of its descendants, rather than a single bind_all
        ("<MouseWheel>", ...) on the Tk-global "all" bindtag — belt and
        suspenders against however a given machine's Tk build happens to
        route wheel events, confirmed (on at least one real machine,
        Tcl/Tk 9.0.4 on Aqua via Homebrew) to sometimes not deliver
        <MouseWheel> to *any* Tk widget at all, bind_all included — down
        to a completely bare `tk.Tk()` with nothing else running, and
        even to Tcl/Tk's own `wish` shell with zero Python involved. That
        turned out to be an environment-level gap (this exact Tcl/Tk
        build not receiving the OS scroll-wheel event at all, on that
        machine, for any Tk app), not a binding-strategy bug — no
        widget-level fix here can compensate for the OS never delivering
        the event in the first place. The scrollbar itself still works
        by dragging regardless, since that's ordinary mouse-button
        press/drag, not a wheel event.

        Plain widget.bind() (no add="+") deliberately overwrites rather
        than stacks, so re-running this over an already-bound subtree
        (see the call at the end of _on_loaded, after _build() has
        populated self.content, and again from _add_input_row/_add_
        instrument_row whenever a row's added later) is safe/idempotent
        instead of accumulating duplicate handlers that would fire
        multiple times per actual scroll."""
        widget.bind("<MouseWheel>", self._on_mousewheel)
        for child in widget.winfo_children():
            self._bind_mousewheel(child)

    def _build_footer(self) -> None:
        """Save (and its status message) live outside the scrollable
        canvas, in their own frame pinned to the bottom of the tab —
        always visible regardless of where the content above is
        scrolled to. save_button starts disabled since there's nothing
        to save until _on_loaded actually finishes (see its own
        enabling at the end of _build())."""
        footer = ttk.Frame(self)
        footer.pack(side="bottom", fill="x")
        ttk.Separator(footer, orient="horizontal").pack(fill="x", pady=(0, 8))
        bottom_row = ttk.Frame(footer)
        bottom_row.pack(fill="x", pady=(0, 8))
        self.status_var = tk.StringVar(value="")
        ttk.Label(bottom_row, textvariable=self.status_var, foreground="#2a7d2a").pack(side="left")
        self.save_button = ttk.Button(bottom_row, text="Save", command=self._on_save)
        self.save_button.pack(side="right")
        self.save_button.state(["disabled"])

    def _on_destroy(self, _event: object) -> None:
        self.unbind_all("<MouseWheel>")

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
        self._compressor_rows = []
        if error or config is None:
            ttk.Label(
                self.content, text=error or "Could not load configuration.", foreground="#b00020",
            ).pack(anchor="w")
            return
        self.config_obj = config
        self.input_devices = [d for d in devices if d["max_input_channels"] > 0]
        self._build()
        self._bind_mousewheel(self._canvas)

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
        row = self._build_compressor_section(row)

        # status_var/save_button themselves live in the fixed footer (see
        # _build_footer) — just enabling it here, now that there's an
        # actually-loaded config to save.
        self.save_button.state(["!disabled"])

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
        # Full re-scan rather than binding row_widget alone — simpler and
        # consistent with _add_instrument_row, where the equivalent isn't
        # a widget of its own to bind (see _InstrumentRow) — and cheap
        # enough given how small this tab's whole tree is.
        self._bind_mousewheel(self._canvas)

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
                 "it again for the other.",
            foreground="#666666", wraplength=760, justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 6))
        row += 1

        self.table = ttk.Frame(self.content)
        self.table.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        row += 1

        for col, text in enumerate(["Full name", "Label", "Input", "Musician", ""]):
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

    def _build_compressor_section(self, row: int) -> int:
        ttk.Separator(self.content, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1

        ttk.Label(self.content, text="Compressor", font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        ttk.Label(
            self.content,
            text="Per instrument label, not per specific instrument — a Stratocaster and a Telecaster "
                 "both sharing \"electric-guitar\" share one set of settings here too, the same way they "
                 "share one take-number sequence. Recorded takes are always stored raw; this only shapes "
                 "what's actually heard, live while recording or on playback (Sessions/Completed Takes' "
                 "Play button). \"Benchmark modifiers\" times whatever's currently saved (click Save "
                 "first if you've just changed something below) against this studio's real audio block "
                 "size, on whichever machine would actually run it.",
            foreground="#666666", wraplength=760, justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 6))
        row += 1

        table = ttk.Frame(self.content)
        table.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        row += 1

        for col, text in enumerate(["Label", "On", "Preset", "Threshold (dB)", "Ratio", "Makeup (dB)"]):
            ttk.Label(table, text=text).grid(row=0, column=col, sticky="w", padx=(0, 6))

        for i, label in enumerate(INSTRUMENT_LABELS, start=1):
            settings = self.config_obj.compressor_for_label(label)
            self._compressor_rows.append(_CompressorRow(table, i, label, settings))

        bench_row = ttk.Frame(self.content)
        bench_row.grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 10))
        row += 1
        self.benchmark_button = ttk.Button(
            bench_row, text="Benchmark modifiers", command=self._on_benchmark_modifiers,
        )
        self.benchmark_button.pack(side="left")
        self.benchmark_var = tk.StringVar(value="")
        ttk.Label(bench_row, textvariable=self.benchmark_var, foreground="#666666").pack(
            side="left", padx=(8, 0)
        )

        return row

    def _on_benchmark_modifiers(self) -> None:
        self.benchmark_button.state(["disabled"])
        self.benchmark_var.set("Benchmarking...")
        backend = self.app_state.backend

        def worker() -> None:
            try:
                result = backend.benchmark_audio_modifiers()
                error = None
            except BackendError as e:
                result, error = None, str(e)
            self.after(0, lambda: self._on_benchmark_result(result, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_benchmark_result(self, result: dict | None, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self.benchmark_button.state(["!disabled"])
        if error or result is None:
            self.benchmark_var.set("")
            messagebox.showerror("Benchmark failed", error or "No result returned.")
            return
        verdict = "within budget" if result["within_budget"] else "OVER BUDGET"
        self.benchmark_var.set(
            f"All {result['labels_measured']} labels: {result['total_ms']:.3f} ms "
            f"(block budget {result['budget_ms']:.3f} ms) — {verdict}"
        )

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
            input_label=input_label, full_name=full_name, label=label, musician=musician,
            freq_min_hz=freq_min_hz, freq_max_hz=freq_max_hz,
        )
        self._instrument_rows.append(row)
        self._bind_mousewheel(self._canvas)

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

    def _on_save(self) -> None:
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
        self.config_obj.compressor_settings = {row.label: row.to_settings() for row in self._compressor_rows}

        errors = self.config_obj.validate()
        if errors:
            messagebox.showerror("Invalid configuration", "\n".join(errors))
            return

        backend = self.app_state.backend
        config = self.config_obj
        compressor_settings = dict(config.compressor_settings)
        self.save_button.state(["disabled"])

        def worker() -> None:
            try:
                backend.save_config(config)
                # save_config alone persists everything, including this —
                # these extra calls are purely so a compressor tweak takes
                # effect immediately on a currently-running engine (see
                # Backend.set_compressor_settings), the one field here
                # that can matter to something already live in the
                # background (the Record tab's persistent engine) while
                # this tab is open.
                for label, settings in compressor_settings.items():
                    backend.set_compressor_settings(label, asdict(settings))
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
