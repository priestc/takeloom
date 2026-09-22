"""Record page: pick an instrument + project, choose a track from the
project's setlist, preview the camera, and record a take.

Everything here goes through `app_state.backend` — in local mode that's a
`LocalBackend` talking directly to this machine's hardware; in remote mode
it's a `RemoteBackend` talking over the network to another takeloom instance.
This frame itself never touches `sounddevice`/`cv2`/`ffmpeg` directly: all
device/take/camera-preview work happens inside the backend, and this frame
just reflects whatever state it reports back (via `recording_status`/
`preview_paused`/`preview_resumed` events, and streamed preview frames).
"""

from __future__ import annotations

import io
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from ..audio.pitch import TunerSmoother
from ..audio.synth import DEFAULT_SYNTH_VOICE, SYNTH_VOICES
from ..backend import BackendError, StartRecordingRequest
from ..config import StudioConfig
from ..inspiration import average_duration, derive_filter_label
from ..project import Setlist, TrackEntry
from ..recording_driver import RecordingDeckDriver
from ..utils import format_duration
from .add_to_setlist_dialog import AddToSetlistDialog
from .app_state import AppState
from .filter_slot_dialogs import EditFilterDialog, ShowTracksDialog
from .instrument_colors import color_for_label
from .level_meter import LevelMeter
from .tuner_meter import TunerMeter
from .new_project_dialog import NewProjectDialog
from .setlist_row import SetlistRow
from .streamdeck_emulator import StreamDeckEmulator
from .video_check_dialog import VideoCheckDialog

# Ring thickness (px) a connected-instrument box gets once auto-detect
# picks it out — see RecordFrame._mark_device_identified.
_BOX_HIGHLIGHT_THICKNESS = 3


