"""Backend abstraction: everything a UI tab needs, independent of whether the
data/hardware lives on this machine or a remote takeloom instance.

`LocalBackend` talks directly to local config, disk, and audio/video
hardware — this is the historical behavior of the UI tabs, just extracted
behind an interface. `RemoteBackend` (in `takeloom/remote/backend.py`) adapts
the same interface over the network to a `RemoteServer` running elsewhere,
which itself wraps its own `LocalBackend`.

Nothing in this module touches tkinter. Callers on the UI side are
responsible for running blocking calls on a background thread and
marshalling results back to the Tk thread (e.g. via `widget.after(0, ...)`).
Event callbacks registered via `on_event`/camera-preview frame callbacks may
be invoked from a non-UI thread for the same reason.
"""

from __future__ import annotations

import json
import random
import shutil
import socket
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .audio.filters import CompressorSettings
from .audio.scarlett2_direct_monitor import FOCUSRITE_DEVICE_NAME, set_channel_gain
from .config import DEFAULT_CONFIG_PATH, StudioConfig
from .project import Project, Setlist, TakeInfo, TrackEntry
from .utils import ensure_dir, timestamp_now, wall_timestamp


class BackendError(Exception):
    """Raised by any Backend method on failure. Message is safe to show to the user."""


# The breather between a song ending naturally and the auto-advanced next
# one starting to play — long enough to reset hands, short enough that the
# session keeps its momentum. The session capture rolls straight through it.
AUTO_ADVANCE_GAP_SECONDS = 2.0


@dataclass
class StartRecordingRequest:
    project_name: str
    instrument_name: str
    track_index: int


EventCallback = Callable[[str, dict], None]
FrameCallback = Callable[[bytes], None]


class PreviewSubscription:
    """Returned by open_camera_preview(); call close() to stop receiving frames."""

    def close(self) -> None:  # pragma: no cover - overridden by subclasses
        raise NotImplementedError


class Backend(ABC):
    """Interface every UI tab depends on (constructor-injected via AppState)."""

    @abstractmethod
    def hostname(self) -> str: ...

    def is_remote(self) -> bool:
        return False

    def close(self) -> None:
        pass

    # --- config ---

    @abstractmethod
    def get_config(self) -> StudioConfig: ...

    @abstractmethod
    def save_config(self, config: StudioConfig) -> None: ...

    # --- devices ---

    @abstractmethod
    def list_audio_devices(self) -> list[dict]: ...

    @abstractmethod
    def list_cameras(self) -> list[tuple[str, str]]: ...

    def list_streamdecks(self) -> list[tuple[str, str]]:
        """(serial_number, label) pairs for every physically attached Stream
        Deck. Concrete default (not abstract) since a Stream Deck is
        inherently local hardware — RemoteBackend has nothing meaningful to
        return and just inherits this empty-list default rather than
        proxying it over the wire, the same reasoning as get_levels()."""
        return []

    @abstractmethod
    def refresh_devices(self) -> None: ...

    # --- projects / setlists ---

    @abstractmethod
    def list_projects(self) -> list[str]: ...

    @abstractmethod
    def get_setlist(self, project_name: str) -> dict: ...

    @abstractmethod
    def save_setlist(self, project_name: str, setlist_data: dict) -> None: ...

    def next_untaken_track_index(
        self, project_name: str, instrument_name: str, start_index: int = 0,
    ) -> int | None:
        """Index of the first setlist track from start_index onward that
        doesn't already have a take for instrument_name, or None if every
        remaining track already has one. Pure setlist query on top of
        get_setlist() — concrete here (not per-subclass) since it needs no
        hardware access and works identically for Local and Remote.

        The single shared "what's next" primitive every recording-driving
        context (Tk UI's StreamDeck Next key, headless takeloom server,
        the CLI) uses instead of each reimplementing this search."""
        setlist = Setlist.from_dict(self.get_setlist(project_name))
        for i in range(start_index, len(setlist.tracks)):
            if setlist.tracks[i].get_take_for_instrument(instrument_name) is None:
                return i
        return None

    @abstractmethod
    def create_project(self, name: str) -> str:
        """Create a new, empty project. Returns its final (sanitized) name."""
        ...

    @abstractmethod
    def add_local_backing_track(
        self, project_name: str, source_path: str, track_name: str | None = None,
    ) -> dict:
        """Add a local audio or video file as a backing track (a video's
        audio stream is extracted for playback/mixing). `source_path` must
        be reachable on the machine the backend actually runs on —
        RemoteBackend refuses, since a path on the controlling client isn't
        reachable from the remote studio's disk."""
        ...

    @abstractmethod
    def add_youtube_backing_track(
        self, project_name: str, url: str, on_progress: Callable[[float | None, str], None] | None = None,
    ) -> dict:
        """Download a full YouTube video (via yt-dlp) and add it as a backing
        track; its audio stream is extracted for playback/mixing as needed.

        If given, on_progress(percent, message) reports live download
        progress — percent is 0-100 when known, else None with just a
        status message. RemoteBackend can't stream this live over its
        simple request/response RPC, so it calls on_progress once with a
        placeholder message instead."""
        ...

    @abstractmethod
    def add_inspiration_filter_slot(self, project_name: str, label: str, filter_criteria: dict) -> dict:
        """Add a standing setlist "slot" that draws a random track matching
        `filter_criteria` (e.g. {"artist": "Miles Davis"} or {"genre":
        "Rock"}) fresh each session, instead of one fixed song — see
        TrackEntry's docstring and backend.py's _resolve_filter_slot_for_
        session. `label` is the slot's display name in the Setlist list.
        This is the only way a project's setlist grows an inspiration-
        sourced entry now — see FilterCriteriaFields/EditFilterDialog. The
        entry's duration_seconds is set to the average across every
        currently-matching track (see inspiration.average_duration),
        since the slot has no single fixed song of its own."""
        ...

    # --- sessions (browse/correct past recordings) ---

    @abstractmethod
    def list_sessions(self) -> list[dict]:
        """Every past session found under the vault's sessions/ directory
        (see vault.vault_session_dir), newest first: each dict has
        `session_dir` (opaque id — pass back to the other session methods),
        `date` (session_log.json's own wall_time, not derived from the
        directory name, which is filename-sanitized and thus lossy),
        `project`, `instrument`, and `track_names` (deduplicated, from the
        session's logged events). Only sessions still present on local
        disk — one already pruned to a remote-only vault (session_vault_
        mode "remote", see vault.sync_and_maybe_prune) won't show up here."""
        ...

    @abstractmethod
    def get_session_detail(self, session_dir: str) -> dict:
        """Full session_log.json contents for `session_dir` (a `session_dir`
        value from list_sessions()), plus, for each track name it touched,
        whatever take (if any) `preferred_takes` currently holds for it
        under the session's own logged instrument — so the UI can show
        "here's what's currently filed" before offering to reassign it.
        Each track's entry also reports whether it was an inspiration
        filter-slot draw (session_log.json's filter_slot_draws) — those
        aren't offered for reassign_take (see its docstring)."""
        ...

    @abstractmethod
    def correct_session_instrument(self, session_dir: str, new_instrument: str) -> None:
        """Fix the historical record alone: rewrite session_log.json's
        instrument/instrument_full_name fields (pulled from `new_instrument`
        in the current StudioConfig). Does not touch any take file,
        setlist.json, the shared inspiration-take index, or the session
        directory's own name (its instrument suffix is cosmetic — nothing
        reads it back out) — see reassign_take for the take-file side, a
        separate and more consequential action. Independent of
        reassign_take: calling this first does not change what
        reassign_take treats as the take's old instrument, since that's
        passed in explicitly rather than re-read from this same field."""
        ...

    @abstractmethod
    def reassign_take(self, session_dir: str, track_name: str, old_instrument: str, new_instrument: str) -> None:
        """Re-file one specific take — the one currently sitting in
        `track_name`'s preferred_takes under `old_instrument` — under
        `new_instrument` instead: renames the take file(s) on disk, and
        re-keys it in the project's setlist.json (and, for an inspiration-
        sourced track, the shared vault-wide inspiration_takes.json index
        too). `old_instrument` is passed explicitly (typically whatever
        get_session_detail's `current_take` reported) rather than re-read
        from session_dir's session_log.json, so this gives the right
        answer regardless of whether correct_session_instrument has
        already been called on the same session. Raises BackendError if
        `track_name` isn't one of session_dir's tracks, if it was an
        inspiration filter-slot draw (no reliable stored link from an old
        session back to exactly which shared-index entry it produced —
        see the Sessions tab's docs), or if there's no take currently
        filed under `old_instrument` to reassign."""
        ...

    # --- inspiration ---

    @abstractmethod
    def search_inspiration_artists(self, partial: str) -> list[str]:
        """Autocomplete suggestions for an inspiration filter's Artist
        field, from the inspiration server's autocomplete endpoint (see
        docs/inspiration-server-autocomplete-api.md). Returns [] rather
        than raising on any failure — this fires on every keystroke, so a
        slow/unreachable server should just mean no suggestions, not an
        error dialog interrupting typing."""
        ...

    @abstractmethod
    def search_inspiration_by_filter(self, filter_criteria: dict) -> list[dict]:
        """Every inspiration-server track matching `filter_criteria` (same
        shape as an inspiration filter slot's inspiration_filter — artist/
        genre/year_min/year_max/duration_min/duration_max) — backs the
        setlist's "Show tracks..." context menu action, so an operator can
        see exactly what a filter slot might draw before recording."""
        ...

    # --- recording ---
    #
    # All recording happens inside a session: one continuous audio stream
    # (and, with a camera, one continuous video) runs from the moment
    # recording starts until it's explicitly stopped. The controls below
    # only steer backing-track playback and append events to the session
    # log while that stream runs; actual takes are clipped out of the
    # continuous recording afterward by replaying the log — see
    # processing/splicer.py. Nothing heavy (file finalizing, muxing,
    # setlist writes) happens while the session is live.

    @abstractmethod
    def start_recording(self, req: StartRecordingRequest) -> None:
        """Load req's track, cued at 0:00 ("waiting" phase) — beginning a
        session for req's project/instrument first if none is active yet.
        Playback (and thus the take segment) starts on unpause_recording()."""
        ...

    @abstractmethod
    def unpause_recording(self) -> None:
        """Start the cued track's backing playback — logs record_start and
        enters the "recording" phase."""
        ...

    @abstractmethod
    def stop_recording(self) -> None:
        """Stop recording = end the session (see end_session()). A song cut
        off mid-play never becomes a take unless it already ran long enough
        to keep (see processing/splicer.py). No-op when no session is
        active."""
        ...

    @abstractmethod
    def restart_take(self) -> None:
        """Send the playing backing track back to 0:00 — logs back_to_start.
        The take still completes if this play-through reaches the natural
        end."""
        ...

    @abstractmethod
    def next_track(self) -> None:
        """Skip to the next setlist track that still needs a take for the
        session's instrument, and start playing it immediately. The
        in-progress song, if any, is logged as skipped (no take). Also what
        the backend itself does automatically when a song plays to its
        natural end."""
        ...

    @abstractmethod
    def redraw_current_track(self) -> None:
        """Replace the currently loaded track with a different random draw
        from the same setlist "inspiration filter" slot (see TrackEntry's
        docstring in project.py) — unlike next_track(), this stays at the
        same setlist position rather than advancing to the next one. The
        in-progress song, if any, is logged as skipped (no take), same as
        next_track(). Raises BackendError if nothing's loaded or the
        current track isn't a filter slot's draw — callers driving this
        from a Stream Deck key just log that rather than treating it as
        fatal (see recording_driver.py)."""
        ...

    @abstractmethod
    def is_recording(self) -> bool: ...

    def get_levels(self) -> tuple[float, float]:
        """Returns (instrument_peak, backing_peak), each 0.0-1.0, for the
        Record tab's VU meters. Not every backend can report this cheaply
        (e.g. RemoteBackend, absent a dedicated streaming RPC); the default
        is silence rather than an error."""
        return (0.0, 0.0)

    @abstractmethod
    def adjust_backing_volume(self, delta: int) -> None: ...

    @abstractmethod
    def adjust_takes_volume(self, delta: int) -> None: ...

    @abstractmethod
    def adjust_instrument_volume(self, delta: int) -> None: ...

    # --- audio filters (compressor now, more later) ---

    @abstractmethod
    def get_compressor_settings(self) -> dict:
        """Current compressor settings (enabled + threshold/ratio/attack/
        release/makeup gain), applied to the instrument input before it's
        recorded or monitored."""
        ...

    @abstractmethod
    def set_compressor_settings(self, settings: dict) -> None:
        """Update compressor settings — persisted for next time, and applied
        immediately to whatever recording/video-check/session engine is
        currently running (mirrors adjust_backing_volume/adjust_takes_volume)."""
        ...

    # --- live monitoring mode (Record page headphone mix) ---

    @abstractmethod
    def get_monitoring_mode(self) -> str:
        """Current live monitoring mode for the Record page's headphone
        mix during start_recording()/begin_session() — one of:

        - "production": the headphones hear exactly what's about to be
          written to the take's produced video (backing + other takes +
          the processed instrument, all mixed together) — a direct preview
          of the final result, at the cost of the software mix's small
          round-trip latency.
        - "recording": the instrument is left out of the software mix
          entirely (only backing/other takes play through headphones),
          relying on the audio interface's own zero-latency hardware
          direct monitor for the instrument itself — used while actually
          laying down a take, where latency matters more than hearing the
          finished blend.

        Purely a live toggle — not persisted, and never applied to Video
        Check (always forced to "recording") or the Latency tab's test
        (always monitors the instrument)."""
        ...

    @abstractmethod
    def set_monitoring_mode(self, mode: str) -> None: ...

    @abstractmethod
    def restart_monitoring(self) -> bool:
        """Point the ambient monitor-only stream (see start_monitoring()) at
        whatever config.last_selected_instrument is now — call after
        changing that (e.g. the Record page's instrument dropdown) so live
        listening follows the switch immediately rather than only at next
        startup/resume. A no-op, not an error, if a real take/session/
        video-check/latency test currently holds the hardware, or nothing
        actually changed. Returns whether a monitor stream is open
        afterward."""
        ...

    @abstractmethod
    def on_event(self, callback: EventCallback) -> None: ...

    @abstractmethod
    def off_event(self, callback: EventCallback) -> None: ...

    # --- camera preview ---

    @abstractmethod
    def open_camera_preview(self, on_frame: FrameCallback) -> PreviewSubscription: ...

    # --- camera latency test (local-only; RemoteBackend refuses) ---

    @abstractmethod
    def start_latency_test(self, instrument_name: str, camera_device: str, play_metronome: bool = True) -> None: ...

    @abstractmethod
    def stop_latency_test(self) -> None: ...

    # --- video check (local-only; RemoteBackend refuses) ---

    @abstractmethod
    def start_video_check(self, req: StartRecordingRequest) -> None:
        """Impromptu, throwaway recording against the currently selected
        backing track (same track/instrument selection as start_recording),
        for the performer to verify mic/camera/levels are set up correctly.
        Never touches the project's completed_takes_dir or setlist. Always
        runs in Recording Monitoring (zero-latency instrument passthrough
        via the audio interface's own hardware direct monitor), regardless
        of the current monitoring_mode setting — it's meant to be played
        the same way a real take would be."""
        ...

    @abstractmethod
    def stop_video_check(self) -> None: ...

    # --- session lifecycle ---

    @abstractmethod
    def begin_session(self, project_name: str, instrument_name: str) -> None:
        """Open the session explicitly, with no track loaded yet — the
        continuous audio (and video) capture plus the event log start here.
        start_recording() calls this itself when no session is active, so
        most contexts never need it directly; the CLI's `start-session`
        command still uses it."""
        ...

    @abstractmethod
    def end_session(self) -> None:
        """Close the session: finalize the continuous recording, save the
        session log, then (in the background) replay the log to clip
        completed takes — and their videos — out of it. stop_recording()
        is the usual way here."""
        ...

    def is_session_active(self) -> bool:
        """Whether a session is currently open. Concrete default (not
        abstract) — always False for a backend that can't know (RemoteBackend
        inherits this unchanged), same reasoning as list_streamdecks()."""
        return False


