"""Shared physical Stream Deck driver for every recording context (Tk UI,
headless `takeloom server`, and the CLI) — one place for button layout,
key dispatch, and state, so the deck behaves identically no matter which
one is driving it.

This module touches only `Backend` and `StreamDeckController` — nothing
Tk-specific, nothing terminal-specific. A context supplies two hooks:

- `resolve_start_request()`: how to pick a project/instrument/track when
  "r" or "n" is pressed while idle. The Tk UI answers from whatever's
  selected in its Setlist/Inspiration picker; the headless server and CLI
  answer from the last-used project/instrument plus the next untaken track.
- `on_video_check_result(path, has_video)`: how to hand off a finished
  video check — there's no Stream Deck button for starting one anymore
  (key index 3 is now the Live/Production monitor toggle, "m"), but a
  check started from the Tk UI or a Remote client still finishes here.
  The Tk UI opens a review dialog; headless/CLI open the OS default
  player directly (no GUI to pop a window in).

The backend owns everything session-shaped: pressing record auto-opens the
session, a song reaching its natural end auto-advances to the next one, and
stop ends the session (see the recording section of backend.py). The keys
here just forward to it.

Both hooks — and `log()` — may be called from a background thread (the
Stream Deck's own key-event thread, or a backend event callback); a
context whose hooks touch UI-toolkit state is responsible for marshalling
onto its own main thread itself, the same way it already does for any
other cross-thread backend call.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from .backend import Backend, BackendError, StartRecordingRequest
from .streamdeck_controller import StreamDeckController

_TICKER_INTERVAL_SECONDS = 1.0


class RecordingDeckDriver:
    def __init__(
        self,
        backend: Backend,
        resolve_start_request: Callable[[], StartRecordingRequest | None],
        on_video_check_result: Callable[[Path, bool], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._backend = backend
        self._resolve_start_request = resolve_start_request
        self._on_video_check_result = on_video_check_result
        self._log = log or (lambda msg: None)
        self.streamdeck = StreamDeckController()
        self.phase = "idle"  # "idle" | "waiting" | "recording"
        self.video_check_phase = "idle"  # "idle" | "recording"
        self.track_name: str | None = None  # currently loaded/playing backing track — see _on_backend_event
        # What Backend.start_auto_detect_instrument last reported — shown
        # on the Stream Deck's touchscreen (see StreamDeckController.
        # _update_touchscreen) for as long as it's still current, whether
        # idle or mid-session: it deliberately keeps showing through an
        # entire take, not just at the moment of detection, so glancing at
        # the deck at any point — including mid-recording — confirms what
        # the take is actually being filed under. That's what would have
        # caught a real bug once already (a headless session silently
        # recording under a stale last-picked instrument with no way to
        # see what was actually about to be used). "Detecting…" while a
        # scan is in progress, the label (e.g. "ELECTRIC-BASS") once one's
        # found — only reset once a *new* scan actually starts for the
        # next take, not merely because a session opened or closed.
        # Nothing here *starts* auto-detect on its own — the Tk UI (record.
        # py) and headless `takeloom server` (__main__.py's server_command)
        # each decide for themselves when a fresh scan makes sense (e.g.
        # after a take finishes) and call Backend.start_auto_detect_
        # instrument() directly; this only ever reacts to whatever result
        # arrives, so it works identically no matter which one is driving.
        self.detected_instrument: str | None = None
        self._events_subscribed = False

        # Polls Backend.get_playback_position() roughly once a second
        # while a session is actually open, feeding the touchscreen's
        # progress bar (see StreamDeckController.update_playback_position)
        # — the only piece of the touchscreen that changes continuously
        # rather than at discrete event points, so it can't be driven by
        # _on_backend_event the way everything else here is. Runs for the
        # driver's whole lifetime (not just between connect()/disconnect())
        # since it's a daemon thread and update_playback_position already
        # no-ops instantly whenever there's no dial deck actually
        # connected — simpler than coordinating start/stop against every
        # place connect()/disconnect()/rebind_backend() get called.
        threading.Thread(target=self._run_ticker, daemon=True).start()

    def _run_ticker(self) -> None:
        while True:
            time.sleep(_TICKER_INTERVAL_SECONDS)
            if self.phase not in ("waiting", "recording") or not self.streamdeck.connected:
                continue
            try:
                position, duration = self._backend.get_playback_position()
            except BackendError:
                continue
            self.streamdeck.update_playback_position(position, duration)

    # --- connect / disconnect ---

    def _subscribe_backend_events(self) -> None:
        if not self._events_subscribed:
            self._backend.on_event(self._on_backend_event)
            self._events_subscribed = True

    def _unsubscribe_backend_events(self) -> None:
        if self._events_subscribed:
            self._backend.off_event(self._on_backend_event)
            self._events_subscribed = False

    def connect(self, key_callback: Callable[[str], None] | None = None) -> bool:
        """Start listening for backend events — this drives phase tracking
        and hooks like on_video_check_result() regardless of whether a
        physical Stream Deck is present, since e.g. a mouse-triggered video
        check still needs its result handed off. Then, separately, open the
        Stream Deck selected in settings (StudioConfig.streamdeck_id): does
        no hardware probing at all if none is configured. `key_callback`, if
        given, wraps handle_key (e.g. to marshal onto a UI's main thread) —
        otherwise handle_key is used directly. Returns whether an actual
        Stream Deck connected."""
        self._subscribe_backend_events()
        try:
            device_id = self._backend.get_config().streamdeck_id
        except BackendError:
            device_id = ""
        if not device_id:
            self._log("Skipping StreamDeck initialization, as none are configured.")
            return False
        if not self.streamdeck.connect(key_callback or self.handle_key, device_id=device_id):
            return False
        self.streamdeck.use_recording_layout()
        self.streamdeck.update_recording_page(self.phase, self.video_check_phase, self.track_name, self.detected_instrument)
        self._refresh_monitoring_mode()
        return True

    def disconnect(self) -> None:
        self._unsubscribe_backend_events()
        self.streamdeck.disconnect()

    def rebind_backend(self, backend: Backend) -> None:
        """Point this driver at a different backend (e.g. the Tk UI
        connecting to/disconnecting from a Remote) without touching the
        physical Stream Deck connection itself. Resets phase state to idle
        since the new backend's actual state is unknown until its first
        event arrives. Always re-subscribes to the new backend's events,
        independent of whether a physical Stream Deck is connected — see
        connect()."""
        self._unsubscribe_backend_events()
        self._backend = backend
        self.phase = "idle"
        self.video_check_phase = "idle"
        self.track_name = None
        self.detected_instrument = None
        self._subscribe_backend_events()
        self.streamdeck.update_recording_page(self.phase, self.video_check_phase, self.track_name, self.detected_instrument)
        self._refresh_monitoring_mode()

    def _refresh_monitoring_mode(self) -> None:
        try:
            self.streamdeck.update_monitoring_mode(self._backend.get_monitoring_mode())
        except BackendError:
            pass

    # --- key dispatch (the single canonical behavior for every context) ---

    def handle_key(self, key: str) -> None:
        try:
            if key == "r":
                if self.phase == "idle":
                    self._start_recording(streaming=False)
                elif self.phase == "waiting":
                    self._backend.unpause_recording()
                elif self.phase == "recording":
                    self._backend.stop_recording()
            elif key == "s":
                if self.phase == "idle":
                    self._start_recording(streaming=True)
            elif key == "n":
                # Only meaningful with a session already open — idx 2 isn't
                # part of the idle layout (RECORDING_IDLE_BUTTONS), so there's
                # no "start via Next" shortcut anymore; use "r"/"s" instead.
                if self.phase != "idle":
                    self._backend.next_track()
            elif key == "d":
                if self.phase in ("waiting", "recording"):
                    self._backend.redraw_current_track()
            elif key == "b":
                if self.phase == "recording":
                    self._backend.restart_take()
            elif key == "m":
                # Monitor toggle (idx 3) is likewise only part of the active
                # layout — nothing to toggle while idle.
                if self.phase != "idle":
                    self._toggle_monitoring_mode()
            elif key in ("l", "u", "[", "]", ",", "."):
                delta = 5 if key in ("u", "]", ".") else -5
                if key in ("[", "]"):
                    self._backend.adjust_takes_volume(delta)
                elif key in (",", "."):
                    self._backend.adjust_instrument_volume(delta)
                else:
                    self._backend.adjust_backing_volume(delta)
        except BackendError as e:
            self._log(f"StreamDeck: {e}")

    def _start_recording(self, streaming: bool) -> None:
        """Start a session, forcing config.streaming_enabled to match which
        idle-layout button ("Start Local"/"Start Streaming") was pressed —
        see RECORDING_IDLE_BUTTONS — rather than starting whatever the
        Streaming settings tab last happened to be left at. Everything else
        about the session is identical either way; start_recording() itself
        decides whether streaming actually happens (e.g. it still needs a
        camera and a YouTube stream key configured)."""
        req = self._resolve_start_request()
        if req is None:
            return
        config = self._backend.get_config()
        if config.streaming_enabled != streaming:
            config.streaming_enabled = streaming
            self._backend.save_config(config)
        self._backend.start_recording(req)

    def _toggle_monitoring_mode(self) -> None:
        current = self._backend.get_monitoring_mode()
        self._backend.set_monitoring_mode("production" if current == "recording" else "recording")
        # _on_backend_event below also redraws the key once the backend's
        # monitoring_mode_changed event arrives — for a RemoteBackend that's
        # a second network round trip away, so update it right away here
        # too rather than leaving the key stale until the event catches up.
        self._refresh_monitoring_mode()

    # --- backend events ---

    def _on_backend_event(self, event: str, data: dict) -> None:
        if event == "recording_status":
            if "track_name" in data:
                self.track_name = data["track_name"]
            if "phase" in data:
                self.phase = data["phase"]
                if self.phase == "idle":
                    self.track_name = None
                self.streamdeck.update_recording_page(
                    self.phase, self.video_check_phase, self.track_name, self.detected_instrument,
                )
                # update_recording_page blanks every key the idle layout
                # doesn't use whenever phase crosses the idle boundary —
                # including the monitor toggle (idx 3), which only exists
                # in the active layout — so re-paint it every time a phase
                # change might have just swapped layouts; connect()'s
                # initial _refresh_monitoring_mode() alone isn't enough
                # once a session has opened and closed at least once.
                self._refresh_monitoring_mode()
        elif event == "video_check_status":
            # RemoteServer never broadcasts the raw video_check_status a
            # server-side check emits (its result_path only means anything
            # on that machine). Over a Remote connection this event only
            # ever reaches us re-dispatched by RemoteBackend after it's
            # transferred the finished file and rewritten result_path to a
            # local copy (see RemoteBackend._on_raw_event's "video_check_
            # result" handling) — so by the time it's here, result_path is
            # always valid for this machine, local or remote alike.
            if "status" in data:
                self._log(data["status"])
            if "phase" in data:
                self.video_check_phase = data["phase"]
                self.streamdeck.update_recording_page(
                    self.phase, self.video_check_phase, self.track_name, self.detected_instrument,
                )
            if self.video_check_phase == "idle" and "result_path" in data and self._on_video_check_result:
                self._on_video_check_result(Path(data["result_path"]), bool(data.get("has_video")))
        elif event == "monitoring_mode_changed":
            # Fired by set_monitoring_mode() from any client (this deck's
            # own "m" key, the Tk UI's radio toggle, or a Remote client) —
            # keeps the physical key in sync regardless of who changed it.
            if "mode" in data:
                self.streamdeck.update_monitoring_mode(data["mode"])
        elif event == "auto_detect_status":
            # Purely reactive — see detected_instrument's own comment for
            # why nothing here ever calls start_auto_detect_instrument
            # itself. "listening"'s own `status` text (skipped-instrument
            # notes etc.) still goes to the log, same as before this
            # touchscreen display existed.
            phase = data.get("phase")
            if phase == "listening":
                if "status" in data:
                    self._log(data["status"])
                self.detected_instrument = "Detecting…"
            elif phase == "detected":
                label = data.get("label") or ""
                full_name = data.get("full_name") or ""
                self.detected_instrument = label.upper() if label else full_name
                self._log(f"StreamDeck: detected '{full_name}' ({label or 'no label'}).")
            elif phase == "stopped":
                self.detected_instrument = None
            self.streamdeck.update_recording_page(
                self.phase, self.video_check_phase, self.track_name, self.detected_instrument,
            )