class RecordFrame(ttk.Frame):
    """Instrument/project/track picker, camera preview, and recording."""

    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None
        self._project_names: list[str] = []
        self._project_name: str | None = None
        self._setlist: Setlist | None = None
        # One SetlistRow widget per current track, rebuilt from scratch by
        # _refresh_setlist on every change — see that method's docstring
        # comment. Parallel array, same order as self._setlist.tracks
        # (kept in sync by every mutator: _on_row_motion/_on_delete_track).
        self._setlist_rows: list[SetlistRow] = []
        # Per-track-index preview of what each inspiration filter slot
        # would draw right now for each installed instrument label (None
        # for an ordinary, non-filter track) — see backend.py's
        # get_filter_slot_previews. Fetched once when a project is opened
        # (_on_setlist_loaded) and deliberately left stale afterward so
        # the "next up" picks shown stay put for the rest of that visit
        # rather than re-randomizing every setlist redisplay; only
        # refetched when the track count changes (_apply_refreshed_setlist)
        # or a filter slot's own criteria is edited (_on_edit_filter).
        self._filter_previews: list[dict | None] = []
        # Whether the setlist can currently be clicked/reordered/right-
        # clicked at all — mirrors the old tk.Listbox's own "disabled"
        # state (see _set_controls_enabled), which used to block native
        # selection changes for free; SetlistRow has no such built-in
        # concept, so this is checked by hand in _on_row_press.
        self._setlist_interactive = True

        self._selected_track: TrackEntry | None = None
        self._selected_track_index: int | None = None
        # Index the current click-and-drag started at, and whether it's
        # actually moved a track (vs. just being a plain click) — see
        # _on_row_press/_on_row_motion/_on_row_release.
        self._drag_index: int | None = None
        self._drag_moved = False

        # "idle" (no session) | "waiting" (session open, track cued or
        # between songs) | "recording" (backing playing). Non-idle means a
        # session — the one continuous recording — is running, which locks
        # the project/track controls for its whole duration.
        self._phase = "idle"
        self._video_check_phase = "idle"  # "idle" | "recording"
        # Name of whichever instrument backend.py's start_auto_detect_
        # instrument locked onto — "" until it has (see _begin_identify/
        # _handle_auto_detect_status). This is what a session actually
        # starts with; there's no manual instrument picker anymore.
        self._detected_instrument = ""
        # Whether that instrument is MIDI-driven (config.Instrument.
        # is_midi) — set alongside _detected_instrument by _handle_auto_
        # detect_status's "detected" branch, and what controls whether
        # the "Sound" picker (self.synth_voice_row, built in _build_left)
        # is shown at all; meaningless while this is "" or False.
        self._detected_is_midi = False
        # Sub-state of an "idle" _phase — see the "instrument identify
        # cycle" section below for the full "idle" -> "identifying" ->
        # "ready" -> (Play opens a session) flow.
        self._identify_state = "idle"
        # Which idle button ("Start Local"=False, "Start Streaming"=True)
        # kicked off the current _identify_state != "idle" cycle — read
        # back by _confirm_start() once Play is pressed.
        self._pending_streaming = False
        # Tk equivalent of RecordingDeckDriver.tuner_note/tuner_cents/
        # _tuner_smoother — see the tuner_meter widget built in
        # _build_left and _handle_tuner_status below. Only meaningful
        # (and only ever set) while self._identify_state != "idle".
        self._tuner_note: str | None = None
        self._tuner_cents = 0.0
        self._tuner_smoother = TunerSmoother()
        self._monitoring_mode = "production"  # "production" | "recording" — refreshed in _on_loaded
        self._preview_sub = None
        self._preview_imgtk = None
        self._preview_width = 0  # current width available for the preview label, tracked via <Configure>
        # (kind, device_name) -> {"frame", "instrument_var", "instrument_label"}
        # for the "Connected Instruments" panel at the top of the left
        # column — see _build_connected_devices_panel/_refresh_connected_
        # devices. kind is "midi" or "audio"; device_name is the raw MIDI
        # port name for a MIDI box, or the configured InputLabel's own
        # label for an audio one. Populated the moment the Record page
        # loads, independent of Start/auto-detect — see that method's
        # own docstring for why.
        self._device_boxes: dict[tuple[str, str], dict] = {}

        self._current_backend = None
        self._streamdeck_driver = RecordingDeckDriver(
            self.app_state.backend,
            resolve_start_request=self._build_selected_request,
            on_video_check_result=self._on_streamdeck_video_check_result,
            log=self._log_streamdeck,
        )
        self.app_state.add_listener(self._on_app_state_changed)
        self.bind("<Destroy>", self._on_destroy)

        ttk.Label(self, text="Loading...").pack(anchor="w")
        self._attach_backend()

        threading.Thread(target=self._connect_streamdeck, daemon=True).start()

        self.after(50, self._poll_levels)
        self.after(3000, self._poll_connected_devices)

    # --- StreamDeck (shared RecordingDeckDriver — see recording_driver.py) ---

    def _connect_streamdeck(self) -> None:
        if not self._streamdeck_driver.connect(key_callback=self._on_streamdeck_key):
            if self._streamdeck_driver.streamdeck.last_error:
                print(
                    f"StreamDeck: found a device but could not connect — "
                    f"{self._streamdeck_driver.streamdeck.last_error}"
                )

    def _on_streamdeck_key(self, key: str) -> None:
        # Marshal onto the Tk thread before the driver runs — resolve_start_
        # request() reads Tk StringVars and on_video_check_result() may open
        # a Toplevel, neither of which is safe off the Tk thread.
        self.after(0, lambda: self._streamdeck_driver.handle_key(key))

    def _on_streamdeck_video_check_result(self, path: Path, has_video: bool) -> None:
        self.after(0, lambda: VideoCheckDialog(self, path, has_video) if self.winfo_exists() else None)

    def _log_streamdeck(self, message: str) -> None:
        def apply() -> None:
            if self.winfo_exists() and hasattr(self, "status_var"):
                self.status_var.set(message)
        self.after(0, apply)

    # --- async backend calls (thread hop + marshal back onto the Tk thread) ---

    def _run_backend(self, fn, on_done=None) -> None:
        """Run `fn()` on a worker thread and, once it finishes, call
        `on_done(result, error)` back on the Tk thread (`error` is a message
        string if `fn` raised BackendError, else None and `result` holds
        whatever `fn` returned). Pass no `on_done` for fire-and-forget calls."""
        def worker() -> None:
            try:
                result, error = fn(), None
            except BackendError as e:
                result, error = None, str(e)
            if on_done is not None:
                self.after(0, lambda: on_done(result, error))

        threading.Thread(target=worker, daemon=True).start()

    # --- backend attach / (re)load ---

    def _attach_backend(self) -> None:
        if self._current_backend is not None:
            self._current_backend.off_event(self._on_backend_event)
            if not self._detected_instrument:
                try:
                    self._current_backend.stop_auto_detect_instrument()
                except BackendError:
                    pass
        self._stop_preview()
        self._detected_instrument = ""
        self._detected_is_midi = False
        self._identify_state = "idle"
        self._pending_streaming = False
        self._tuner_note = None
        if hasattr(self, "detected_instrument_var"):
            self.detected_instrument_var.set("")
            self.auto_detect_status_var.set("")
        self._current_backend = self.app_state.backend
        self._current_backend.on_event(self._on_backend_event)
        self._streamdeck_driver.rebind_backend(self._current_backend)
        self._clear_selection()
        self._load()

    def _on_app_state_changed(self) -> None:
        if self.app_state.backend is not self._current_backend:
            self._attach_backend()

    def _load(self) -> None:
        backend = self.app_state.backend

        def on_done(result: tuple | None, error: str | None) -> None:
            config, projects, monitoring_mode = result if result is not None else (None, [], "production")
            self._on_loaded(config, projects, monitoring_mode, error)

        self._run_backend(
            lambda: (backend.get_config(), backend.list_projects(), backend.get_monitoring_mode()), on_done
        )

    def _on_loaded(
        self, config: StudioConfig | None, projects: list[str], monitoring_mode: str, error: str | None,
    ) -> None:
        if not self.winfo_exists():
            return  # app closed (or frame torn down) before this load finished
        for child in self.winfo_children():
            child.destroy()
        if error or config is None:
            ttk.Label(self, text=error or "Could not load configuration.", foreground="#b00020").pack(anchor="w")
            return
        self.config_obj = config
        self._project_names = projects
        self._monitoring_mode = monitoring_mode

        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="new", padx=(0, 10))
        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self.columnconfigure(0, weight=1, uniform="record_halves")
        self.columnconfigure(1, weight=1, uniform="record_halves")
        self.rowconfigure(0, weight=1)

        self._build_left(left)
        self._build_right(right)
        self._build_loading_overlay()

        self._start_preview()
        self._on_project_change()

    # --- loading overlay (shown while a project's setlist + inspiration-
    # filter previews are being fetched — see _on_project_change/_on_setlist_
    # loaded/_apply_filter_previews) ---

    def _build_loading_overlay(self) -> None:
        """Built once, on top of both columns via place() (independent of
        their own grid layout) — shown/hidden by (re)calling place()/
        place_forget() rather than being torn down and rebuilt, since it
        needs to appear the instant a project switch starts, before
        there's anything new to show yet."""
        overlay = tk.Frame(self, background="#f5f5f5")
        center = tk.Frame(overlay, background="#f5f5f5")
        center.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(
            center, text="Loading project...", font=("TkDefaultFont", 16, "bold"), background="#f5f5f5",
        ).pack()
        self.loading_status_var = tk.StringVar(value="")
        tk.Label(
            center, textvariable=self.loading_status_var, foreground="#666666", background="#f5f5f5",
        ).pack(pady=(6, 0))
        self._loading_overlay = overlay

    def _show_loading_overlay(self, status: str = "") -> None:
        if not hasattr(self, "_loading_overlay"):
            return  # frame hasn't finished its own initial build yet
        self.loading_status_var.set(status)
        self._loading_overlay.place(x=0, y=0, relwidth=1, relheight=1)
        self._loading_overlay.lift()

    def _set_loading_status(self, status: str) -> None:
        if hasattr(self, "loading_status_var"):
            self.loading_status_var.set(status)

    def _hide_loading_overlay(self) -> None:
        if hasattr(self, "_loading_overlay"):
            self._loading_overlay.place_forget()

    # --- left column: instrument/project/preview/controls ---

    def _build_left(self, left: ttk.Frame) -> None:
        row = 0
        ttk.Label(left, text="Record", font=("TkDefaultFont", 14, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        row += 1

        row = self._build_connected_devices_panel(left, row)

        # No more manual Instrument dropdown — pressing Start Local/Start
        # Streaming (on the on-screen Stream Deck emulator below) kicks off
        # backend.py's start_auto_detect_instrument(), which listens across
        # every configured instrument's own channel at once and locks onto
        # whichever one the classifier commits to; that becomes "the
        # instrument" the session Play then opens uses, same role the
        # dropdown used to play. This label doubles as that flow's live
        # status ("Listening...") and, once a session is open, the same
        # "here's what we think you're playing" realtime display it always
        # was (see "instrument_detected" handling below) — the two don't
        # conflict since auto-detect only ever runs before a session
        # starts. Redetect here is a convenience duplicate of the deck's
        # own Re-identify tile (_redo_identify) — same action, two entry
        # points. See _handle_auto_detect_status/_begin_identify.
        detect_row = ttk.Frame(left)
        detect_row.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 2))
        self.detected_instrument_var = tk.StringVar(value="")
        ttk.Label(
            detect_row, textvariable=self.detected_instrument_var,
            font=("TkDefaultFont", 28, "bold"), foreground="#2a6db0",
        ).pack(side="left")
        self.redetect_button = ttk.Button(detect_row, text="Redetect", command=self._redo_identify)
        self.redetect_button.pack(side="left", padx=(12, 0))
        self.redetect_button.state(["disabled"])  # enabled once identifying/ready — see _set_controls_enabled
        row += 1

        self.auto_detect_status_var = tk.StringVar(value="")
        ttk.Label(left, textvariable=self.auto_detect_status_var, foreground="#666666", wraplength=360).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        row += 1

        # Only shown once auto-detect commits to a MIDI-driven instrument
        # (self._detected_is_midi — see _handle_auto_detect_status's
        # "detected" branch, which grid()/grid_remove()s this and sets
        # synth_voice_var to whatever's currently playing) and stays up
        # right through an active session, not just the "ready" phase —
        # a MIDI instrument's sound is meant to be changeable live while
        # playing, not just chosen once before Record. Selecting a new
        # value calls backend.set_synth_voice immediately (see _on_synth_
        # voice_changed); there's no separate "apply" step.
        self.synth_voice_row = ttk.Frame(left)
        self.synth_voice_row.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(self.synth_voice_row, text="Sound").pack(side="left", padx=(0, 8))
        self.synth_voice_var = tk.StringVar(value=DEFAULT_SYNTH_VOICE)
        self.synth_voice_combo = ttk.Combobox(
            self.synth_voice_row, textvariable=self.synth_voice_var, values=SYNTH_VOICES,
            state="readonly", width=12,
        )
        self.synth_voice_combo.pack(side="left")
        self.synth_voice_combo.bind("<<ComboboxSelected>>", self._on_synth_voice_changed)
        self.synth_voice_row.grid_remove()
        row += 1

        # Tk equivalent of streamdeck_controller.py's touchscreen tuner
        # needle — only shown while self._identify_state == "identifying"
        # (see _begin_identify/_redo_identify, which grid() it, and
        # _handle_auto_detect_status's "detected" branch, which grid_
        # remove()s it again once a scan locks onto an instrument).
        self.tuner_meter = TunerMeter(left)
        self.tuner_meter.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        self.tuner_meter.grid_remove()
        row += 1

        default_project = self.config_obj.last_selected_project
        if default_project not in self._project_names:
            default_project = self._project_names[0] if self._project_names else ""
        ttk.Label(left, text="Project").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        project_row = ttk.Frame(left)
        project_row.grid(row=row, column=1, sticky="w")
        self.project_var = tk.StringVar(value=default_project)
        self.project_combo = ttk.Combobox(
            project_row, textvariable=self.project_var, values=self._project_names, state="readonly", width=22,
        )
        self.project_combo.pack(side="left")
        self.project_combo.bind("<<ComboboxSelected>>", self._on_project_change)
        ttk.Button(project_row, text="+ New Project", command=self._on_new_project).pack(side="left", padx=(6, 0))
        row += 1

        if not self.config_obj.instruments:
            ttk.Label(
                left, text="No instruments configured. Set them up on the Studio Setup tab first.",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1
        if not self._project_names:
            ttk.Label(
                left, text=f"No projects found in {self.config_obj.projects_dir}.",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1

        left.columnconfigure(1, weight=1)
        self.refresh_devices_button = ttk.Button(
            left, text="↻ Refresh Devices", command=self._on_refresh_devices,
        )
        self.refresh_devices_button.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        self.preview_label = tk.Label(left, background="#1a1a1a", foreground="white", text="No camera preview")
        self.preview_label.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        self.preview_label.bind("<Configure>", self._on_preview_label_resize)
        row += 1

        ttk.Label(left, text="Instrument", foreground="#666666").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1
        self.instrument_meter = LevelMeter(left)
        self.instrument_meter.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        row += 1

        ttk.Label(left, text="Backing Track", foreground="#666666").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1
        self.backing_meter = LevelMeter(left)
        self.backing_meter.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        row += 1

        self.selection_var = tk.StringVar(value="No track selected")
        ttk.Label(left, textvariable=self.selection_var).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        self.status_var = tk.StringVar(value="")
        ttk.Label(left, textvariable=self.status_var, foreground="#2a7d2a", wraplength=360).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        row += 1

        self.streamdeck_emulator = StreamDeckEmulator(left, on_key=self._on_emulator_key)
        self.streamdeck_emulator.grid(row=row, column=0, columnspan=2, pady=(8, 0))
        self.streamdeck_emulator.update_recording_page(self._phase, self._video_check_phase, self._identify_state)
        self.streamdeck_emulator.update_monitoring_mode(self._monitoring_mode)
        row += 1

        self.video_check_button = ttk.Button(
            left, text="Video Check", command=self._on_toggle_video_check,
        )
        self.video_check_button.grid(row=row, column=0, columnspan=2, pady=(4, 0))
        if self.app_state.backend.is_remote():
            self.video_check_button.state(["disabled"])
            self.video_check_button.grid_remove()
        elif not self.config_obj.instruments or not self._project_names:
            self.video_check_button.state(["disabled"])

    # --- connected instruments panel (top of left column) ---
    #
    # Deliberately independent of the Start/auto-detect cycle below: a
    # box for every *configured Instrument* whose own device is
    # currently connected appears the moment the Record page loads,
    # already labeled with that instrument's name — not one box per raw
    # device. That distinction matters in practice: a device with no
    # instrument configured on it at all (e.g. an audio interface's own
    # unused MIDI DIN port) never gets a box, since there's nothing to
    # call it; and two different instruments sharing one physical device
    # (a bass and a guitar both wired through the same audio interface,
    # a home rig's two MIDI keyboards that both happen to still say the
    # same generic port name) each still get their own box, since each
    # is independently "ready to record" the moment that shared device
    # is connected — you just don't know which one is actually being
    # played until you press Start. That's what auto-detect answers:
    # once it commits (_handle_auto_detect_status's "detected" branch),
    # the one matching box is highlighted (a colored ring — see
    # _mark_device_identified/_reset_device_identifications), not
    # relabeled, since every box already shows its own instrument's name
    # from the moment it appeared.

    def _build_connected_devices_panel(self, left: ttk.Frame, row: int) -> int:
        ttk.Label(left, text="Connected Instruments", foreground="#666666").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1
        self.connected_devices_frame = ttk.Frame(left)
        self.connected_devices_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(2, 12))
        row += 1
        self.no_devices_label = ttk.Label(
            self.connected_devices_frame, text="No configured instrument is currently connected.",
            foreground="#999999",
        )
        # Instrument full_name -> {"frame", "label"} (its own
        # config.Instrument.label, cached here so _mark_device_
        # identified doesn't need a config_obj lookup just to know
        # which color ring to draw).
        self._device_boxes: dict[str, dict] = {}
        self._refresh_connected_devices()
        return row

    def _refresh_connected_devices(self) -> None:
        if not hasattr(self, "connected_devices_frame") or not self.connected_devices_frame.winfo_exists():
            return
        backend = self.app_state.backend
        self._run_backend(
            lambda: (backend.list_audio_devices(), backend.list_midi_devices()),
            lambda result, error: self._on_connected_devices_loaded(result, error),
        )

    def _poll_connected_devices(self) -> None:
        """A slower, independent timer from _poll_levels' 50ms VU-meter
        poll — enumerating MIDI ports isn't free the way reading an
        already-open stream's last level is, and doesn't need to be
        anywhere near that responsive; this just needs to notice a
        keyboard being plugged in without the operator having to press
        Refresh Devices. Audio devices are deliberately not re-polled
        here — unlike MIDI, PortAudio snapshots its device list once and
        needs refresh_devices()'s heavier reinit to see a new one (see
        that method's own docstring), which isn't something to do
        silently on a timer."""
        if not self.winfo_exists():
            return
        self._refresh_connected_devices()
        self.after(3000, self._poll_connected_devices)

    def _instrument_is_connected(self, inst, audio_names: set, midi_names: list[str]) -> bool:
        """Whether `inst`'s own device currently shows up in a fresh
        list_audio_devices()/list_midi_devices() call — same fuzzy,
        one-direction substring tolerance MidiInput/resolve_device
        themselves already extend a configured name (a live device name
        sometimes carries a changing numeric suffix a stored name
        wouldn't), not the other way around."""
        if inst.is_midi:
            configured = inst.midi_device.strip().lower()
            return any(configured in name.lower() or name.lower() in configured for name in midi_names)
        input_info = self.config_obj.resolve_input(inst.input_label) if self.config_obj else None
        if input_info is None:
            return False
        return any(input_info.device == name or input_info.device.lower() in name.lower() for name in audio_names)

    def _on_connected_devices_loaded(self, result: tuple | None, error: str | None) -> None:
        if not self.winfo_exists() or not hasattr(self, "connected_devices_frame"):
            return
        if not self.connected_devices_frame.winfo_exists():
            return
        if error or result is None:
            return  # best-effort — leave whatever's already shown rather than clearing it on a hiccup
        audio_devices, midi_devices = result
        audio_names = {d["name"] for d in audio_devices if d.get("max_input_channels", 0) > 0}

        wanted: list[str] = []
        if self.config_obj is not None:
            for inst in self.config_obj.instruments:
                if self._instrument_is_connected(inst, audio_names, midi_devices):
                    wanted.append(inst.full_name)

        for key in list(self._device_boxes):
            if key not in wanted:
                self._device_boxes.pop(key)["frame"].destroy()
        for key in wanted:
            if key not in self._device_boxes:
                self._device_boxes[key] = self._make_device_box(key)

        if wanted:
            self.no_devices_label.pack_forget()
        else:
            self.no_devices_label.pack(side="left")
        # Re-pack every surviving/new box in `wanted` order each time —
        # cheap, and the simplest way to both position newcomers
        # correctly and reflect a device that's since disappeared,
        # without disturbing an already-highlighted box's own state.
        # expand=True + fill="both" on every packed box is what makes
        # them split the row's full width evenly between however many
        # there are, rather than sitting at their own minimal content
        # width with dead space after them.
        for key in wanted:
            self._device_boxes[key]["frame"].pack(side="left", expand=True, fill="both", padx=(0, 8))

    def _make_device_box(self, full_name: str) -> dict:
        inst = self.config_obj.get_instrument(full_name) if self.config_obj else None
        label = inst.label if inst is not None else ""
        frame = tk.Frame(
            self.connected_devices_frame, relief="solid", borderwidth=1,
            highlightthickness=0, highlightbackground=color_for_label(label), padx=8, pady=4,
        )
        if label:
            ttk.Label(frame, text=label, font=("TkDefaultFont", 8), foreground=color_for_label(label)).pack(
                anchor="w"
            )
        ttk.Label(frame, text=full_name, font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        return {"frame": frame, "label": label}

    def _mark_device_identified(self, full_name: str) -> None:
        """Auto-detect just committed to `full_name` — ring its box (it
        already shows that instrument's own name; see the panel's own
        docstring for why nothing here needs relabeling) and make sure
        every other box's ring, if any, is off. A no-op if that
        instrument somehow has no box right now (shouldn't happen —
        auto-detect only ever finds an instrument whose device is
        connected, the same condition a box requires — but harmless
        either way)."""
        for key, box in self._device_boxes.items():
            box["frame"].configure(highlightthickness=_BOX_HIGHLIGHT_THICKNESS if key == full_name else 0)

    def _reset_device_identifications(self) -> None:
        """Turn off every box's highlight ring — called wherever the
        rest of the identify cycle resets (_begin_identify/_redo_
        identify/session end), so a box never keeps showing a *previous*
        session's highlight once a new identify cycle is underway. The
        box itself (and its instrument name) stays exactly as it was —
        unlike the old device-keyed design, there's nothing here that
        ever needs to go back to a "not yet identified" placeholder."""
        for box in self._device_boxes.values():
            box["frame"].configure(highlightthickness=0)

    # --- right column: Setlist ---

    def _build_right(self, right: ttk.Frame) -> None:
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        setlist_tab = ttk.Frame(right)
        setlist_tab.grid(row=0, column=0, sticky="nsew")

        setlist_tab.columnconfigure(0, weight=1)
        setlist_tab.rowconfigure(2, weight=1)
        setlist_header = ttk.Frame(setlist_tab)
        setlist_header.grid(row=0, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(setlist_header, text="Setlist", font=("TkDefaultFont", 11, "bold")).pack(side="left")
        ttk.Button(setlist_header, text="+ Add to Setlist", command=self._on_add_to_setlist).pack(side="right")

        self.setlist_total_var = tk.StringVar(value="")
        ttk.Label(setlist_tab, textvariable=self.setlist_total_var, foreground="#666666").grid(
            row=1, column=0, sticky="w", pady=(2, 4)
        )

        setlist_wrap = ttk.Frame(setlist_tab)
        setlist_wrap.grid(row=2, column=0, sticky="nsew", pady=(4, 0))
        setlist_wrap.columnconfigure(0, weight=1)
        setlist_wrap.rowconfigure(0, weight=1)

        canvas = tk.Canvas(setlist_wrap, highlightthickness=0, background="white")
        scrollbar = ttk.Scrollbar(setlist_wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.setlist_rows_frame = tk.Frame(canvas, background="white")
        content_window = canvas.create_window((0, 0), window=self.setlist_rows_frame, anchor="nw")

        def _on_content_configure(_event: object) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event: object) -> None:
            # Stretches setlist_rows_frame to the canvas's own width so
            # each row's labels fill it (and wrap at that width — see
            # SetlistRow._on_resize) — only the vertical extent scrolls.
            canvas.itemconfigure(content_window, width=event.width)

        self.setlist_rows_frame.bind("<Configure>", _on_content_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        self._setlist_canvas = canvas
        self._bind_setlist_mousewheel(canvas)

    def _on_setlist_mousewheel(self, event: object) -> None:
        # See studio_setup.StudioSetupFrame._on_mousewheel — same
        # macOS-vs-Windows delta-scaling story applies here.
        self._setlist_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")  # type: ignore[attr-defined]

    def _bind_setlist_mousewheel(self, widget: tk.Misc) -> None:
        """See studio_setup.StudioSetupFrame._bind_mousewheel's docstring
        for why this binds directly and recursively rather than once via
        bind_all. Re-run over self.setlist_rows_frame at the end of every
        _refresh_setlist, since that tears down and rebuilds every row."""
        widget.bind("<MouseWheel>", self._on_setlist_mousewheel)
        for child in widget.winfo_children():
            self._bind_setlist_mousewheel(child)

    def _installed_labels(self) -> list[str]:
        """Every distinct instrument label configured on the Studio Setup
        tab, in the order instruments were added — the set of labels a
        song can have a take "installed" for, and what a filter slot's
        next-up preview is computed per (see get_filter_slot_previews)."""
        if not self.config_obj:
            return []
        labels: list[str] = []
        for inst in self.config_obj.instruments:
            if inst.label and inst.label not in labels:
                labels.append(inst.label)
        return labels

    @staticmethod
    def _label_display_name(label: str) -> str:
        return label.replace("-", " ").title()

    def _track_title(self, track: TrackEntry, inst_name: str) -> str:
        dur = format_duration(track.duration_seconds)
        if track.is_inspiration_filter:
            # Never has a take of its own (see TrackEntry's docstring), and
            # which songs it's drawn — and their takes — now live in the
            # vault-wide shared index (vault.py), not on this slot. The
            # slot's own name is already the auto-derived filter label
            # (see inspiration.derive_filter_label). duration_seconds is
            # the average across every currently-matching track (see
            # inspiration.average_duration) rather than one fixed song's.
            return f"🎲 {track.name}  (~{dur})"
        # Takes are filed by label, not by which specific instrument
        # played them — a Telecaster take should still check off a song
        # for the Stratocaster too, since both are "electric-guitar"
        # (see StudioConfig.label_for_instrument).
        label = self.config_obj.label_for_instrument(inst_name) if self.config_obj else inst_name
        take = track.get_take_for_instrument(label)
        if take is None:
            mark = ""
        elif take.has_video:
            mark = " ✓"
        else:
            mark = " ✓ (audio only)"
        return f"{track.name}  ({dur}){mark}"

    def _track_stats(self, track: TrackEntry, preview: dict | None, inst_name: str) -> tuple[str, str]:
        """(stats, highlight) — the smaller-text line(s) shown below a
        setlist row's title, and an optional "next up" line called out in
        bigger text (see SetlistRow) — see _refresh_setlist. For an
        ordinary track: (which installed instrument labels already have a
        completed take, ""). For an inspiration filter slot: how many
        inspiration-server tracks currently match its criteria, plus —
        once autodetect knows what's being recorded (`inst_name`) — its
        "next up" pick specifically for that instrument's label, broken
        out as `highlight` instead of just another line among every
        installed label's; before that's known, every installed label's
        "next up" pick is listed in `stats` instead, same as before
        autodetect existed. (`preview` is from self._filter_previews —
        see that field's docstring for why it's not recomputed on every
        call.)"""
        labels = self._installed_labels()
        if not labels:
            return "", ""
        if track.is_inspiration_filter:
            if preview is None:
                return "Checking matches...", ""
            count = preview.get("match_count", 0)
            stats = f"{count} matching track{'s' if count != 1 else ''}"
            next_up = preview.get("next_up") or {}
            detected_label = self.config_obj.label_for_instrument(inst_name) if inst_name and self.config_obj else ""
            if detected_label and detected_label in labels:
                song = next_up.get(detected_label)
                name = self._label_display_name(detected_label)
                highlight = f"{name} next up: {song}" if song else f"{name}: no match"
                return stats, highlight
            lines = [stats]
            for label in labels:
                song = next_up.get(label)
                name = self._label_display_name(label)
                lines.append(f"{name} next up: {song}" if song else f"{name}: no match")
            return "\n".join(lines), ""
        parts = []
        for label in labels:
            take = track.get_take_for_instrument(label)
            name = self._label_display_name(label)
            if take is None:
                parts.append(f"{name} —")
            elif take.has_video:
                parts.append(f"{name} ✓")
            else:
                parts.append(f"{name} ✓ (audio)")
        return "    ".join(parts), ""

    def _refresh_setlist(self) -> None:
        for row in self._setlist_rows:
            row.destroy()
        self._setlist_rows = []
        if not self._setlist:
            self.setlist_total_var.set("")
            return
        inst_name = self._detected_instrument
        for i, track in enumerate(self._setlist.tracks):
            preview = self._filter_previews[i] if i < len(self._filter_previews) else None
            row = SetlistRow(
                self.setlist_rows_frame, i,
                on_press=self._on_row_press, on_motion=self._on_row_motion,
                on_release=self._on_row_release, on_right_click=self._on_row_right_click,
            )
            stats, highlight = self._track_stats(track, preview, inst_name)
            row.set_content(self._track_title(track, inst_name), stats, highlight)
            row.set_selected(i == self._selected_track_index)
            row.pack(fill="x")
            self._setlist_rows.append(row)
        self._bind_setlist_mousewheel(self.setlist_rows_frame)
        self._update_setlist_total()

    def _update_setlist_total(self) -> None:
        tracks = self._setlist.tracks if self._setlist else []
        if not tracks:
            self.setlist_total_var.set("")
            return
        total = sum(t.duration_seconds for t in tracks)
        track_word = "track" if len(tracks) == 1 else "tracks"
        self.setlist_total_var.set(f"{len(tracks)} {track_word} — {format_duration(total)} total")

    def _clear_selection(self) -> None:
        self._selected_track = None
        self._selected_track_index = None
        if hasattr(self, "selection_var"):
            self.selection_var.set("No track selected")
        for row in self._setlist_rows:
            row.set_selected(False)
        self._update_start_button_state()

    def _on_project_change(self, _event: object = None) -> None:
        self._clear_selection()
        if hasattr(self, "setlist_rows_frame"):
            for row in self._setlist_rows:
                row.destroy()
            self._setlist_rows = []
        self._filter_previews = []
        self._setlist = None
        project_name = self.project_var.get() if hasattr(self, "project_var") else ""
        self._project_name = project_name or None
        if _event is not None:
            self._persist_last_selection()
        if not self._project_name:
            self._hide_loading_overlay()
            return

        self._show_loading_overlay("Loading setlist...")
        backend = self.app_state.backend
        project_name = self._project_name
        self._run_backend(lambda: backend.get_setlist(project_name), self._on_setlist_loaded)

    def _on_new_project(self) -> None:
        NewProjectDialog(self, self.app_state.backend, self._on_project_created)

    def _on_add_to_setlist(self) -> None:
        if not self._project_name:
            messagebox.showerror("Cannot add", "Select a project first.")
            return
        AddToSetlistDialog(self, self.app_state.backend, self._project_name, self._refresh_setlist_from_server)

    def _on_project_created(self, project_name: str) -> None:
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.list_projects(),
            lambda projects, error: self._apply_new_project_list(
                projects if error is None else self._project_names, project_name
            ),
        )

    def _apply_new_project_list(self, projects: list[str], project_name: str) -> None:
        if not self.winfo_exists():
            return
        self._project_names = projects
        self.project_combo.configure(values=projects)
        self.project_var.set(project_name)
        self._on_project_change()
        self._persist_last_selection()

    def _on_setlist_loaded(self, data: dict | None, error: str | None) -> None:
        if error or data is None:
            self.selection_var.set(f"Could not load project: {error}")
            self._hide_loading_overlay()
            return
        self._setlist = Setlist.from_dict(data)
        self._filter_previews = []
        self._refresh_setlist()
        self._auto_select_default_track()
        if any(t.is_inspiration_filter for t in self._setlist.tracks):
            # Keep the overlay up through the (possibly several-second,
            # multi-query) filter-preview fetch — _handle_filter_preview_
            # status updates its text live, _apply_filter_previews takes
            # it down once this resolves either way.
            self._show_loading_overlay("Checking inspiration filters...")
            self._fetch_filter_previews(loading=True)
        else:
            # Nothing to query — no inspiration-server calls are about to
            # happen, so there's nothing worth blocking on.
            self._filter_previews = [None] * len(self._setlist.tracks)
            self._hide_loading_overlay()

    def _auto_select_default_track(self) -> None:
        """Pick a sensible starting track so Record is one click away right
        after opening: the setlist's first track."""
        if self._selected_track_index is not None:
            return  # already selected (e.g. this ran once for this project already)
        if self._setlist and self._setlist.tracks:
            self._select_row(0)

    def _fetch_filter_previews(self, loading: bool = False) -> None:
        """Kick off get_filter_slot_previews for the current project — see
        self._filter_previews's docstring for when this is (and isn't)
        called. `loading` means this is the project-open fetch driving the
        loading overlay (see _on_setlist_loaded) rather than a quiet
        background refresh (post-take, or after editing a filter's
        criteria) that shouldn't block the whole tab."""
        if not self._project_name:
            return
        backend = self.app_state.backend
        project_name = self._project_name
        self._run_backend(
            lambda: backend.get_filter_slot_previews(project_name),
            lambda previews, error: self._apply_filter_previews(project_name, previews, error, loading),
        )

    def _apply_filter_previews(
        self, project_name: str, previews: list | None, error: str | None, loading: bool = False,
    ) -> None:
        if loading and project_name == self._project_name:
            self._hide_loading_overlay()
        if error or previews is None or project_name != self._project_name:
            # A transient inspiration-server hiccup, or the project changed
            # again before this fetch returned — either way, leave whatever
            # self._filter_previews already had rather than blank it out.
            return
        self._filter_previews = previews
        self._refresh_setlist()

    def _refresh_setlist_from_server(self) -> None:
        """Re-fetch just the current project's setlist (e.g. after a take
        finishes) without disturbing the current project/instrument selection."""
        if not self._project_name:
            return
        backend = self.app_state.backend
        project_name = self._project_name
        self._run_backend(
            lambda: backend.get_setlist(project_name),
            lambda data, error: None if error else self._apply_refreshed_setlist(data),
        )

    def _apply_refreshed_setlist(self, data: dict) -> None:
        self._setlist = Setlist.from_dict(data)
        if len(self._setlist.tracks) != len(self._filter_previews):
            # The track count changed underneath us (a track was added or
            # removed elsewhere) — self._filter_previews is a parallel
            # array to self._setlist.tracks, so it's now misaligned and
            # needs a fresh fetch rather than being reused as-is. A take
            # simply completing doesn't change the count, so the normal
            # case (this firing after every take) skips the round trip.
            self._filter_previews = []
            self._fetch_filter_previews()
        self._refresh_setlist()

    def _persist_last_selection(self) -> None:
        """Remembered so the Record tab reopens on the same project next
        launch. last_selected_instrument isn't touched here — there's no
        manual instrument picker anymore, start_auto_detect_instrument
        persists it itself the moment it locks onto one (see backend.py)."""
        if self.config_obj is None:
            return
        self.config_obj.last_selected_project = self.project_var.get()
        backend = self.app_state.backend
        config = self.config_obj
        self._run_backend(lambda: backend.save_config(config))

    # --- instrument identify cycle (replaces the old manual dropdown) ---
    #
    # Mirrors RecordingDeckDriver's own identify_state machine (see
    # recording_driver.py's _begin_identify/_redo_identify/_confirm_start)
    # so the on-screen Stream Deck emulator behaves identically to the
    # physical one, just with this frame's own Tk-specific UI (spinner,
    # button disabling, error dialogs) layered on top instead of going
    # through the shared driver — same reason _on_emulator_key already
    # special-cased "r"/"s" before this feature existed. `self._identify_
    # state` is "idle" (Start Local/Start Streaming shown) | "identifying"
    # (a scan is listening; only Re-identify shown) | "ready" (auto-detect
    # committed to an instrument; Play + Re-identify shown).

    def _begin_identify(self, streaming: bool) -> None:
        """Start Local/Start Streaming was just pressed — remember which
        (read back by _confirm_start once Play is pressed) and kick off a
        fresh auto-detect scan. `self._phase` stays "idle" throughout;
        only Play actually opens a session."""
        self._pending_streaming = streaming
        self._identify_state = "identifying"
        self._detected_instrument = ""
        self._detected_is_midi = False
        self.detected_instrument_var.set("")
        self._reset_device_identifications()
        self.synth_voice_row.grid_remove()
        self.auto_detect_status_var.set("Listening...")
        self.redetect_button.state(["disabled"])
        self._tuner_note = None
        self._tuner_smoother.reset()
        self.tuner_meter.clear()
        self.tuner_meter.grid()
        self.streamdeck_emulator.update_recording_page(self._phase, self._video_check_phase, self._identify_state)
        self._update_start_button_state()
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.start_auto_detect_instrument(),
            lambda _result, error: self._on_auto_detect_start_result(error),
        )

    def _on_auto_detect_start_result(self, error: str | None) -> None:
        if not self.winfo_exists():
            return
        if error:
            self.auto_detect_status_var.set(error)

    def _redo_identify(self) -> None:
        """Re-identify — available throughout "identifying"/"ready" so a
        stalled or wrong detection can be restarted without backing all
        the way out to Start Local/Start Streaming (which would also lose
        the streaming/local choice already made)."""
        self._identify_state = "identifying"
        self._detected_instrument = ""
        self._detected_is_midi = False
        self.detected_instrument_var.set("")
        self._reset_device_identifications()
        self.synth_voice_row.grid_remove()
        self.auto_detect_status_var.set("Listening...")
        self.redetect_button.state(["disabled"])
        self._tuner_note = None
        self._tuner_smoother.reset()
        self.tuner_meter.clear()
        self.streamdeck_emulator.update_recording_page(self._phase, self._video_check_phase, self._identify_state)
        self._update_start_button_state()
        backend = self.app_state.backend
        # Fire-and-forget the stop (a no-op if nothing's actually still
        # running, e.g. detection already committed) then start a fresh
        # scan — chained so the second call doesn't race the first's
        # mutual-exclusion teardown.
        self._run_backend(
            lambda: (backend.stop_auto_detect_instrument(), backend.start_auto_detect_instrument()),
            lambda _result, error: self._on_auto_detect_start_result(error),
        )

    def _confirm_start(self) -> None:
        """Play, pressed once auto-detect has committed to an instrument —
        the moment the identify cycle actually ends and a session opens.
        Deliberately doesn't reset self._identify_state itself:
        _handle_backend_event's "recording_status" handling does that once
        _phase actually leaves "idle", so a start failure (see
        _on_start_result) leaves identify_state — and the emulator's
        still-displayed "ready" layout — untouched, ready for the user to
        just press Play again rather than losing the detected instrument
        and having to redo the whole identify cycle."""
        self._start_recording(streaming=self._pending_streaming)

    def _handle_auto_detect_status(self, data: dict) -> None:
        phase = data.get("phase")
        if phase == "listening":
            self.auto_detect_status_var.set(data.get("status", "Listening..."))
        elif phase == "detected":
            name = data.get("instrument", "")
            self._detected_instrument = name
            self.detected_instrument_var.set(name.upper())
            self._mark_device_identified(name)
            label = data.get("label", "")
            full_name = data.get("full_name", "")
            detail = " — ".join(part for part in (label, full_name) if part)
            self.auto_detect_status_var.set(f"Detected ({detail})" if detail else "Detected.")
            self._detected_is_midi = bool(data.get("is_midi"))
            if self._detected_is_midi:
                self.synth_voice_var.set(data.get("synth_voice") or DEFAULT_SYNTH_VOICE)
                self.synth_voice_row.grid()
            else:
                self.synth_voice_row.grid_remove()
            if self._identify_state == "identifying":
                self._identify_state = "ready"
                # Every channel's stream just got torn down (see Backend.
                # start_auto_detect_instrument), so no more tuner_status
                # events are coming until the next scan — but deliberately
                # leave the meter showing its last reading rather than
                # hiding it: it should stay up right through "ready" until
                # Play actually opens a session (_confirm_start) or Re-
                # identify starts over (_redo_identify), not disappear the
                # instant detection locks in before there's been a chance
                # to actually look at it and finish tuning.
            self.redetect_button.state(["!disabled" if self._phase == "idle" else "disabled"])
            self._refresh_setlist()
            self._update_start_button_state()
        elif phase == "stopped":
            self.auto_detect_status_var.set("")
        self.streamdeck_emulator.update_recording_page(self._phase, self._video_check_phase, self._identify_state)

    def _on_synth_voice_changed(self, _event: object = None) -> None:
        """The "Sound" picker was changed — applies immediately (no
        separate confirm step), whether or not a session is currently
        recording (see Backend.set_synth_voice: it reaches whichever
        engine — the ambient monitor's during "ready", the session's
        during "recording" — currently has this exact instrument open)."""
        if not self._detected_instrument:
            return
        instrument_name = self._detected_instrument
        voice = self.synth_voice_var.get()
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.set_synth_voice(instrument_name, voice),
            lambda _result, error: self._on_synth_voice_set_result(error),
        )

    def _on_synth_voice_set_result(self, error: str | None) -> None:
        if not self.winfo_exists():
            return
        if error:
            messagebox.showerror("Couldn't change sound", error)

    def _handle_tuner_status(self, data: dict) -> None:
        # Meaningful throughout the whole identify cycle, not just
        # "identifying" — once auto-detect commits, Backend keeps the
        # needle live via _attach_tuner_sink (tapping the ambient monitor
        # it opens on the just-detected channel), so readings keep
        # arriving all through "ready" too, right up until Play actually
        # opens a session (see backend.py's on_channel_detected/
        # _attach_tuner_sink). Only "idle" — no identify cycle in
        # progress at all — means a reading has nothing left to show it
        # on (e.g. one straggling in from a scan already superseded by a
        # session opening some other way).
        if self._identify_state == "idle":
            return
        note = data.get("note")
        if note is None:
            return
        self._tuner_note = note
        self._tuner_cents = self._tuner_smoother.update(note, float(data.get("cents", 0.0)))
        self.tuner_meter.set_reading(self._tuner_note, self._tuner_cents)

    def _select_row(self, index: int) -> None:
        if not self._setlist or index >= len(self._setlist.tracks):
            return
        self._selected_track = self._setlist.tracks[index]
        self._selected_track_index = index
        self.selection_var.set(f"Selected: {self._selected_track.name}")
        for row in self._setlist_rows:
            row.set_selected(row.index == index)
        self._update_start_button_state()

    def _row_index_at_y(self, y_root: int) -> int | None:
        """Which setlist row (by index) currently covers root-window y
        coordinate y_root, clamped to the first/last row — the SetlistRow
        equivalent of tk.Listbox.nearest(), needed since a drag started on
        one row keeps receiving <B1-Motion> events (with coordinates
        relative to that original row) even once the pointer has moved
        over another; x_root/y_root stay accurate regardless."""
        if not self._setlist_rows:
            return None
        y_local = y_root - self.setlist_rows_frame.winfo_rooty()
        if y_local <= 0:
            return self._setlist_rows[0].index
        for row in self._setlist_rows:
            top = row.winfo_y()
            if top <= y_local < top + row.winfo_height():
                return row.index
        return self._setlist_rows[-1].index

    def _on_row_press(self, index: int) -> None:
        if self._setlist_interactive and self._setlist:
            self._select_row(index)
        if self._phase != "idle" or not self._setlist:
            self._drag_index = None
            return  # don't let the setlist reorder out from under an active take
        self._drag_index = index
        self._drag_moved = False

    def _on_row_motion(self, event: object) -> None:
        if self._drag_index is None or not self._setlist or not self._setlist.tracks:
            return
        target = self._row_index_at_y(event.y_root)  # type: ignore[attr-defined]
        if target is None or target == self._drag_index:
            return
        self._setlist.move_track(self._drag_index, target)
        if len(self._filter_previews) == len(self._setlist.tracks):
            preview = self._filter_previews.pop(self._drag_index)
            self._filter_previews.insert(target, preview)
        self._drag_index = target
        self._drag_moved = True
        if self._selected_track_index is not None:
            self._selected_track_index = target
        self._refresh_setlist()

    def _on_row_release(self) -> None:
        if self._drag_moved:
            self._save_setlist_and_refresh()
        self._drag_index = None
        self._drag_moved = False

    def _on_row_right_click(self, index: int, event: object) -> None:
        if self._phase != "idle" or not self._setlist:
            return  # don't let the setlist change out from under an active take
        if index < 0 or index >= len(self._setlist.tracks):
            return
        self._select_row(index)

        track = self._setlist.tracks[index]
        menu = tk.Menu(self, tearoff=0)
        if track.is_inspiration_filter:
            menu.add_command(label="Edit...", command=lambda: self._on_edit_filter(index))
            menu.add_command(label="Show tracks...", command=lambda: self._on_show_filter_tracks(index))
        else:
            menu.add_command(label="Rename...", command=lambda: self._on_rename_track(index))
        menu.add_command(label="Delete", command=lambda: self._on_delete_track(index))
        try:
            menu.tk_popup(event.x_root, event.y_root)  # type: ignore[attr-defined]
        finally:
            menu.grab_release()

    def _on_edit_filter(self, index: int) -> None:
        if not self._setlist or index >= len(self._setlist.tracks):
            return
        track = self._setlist.tracks[index]

        def on_save(new_criteria: dict) -> None:
            track.inspiration_filter = new_criteria
            # The cached match list (backend.py's get_filter_slot_previews)
            # was queried against the *old* criteria — stale now, and left
            # alone it would keep being reused for a filter it no longer
            # describes. Clearing it here forces a fresh inspiration-server
            # query (and a fresh cache) the next time previews are fetched.
            track.cached_matches = []
            track.name = derive_filter_label(new_criteria)
            if self._selected_track_index == index:
                self.selection_var.set(f"Selected: {track.name}")
            backend = self.app_state.backend
            self._run_backend(
                lambda: average_duration(backend.search_inspiration_by_filter(new_criteria)),
                lambda avg, error: self._apply_filter_slot_duration(index, avg if error is None else 0.0),
            )

        EditFilterDialog(self, self.app_state.backend, track.inspiration_filter, on_save)

    def _apply_filter_slot_duration(self, index: int, avg_duration: float) -> None:
        if not self._setlist or index >= len(self._setlist.tracks):
            return
        self._setlist.tracks[index].duration_seconds = avg_duration
        # The filter's criteria just changed (this only ever runs from
        # _on_edit_filter's on_save) — self._filter_previews[index] was
        # computed against the *old* criteria, so it needs a fresh fetch
        # once the new criteria are actually saved to disk (get_filter_
        # slot_previews reads the setlist back from disk, so it can't be
        # fetched before the save below completes).
        self._save_setlist_and_refresh(refresh_filter_previews=True)

    def _on_show_filter_tracks(self, index: int) -> None:
        if not self._setlist or index >= len(self._setlist.tracks):
            return
        track = self._setlist.tracks[index]
        ShowTracksDialog(self, self.app_state.backend, track.name, track.inspiration_filter)

    def _on_rename_track(self, index: int) -> None:
        if not self._setlist or index >= len(self._setlist.tracks):
            return
        track = self._setlist.tracks[index]
        new_name = simpledialog.askstring(
            "Rename Track", "Track name:", initialvalue=track.name, parent=self,
        )
        new_name = (new_name or "").strip()
        if not new_name or new_name == track.name:
            return
        track.name = new_name
        if self._selected_track_index == index:
            self.selection_var.set(f"Selected: {track.name}")
        self._save_setlist_and_refresh()

    def _on_delete_track(self, index: int) -> None:
        if not self._setlist or index >= len(self._setlist.tracks):
            return
        track = self._setlist.tracks[index]
        if not messagebox.askyesno("Delete Track", f'Remove "{track.name}" from the setlist?', parent=self):
            return
        self._setlist.remove_track(index)
        if len(self._filter_previews) > index:
            self._filter_previews.pop(index)
        if self._selected_track_index == index:
            self._clear_selection()
        self._save_setlist_and_refresh()

    def _save_setlist_and_refresh(self, refresh_filter_previews: bool = False) -> None:
        if not self._project_name or not self._setlist:
            return
        backend = self.app_state.backend
        project_name = self._project_name
        setlist_data = self._setlist.to_dict()
        self._run_backend(
            lambda: backend.save_setlist(project_name, setlist_data),
            lambda _result, error: self._on_setlist_saved(error, refresh_filter_previews),
        )

    def _on_setlist_saved(self, error: str | None, refresh_filter_previews: bool = False) -> None:
        if error:
            messagebox.showerror("Save failed", error)
            return
        if refresh_filter_previews:
            self._fetch_filter_previews()
        self._refresh_setlist()

    def _update_start_button_state(self) -> None:
        if not hasattr(self, "video_check_button"):
            return
        ready = (
            bool(self._detected_instrument)
            and self._project_name is not None
            and self._selected_track_index is not None
        )
        if self._video_check_phase != "idle":
            self.video_check_button.state(["!disabled"])
        else:
            can_check = ready and self._phase == "idle"
            self.video_check_button.state(["!disabled"] if can_check else ["disabled"])

    def _set_controls_enabled(self, enabled: bool) -> None:
        combo_state = "readonly" if enabled else "disabled"
        self.project_combo.configure(state=combo_state)
        self._setlist_interactive = enabled
        self.refresh_devices_button.state(["!disabled"] if enabled else ["disabled"])
        self.redetect_button.state(
            ["!disabled" if (enabled and self._identify_state in ("identifying", "ready")) else "disabled"]
        )

    # --- refresh devices (camera/audio plugged in after the UI was launched) ---

    def _on_refresh_devices(self) -> None:
        if self._phase != "idle":
            return  # camera's held exclusively by the active recording; nothing to refresh into
        self.refresh_devices_button.state(["disabled"])
        self.status_var.set("Refreshing devices...")
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.refresh_devices(), lambda _result, error: self._on_refresh_devices_result(error)
        )

    def _on_refresh_devices_result(self, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self.refresh_devices_button.state(["!disabled"])
        if error:
            self.status_var.set(f"Refresh failed: {error}")
            return
        self.status_var.set("Devices refreshed.")
        # The backend's camera capture thread has been restarted by
        # refresh_devices(); tear down and reopen this frame's own
        # subscription so a preview that never came up (no camera at launch)
        # gets a fresh "Waiting for camera preview..." rather than staying on
        # "No camera preview" from before the config even had one attached.
        self._stop_preview()
        self._start_preview()

    # --- camera preview (frames streamed from the backend, local or remote) ---

    def _start_preview(self) -> None:
        if self._preview_sub is not None:
            return
        if self.config_obj is not None and not self.config_obj.camera_device:
            self.preview_label.configure(text="No camera configured", image="")
            return
        self.preview_label.configure(text="Waiting for camera preview...", image="")
        self._preview_sub = self.app_state.backend.open_camera_preview(self._on_preview_frame)

    def _on_preview_label_resize(self, event: object) -> None:
        self._preview_width = event.width  # type: ignore[attr-defined]

    def _on_preview_frame(self, jpeg: bytes) -> None:
        self.after(0, lambda: self._render_preview_frame(jpeg))

    def _render_preview_frame(self, jpeg: bytes) -> None:
        if self._preview_sub is None:
            return  # unsubscribed since this frame was queued onto the Tk thread
        from PIL import Image, ImageTk
        try:
            image = Image.open(io.BytesIO(jpeg))
        except Exception:
            return
        # Camera frames come in at a fixed, deliberately small capture size (see
        # backend.py) to keep local/remote streaming cheap. Upscale to fill the
        # width Tk has actually given the label, preserving aspect ratio.
        target_width = self._preview_width
        if target_width and target_width != image.width:
            new_height = max(1, round(image.height * target_width / image.width))
            image = image.resize((target_width, new_height), Image.LANCZOS)
        self._preview_imgtk = ImageTk.PhotoImage(image)
        self.preview_label.configure(image=self._preview_imgtk, text="")

    def _stop_preview(self) -> None:
        if self._preview_sub is not None:
            self._preview_sub.close()
            self._preview_sub = None

    # --- VU meters (polled directly — get_levels() is cheap: a local attribute
    # read for LocalBackend, and a no-op returning silence for RemoteBackend,
    # so there's no need to hop to a worker thread like the other backend calls) ---

    def _poll_levels(self) -> None:
        if not self.winfo_exists():
            return
        if hasattr(self, "instrument_meter"):
            try:
                instrument_level, backing_level = self.app_state.backend.get_levels()
            except BackendError:
                instrument_level, backing_level = 0.0, 0.0
            self.instrument_meter.set_level(instrument_level)
            self.backing_meter.set_level(backing_level)
        self.after(50, self._poll_levels)

    def _on_destroy(self, _event: object) -> None:
        self._stop_preview()
        if self._current_backend is not None:
            self._current_backend.off_event(self._on_backend_event)
            if not self._detected_instrument:
                # Fire-and-forget: this frame is persistent (see app.py's
                # TABS), so _on_destroy only actually runs at app
                # shutdown — best-effort cleanup, not something anything
                # else waits on.
                try:
                    self._current_backend.stop_auto_detect_instrument()
                except BackendError:
                    pass
        self.app_state.remove_listener(self._on_app_state_changed)
        self._streamdeck_driver.disconnect()

    # --- recording ---

    def _on_emulator_key(self, key: str) -> None:
        """Dispatch a click on the on-screen Stream Deck emulator. "r"
        (Start Local/Unpause/Stop), "s" (Start Streaming), "i" (Re-
        identify), and "p" (Play) reuse this frame's own request-building
        and loading-state handling verbatim — same as a mouse click on the
        old ttk button did — since they're the keys with real Tk-side UI
        state to manage (see the "instrument identify cycle" section
        above). Every other key (Next/Restart/Monitor/volume) has no Tk
        equivalent of its own and goes straight through the shared driver,
        exactly like a physical Stream Deck press does (see
        _on_streamdeck_key)."""
        if key in ("r", "s"):
            self._on_toggle_recording(streaming=(key == "s"))
        elif key == "i":
            if self._phase == "idle" and self._identify_state in ("identifying", "ready"):
                self._redo_identify()
        elif key == "p":
            if self._phase == "idle" and self._identify_state == "ready":
                self._confirm_start()
        else:
            self._streamdeck_driver.handle_key(key)

    def _on_toggle_recording(self, streaming: bool = False) -> None:
        if self._phase == "idle":
            if self._identify_state == "idle":
                self._begin_identify(streaming=streaming)
        elif self._phase == "waiting":
            self._unpause_recording()
        elif self._phase == "recording":
            self._stop_recording()

    def _build_selected_request(self) -> StartRecordingRequest | None:
        """Build a StartRecordingRequest from the auto-detected instrument
        and current project/track selection — shared by _start_recording
        and _start_video_check. Shows an error dialog and returns None if
        incomplete."""
        instrument_name = self._detected_instrument
        if not instrument_name:
            messagebox.showerror("Cannot start", "Still listening for an instrument — play something first.")
            return None
        if not self._project_name:
            messagebox.showerror("Cannot start", "Select a project first.")
            return None

        if self._selected_track_index is not None:
            return StartRecordingRequest(
                project_name=self._project_name, instrument_name=instrument_name,
                track_index=self._selected_track_index,
            )
        messagebox.showerror("Cannot start", "Select a track from the Setlist first.")
        return None

    def _start_recording(self, streaming: bool = False) -> None:
        req = self._build_selected_request()
        if req is None:
            return

        self.streamdeck_emulator.set_key_enabled(0, False)
        self.streamdeck_emulator.set_key_enabled(1, False)
        self._set_controls_enabled(False)
        self.status_var.set("Loading...")
        backend = self.app_state.backend

        def do_start():
            # Force config.streaming_enabled to match which idle-layout
            # button ("Start Local"/"Start Streaming") was pressed, rather
            # than starting whatever the Streaming settings tab last
            # happened to be left at — everything else about the session is
            # identical either way; start_recording() itself opens the
            # session, same as a physical Stream Deck press does (see the
            # recording section of backend.py).
            config = backend.get_config()
            if config.streaming_enabled != streaming:
                config.streaming_enabled = streaming
                backend.save_config(config)
            backend.start_recording(req)

        self._run_backend(do_start, lambda _result, error: self._on_start_result(error))

    def _on_start_result(self, error: str | None) -> None:
        # On success, the "recording_status" event the backend emits before
        # start_recording() returns already updated button/state via
        # _handle_backend_event — nothing left to do here.
        if error:
            messagebox.showerror("Cannot start", error)
            self._set_controls_enabled(True)
            self.status_var.set("")
            self.streamdeck_emulator.set_key_enabled(0, True)
            self.streamdeck_emulator.set_key_enabled(1, True)
            self._update_start_button_state()

    def _unpause_recording(self) -> None:
        self.streamdeck_emulator.set_key_enabled(0, False)
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.unpause_recording(), lambda _result, error: self._on_unpause_result(error)
        )

    def _on_unpause_result(self, error: str | None) -> None:
        # On success, the "recording_status" event already updated the button
        # via _handle_backend_event — nothing left to do here.
        if error:
            messagebox.showerror("Cannot unpause", error)
            self.streamdeck_emulator.set_key_enabled(0, True)

    def _stop_recording(self) -> None:
        self.streamdeck_emulator.set_key_enabled(0, False)
        backend = self.app_state.backend
        self._run_backend(lambda: backend.stop_recording(), lambda _result, error: self._on_stop_result(error))

    def _on_stop_result(self, error: str | None) -> None:
        self.streamdeck_emulator.set_key_enabled(0, True)
        if error:
            messagebox.showerror("Stop failed", error)

    # --- video check ---

    def _on_toggle_video_check(self) -> None:
        if self._video_check_phase == "idle":
            self._start_video_check()
        else:
            self._stop_video_check()

    def _start_video_check(self) -> None:
        req = self._build_selected_request()
        if req is None:
            return

        self.video_check_button.state(["disabled"])
        self._set_controls_enabled(False)
        self.status_var.set("Loading...")
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.start_video_check(req), lambda _result, error: self._on_video_check_start_result(error)
        )

    def _on_video_check_start_result(self, error: str | None) -> None:
        # On success, the "video_check_status" event the backend emits before
        # start_video_check() returns already updated button/state via
        # _handle_backend_event — nothing left to do here.
        if error:
            messagebox.showerror("Cannot start video check", error)
            self._set_controls_enabled(True)
            self.status_var.set("")
            self._update_start_button_state()

    def _stop_video_check(self) -> None:
        self.video_check_button.state(["disabled"])
        backend = self.app_state.backend
        self._run_backend(
            lambda: backend.stop_video_check(), lambda _result, error: self._on_video_check_stop_result(error)
        )

    def _on_video_check_stop_result(self, error: str | None) -> None:
        self.video_check_button.state(["!disabled"])
        if error:
            messagebox.showerror("Stop failed", error)

    # --- backend events (recording_status / preview_paused / preview_resumed) ---

    def _on_backend_event(self, event: str, data: dict) -> None:
        self.after(0, lambda: self._handle_backend_event(event, data))

    _VIDEO_CHECK_BUTTON_TEXT = {"idle": "Video Check", "recording": "Stop Video Check"}

    def _update_recording_active(self) -> None:
        self.app_state.recording_active = (
            self._phase in ("waiting", "recording") or self._video_check_phase == "recording"
        )

    def _handle_backend_event(self, event: str, data: dict) -> None:
        if event == "recording_status":
            if "status" in data:
                self.status_var.set(data["status"])
            if "phase" in data:
                self._phase = data["phase"]
                self._update_recording_active()
                if self._phase == "idle":
                    self._refresh_setlist_from_server()
                    # Reset for the *next* recording — a session just
                    # ended, and the performer may well pick up something
                    # else for the next one. Detection itself only
                    # (re)starts once Start Local/Start Streaming is
                    # pressed again — see _begin_identify.
                    self._detected_instrument = ""
                    self._detected_is_midi = False
                    self.detected_instrument_var.set("")
                    self._reset_device_identifications()
                    self.synth_voice_row.grid_remove()
                    self.auto_detect_status_var.set("")
                    self._identify_state = "idle"
                    self.redetect_button.state(["disabled"])
                    self._tuner_note = None
                    self.tuner_meter.grid_remove()
                else:
                    # A session just actually opened — whether via Play
                    # finishing the identify cycle (the normal path;
                    # _confirm_start deliberately leaves this alone so a
                    # start failure doesn't lose "ready") or some other way
                    # entirely (e.g. a different Remote client called
                    # start_recording directly while this frame's own
                    # identify cycle was still in flight).
                    self._identify_state = "idle"
                    self._tuner_note = None
                    self.tuner_meter.grid_remove()
                self.streamdeck_emulator.update_recording_page(self._phase, self._video_check_phase, self._identify_state)
                self.streamdeck_emulator.set_key_enabled(0, True)
                self._set_controls_enabled(self._phase == "idle")
                self._update_start_button_state()
        elif event == "instrument_detected":
            # Fired continuously during an active session by a separate,
            # best-effort live classifier (see backend.py's start_recording)
            # that keeps re-guessing which *configured* instrument the input
            # sounds most like from its frequency content alone — a "does
            # this still sound right" sanity check, not authoritative: it
            # never touches the take's actual instrument/label assignment
            # (that was already locked in by auto-detect before the session
            # began). It used to overwrite the big "what we're recording as"
            # header with its latest guess — which, being untrained/best-
            # effort, could misfire (e.g. call a bass "Keystation" mid-take)
            # and make it look like the session's instrument had silently
            # changed, when nothing about the take actually had. Surface it
            # on the smaller status line instead, and never touch
            # detected_instrument_var once a session has locked one in.
            if "instrument" in data:
                confidence = data.get("confidence")
                detail = f" ({confidence:.0%} confidence)" if isinstance(confidence, (int, float)) else ""
                self.auto_detect_status_var.set(f"Live check: sounds like {data['instrument']}{detail}")
        elif event == "auto_detect_status":
            self._handle_auto_detect_status(data)
        elif event == "tuner_status":
            self._handle_tuner_status(data)
        elif event == "video_check_status":
            if "status" in data:
                self.status_var.set(data["status"])
            if "phase" in data:
                self._video_check_phase = data["phase"]
                self._update_recording_active()
                self.streamdeck_emulator.update_recording_page(self._phase, self._video_check_phase, self._identify_state)
                self.video_check_button.configure(text=self._VIDEO_CHECK_BUTTON_TEXT[self._video_check_phase])
                self.video_check_button.state(["!disabled"])
                self._set_controls_enabled(self._video_check_phase == "idle")
                self._update_start_button_state()
                # Dialog opening is handled by _streamdeck_driver's own
                # on_video_check_result hook (see _on_streamdeck_video_check_
                # result) — it's subscribed to this same event independently,
                # regardless of whether Video Check was triggered by mouse or
                # by the physical Stream Deck.
        elif event == "monitoring_mode_changed":
            # Fired by Backend.set_monitoring_mode() from any client — this
            # frame's own emulator "m" key, a physical Stream Deck, or a
            # Remote client — so the emulator's monitor-toggle tile stays in
            # sync no matter which one changed it.
            if "mode" in data:
                self._monitoring_mode = data["mode"]
                self.streamdeck_emulator.update_monitoring_mode(self._monitoring_mode)
        elif event == "preview_paused":
            self.preview_label.configure(text="Recording — preview paused", image="")
        elif event == "preview_resumed":
            self.preview_label.configure(text="Waiting for camera preview...", image="")
        elif event == "preview_error":
            self.preview_label.configure(text=data.get("message", "Camera preview error."), image="")
        elif event == "streaming_status":
            if "status" in data:
                self.status_var.set(data["status"])
        elif event == "filter_preview_status":
            # Broadcast by get_filter_slot_previews (backend.py) as it works
            # through a project's filter slots — see that method's
            # docstring. Ignore one meant for a project we're not (or no
            # longer) looking at: e.g. a stale event from a project switch
            # that's since moved on, or in Remote mode, another connected
            # client loading a different project.
            if data.get("project_name") == self._project_name:
                index, total, label = data.get("index", 0), data.get("total", 0), data.get("label", "")
                status = f"Checking filter {index} of {total}: {label}" if label else f"Checking filter {index} of {total}..."
                self._set_loading_status(status)
