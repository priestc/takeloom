"""Shared physical Stream Deck driver for every recording context (Tk UI,
headless `takeloom server`, and the CLI) — one place for button layout,
key dispatch, and state, so the deck behaves identically no matter which
one is driving it.

This module touches only `Backend` and `StreamDeckController` — nothing
Tk-specific, nothing terminal-specific. A context supplies two hooks:

- `resolve_start_request()`: how to pick a project/instrument/track once
  Play is pressed to actually open a session (see identify_state below).
  The Tk UI answers from whatever's selected in its Setlist/Inspiration
  picker; the headless server and CLI answer from the last-used project/
  instrument plus the next untaken track.
- `on_video_check_result(path, has_video)`: how to hand off a finished
  video check — there's no Stream Deck button for starting one anymore
  (key index 3 is now the Live/Production monitor toggle, "m"), but a
  check started from the Tk UI or a Remote client still finishes here.
  The Tk UI opens a review dialog; headless/CLI open the OS default
  player directly (no GUI to pop a window in).

The backend owns everything session-shaped: a song reaching its natural end
auto-advances to the next one, and stop ends the session (see the recording
section of backend.py). Opening a session itself now goes through an
identify cycle first — Start Local/Start Streaming kicks off an auto-detect
scan (identify_state "identifying"), and only once it commits to an
instrument (identify_state "ready") does Play actually call Backend.start_
recording() — see _begin_identify/_redo_identify/_confirm_start below.
Every other key here just forwards straight to the backend.

Both hooks — and `log()` — may be called from a background thread (the
Stream Deck's own key-event thread, or a backend event callback); a
context whose hooks touch UI-toolkit state is responsible for marshalling
onto its own main thread itself, the same way it already does for any
other cross-thread backend call.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Callable

from .audio.pitch import TunerSmoother
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
        # Sub-state of an "idle" phase — see streamdeck_controller.py's
        # RECORDING_IDLE_BUTTONS/RECORDING_IDENTIFYING_BUTTONS/RECORDING_
        # IDENTIFIED_BUTTONS table comment. "idle": Start Local/Start
        # Streaming shown, nothing pressed yet. "identifying": one of those
        # was just pressed — _begin_identify() below kicked off a fresh
        # auto-detect scan and only Re-identify shows while it's listening.
        # "ready": auto-detect committed to an instrument — Play (and
        # Re-identify, in case it's wrong) show; pressing Play is what
        # actually calls Backend.start_recording(), which is also the only
        # thing that ever moves `phase` itself off "idle".
        self.identify_state = "idle"
        # Which idle button ("Start Local"=False, "Start Streaming"=True)
        # kicked off the current identify_state != "idle" cycle — read back
        # by _confirm_start() once Play is pressed. Meaningless while
        # identify_state == "idle".
        self._pending_streaming = False
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
        # next take, not merely because a session opened or closed. The
        # scan itself is only ever started by _begin_identify()/_redo_
        # identify() below (a Start button or Re-identify actually
        # pressed) — there's deliberately no "kick off a scan the instant
        # phase becomes idle" path anymore (that briefly existed in
        # headless server mode and caused a duplicate-scan storm on every
        # post-processing status update, not just genuine idle
        # transitions); identify_state's own "idle" sub-state is what
        # shows Start Local/Start Streaming until one is actually pressed.
        self.detected_instrument: str | None = None
        # The live tuner needle (see StreamDeckController._draw_tuner_
        # needle) — what Backend's "tuner_status" events last reported
        # (see _on_backend_event), smoothed via _tuner_smoother so it
        # doesn't visibly jitter between individual readings. Live for
        # the whole identify cycle ("identifying" *and* "ready" — Backend
        # keeps readings coming after detection too, via an ambient-
        # monitor tap, see backend.py's _attach_tuner_sink), cleared the
        # moment the cycle actually ends (Play opens a session) or starts
        # over (Re-identify) since a stale reading has nothing to do with
        # what's about to be played next. None until the first confident
        # reading arrives.
        self.tuner_note: str | None = None
        self.tuner_cents: float = 0.0
        self._tuner_smoother = TunerSmoother()
        self._events_subscribed = False

        # Physical Stream Deck key presses are handled off the deck's own
        # HID event thread, on this single worker: handle_key() makes
        # backend calls that can block for real (an inspiration-server
        # query, a backing-track download, hardware spin-up), and if that
        # ran on the HID thread the whole deck would freeze mid-press —
        # no further keys, no redraws — with nothing on it to say why.
        # One worker (not a thread per press) keeps presses ordered and
        # naturally serialized against the backend's own mutual exclusion.
        # See _dispatch_key / connect(). The CLI's own terminal-key loop
        # still calls handle_key directly — a blocking terminal is fine.
        self._key_queue: "queue.Queue[str | None]" = queue.Queue()
        threading.Thread(target=self._run_key_worker, daemon=True, name="deck-key-worker").start()

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
        otherwise presses go through _dispatch_key, which runs handle_key on
        this driver's worker thread so a slow backend call can't freeze the
        deck's HID event thread. Returns whether an actual Stream Deck
        connected."""
        self._subscribe_backend_events()
        try:
            device_id = self._backend.get_config().streamdeck_id
        except BackendError:
            device_id = ""
        if not device_id:
            self._log("Skipping StreamDeck initialization, as none are configured.")
            return False
        if not self.streamdeck.connect(key_callback or self._dispatch_key, device_id=device_id):
            return False
        self.streamdeck.use_recording_layout()
        self.streamdeck.update_recording_page(
            self.phase, self.video_check_phase, self.track_name, self.detected_instrument,
            self.identify_state, self.tuner_note, self.tuner_cents,
        )
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
        self.identify_state = "idle"
        self._pending_streaming = False
        self.tuner_note = None
        self._subscribe_backend_events()
        self.streamdeck.update_recording_page(
            self.phase, self.video_check_phase, self.track_name, self.detected_instrument,
            self.identify_state, self.tuner_note, self.tuner_cents,
        )
        self._refresh_monitoring_mode()

    def _refresh_monitoring_mode(self) -> None:
        try:
            self.streamdeck.update_monitoring_mode(self._backend.get_monitoring_mode())
        except BackendError:
            pass

    # --- key dispatch (the single canonical behavior for every context) ---

    def _dispatch_key(self, key: str) -> None:
        """The physical Stream Deck's key callback (see connect()). Runs on
        the deck's HID event thread and must return at once, so the actual
        work is handed to _run_key_worker instead of blocking here."""
        self._key_queue.put(key)

    def _run_key_worker(self) -> None:
        while True:
            key = self._key_queue.get()
            if key is None:
                return
            try:
                self.handle_key(key)
            except Exception as e:  # never let the worker thread die on a press
                self._log(f"StreamDeck: key '{key}' failed: {e}")

    def handle_key(self, key: str) -> None:
        try:
            if key == "r":
                if self.phase == "idle":
                    if self.identify_state == "idle":
                        self._begin_identify(streaming=False)
                elif self.phase == "waiting":
                    self._backend.unpause_recording()
                elif self.phase == "recording":
                    self.streamdeck.notify("Stopping session…")
                    self._backend.stop_recording()
            elif key == "s":
                if self.phase == "idle" and self.identify_state == "idle":
                    self._begin_identify(streaming=True)
            elif key == "i":
                if self.phase == "idle" and self.identify_state in ("identifying", "ready"):
                    self._redo_identify()
            elif key == "p":
                if self.phase == "idle" and self.identify_state == "ready":
                    self._confirm_start()
            elif key == "n":
                # No local phase pre-check — see "b"/"d" below for why:
                # Backend.next_track() already raises its own clear
                # BackendError ("No session in progress.") when there's
                # nothing to advance, caught below same as any other
                # failure, so gating on this driver's own (possibly
                # stale — see the "b" comment) cached self.phase would
                # only risk silently swallowing a press that the backend
                # itself would have handled correctly.
                self.streamdeck.notify("Loading next track…")
                self._backend.next_track()
            elif key == "d":
                # Same reasoning as "n" — Backend.redraw_current_track()
                # already raises its own clear BackendError ("Nothing is
                # currently loaded." / "...isn't a random draw...").
                self.streamdeck.notify("Finding another track…")
                self._backend.redraw_current_track()
            elif key == "b":
                # Deliberately no local `if self.phase == "recording":`
                # guard (there used to be one) — this driver's own
                # cached self.phase is only ever updated by whichever
                # backend events have actually arrived, and can end up
                # stale relative to the real session state (e.g. a
                # Remote connection that dropped and reconnected — see
                # CLAUDE.md's testing notes on the laptop sleeping/
                # dropping off the network mid-session — resets phase to
                # "idle" locally and only corrects itself once a *new*
                # event happens to arrive). A stale-but-wrong local guard
                # here meant a real, actively-playing take could make
                # Restart silently do nothing at all, no error, nothing
                # logged — exactly the "pressed it and nothing happened"
                # failure this driver's whole design otherwise goes out
                # of its way to avoid (see the module docstring's own
                # "ack presses instantly" principle). Backend.
                # restart_take() already raises its own clear
                # BackendError ("Not currently recording.") when there's
                # genuinely nothing to restart, which the except clause
                # below surfaces exactly like any other failure — so the
                # backend's own live, authoritative state is what
                # actually decides this now, not a local cache of it.
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
            # Surface it on the deck too, not just this log line — a press
            # that silently does nothing (the backend refused it, a server
            # was unreachable) otherwise just looks like a frozen deck.
            self._log(f"StreamDeck: {e}")
            self.streamdeck.notify(f"Couldn't: {e}", revert_after=5.0)

    def _begin_identify(self, streaming: bool) -> None:
        """Start Local/Start Streaming was just pressed — remember which
        one (read back by _confirm_start once Play is pressed), enter the
        "identifying" sub-state, and kick off a fresh auto-detect scan.
        `phase` itself stays "idle" throughout the whole identify cycle;
        only Play (_confirm_start) actually opens a session."""
        self._pending_streaming = streaming
        self.identify_state = "identifying"
        self.detected_instrument = "Detecting…"
        self.tuner_note = None
        self._tuner_smoother.reset()
        self.streamdeck.update_recording_page(
            self.phase, self.video_check_phase, self.track_name, self.detected_instrument,
            self.identify_state, self.tuner_note, self.tuner_cents,
        )
        self._backend.start_auto_detect_instrument()

    def _redo_identify(self) -> None:
        """Re-identify — available throughout "identifying"/"ready" so a
        stalled or wrong detection can be restarted without backing all
        the way out to Start Local/Start Streaming (which would also lose
        the streaming/local choice already made)."""
        self.identify_state = "identifying"
        self.detected_instrument = "Detecting…"
        self.tuner_note = None
        self._tuner_smoother.reset()
        self.streamdeck.update_recording_page(
            self.phase, self.video_check_phase, self.track_name, self.detected_instrument,
            self.identify_state, self.tuner_note, self.tuner_cents,
        )
        # Fire-and-forget the stop (a no-op if detection already committed
        # — "ready" means it has — since a finished scan already tore
        # itself down) then start a fresh one.
        self._backend.stop_auto_detect_instrument()
        self._backend.start_auto_detect_instrument()

    def _confirm_start(self) -> None:
        """Play, pressed once auto-detect has committed to an instrument —
        the moment the identify cycle actually ends and a session opens.
        Deliberately doesn't reset identify_state itself: _on_backend_event's
        "recording_status" handling above does that once phase actually
        leaves "idle", so a start_recording() failure here (still caught by
        handle_key's own try/except) leaves identify_state — and the deck's
        still-displayed "ready" layout — untouched, ready for the user to
        just press Play again rather than losing the detected instrument
        and having to redo the whole identify cycle."""
        self._start_recording(self._pending_streaming)

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
        # start_recording can take a beat — a filter slot has to query the
        # inspiration server for a match and may then download the backing
        # track — so say so, or Play just looks dead until "Loaded …".
        self.streamdeck.notify("Opening session…", revert_after=4.0)
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
            if "status" in data:
                # Includes post-processing progress — backend.py's
                # _process_session's own completion summary and vault.
                # sync_and_maybe_prune's sync messages are routed through
                # "recording_status" same as everything else here, tagged
                # phase "idle" since post-processing has no better phase
                # to report through. Without surfacing the text itself
                # too, headless `takeloom server` had no indication
                # whatsoever that splicing/syncing was still working in
                # the background after a session ended — same reasoning
                # video_check_status's handling below already applies to
                # its own status text.
                self._log(data["status"])
            if "track_name" in data:
                self.track_name = data["track_name"]
            if "phase" in data:
                self.phase = data["phase"]
                if self.phase == "idle":
                    self.track_name = None
                else:
                    # A session just actually opened — whether via Play
                    # finishing the identify cycle (the normal path;
                    # _confirm_start deliberately leaves this alone so a
                    # start failure doesn't lose "ready") or some other way
                    # entirely (e.g. a different Remote client called
                    # start_recording directly while this deck's own
                    # identify cycle was still in flight).
                    self.identify_state = "idle"
                    self.tuner_note = None
                self.streamdeck.update_recording_page(
                    self.phase, self.video_check_phase, self.track_name, self.detected_instrument,
                    self.identify_state, self.tuner_note, self.tuner_cents,
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
                    self.identify_state, self.tuner_note, self.tuner_cents,
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
            # A scan is only ever started by _begin_identify()/_redo_
            # identify() above (Start Local/Start Streaming or Re-identify
            # actually pressed) — this just reacts to whatever result
            # arrives. "listening"'s own `status` text (skipped-instrument
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
                if self.identify_state == "identifying":
                    self.identify_state = "ready"
                    # Every channel's stream just got torn down (see
                    # Backend.start_auto_detect_instrument), so no more
                    # tuner_status events are coming until the next scan —
                    # but deliberately leave tuner_note/tuner_cents as
                    # they are rather than clearing them: the needle
                    # should keep showing its last reading right up until
                    # Play actually opens a session (see _confirm_start)
                    # or Re-identify starts a fresh scan (_redo_identify),
                    # not disappear the instant detection locks in — that
                    # was confirmed as the wrong call after it shipped:
                    # it vanished before there was time to actually
                    # glance at it and finish tuning.
            elif phase == "stopped":
                self.detected_instrument = None
            self.streamdeck.update_recording_page(
                self.phase, self.video_check_phase, self.track_name, self.detected_instrument,
                self.identify_state, self.tuner_note, self.tuner_cents,
            )
        elif event == "tuner_status":
            # Meaningful throughout the whole identify cycle, not just
            # "identifying" — once auto-detect commits, Backend keeps the
            # needle live via _attach_tuner_sink (tapping the ambient
            # monitor it opens on the just-detected channel), so readings
            # keep arriving all through "ready" too, right up until Play
            # actually opens a session (see backend.py's on_channel_
            # detected/_attach_tuner_sink). Only "idle" — no identify
            # cycle in progress at all — means a reading has nothing left
            # to show it on (e.g. one straggling in from a scan that's
            # already been superseded by a session opening some other
            # way).
            if self.identify_state == "idle":
                return
            note = data.get("note")
            self.tuner_note = note
            self.tuner_cents = self._tuner_smoother.update(note, float(data.get("cents", 0.0)))
            self.streamdeck.update_recording_page(
                self.phase, self.video_check_phase, self.track_name, self.detected_instrument,
                self.identify_state, self.tuner_note, self.tuner_cents,
            )
