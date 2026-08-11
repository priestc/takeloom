"""Standalone "detect-test" window — `takeloom detect-test`.

A read-only diagnostic, deliberately its own top-level window rather than
a tab in the main app: lists every configured instrument (and all its
metadata) and every configured recording device, then listens on all of
them at once — Backend.start_detect_all/stop_detect_all, the same
detect-all mechanism Studio Setup's Instruments table used to expose
inline (see studio_setup.py's module docstring for why it was pulled out
of there) — and lights up an instrument's row the moment its input is
recognized as being played.

Local hardware only, same as start_detect_all itself (RemoteBackend
refuses it — see Backend's docstring) — this always talks to a fresh
LocalBackend, never app_state/a Remote connection.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from ..backend import BackendError, LocalBackend
from .platform_style import normalize as normalize_platform_style

_BG = "#ffffff"
_UNLIT = "#cccccc"
_LIT = "#2a9d3f"


def _fmt_hz(value: float) -> str:
    return f"{value:g}" if value else "default"


class _InstrumentIndicator(tk.Frame):
    """One read-only instrument row: its metadata, plus a light that
    turns on when detect-all reports a match for it and back off when its
    input's own channel light (see _DeviceIndicator) goes quiet — see
    DetectTestWindow._handle_detect_all_status, which is what actually
    decides when to call light_up/light_off (this class just renders
    whatever it's told)."""

    def __init__(self, master: tk.Misc, full_name: str, label: str, input_label: str, musician: str,
                 freq_min_hz: float, freq_max_hz: float) -> None:
        super().__init__(master, bg=_BG, highlightbackground="#dddddd", highlightthickness=1)
        self.full_name = full_name
        self.input_label = input_label

        self.light = tk.Label(self, text="\N{BLACK CIRCLE}", font=("TkDefaultFont", 20), bg=_BG, fg=_UNLIT, width=2)
        self.light.grid(row=0, column=0, rowspan=2, padx=(10, 6), pady=8)

        tk.Label(
            self, text=full_name or "(unnamed)", font=("TkDefaultFont", 13, "bold"), bg=_BG, anchor="w",
        ).grid(row=0, column=1, sticky="w", padx=(0, 12), pady=(8, 0))

        detail = (
            f"label: {label or '—'}    input: {input_label or '—'}    musician: {musician or '—'}    "
            f"range: {_fmt_hz(freq_min_hz)}\N{EN DASH}{_fmt_hz(freq_max_hz)} Hz"
        )
        tk.Label(
            self, text=detail, font=("TkDefaultFont", 10), bg=_BG, fg="#666666", anchor="w",
        ).grid(row=1, column=1, sticky="w", padx=(0, 12), pady=(0, 8))

        self.columnconfigure(1, weight=1)

    def light_up(self) -> None:
        self.light.configure(fg=_LIT)

    def light_off(self) -> None:
        self.light.configure(fg=_UNLIT)


class _DeviceIndicator(tk.Frame):
    """One read-only recording-device row: a green/gray light showing
    whether that input currently has live (non-silent) signal on it —
    see DetectTestWindow._handle_backend_event's "channel" phase — plus
    its label/device/channel. Independent of whether any instrument has
    actually been identified on it; this just answers "is anything
    coming through this input right now"."""

    def __init__(self, master: tk.Misc, label: str, device: str, channel: int) -> None:
        super().__init__(master, bg=_BG)
        self.light = tk.Label(self, text="\N{BLACK CIRCLE}", font=("TkDefaultFont", 12), bg=_BG, fg=_UNLIT, width=2)
        self.light.pack(side="left", padx=(24, 4))
        tk.Label(
            self, text=f"{label}  —  {device}, channel {channel}", bg=_BG, fg="#333333",
        ).pack(side="left")

    def set_active(self, active: bool) -> None:
        self.light.configure(fg=_LIT if active else _UNLIT)


class DetectTestWindow(ttk.Frame):
    def __init__(self, master: tk.Misc, backend: LocalBackend) -> None:
        super().__init__(master)
        self.backend = backend
        self._listening = False
        self._indicators: dict[str, _InstrumentIndicator] = {}  # keyed by instrument full_name
        self._device_indicators: dict[str, _DeviceIndicator] = {}  # keyed by InputLabel.label
        # Which instrument is currently the confirmed answer for a given
        # input_label, if any — tracked here (not on the indicators
        # themselves) so a "channel" phase event going inactive knows
        # exactly which instrument's light, if any, to turn back off; an
        # input_label with no entry has nothing currently lit for it.
        self._lit_instrument_for_input: dict[str, str] = {}

        self.pack(fill="both", expand=True)
        self._build()
        self.backend.on_event(self._on_backend_event)
        self.bind("<Destroy>", self._on_destroy)
        self._start_listening()

    # --- layout ---

    def _build(self) -> None:
        config = self.backend.get_config()

        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(
            self, textvariable=self.status_var, foreground="#2a6db0", wraplength=640, justify="left",
        ).pack(anchor="w", fill="x", padx=12, pady=(12, 8))

        # Scrollable below the (fixed) status line, same reasoning/pattern
        # as studio_setup.StudioSetupFrame._build_scroll_container — an
        # arbitrary number of instruments/devices shouldn't be able to
        # push the window's own controls off-screen.
        canvas = tk.Canvas(self, highlightthickness=0, background=_BG)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        content = tk.Frame(canvas, background=_BG)
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")

        def _on_content_configure(_event: object) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event: object) -> None:
            canvas.itemconfigure(content_window, width=event.width)

        content.bind("<Configure>", _on_content_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(event: object) -> None:
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        def _bind_mousewheel(widget: tk.Misc) -> None:
            widget.bind("<MouseWheel>", _on_mousewheel)
            for child in widget.winfo_children():
                _bind_mousewheel(child)

        ttk.Label(content, text="Installed Instruments", font=("TkDefaultFont", 12, "bold")).pack(
            anchor="w", padx=12, pady=(8, 4)
        )
        if not config.instruments:
            ttk.Label(content, text="No instruments configured.", foreground="#666666").pack(
                anchor="w", padx=12, pady=(0, 12)
            )
        for inst in config.instruments:
            indicator = _InstrumentIndicator(
                content, inst.full_name, inst.label, inst.input_label, inst.musician,
                inst.freq_min_hz, inst.freq_max_hz,
            )
            indicator.pack(fill="x", padx=12, pady=(0, 4))
            self._indicators[inst.full_name] = indicator

        ttk.Separator(content, orient="horizontal").pack(fill="x", padx=12, pady=12)

        ttk.Label(content, text="Recording Devices", font=("TkDefaultFont", 12, "bold")).pack(
            anchor="w", padx=12, pady=(0, 4)
        )
        if not config.input_labels:
            ttk.Label(content, text="No recording devices configured.", foreground="#666666").pack(
                anchor="w", padx=12, pady=(0, 12)
            )
        for il in config.input_labels:
            device_indicator = _DeviceIndicator(content, il.label, il.device, il.channel)
            device_indicator.pack(anchor="w", pady=1)
            self._device_indicators[il.label] = device_indicator

        _bind_mousewheel(canvas)

    # --- listening lifecycle ---

    def _start_listening(self) -> None:
        self.status_var.set("Starting...")

        def worker() -> None:
            try:
                self.backend.start_detect_all()
            except BackendError as e:
                self.after(0, lambda: self._on_start_result(str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_start_result(self, error: str) -> None:
        if not self.winfo_exists():
            return
        self.status_var.set(f"Could not start listening: {error}")
        messagebox.showerror("Cannot start detect-test", error, parent=self)

    def _on_destroy(self, _event: object) -> None:
        self.backend.off_event(self._on_backend_event)
        if self._listening:
            try:
                self.backend.stop_detect_all()
            except BackendError:
                pass

    # --- backend events ---

    def _on_backend_event(self, event: str, data: dict) -> None:
        self.after(0, lambda: self._handle_backend_event(event, data))

    def _handle_backend_event(self, event: str, data: dict) -> None:
        if not self.winfo_exists() or event != "detect_all_status":
            return
        phase = data.get("phase")
        if phase == "started":
            self._listening = True
            self.status_var.set(data.get("status", "Listening."))
        elif phase == "detected":
            name = data.get("instrument", "")
            indicator = self._indicators.get(name)
            if indicator is not None:
                indicator.light_up()
                self._lit_instrument_for_input[indicator.input_label] = name
        elif phase == "channel":
            input_label = data.get("input_label", "")
            active = bool(data.get("active"))
            device_indicator = self._device_indicators.get(input_label)
            if device_indicator is not None:
                device_indicator.set_active(active)
            if not active:
                # The channel just went quiet — whichever instrument was
                # last confirmed on it (if any) isn't playing anymore
                # either, since there's no finer-grained per-instrument
                # "stopped" signal than this.
                lit_name = self._lit_instrument_for_input.pop(input_label, None)
                if lit_name is not None:
                    indicator = self._indicators.get(lit_name)
                    if indicator is not None:
                        indicator.light_off()
        elif phase == "stopped":
            self._listening = False


def run() -> None:
    root = tk.Tk()
    normalize_platform_style(root)
    root.title("Takeloom — Detect Test")

    icon_path = Path(__file__).resolve().parent.parent / "data" / "icon.png"
    if icon_path.exists():
        icon = tk.PhotoImage(file=str(icon_path))
        root.iconphoto(True, icon)
        root._takeloom_icon = icon  # keep a reference or Tk drops the image

    root.geometry("700x500")
    DetectTestWindow(root, LocalBackend())
    root.mainloop()