class _CameraPreviewManager:
    """Owns the single physical camera's capture loop and fans JPEG frames out
    to any number of subscribers. Paused/resumed around exclusive ffmpeg
    access during recording (mirrors RecordFrame's old _stop_preview/
    _start_preview dance, just headless)."""

    def __init__(
        self,
        get_camera_device: Callable[[], str],
        fps: float = 10.0,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._get_camera_device = get_camera_device
        self._fps = fps
        self._on_error = on_error
        self._lock = threading.Lock()
        self._subscribers: list[FrameCallback] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._paused = False

    def subscribe(self, on_frame: FrameCallback) -> PreviewSubscription:
        with self._lock:
            self._subscribers.append(on_frame)
            if self._thread is None and not self._paused:
                self._start_thread_locked()
        return _ManagerSubscription(self, on_frame)

    def _unsubscribe(self, on_frame: FrameCallback) -> None:
        with self._lock:
            if on_frame in self._subscribers:
                self._subscribers.remove(on_frame)
            if not self._subscribers:
                self._stop_thread_locked()

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            self._stop_thread_locked()

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            if self._subscribers and self._thread is None:
                self._start_thread_locked()

    def restart(self) -> None:
        """Force the capture thread to close and reopen the camera device —
        used by refresh_devices() for a camera that wasn't plugged in yet
        when a subscriber first opened the preview (in which case the
        capture thread would have opened, immediately failed, and exited,
        leaving nothing to retry the open on its own)."""
        with self._lock:
            self._stop_thread_locked()
            if self._subscribers and not self._paused:
                self._start_thread_locked()

    def push_external_frame(self, jpeg: bytes) -> None:
        """Fan a frame from some other source (currently: VideoRecorder's
        tee'd preview stream while it holds the camera exclusively for a real
        take/video check) out to the same subscribers as the normal capture
        loop — so the Record tab's live feed keeps running throughout a
        recording instead of freezing while this manager's own thread is
        paused for ffmpeg's exclusive access."""
        with self._lock:
            subscribers = list(self._subscribers)
        for cb in subscribers:
            try:
                cb(jpeg)
            except Exception:
                pass

    def _start_thread_locked(self) -> None:
        device = self._get_camera_device()
        if not device:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, args=(device,), daemon=True)
        self._thread.start()

    def _stop_thread_locked(self) -> None:
        self._stop_event.set()
        self._thread = None  # the running thread notices stop_event and exits/releases the camera itself

    def _report_error(self, message: str) -> None:
        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:
                pass

    def _run(self, device: str) -> None:
        try:
            import cv2
        except ImportError:
            self._report_error("Camera preview requires opencv-python (cv2), which is not installed.")
            return
        index = int(device) if device.isdigit() else device
        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            cap.release()
            self._report_error(
                f"Could not open camera device '{device}'. It may be disconnected, in use by "
                "another app, or lacking camera permission. Reconnect/grant access, then use "
                "↻ Refresh Devices."
            )
            return
        interval = 1.0 / self._fps
        target_width = 320
        # Some failure modes (e.g. macOS denying camera permission to this
        # process) leave isOpened() True but every read() failing forever —
        # treat a long unbroken run of failed reads the same as a failed open.
        consecutive_failures = 0
        max_consecutive_failures = round(self._fps * 3)  # ~3 seconds of nothing but failures
        ever_succeeded = False
        try:
            while not self._stop_event.is_set():
                ok, frame = cap.read()
                if ok:
                    ever_succeeded = True
                    consecutive_failures = 0
                    h, w = frame.shape[:2]
                    new_h = max(1, int(target_width * h / w))
                    frame = cv2.resize(frame, (target_width, new_h))
                    # cv2.imencode expects BGR (what cap.read() already returns) — no color
                    # conversion here, or the encoded JPEG's colors come out swapped.
                    ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if ok2:
                        jpeg = buf.tobytes()
                        with self._lock:
                            subscribers = list(self._subscribers)
                        for cb in subscribers:
                            try:
                                cb(jpeg)
                            except Exception:
                                pass
                else:
                    consecutive_failures += 1
                    if not ever_succeeded and consecutive_failures >= max_consecutive_failures:
                        self._report_error(
                            f"Camera device '{device}' opened but never produced a frame — "
                            "check camera permissions for this app, then use ↻ Refresh Devices."
                        )
                        return
                self._stop_event.wait(interval)
        finally:
            cap.release()


class _ManagerSubscription(PreviewSubscription):
    def __init__(self, manager: _CameraPreviewManager, callback: FrameCallback) -> None:
        self._manager = manager
        self._callback = callback
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._manager._unsubscribe(self._callback)


@dataclass
class _SessionEvent:
    """One entry in a session's session_log.json — see _ActiveSession.
    `frame` (a position on the session recording's timeline, in samples) and
    the track fields are what post-session take splicing runs on — see
    processing/splicer.py for the event vocabulary and completion rules."""
    timestamp: float  # seconds since session start
    wall_time: str
    event_type: str  # session_start | track_loaded | record_start | back_to_start | song_end | track_skipped | song_stopped | session_end
    details: str = ""
    frame: int | None = None
    track_index: int | None = None
    track_name: str = ""

    def to_dict(self) -> dict:
        d = {
            "timestamp": self.timestamp, "wall_time": self.wall_time,
            "event_type": self.event_type, "details": self.details,
        }
        if self.frame is not None:
            d["frame"] = self.frame
        if self.track_index is not None:
            d["track_index"] = self.track_index
        if self.track_name:
            d["track_name"] = self.track_name
        return d


@dataclass
class _ActiveSession:
    """The recording: one continuous audio stream (session.flac) and, with a
    camera, one continuous video, running from begin_session() until
    end_session(), plus the JSON event log that post-session take splicing
    replays. Tracks are loaded into / played through this one engine; nothing
    is finalized until the session ends. See the Backend recording-section
    comment."""
    engine: object
    project: Project
    inst: object
    session_dir: Path
    session_start: float  # timestamp_now() at begin_session()
    musician: str
    instrument_full_name: str
    studio_name: str
    studio_location: str
    session_flac: Path
    session_video: Path
    events: list = field(default_factory=list)
    video_recorder: object | None = None
    session_video_raw: Path | None = None
    session_mix_flac: Path | None = None
    stream_feeder: object | None = None  # LiveAudioFeeder, when streaming to YouTube is on for this session
    youtube_broadcast_id: str | None = None  # set when a titled broadcast was created/bound for this session
    video_start_wall_time: str | None = None
    video_start_track_name: str = ""
    # Position on the session-audio timeline when the mix/video recording
    # started — the offset splicing needs to map take frames onto the
    # session video's timeline.
    mix_start_frame: int = 0
    # The track currently loaded in the mixer (None between "no more
    # tracks" and session end) and whether its backing is playing — the
    # session's phase is "recording" while playing, else "waiting".
    current_track: TrackEntry | None = None
    current_track_index: int | None = None
    playing: bool = False
    # Setlist indices that completed a take *during this session* — the
    # setlist itself isn't updated until post-processing, so auto-advance
    # has to remember these itself to not offer the same song twice. A
    # filter slot's own index is added here the moment it's resolved
    # (see _resolve_filter_slot_for_session), not when a take completes —
    # otherwise it would keep getting redrawn every auto-advance cycle
    # within this same session, since its own preferred_takes never gets
    # a take (the take belongs to whatever song got drawn, which is never
    # itself added to the setlist — see _resolve_filter_slot).
    completed_track_indices: set = field(default_factory=set)
    # filter-slot index -> the TrackEntry drawn for it this session (never
    # added to project.setlist.tracks — see _resolve_filter_slot). Session-
    # only cache: a filter slot revisited later in the same session (e.g. a
    # manual reselect) gets the same resolved track back rather than a
    # fresh random draw each time.
    resolved_filter_picks: dict = field(default_factory=dict)


