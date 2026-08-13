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
from .config import DEFAULT_CONFIG_PATH, INSTRUMENT_LABELS, Instrument, StudioConfig
from .project import Project, Setlist, TakeInfo, TrackEntry
from .utils import ensure_dir, timestamp_now, wall_timestamp


class BackendError(Exception):
    """Raised by any Backend method on failure. Message is safe to show to the user."""


# The breather between a song ending naturally and the auto-advanced next
# one starting to play — long enough to reset hands, short enough that the
# session keeps its momentum. The session capture rolls straight through it.
AUTO_ADVANCE_GAP_SECONDS = 2.0

# "Train"'s two capture windows ("play your highest/lowest note") — long
# enough to average out a shaky start and settle on a steady pitch.
_INSTRUMENT_TRAIN_CAPTURE_SECONDS = 3.0


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
        doesn't already have a take for instrument_name's label (takes
        are filed by label — see TrackEntry.preferred_takes — so any
        instrument sharing it counts, not just instrument_name itself),
        or None if every remaining track already has one. Pure setlist
        query on top of get_setlist() — concrete here (not per-subclass)
        since it needs no hardware access and works identically for
        Local and Remote.

        The single shared "what's next" primitive every recording-driving
        context (Tk UI's StreamDeck Next key, headless takeloom server,
        the CLI) uses instead of each reimplementing this search."""
        setlist = Setlist.from_dict(self.get_setlist(project_name))
        label = self.get_config().label_for_instrument(instrument_name)
        for i in range(start_index, len(setlist.tracks)):
            if setlist.tracks[i].get_take_for_instrument(label) is None:
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

    @abstractmethod
    def get_filter_slot_previews(self, project_name: str) -> list[dict | None]:
        """Read-only preview of what each inspiration-filter slot in
        project_name's setlist would currently draw, one entry per
        setlist track in order (None for an ordinary, non-filter track).
        Each filter slot's entry is {"match_count": N, "next_up": {label:
        name_or_None}} — match_count is how many inspiration-server
        tracks currently match its criteria, and next_up is, for every
        configured instrument label, the song _resolve_filter_slot would
        currently pick for it (same reuse-preferring logic — see
        _pick_filter_match), without committing to anything. Meant to be
        called once when a project is opened in the UI (see record.py's
        Setlist panel) so the picks shown stay stable for the rest of
        that visit rather than re-randomizing on every setlist
        redisplay.

        Emits a "filter_preview_status" event ({"project_name", "index",
        "total", "label"}) just before each filter slot's own query, so a
        caller can show live progress across what can be a several-second
        call over a setlist with many filter slots."""
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
        exactly which take(s) *this session* produced for it — never a
        take some other session made for the same track/song, and never
        one of this session's own that's since been superseded by a later
        session's re-record, both of which a naive "whatever's currently
        filed for this track" lookup would wrongly include. Sourced from
        session_log.json's own "takes" snapshot (written by processing/
        splicer.py's process_session once it finishes splicing this
        session — one entry per take, in the same shape reassign_take/
        analyze_take's `instrument_name` param expects). Falls back to
        "whatever's currently filed for this track" — the old, no-longer-
        session-scoped behavior, with its stated risk — only for a session
        recorded before that snapshot existed. Each track's entry also
        reports whether it was an inspiration filter-slot draw
        (session_log.json's filter_slot_draws) — reassign_take/
        analyze_take both work on those the same as any other take (see
        reassign_take's docstring)."""
        ...

    @abstractmethod
    def correct_session_instrument(self, session_dir: str, new_instrument: str) -> None:
        """Fix the historical record alone: rewrite session_log.json's
        instrument/instrument_label fields (pulled from `new_instrument`
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
        """Re-file one specific take — the one currently sitting under
        `old_instrument` for `track_name` — under `new_instrument`
        instead: renames the take file(s) on disk, and re-keys it either
        in the project's setlist.json (an ordinary track), or in the
        shared vault-wide inspiration_takes.json index (a track drawn
        from an inspiration filter slot — see TrackEntry's docstring for
        why its take never lives on the setlist entry itself; session_
        dir's own session_log.json records exactly which shared-index
        entry a filter slot drew via filter_slot_draws, so this is just
        as reliable either way — same lookup get_session_detail/
        analyze_take already use). For an ordinary, non-filter track
        that's also inspiration-sourced, both the setlist entry and the
        shared index get updated, same as ever. Both `old_instrument` and
        `new_instrument` are instrument *labels* (one of config.
        INSTRUMENT_LABELS) — takes are filed by label, not by which
        specific piece of gear played them (see TrackEntry.
        preferred_takes) — not a particular Instrument's full_name.
        `old_instrument` is passed explicitly (typically whatever
        get_session_detail's `current_take` reported) rather than re-read
        from session_dir's session_log.json, so this gives the right
        answer regardless of whether correct_session_instrument has
        already been called on the same session. Raises BackendError if
        `new_instrument` isn't a recognized label, `track_name` isn't one
        of session_dir's tracks, or if there's no take currently filed
        under `old_instrument` to reassign."""
        ...

    @abstractmethod
    def analyze_take(self, session_dir: str, track_name: str, instrument_name: str) -> dict:
        """Run the take currently filed under `instrument_name` (a
        label — takes are filed by label, see TrackEntry.preferred_takes)
        for `track_name` through the frequency-based instrument
        classifier (audio/instrument_classifier.py's classify_audio_file)
        and report which configured instrument *label* its actual
        recorded audio most resembles — a read-only diagnostic behind the
        Sessions tab's "Analyze" button, to flag a take that may have
        been filed under the wrong label in the first place (the reason
        to reach for reassign_take). Works for an inspiration filter-slot
        draw too, same as reassign_take (looked up from the shared
        vault-wide inspiration-take index, same as get_session_detail).
        Narrows the comparison as precisely as the take's own recorded
        history allows, in order: if the take carries its own
        TakeInfo.input_label (the physical input it was actually recorded
        from, captured at record time — see _SessionEvent/CompletedTake),
        compares only against instruments currently on that exact input —
        the most precise scope, since it's an immutable fact about the
        take rather than derived from today's config, and survives the
        take's label being renamed or removed entirely. Failing that
        (an older take with no stored input_label), falls back to every
        instrument sharing a physical channel with one of `instrument_
        name`'s own label, if that label still resolves. Either way,
        narrowing to unrelated hardware inputs is avoided because it
        would reintroduce the classifier's bias toward whichever
        candidate has the widest default frequency range. If neither
        signal narrows anything (e.g. the label's since been renamed or
        removed *and* there's no stored input_label either) — precisely
        the take most worth analyzing, since there's no other way left to
        tell where it belongs — falls back to comparing against every
        currently configured instrument instead of refusing; the same
        bias caveat applies there with less precision, but a
        possibly-imprecise guess beats none. Returns {"guess": str |
        None, "confidence": float} — guess (a label) is None if the
        take's audio had no non-silent windows to analyze (e.g. it's
        silence). Never modifies anything. Raises BackendError if
        `track_name` isn't one of session_dir's tracks, there's no take
        currently filed under `instrument_name` (same as reassign_take),
        the take's file isn't available locally right now (e.g. pruned
        under "remote" vault mode), or no instruments are configured at
        all to compare against."""
        ...

    @abstractmethod
    def ensure_take_local(self, project_name: str, filename: str) -> str:
        """Make sure a specific take file (`filename`, relative to
        project_name's completed_takes_dir — as named in a get_session_
        detail/reassign_take/analyze_take take dict) actually exists on
        *this machine's* local disk, downloading it from the configured
        backup server if it doesn't (same reasoning as vault.
        ensure_setlist_files_local, just for one arbitrary already-named
        file rather than everything a setlist currently needs — a
        Sessions tab take can be one no project currently has as its
        *preferred* take at all, e.g. superseded by a later reassign_
        take). Returns the local absolute path as a string. Raises
        BackendError if it's not local and either no backup server is
        configured or the download fails.

        Local-only: on a Remote connection this would download to the
        *server's* disk, useless to a client that wants to actually play
        the file — RemoteBackend refuses outright; see play_take for the
        Remote-capable equivalent, which uses this method server-side as
        a step of its own, not by calling this one directly over the
        wire."""
        ...

    @abstractmethod
    def play_take(self, project_name: str, filename: str) -> None:
        """Open a specific take file (see ensure_take_local for what
        `filename` means and the local-availability guarantee this gives
        first) in the OS's default player, on whichever machine the
        caller is actually running on — the point being that a UI tab
        can call this identically whether app_state.backend is local or
        a Remote connection, and it Just Plays on the right machine
        either way, unlike ensure_take_local. Raises BackendError under
        the same conditions ensure_take_local does."""
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

    # --- instrument train (local-only; RemoteBackend refuses) ---
    #
    # Studio Setup's per-instrument "Train" button — see
    # audio/instrument_classifier.py for the underlying analysis. Opens
    # instrument_name's own configured input channel (not whatever's
    # "currently selected" elsewhere) and reports progress via
    # "instrument_test_status" events rather than a return value, since
    # it runs for a while and can finish several different ways
    # (trained / stopped).

    @abstractmethod
    def start_instrument_train(self, instrument_name: str) -> None:
        """Guided calibration: opens instrument_name's own input channel
        and walks through two timed capture windows — phase "train_high"
        ("play your highest note"), then "train_low" ("play your lowest
        note") — estimating each note's fundamental frequency (see
        audio/instrument_classifier.py's estimate_pitch) via
        "instrument_test_status" events at each phase change. Finishes by
        emitting phase "trained" carrying freq_min_hz/freq_max_hz in the
        event data — this never writes them to config itself, the caller
        (Studio Setup) fills the instrument row's fields in from the
        event and Save persists it like any other edit, same as every
        other field on that tab. Raises BackendError immediately if a
        session, video check, latency test, another instrument train, or
        a detect-all run is already active — same mutual exclusion as
        those."""
        ...

    @abstractmethod
    def stop_instrument_test(self) -> None:
        """Cancel a running start_instrument_train, if one is — a no-op,
        not an error, if none is. Emits "instrument_test_status" with
        phase "idle" only when it actually stopped something."""
        ...

    # --- detect-all (local-only; RemoteBackend refuses) ---
    #
    # Studio Setup's single "Detect" button, above the instrument table
    # rather than next to any one row — opens every configured
    # instrument's own channel at once (grouped by physical device) and
    # listens on each. A channel with only one instrument assigned to it
    # is unambiguous — any signal on it is that instrument, no frequency
    # guessing needed. A channel shared by more than one instrument (e.g.
    # bass and electric guitar wired through the same DI, swapped between
    # takes) is told apart by an InstrumentClassifier (audio/instrument_
    # classifier.py) scoped to just the instruments actually on that
    # channel — unlike the old per-instrument Detect, which compared
    # every configured instrument's frequency band against whatever
    # channel was under test and so was biased toward whichever unrelated
    # instrument elsewhere had the widest default range. Meant to be left
    # running while the performer walks around playing each instrument in
    # turn, so — unlike start_instrument_train — it does not stop itself
    # once something is detected.

    @abstractmethod
    def start_detect_all(self) -> None:
        """Opens every configured instrument's own input channel at once
        and starts listening. Emits "detect_all_status" events:

        - phase "started" right away (its `status` notes any instrument
          skipped because its input isn't available right now, e.g. the
          interface is powered off)
        - phase "detected", with `instrument` set, whenever an
          instrument's channel (or, for a channel shared by several
          instruments, the frequency classifier's current best guess
          among just those) reports a match — order and timing depend
          entirely on what the performer plays. Won't re-fire for the
          same instrument back-to-back (see InstrumentClassifier's own
          docstring), but a channel going quiet (see "channel" below)
          resets that, so the same instrument confirmed again after a
          real gap does re-fire — there's no separate "un-detected"
          signal for a specific instrument, only the channel-level one
          below.
        - phase "channel", with `input_label` and `active` (bool), every
          time a channel's live signal crosses the (silence-gated, ~0.5s-
          held) threshold from quiet to playing or back — independent of
          whether anything's been identified on it yet. This is what
          lets a caller (see ui/detect_test.py's `takeloom detect-test`
          window, the only current caller) turn an indicator back off
          once the performer stops playing, which "detected" alone can't
          do.
        - phase "stats", with `input_label`, `min_hz`, `max_hz`,
          `polyphony`, and `peak_hz` (every individual fundamental found,
          ascending — `polyphony` is just its length), roughly every
          SpectralStatsTracker window (audio/instrument_classifier.py) of
          non-silent audio on a channel — raw frequency content,
          unrelated to whether an instrument's been identified. Behind
          detect-test's "Currently Detected" stats panel and its spectrum
          indicator.

        Runs until stop_detect_all() or the caller's own window closing.
        Raises BackendError immediately if a session, video check,
        latency test, or an instrument train is already active — same
        mutual exclusion as those — or if no instrument's input can
        currently be opened at all."""
        ...

    @abstractmethod
    def stop_detect_all(self) -> None:
        """Stop a running start_detect_all, if one is — a no-op, not an
        error, if none is. Emits "detect_all_status" with phase "stopped"
        only when it actually stopped something."""
        ...

    # --- auto-detect instrument (remote-capable, unlike everything else
    # in this section) ---
    #
    # The Record tab no longer has a manual Instrument dropdown — this is
    # what replaces it. Structurally the same scan as start_detect_all
    # (every configured instrument's own channel, listened to at once,
    # shared channels told apart by a scoped InstrumentClassifier) but
    # with different intent: rather than running indefinitely for a
    # human to walk around and confirm each instrument in turn, this
    # locks onto and reports exactly the first instrument any channel's
    # classifier commits to, tears every stream down, and finishes — a
    # one-shot "which instrument is this session going to be" resolution
    # that then feeds start_recording/begin_session's existing
    # instrument_name parameter unchanged. For now a session still has
    # exactly one instrument, decided once before Record is pressed —
    # there's no mid-session re-detection yet.
    #
    # Unlike start_detect_all/start_instrument_train (Studio Setup tools
    # for someone standing at the machine with the hardware), this has to
    # work from a laptop in Remote mode too, since that's the normal way
    # a session actually gets recorded (see CLAUDE.md) — so both methods
    # are real RPC-backed Backend operations, not "RemoteBackend refuses"
    # stubs. Progress reaches a remote caller the same way start_
    # recording's already does: the call itself only kicks the scan
    # off/stops it, everything else (including the final "detected")
    # arrives via "auto_detect_status" events broadcast to every
    # connected client, local or remote, same as any other backend event.

    @abstractmethod
    def start_auto_detect_instrument(self) -> None:
        """Opens every configured instrument's own input channel at once
        (same scan as start_detect_all) and starts listening. Emits
        "auto_detect_status" events: phase "listening" right away (its
        `status` notes any instrument skipped because its input isn't
        available right now), then phase "detected" — with `instrument`,
        `full_name`, and `label` set — the moment any channel's
        classifier commits to a match, at which point every stream is
        torn down, config.last_selected_instrument is updated to match
        (same as manually picking it used to do), and ambient monitoring
        resumes on that instrument's channel. Unlike start_detect_all,
        this stops itself after the first detection rather than running
        indefinitely. Raises BackendError immediately if a session, video
        check, latency test, instrument train, or a detect-all run is
        already active — same mutual exclusion as those — or if no
        instrument's input can currently be opened at all."""
        ...

    @abstractmethod
    def stop_auto_detect_instrument(self) -> None:
        """Cancel a running start_auto_detect_instrument before it's
        found anything — a no-op, not an error, if none is running (in
        particular, calling this after a "detected" event has already
        fired is always a no-op, since the run already finished and tore
        itself down at that point). Emits "auto_detect_status" with phase
        "stopped" only when it actually stopped something."""
        ...

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
    # Whichever instrument was active when this event was logged — every
    # event carries its own copy rather than relying on session_log.json's
    # single top-level instrument/instrument_label fields, so that
    # processing/splicer.py's parse_session_log can tell which instrument
    # recorded which take without assuming one instrument for the whole
    # session. Constant across a session's events for now (no mid-session
    # instrument switching yet — see backend.py's Record-tab auto-detect),
    # but logged per-event so that when switching does land, no further
    # session-log schema change is needed. `instrument` is the Instrument's
    # full_name (manufacturer/model) — its only identifying field, see
    # config.py's Instrument.
    instrument: str = ""
    instrument_label: str = ""
    # Which physical input (an InputLabel.label) actually recorded this —
    # same per-event-carries-its-own-copy reasoning as instrument/
    # instrument_label above. This is what ends up on each completed
    # take's own TakeInfo.input_label (see processing/splicer.py's
    # CompletedTake/process_session), letting analyze_take precisely
    # scope its comparison even once a take's label has since been
    # renamed or removed from config — see that method's docstring.
    input_label: str = ""

    def to_dict(self) -> dict:
        d = {
            "timestamp": self.timestamp, "wall_time": self.wall_time,
            "event_type": self.event_type, "details": self.details,
            "instrument": self.instrument, "instrument_label": self.instrument_label,
            "input_label": self.input_label,
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


@dataclass
class _ActiveInstrumentTest:
    """A Studio Setup "Detect"/"Train" run — see Backend.start_instrument_
    detect/start_instrument_train. stop_event is set the moment the test
    ends for any reason (detected/trained/timed out/explicitly stopped),
    so a background phase's late-arriving result (e.g. a capture window
    that was already mid-flight when Stop was clicked) knows to discard
    itself instead of emitting a stale status or advancing to a phase
    that no longer has an engine to listen on."""
    engine: object
    instrument_name: str
    stop_event: threading.Event


@dataclass
class _ActiveDetectAll:
    """A Studio Setup "Detect" run across every configured instrument at
    once — see Backend.start_detect_all. One raw sd.InputStream per
    distinct resolved input device (instruments sharing a device share
    its stream, each read from its own channel within it) rather than an
    AudioEngine per instrument, since this needs no playback/mixing, just
    listening. stop_event is set the moment the run ends, so a channel's
    late-arriving detection (already mid-flight when Stop was clicked)
    knows to discard itself instead of emitting a stale event."""
    streams: list
    stop_event: threading.Event


@dataclass
class _ActiveAutoDetect:
    """The Record tab's "no instrument dropdown anymore" auto-detect run
    — see Backend.start_auto_detect_instrument. Structurally the same as
    _ActiveDetectAll (raw per-device sd.InputStreams, no engine) but with
    different intent: this one locks onto and reports exactly one
    instrument the moment any channel's classifier commits to a match,
    then tears itself down — it's a one-shot "which instrument is this
    session going to be" resolution, not Studio Setup's leave-it-running
    walk-around-and-confirm-everything tool. Kept as a separate dataclass
    (and separate mutual-exclusion slot) rather than reusing
    _ActiveDetectAll so the two features can't be confused for each other
    or accidentally torn down by the wrong stop_*/guard path."""
    streams: list
    stop_event: threading.Event


class LocalBackend(Backend):
    """Direct local implementation — talks to this machine's config, disk,
    and audio/video hardware. Historical RecordFrame behavior, unchanged."""

    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self._config_path = config_path
        self._event_callbacks: list[EventCallback] = []
        self._record_lock = threading.Lock()
        self._active_latency_test: _ActiveLatencyTest | None = None
        self._active_video_check: _ActiveVideoCheck | None = None
        self._active_instrument_test: _ActiveInstrumentTest | None = None
        self._active_detect_all: _ActiveDetectAll | None = None
        self._active_auto_detect: _ActiveAutoDetect | None = None
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
        if self._active_instrument_test is not None:
            return self._active_instrument_test.engine
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

    def get_filter_slot_previews(self, project_name: str) -> list[dict | None]:
        config = self.get_config()
        project = self._open_project(project_name)
        labels: list[str] = []
        for inst in config.instruments:
            if inst.label and inst.label not in labels:
                labels.append(inst.label)

        from .inspiration import InspirationError, build_inspiration_track_entry, search_tracks_by_filter
        from .vault import load_inspiration_index, vault_root

        total = sum(1 for t in project.setlist.tracks if t.is_inspiration_filter)
        index = None  # lazily loaded — only needed once any filter slot is actually hit
        previews: list[dict | None] = []
        checked = 0
        for track in project.setlist.tracks:
            if not track.is_inspiration_filter:
                previews.append(None)
                continue
            checked += 1
            # This call runs on its own request thread (see remote/server.py)
            # and can involve several seconds of inspiration-server round
            # trips across a whole setlist's worth of filter slots — this
            # event is what lets the UI show a live "checking N of M" status
            # (and know when to put up/take down its loading overlay — see
            # record.py's _show_loading_overlay) instead of just staring at
            # a stalled-looking Setlist panel for however long it takes.
            self._emit(
                "filter_preview_status",
                {"project_name": project_name, "index": checked, "total": total, "label": track.name},
            )
            try:
                matches = search_tracks_by_filter(config, track.inspiration_filter)
            except InspirationError:
                previews.append({"match_count": 0, "next_up": {label: None for label in labels}})
                continue
            if not matches:
                previews.append({"match_count": 0, "next_up": {label: None for label in labels}})
                continue
            if index is None:
                index = load_inspiration_index(vault_root(config))
            next_up = {}
            for label in labels:
                chosen = self._pick_filter_match(matches, label, index)
                next_up[label] = build_inspiration_track_entry(chosen).name
            previews.append({"match_count": len(matches), "next_up": next_up})
        return previews

    # --- sessions (browse/correct past recordings) ---

    def _local_session_dir_path(self, session_dir: str) -> Path | None:
        """session_dir's local directory, or None if it isn't (or isn't
        currently) present on local disk — which is normal and expected,
        not an error, once session_vault_mode "remote" has pushed and
        pruned it (see vault.sync_and_maybe_prune). Callers needing its
        session_log.json either way should go through _read_session_log,
        which falls back to the backup server transparently."""
        from .vault import vault_root
        path = vault_root(self.get_config()) / "sessions" / session_dir
        return path if path.is_dir() else None

    def _read_session_log(self, session_dir: str) -> tuple[Path | None, dict]:
        """session_dir's session_log.json, from local disk if it's there,
        else fetched fresh from the backup server (see sync.
        fetch_remote_session_log) if session_vault_mode allows it —
        never written to local disk in that case, so there's nothing
        left behind to double as a second, possibly-stale copy; the
        vault (wherever it currently lives) stays the only copy. Returns
        (log_path, data) — log_path is None for a remote-only session,
        which correct_session_instrument checks to decide whether to
        write the correction back to disk or over SSH instead."""
        local_dir = self._local_session_dir_path(session_dir)
        if local_dir is not None:
            log_path = local_dir / "session_log.json"
            if not log_path.exists():
                raise BackendError(f"Session '{session_dir}' has no session_log.json.")
            try:
                data = json.loads(log_path.read_text())
            except json.JSONDecodeError as e:
                raise BackendError(f"Session '{session_dir}' has a corrupt session_log.json: {e}") from e
            return log_path, data

        config = self.get_config()
        if config.session_vault_mode in ("remote", "both") and config.backup_server:
            from .sync import fetch_remote_session_log
            data = fetch_remote_session_log(config.backup_server, session_dir)
            if data is not None:
                return None, data
        raise BackendError(f"Session '{session_dir}' not found.")

    def list_sessions(self) -> list[dict]:
        """Every past session this machine can currently see: whatever's
        on local disk, plus — if session_vault_mode allows a backup
        server — whatever's on the backup server that isn't already
        accounted for locally (the common case in "remote" mode: vault.
        sync_and_maybe_prune deletes each session's local directory
        right after syncing it, so almost everything normally lives only
        there). Local always wins on a name collision — vault.py's own
        migration/collision-avoidance already guarantees names don't
        collide in practice, but preferring local (the copy this machine
        can actually still write to) is the safer tiebreak regardless."""
        config = self.get_config()
        from .vault import vault_root
        sessions_dir = vault_root(config) / "sessions"
        results = []
        seen = set()
        if sessions_dir.exists():
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
                results.append(self._session_summary(session_dir.name, data))
                seen.add(session_dir.name)

        if config.session_vault_mode in ("remote", "both") and config.backup_server:
            from .sync import fetch_remote_session_logs
            for name, data in fetch_remote_session_logs(config.backup_server).items():
                if name in seen:
                    continue
                results.append(self._session_summary(name, data))

        results.sort(key=lambda s: s["date"], reverse=True)
        return results

    @staticmethod
    def _session_summary(session_dir_name: str, data: dict) -> dict:
        events = data.get("events", [])
        track_names = []
        for e in events:
            name = e.get("track_name")
            if name and name not in track_names:
                track_names.append(name)
        return {
            "session_dir": session_dir_name,
            # events[0]'s wall_time (real, human-typed timestamp) rather
            # than parsed back out of the directory name, which is
            # filename-sanitized and thus lossy/ambiguous.
            "date": events[0]["wall_time"] if events else "",
            "project": data.get("project", ""),
            "instrument": data.get("instrument", ""),
            "track_names": track_names,
        }

    def get_session_detail(self, session_dir: str) -> dict:
        _, data = self._read_session_log(session_dir)
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

        config = self.get_config()
        filter_slot_draws = data.get("filter_slot_draws", {})
        from .vault import get_inspiration_entry, vault_root
        root = vault_root(config)

        # This session's own snapshot of exactly which take(s) it produced,
        # per track_index — written by processing/splicer.py's
        # process_session once it finishes splicing. Absent only for a
        # session recorded before this existed, in which case there's no
        # record of what specifically got made here, so this falls back to
        # whatever's *currently* filed for the track (which does risk
        # showing another session's take, or missing one of this session's
        # own that's since been superseded — the very reason the snapshot
        # exists now).
        session_takes = data.get("takes")

        tracks = []
        for name in track_names:
            track_index = track_index_by_name.get(name)
            is_filter_draw = track_index in filter_slot_indices
            if session_takes is not None:
                takes = session_takes.get(str(track_index), [])
            elif is_filter_draw:
                # A filter slot's own TrackEntry never holds a take (see
                # TrackEntry's docstring) — what this session actually
                # drew and recorded lives in the shared vault-wide
                # inspiration-take index instead, keyed by the song's
                # inspiration_track_id (filter_slot_draws[track_index]).
                draw_info = filter_slot_draws.get(str(track_index))
                track_id = draw_info.get("inspiration_track_id") if draw_info else None
                shared = get_inspiration_entry(root, track_id) if track_id else None
                takes = [
                    {"instrument": take_instrument, **asdict(take)}
                    for take_instrument, take in shared.preferred_takes.items()
                ] if shared is not None else []
            elif project is not None:
                entry = next((t for t in project.setlist.tracks if t.name == name), None)
                takes = [
                    {"instrument": take_instrument, **asdict(take)}
                    for take_instrument, take in entry.preferred_takes.items()
                ] if entry is not None else []
            else:
                takes = []
            tracks.append({"track_name": name, "is_filter_draw": is_filter_draw, "takes": takes})

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
        data["instrument"] = inst.full_name
        data["instrument_label"] = inst.label
        if log_path is not None:
            log_path.write_text(json.dumps(data, indent=2))
            return
        # Remote-only session (see _read_session_log) — write the
        # correction back over SSH instead of to a local file, so the
        # backup server stays the one true copy rather than this machine
        # quietly growing a local fork of it.
        from .sync import write_remote_session_log
        if not write_remote_session_log(config.backup_server, session_dir, data):
            raise BackendError(f"Could not write the correction back to {config.backup_server}.")

    def _move_take_file(
        self, project: Project, track_name: str, new_instrument: str, take: TakeInfo, source: str,
        backing_track: str,
    ) -> TakeInfo:
        """Rename a completed take's audio (and video, if present) file(s)
        on disk to new_instrument's naming convention, returning the new
        TakeInfo — reassign_take's two cases (an ordinary setlist track,
        and a track drawn from an inspiration filter slot) do this
        identical file move; only where the resulting TakeInfo then gets
        stored differs."""
        from .utils import next_take_number, take_filename
        old_stem = Path(take.filename).stem
        old_audio_path = project.completed_takes_dir / take.filename
        old_video_path = project.completed_takes_dir / f"{old_stem}.mp4"
        ext = Path(take.filename).suffix.lstrip(".") or "flac"

        new_take_number = next_take_number(project.completed_takes_dir, track_name, new_instrument)
        new_filename = take_filename(track_name, new_instrument, new_take_number, source, backing_track, ext)
        new_stem = Path(new_filename).stem
        new_audio_path = project.completed_takes_dir / new_filename
        new_video_path = project.completed_takes_dir / f"{new_stem}.mp4"

        if old_audio_path.exists():
            shutil.move(str(old_audio_path), str(new_audio_path))
        has_video = take.has_video and old_video_path.exists()
        if has_video:
            shutil.move(str(old_video_path), str(new_video_path))

        return TakeInfo(
            instrument=new_instrument, take_number=new_take_number, filename=new_filename,
            volume=take.volume, has_video=has_video, input_label=take.input_label,
        )

    def reassign_take(self, session_dir: str, track_name: str, old_instrument: str, new_instrument: str) -> None:
        config = self.get_config()
        if new_instrument not in INSTRUMENT_LABELS:
            raise BackendError(f"'{new_instrument}' isn't a recognized instrument label.")
        _, data = self._read_session_log(session_dir)
        filter_slot_draws = data.get("filter_slot_draws", {})

        track_index = None
        for e in data.get("events", []):
            if e.get("track_name") == track_name:
                track_index = e.get("track_index")
                break
        if track_index is None:
            raise BackendError(f"Track '{track_name}' wasn't touched by session '{session_dir}'.")

        project = self._open_project(data.get("project", ""))
        from .vault import load_inspiration_index, save_inspiration_index, vault_root
        root = vault_root(config)

        if str(track_index) in filter_slot_draws:
            # Drawn from an inspiration filter slot — its take isn't filed
            # on any TrackEntry in the setlist (a filter slot's own entry
            # never holds one, see TrackEntry's docstring); it lives in the
            # shared vault-wide inspiration-take index instead, keyed by
            # exactly which song this session drew — filter_slot_draws
            # records that, same lookup get_session_detail/analyze_take
            # already use to find it reliably.
            track_id = filter_slot_draws[str(track_index)].get("inspiration_track_id")
            index = load_inspiration_index(root)
            shared = index.get(str(track_id)) if track_id else None
            if shared is None:
                raise BackendError(f"No shared inspiration record found for '{track_name}'.")
            take = shared.get_take_for_instrument(old_instrument)
            if take is None:
                raise BackendError(f"No take is currently filed under '{old_instrument}' for '{track_name}'.")

            new_take = self._move_take_file(
                project, track_name, new_instrument, take,
                source="inspiration", backing_track=f"inspiration_{track_id}",
            )
            del shared.preferred_takes[old_instrument]
            shared.set_preferred_take(new_instrument, new_take)
            save_inspiration_index(root, index)
            return

        entry = next((t for t in project.setlist.tracks if t.name == track_name), None)
        if entry is None:
            raise BackendError(f"Track '{track_name}' no longer exists in project '{project.name}'.")
        take = entry.get_take_for_instrument(old_instrument)
        if take is None:
            raise BackendError(f"No take is currently filed under '{old_instrument}' for '{track_name}'.")

        new_take = self._move_take_file(
            project, track_name, new_instrument, take,
            source=entry.source_label(), backing_track=entry.backing_track,
        )
        del entry.preferred_takes[old_instrument]
        entry.set_preferred_take(new_instrument, new_take)
        project.save_setlist()

        if entry.inspiration_track_id:
            # Non-filter inspiration-sourced track: splicer.py mirrors its
            # take into the shared vault-wide index alongside the setlist's
            # own preferred_takes (see process_session) — keep both in
            # sync here too, so another project referencing the same song
            # doesn't keep offering the take under the old instrument.
            index = load_inspiration_index(root)
            shared_entry = index.get(str(entry.inspiration_track_id))
            if shared_entry is not None and old_instrument in shared_entry.preferred_takes:
                del shared_entry.preferred_takes[old_instrument]
                shared_entry.set_preferred_take(new_instrument, new_take)
                save_inspiration_index(root, index)

    def analyze_take(self, session_dir: str, track_name: str, instrument_name: str) -> dict:
        config = self.get_config()
        _, data = self._read_session_log(session_dir)
        project = self._open_project(data.get("project", ""))

        track_index = None
        for e in data.get("events", []):
            if e.get("track_name") == track_name:
                track_index = e.get("track_index")
                break
        filter_slot_draws = data.get("filter_slot_draws", {})

        take = None
        if track_index is not None and str(track_index) in filter_slot_draws:
            # Same reasoning as get_session_detail: a filter slot's own
            # TrackEntry never holds a take — look the drawn song up in
            # the shared vault-wide inspiration-take index instead.
            from .vault import get_inspiration_entry, vault_root
            track_id = filter_slot_draws[str(track_index)].get("inspiration_track_id")
            shared = get_inspiration_entry(vault_root(config), track_id) if track_id else None
            take = shared.get_take_for_instrument(instrument_name) if shared is not None else None
        else:
            entry = next((t for t in project.setlist.tracks if t.name == track_name), None)
            if entry is None:
                raise BackendError(f"Track '{track_name}' no longer exists in project '{project.name}'.")
            take = entry.get_take_for_instrument(instrument_name)
        if take is None:
            raise BackendError(f"No take is currently filed under '{instrument_name}' for '{track_name}'.")

        take_path = project.completed_takes_dir / take.filename
        if not take_path.exists():
            # Distinct from the audio-analyzed-but-inconclusive case
            # below (also {"guess": None}) — this one's a file that
            # simply isn't here right now (e.g. pruned locally under
            # "remote" vault mode), not something classify_audio_file
            # ever got to look at.
            raise BackendError(f"'{take.filename}' isn't available locally right now.")

        # Narrow the comparison as precisely as possible, in order of how
        # trustworthy each signal is — comparing against unrelated
        # hardware inputs (e.g. a guitar's DI against a piano on a
        # different device entirely) can't be what actually got recorded
        # here regardless of what the audio sounds like, and would
        # reintroduce the classifier's bias toward whichever candidate has
        # the widest default frequency range (see audio/instrument_
        # classifier.py's module docstring and _DEFAULT_RANGES_BY_LABEL).
        if take.input_label:
            # take.input_label is the physical input this take was
            # *actually* recorded from, captured at record time (see
            # backend.py's _SessionEvent/_save_session_log and processing/
            # splicer.py's CompletedTake) — an immutable fact about the
            # take itself, so this is the most precise possible scope and
            # survives instrument_name's label being renamed or removed
            # from config entirely since the take was recorded.
            candidates = [i for i in config.instruments if i.input_label == take.input_label]
        else:
            # Older take, recorded before takes carried their own
            # input_label — fall back to the label-based heuristic: every
            # instrument currently sharing a physical channel with any
            # instrument of instrument_name's own label (empty if
            # instrument_name doesn't match anything currently configured
            # either).
            label_instruments = [i for i in config.instruments if i.label == instrument_name]
            input_labels = {i.input_label for i in label_instruments}
            candidates = [i for i in config.instruments if i.input_label in input_labels]
        if not candidates:
            # Nothing currently configured shares the take's actual
            # (or label-inferred) input — exactly the take that most needs
            # analysis, not less: refusing outright leaves no way to find
            # out where it actually belongs. Fall back to comparing
            # against every currently configured instrument instead — the
            # bias caveat above still applies with less precision, but a
            # possibly-imprecise guess beats no guess at all here.
            candidates = list(config.instruments)
        if not candidates:
            raise BackendError("No instruments are currently configured to compare this take's audio against.")

        from .audio.instrument_classifier import classify_audio_file
        guess, confidence = classify_audio_file(take_path, candidates)
        return {"guess": guess, "confidence": confidence}

    def ensure_take_local(self, project_name: str, filename: str) -> str:
        config = self.get_config()
        project = self._open_project(project_name)
        local_path = project.completed_takes_dir / filename
        if local_path.exists():
            return str(local_path)
        if not config.backup_server:
            raise BackendError(f"'{filename}' isn't available locally, and no backup server is configured.")
        from .sync import sync_vault_file_down
        if not sync_vault_file_down(config.backup_server, f"completed_takes/{filename}", local_path):
            raise BackendError(f"Could not download '{filename}' from {config.backup_server}.")
        return str(local_path)

    def play_take(self, project_name: str, filename: str) -> None:
        path = self.ensure_take_local(project_name, filename)
        from .video.capture import open_in_default_player
        open_in_default_player(Path(path))

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
            or self._active_instrument_test is not None
            or self._active_detect_all is not None
            or self._active_auto_detect is not None
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
                if self._active_monitor.inst.full_name.lower() == (config.last_selected_instrument or "").lower():
                    return True  # already monitoring the right thing
                self._close_active_monitor()
            return self._start_monitoring_locked()

    def start_recording(self, req: StartRecordingRequest) -> None:
        with self._record_lock:
            if (
                self._active_latency_test is not None or self._active_video_check is not None
                or self._active_instrument_test is not None or self._active_detect_all is not None
                or self._active_auto_detect is not None
            ):
                raise BackendError("Another recording is already in progress.")

            config = self.get_config()
            inst = config.get_instrument(req.instrument_name)
            if inst is None:
                raise BackendError(f"Instrument '{req.instrument_name}' not found.")

            session = self._active_session
            if session is not None and (
                session.project.name != req.project_name or session.inst.full_name.lower() != inst.full_name.lower()
            ):
                raise BackendError(
                    f"A session is active for '{session.inst.full_name}' in '{session.project.name}' — "
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
            if other_inst.lower() == session.inst.label.lower():
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
        already recorded a take on (but not yet for this instrument's
        label — takes are filed by label, see TrackEntry.preferred_takes,
        so any instrument sharing it counts) — via the shared vault-wide
        inspiration-take index (vault.py), not just this project's own
        history — so a later instrument can layer onto the same song
        instead of the setlist only ever accumulating unrelated one-off
        takes. Falls back to a genuinely random pick among every match
        when none qualify. Either way, the setlist itself never gains a
        new entry here — see TrackEntry's docstring and _resolve_filter_
        slot_for_session's caching wrapper, which is what actually gets
        called during a session; this is the pure "pick one" step, split
        out for testability.

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

        from .vault import load_inspiration_index, vault_root
        index = load_inspiration_index(vault_root(config))
        label = config.label_for_instrument(instrument_name)
        chosen = self._pick_filter_match(matches, label, index, exclude_id=exclude_id)
        return build_inspiration_track_entry(chosen)

    @staticmethod
    def _pick_filter_match(
        matches: list[dict], label: str, index: dict, exclude_id: int | None = None,
    ) -> dict:
        """The actual "which song" choice within `matches` for `label`,
        split out of _resolve_filter_slot so get_filter_slot_previews can
        reuse the exact same reuse-preferring logic per label without
        re-querying the inspiration server or reloading the vault index
        for each one. Prefers a match some other instrument has already
        recorded a take for (but not yet under `label`), falling back to
        a genuinely random pick among every match when none qualify —
        see _resolve_filter_slot's own docstring for why."""
        candidates = [m for m in matches if m.get("id") != exclude_id] or matches
        reusable = []
        for m in candidates:
            shared = index.get(str(m.get("id")))
            if shared is not None and shared.preferred_takes and shared.get_take_for_instrument(label) is None:
                reusable.append(m)
        return random.choice(reusable) if reusable else random.choice(candidates)

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
        resolved = self._resolve_filter_slot(config, track, session.inst.full_name)
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
        still needs a take for the session's instrument's label (takes
        are filed by label — see TrackEntry.preferred_takes — so any
        instrument sharing it counts) — skipping both tracks with takes
        already on disk and ones completed earlier in this same session
        (the setlist doesn't learn about those until post-processing).
        Returns the loaded track (resolved, if the setlist position found
        is a filter slot — see _resolve_filter_slot_for_session), or None
        (emitting a "waiting" status) when nothing's left. Called with
        self._record_lock held; playback must already be stopped."""
        tracks = session.project.setlist.tracks
        start = (session.current_track_index + 1) if session.current_track_index is not None else 0
        config = self.get_config()
        label = session.inst.label
        index = None
        for i in range(start, len(tracks)):
            if i in session.completed_track_indices:
                continue
            if tracks[i].get_take_for_instrument(label) is None:
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
            resolved = self._resolve_filter_slot(config, slot, session.inst.full_name, exclude_id=exclude_id)
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
            return session.project.name, session.inst.full_name, session.current_track_index

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
                or self._active_instrument_test is not None
                or self._active_detect_all is not None
                or self._active_auto_detect is not None
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
                    f"Instrument '{inst.full_name}' needs input channel {input_info.channel} "
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

    # --- instrument train (local-only; RemoteBackend refuses) ---

    def _open_instrument_test_engine(self, instrument_name: str):
        """Shared setup for start_instrument_train: resolves instrument_
        name's own configured input channel (same resolution/validation
        as _begin_session_locked) and opens a bare AudioEngine on it —
        no recorder, no session, just enough of a live stream for
        instrument_classifier analysis and for the performer to hear
        themselves through the normal output device. Returns (inst,
        engine); raises BackendError on the usual missing-instrument/
        device/channel problems."""
        config = self.get_config()
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
        in_dev = resolve_device(sd, input_info.device, "input")
        if in_dev is None:
            raise BackendError(f"Input device '{input_info.device}' not found.")
        out_dev = resolve_device(sd, config.output_device, "output")
        if config.output_device and out_dev is None:
            raise BackendError(f"Output device '{config.output_device}' not found.")

        in_info = sd.query_devices(in_dev, "input")
        if input_info.channel > in_info["max_input_channels"]:
            raise BackendError(
                f"Instrument '{inst.full_name}' needs input channel {input_info.channel} "
                f"but device only has {in_info['max_input_channels']} channels."
            )
        out_info = sd.query_devices(out_dev, "output")
        output_channels = min(config.output_channels, out_info["max_output_channels"])

        from .audio.engine import AudioEngine
        engine = AudioEngine(
            sample_rate=config.sample_rate, buffer_size=config.buffer_size,
            input_device=in_dev, output_device=out_dev,
            input_channels=max(input_info.channel, 1), output_channels=max(1, output_channels),
            monitor_channel=input_info.channel - 1, compressor_settings=self._compressor_settings,
        )
        engine.start()
        return inst, engine

    def _begin_instrument_test_locked(self, instrument_name: str):
        """Caller must hold self._record_lock. Guards against every other
        recording-shaped activity, opens the engine, and registers
        self._active_instrument_test. Returns (inst, engine, stop_event)."""
        if (
            self._active_session is not None or self._active_video_check is not None
            or self._active_latency_test is not None or self._active_instrument_test is not None
            or self._active_detect_all is not None
            or self._active_auto_detect is not None
        ):
            raise BackendError("Another recording is already in progress.")
        self._close_active_monitor()  # always opens its own engine, never reuses the ambient one
        inst, engine = self._open_instrument_test_engine(instrument_name)
        stop_event = threading.Event()
        self._active_instrument_test = _ActiveInstrumentTest(
            engine=engine, instrument_name=inst.full_name, stop_event=stop_event,
        )
        return inst, engine, stop_event

    def _end_instrument_test_locked(self) -> _ActiveInstrumentTest | None:
        """Caller must hold self._record_lock. Tears the engine down and
        clears self._active_instrument_test — every termination path
        (detected/trained/timed out/explicitly stopped) goes through this
        exactly once. Returns what was active, or None if nothing was
        (the no-op case for stop_instrument_test)."""
        active = self._active_instrument_test
        if active is None:
            return None
        self._active_instrument_test = None
        active.stop_event.set()
        active.engine.set_instrument_sink(None)
        active.engine.stop()
        self._start_monitoring_locked()
        return active

    def stop_instrument_test(self) -> None:
        with self._record_lock:
            active = self._end_instrument_test_locked()
        if active is not None:
            self._emit("instrument_test_status", {"phase": "idle", "status": "Stopped."})

    def start_instrument_train(self, instrument_name: str) -> None:
        from .audio.instrument_classifier import NoteCapture
        with self._record_lock:
            inst, engine, stop_event = self._begin_instrument_test_locked(instrument_name)
            self._emit("instrument_test_status", {
                "phase": "train_high", "instrument": inst.full_name,
                "status": f"Play the HIGHEST note '{inst.full_name}' can play, and hold it...",
            })

            def on_low_captured(high_hz: float | None, low_hz: float | None) -> None:
                with self._record_lock:
                    active = self._end_instrument_test_locked()
                if active is None:
                    return  # stopped in the meantime
                if high_hz is None or low_hz is None:
                    self._emit("instrument_test_status", {
                        "phase": "idle", "instrument": inst.full_name,
                        "status": "Couldn't hear a clear note — try again, closer to the mic/pickup.",
                    })
                    return
                freq_min, freq_max = min(high_hz, low_hz), max(high_hz, low_hz)
                self._emit("instrument_test_status", {
                    "phase": "trained", "instrument": inst.full_name,
                    "status": f"Trained: {freq_min:.0f}\N{EN DASH}{freq_max:.0f} Hz.",
                    "freq_min_hz": freq_min, "freq_max_hz": freq_max,
                })

            def on_high_captured(high_hz: float | None) -> None:
                with self._record_lock:
                    still_active = self._active_instrument_test is not None and not stop_event.is_set()
                if not still_active:
                    return  # stopped in the meantime
                self._emit("instrument_test_status", {
                    "phase": "train_low", "instrument": inst.full_name,
                    "status": f"Got it. Now play the LOWEST note '{inst.full_name}' can play, and hold it...",
                })
                low_capture = NoteCapture(
                    engine.sample_rate, _INSTRUMENT_TRAIN_CAPTURE_SECONDS,
                    lambda low_hz: on_low_captured(high_hz, low_hz),
                )
                engine.set_instrument_sink(low_capture.process_block)

            high_capture = NoteCapture(engine.sample_rate, _INSTRUMENT_TRAIN_CAPTURE_SECONDS, on_high_captured)
            engine.set_instrument_sink(high_capture.process_block)

    # --- Detect-all (local-only; RemoteBackend refuses) ---

    def _open_channel_classifier_streams(
        self, config: StudioConfig, on_channel_detected, on_channel_active=None, on_channel_stats=None,
    ) -> tuple[list, list[str]]:
        """Shared scanning core behind start_detect_all and start_auto_
        detect_instrument: opens one raw sd.InputStream per distinct
        resolved input device — no AudioEngine/recorder/mixer, just
        listening — grouped by physical channel within each device, so
        instruments sharing a channel (e.g. bass and electric guitar off
        the same DI, swapped between takes) are told apart by an
        InstrumentClassifier scoped to just those instruments, narrower
        and much less bias-prone than comparing against every configured
        instrument regardless of which channel is actually shared (see
        instrument_classifier.py's module docstring). A channel with only
        one instrument on it still gets a classifier, but with one
        candidate it trivially always picks that instrument.

        `on_channel_detected(name, confidence)` fires from a background
        thread whenever any channel's classifier picks a match; the two
        callers differ only in what that means (start_detect_all reports
        it and keeps every stream running; start_auto_detect_instrument
        locks onto the first one and tears every stream down).

        `on_channel_active(input_label, active)`, if given, fires
        synchronously from the realtime callback itself (not a background
        thread, unlike on_channel_detected — keep it cheap) on every
        silent<->non-silent transition of a channel, using the same
        SILENCE_THRESHOLD the classifier itself gates on, with a short
        release hold (see `release_blocks` below) so an ordinary gap
        between notes doesn't flicker it — this is what drives detect-
        test's per-input "is anything coming through this channel right
        now" light (see ui/detect_test.py), independent of and unrelated
        to whether any instrument has actually been identified on it yet.
        start_auto_detect_instrument has no use for this and leaves it
        None, at zero extra cost (the level check is skipped entirely
        when there's no callback to report it to).

        `on_channel_stats(input_label, min_hz, max_hz, polyphony, peak_hz)`,
        if given, fires from its own background thread (same pattern as
        on_channel_detected) roughly every SpectralStatsTracker window of
        non-silent audio on a channel — see that class and analyze_
        spectrum in audio/instrument_classifier.py for what the values
        mean (`peak_hz` is every individual fundamental found, ascending;
        `polyphony` is just its length). Independent of on_channel_
        detected/InstrumentClassifier entirely: describes raw frequency
        content, not an identified instrument. Also None (skipped, zero
        cost) for start_auto_detect_instrument.

        Caller must hold self._record_lock and have already called
        self._close_active_monitor(). Returns (streams, skipped_
        instrument_names) — skipped is every instrument whose input
        couldn't be resolved right now (e.g. its interface is powered
        off), left out rather than failing the whole scan. Raises
        BackendError if config has no instruments, or none of their
        inputs can currently be resolved."""
        from .audio.instrument_classifier import InstrumentClassifier, SILENCE_THRESHOLD, SpectralStatsTracker
        if not config.instruments:
            raise BackendError("No instruments configured.")

        try:
            import numpy as np
            import sounddevice as sd
        except Exception as e:
            raise BackendError(f"sounddevice unavailable: {e}") from e
        from .audio.devices import resolve_device

        by_device: dict[int, list[tuple[Instrument, int, str]]] = {}
        skipped: list[str] = []
        for inst in config.instruments:
            input_info = config.resolve_input(inst.input_label)
            in_dev = resolve_device(sd, input_info.device, "input") if input_info else None
            if input_info is None or in_dev is None:
                skipped.append(inst.full_name)
                continue
            in_info = sd.query_devices(in_dev, "input")
            if input_info.channel > in_info["max_input_channels"]:
                skipped.append(inst.full_name)
                continue
            by_device.setdefault(in_dev, []).append((inst, input_info.channel - 1, inst.input_label))

        if not by_device:
            raise BackendError("None of the configured instruments' inputs are available right now.")

        # Consecutive silent blocks to ride through before reporting a
        # channel inactive (~0.5s) — block-count rather than a wall-clock
        # timer since this is evaluated inline in the realtime callback.
        release_blocks = max(1, round(0.5 * config.sample_rate / config.buffer_size))

        def make_callback(entries: list[tuple[Instrument, int, str]]):
            by_channel: dict[int, list] = {}
            channel_input_label: dict[int, str] = {}
            for inst, ch, input_label in entries:
                by_channel.setdefault(ch, []).append(inst)
                channel_input_label[ch] = input_label
            classifiers = {
                ch: InstrumentClassifier(config.sample_rate, insts, on_channel_detected)
                for ch, insts in by_channel.items()
            }
            stats_trackers = {}
            if on_channel_stats is not None:
                stats_trackers = {
                    ch: SpectralStatsTracker(
                        config.sample_rate,
                        lambda min_hz, max_hz, poly, peaks, il=channel_input_label[ch]: on_channel_stats(
                            il, min_hz, max_hz, poly, peaks,
                        ),
                    )
                    for ch in by_channel
                }
            active = {ch: False for ch in by_channel}
            silent_run = {ch: 0 for ch in by_channel}

            def _callback(indata, frames, time_info, status) -> None:
                for ch, classifier in classifiers.items():
                    if ch >= indata.shape[1]:
                        continue
                    block = indata[:, ch]
                    classifier.process_block(block)
                    tracker = stats_trackers.get(ch)
                    if tracker is not None:
                        tracker.process_block(block)
                    if on_channel_active is None:
                        continue
                    if float(np.max(np.abs(block))) >= SILENCE_THRESHOLD:
                        silent_run[ch] = 0
                        if not active[ch]:
                            active[ch] = True
                            on_channel_active(channel_input_label[ch], True)
                    elif active[ch]:
                        silent_run[ch] += 1
                        if silent_run[ch] >= release_blocks:
                            active[ch] = False
                            # Reset before notifying: on_channel_active's
                            # caller (detect-test) turns the channel's
                            # instrument light off on this same event, and
                            # the classifier needs to forget its last
                            # answer now too — otherwise the *same*
                            # instrument being confirmed again after this
                            # gap would be silently suppressed as "no
                            # change" (see InstrumentClassifier.reset's
                            # docstring) and the light would never come
                            # back on for it.
                            classifier.reset()
                            on_channel_active(channel_input_label[ch], False)
            return _callback

        streams = []
        try:
            for in_dev, entries in by_device.items():
                channel_count = max(ch for _, ch, _ in entries) + 1
                stream = sd.InputStream(
                    device=in_dev, channels=channel_count,
                    samplerate=config.sample_rate, blocksize=config.buffer_size,
                    dtype="float32", callback=make_callback(entries),
                )
                stream.start()
                streams.append(stream)
        except Exception as e:
            for s in streams:
                s.stop()
                s.close()
            raise BackendError(f"Could not open an input device: {e}") from e

        return streams, skipped

    def start_detect_all(self) -> None:
        with self._record_lock:
            if (
                self._active_session is not None or self._active_video_check is not None
                or self._active_latency_test is not None or self._active_instrument_test is not None
                or self._active_detect_all is not None
                or self._active_auto_detect is not None
            ):
                raise BackendError("Another recording is already in progress.")

            config = self.get_config()
            self._close_active_monitor()  # always opens its own streams, never reuses the ambient one
            stop_event = threading.Event()

            def on_channel_detected(name: str, _confidence: float) -> None:
                if not stop_event.is_set():
                    self._emit("detect_all_status", {"phase": "detected", "instrument": name})

            def on_channel_active(input_label: str, active: bool) -> None:
                if not stop_event.is_set():
                    self._emit("detect_all_status", {"phase": "channel", "input_label": input_label, "active": active})

            def on_channel_stats(
                input_label: str, min_hz: float, max_hz: float, polyphony: int, peak_hz: list[float],
            ) -> None:
                if not stop_event.is_set():
                    self._emit("detect_all_status", {
                        "phase": "stats", "input_label": input_label,
                        "min_hz": min_hz, "max_hz": max_hz, "polyphony": polyphony, "peak_hz": peak_hz,
                    })

            streams, skipped = self._open_channel_classifier_streams(
                config, on_channel_detected, on_channel_active, on_channel_stats,
            )
            self._active_detect_all = _ActiveDetectAll(streams=streams, stop_event=stop_event)

        status = "Listening on every instrument's own input — play each one to confirm it."
        if skipped:
            status += f" Not available right now: {', '.join(skipped)}."
        self._emit("detect_all_status", {"phase": "started", "status": status})

    def stop_detect_all(self) -> None:
        with self._record_lock:
            active = self._active_detect_all
            if active is None:
                return
            self._active_detect_all = None
            active.stop_event.set()
            for stream in active.streams:
                stream.stop()
                stream.close()
            self._start_monitoring_locked()
        self._emit("detect_all_status", {"phase": "stopped"})

    # --- Auto-detect instrument (remote-capable — see Backend's docstring) ---

    def start_auto_detect_instrument(self) -> None:
        with self._record_lock:
            if (
                self._active_session is not None or self._active_video_check is not None
                or self._active_latency_test is not None or self._active_instrument_test is not None
                or self._active_detect_all is not None
                or self._active_auto_detect is not None
            ):
                raise BackendError("Another recording is already in progress.")

            config = self.get_config()
            self._close_active_monitor()
            stop_event = threading.Event()

            def on_channel_detected(name: str, _confidence: float) -> None:
                with self._record_lock:
                    active = self._active_auto_detect
                    # The self._active_auto_detect is not None check is the
                    # actual "first one wins" guard: whichever channel's
                    # background classification thread gets here first
                    # clears it immediately, so any other channel that was
                    # also mid-classification at the same moment sees None
                    # and backs off instead of double-locking or stomping
                    # on a session that may already be starting.
                    if active is None or stop_event.is_set():
                        return
                    self._active_auto_detect = None
                    active.stop_event.set()
                    for s in active.streams:
                        s.stop()
                        s.close()
                    inst = config.get_instrument(name)
                    if inst is not None:
                        config.last_selected_instrument = inst.full_name
                        config.save(self._config_path)
                    self._start_monitoring_locked()
                self._emit("auto_detect_status", {
                    "phase": "detected", "instrument": name,
                    "full_name": inst.full_name if inst is not None else "",
                    "label": inst.label if inst is not None else "",
                })

            streams, skipped = self._open_channel_classifier_streams(config, on_channel_detected)
            self._active_auto_detect = _ActiveAutoDetect(streams=streams, stop_event=stop_event)

        status = "Listening — play your instrument to begin."
        if skipped:
            status += f" Not available right now: {', '.join(skipped)}."
        self._emit("auto_detect_status", {"phase": "listening", "status": status})

    def stop_auto_detect_instrument(self) -> None:
        with self._record_lock:
            active = self._active_auto_detect
            if active is None:
                return
            self._active_auto_detect = None
            active.stop_event.set()
            for stream in active.streams:
                stream.stop()
                stream.close()
            self._start_monitoring_locked()
        self._emit("auto_detect_status", {"phase": "stopped"})

    # --- Video check (local-only; RemoteBackend refuses) ---

    def start_video_check(self, req: StartRecordingRequest) -> None:
        with self._record_lock:
            if (
                self._active_latency_test is not None
                or self._active_video_check is not None
                or self._active_session is not None
                or self._active_instrument_test is not None
                or self._active_detect_all is not None
                or self._active_auto_detect is not None
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
            track = self._resolve_filter_slot(config, track, inst.full_name)

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
                    f"Instrument '{inst.full_name}' needs input channel {input_info.channel} "
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
                if other_inst.lower() == inst.label.lower():
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
            instrument=session.inst.full_name,
            instrument_label=session.inst.label,
            input_label=session.inst.input_label,
        ))

    def begin_session(self, project_name: str, instrument_name: str) -> None:
        with self._record_lock:
            self._begin_session_locked(project_name, instrument_name)
            session = self._active_session
            self._emit("recording_status", {
                "phase": "waiting",
                "status": f"Session started for '{session.inst.full_name}' in '{session.project.name}'.",
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
                musician=inst.musician or config.studio_musician, project=project.name, instrument=inst.full_name,
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
            or self._active_instrument_test is not None
            or self._active_detect_all is not None
            or self._active_auto_detect is not None
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
                f"Instrument '{inst.full_name}' needs input channel {input_info.channel} "
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
        session_dir = ensure_dir(vault_session_dir(config, project.name, f"{session_name}_{inst.full_name}"))
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
            studio_name=config.studio_name, studio_location=config.studio_location,
            session_flac=session_flac, session_video=session_dir / "session_video.mp4",
            video_recorder=video_recorder, session_video_raw=session_video_raw,
            session_mix_flac=session_mix_flac, video_start_wall_time=video_start_wall_time,
            mix_start_frame=mix_start_frame, stream_feeder=stream_feeder,
            youtube_broadcast_id=youtube_broadcast_id,
        )
        self._log_session_event("session_start", f"instrument={inst.full_name}")

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
            "instrument": session.inst.full_name,
            "instrument_label": session.inst.label,
            "input_label": session.inst.input_label,
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