@dataclass
class _ActiveLatencyTest:
    engine: object
    video_recorder: object
    metronome_wav: Path
    take_path: Path
    video_raw: Path
    mix_flac: Path
    final_video: Path
    camera_paired_with_preview: bool  # True if this test's camera is the one open_camera_preview streams


@dataclass
class _ActiveVideoCheck:
    engine: object
    video_recorder: object | None
    take_path: Path
    video_raw: Path | None
    mix_flac: Path | None
    final_video: Path | None


@dataclass
class _ActiveMonitor:
    """A live-listening-only audio stream — no recorder/session attached,
    just the instrument audible in the headphones. Opened by
    start_monitoring() for config.last_selected_instrument as soon as the
    backend starts, so Production/Recording monitoring mode (and the
    Instrument Volume dial) has something to act on before Record is ever
    pressed. Superseded (see _close_active_monitor()/start_recording()) the
    moment anything else needs the audio hardware."""
    engine: object
    inst: object


class LocalBackend(Backend):
    """Direct local implementation — talks to this machine's config, disk,
    and audio/video hardware. Historical RecordFrame behavior, unchanged."""

    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self._config_path = config_path
        self._event_callbacks: list[EventCallback] = []
        self._record_lock = threading.Lock()
        self._active_latency_test: _ActiveLatencyTest | None = None
        self._active_video_check: _ActiveVideoCheck | None = None
        self._active_session: _ActiveSession | None = None
        self._active_monitor: _ActiveMonitor | None = None
        self._processing_thread: threading.Thread | None = None
        # Manual live-monitoring toggle for the Record page (see
        # get_monitoring_mode/set_monitoring_mode) — not persisted, always
        # starts each launch in "production" (hear the full produced mix).
        # Video Check ignores this entirely and always runs "recording".
        self._monitoring_mode: str = "production"
        self._preview = _CameraPreviewManager(self._current_camera_device, on_error=self._on_preview_error)
        # "Sticky" mixer levels: once the operator nudges backing/takes volume,
        # that level carries forward to every track loaded afterward (like a
        # mixing-console fader), instead of each track reverting to its own
        # saved default. Seeded from — and persisted back to — StudioConfig, so
        # a fresh app launch starts from last session's level rather than
        # jumping to whatever full volume an untouched track happened to save.
        config = self.get_config()
        self._backing_volume: int = config.last_backing_volume
        self._takes_volume: int = config.last_takes_volume
        self._instrument_volume: int = config.last_instrument_volume
        self._compressor_settings = CompressorSettings(
            enabled=config.compressor_enabled,
            threshold_db=config.compressor_threshold_db,
            ratio=config.compressor_ratio,
            attack_ms=config.compressor_attack_ms,
            release_ms=config.compressor_release_ms,
            makeup_gain_db=config.compressor_makeup_db,
        )

    def _save_last_volumes(self) -> None:
        config = self.get_config()
        config.last_backing_volume = self._backing_volume
        config.last_takes_volume = self._takes_volume
        config.last_instrument_volume = self._instrument_volume
        config.save(self._config_path)

    def _save_compressor_settings(self) -> None:
        config = self.get_config()
        s = self._compressor_settings
        config.compressor_enabled = s.enabled
        config.compressor_threshold_db = s.threshold_db
        config.compressor_ratio = s.ratio
        config.compressor_attack_ms = s.attack_ms
        config.compressor_release_ms = s.release_ms
        config.compressor_makeup_db = s.makeup_gain_db
        config.save(self._config_path)

    def _get_active_engine(self):
        """Whichever AudioEngine is currently live, if any — used to apply a
        settings change (e.g. the compressor) immediately rather than only on
        the next recording/video-check/session start."""
        if self._active_session is not None:
            return self._active_session.engine
        if self._active_video_check is not None:
            return self._active_video_check.engine
        if self._active_latency_test is not None:
            return self._active_latency_test.engine
        if self._active_monitor is not None:
            return self._active_monitor.engine
        return None

    def _get_active_engine_and_inst(self):
        """Like _get_active_engine(), but also returns the Instrument it's
        currently open for (None if there isn't one, e.g. mid Video Check)
        — needed by anything that must resolve the instrument's InputLabel
        (hardware direct monitor, live monitoring mode). Checked in the same
        priority order: an actual take always wins over the ambient
        monitor-only stream."""
        if self._active_session is not None:
            return self._active_session.engine, self._active_session.inst
        if self._active_monitor is not None:
            return self._active_monitor.engine, self._active_monitor.inst
        return None, None

    def hostname(self) -> str:
        return socket.gethostname()

    def ip_address(self) -> str:
        """Best-effort numeric LAN IP, useful alongside hostname() since
        mDNS names like 'Mac.local' don't always resolve for every client."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                # UDP "connect" sends nothing on the wire; it just asks the
                # OS which local interface/address would be used to reach
                # this destination, which is the LAN IP we want.
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            except OSError:
                return "unknown"

    def _current_camera_device(self) -> str:
        return self.get_config().camera_device

    def _on_preview_error(self, message: str) -> None:
        self._emit("preview_error", {"message": message})

    # --- config ---

    def get_config(self) -> StudioConfig:
        return StudioConfig.load(self._config_path)

    def save_config(self, config: StudioConfig) -> None:
        errors = config.validate()
        if errors:
            raise BackendError("\n".join(errors))
        config.save(self._config_path)

    # --- devices ---

    def list_audio_devices(self) -> list[dict]:
        try:
            import sounddevice as sd
            return list(sd.query_devices())
        except Exception:
            return []

    def list_cameras(self) -> list[tuple[str, str]]:
        from .video.devices import list_cameras
        try:
            return list_cameras()
        except Exception:
            return []

    def list_streamdecks(self) -> list[tuple[str, str]]:
        from .streamdeck_controller import list_streamdecks
        try:
            return list_streamdecks()
        except Exception:
            return []

    def refresh_devices(self) -> None:
        """Re-scan hardware for the case the UI was launched (or a remote
        connection made) before the camera/audio interface was plugged in.

        list_cameras() already shells out to ffmpeg fresh each call, so it
        always sees current hardware. Audio is different: PortAudio snapshots
        its device list once, at first use, so a plugged-in-later interface
        stays invisible to sd.query_devices() until PortAudio is
        re-initialized. The camera preview also needs a nudge of its own —
        if the camera wasn't present when the preview was first opened, its
        capture thread will have opened, failed, and exited for good.
        """
        try:
            import sounddevice as sd
            sd._terminate()
            sd._initialize()
        except Exception:
            pass
        self._preview.restart()

    # --- projects / setlists ---

    def list_projects(self) -> list[str]:
        config = self.get_config()
        return [p.stem for p in Project.list_projects(Path(config.projects_dir))]

    def _open_project(self, project_name: str) -> Project:
        config = self.get_config()
        projects = Project.list_projects(Path(config.projects_dir))
        path = next((p for p in projects if p.stem == project_name), None)
        if path is None:
            raise BackendError(f"Project '{project_name}' not found.")
        return Project.open(path, Path(config.session_vault_path))

    def get_setlist(self, project_name: str) -> dict:
        return self._open_project(project_name).setlist.to_dict()

    def save_setlist(self, project_name: str, setlist_data: dict) -> None:
        project = self._open_project(project_name)
        project.setlist = Setlist.from_dict(setlist_data)
        project.save_setlist()

    def create_project(self, name: str) -> str:
        config = self.get_config()
        projects_dir = Path(config.projects_dir)
        from .utils import sanitize_filename
        safe_name = sanitize_filename(name)
        if not safe_name:
            raise BackendError("Enter a project name.")
        if (projects_dir / f"{safe_name}.json").exists():
            raise BackendError(f"A project named '{safe_name}' already exists.")
        project = Project.create_new(projects_dir, name, Path(config.session_vault_path))
        return project.name

    def add_local_backing_track(
        self, project_name: str, source_path: str, track_name: str | None = None,
    ) -> dict:
        project = self._open_project(project_name)
        src = Path(source_path)
        if not src.exists():
            raise BackendError(f"File not found: {source_path}")
        from .audio.formats import get_duration
        try:
            duration = get_duration(src)
        except Exception:
            duration = 0.0
        entry = project.add_backing_track(src, track_name=track_name, duration_seconds=duration)
        return entry.to_dict()

    def add_youtube_backing_track(
        self, project_name: str, url: str, on_progress: Callable[[float | None, str], None] | None = None,
    ) -> dict:
        project = self._open_project(project_name)
        from .youtube import YouTubeDownloadError, download_youtube_video
        try:
            dest_path, title, duration = download_youtube_video(
                url, project.backing_tracks_dir, on_progress=on_progress,
            )
        except YouTubeDownloadError as e:
            raise BackendError(str(e)) from e
        entry = project.add_backing_track(dest_path, track_name=title, duration_seconds=duration, source="youtube")
        return entry.to_dict()

    def add_inspiration_filter_slot(self, project_name: str, label: str, filter_criteria: dict) -> dict:
        if not filter_criteria:
            raise BackendError("Enter at least one filter field (artist and/or genre).")
        project = self._open_project(project_name)
        from .inspiration import InspirationError, average_duration, search_tracks_by_filter
        try:
            matches = search_tracks_by_filter(self.get_config(), filter_criteria)
            duration = average_duration(matches)
        except InspirationError:
            # Still worth adding the slot even if the inspiration server
            # is briefly unreachable — just without a duration estimate
            # yet (0.0, same as before this was tracked at all).
            duration = 0.0
        entry = project.add_inspiration_filter_slot(label, filter_criteria, duration_seconds=duration)
        return entry.to_dict()

    # --- sessions (browse/correct past recordings) ---

    def _session_dir_path(self, session_dir: str) -> Path:
        from .vault import vault_root
        path = vault_root(self.get_config()) / "sessions" / session_dir
        if not path.is_dir():
            raise BackendError(f"Session '{session_dir}' not found.")
        return path

    def _read_session_log(self, session_dir: str) -> tuple[Path, dict]:
        log_path = self._session_dir_path(session_dir) / "session_log.json"
        if not log_path.exists():
            raise BackendError(f"Session '{session_dir}' has no session_log.json.")
        try:
            data = json.loads(log_path.read_text())
        except json.JSONDecodeError as e:
            raise BackendError(f"Session '{session_dir}' has a corrupt session_log.json: {e}") from e
        return log_path, data

    def list_sessions(self) -> list[dict]:
        from .vault import vault_root
        sessions_dir = vault_root(self.get_config()) / "sessions"
        if not sessions_dir.exists():
            return []
        results = []
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            log_path = session_dir / "session_log.json"
            if not log_path.exists():
                continue
            try:
                data = json.loads(log_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            events = data.get("events", [])
            track_names = []
            for e in events:
                name = e.get("track_name")
                if name and name not in track_names:
                    track_names.append(name)
            results.append({
                "session_dir": session_dir.name,
                # events[0]'s wall_time (real, human-typed timestamp) rather
                # than parsed back out of the directory name, which is
                # filename-sanitized and thus lossy/ambiguous.
                "date": events[0]["wall_time"] if events else "",
                "project": data.get("project", ""),
                "instrument": data.get("instrument", ""),
                "track_names": track_names,
            })
        results.sort(key=lambda s: s["date"], reverse=True)
        return results

    def get_session_detail(self, session_dir: str) -> dict:
        _, data = self._read_session_log(session_dir)
        instrument = data.get("instrument", "")
        project_name = data.get("project", "")
        filter_slot_indices = {int(k) for k in data.get("filter_slot_draws", {})}

        track_names: list[str] = []
        track_index_by_name: dict[str, int] = {}
        for e in data.get("events", []):
            name = e.get("track_name")
            if name and name not in track_names:
                track_names.append(name)
                track_index_by_name[name] = e.get("track_index")

        try:
            project = self._open_project(project_name)
        except BackendError:
            project = None  # project may have been deleted/renamed since

        tracks = []
        for name in track_names:
            is_filter_draw = track_index_by_name.get(name) in filter_slot_indices
            current_take = None
            if project is not None and not is_filter_draw:
                entry = next((t for t in project.setlist.tracks if t.name == name), None)
                if entry is not None:
                    take = entry.get_take_for_instrument(instrument)
                    if take is not None:
                        current_take = asdict(take)
            tracks.append({"track_name": name, "is_filter_draw": is_filter_draw, "current_take": current_take})

        return {**data, "session_dir": session_dir, "tracks": tracks}

    def correct_session_instrument(self, session_dir: str, new_instrument: str) -> None:
        config = self.get_config()
        inst = config.get_instrument(new_instrument)
        if inst is None:
            raise BackendError(f"Instrument '{new_instrument}' not found.")
        log_path, data = self._read_session_log(session_dir)
        # Deliberately doesn't also rename the session directory: its
        # instrument suffix is cosmetic only (nothing reads it back out —
        # every actual lookup goes through session_log.json's own
        # "instrument" field, corrected below), and string-surgery on a
        # real directory path for a cosmetic fix isn't worth the risk of
        # getting it wrong.
        data["instrument"] = inst.name
        data["instrument_full_name"] = inst.full_name
        log_path.write_text(json.dumps(data, indent=2))

    def reassign_take(self, session_dir: str, track_name: str, old_instrument: str, new_instrument: str) -> None:
        config = self.get_config()
        new_inst = config.get_instrument(new_instrument)
        if new_inst is None:
            raise BackendError(f"Instrument '{new_instrument}' not found.")
        _, data = self._read_session_log(session_dir)
        filter_slot_indices = {int(k) for k in data.get("filter_slot_draws", {})}

        track_index = None
        for e in data.get("events", []):
            if e.get("track_name") == track_name:
                track_index = e.get("track_index")
                break
        if track_index is None:
            raise BackendError(f"Track '{track_name}' wasn't touched by session '{session_dir}'.")
        if track_index in filter_slot_indices:
            raise BackendError(
                f"'{track_name}' was drawn from an inspiration filter slot — there's no reliable "
                "record of exactly which shared take it produced, so it can't be reassigned here."
            )

        project = self._open_project(data.get("project", ""))
        entry = next((t for t in project.setlist.tracks if t.name == track_name), None)
        if entry is None:
            raise BackendError(f"Track '{track_name}' no longer exists in project '{project.name}'.")
        take = entry.get_take_for_instrument(old_instrument)
        if take is None:
            raise BackendError(f"No take is currently filed under '{old_instrument}' for '{track_name}'.")

        from .utils import next_take_number, take_filename
        old_stem = Path(take.filename).stem
        old_audio_path = project.completed_takes_dir / take.filename
        old_video_path = project.completed_takes_dir / f"{old_stem}.mp4"
        ext = Path(take.filename).suffix.lstrip(".") or "flac"

        new_take_number = next_take_number(project.completed_takes_dir, track_name, new_inst.name)
        new_filename = take_filename(
            track_name, new_inst.name, new_take_number, entry.source_label(), entry.backing_track, ext,
        )
        new_stem = Path(new_filename).stem
        new_audio_path = project.completed_takes_dir / new_filename
        new_video_path = project.completed_takes_dir / f"{new_stem}.mp4"

        if old_audio_path.exists():
            shutil.move(str(old_audio_path), str(new_audio_path))
        has_video = take.has_video and old_video_path.exists()
        if has_video:
            shutil.move(str(old_video_path), str(new_video_path))

        new_take = TakeInfo(
            instrument=new_inst.name, take_number=new_take_number, filename=new_filename,
            volume=take.volume, has_video=has_video,
        )
        del entry.preferred_takes[old_instrument]
        entry.set_preferred_take(new_inst.name, new_take)
        project.save_setlist()

        if entry.inspiration_track_id:
            # Non-filter inspiration-sourced track: splicer.py mirrors its
            # take into the shared vault-wide index alongside the setlist's
            # own preferred_takes (see process_session) — keep both in
            # sync here too, so another project referencing the same song
            # doesn't keep offering the take under the old instrument.
            from .vault import load_inspiration_index, save_inspiration_index, vault_root
            root = vault_root(config)
            index = load_inspiration_index(root)
            shared_entry = index.get(str(entry.inspiration_track_id))
            if shared_entry is not None and old_instrument in shared_entry.preferred_takes:
                del shared_entry.preferred_takes[old_instrument]
                shared_entry.set_preferred_take(new_inst.name, new_take)
                save_inspiration_index(root, index)

    # --- inspiration ---

    def search_inspiration_artists(self, partial: str) -> list[str]:
        from .inspiration import search_artist_suggestions
        return search_artist_suggestions(self.get_config(), partial)

    def search_inspiration_by_filter(self, filter_criteria: dict) -> list[dict]:
        from .inspiration import InspirationError, search_tracks_by_filter
        try:
            return search_tracks_by_filter(self.get_config(), filter_criteria)
        except InspirationError as e:
            raise BackendError(str(e)) from e

    # --- events ---

    def on_event(self, callback: EventCallback) -> None:
        self._event_callbacks.append(callback)

    def off_event(self, callback: EventCallback) -> None:
        if callback in self._event_callbacks:
            self._event_callbacks.remove(callback)

    def _emit(self, event: str, data: dict) -> None:
        for cb in list(self._event_callbacks):
            try:
                cb(event, data)
            except Exception:
                pass

    # --- recording ---

    def is_recording(self) -> bool:
        # A session *is* the recording — the continuous stream is being
        # captured for its whole duration, playing or not.
        return self._active_session is not None

    def get_levels(self) -> tuple[float, float]:
        # _get_active_engine() rather than session-only, so the VU meters
        # also work during a plain monitoring stream (before Record is
        # pressed) — same as set_compressor_settings already relies on.
        engine = self._get_active_engine()
        if engine is None:
            return (0.0, 0.0)
        return (engine.peak_level, engine.backing_peak_level)

    def adjust_backing_volume(self, delta: int) -> None:
        with self._record_lock:
            session = self._active_session
            if session is None or session.current_track is None:
                return
            track = session.current_track
            self._backing_volume = max(0, self._backing_volume + delta)
            track.volume = self._backing_volume
            session.engine.mixer.set_volume("backing", track.volume / 100.0)
            self._log_session_event("backing_volume", f"volume={track.volume}")
            self._save_last_volumes()
            self._emit("recording_status", {"status": f"Backing volume: {track.volume}%"})

    def adjust_takes_volume(self, delta: int) -> None:
        with self._record_lock:
            session = self._active_session
            if session is None or session.current_track is None:
                return
            track = session.current_track
            self._takes_volume = max(0, self._takes_volume + delta)
            track.takes_volume = self._takes_volume
            for src in session.engine.mixer.sources:
                if src.name.startswith("take:"):
                    inst_name = src.name[5:]
                    take_info = track.preferred_takes.get(inst_name)
                    base_vol = take_info.volume if take_info else 1.0
                    session.engine.mixer.set_volume(src.name, base_vol * (track.takes_volume / 100.0))
            self._log_session_event("takes_volume", f"volume={track.takes_volume}")
            self._save_last_volumes()
            self._emit("recording_status", {"status": f"Takes volume: {track.takes_volume}%"})

    def adjust_instrument_volume(self, delta: int) -> None:
        """Nudge the instrument's live-monitor gain — see
        AudioEngine.set_instrument_volume. Applied in software (uncapped,
        like backing/takes volume) and, when the "recording" monitoring mode
        has the instrument routed through the interface's own hardware
        direct monitor instead, mirrored onto that fader too — see
        _apply_hardware_direct_monitor.

        Unlike adjust_backing_volume/adjust_takes_volume (which only make
        sense mid-take, since they write onto the loaded track's own volume
        fields), instrument monitoring is meaningful any time an engine is
        open at all — including the ambient monitor-only stream opened by
        start_monitoring() before Record is ever pressed."""
        with self._record_lock:
            engine, inst = self._get_active_engine_and_inst()
            if engine is None:
                return
            self._instrument_volume = max(0, self._instrument_volume + delta)
            engine.set_instrument_volume(self._instrument_volume / 100.0)
            if inst is not None and self._monitoring_mode == "recording":
                config = self.get_config()
                input_info = config.resolve_input(inst.input_label)
                self._apply_hardware_direct_monitor(input_info, True)
            self._save_last_volumes()
            self._emit("recording_status", {"status": f"Instrument volume: {self._instrument_volume}%"})

    # --- audio filters (compressor now, more later) ---

    def get_compressor_settings(self) -> dict:
        return asdict(self._compressor_settings)

    def set_compressor_settings(self, settings: dict) -> None:
        with self._record_lock:
            self._compressor_settings = CompressorSettings(**settings)
            self._save_compressor_settings()
            engine = self._get_active_engine()
            if engine is not None:
                engine.set_compressor_settings(self._compressor_settings)

    # --- live monitoring mode (Record page headphone mix) ---

    def get_monitoring_mode(self) -> str:
        return self._monitoring_mode

    def set_monitoring_mode(self, mode: str) -> None:
        if mode not in ("production", "recording"):
            raise BackendError(f"Unknown monitoring mode '{mode}'.")
        with self._record_lock:
            self._monitoring_mode = mode
            # The normal Record-page engine (a real take, the session engine
            # a take reuses, or the ambient monitor-only stream opened by
            # start_monitoring()) respects this live toggle — Video Check
            # and the Latency test each have their own fixed monitoring
            # behavior regardless of this setting (see
            # start_video_check/start_latency_test).
            engine, inst = self._get_active_engine_and_inst()
            if engine is not None:
                engine.set_monitor_instrument(mode == "production")
            if inst is not None:
                config = self.get_config()
                input_info = config.resolve_input(inst.input_label)
                self._apply_hardware_direct_monitor(input_info, mode == "recording")
            self._emit("monitoring_mode_changed", {"mode": mode})

    def _apply_hardware_direct_monitor(self, input_info, enabled: bool) -> None:
        """Best-effort: also flip the audio interface's own zero-latency
        hardware direct monitor, for interfaces this is known to work on
        (currently just the studio's Scarlett 4i4 4th Gen — see
        scarlett2_direct_monitor.py). Silently does nothing for any other
        input device; failures here (device unplugged, Focusrite Control 2
        has it open, etc.) are never surfaced — the Record page's on-screen
        reminder is the fallback either way.

        When enabling, uses the operator's current Instrument Volume dial
        level (self._instrument_volume) rather than a fixed unity gain, so
        the dial reaches "recording" monitoring mode too, where the
        instrument is heard purely through this hardware path — see
        adjust_instrument_volume."""
        if input_info is None or input_info.device != FOCUSRITE_DEVICE_NAME:
            return
        try:
            set_channel_gain(input_info.channel, (self._instrument_volume / 100.0) if enabled else 0.0)
        except Exception:
            pass

    def start_monitoring(self) -> bool:
        """Best-effort: open a live, listen-only audio stream for
        config.last_selected_instrument, with nothing recorded to disk —
        so the operator can hear themselves in whatever monitoring mode is
        selected the moment the app/server starts, rather than only once a
        take begins (see set_monitoring_mode/adjust_instrument_volume,
        which both now reach this stream too via
        _get_active_engine_and_inst()).

        A no-op, not an error, if there's no instrument selected yet,
        another engine already holds the hardware, or the device can't be
        opened for any reason — this is a nice-to-have layered on top of
        start_recording()/begin_session()/start_video_check()/
        start_latency_test(), which each call _close_active_monitor() first
        to take the hardware for themselves. Returns whether a monitor
        stream actually opened."""
        with self._record_lock:
            return self._start_monitoring_locked()

    def _start_monitoring_locked(self) -> bool:
        """start_monitoring()'s body, for callers that already hold
        self._record_lock (the teardown of any other engine, so ambient
        monitoring resumes right after)."""
        if (
            self._active_latency_test is not None
            or self._active_video_check is not None
            or self._active_session is not None
            or self._active_monitor is not None
        ):
            return False
        config = self.get_config()
        inst = config.get_instrument(config.last_selected_instrument)
        if inst is None:
            return False
        input_info = config.resolve_input(inst.input_label)
        if input_info is None:
            return False
        try:
            import sounddevice as sd
            from .audio.devices import resolve_device
            out_dev = resolve_device(sd, config.output_device, "output")
            in_dev = resolve_device(sd, input_info.device, "input")
            if in_dev is None or (config.output_device and out_dev is None):
                return False
            in_info = sd.query_devices(in_dev, "input")
            out_info = sd.query_devices(out_dev, "output")
            if input_info.channel > in_info["max_input_channels"]:
                return False
            output_channels = min(config.output_channels, out_info["max_output_channels"])

            from .audio.engine import AudioEngine
            engine = AudioEngine(
                sample_rate=config.sample_rate,
                buffer_size=config.buffer_size,
                input_device=in_dev,
                output_device=out_dev,
                input_channels=max(input_info.channel, 1),
                output_channels=max(1, output_channels),
                monitor_channel=input_info.channel - 1,
                compressor_settings=self._compressor_settings,
                monitor_instrument=self._monitoring_mode == "production",
                instrument_volume=self._instrument_volume / 100.0,
            )
            engine.start()
        except Exception:
            return False
        self._apply_hardware_direct_monitor(input_info, self._monitoring_mode == "recording")
        self._active_monitor = _ActiveMonitor(engine=engine, inst=inst)
        return True

    def _close_active_monitor(self) -> None:
        """Release start_monitoring()'s ambient stream so something else can
        open the hardware exclusively. Called with self._record_lock already
        held, same as _apply_hardware_direct_monitor."""
        if self._active_monitor is not None:
            self._active_monitor.engine.stop()
            self._active_monitor = None

    def restart_monitoring(self) -> bool:
        with self._record_lock:
            config = self.get_config()
            if self._active_monitor is not None:
                if self._active_monitor.inst.name.lower() == (config.last_selected_instrument or "").lower():
                    return True  # already monitoring the right thing
                self._close_active_monitor()
            return self._start_monitoring_locked()

    def start_recording(self, req: StartRecordingRequest) -> None:
        with self._record_lock:
            if self._active_latency_test is not None or self._active_video_check is not None:
                raise BackendError("Another recording is already in progress.")

            config = self.get_config()
            inst = config.get_instrument(req.instrument_name)
            if inst is None:
                raise BackendError(f"Instrument '{req.instrument_name}' not found.")

            session = self._active_session
            if session is not None and (
                session.project.name != req.project_name or session.inst.name.lower() != inst.name.lower()
            ):
                raise BackendError(
                    f"A session is active for '{session.inst.name}' in '{session.project.name}' — "
                    "end it before recording a different project/instrument."
                )

            opened_here = False
            if session is None:
                self._begin_session_locked(req.project_name, req.instrument_name)
                session = self._active_session
                opened_here = True

            try:
                project = session.project
                if not (0 <= req.track_index < len(project.setlist.tracks)):
                    raise BackendError("Invalid track selection.")
                index = req.track_index
                track = self._resolve_filter_slot_for_session(session, config, project.setlist.tracks[index], index)

                if session.playing and session.current_track is not None:
                    # Loading a different track over a playing one abandons
                    # that play-through — same as Next, minus the auto-pick.
                    self._log_session_event(
                        "track_skipped", frame=session.engine.session_frames,
                        track_index=session.current_track_index, track_name=session.current_track.name,
                    )
                self._load_track_locked(session, track, index, config)
            except BackendError:
                if opened_here:
                    self._abort_empty_session_locked(session)
                raise

            self._emit("recording_status", {
                "phase": "waiting",
                "status": f"Loaded '{track.name}' — press Record to start",
                "track_name": track.name,
            })

    def _load_track_locked(
        self, session: "_ActiveSession", track: TrackEntry, index: int, config: StudioConfig,
    ) -> None:
        """Swap `track` into the session engine's mixer, cued at 0:00 and
        not playing — downloading its backing first if needed. Called with
        self._record_lock held; emits no phase event (callers word their
        own)."""
        project = session.project
        backing_path = project.backing_tracks_dir / track.backing_track
        if track.inspiration_track_id and not backing_path.exists():
            from .inspiration import InspirationError, download_inspiration_track
            self._emit("recording_status", {"status": f"Downloading '{track.name}'..."})
            try:
                download_inspiration_track(track, backing_path, config)
            except InspirationError as e:
                raise BackendError(str(e)) from e

        engine = session.engine
        engine.mixer.set_playing(False)
        session.playing = False
        engine.mixer.clear()

        # The remembered mixer level (this run's, or carried over from the
        # last time anything was recorded) always wins over the track's own
        # saved default — otherwise an untouched track loads at whatever
        # volume it last happened to be saved at, which can be jarringly loud.
        track.volume = self._backing_volume
        track.takes_volume = self._takes_volume

        if backing_path.exists():
            engine.mixer.add_source("backing", backing_path, volume=track.volume / 100.0)

        # For an inspiration-sourced track, an *other* project could have
        # recorded a take on this exact song too — merged in from the
        # shared vault-wide index (vault.py), not just this project's own
        # preferred_takes, so layering finds it regardless of which
        # project originally recorded it. This project's own record wins
        # on conflict (same instrument in both) since it's the more
        # specific/authoritative one for what's actually loaded here.
        other_takes = dict(track.preferred_takes)
        if track.inspiration_track_id:
            from .vault import get_inspiration_entry, vault_root
            shared = get_inspiration_entry(vault_root(config), track.inspiration_track_id)
            if shared is not None:
                for inst_name, take_info in shared.preferred_takes.items():
                    other_takes.setdefault(inst_name, take_info)

        trim = int(config.latency_compensation_ms / 1000.0 * config.sample_rate)
        for other_inst, take_info in other_takes.items():
            if other_inst.lower() == session.inst.name.lower():
                continue
            take_path = project.completed_takes_dir / take_info.filename
            if take_path.exists():
                effective_vol = take_info.volume * (track.takes_volume / 100.0)
                engine.mixer.add_source(f"take:{other_inst}", take_path, volume=effective_vol, trim_frames=trim)

        engine.mixer.reset()
        session.current_track = track
        session.current_track_index = index
        self._log_session_event(
            "track_loaded", frame=engine.session_frames, track_index=index, track_name=track.name,
        )

    def _resolve_filter_slot(
        self, config: StudioConfig, track: TrackEntry, instrument_name: str, exclude_id: int | None = None,
    ) -> TrackEntry:
        """Resolve `track` for `instrument_name` to record. A non-filter
        track passes through unchanged.

        Queries the inspiration server for every song matching the
        filter, then prefers one that some *other* project or session has
        already recorded a take on (but not yet this instrument) — via
        the shared vault-wide inspiration-take index (vault.py), not just
        this project's own history — so a later instrument can layer onto
        the same song instead of the setlist only ever accumulating
        unrelated one-off takes. Falls back to a genuinely random pick
        among every match when none qualify. Either way, the setlist
        itself never gains a new entry here — see TrackEntry's docstring
        and _resolve_filter_slot_for_session's caching wrapper, which is
        what actually gets called during a session; this is the pure
        "pick one" step, split out for testability.

        `exclude_id` (redraw_current_track's use) leaves one specific
        inspiration_track_id out of consideration — so "give me a
        different one" doesn't just hand back what's already loaded —
        falling back to allowing it anyway if excluding it would leave no
        candidates at all (a filter matching only one song shouldn't error
        out just because that one song is the one being redrawn away
        from)."""
        if not track.is_inspiration_filter:
            return track
        from .inspiration import InspirationError, build_inspiration_track_entry, search_tracks_by_filter
        try:
            matches = search_tracks_by_filter(config, track.inspiration_filter)
        except InspirationError as e:
            raise BackendError(str(e)) from e
        if not matches:
            raise BackendError(f"No inspiration tracks match the filter for '{track.name}'.")
        candidates = [m for m in matches if m.get("id") != exclude_id] or matches

        from .vault import load_inspiration_index, vault_root
        index = load_inspiration_index(vault_root(config))
        reusable = []
        for m in candidates:
            shared = index.get(str(m.get("id")))
            if shared is not None and shared.preferred_takes and instrument_name not in shared.preferred_takes:
                reusable.append(m)
        chosen = random.choice(reusable) if reusable else random.choice(candidates)
        return build_inspiration_track_entry(chosen)

    def _resolve_filter_slot_for_session(
        self, session: "_ActiveSession", config: StudioConfig, track: TrackEntry, index: int,
    ) -> TrackEntry:
        """_resolve_filter_slot(), cached for the rest of `session` — a
        filter slot revisited later in the same session (e.g. a manual
        reselect) gets the same resolved track back rather than a fresh
        random draw each time. Also marks the filter slot's own `index` as
        completed for this session (session.completed_track_indices) the
        moment it's drawn, regardless of whether the resulting take itself
        later succeeds — otherwise _advance_locked would keep re-offering
        the same filter slot indefinitely within one session, since the
        slot's own preferred_takes never actually gets a take (the take
        belongs to whatever song got drawn — recorded into the shared
        vault-wide index instead, see vault.record_inspiration_take).
        A non-filter track passes through unchanged (and isn't cached —
        nothing to cache)."""
        if not track.is_inspiration_filter:
            return track
        cached = session.resolved_filter_picks.get(index)
        if cached is not None:
            return cached
        resolved = self._resolve_filter_slot(config, track, session.inst.name)
        session.resolved_filter_picks[index] = resolved
        session.completed_track_indices.add(index)
        return resolved

    def _start_playback_locked(self, session: "_ActiveSession") -> None:
        """Start the loaded track's backing from 0:00 — the moment a take
        segment begins on the session timeline. Called with
        self._record_lock held."""
        track = session.current_track
        engine = session.engine
        if not session.video_start_track_name:
            session.video_start_track_name = track.name
        self._log_session_event(
            "record_start", frame=engine.session_frames,
            track_index=session.current_track_index, track_name=track.name,
        )
        engine.mixer.reset()
        engine.mixer.set_playing(True)
        session.playing = True
        self._emit("recording_status", {
            "phase": "recording",
            "status": f"Recording '{track.name}'",
            "track_name": track.name,
        })

    def _advance_locked(self, session: "_ActiveSession", status_prefix: str = "") -> TrackEntry | None:
        """Find and load the next setlist track after the current one that
        still needs a take for the session's instrument — skipping both
        tracks with takes already on disk and ones completed earlier in
        this same session (the setlist doesn't learn about those until
        post-processing). Returns the loaded track (resolved, if the
        setlist position found is a filter slot — see
        _resolve_filter_slot_for_session), or None (emitting a "waiting"
        status) when nothing's left. Called with self._record_lock held;
        playback must already be stopped."""
        tracks = session.project.setlist.tracks
        start = (session.current_track_index + 1) if session.current_track_index is not None else 0
        index = None
        for i in range(start, len(tracks)):
            if i in session.completed_track_indices:
                continue
            if tracks[i].get_take_for_instrument(session.inst.name) is None:
                index = i
                break
        if index is None:
            session.current_track = None
            session.current_track_index = None
            session.engine.mixer.clear()
            self._emit("recording_status", {
                "phase": "waiting",
                "status": status_prefix + "No more tracks need a take — press Stop to end the session.",
                "track_name": None,
            })
            return None
        config = self.get_config()
        track = self._resolve_filter_slot_for_session(session, config, tracks[index], index)
        self._load_track_locked(session, track, index, config)
        return track

    def _abort_empty_session_locked(self, session: "_ActiveSession") -> None:
        """Roll back a session start_recording() itself just opened when its
        track load then failed — tear the capture down and delete the
        milliseconds-old session dir, so a failed start never leaves an
        orphaned, take-less session behind. Called with self._record_lock
        held."""
        self._active_session = None
        session.engine.set_on_song_end(None)
        session.engine.set_stream_sink(None)
        session.engine.stop()
        if session.stream_feeder:
            # Before video_recorder.stop() — see the matching comment in
            # _end_session for why this order matters.
            session.stream_feeder.stop()
        if session.video_recorder:
            session.video_recorder.stop()
            self._preview.resume()
            self._emit("preview_resumed", {})
        if session.youtube_broadcast_id is not None:
            self._complete_youtube_broadcast(self.get_config(), session.youtube_broadcast_id)
        shutil.rmtree(session.session_dir, ignore_errors=True)
        self._start_monitoring_locked()

    def _open_video_recorder(self, device: str, output_path: Path) -> "VideoRecorder | None":
        """Pause the live-preview capture loop (ffmpeg needs exclusive access
        to the camera device) and start recording `device` to `output_path` —
        used by a video check (start_video_check()) and the latency test. The VideoRecorder it returns tees a low-res copy of
        every frame back through _CameraPreviewManager.push_external_frame()
        while it runs, so the Record tab's live feed keeps showing real
        camera frames for the duration instead of freezing.

        Returns None (leaving the preview running untouched) if there's no
        camera configured, ffmpeg isn't available, or the recorder failed
        to start."""
        if not device:
            return None
        from .video.capture import VideoRecorder, ffmpeg_available
        if not ffmpeg_available():
            return None
        self._preview.pause()
        recorder = VideoRecorder(device, output_path, on_preview_frame=self._preview.push_external_frame)
        if recorder.start():
            return recorder
        self._preview.resume()
        return None


    def unpause_recording(self) -> None:
        with self._record_lock:
            session = self._active_session
            if session is None or session.current_track is None or session.playing:
                raise BackendError("Not ready to unpause.")
            self._start_playback_locked(session)

    def restart_take(self) -> None:
        """Send the playing backing back to 0:00 — logged as back_to_start,
        so the abandoned play-through never becomes a take (unless it was
        already long enough to keep — see processing/splicer.py) and the
        one now starting still can. Nothing is discarded or re-created:
        the continuous session capture just keeps rolling."""
        with self._record_lock:
            session = self._active_session
            if session is None or not session.playing or session.current_track is None:
                raise BackendError("Not currently recording.")
            track = session.current_track
            self._log_session_event(
                "back_to_start", frame=session.engine.session_frames,
                track_index=session.current_track_index, track_name=track.name,
            )
            session.engine.mixer.reset()
            self._emit("recording_status", {
                "phase": "recording",
                "status": f"Back to the beginning of '{track.name}'",
                "track_name": track.name,
            })

    def next_track(self) -> None:
        with self._record_lock:
            session = self._active_session
            if session is None:
                raise BackendError("No session in progress.")
            prefix = ""
            if session.playing and session.current_track is not None:
                self._log_session_event(
                    "track_skipped", frame=session.engine.session_frames,
                    track_index=session.current_track_index, track_name=session.current_track.name,
                )
                session.engine.mixer.set_playing(False)
                session.playing = False
                prefix = f"Skipped '{session.current_track.name}'. "
            if self._advance_locked(session, status_prefix=prefix) is not None:
                self._start_playback_locked(session)

    def redraw_current_track(self) -> None:
        with self._record_lock:
            session = self._active_session
            if session is None or session.current_track is None or session.current_track_index is None:
                raise BackendError("Nothing is currently loaded.")
            index = session.current_track_index
            slot = session.project.setlist.tracks[index]
            if not slot.is_inspiration_filter:
                raise BackendError("The current track isn't a random draw — nothing to redraw.")

            if session.playing:
                self._log_session_event(
                    "track_skipped", frame=session.engine.session_frames,
                    track_index=index, track_name=session.current_track.name,
                )
                session.engine.mixer.set_playing(False)
                session.playing = False

            config = self.get_config()
            # Excludes whatever's currently loaded, so "redraw" doesn't
            # just hand the same song back (see _resolve_filter_slot).
            exclude_id = session.current_track.inspiration_track_id
            resolved = self._resolve_filter_slot(config, slot, session.inst.name, exclude_id=exclude_id)
            session.resolved_filter_picks[index] = resolved
            self._load_track_locked(session, resolved, index, config)
            self._start_playback_locked(session)

    def get_active_recording_target(self) -> tuple[str, str, int] | None:
        """Returns (project_name, instrument_name, track_index) for the
        track currently loaded in the session, or None. Lets a driver with
        no track-selection UI of its own figure out where the session is,
        regardless of which client loaded the track."""
        with self._record_lock:
            session = self._active_session
            if session is None or session.current_track_index is None:
                return None
            return session.project.name, session.inst.name, session.current_track_index

    def _on_song_naturally_ended(self) -> None:
        """Called (off the audio thread) when the backing track plays to its
        end — the moment a take completes. Logs song_end (post-processing
        turns that into the actual take file later; nothing is finalized
        here) and auto-advances: the next track that needs a take starts
        playing by itself after a short breather, no key press needed."""
        with self._record_lock:
            session = self._active_session
            if session is None or not session.playing or session.current_track is None:
                return
            finished = session.current_track
            self._log_session_event(
                "song_end", frame=session.engine.session_frames,
                track_index=session.current_track_index, track_name=finished.name,
            )
            session.completed_track_indices.add(session.current_track_index)
            session.engine.mixer.set_playing(False)
            session.playing = False
            loaded = self._advance_locked(
                session, status_prefix=f"Completed take for '{finished.name}'. ",
            )
            if loaded is not None:
                self._emit("recording_status", {
                    "phase": "waiting",
                    "status": f"Completed take for '{finished.name}' — '{loaded.name}' starts "
                              f"in {AUTO_ADVANCE_GAP_SECONDS:g}s",
                    "track_name": loaded.name,
                })

        if loaded is None:
            return
        # The breather happens outside the lock so keys stay live; whoever
        # pressed one meanwhile (Next, Stop, a manual track load) wins —
        # the re-check below just stands down if the world changed.
        time.sleep(AUTO_ADVANCE_GAP_SECONDS)
        with self._record_lock:
            current = self._active_session
            if current is session and not session.playing and session.current_track is loaded:
                self._start_playback_locked(session)

    def stop_recording(self) -> None:
        """Stop recording = end the session. No-op when nothing is active,
        so a stray stop press never errors."""
        self._end_session(missing_ok=True)


    # --- camera preview ---

    def open_camera_preview(self, on_frame: FrameCallback) -> PreviewSubscription:
        return self._preview.subscribe(on_frame)

    # --- camera latency test ---

    def start_latency_test(self, instrument_name: str, camera_device: str, play_metronome: bool = True) -> None:
        with self._record_lock:
            if (
                self._active_latency_test is not None
                or self._active_video_check is not None
                or self._active_session is not None
            ):
                raise BackendError("Another recording is already in progress.")
            if not camera_device:
                raise BackendError("Select a camera first.")
            self._close_active_monitor()  # always opens its own engine, never reuses the ambient one

            config = self.get_config()
            inst = config.get_instrument(instrument_name)
            if inst is None:
                raise BackendError(f"Instrument '{instrument_name}' not found.")
            input_info = config.resolve_input(inst.input_label)
            if input_info is None:
                raise BackendError(f"Input label '{inst.input_label}' not found in config.")

            from .video.capture import ffmpeg_available
            if not ffmpeg_available():
                raise BackendError("ffmpeg is required for the camera latency test.")

            try:
                import sounddevice as sd
            except Exception as e:
                raise BackendError(f"sounddevice unavailable: {e}") from e

            from .audio.devices import resolve_device
            out_dev = resolve_device(sd, config.output_device, "output")
            in_dev = resolve_device(sd, input_info.device, "input")
            if in_dev is None:
                raise BackendError(f"Input device '{input_info.device}' not found.")
            # config.output_device is optional (empty means "just use the
            # system default"), so only treat a miss as an error when a
            # specific device *was* configured — otherwise a device that was
            # explicitly chosen but has since disconnected (e.g. a USB audio
            # interface dropping out) would silently fall back to whatever
            # the system default output happens to be instead of raising,
            # playing audio to the wrong place with no indication why.
            if config.output_device and out_dev is None:
                raise BackendError(f"Output device '{config.output_device}' not found.")

            in_info = sd.query_devices(in_dev, "input")
            out_info = sd.query_devices(out_dev, "output")
            if input_info.channel > in_info["max_input_channels"]:
                raise BackendError(
                    f"Instrument '{inst.name}' needs input channel {input_info.channel} "
                    f"but device only has {in_info['max_input_channels']} channels."
                )
            output_channels = min(config.output_channels, out_info["max_output_channels"])

            from .audio.engine import AudioEngine
            from .audio.metronome import generate_metronome_wav
            from .video.capture import VideoRecorder

            work_dir = ensure_dir(Path(tempfile.gettempdir()) / "takeloom_latency_test")
            metronome_wav = work_dir / "metronome.wav"
            take_path = work_dir / "instrument.flac"
            video_raw = work_dir / "video_raw.mp4"
            mix_flac = work_dir / "mix.flac"
            final_video = work_dir / "result.mp4"

            if play_metronome:
                generate_metronome_wav(metronome_wav, config.sample_rate)

            engine = AudioEngine(
                sample_rate=config.sample_rate,
                buffer_size=config.buffer_size,
                input_device=in_dev,
                output_device=out_dev,
                input_channels=max(input_info.channel, 1),
                output_channels=max(1, output_channels),
                monitor_channel=input_info.channel - 1,
                compressor_settings=self._compressor_settings,
            )
            if play_metronome:
                engine.mixer.add_source("metronome", metronome_wav)
            engine.start()
            engine.mixer.reset()
            engine.mixer.set_playing(True)
            engine.start_recording(take_path)
            engine.start_mix_recording(mix_flac)

            # Same pause/resume dance as a real take (unpause_recording): if
            # the chosen test camera is the one open_camera_preview streams,
            # its cv2 capture has to let go before ffmpeg can open it exclusively.
            camera_paired_with_preview = camera_device == config.camera_device
            if camera_paired_with_preview:
                self._preview.pause()
                self._emit("preview_paused", {})

            video_recorder = VideoRecorder(camera_device, video_raw)
            if not video_recorder.start():
                engine.stop()
                if camera_paired_with_preview:
                    self._preview.resume()
                    self._emit("preview_resumed", {})
                raise BackendError("Could not start camera capture.")

            self._active_latency_test = _ActiveLatencyTest(
                engine=engine, video_recorder=video_recorder, metronome_wav=metronome_wav,
                take_path=take_path, video_raw=video_raw, mix_flac=mix_flac, final_video=final_video,
                camera_paired_with_preview=camera_paired_with_preview,
            )
            self._emit("latency_test_status", {
                "phase": "recording",
                "status": "Recording — clap or hit your instrument along with the click, then Stop.",
            })

    def stop_latency_test(self) -> None:
        with self._record_lock:
            active = self._active_latency_test
            if active is None:
                raise BackendError("No latency test in progress.")
            self._active_latency_test = None

            active.engine.stop_recording()
            active.engine.mixer.set_playing(False)
            active.engine.stop()
            active.video_recorder.stop()
            self._start_monitoring_locked()

            if active.camera_paired_with_preview:
                self._preview.resume()
                self._emit("preview_resumed", {})

            from .video.capture import mux_video_audio, open_in_default_player

            # Muxed with the currently saved video offset applied, so this
            # clip previews exactly what a real take would look like — the
            # operator dials the offset in by re-running the test after each
            # Save, not by eyeballing a fixed raw gap and doing the ms math
            # themselves.
            video_offset_ms = self.get_config().video_latency_compensation_ms
            ok = mux_video_audio(
                active.video_raw, active.mix_flac, active.take_path, active.final_video,
                video_offset_ms=video_offset_ms,
            )

            active.video_raw.unlink(missing_ok=True)
            active.mix_flac.unlink(missing_ok=True)
            active.take_path.unlink(missing_ok=True)
            active.metronome_wav.unlink(missing_ok=True)

            if not ok:
                self._emit("latency_test_status", {"phase": "idle", "status": "Video mux failed."})
                raise BackendError("Could not combine video and audio.")

            open_in_default_player(active.final_video)
            self._emit("latency_test_status", {
                "phase": "idle",
                "status": "Test recording opened for review — adjust the offsets below and Save.",
                "video_path": str(active.final_video),
            })

    # --- Video check (local-only; RemoteBackend refuses) ---

    def start_video_check(self, req: StartRecordingRequest) -> None:
        with self._record_lock:
            if (
                self._active_latency_test is not None
                or self._active_video_check is not None
                or self._active_session is not None
            ):
                raise BackendError("Another recording is already in progress.")
            self._close_active_monitor()  # always opens its own engine, never reuses the ambient one

            config = self.get_config()
            project = self._open_project(req.project_name)

            inst = config.get_instrument(req.instrument_name)
            if inst is None:
                raise BackendError(f"Instrument '{req.instrument_name}' not found.")

            if not (0 <= req.track_index < len(project.setlist.tracks)):
                raise BackendError("Invalid track selection.")
            track = project.setlist.tracks[req.track_index]
            track = self._resolve_filter_slot(config, track, inst.name)

            input_info = config.resolve_input(inst.input_label)
            if input_info is None:
                raise BackendError(f"Input label '{inst.input_label}' not found in config.")

            backing_path = project.backing_tracks_dir / track.backing_track
            if track.inspiration_track_id and not backing_path.exists():
                from .inspiration import InspirationError, download_inspiration_track
                self._emit("video_check_status", {"status": f"Downloading '{track.name}'..."})
                try:
                    download_inspiration_track(track, backing_path, config)
                except InspirationError as e:
                    raise BackendError(str(e)) from e

            try:
                import sounddevice as sd
            except Exception as e:
                raise BackendError(f"sounddevice unavailable: {e}") from e

            from .audio.devices import resolve_device
            out_dev = resolve_device(sd, config.output_device, "output")
            in_dev = resolve_device(sd, input_info.device, "input")
            if in_dev is None:
                raise BackendError(f"Input device '{input_info.device}' not found.")
            if config.output_device and out_dev is None:
                raise BackendError(f"Output device '{config.output_device}' not found.")

            in_info = sd.query_devices(in_dev, "input")
            out_info = sd.query_devices(out_dev, "output")
            max_in = in_info["max_input_channels"]
            if input_info.channel > max_in:
                raise BackendError(
                    f"Instrument '{inst.name}' needs input channel {input_info.channel} "
                    f"but device only has {max_in} channels."
                )
            output_channels = min(config.output_channels, out_info["max_output_channels"])

            from .audio.engine import AudioEngine
            engine = AudioEngine(
                sample_rate=config.sample_rate,
                buffer_size=config.buffer_size,
                input_device=in_dev,
                output_device=out_dev,
                input_channels=max(input_info.channel, 1),
                output_channels=max(1, output_channels),
                monitor_channel=input_info.channel - 1,
                compressor_settings=self._compressor_settings,
                # Video Check is played the same way a real take is, so it
                # always runs Recording Monitoring (zero-latency hardware
                # direct monitor for the instrument) regardless of whatever
                # the Record page's monitoring_mode toggle is currently set
                # to — see get_monitoring_mode()/set_monitoring_mode().
                monitor_instrument=False,
            )
            self._apply_hardware_direct_monitor(input_info, True)

            if backing_path.exists():
                engine.mixer.add_source("backing", backing_path, volume=self._backing_volume / 100.0)

            trim = int(config.latency_compensation_ms / 1000.0 * config.sample_rate)
            for other_inst, take_info in track.preferred_takes.items():
                if other_inst.lower() == inst.name.lower():
                    continue
                take_path = project.completed_takes_dir / take_info.filename
                if take_path.exists():
                    effective_vol = take_info.volume * (self._takes_volume / 100.0)
                    engine.mixer.add_source(f"take:{other_inst}", take_path, volume=effective_vol, trim_frames=trim)

            work_dir = ensure_dir(Path(tempfile.gettempdir()) / "takeloom_video_check")
            take_path = work_dir / "instrument.flac"
            video_raw = work_dir / "video_raw.mp4"
            mix_flac = work_dir / "mix.flac"
            final_video = work_dir / "result.mp4"

            engine.start()
            engine.mixer.reset()
            engine.mixer.set_playing(True)
            engine.start_recording(take_path)
            engine.set_on_song_end(self._on_video_check_naturally_ended)

            # Video check always uses the configured camera (unlike the
            # latency test, which lets the operator pick a different one to
            # test), and captures it through the exact same
            # _open_video_recorder() path a real take does — so what you see
            # played back afterward is a true preview of production
            # quality/behavior, not a separate approximation.
            video_recorder = self._open_video_recorder(config.camera_device, video_raw)
            if video_recorder is not None:
                engine.start_mix_recording(mix_flac)

            self._active_video_check = _ActiveVideoCheck(
                engine=engine, video_recorder=video_recorder, take_path=take_path,
                video_raw=video_raw if video_recorder else None,
                mix_flac=mix_flac if video_recorder else None,
                final_video=final_video if video_recorder else None,
            )
            self._emit("video_check_status", {
                "phase": "recording",
                "status": f"Video check — playing '{track.name}'...",
                "track_name": track.name,
            })

    def stop_video_check(self) -> None:
        with self._record_lock:
            active = self._active_video_check
            if active is None:
                raise BackendError("No video check in progress.")
            self._active_video_check = None
            self._finish_video_check(active)

    def _on_video_check_naturally_ended(self) -> None:
        with self._record_lock:
            active = self._active_video_check
            if active is None:
                return
            self._active_video_check = None
            self._finish_video_check(active)

    def _finish_video_check(self, active: "_ActiveVideoCheck") -> None:
        """Tear down an in-progress video check. Called with self._record_lock
        held and self._active_video_check already cleared."""
        active.engine.set_on_song_end(None)
        active.engine.stop_recording()
        active.engine.mixer.set_playing(False)
        active.engine.stop()
        self._start_monitoring_locked()
        if active.video_recorder:
            active.video_recorder.stop()
            self._preview.resume()

        if active.video_recorder and active.video_raw and active.mix_flac and active.final_video:
            from .video.capture import mux_video_audio
            video_offset_ms = self.get_config().video_latency_compensation_ms
            ok = mux_video_audio(
                active.video_raw, active.mix_flac, active.take_path, active.final_video,
                video_offset_ms=video_offset_ms,
            )
            active.video_raw.unlink(missing_ok=True)
            active.mix_flac.unlink(missing_ok=True)
            active.take_path.unlink(missing_ok=True)
            if not ok:
                self._emit("video_check_status", {"phase": "idle", "status": "Video mux failed."})
                raise BackendError("Could not combine video and audio.")
            self._emit("video_check_status", {
                "phase": "idle",
                "status": "Video check recorded — review it, then close the window to discard it.",
                "result_path": str(active.final_video),
                "has_video": True,
            })
        else:
            self._emit("video_check_status", {
                "phase": "idle",
                "status": "Video check recorded — review it, then close the window to discard it.",
                "result_path": str(active.take_path),
                "has_video": False,
            })

    # --- continuous multi-track session recording (local-only; RemoteBackend refuses) ---

    def _log_session_event(
        self, event_type: str, details: str = "",
        frame: int | None = None, track_index: int | None = None, track_name: str = "",
    ) -> None:
        session = self._active_session
        if session is None:
            return
        session.events.append(_SessionEvent(
            timestamp=timestamp_now() - session.session_start,
            wall_time=wall_timestamp(),
            event_type=event_type,
            details=details,
            frame=frame,
            track_index=track_index,
            track_name=track_name,
        ))

    def begin_session(self, project_name: str, instrument_name: str) -> None:
        with self._record_lock:
            self._begin_session_locked(project_name, instrument_name)
            session = self._active_session
            self._emit("recording_status", {
                "phase": "waiting",
                "status": f"Session started for '{session.inst.name}' in '{session.project.name}'.",
            })

    def _create_youtube_broadcast(self, config: StudioConfig, project: Project, inst) -> str | None:
        """Best-effort: title/describe and bind a fresh YouTube broadcast
        to the configured stream key via the Data API (see youtube_api.py),
        rendering the user's own title/description templates (Streaming
        tab) instead of whatever title (or none) was left on that stream
        key from last time. Returns the new broadcast's id (for
        _complete_youtube_broadcast at session end), or None on any
        failure — which never blocks the RTMP stream itself, just leaves
        its title/description alone."""
        from .youtube_api import (
            YouTubeAPIError, create_and_bind_broadcast, find_stream_id, refresh_access_token, render_stream_template,
        )
        try:
            access_token = refresh_access_token(
                config.youtube_oauth_client_id, config.youtube_oauth_client_secret, config.youtube_oauth_refresh_token,
            )
            stream_id = find_stream_id(access_token, config.youtube_stream_key)
            template_values = dict(
                studio=config.studio_name, studio_location=config.studio_location,
                musician=inst.musician or config.studio_musician, project=project.name, instrument=inst.name,
            )
            title = render_stream_template(config.youtube_title_template, **template_values)
            description = render_stream_template(config.youtube_description_template, **template_values)
            broadcast_id = create_and_bind_broadcast(
                access_token, stream_id, title, description, config.youtube_broadcast_visibility,
            )
            # The broadcast id is proof YouTube actually accepted the create
            # + bind calls (an HTTPError anywhere in that chain would have
            # been caught below instead), not just that we made the request.
            self._emit("streaming_status", {
                "status": f'YouTube accepted the broadcast request — titled "{title}" (id {broadcast_id}).',
            })
            return broadcast_id
        except YouTubeAPIError as e:
            self._emit("streaming_status", {"status": f"Streaming live, but the YouTube API rejected the title request: {e}"})
            return None

    def _complete_youtube_broadcast(self, config: StudioConfig, broadcast_id: str) -> None:
        """Best-effort: end a session's bound broadcast right away instead
        of leaving it for YouTube's own stream-health timeout to notice the
        RTMP connection dropped. Never raises — even on failure, YouTube's
        own timeout still ends it a little later regardless."""
        from .youtube_api import YouTubeAPIError, refresh_access_token, transition_broadcast
        try:
            access_token = refresh_access_token(
                config.youtube_oauth_client_id, config.youtube_oauth_client_secret, config.youtube_oauth_refresh_token,
            )
            transition_broadcast(access_token, broadcast_id, "complete")
            self._emit("streaming_status", {"status": f"YouTube accepted the request to end broadcast {broadcast_id}."})
        except YouTubeAPIError as e:
            self._emit("streaming_status", {"status": f"Couldn't tell YouTube to end broadcast {broadcast_id}: {e}"})

    def _begin_session_locked(self, project_name: str, instrument_name: str) -> None:
        """begin_session()'s body, for start_recording()'s auto-open (which
        already holds self._record_lock and words its own status). Opens the
        continuous session capture: audio stream + session recorder, the
        session video if a camera's configured, and the event log."""
        if (
            self._active_latency_test is not None
            or self._active_video_check is not None
            or self._active_session is not None
        ):
            raise BackendError("Another recording is already in progress.")
        self._close_active_monitor()  # always opens its own engine, never reuses the ambient one

        config = self.get_config()
        project = self._open_project(project_name)
        # Best-effort: pulls back anything "remote" vault mode pruned
        # locally after an earlier session — see vault.py. A no-op in
        # "local"/"both" modes, where nothing's ever missing, and never
        # blocks the session from starting if a download fails.
        from .vault import ensure_setlist_files_local
        ensure_setlist_files_local(
            config, project, log=lambda msg: self._emit("recording_status", {"status": msg}),
        )
        inst = config.get_instrument(instrument_name)
        if inst is None:
            raise BackendError(f"Instrument '{instrument_name}' not found.")
        input_info = config.resolve_input(inst.input_label)
        if input_info is None:
            raise BackendError(f"Input label '{inst.input_label}' not found in config.")

        try:
            import sounddevice as sd
        except Exception as e:
            raise BackendError(f"sounddevice unavailable: {e}") from e

        from .audio.devices import resolve_device
        out_dev = resolve_device(sd, config.output_device, "output")
        in_dev = resolve_device(sd, input_info.device, "input")
        if in_dev is None:
            raise BackendError(f"Input device '{input_info.device}' not found.")
        if config.output_device and out_dev is None:
            raise BackendError(f"Output device '{config.output_device}' not found.")

        in_info = sd.query_devices(in_dev, "input")
        out_info = sd.query_devices(out_dev, "output")
        max_in = in_info["max_input_channels"]
        if input_info.channel > max_in:
            raise BackendError(
                f"Instrument '{inst.name}' needs input channel {input_info.channel} "
                f"but device only has {max_in} channels."
            )
        output_channels = min(config.output_channels, out_info["max_output_channels"])

        from .audio.engine import AudioEngine
        engine = AudioEngine(
            sample_rate=config.sample_rate,
            buffer_size=config.buffer_size,
            input_device=in_dev,
            output_device=out_dev,
            input_channels=max(input_info.channel, 1),
            output_channels=max(1, output_channels),
            monitor_channel=input_info.channel - 1,
            compressor_settings=self._compressor_settings,
            monitor_instrument=self._monitoring_mode == "production",
            instrument_volume=self._instrument_volume / 100.0,
        )
        self._apply_hardware_direct_monitor(input_info, self._monitoring_mode == "recording")
        engine.start()

        # Best-effort, testing-stage feature: continuously guesses which
        # configured instrument is actually playing from the live input's
        # frequency content, and surfaces it via "instrument_detected" so
        # the Record tab can show it — purely informational for now, does
        # not touch `inst`/session metadata. See audio/instrument_classifier.py.
        if config.instruments:
            from .audio.instrument_classifier import InstrumentClassifier

            def _on_instrument_detected(name: str, confidence: float) -> None:
                self._emit("instrument_detected", {"instrument": name, "confidence": confidence})

            classifier = InstrumentClassifier(config.sample_rate, config.instruments, _on_instrument_detected)
            engine.set_instrument_sink(classifier.process_block)

        session_name = wall_timestamp().replace(":", "-").replace(" ", "_")
        from .vault import vault_session_dir
        session_dir = ensure_dir(vault_session_dir(config, project.name, f"{session_name}_{inst.name}"))
        session_flac = session_dir / "session.flac"
        engine.start_session_recording(session_flac)

        video_recorder = None
        session_video_raw = session_mix_flac = None
        video_start_wall_time = None
        mix_start_frame = 0
        stream_feeder = None
        youtube_broadcast_id = None
        if config.camera_device:
            from .video.capture import VideoRecorder, ffmpeg_available
            if ffmpeg_available():
                self._preview.pause()
                self._emit("preview_paused", {})
                session_video_raw = session_dir / "session_video_raw.mp4"
                session_mix_flac = session_dir / "session_mix.flac"

                stream_target = None
                if config.streaming_enabled and config.youtube_stream_key:
                    from .streaming import LiveAudioFeeder, StreamTarget, fifo_supported, youtube_rtmp_url
                    if fifo_supported():
                        stream_feeder = LiveAudioFeeder(
                            session_dir / "stream_audio.fifo",
                            sample_rate=config.sample_rate, channels=engine.output_channels,
                        )
                        stream_feeder.start()
                        stream_target = StreamTarget(
                            rtmp_url=youtube_rtmp_url(config.youtube_stream_key),
                            audio_fifo=stream_feeder.fifo_path,
                            sample_rate=config.sample_rate, channels=engine.output_channels,
                            width=config.streaming_video_width, bitrate_kbps=config.streaming_bitrate_kbps,
                        )
                        # Bind a freshly titled broadcast before ffmpeg starts
                        # pushing RTMP, so it's already in place once data
                        # starts flowing (see _create_youtube_broadcast).
                        # Best-effort: title automation failing never blocks
                        # the stream itself.
                        if (
                            config.youtube_oauth_client_id
                            and config.youtube_oauth_client_secret
                            and config.youtube_oauth_refresh_token
                        ):
                            youtube_broadcast_id = self._create_youtube_broadcast(config, project, inst)
                    else:
                        stream_feeder = None
                        self._emit("streaming_status", {
                            "status": "Live streaming isn't supported on this platform.", "active": False,
                        })

                # on_preview_frame tees a low-res copy of every frame back
                # through _CameraPreviewManager (see push_external_frame) —
                # same as _open_video_recorder's video check/latency-test
                # path — so the Record tab's live feed keeps showing real
                # camera frames for the whole session instead of freezing.
                video_recorder = VideoRecorder(
                    config.camera_device, session_video_raw, on_preview_frame=self._preview.push_external_frame,
                    stream_target=stream_target,
                )
                if video_recorder.start():
                    # Where the mix/video timeline begins on the session-audio
                    # timeline — splicing maps take frames onto the video with
                    # this (see _save_session_log / processing/splicer.py).
                    mix_start_frame = engine.session_frames
                    engine.start_mix_recording(session_mix_flac)
                    video_start_wall_time = wall_timestamp()
                    if stream_feeder is not None:
                        engine.set_stream_sink(stream_feeder.push)
                        self._emit("streaming_status", {"status": "Streaming live to YouTube.", "active": True})
                else:
                    video_recorder = None
                    if stream_feeder is not None:
                        stream_feeder.stop()
                        stream_feeder = None
                    if youtube_broadcast_id is not None:
                        # ffmpeg never actually started, so RTMP data never
                        # flows and enableAutoStart never fires — without
                        # this the broadcast would sit orphaned in YouTube
                        # Studio forever instead of ending itself.
                        self._complete_youtube_broadcast(config, youtube_broadcast_id)
                        youtube_broadcast_id = None
                    self._preview.resume()
                    self._emit("preview_resumed", {})
        elif config.streaming_enabled and config.youtube_stream_key:
            self._emit("streaming_status", {
                "status": "Streaming needs a camera — set one up on the Recording Devices tab.", "active": False,
            })

        engine.set_on_song_end(self._on_song_naturally_ended)
        self._active_session = _ActiveSession(
            engine=engine, project=project, inst=inst, session_dir=session_dir,
            session_start=timestamp_now(),
            musician=inst.musician or config.studio_musician,
            instrument_full_name=inst.full_name,
            studio_name=config.studio_name, studio_location=config.studio_location,
            session_flac=session_flac, session_video=session_dir / "session_video.mp4",
            video_recorder=video_recorder, session_video_raw=session_video_raw,
            session_mix_flac=session_mix_flac, video_start_wall_time=video_start_wall_time,
            mix_start_frame=mix_start_frame, stream_feeder=stream_feeder,
            youtube_broadcast_id=youtube_broadcast_id,
        )
        self._log_session_event("session_start", f"instrument={inst.name}")

    def end_session(self) -> None:
        self._end_session(missing_ok=False)

    def _end_session(self, missing_ok: bool) -> None:
        """Close the session: log the final events, finalize the continuous
        audio/video capture, save the log — all quick — then hand the heavy
        lifting (finding completed takes in the log and clipping them + their
        videos out of the recording) to a background thread, so the app is
        back to idle the moment recording stops. See _process_session()."""
        with self._record_lock:
            session = self._active_session
            if session is None:
                if missing_ok:
                    return
                raise BackendError("No session in progress.")

            if session.playing and session.current_track is not None:
                # Cut off mid-song: logged so post-processing can still keep
                # it if it ran long enough (see processing/splicer.py) —
                # otherwise it just never becomes a take.
                self._log_session_event(
                    "song_stopped", frame=session.engine.session_frames,
                    track_index=session.current_track_index, track_name=session.current_track.name,
                )
                session.engine.mixer.set_playing(False)
                session.playing = False
            self._log_session_event("session_end", frame=session.engine.session_frames)
            self._active_session = None

            session.engine.set_on_song_end(None)
            session.engine.set_stream_sink(None)
            session.engine.set_instrument_sink(None)
            session.engine.stop()  # closes the stream and every recorder on it (session/mix)
            self._start_monitoring_locked()

            if session.stream_feeder:
                # Closes the FIFO before video_recorder.stop() sends ffmpeg
                # its 'q' — ffmpeg's demuxer can be sitting in a blocking
                # read() on the audio FIFO input waiting for more data (there
                # won't be any, now that set_stream_sink(None) above stopped
                # feeding it), and 'q' on stdin only gets noticed once ffmpeg
                # is back in its main loop; closing this first delivers EOF
                # on that read() so it returns and 'q' actually lands instead
                # of ffmpeg hanging indefinitely.
                session.stream_feeder.stop()
                self._emit("streaming_status", {"status": "Stream ended.", "active": False})
            if session.video_recorder:
                # Ends the RTMP connection too, if streaming was on: it's the
                # same ffmpeg process (see _begin_session_locked), and this
                # stop() is what finalizes it.
                session.video_recorder.stop()
                self._preview.resume()
                self._emit("preview_resumed", {})
            if session.youtube_broadcast_id is not None:
                self._complete_youtube_broadcast(self.get_config(), session.youtube_broadcast_id)

            self._save_session_log(session)
            self._emit("recording_status", {
                "phase": "idle",
                "status": "Session ended — processing takes...",
            })

        # Non-daemon deliberately: if the app quits right after stopping, the
        # process lingers (headless) until the takes are safely spliced
        # rather than losing them. CLI/server exits join it explicitly — see
        # join_session_processing().
        self._processing_thread = threading.Thread(target=self._process_session, args=(session,))
        self._processing_thread.start()

    def join_session_processing(self, timeout: float | None = None) -> None:
        """Block until the most recent session's post-processing (take
        splicing/video clipping) finishes — for exits that want to report
        completion rather than silently linger (CLI `start-session`,
        `takeloom server` shutdown). No-op if nothing is processing."""
        thread = self._processing_thread
        if thread is not None:
            thread.join(timeout)

    def _process_session(self, session: "_ActiveSession") -> None:
        """Replay the just-ended session's log and clip every completed take
        (and, with a camera, its video) out of the continuous recording,
        updating the setlist — the deferred work the live session never did.
        Runs on a background thread; a fresh session can already be recording
        while this grinds along on the old one's files."""
        from .processing.splicer import process_session
        config = self.get_config()
        try:
            summary = process_session(session_dir=session.session_dir, config=config)
        except Exception as e:
            self._emit("recording_status", {
                "phase": "idle",
                "status": f"Session take processing failed: {e} — raw files kept in {session.session_dir}",
            })
            return
        self._emit("recording_status", {"phase": "idle", "status": summary})

        # Only after splicing has safely pulled every completed take out
        # into its project — sync_and_maybe_prune can delete session_dir
        # entirely in "remote" vault mode, which splicing still needs to
        # read from above.
        from .vault import sync_and_maybe_prune
        sync_and_maybe_prune(
            config, session.session_dir,
            log=lambda msg: self._emit("recording_status", {"phase": "idle", "status": msg}),
        )

    def is_session_active(self) -> bool:
        return self._active_session is not None

    def _save_session_log(self, session: "_ActiveSession") -> Path | None:
        log_path = session.session_dir / "session_log.json"
        data = {
            "instrument": session.inst.name,
            "instrument_full_name": session.instrument_full_name,
            "musician": session.musician,
            "project": session.project.name,
            "studio_name": session.studio_name,
            "studio_location": session.studio_location,
            "sample_rate": session.engine.sample_rate,
            "mix_start_frame": session.mix_start_frame,
            "has_video": session.session_video_raw is not None,
            # filter-slot index -> the song actually drawn for it this
            # session (see _resolve_filter_slot) — the setlist itself
            # never records this, so post-processing needs it from here
            # to record a completed take into the shared vault-wide
            # inspiration-take index (vault.py) rather than the setlist.
            "filter_slot_draws": {
                str(index): {
                    "name": entry.name, "backing_track": entry.backing_track,
                    "duration_seconds": entry.duration_seconds,
                    "inspiration_track_id": entry.inspiration_track_id,
                }
                for index, entry in session.resolved_filter_picks.items()
            },
            "events": [e.to_dict() for e in session.events],
        }
        log_path.write_text(json.dumps(data, indent=2))
        return log_path
