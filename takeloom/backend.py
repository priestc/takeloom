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
from .audio.pitch import effective_tuning, nearest_target
from .audio.scarlett2_direct_monitor import FOCUSRITE_DEVICE_NAME, set_channel_gains
from .config import DEFAULT_CONFIG_PATH, INSTRUMENT_LABELS, MAX_INSTRUMENT_VOLUME_PERCENT, Instrument, StudioConfig
from .project import Project, Setlist, TakeInfo, TrackEntry
from .utils import atomic_write_text, ensure_dir, timestamp_now, wall_timestamp


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


def _scan_song_take_files(stems: list[str], entry: TrackEntry, completed_dir: Path) -> dict[str, list[dict]]:
    """Every take file of `entry`'s song among `stems` (completed_takes/
    .flac stems), by label: {label: [{"take_number", "filename",
    "has_video", "has_midi"}, ...]} sorted by take_number — matched by
    utils.take_filename's 'track - label - takeN [source:id]' pattern.
    A file tagged as recorded against a *different* backing track (same
    song name, other audio) is left out, since it wouldn't line up; an
    untagged one (from before tags existed) is kept."""
    import re
    from .utils import _backing_track_id, sanitize_filename

    prefix = f"{sanitize_filename(entry.name)} - "
    source = entry.source_label()
    expected_tag = f"{source}:{sanitize_filename(_backing_track_id(source, entry.backing_track))}"
    pattern = re.compile(r"^(?P<label>(?:(?! - ).)+) - take(?P<n>\d+)(?: \[(?P<tag>.*)\])?$")
    found: dict[str, list[dict]] = {}
    for stem in stems:
        if not stem.startswith(prefix):
            continue
        m = pattern.match(stem[len(prefix):])
        if m is None or (m.group("tag") is not None and m.group("tag") != expected_tag):
            continue
        found.setdefault(m.group("label"), []).append({
            "take_number": int(m.group("n")), "filename": f"{stem}.flac",
            "has_video": (completed_dir / f"{stem}.mp4").exists(),
            "has_midi": (completed_dir / f"{stem}.mid").exists(),
        })
    for takes in found.values():
        takes.sort(key=lambda t: t["take_number"])
    return found


class Backend(ABC):
    """Interface every UI tab depends on (constructor-injected via AppState)."""

    @abstractmethod
    def hostname(self) -> str: ...

    def is_remote(self) -> bool:
        return False

    def close(self) -> None:
        pass

    def clear_playback_cache(self) -> None:
        """Forget every cached playback file (see _playback_cache) and
        delete the scratch copies — the Completed Takes tab's Reload
        button. Purely client-side: these files live on whichever machine
        the UI runs on, so RemoteBackend extends rather than forwards it."""
        import shutil
        _playback_cache.clear()
        shutil.rmtree(Path(tempfile.gettempdir()) / "takeloom_playback", ignore_errors=True)

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

    @abstractmethod
    def list_midi_devices(self) -> list[str]:
        """Every currently visible USB MIDI input port name — backs
        Studio Setup's Input dropdown for a "midi-keyboard" Instrument
        (see config.Instrument.midi_device/audio/midi_input.py). Unlike
        list_streamdecks, this *is* forwarded over a Remote connection
        (same as list_audio_devices/list_cameras) since the MIDI keyboard
        is real hardware attached to whichever machine actually runs the
        session (see CLAUDE.md's studio hardware notes), which can be
        configured from a Remote-connected client the same as any other
        device. Returns [] on any failure — never raises."""
        ...

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
    def add_inspiration_backing_track(
        self, project_name: str, artist: str, title: str,
        on_progress: Callable[[float | None, str], None] | None = None,
    ) -> dict:
        """Search the inspiration server by artist and/or title and add the
        exact match as a backing track — the Add to Setlist dialog's
        "Inspiration" tab, as opposed to a song set slot
        (add_song_set_slot) which draws a random song from its list
        fresh each session instead of one fixed song. Downloads the audio
        immediately, so this leaves the track fully ready to record.
        Raises BackendError if no exact artist/title match is found (see
        inspiration.select_best_match).

        If given, on_progress(percent, message) reports live download
        progress — inspiration files are full-quality and can take a
        while. RemoteBackend can't stream this live over its simple
        request/response RPC, so it calls on_progress once with a
        placeholder message instead, same as add_youtube_backing_track."""
        ...

    @abstractmethod
    def add_song_set_slot(self, project_name: str, label: str, songs: list[dict]) -> dict:
        """Add a standing setlist "song set" slot: each session draws one
        song at random from `songs` — a fixed list of inspiration-server
        track dicts (as from search_inspiration_titles/
        find_inspiration_track/search_inspiration_by_filter) — instead of
        one fixed song. See TrackEntry's docstring and _resolve_filter_
        slot_for_session. `label` is the slot's display name in the
        Setlist list."""
        ...

    @abstractmethod
    def find_inspiration_track(self, artist: str, title: str) -> dict:
        """The one inspiration-server track exactly matching artist/
        title (see inspiration.select_best_match), without downloading or
        adding anything — for typing a song into a song set by hand when
        it wasn't picked off the Title autocomplete. Raises BackendError
        if there's no exact match."""
        ...

    @abstractmethod
    def get_filter_slot_previews(self, project_name: str) -> list[dict | None]:
        """Read-only preview of what each song set slot in project_name's
        setlist would currently draw, one entry per setlist track in order
        (None for an ordinary track). Each slot's entry is {"match_count":
        N, "next_up": {label: name_or_None}} — match_count is how many
        songs are in the set, and next_up is, for every configured
        instrument label, a random pick from it (see _pick_filter_match),
        without committing to anything. Meant to be called once when a
        project is opened in the UI (see record.py's Setlist panel) so the
        picks shown stay stable for the rest of that visit rather than
        re-randomizing on every setlist redisplay."""
        ...

    # --- sessions (browse/correct past recordings) ---

    @abstractmethod
    def list_sessions(self) -> list[dict]:
        """Every past session found under the vault's sessions/ directory
        (see vault.vault_session_dir), newest first: each dict has
        `session_dir` (opaque id — pass back to the other session methods),
        `date` (session_log.json's own wall_time, not derived from the
        directory name, which is filename-sanitized and thus lossy),
        `project`, `instrument`, `track_names` (deduplicated, from the
        session's logged events), `status_summary` ("N completed takes",
        or "Pending processing" before process_session has run — see
        _session_summary), `duration` (m:ss, or h:mm:ss past an hour —
        the last logged event's timestamp, i.e. time elapsed since the
        session started), and `processed` (the same thing status_summary
        being "Pending processing" already tells you, as a plain bool —
        see ui/app.py's startup check, which processes the vault's own
        latest session automatically if this is ever False for it).
        Only sessions still present on local disk — one already pruned
        to a remote-only vault (session_vault_mode "remote", see vault.
        sync_and_maybe_prune) won't show up here."""
        ...

    @abstractmethod
    def get_session_detail(self, session_dir: str) -> dict:
        """Full session_log.json contents for `session_dir` (a `session_dir`
        value from list_sessions()), plus, for each track name it touched,
        exactly which take(s) *this session* produced for it — never a
        take some other session made for the same track/song, and never
        one of this session's own that's since been superseded by a later
        session's re-record. Sourced from session_log.json's own "takes"
        snapshot (written by processing/splicer.py's process_session once
        it finishes splicing this session — one entry per take, in the
        same shape reassign_take/analyze_take's `instrument_name` param
        expects; kept in sync by reassign_take if a take it names is later
        renamed — see that method's docstring). Each track's entry also
        reports whether it was a song set draw
        (session_log.json's filter_slot_draws) — reassign_take/
        analyze_take both work on those the same as any other take (see
        reassign_take's docstring) — and a `status`: "completed" (a take
        is on file), "skipped"/"stopped early" (the play-through was
        abandoned — see processing/splicer.py's module docstring),
        "recorded, not filed" (reached the end of the song but still has
        no take on file — abnormal once processing has run; usually
        means process_session raised partway through), or "pending"
        (this session hasn't been through process_session at all yet, so
        nothing about its outcome is known — see _track_take_status).

        Also adds `date_display` (the session's start time, spelled out —
        see _format_session_datetime), `duration` (m:ss/h:mm:ss, time
        elapsed since it started), `vault_tags` (which of this session's
        own raw files — "flac"/"midi"/"video" — are still present in the
        local vault right now; not the same question as a take's own
        has_video/has_midi, which is about a file already filed into a
        project), and `processed` (whether process_session has been run
        on this session at all yet — the same thing every per-track
        `status` above being "pending" would already tell you, just
        answered once for the whole session rather than per track, for
        deciding whether to show process_pending_session's button)."""
        ...

    @abstractmethod
    def process_pending_session(self, session_dir: str) -> str:
        """Run processing/splicer.py's process_session on `session_dir`
        right now, synchronously, rather than waiting for whatever would
        normally trigger it — the session having just ended live in this
        same process (see backend.py's _end_session/_process_session).
        Covers two cases: a session process_session simply hasn't reached
        yet for any reason, and recovering one whose owning process died
        mid-recording (a crash, power loss) before it ever got the
        chance — parse_session_log tolerates a log that just stops with
        no closing event at all for exactly this reason (see its
        docstring), closing out whatever was still open using session.
        flac's own actual length rather than losing it outright. Also
        runs vault.sync_and_maybe_prune afterward, same as the live path,
        so a "remote" vault session gets pushed off local disk the same
        way it normally would once its takes are safely spliced out.
        Raises BackendError if `session_dir` isn't available locally
        (nothing to process from), or is the session currently actively
        recording (wait for it to end first)."""
        ...

    @abstractmethod
    def delete_session(self, session_dir: str) -> dict:
        """Permanently delete `session_dir` and everything it produced:
        its directory (raw flac/video/MIDI and session_log.json) locally
        and on the backup server, plus every take its "takes" snapshot
        lists (see get_session_detail) — the take files themselves
        (.flac/.mp4/.mid, local and backup server) and their entries in
        every project setlist and the shared inspiration-take index, so
        each such track is left with no take for that label (an older
        take file of the same song isn't promoted in its place). The
        backup server is cleaned first; if that fails nothing local is
        touched. Returns {"takes_deleted": N}. Raises BackendError while
        any session is recording or still processing (its in-memory
        setlist could write deleted takes back), or if `session_dir`
        isn't found."""
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
        from a song set slot — see TrackEntry's docstring for
        why its take never lives on the setlist entry itself; session_
        dir's own session_log.json records exactly which shared-index
        entry a song set slot drew via filter_slot_draws, so this is just
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
        already been called on the same session.

        Also best-effort updates session_dir's own "takes" snapshot (see
        get_session_detail's docstring) to the new filename, so this
        session keeps showing (and, via get_take_playback_path, keeps able to
        actually fetch) the take it produced instead of a now-stale
        reference to the pre-rename filename — silently, since this is
        secondary to the reassignment itself actually succeeding; a log
        read/write failure just leaves it as-is rather than failing the
        whole call.

        Raises BackendError if `new_instrument` isn't a recognized
        label, `track_name` isn't one of session_dir's tracks, or if
        there's no take currently filed under `old_instrument` to
        reassign."""
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
        to reach for reassign_take). Works for a song set
        draw too, same as reassign_take (looked up from the shared
        vault-wide inspiration-take index, same as get_session_detail).
        Narrows the comparison to instruments on the take's own
        TakeInfo.input_label (the physical input it was actually recorded
        from, captured at record time — see _SessionEvent/CompletedTake)
        — the most precise possible scope, since it's an immutable fact
        about the take rather than derived from today's config, and
        survives the take's label being renamed or removed entirely.
        Narrowing to unrelated hardware inputs is avoided because it
        would reintroduce the classifier's bias toward whichever
        candidate has the widest default frequency range. If nothing
        currently configured shares that input (e.g. the instrument's
        since been removed from config) — precisely the take most worth
        analyzing, since there's no other way left to tell where it
        belongs — falls back to comparing against every currently
        configured instrument instead of refusing; the same bias caveat
        applies there with less precision, but a possibly-imprecise guess
        beats none. Returns {"guess": str | None, "confidence": float} —
        guess (a label) is None if the take's audio had no non-silent
        windows to analyze (e.g. it's silence). Never modifies anything.
        Raises BackendError if `track_name` isn't one of session_dir's
        tracks, there's no take currently filed under `instrument_name`
        (same as reassign_take), the take's file isn't available locally
        right now (e.g. pruned under "remote" vault mode), or no
        instruments are configured at
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
        the file — RemoteBackend refuses outright; see get_take_playback_path for the
        Remote-capable equivalent, which uses this method server-side as
        a step of its own, not by calling this one directly over the
        wire."""
        ...

    @abstractmethod
    def ensure_backing_track_local(self, take_filename: str) -> str:
        """Make sure the backing track of the song `take_filename` (any one
        of its current takes — resolved the same way edit_backing_track
        does) exists on *this machine's* vault disk — downloading it from
        the inspiration server or the backup server if not — and return
        its path. Local-only, like ensure_take_local: RemoteBackend
        refuses; see get_backing_playback_path for the Remote-capable
        equivalent."""
        ...

    @abstractmethod
    def get_backing_playback_path(self, take_filename: str) -> str:
        """Like get_take_playback_path, for the backing track of the song
        `take_filename` belongs to — a path on the caller's own machine
        (fetched from the studio over Remote) for the Completed Takes
        mixer's "Backing track" strip. Raises BackendError if it can't be
        made available."""
        ...

    @abstractmethod
    def get_take_playback_path(self, project_name: str, filename: str, label: str) -> str:
        """Return a path, on whichever machine the caller is actually
        running on, to a playable copy of a specific take file (see
        ensure_take_local for what `filename` means and the local-
        availability guarantee this gives first) — for the UI's built-in
        player (ui/audio_player.py) to play. The point being that a UI
        tab can call this identically whether app_state.backend is local
        or a Remote connection, and gets a file on its own disk either
        way, unlike ensure_take_local. Raises BackendError under
        the same conditions ensure_take_local does.

        `label` is the take's own instrument label (e.g. a Sessions-tab
        take row's `take["instrument"]`, or a Completed Takes row's
        `take["instrument"]`) — the take file itself is always raw on
        disk (see AudioEngine._callback), so if that label's compressor
        (StudioConfig.compressor_for_label) is enabled, this processes a
        scratch temp copy through it (see audio.filters.apply_compressor)
        and returns *that*, leaving the original file untouched. Returns
        the original's path, no temp copy, if that label's compressor
        is disabled."""
        ...

    @abstractmethod
    def list_completed_takes(self) -> list[dict]:
        """Every completed take currently on file, vault-wide — behind
        the Completed Takes tab, which (unlike Sessions) isn't scoped to
        one session or project. Gathered from every project's own
        setlist.json (an ordinary track's preferred_takes) plus the
        shared vault-wide inspiration-take index (vault.py's
        load_inspiration_index) — the only place a take drawn from an
        song set slot is ever recorded, since a song set slot's
        own TrackEntry.preferred_takes stays empty forever (see
        TrackEntry's docstring) — deduplicated by filename, since a
        non-filter inspiration-sourced track's take is written to both.
        Each dict: {"track_name": str, "instrument": str (a label),
        "filename": str, "take_number": int, "has_video": bool,
        "has_midi": bool, "volume": float, "recorded_at": float | None,
        "trim_start_seconds": float, "trim_end_seconds": float,
        "backing_source": str, "backing_duration_seconds": float | None},
        sorted by
        track_name. The last two are the song's current non-destructive
        "edit backing track" trim (see edit_backing_track) — 0.0/0.0 if
        it's never been trimmed — carried here so a caller (play_song_
        takes, or the edit dialog reopening on an already-trimmed song)
        doesn't need a second lookup just to read them back.
        backing_source is TrackEntry.source_label() — "inspiration",
        "youtube", or "upload" — where the song's backing track came from.
        backing_duration_seconds is the backing track's full, untrimmed
        length (None if never measured) — the range the Completed Takes
        tab's trim editor (ui/backing_trim_editor.py) lets you crop within.
        A take
        superseded by a later reassign_take/re-record
        no longer appears here, same as it wouldn't in any project's
        setlist — this reflects each track+label's *current* take, not
        every file ever written to completed_takes/. Those older files
        are still offered, though: "alternate_takes" lists every take of
        this row's song+label found in completed_takes/ (this one
        included), and "unpreferred_takes" maps each *other* label that
        has take files for this song but no current take ("no preferred
        take") to its own such list — the Completed Takes mixer's take
        dropdowns, which switch between them via set_preferred_take.
        Each listed take: {"take_number", "filename", "has_video",
        "has_midi"}, sorted by take_number. Only files recorded against
        this song's own backing track count (see _scan_song_take_files).

        recorded_at is the take file's own filesystem mtime (a Unix
        timestamp), or None if it isn't on local disk right now (e.g.
        pruned under "remote" vault mode) — there's no dedicated "when
        was this actually recorded" field tracked anywhere else, and
        deriving it would mean either a network round trip per take just
        to list them (unacceptable for what's otherwise a fast, local-
        only read) or reconstructing it from session history, which
        predates plenty of existing takes anyway. mtime survives a
        reassign_take rename (same-filesystem renames don't touch it),
        so this stays meaningful even for a take that's been re-filed
        under a different label since it was recorded."""
        ...

    @abstractmethod
    def set_preferred_take(self, take_filename: str, instrument: str, new_filename: str | None) -> None:
        """Make `new_filename` — one of list_completed_takes' alternate_
        takes/unpreferred_takes for this song and `instrument` (a label)
        — the song's current take for that label, or with None, clear it
        ("no preferred take") so the label has no take at all. The take
        files themselves are never touched. `take_filename` is any one
        current take of the song, resolving *which* song exactly as
        edit_backing_track does — every matching record (a project's own
        TrackEntry and/or the shared inspiration-take index) is updated.
        Raises BackendError if no record references `take_filename` or
        `new_filename` isn't a take of this song for `instrument`."""
        ...

    @abstractmethod
    def get_song_mix(self, track_name: str) -> dict | None:
        """The Completed Takes mixer's saved settings for `track_name` —
        {"track_name", "volumes": {label: gain}, "muted": [label, ...],
        "saved_at"} — or None if none has been saved. See vault.py's
        load_song_mix."""
        ...

    @abstractmethod
    def save_song_mix(self, track_name: str, volumes: dict[str, float], muted: list[str]) -> dict:
        """Save the Completed Takes mixer's settings for `track_name` into
        the vault (mixes/<song>.json — see vault.py's save_song_mix),
        returning the saved mix in get_song_mix's shape."""
        ...

    @abstractmethod
    def edit_backing_track(
        self, take_filename: str, trim_start_seconds: float, trim_end_seconds: float,
    ) -> dict:
        """Non-destructively crop a song's backing track — for a long
        unwanted intro/outro — by `trim_start_seconds` off the start
        and/or `trim_end_seconds` off the end. No audio file is ever
        touched: this only sets TrackEntry.trim_start_seconds/
        trim_end_seconds (replacing, not adding to, whatever was set
        before), which every current and future consumer of this song's
        backing_track/preferred_takes applies as a virtual playback
        window instead — see _load_track_locked (a session recording a
        new take, or layering an existing one in), the Video Check path,
        _resolve_filter_slot (a song set slot redrawing this
        same song later inherits the trim from the shared index), and
        the Completed Takes mixer (ui/song_mixer.py). A newly recorded take is therefore already
        exactly the trimmed length with nothing further to do; an
        existing take recorded before the trim was set gets the identical
        window applied at playback/mix time instead, so it stays in sync
        with the now-"shorter" backing track without its file ever being
        rewritten. (A take's video/MIDI sidecar isn't windowed this way
        yet — only its audio is — so review playback of either still
        includes the untrimmed footage/performance for now.)

        `take_filename` is any one current take's filename (a Completed
        Takes row's `take["filename"]`) — used only to resolve *which*
        song this is: every project's setlist is scanned for the
        TrackEntry whose preferred_takes contains it, and the shared
        vault-wide inspiration-take index (vault.py) is checked the same
        way, so this also works for a song a song set slot
        drew (which has no TrackEntry of its own — see TrackEntry's
        docstring) as long as it has at least one take recorded. Every
        record found that way is updated (an ordinary, inspiration-
        sourced track has both its own setlist entry and a mirrored
        shared-index entry — see reassign_take's docstring for that same
        split) — trim_start_seconds/trim_end_seconds and duration_seconds
        (the *effective*, trimmed length — always re-measured from the
        untouched backing track file, not derived from whatever was
        previously stored) are kept identical across every one of them.

        Returns {"track_name": str, "new_duration_seconds": float,
        "affected_takes": [{"instrument": str, "filename": str}, ...]}
        (every instrument currently on file for this song, across every
        matched record, deduplicated by filename — these are what will
        now play back cropped, not files that were just changed). Raises
        BackendError if no record references `take_filename`, the trim
        amounts are negative, or they'd leave the backing track at ≤0
        seconds (measured against its real, untouched duration)."""
        ...

    # --- inspiration ---

    @abstractmethod
    def search_inspiration_artists(self, partial: str) -> list[str]:
        """Autocomplete suggestions for an inspiration Artist field (the
        song set builder's filter, the Add to Setlist dialog), from the inspiration server's autocomplete endpoint (see
        docs/inspiration-server-autocomplete-api.md). Returns [] rather
        than raising on any failure — this fires on every keystroke, so a
        slow/unreachable server should just mean no suggestions, not an
        error dialog interrupting typing."""
        ...

    @abstractmethod
    def search_inspiration_titles(self, partial: str, artist: str = "") -> list[dict]:
        """Same as search_inspiration_artists, for the Add to Setlist
        dialog's Inspiration tab Title field — narrowed to `artist`'s
        tracks if given. Each result is a full track dict (id/artist/
        title/year/format/duration), not just a title string, so picking
        one off the dropdown can add that exact track directly (see
        add_inspiration_track_by_id) instead of falling back to
        add_inspiration_backing_track's fuzzier by-name search."""
        ...

    @abstractmethod
    def add_inspiration_track_by_id(
        self, project_name: str, track_info: dict,
        on_progress: Callable[[float | None, str], None] | None = None,
    ) -> dict:
        """Add a specific, already-known inspiration track directly, with
        no by-name search step — used when `track_info` came straight off
        the Add to Setlist dialog's Title autocomplete (which returns full
        track records — see search_inspiration_titles), so the exact
        track the user picked in the dropdown is the exact track that
        gets added, instead of add_inspiration_backing_track's fuzzier
        search-then-guess. `track_info` is one of those track dicts
        (id/artist/title/year/format/duration)."""
        ...

    @abstractmethod
    def search_inspiration_by_filter(self, filter_criteria: dict, all_matches: bool = False) -> list[dict]:
        """Inspiration-server tracks matching `filter_criteria` (artist/
        genre/year_min/year_max/duration_min/duration_max) — backs the
        song set builder's "Add from filter" tab. A random sample of at
        most 100 unless `all_matches`, which asks for every match — see
        inspiration._post_track_query."""
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
        current track isn't a song set slot's draw — callers driving this
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

    def get_playback_position(self) -> tuple[float, float]:
        """(position_seconds, duration_seconds) of whatever backing track
        is currently loaded in the active engine's mixer — for the Stream
        Deck touchscreen's playback progress bar (see recording_driver.py's
        polling ticker). Same "not every backend can report this cheaply"
        reasoning as get_levels() — RemoteBackend doesn't override this,
        so a Stream Deck driven by a Remote-connected Tk UI just shows no
        progress bar rather than polling it over the network every
        second; (0.0, 0.0) either way means "nothing to show"."""
        return (0.0, 0.0)

    @abstractmethod
    def adjust_backing_volume(self, delta: int) -> None: ...

    @abstractmethod
    def adjust_takes_volume(self, delta: int) -> None: ...

    @abstractmethod
    def adjust_instrument_volume(self, delta: int) -> None: ...

    # --- audio filters (compressor now, more later) ---

    @abstractmethod
    def set_compressor_settings(self, label: str, settings: dict) -> None:
        """Update `label`'s compressor settings (one of INSTRUMENT_LABELS,
        not a particular Instrument's full_name — see StudioConfig.
        compressor_for_label, which is how everything else reads them
        back, including get_config() itself; there's no dedicated getter)
        — persisted for next time, and applied immediately to a
        currently-running recording/video-check/session engine *if* it's
        currently open for an instrument sharing this exact label (mirrors
        adjust_backing_volume/adjust_takes_volume for how "immediately"
        works here). Has no live effect on an "other instrument's take"
        already layered into a running session's monitor mix — those were
        compressed once, offline, at load time (see Mixer.add_source) —
        only takes effect the next time that track is loaded."""
        ...

    @abstractmethod
    def set_synth_voice(self, instrument_name: str, voice: str) -> None:
        """Change instrument_name's (a MIDI-driven Instrument's
        full_name — see config.Instrument.is_midi/midi_device) synth
        voice to `voice` (one of audio.synth.SYNTH_VOICES) — the Record
        page's "Sound" picker, not Studio Setup, is what calls this; a
        MIDI instrument's voice is meant to be changed live while
        playing, not fixed at setup time the way its label/input are.

        Persisted onto that Instrument in config for next time (per
        specific instrument, not per label, since two different MIDI
        instruments could each want a different default voice — unlike
        set_compressor_settings, which is keyed by label) and, if a
        currently-running engine (an active session's, or the ambient
        monitor's) is open for this exact instrument, applied to its
        live Synth immediately (see audio/synth.py's Synth.set_voice) —
        no restart needed, same "immediately" as adjust_instrument_
        volume/set_compressor_settings.

        Raises BackendError if instrument_name isn't a configured
        instrument, isn't MIDI-driven, or voice isn't a recognized
        SYNTH_VOICES value."""
        ...

    @abstractmethod
    def get_voice_switch(self) -> dict | None:
        """The MIDI keyboard the Stream Deck's pre-session "Voice" key
        would act on right now — {"instrument": full_name, "voice":
        current synth voice} — or None if the key shouldn't be shown: no
        session open, the keyboard is connected, and it has no "switch
        voice" control of its own (its keyboard_drivers driver has no
        voice knob). Once
        auto-detect has identified an instrument, only that one counts —
        an identified guitar hides the key even with a keyboard plugged
        in."""
        ...

    @abstractmethod
    def cycle_synth_voice(self) -> dict | None:
        """Advance get_voice_switch()'s keyboard to the next of audio.
        synth.SYNTH_VOICES (wrapping around), exactly as set_synth_voice
        would. Returns the updated get_voice_switch() dict, or None if
        there's nothing to cycle. Raises BackendError mid-session — the
        Stream Deck key is only for picking a voice before starting."""
        ...

    @abstractmethod
    def benchmark_audio_modifiers(self) -> dict:
        """Run audio.benchmark.run_audio_modifier_benchmark() against
        this machine's own current config (sample_rate/buffer_size, and
        every instrument label's compressor_settings) and return its
        result as a plain dict (dataclasses.asdict(ModifierBenchmarkResult)
        plus a "within_budget" bool, since that property doesn't survive
        asdict on its own). Always runs on whichever machine would
        actually run AudioEngine for real — i.e. this method, not the
        rig itself, is what a Remote connection forwards to the studio
        machine, the same way every other real-time-audio-relevant call
        does."""
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
        instrument's input can currently be opened at all.

        Also emits "tuner_status" events ({input_label, note, cents,
        frequency_hz}) roughly every quarter-second of non-silent audio
        on any channel, for the Record tab's live tuner needle — see
        audio/instrument_classifier.py's TunerTracker and audio/pitch.py's
        nearest_target. `note`/`cents` are already resolved against
        whichever instrument(s) share that channel's own tuning (or their
        label's default), so a client just displays them; no separate
        lookup needed."""
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
    # Which sound (audio/synth.py's SYNTH_VOICES — "piano"/"organ") was
    # selected on this instrument when the event was logged — empty for a
    # non-MIDI instrument, where the concept doesn't apply. Same per-event-
    # carries-its-own-copy reasoning as instrument/instrument_label above:
    # processing/splicer.py's take-filing uses this (not "midi-keyboard")
    # as a MIDI take's actual label, so it survives the instrument's voice
    # being changed later, and two voices recorded in the same session
    # file as distinctly as if they were different instruments.
    synth_voice: str = ""

    def to_dict(self) -> dict:
        d = {
            "timestamp": self.timestamp, "wall_time": self.wall_time,
            "event_type": self.event_type, "details": self.details,
            "instrument": self.instrument, "instrument_label": self.instrument_label,
            "input_label": self.input_label, "synth_voice": self.synth_voice,
        }
        if self.frame is not None:
            d["frame"] = self.frame
        if self.track_index is not None:
            d["track_index"] = self.track_index
        if self.track_name:
            d["track_name"] = self.track_name
        return d


def _filter_draw_dict(entry: TrackEntry) -> dict:
    """One song-set draw as session_log.json records it (see
    _save_session_log)."""
    return {
        "name": entry.name, "backing_track": entry.backing_track,
        "duration_seconds": entry.duration_seconds,
        "inspiration_track_id": entry.inspiration_track_id,
    }


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
    midi_input: object | None
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
    # song set slot's own index is added here the moment it's resolved
    # (see _resolve_filter_slot_for_session), not when a take completes —
    # otherwise it would keep getting redrawn every auto-advance cycle
    # within this same session, since its own preferred_takes never gets
    # a take (the take belongs to whatever song got drawn, which is never
    # itself added to the setlist — see _resolve_filter_slot).
    completed_track_indices: set = field(default_factory=set)
    # song-set index -> the TrackEntry drawn for it this session (never
    # added to project.setlist.tracks — see _resolve_filter_slot). Session-
    # only cache: a song set slot revisited later in the same session (e.g. a
    # manual reselect) gets the same resolved track back rather than a
    # fresh random draw each time.
    resolved_filter_picks: dict = field(default_factory=dict)
    # song-set index -> every earlier draw a redraw replaced in
    # resolved_filter_picks, oldest first. A take recorded on one of
    # those (kept if it ran long enough, see processing/splicer.py) must
    # still be filed under its own song, not whichever draw the slot
    # ended up on — see _save_session_log's filter_slot_draw_history.
    replaced_filter_picks: dict = field(default_factory=dict)
    # Raw MIDI performance captured alongside the audio (see audio/
    # midi_log.py) when `inst.is_midi` — None for an analog instrument,
    # where the concept doesn't apply. Written out to session_midi.mid
    # at session end (_end_session) and later sliced per-take by
    # processing/splicer.py, so a completed take can be revoiced later.
    midi_log: object | None = None


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
    midi_input: object | None = None


class _EngineInputTap:
    """One of auto-detect's classifier callbacks, fed from an already-
    running AudioEngine's input (AudioEngine.add_input_sink) rather than
    its own sd.InputStream — same stop()/close() shape, so it sits in the
    same `streams` list and gets torn down the same way."""

    def __init__(self, engine, callback) -> None:
        self._engine = engine
        self._callback = callback
        engine.add_input_sink(callback)

    def stop(self) -> None:
        self._engine.remove_input_sink(self._callback)

    def close(self) -> None:
        self.stop()


@dataclass
class _ActiveMonitor:
    """A live-listening-only audio stream — no recorder/session attached,
    just the instrument audible in the headphones. Opened by
    start_monitoring() for config.last_selected_instrument as soon as the
    backend starts, so Production/Recording monitoring mode (and the
    Instrument Volume dial) has something to act on before Record is ever
    pressed. Superseded (see _close_active_monitor()/start_recording()) the
    moment anything else needs the audio hardware.

    `all_inputs`: this is the all-inputs monitor instead (see Backend.
    _build_all_inputs_engine) — every input channel plus every connected
    MIDI keyboard at once, `inst` None, and `midi_inputs` holding one open
    MidiInput per keyboard."""
    engine: object
    inst: object | None
    midi_input: object | None = None
    all_inputs: bool = False
    midi_inputs: list = field(default_factory=list)
    # The all-inputs monitor's Synth per keyboard, keyed by lowercased
    # midi_device — so set_synth_voice can reach the right one live.
    synths_by_device: dict = field(default_factory=dict)


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


# Scratch playback file -> the inputs it was last rendered from. Lets
# _compressed_playback_path hand back the same file on a repeat Play
# instead of re-decoding/compressing every time — keyed on the source's
# size+mtime and the compressor settings, so changing either re-renders on
# its own. Backend.clear_playback_cache empties it.
_playback_cache: dict[Path, tuple] = {}


def _file_signature(path: Path) -> tuple:
    st = path.stat()
    return (str(path), st.st_size, st.st_mtime_ns)


def _playback_cache_hit(out_path: Path, key: tuple) -> bool:
    return _playback_cache.get(out_path) == key and out_path.exists()


def _compressed_playback_path(path: Path, settings: CompressorSettings) -> Path:
    """If `settings` is enabled, run `path`'s audio through the compressor
    and write the result to a scratch temp copy, returning that instead of
    `path` — shared by LocalBackend.get_take_playback_path and RemoteBackend.get_take_playback_path
    so a completed take's own file on disk always stays exactly what
    AudioEngine captured (raw), while anything actually listened to
    through a takeloom player reflects that take's current instrument-
    label compressor settings, looked up fresh each time rather than
    whatever was true the day it was recorded. Returns `path` unchanged
    when disabled — no reason to make a redundant copy nobody asked for."""
    if not settings.enabled:
        return path
    work_dir = ensure_dir(Path(tempfile.gettempdir()) / "takeloom_playback")
    out_path = work_dir / path.name
    key = (_file_signature(path), repr(settings))
    if _playback_cache_hit(out_path, key):
        return out_path
    from .audio.filters import apply_compressor
    from .audio.formats import read_audio, write_flac
    data, sr = read_audio(path)
    processed = apply_compressor(data, sr, settings)
    write_flac(out_path, processed, sr)
    _playback_cache[out_path] = key
    return out_path


def _pitched_playback_path(path: Path, cents: float) -> Path:
    """_compressed_playback_path's counterpart for a backing track's saved
    pitch correction (vault.py's load_backing_tuning): a scratch copy
    shifted by `cents` the same way a session plays it live (audio/
    pitch_shift.py's render), or `path` itself when there's none."""
    if not cents:
        return path
    work_dir = ensure_dir(Path(tempfile.gettempdir()) / "takeloom_playback")
    out_path = work_dir / f"{path.stem}.pitch{cents:+.2f}.flac"
    key = (_file_signature(path), round(cents, 2))
    if _playback_cache_hit(out_path, key):
        return out_path
    from .audio.formats import read_audio, write_flac
    from .audio.pitch_shift import render
    data, sr = read_audio(path)
    if data.ndim == 1:
        data = data[:, None]
    write_flac(out_path, render(data, sr, cents), sr)
    _playback_cache[out_path] = key
    return out_path


def knob_to_cents(value: int) -> float:
    """A 0-127 knob position to a backing-track pitch correction: the full
    travel spans -MAX_PITCH_CENTS..+MAX_PITCH_CENTS (one semitone end to
    end), centre (64) exactly 0, with a ±1-cent dead zone so a knob left
    "at the middle" really is untouched."""
    from .audio.pitch_shift import MAX_PITCH_CENTS
    cents = max(-MAX_PITCH_CENTS, min(MAX_PITCH_CENTS, (value - 64) / 63.0 * MAX_PITCH_CENTS))
    return 0.0 if abs(cents) < 1.0 else cents


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
        # Whether ambient monitoring (_start_monitoring_locked) opens every
        # input at once — each analog channel plus each connected MIDI
        # keyboard — rather than just config.last_selected_instrument. True
        # from launch / the audio interface being powered on until auto-
        # detect commits to an instrument (or a session is opened for one
        # directly), and again whenever a new auto-detect scan starts: until
        # something's been identified, whatever's about to be played should
        # be heard, not whatever happened to be used last time.
        self._monitor_all_inputs = True
        # The keyboard-knob listener (_ensure_control_listener): one
        # knob-only MidiInput per plugged-in keyboard whose driver has a
        # voice or backing-pitch knob, keyed by lowercased midi_device, plus the config
        # snapshot its rtmidi-thread handler reads (refreshed every poll).
        self._control_thread: threading.Thread | None = None
        self._control_inputs: dict = {}
        self._control_config: StudioConfig | None = None
        self._voice_knob_zone: dict[str, int] = {}
        self._tuning_save_timer: threading.Timer | None = None
        self._last_emitted_pitch: int | None = None
        # Set by set_audio_hardware_present(False) — the always-running
        # server's hardware watcher (rig_watcher.py) saw the audio interface
        # get powered off. Keeps _start_monitoring_locked() — which every
        # session/scan/test teardown calls to resume ambient monitoring —
        # from trying to reopen a device that isn't there any more (PortAudio
        # still lists it until it's re-initialized).
        self._audio_hardware_absent = False
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
        # No self._instrument_volume — unlike backing/takes, instrument
        # volume is per-Instrument (config.Instrument.instrument_volume),
        # not one sticky value this backend carries in memory; see
        # adjust_instrument_volume, which reads/writes it straight on
        # whichever Instrument is currently active.

    def _save_last_volumes(self) -> None:
        config = self.get_config()
        config.last_backing_volume = self._backing_volume
        config.last_takes_volume = self._takes_volume
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

    def list_midi_devices(self) -> list[str]:
        from .audio.midi_input import list_midi_devices
        return list_midi_devices()

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

    def refresh_camera_preview(self) -> None:
        """Just the camera half of refresh_devices() — for the server's
        hardware watcher (rig_watcher.py) when the webcam powers on. Leaves
        PortAudio alone, which is never safe to re-initialize under an open
        stream."""
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

    def add_inspiration_backing_track(
        self, project_name: str, artist: str, title: str,
        on_progress: Callable[[float | None, str], None] | None = None,
    ) -> dict:
        from .inspiration import InspirationError, search_inspiration_tracks, select_best_match
        project = self._open_project(project_name)
        config = self.get_config()
        if on_progress:
            on_progress(None, f"Searching for {artist or title}...")
        try:
            matches = search_inspiration_tracks(config, artist=artist, title=title)
            track_info = select_best_match(matches, artist, title)
        except InspirationError as e:
            raise BackendError(str(e)) from e
        return self._add_inspiration_entry(project, config, track_info, on_progress)

    def add_inspiration_track_by_id(
        self, project_name: str, track_info: dict,
        on_progress: Callable[[float | None, str], None] | None = None,
    ) -> dict:
        project = self._open_project(project_name)
        config = self.get_config()
        return self._add_inspiration_entry(project, config, track_info, on_progress)

    @staticmethod
    def _add_inspiration_entry(
        project: Project, config: StudioConfig, track_info: dict,
        on_progress: Callable[[float | None, str], None] | None = None,
    ) -> dict:
        from .inspiration import InspirationError, download_inspiration_track, find_or_add_inspiration_track
        entry = find_or_add_inspiration_track(project, track_info)
        project.save_setlist()
        backing_path = project.backing_tracks_dir / entry.backing_track
        if not backing_path.exists():
            try:
                download_inspiration_track(entry, backing_path, config, on_progress=on_progress)
            except InspirationError as e:
                raise BackendError(str(e)) from e
        return entry.to_dict()

    def add_song_set_slot(self, project_name: str, label: str, songs: list[dict]) -> dict:
        songs = [s for s in songs if s.get("id")]
        if not songs:
            raise BackendError("Add at least one song to the set.")
        label = label.strip() or f"Song set ({len(songs)} songs)"
        from .inspiration import average_duration
        project = self._open_project(project_name)
        entry = project.add_song_set_slot(label, songs, duration_seconds=average_duration(songs))
        return entry.to_dict()

    def find_inspiration_track(self, artist: str, title: str) -> dict:
        from .inspiration import (
            InspirationError, search_inspiration_tracks, search_title_suggestions, select_best_match,
        )
        config = self.get_config()
        # Title autocomplete first: a cheap, precise title lookup returning
        # full track dicts. The filter search below is the fallback — only
        # exact on radioserver 41eda00+ (title filter + all matches); an
        # older server ignores the title and returns a random 100 of the
        # artist's tracks, which once buried "Miles Davis - So What"
        # entirely.
        suggestions = [t for t in search_title_suggestions(config, title, artist=artist, limit=50) if t.get("id")]
        try:
            return select_best_match(suggestions, artist, title)
        except InspirationError:
            pass
        try:
            tracks = search_inspiration_tracks(config, artist, title)
            return select_best_match(tracks, artist, title)
        except InspirationError as e:
            raise BackendError(str(e)) from e

    def get_filter_slot_previews(self, project_name: str) -> list[dict | None]:
        config = self.get_config()
        project = self._open_project(project_name)
        labels: list[str] = []
        for inst in config.instruments:
            if inst.label and inst.label not in labels:
                labels.append(inst.label)

        from .inspiration import build_inspiration_track_entry

        previews: list[dict | None] = []
        for track in project.setlist.tracks:
            if not track.is_inspiration_filter:
                previews.append(None)
                continue
            matches = track.song_set
            if not matches:
                previews.append({"match_count": 0, "next_up": {label: None for label in labels}})
                continue
            next_up = {label: build_inspiration_track_entry(self._pick_filter_match(matches)).name for label in labels}
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

    # A track's outcome, in the vocabulary shown in the Sessions tab —
    # see _track_take_status's docstring for what each one means.
    _TERMINAL_EVENT_TYPES = ("song_end", "track_skipped", "song_stopped")

    @staticmethod
    def _track_take_status(data: dict, track_index: int) -> str:
        """What actually became of one track_index in this session:
        "completed" (a take is on file for it — see process_session's
        session_takes snapshot), "skipped"/"stopped early" (the
        play-through was abandoned, per the raw event log — see
        processing/splicer.py's module docstring for the exact rules,
        including when an abandoned one is long enough to be kept
        anyway, which already shows up as "completed" above), "recorded,
        not filed" (reached song_end but no take is on file for it —
        not a normal outcome once processing has run; usually means
        process_session raised partway through, e.g. on a later track in
        the same session, before it could write this track's take back —
        see backend.py's _process_session), or "pending" (this session's
        "takes" key is entirely absent — process_session hasn't run on
        it yet at all, so nothing above can be answered from event-log
        guessing alone)."""
        if "takes" not in data:
            return "pending"
        if data.get("takes", {}).get(str(track_index)):
            return "completed"
        last_terminal: str | None = None
        for e in data.get("events", []):
            if e.get("track_index") == track_index and e.get("event_type") in LocalBackend._TERMINAL_EVENT_TYPES:
                last_terminal = e["event_type"]
        if last_terminal == "song_end":
            return "recorded, not filed"
        if last_terminal == "track_skipped":
            return "skipped"
        if last_terminal == "song_stopped":
            return "stopped early"
        return "not recorded"

    @staticmethod
    def _format_session_duration(seconds: float) -> str:
        """MM:SS, or utils.format_duration_hms's HH:MM:SS past an hour —
        a session (unlike a single song, everything else in this app
        uses utils.format_duration for) can run long enough to need it."""
        from .utils import format_duration, format_duration_hms
        seconds = max(0.0, seconds)
        return format_duration_hms(seconds) if seconds >= 3600 else format_duration(seconds)

    @staticmethod
    def _session_summary(session_dir_name: str, data: dict) -> dict:
        events = data.get("events", [])
        track_names: list[str] = []
        for e in events:
            name = e.get("track_name")
            if name and name not in track_names:
                track_names.append(name)

        # "takes" (process_session's snapshot — see get_session_detail's
        # docstring) absent entirely means this session hasn't been
        # through processing yet, same "pending" case _track_take_status
        # reports per-track; present (even if every value is empty)
        # means it has, so a plain count of every take actually filed —
        # a track revisited more than once in the same session can have
        # more than one — is the real answer.
        if "takes" in data:
            take_count = sum(len(v) for v in data.get("takes", {}).values())
            status_summary = f"{take_count} completed take" + ("" if take_count == 1 else "s")
        else:
            status_summary = "Pending processing"

        # events[-1]'s timestamp (seconds since session start — see
        # _SessionEvent) rather than a wall-clock difference, so it's
        # unaffected by the session spanning a DST change or similar.
        duration = LocalBackend._format_session_duration(events[-1]["timestamp"]) if events else ""

        return {
            "session_dir": session_dir_name,
            # events[0]'s wall_time (real, human-typed timestamp) rather
            # than parsed back out of the directory name, which is
            # filename-sanitized and thus lossy/ambiguous.
            "date": events[0]["wall_time"] if events else "",
            "project": data.get("project", ""),
            "instrument": data.get("instrument", ""),
            "track_names": track_names,
            "status_summary": status_summary,
            "duration": duration,
            "processed": "takes" in data,
        }

    def get_session_detail(self, session_dir: str) -> dict:
        _, data = self._read_session_log(session_dir)
        filter_slot_draws = data.get("filter_slot_draws", {})
        filter_slot_indices = {int(k) for k in filter_slot_draws}

        track_names: list[str] = []
        track_index_by_name: dict[str, int] = {}
        for e in data.get("events", []):
            name = e.get("track_name")
            if name and name not in track_names:
                track_names.append(name)
                track_index_by_name[name] = e.get("track_index")

        # This session's own snapshot of exactly which take(s) it produced,
        # per track_index — written by processing/splicer.py's
        # process_session once it finishes splicing.
        session_takes = data.get("takes", {})

        tracks = []
        for name in track_names:
            track_index = track_index_by_name.get(name)
            is_filter_draw = track_index in filter_slot_indices
            takes = session_takes.get(str(track_index), [])
            status = self._track_take_status(data, track_index) if track_index is not None else "not recorded"
            # A song set slot redrawn mid-session logs every draw under
            # the same track_index, so its takes (and status) have to be
            # split back out per song — otherwise every draw's row shows
            # every take, and reassigning one from the wrong row renamed
            # its file after the wrong song (see reassign_take). A take
            # snapshot carries its own track_name since splicer.py began
            # recording it; an older one is assumed to belong to the
            # slot's final draw (filter_slot_draws' "name"), the only
            # draw such a session could have filed a take under.
            if is_filter_draw:
                final_draw_name = (filter_slot_draws.get(str(track_index)) or {}).get("name")
                takes = [
                    t for t in takes
                    if t.get("track_name", final_draw_name) in (name, None)
                ]
                if status == "completed" and not takes:
                    status = "skipped"
            tracks.append({
                "track_name": name, "is_filter_draw": is_filter_draw, "takes": takes, "status": status,
            })

        events = data.get("events", [])
        date_display = self._format_session_datetime(events[0]["wall_time"]) if events else ""
        duration = self._format_session_duration(events[-1]["timestamp"]) if events else ""

        # Which of this session's own raw vault files are still present
        # on local disk right now — not the same question as a *take's*
        # has_video/has_midi (project.py), which is about a take already
        # filed into a project; this is about the session's own capture,
        # before/regardless of whether it's ever been spliced into any.
        # None of local/local-only if session_vault_mode "remote" already
        # pruned it away after syncing (see vault.sync_and_maybe_prune) —
        # not checked against the remote itself, so this only ever
        # answers "is it here right now", same as has_video/has_midi.
        vault_tags: list[str] = []
        session_dir_path = self._local_session_dir_path(session_dir)
        if session_dir_path is not None:
            if (session_dir_path / "session.flac").exists():
                vault_tags.append("flac")
            if (session_dir_path / "session_midi.mid").exists():
                vault_tags.append("midi")
            if (
                (session_dir_path / "session_video.mp4").exists()
                or (session_dir_path / "session_video_raw.mp4").exists()
            ):
                vault_tags.append("video")

        return {
            **data, "session_dir": session_dir, "tracks": tracks,
            "date_display": date_display, "duration": duration, "vault_tags": vault_tags,
            "processed": "takes" in data,
        }

    @staticmethod
    def _format_session_datetime(wall_time: str) -> str:
        """wall_time (utils.wall_timestamp's "%Y-%m-%d %H:%M:%S") as
        "Tuesday, September 23, 2026 at 5:14 PM" — falls back to
        `wall_time` itself unparsed rather than raising, for a log
        recorded before this format was in use."""
        from datetime import datetime
        try:
            dt = datetime.strptime(wall_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return wall_time
        return dt.strftime("%A, %B %-d, %Y at %-I:%M %p")

    def process_pending_session(self, session_dir: str) -> str:
        config = self.get_config()
        session_dir_path = self._local_session_dir_path(session_dir)
        if session_dir_path is None:
            raise BackendError(f"Session '{session_dir}' isn't available locally to process.")
        with self._record_lock:
            active = self._active_session
            if active is not None and active.session_dir == session_dir_path:
                raise BackendError("This session is still recording — wait for it to end first.")
        from .processing.splicer import process_session
        summary = process_session(session_dir=session_dir_path, config=config)
        from .vault import sync_and_maybe_prune
        sync_and_maybe_prune(config, session_dir_path)
        return summary

    def delete_session(self, session_dir: str) -> dict:
        if not session_dir or "/" in session_dir or "\\" in session_dir or session_dir in (".", ".."):
            raise BackendError(f"Invalid session name '{session_dir}'.")
        with self._record_lock:
            if self._active_session is not None:
                raise BackendError("A session is recording — end it before deleting a session.")
        thread = self._processing_thread
        if thread is not None and thread.is_alive():
            raise BackendError("A session is still being processed — try again once it's done.")

        config = self.get_config()
        from .vault import load_inspiration_index, save_inspiration_index, vault_root
        root = vault_root(config)
        local_dir = self._local_session_dir_path(session_dir)
        try:
            _, data = self._read_session_log(session_dir)
        except BackendError:
            if local_dir is None:
                raise
            data = {}  # a local session with a missing/corrupt log: still deletable

        # label -> filename for every take this session filed.
        takes = [
            (t.get("instrument", ""), t["filename"])
            for entries in (data.get("takes") or {}).values() for t in entries if t.get("filename")
        ]
        take_files = [
            f"{Path(filename).stem}{ext}" for _, filename in takes for ext in (".flac", ".mp4", ".mid")
        ]

        remote = config.backup_server if config.session_vault_mode in ("remote", "both") else ""
        if remote:
            from .sync import delete_remote_vault_paths
            relatives = [f"sessions/{session_dir}"] + [f"completed_takes/{name}" for name in take_files]
            if not delete_remote_vault_paths(remote, relatives):
                raise BackendError(f"Could not delete '{session_dir}' from the backup server ({remote}) — nothing was deleted.")

        filenames = {filename for _, filename in takes}

        def drop(entry: TrackEntry) -> bool:
            stale = [label for label, take in entry.preferred_takes.items() if take.filename in filenames]
            for label in stale:
                del entry.preferred_takes[label]
            return bool(stale)

        if filenames:
            for path in Project.list_projects(Path(config.projects_dir)):
                try:
                    project = Project.open(path, root)
                except Exception:
                    continue
                if any([drop(track) for track in project.setlist.tracks]):
                    project.save_setlist()
            index = load_inspiration_index(root)
            if any([drop(entry) for entry in index.values()]):
                save_inspiration_index(root, index)
            for name in take_files:
                (root / "completed_takes" / name).unlink(missing_ok=True)

        if local_dir is not None:
            shutil.rmtree(local_dir)
        return {"takes_deleted": len(filenames)}

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
        """Rename a completed take's audio (and video/MIDI, if present)
        file(s) on disk to new_instrument's naming convention, returning
        the new TakeInfo — reassign_take's two cases (an ordinary setlist
        track, and a track drawn from a song set slot) do this
        identical file move; only where the resulting TakeInfo then gets
        stored differs."""
        from .utils import next_take_number, take_filename
        old_stem = Path(take.filename).stem
        old_audio_path = project.completed_takes_dir / take.filename
        old_video_path = project.completed_takes_dir / f"{old_stem}.mp4"
        old_midi_path = project.completed_takes_dir / f"{old_stem}.mid"
        ext = Path(take.filename).suffix.lstrip(".") or "flac"

        new_take_number = next_take_number(project.completed_takes_dir, track_name, new_instrument)
        new_filename = take_filename(track_name, new_instrument, new_take_number, source, backing_track, ext)
        new_stem = Path(new_filename).stem
        new_audio_path = project.completed_takes_dir / new_filename
        new_video_path = project.completed_takes_dir / f"{new_stem}.mp4"
        new_midi_path = project.completed_takes_dir / f"{new_stem}.mid"

        if old_audio_path.exists():
            shutil.move(str(old_audio_path), str(new_audio_path))
        has_video = take.has_video and old_video_path.exists()
        if has_video:
            shutil.move(str(old_video_path), str(new_video_path))
        has_midi = take.has_midi and old_midi_path.exists()
        if has_midi:
            shutil.move(str(old_midi_path), str(new_midi_path))

        return TakeInfo(
            instrument=new_instrument, take_number=new_take_number, filename=new_filename,
            volume=take.volume, has_video=has_video, has_midi=has_midi, input_label=take.input_label,
        )

    def _update_session_take_snapshot(
        self, session_dir: str, track_index: int, old_filename: str, new_take: TakeInfo,
    ) -> None:
        """Keep session_dir's own "takes" snapshot (see get_session_
        detail's docstring) in sync with a take reassign_take just
        renamed — without this, the session that originally produced a
        take keeps pointing at a filename that no longer exists the
        moment it's ever reassigned, which later surfaces as a "could
        not download" error for a take that isn't actually gone, just
        renamed.

        Best-effort and silent on any failure (log read/write, or this
        take not actually appearing under old_filename) — reassign_take's
        own, more important work (the actual file move) has already
        succeeded by the time this runs; a session history staying one
        step stale isn't worth failing the whole reassignment over."""
        try:
            log_path, data = self._read_session_log(session_dir)
        except BackendError:
            return
        entries = (data.get("takes") or {}).get(str(track_index))
        if not entries:
            return
        changed = False
        for i, entry in enumerate(entries):
            if entry.get("filename") == old_filename:
                entries[i] = {**entry, "instrument": new_take.instrument, **asdict(new_take)}
                changed = True
        if not changed:
            return
        if log_path is not None:
            log_path.write_text(json.dumps(data, indent=2))
        else:
            from .sync import write_remote_session_log
            config = self.get_config()
            if config.backup_server:
                write_remote_session_log(config.backup_server, session_dir, data)

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
        from .processing.splicer import filter_draw_for_track
        from .vault import load_inspiration_index, save_inspiration_index, vault_root
        root = vault_root(config)

        if str(track_index) in filter_slot_draws:
            # Drawn from a song set slot — its take isn't filed
            # on any TrackEntry in the setlist (a song set slot's own entry
            # never holds one, see TrackEntry's docstring); it lives in the
            # shared vault-wide inspiration-take index instead, keyed by
            # exactly which song this session drew — filter_slot_draws
            # records that, same lookup get_session_detail/analyze_take
            # already use to find it reliably.
            track_id = (filter_draw_for_track(data, track_index, track_name) or {}).get("inspiration_track_id")
            index = load_inspiration_index(root)
            shared = index.get(str(track_id)) if track_id else None
            if shared is None:
                raise BackendError(f"No shared inspiration record found for '{track_name}'.")
            take = shared.get_take_for_instrument(old_instrument)
            if take is None:
                raise BackendError(f"No take is currently filed under '{old_instrument}' for '{track_name}'.")

            # shared.name, not the caller's track_name: a slot that skipped
            # past earlier draws logs all of them under this same
            # track_index, so track_name may be a skipped draw's name —
            # which once renamed a take's file after the wrong song.
            new_take = self._move_take_file(
                project, shared.name or track_name, new_instrument, take,
                source="inspiration", backing_track=f"inspiration_{track_id}",
            )
            del shared.preferred_takes[old_instrument]
            shared.set_preferred_take(new_instrument, new_take)
            save_inspiration_index(root, index)
            self._update_session_take_snapshot(session_dir, track_index, take.filename, new_take)
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

        self._update_session_take_snapshot(session_dir, track_index, take.filename, new_take)

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
            # Same reasoning as get_session_detail: a song set slot's own
            # TrackEntry never holds a take — look the drawn song up in
            # the shared vault-wide inspiration-take index instead.
            from .processing.splicer import filter_draw_for_track
            from .vault import get_inspiration_entry, vault_root
            track_id = (filter_draw_for_track(data, track_index, track_name) or {}).get("inspiration_track_id")
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

        # Narrow the comparison to instruments on the take's own actual
        # input — take.input_label is the physical input this take was
        # *actually* recorded from, captured at record time (see backend.
        # py's _SessionEvent/_save_session_log and processing/splicer.py's
        # CompletedTake) — an immutable fact about the take itself, so
        # this is the most precise possible scope and survives instrument_
        # name's label being renamed or removed from config entirely
        # since the take was recorded. Comparing against unrelated
        # hardware inputs (e.g. a guitar's DI against a piano on a
        # different device entirely) can't be what actually got recorded
        # here regardless of what the audio sounds like, and would
        # reintroduce the classifier's bias toward whichever candidate has
        # the widest default frequency range (see audio/instrument_
        # classifier.py's module docstring and _DEFAULT_RANGES_BY_LABEL).
        candidates = [i for i in config.instruments if i.input_label == take.input_label]
        if not candidates:
            # Nothing currently configured shares the take's actual input
            # — exactly the take that most needs analysis, not less:
            # refusing outright leaves no way to find out where it
            # actually belongs. Fall back to comparing against every
            # currently configured instrument instead — the bias caveat
            # above still applies with less precision, but a possibly-
            # imprecise guess beats no guess at all here.
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

    def ensure_backing_track_local(self, take_filename: str) -> str:
        config = self.get_config()
        records, _index = self._find_track_records(config, take_filename)
        if not records:
            raise BackendError(f"Could not find any project or record referencing take '{take_filename}'.")
        entry = records[0][1]
        if not entry.backing_track:
            raise BackendError(f"'{entry.name}' has no backing track.")
        local_path = Path(config.session_vault_path) / "backing_tracks" / entry.backing_track
        if local_path.exists():
            return str(local_path)
        if entry.inspiration_track_id:
            from .inspiration import InspirationError, download_inspiration_track
            try:
                download_inspiration_track(entry, local_path, config)
            except InspirationError as e:
                raise BackendError(f"Could not download the backing track for '{entry.name}': {e}") from e
            return str(local_path)
        if not config.backup_server:
            raise BackendError(
                f"Backing track '{entry.backing_track}' isn't available locally, and no backup server is configured."
            )
        from .sync import sync_vault_file_down
        if not sync_vault_file_down(config.backup_server, f"backing_tracks/{entry.backing_track}", local_path):
            raise BackendError(f"Could not download '{entry.backing_track}' from {config.backup_server}.")
        return str(local_path)

    def get_backing_playback_path(self, take_filename: str) -> str:
        path = Path(self.ensure_backing_track_local(take_filename))
        from .vault import load_backing_tuning
        cents = load_backing_tuning(Path(self.get_config().session_vault_path), path.name)
        return str(_pitched_playback_path(path, cents))

    def get_take_playback_path(self, project_name: str, filename: str, label: str) -> str:
        path = Path(self.ensure_take_local(project_name, filename))
        settings = self.get_config().compressor_for_label(label)
        play_path = _compressed_playback_path(path, settings)
        return str(play_path)

    def list_completed_takes(self) -> list[dict]:
        config = self.get_config()
        vault_root = Path(config.session_vault_path)
        completed_dir = vault_root / "completed_takes"
        by_filename: dict[str, dict] = {}

        def add(entry: TrackEntry, label: str, take: TakeInfo) -> None:
            try:
                recorded_at = (completed_dir / take.filename).stat().st_mtime
            except OSError:
                recorded_at = None
            by_filename[take.filename] = {
                "track_name": entry.name, "instrument": label, "filename": take.filename,
                "backing_source": entry.source_label(),
                # Full, untrimmed backing-track length — duration_seconds is
                # already net of the trim (see TrackEntry). None if unknown.
                "backing_duration_seconds": (
                    entry.duration_seconds + entry.trim_start_seconds + entry.trim_end_seconds
                    if entry.duration_seconds > 0 else None
                ),
                "take_number": take.take_number, "has_video": take.has_video, "has_midi": take.has_midi,
                "volume": take.volume,
                "recorded_at": recorded_at,
                # Non-destructive "edit backing track" trim (see
                # edit_backing_track) — already-affected takes carry this
                # along to wherever a take dict ends up used for playback
                # (the Completed Takes mixer) or re-edit (its trim editor,
                # ui/backing_trim_editor.py, showing the current values)
                # without a second lookup.
                "trim_start_seconds": entry.trim_start_seconds,
                "trim_end_seconds": entry.trim_end_seconds,
            }

        entry_of: dict[str, TrackEntry] = {}  # row filename -> the record it came from
        for path in Project.list_projects(Path(config.projects_dir)):
            try:
                project = Project.open(path, vault_root)
            except Exception:
                continue
            for track in project.setlist.tracks:
                for label, take in track.preferred_takes.items():
                    add(track, label, take)
                    entry_of[take.filename] = track

        from .vault import load_inspiration_index
        for entry in load_inspiration_index(vault_root).values():
            for label, take in entry.preferred_takes.items():
                add(entry, label, take)
                entry_of[take.filename] = entry

        # Every take file on disk per song+label — one directory listing
        # for the whole vault, then scanned once per distinct backing
        # track (two records can share a song name but not its audio).
        try:
            stems = [p.stem for p in completed_dir.iterdir() if p.suffix == ".flac"]
        except OSError:
            stems = []
        scans: dict[tuple, dict[str, list[dict]]] = {}
        preferred_labels: dict[str, set[str]] = {}
        for row in by_filename.values():
            preferred_labels.setdefault(row["track_name"], set()).add(row["instrument"])
        for row in by_filename.values():
            entry = entry_of[row["filename"]]
            key = (entry.name, entry.source_label(), entry.backing_track)
            if key not in scans:
                scans[key] = _scan_song_take_files(stems, entry, completed_dir)
            song_files = scans[key]
            alternates = {t["filename"]: t for t in song_files.get(row["instrument"], [])}
            # The current take is always offered, even if its file isn't on
            # local disk right now (pruned under "remote" vault mode).
            alternates.setdefault(row["filename"], {
                "take_number": row["take_number"], "filename": row["filename"],
                "has_video": row["has_video"], "has_midi": row["has_midi"],
            })
            row["alternate_takes"] = sorted(alternates.values(), key=lambda t: t["take_number"])
            row["unpreferred_takes"] = {
                label: takes for label, takes in song_files.items()
                if label not in preferred_labels[row["track_name"]]
            }

        return sorted(by_filename.values(), key=lambda d: d["track_name"].lower())

    def set_preferred_take(self, take_filename: str, instrument: str, new_filename: str | None) -> None:
        config = self.get_config()
        root = Path(config.session_vault_path)
        completed_dir = root / "completed_takes"
        records, inspiration_index = self._find_track_records(config, take_filename)
        if not records:
            raise BackendError(f"Could not find any project or record referencing take '{take_filename}'.")

        new_take: TakeInfo | None = None
        if new_filename is not None:
            stems = [Path(new_filename).stem] if (completed_dir / new_filename).exists() else []
            match = next(
                (t for t in _scan_song_take_files(stems, records[0][1], completed_dir).get(instrument, [])
                 if t["filename"] == new_filename),
                None,
            )
            if match is None:
                raise BackendError(
                    f"'{new_filename}' isn't a {instrument} take of '{records[0][1].name}' in {completed_dir}."
                )
            new_take = TakeInfo(
                instrument=instrument, take_number=match["take_number"], filename=new_filename,
                has_video=match["has_video"], has_midi=match["has_midi"],
            )

        shared_touched = False
        for project, entry in records:
            if new_take is None:
                entry.preferred_takes.pop(instrument, None)
            else:
                entry.set_preferred_take(instrument, new_take)
            if project is not None:
                project.save_setlist()
            else:
                shared_touched = True
        if shared_touched:
            from .vault import save_inspiration_index
            save_inspiration_index(root, inspiration_index)

    def _find_track_records(
        self, config: StudioConfig, take_filename: str,
    ) -> tuple[list[tuple[Project | None, TrackEntry]], dict[str, TrackEntry]]:
        """Every record (a project's own TrackEntry, and/or the shared
        vault-wide inspiration-take index's entry — vault.py) referencing
        `take_filename` in its preferred_takes — the same vault-wide scan
        list_completed_takes does, just stopping at "which record(s)" and
        returned with each project object alongside its entry, rather
        than flattening into plain dicts, so a caller can both read and
        write them back (project.save_setlist() / save_inspiration_index
        with the second return value). A project entry pairs with that
        Project; the shared index's own entry (if any — the only place a
        take drawn from a song set slot ever lives, see TrackEntry's
        docstring) pairs with None instead, since there's no Project to
        save it through. Used by edit_backing_track (resolve what to
        update) and list_completed_takes (read trim_start_seconds/
        trim_end_seconds onto each take row)."""
        root = Path(config.session_vault_path)
        records: list[tuple[Project | None, TrackEntry]] = []
        for path in Project.list_projects(Path(config.projects_dir)):
            try:
                project = Project.open(path, root)
            except Exception:
                continue
            for track in project.setlist.tracks:
                if any(t.filename == take_filename for t in track.preferred_takes.values()):
                    records.append((project, track))

        from .vault import load_inspiration_index
        inspiration_index = load_inspiration_index(root)
        for entry in inspiration_index.values():
            if any(t.filename == take_filename for t in entry.preferred_takes.values()):
                records.append((None, entry))
                break
        return records, inspiration_index

    def get_song_mix(self, track_name: str) -> dict | None:
        from .vault import load_song_mix
        return load_song_mix(Path(self.get_config().session_vault_path), track_name)

    def save_song_mix(self, track_name: str, volumes: dict[str, float], muted: list[str]) -> dict:
        if not track_name:
            raise BackendError("Can't save a mix without a song name.")
        from .vault import save_song_mix
        try:
            return save_song_mix(Path(self.get_config().session_vault_path), track_name, volumes, muted)
        except OSError as e:
            raise BackendError(f"Could not save mix for '{track_name}': {e}") from e

    def edit_backing_track(
        self, take_filename: str, trim_start_seconds: float, trim_end_seconds: float,
    ) -> dict:
        if trim_start_seconds < 0 or trim_end_seconds < 0:
            raise BackendError("Trim amounts can't be negative.")

        config = self.get_config()
        root = Path(config.session_vault_path)
        backing_dir = root / "backing_tracks"

        records, inspiration_index = self._find_track_records(config, take_filename)
        if not records:
            raise BackendError(f"Could not find any project or record referencing take '{take_filename}'.")

        track_name = records[0][1].name
        backing_track = records[0][1].backing_track
        if not backing_track:
            raise BackendError(f"'{track_name}' has no backing track file to trim.")
        backing_path = backing_dir / backing_track
        if not backing_path.exists():
            raise BackendError(f"Backing track file not found: {backing_path}")

        # Always measured fresh from the untouched file — never derived
        # from a previously-stored duration_seconds, which may already
        # reflect an earlier trim (this call replaces, not adds to, the
        # trim amounts, so re-trimming from scratch needs the real,
        # original length every time).
        from .audio.formats import get_duration
        full_duration = get_duration(backing_path)
        if trim_start_seconds + trim_end_seconds >= full_duration:
            raise BackendError(
                f"Trim amount ({trim_start_seconds + trim_end_seconds:.1f}s) leaves nothing of "
                f"'{track_name}' ({full_duration:.1f}s long)."
            )
        new_duration = full_duration - trim_start_seconds - trim_end_seconds

        # Union of every instrument's current take across every matched
        # record, deduplicated by filename — an inspiration-sourced
        # track's project entry and its shared-index mirror reference the
        # exact same files, so this just reports each one once.
        affected_takes: dict[str, str] = {}  # filename -> instrument label
        shared_touched = False
        for project, entry in records:
            entry.trim_start_seconds = trim_start_seconds
            entry.trim_end_seconds = trim_end_seconds
            entry.duration_seconds = new_duration
            for label, take in entry.preferred_takes.items():
                affected_takes[take.filename] = label
            if project is not None:
                project.save_setlist()
            else:
                shared_touched = True

        if shared_touched:
            from .vault import save_inspiration_index
            save_inspiration_index(root, inspiration_index)

        return {
            "track_name": track_name,
            "new_duration_seconds": new_duration,
            "affected_takes": [{"instrument": label, "filename": f} for f, label in affected_takes.items()],
        }

    # --- inspiration ---

    def search_inspiration_artists(self, partial: str) -> list[str]:
        from .inspiration import search_artist_suggestions
        return search_artist_suggestions(self.get_config(), partial)

    def search_inspiration_titles(self, partial: str, artist: str = "") -> list[dict]:
        from .inspiration import search_title_suggestions
        return search_title_suggestions(self.get_config(), partial, artist=artist)

    def search_inspiration_by_filter(self, filter_criteria: dict, all_matches: bool = False) -> list[dict]:
        from .inspiration import InspirationError, search_tracks_by_filter
        try:
            return search_tracks_by_filter(self.get_config(), filter_criteria, all_matches=all_matches)
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

    def get_playback_position(self) -> tuple[float, float]:
        engine = self._get_active_engine()
        if engine is None:
            return (0.0, 0.0)
        return (engine.mixer.position_seconds, engine.mixer.duration_seconds)

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
        """Nudge *this instrument's own* live-monitor gain — see
        AudioEngine.set_instrument_volume and config.Instrument.
        instrument_volume's own comment for why this is per-instrument
        rather than one shared "sticky" fader the way adjust_backing_
        volume/adjust_takes_volume are. Persisted straight onto whichever
        Instrument is currently active (config.get_instrument, matched
        by full_name — get_config() always hands back a fresh object
        graph, so this never assumes the cached _active_session/_active_
        monitor Instrument reference is the same object) and, when the
        "recording" monitoring mode has the instrument routed through
        the interface's own hardware direct monitor instead, mirrored
        onto that fader too — see _apply_hardware_direct_monitor.

        Clamped to [0, MAX_INSTRUMENT_VOLUME_PERCENT] — see that
        constant's own comment for why a ceiling exists at all.

        Meaningful any time an engine is open at all — including the
        ambient monitor-only stream opened by start_monitoring() before
        Record is ever pressed — unlike adjust_backing_volume/adjust_
        takes_volume, which only make sense mid-take."""
        with self._record_lock:
            engine, active_inst = self._get_active_engine_and_inst()
            if engine is None or active_inst is None:
                return
            config = self.get_config()
            inst = config.get_instrument(active_inst.full_name)
            if inst is None:
                return
            inst.instrument_volume = max(0, min(MAX_INSTRUMENT_VOLUME_PERCENT, inst.instrument_volume + delta))
            config.save(self._config_path)
            engine.set_instrument_volume(inst.instrument_volume / 100.0)
            if self._monitoring_mode == "recording":
                input_info = config.resolve_input(inst.input_label)
                self._apply_hardware_direct_monitor(input_info, True, inst.instrument_volume)
            self._emit("recording_status", {"status": f"Instrument volume: {inst.instrument_volume}%"})

    # --- audio filters (compressor now, more later) ---

    def set_compressor_settings(self, label: str, settings: dict) -> None:
        with self._record_lock:
            config = self.get_config()
            new_settings = CompressorSettings(**settings)
            config.compressor_settings[label] = new_settings
            config.save(self._config_path)
            engine, inst = self._get_active_engine_and_inst()
            if engine is not None and inst is not None and inst.label == label:
                engine.set_compressor_settings(new_settings)

    def set_synth_voice(self, instrument_name: str, voice: str) -> None:
        from .audio.synth import SYNTH_VOICES
        if voice not in SYNTH_VOICES:
            raise BackendError(f"Unknown synth voice '{voice}' (must be one of {', '.join(SYNTH_VOICES)}).")
        with self._record_lock:
            config = self.get_config()
            inst = config.get_instrument(instrument_name)
            if inst is None:
                raise BackendError(f"Instrument '{instrument_name}' not found.")
            if not inst.is_midi:
                raise BackendError(f"'{inst.full_name}' isn't a MIDI-driven instrument.")
            inst.synth_voice = voice
            config.save(self._config_path)
            engine, active_inst = self._get_active_engine_and_inst()
            if (
                engine is not None and active_inst is not None
                and active_inst.full_name.lower() == inst.full_name.lower()
                and engine.synth is not None
            ):
                engine.synth.set_voice(voice)
            monitor = self._active_monitor
            if monitor is not None and monitor.all_inputs:
                synth = monitor.synths_by_device.get(inst.midi_device.lower())
                if synth is not None:
                    synth.set_voice(voice)
        self._emit("synth_voice_changed", {"instrument": inst.full_name, "voice": voice})

    def get_voice_switch(self) -> dict | None:
        from .audio.midi_input import list_midi_devices, match_port
        from .audio.synth import DEFAULT_SYNTH_VOICE
        with self._record_lock:
            if self._active_session is not None:
                return None
            config = self.get_config()
            if self._monitor_all_inputs:
                candidates = [inst for inst in config.instruments if inst.is_midi]
            else:
                inst = config.get_instrument(config.last_selected_instrument)
                candidates = [inst] if inst is not None and inst.is_midi else []
        candidates = [inst for inst in candidates if not inst.keyboard_driver.voice_cc]
        if not candidates:
            return None
        ports = list_midi_devices()
        for inst in candidates:
            if match_port(inst.midi_device, ports) is not None:
                return {"instrument": inst.full_name, "voice": inst.synth_voice or DEFAULT_SYNTH_VOICE}
        return None

    def cycle_synth_voice(self) -> dict | None:
        if self.is_session_active():
            raise BackendError("The voice can only be changed from the Stream Deck before a session starts.")
        target = self.get_voice_switch()
        if target is None:
            return None
        voice = self._next_synth_voice(target["voice"])
        self.set_synth_voice(target["instrument"], voice)
        return {"instrument": target["instrument"], "voice": voice}

    @staticmethod
    def _next_synth_voice(voice: str) -> str:
        from .audio.synth import SYNTH_VOICES
        index = SYNTH_VOICES.index(voice) if voice in SYNTH_VOICES else -1
        return SYNTH_VOICES[(index + 1) % len(SYNTH_VOICES)]

    # --- keyboard control knobs (voice / backing pitch) ---

    def _ensure_control_listener(self) -> None:
        """Start (once) the background thread that keeps a knob-only
        MidiInput open on every plugged-in keyboard whose driver (see
        keyboard_drivers/) has a voice or backing-pitch knob — independent
        of whatever engine is open, so
        e.g. the QX25's K2 can tune the backing track during a guitar
        session, not only while the QX25 itself is being played. Only
        started by things that mean this backend really is driving the
        studio's hardware (start_monitoring/set_audio_hardware_present),
        so a Tk UI that's only a Remote client never listens."""
        if self._control_thread is not None:
            return
        self._control_thread = threading.Thread(target=self._run_control_listener, daemon=True, name="midi-knobs")
        self._control_thread.start()

    def _run_control_listener(self) -> None:
        """Every 2s: (re)subscribe to each wanted keyboard that's plugged in
        and drop any that went away or no longer have a knob configured —
        so a keyboard plugged in mid-session, or a Studio Setup change,
        is picked up without a restart."""
        from .audio.midi_input import MidiInput, MidiUnavailableError, list_midi_devices, match_port
        while True:
            try:
                config = self.get_config()
                self._control_config = config
                wanted = {
                    inst.midi_device.lower(): inst for inst in config.instruments
                    if inst.is_midi and inst.keyboard_driver.handled_ccs
                }
                ports = list_midi_devices()
                for device, midi_in in list(self._control_inputs.items()):
                    inst = wanted.get(device)
                    if inst is None or match_port(inst.midi_device, ports) is None:
                        midi_in.close()
                        del self._control_inputs[device]
                        self._voice_knob_zone.pop(device, None)
                for device, inst in wanted.items():
                    if device in self._control_inputs or match_port(inst.midi_device, ports) is None:
                        continue
                    try:
                        self._control_inputs[device] = MidiInput(
                            inst.midi_device,
                            on_control_change=lambda cc, value, device=device: self._on_control_change(
                                device, cc, value,
                            ),
                        )
                    except MidiUnavailableError:
                        pass
            except Exception as e:  # never let the listener die
                print(f"takeloom: MIDI knob listener error: {e}")
            time.sleep(2.0)

    def _on_control_change(self, device: str, cc: int, value: int) -> None:
        """A knob-listener CC (see _run_control_listener). Runs on rtmidi's
        callback thread, so only cheap, non-blocking work happens here;
        anything that takes self._record_lock or writes config goes to a
        thread of its own."""
        config = self._control_config
        if config is None:
            return
        inst = next((i for i in config.instruments if i.is_midi and i.midi_device.lower() == device), None)
        if inst is None:
            return
        driver = inst.keyboard_driver
        if driver.backing_pitch_cc and cc == driver.backing_pitch_cc:
            self.set_backing_pitch(knob_to_cents(value))
        elif driver.voice_cc and cc == driver.voice_cc:
            from .audio.synth import SYNTH_VOICES
            # The knob's travel split evenly across the voices (left half
            # piano, right half organ) — a position, not a press, so it
            # only acts when the knob actually crosses into another zone.
            zone = min(len(SYNTH_VOICES) - 1, value * len(SYNTH_VOICES) // 128)
            if self._voice_knob_zone.get(device) == zone:
                return
            self._voice_knob_zone[device] = zone
            voice = SYNTH_VOICES[zone]
            name = inst.full_name

            def apply() -> None:
                fresh = self.get_config().get_instrument(name)
                if fresh is not None and fresh.synth_voice != voice:
                    try:
                        self.set_synth_voice(name, voice)
                    except BackendError:
                        pass
            threading.Thread(target=apply, daemon=True).start()

    def set_backing_pitch(self, cents: float) -> bool:
        """Shift the backing track of the session's currently loaded song by
        `cents` (clamped to ±MAX_PITCH_CENTS), live — see audio/pitch_
        shift.py — and remember it for that backing track file (vault.py's
        save_backing_tuning, written half a second after the knob stops
        moving rather than on every message). Returns False, doing
        nothing, if no session has a song loaded — there's no backing
        playing to tune. Safe to call from rtmidi's callback thread: only
        attribute reads and live-safe Mixer calls happen inline."""
        from .audio.pitch_shift import MAX_PITCH_CENTS
        session = self._active_session
        track = session.current_track if session is not None else None
        if track is None or not track.backing_track:
            return False
        cents = max(-MAX_PITCH_CENTS, min(MAX_PITCH_CENTS, float(cents)))
        session.engine.mixer.set_pitch("backing", cents)
        backing_track = track.backing_track
        config = self._control_config or self.get_config()
        root = Path(config.session_vault_path)

        def save() -> None:
            from .vault import save_backing_tuning
            try:
                save_backing_tuning(root, backing_track, cents)
            except OSError as e:
                print(f"takeloom: couldn't save backing tuning for '{backing_track}': {e}")
        if self._tuning_save_timer is not None:
            self._tuning_save_timer.cancel()
        self._tuning_save_timer = threading.Timer(0.5, save)
        self._tuning_save_timer.daemon = True
        self._tuning_save_timer.start()
        shown = round(cents)
        if shown != self._last_emitted_pitch:
            self._last_emitted_pitch = shown
            self._emit("backing_pitch_changed", {"cents": shown, "track_name": track.name})
        return True

    def benchmark_audio_modifiers(self) -> dict:
        from .audio.benchmark import run_audio_modifier_benchmark
        result = run_audio_modifier_benchmark(self.get_config())
        return {**asdict(result), "within_budget": result.within_budget}

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
                # Re-fetched by full_name rather than trusting the cached
                # inst's own instrument_volume — get_config() always hands
                # back a fresh object graph, and another client could have
                # just nudged this instrument's volume (adjust_instrument_
                # volume) since inst was captured.
                fresh_inst = config.get_instrument(inst.full_name) or inst
                input_info = config.resolve_input(fresh_inst.input_label)
                self._apply_hardware_direct_monitor(input_info, mode == "recording", fresh_inst.instrument_volume)
            elif engine is not None and self._active_monitor is not None and self._active_monitor.all_inputs:
                self._apply_hardware_direct_monitor_all(self.get_config(), mode == "recording")
            self._emit("monitoring_mode_changed", {"mode": mode})

    def _apply_hardware_direct_monitor(self, input_info, enabled: bool, instrument_volume_percent: int) -> None:
        """Best-effort: also flip the audio interface's own zero-latency
        hardware direct monitor, for interfaces this is known to work on
        (currently just the studio's Scarlett 4i4 4th Gen — see
        scarlett2_direct_monitor.py). Silently does nothing for any other
        input device; failures here (device unplugged, Focusrite Control 2
        has it open, etc.) are never surfaced — the Record page's on-screen
        reminder is the fallback either way.

        When enabling, uses `instrument_volume_percent` — the specific
        instrument's own Instrument Volume dial level (config.Instrument.
        instrument_volume; every caller already has this in scope for
        whichever instrument it's actually acting on) rather than a fixed
        unity gain, so the dial reaches "recording" monitoring mode too,
        where the instrument is heard purely through this hardware path
        — see adjust_instrument_volume.

        Every *other* configured input channel on the interface is muted
        at the same time, so once one instrument is selected nothing left
        over from the all-inputs monitor (see _apply_hardware_direct_
        monitor_all) keeps passing through. `input_info` None (a MIDI
        instrument, which has no channel of its own) mutes them all."""
        config = self.get_config()
        volumes = {
            il.channel: 0.0 for il in config.input_labels if il.device == FOCUSRITE_DEVICE_NAME
        }
        if input_info is not None and input_info.device == FOCUSRITE_DEVICE_NAME:
            volumes[input_info.channel] = (instrument_volume_percent / 100.0) if enabled else 0.0
        if not volumes:
            return
        try:
            set_channel_gains(volumes)
        except Exception:
            pass

    def _apply_hardware_direct_monitor_all(self, config: StudioConfig, enabled: bool) -> None:
        """_apply_hardware_direct_monitor for the all-inputs monitor: every
        configured channel on the interface open at once (each at the
        loudest Instrument Volume of any instrument assigned to it), or all
        of them muted. Same best-effort, never-surfaced failure handling."""
        volumes: dict[int, float] = {}
        for il in config.input_labels:
            if il.device != FOCUSRITE_DEVICE_NAME:
                continue
            levels = [
                inst.instrument_volume for inst in config.instruments
                if not inst.is_midi and inst.input_label == il.label
            ]
            volumes[il.channel] = (max(levels, default=100) / 100.0) if enabled else 0.0
        if not volumes:
            return
        try:
            set_channel_gains(volumes)
        except Exception:
            pass


    def _resolve_midi_route(self, config: StudioConfig, sd, resolve_device) -> tuple[int | None, int]:
        """Best-effort analog (input_device, input_channels) to pair with
        a MIDI-driven instrument's duplex audio stream: its captured
        audio is discarded entirely (AudioEngine ignores `indata`
        whenever its synth is set — see audio/engine.py's _callback), but
        PortAudio's combined record+playback Stream still needs *some*
        valid input device/channel count. Reuses whichever InputLabel is
        configured first — the studio's real interface, already proven
        to open elsewhere in this exact config — falling back to the
        system's own default input device if none is configured at all."""
        if config.input_labels:
            in_dev = resolve_device(sd, config.input_labels[0].device, "input")
            if in_dev is not None:
                return in_dev, 1
        return None, 1

    def _build_engine_for_instrument(
        self, config: StudioConfig, inst: Instrument, *,
        monitor_instrument: bool, instrument_volume: float | None = None,
        midi_log: "MidiEventLog | None" = None,
    ) -> tuple[object, object | None, object | None]:
        """Build (but don't start) an AudioEngine for `inst`, branching on
        whether it's MIDI-driven (see config.Instrument.is_midi) or an
        ordinary analog input — the device-resolution/validation logic
        every recording/monitoring/video-check call site used to
        duplicate inline. Returns (engine, midi_input, input_info):

        - midi_input is an already-started MidiInput feeding the engine's
          own Synth (see audio/synth.py) for a MIDI instrument, else
          None. Callers must hold onto it and .close() it whenever they
          tear the engine down — its USB MIDI port stays open,
          independent of the engine's own sd.Stream lifecycle, until
          then (mirrors how a caller already has to hold onto/close a
          VideoRecorder alongside its engine).
        - input_info is the resolved InputLabel for an analog instrument,
          or None for a MIDI one — same as config.resolve_input's own
          "not applicable" return, so a caller that only cares about the
          analog case (e.g. _apply_hardware_direct_monitor, which
          already no-ops on None) can keep branching on it exactly as
          before.

        `instrument_volume`, left at its default (None), resolves to
        `inst.instrument_volume / 100.0` — this instrument's own saved
        dial level (see that field's own comment for why it's per-
        instrument). Pass an explicit value only to override that, which
        only start_video_check does (a fixed 1.0 — Video Check has no
        Instrument Volume dial of its own to reflect).

        `midi_log`, only meaningful when `inst.is_midi`: every note/
        sustain/volume/expression event MidiInput delivers also gets
        appended there, frame-stamped against `engine.session_frames` —
        see audio/midi_log.py. Only _begin_session_locked passes one (a
        real recording is the only case that ever produces a completed
        take worth revoicing later); monitoring/video-check/latency-test
        leave it None and nothing is captured.

        Raises BackendError on any device-resolution failure (bad output
        device, bad/unavailable MIDI device, bad input channel/device for
        an analog instrument) — every existing call site already either
        wraps this kind of setup in a try/except or lets BackendError
        propagate, so this doesn't change any caller's error-handling
        shape, just where the logic that can raise it lives."""
        if instrument_volume is None:
            instrument_volume = inst.instrument_volume / 100.0
        import sounddevice as sd
        from .audio.devices import resolve_device

        out_dev = resolve_device(sd, config.output_device, "output")
        if config.output_device and out_dev is None:
            raise BackendError(f"Output device '{config.output_device}' not found.")
        out_info = sd.query_devices(out_dev, "output")
        output_channels = min(config.output_channels, out_info["max_output_channels"])

        input_info = None
        synth = None
        if inst.is_midi:
            from .audio.synth import Synth
            synth = Synth(config.sample_rate, voice=inst.synth_voice)
            in_dev, input_channels = self._resolve_midi_route(config, sd, resolve_device)
            monitor_channel = 0
        else:
            input_info = config.resolve_input(inst.input_label)
            if input_info is None:
                raise BackendError(f"Input label '{inst.input_label}' not found in config.")
            in_dev = resolve_device(sd, input_info.device, "input")
            if in_dev is None:
                raise BackendError(f"Input device '{input_info.device}' not found.")
            in_info = sd.query_devices(in_dev, "input")
            max_in = in_info["max_input_channels"]
            if input_info.channel > max_in:
                raise BackendError(
                    f"Instrument '{inst.full_name}' needs input channel {input_info.channel} "
                    f"but device only has {max_in} channels."
                )
            input_channels = max(input_info.channel, 1)
            monitor_channel = input_info.channel - 1

        from .audio.engine import AudioEngine
        engine = AudioEngine(
            sample_rate=config.sample_rate, buffer_size=config.buffer_size,
            input_device=in_dev, output_device=out_dev,
            input_channels=input_channels, output_channels=max(1, output_channels),
            monitor_channel=monitor_channel,
            compressor_settings=config.compressor_for_label(inst.label),
            monitor_instrument=monitor_instrument, instrument_volume=instrument_volume,
            synth=synth,
        )

        midi_input = None
        if inst.is_midi:
            from .audio.midi_input import MidiInput, MidiUnavailableError

            # Built after `engine` exists (unlike everything else above,
            # which only has to precede it) specifically so these closures
            # can read engine.session_frames — the same frame counter
            # process_session's audio splicing keys off of, so a take's
            # sliced-out .mid (see audio/midi_log.py) lines up with its
            # .flac exactly. Each still forwards to the Synth exactly as
            # before; midi_log is only ever an additional tap, never a
            # substitute.
            def on_note_on(note: int, velocity: int) -> None:
                if midi_log is not None:
                    midi_log.append(engine.session_frames, "note_on", note=note, velocity=velocity)
                synth.note_on(note, velocity)

            def on_note_off(note: int) -> None:
                if midi_log is not None:
                    midi_log.append(engine.session_frames, "note_off", note=note)
                synth.note_off(note)

            def on_sustain(down: bool) -> None:
                if midi_log is not None:
                    midi_log.append(engine.session_frames, "sustain", down=down)
                synth.set_sustain(down)

            def on_volume(value: int) -> None:
                if midi_log is not None:
                    midi_log.append(engine.session_frames, "volume", value=value)
                synth.set_channel_volume(value)

            def on_expression(value: int) -> None:
                if midi_log is not None:
                    midi_log.append(engine.session_frames, "expression", value=value)
                synth.set_expression(value)

            try:
                midi_input = MidiInput(
                    inst.midi_device, on_note_on=on_note_on, on_note_off=on_note_off,
                    on_sustain=on_sustain, on_volume=on_volume,
                    on_expression=on_expression, volume_cc=inst.keyboard_driver.volume_cc,
                    ignore_ccs=inst.keyboard_driver.handled_ccs,
                )
            except MidiUnavailableError as e:
                raise BackendError(str(e)) from e
            midi_input.start()

        return engine, midi_input, input_info

    def _build_all_inputs_engine(self, config: StudioConfig) -> tuple[object, list, dict]:
        """Build (but don't start) the all-inputs monitor engine: every
        configured input channel on one device (the first input label's
        that resolves — channels on any other device are left out, since
        one duplex stream can only read one input device) summed together,
        plus a Synth for every MIDI keyboard that's connected right now,
        each fed by its own already-open MidiInput. Returns (engine,
        midi_inputs, synths_by_device); the caller owns closing
        midi_inputs.

        Nothing here is recorded or classified — it only exists so every
        input is audible until auto-detect picks one (see
        _monitor_all_inputs). Raises BackendError if the output device
        can't be resolved or there's nothing at all to monitor."""
        import sounddevice as sd
        from .audio.devices import resolve_device
        from .audio.midi_input import MidiInput
        from .audio.synth import Synth

        out_dev = resolve_device(sd, config.output_device, "output")
        if config.output_device and out_dev is None:
            raise BackendError(f"Output device '{config.output_device}' not found.")
        out_info = sd.query_devices(out_dev, "output")
        output_channels = min(config.output_channels, out_info["max_output_channels"])

        in_dev = None
        max_in = 0
        channels: list[int] = []
        for il in config.input_labels:
            dev = resolve_device(sd, il.device, "input")
            if dev is None:
                continue
            if in_dev is None:
                in_dev = dev
                max_in = sd.query_devices(dev, "input")["max_input_channels"]
            if dev != in_dev or not 1 <= il.channel <= max_in:
                continue
            if il.channel - 1 not in channels:
                channels.append(il.channel - 1)

        synths = []
        synths_by_device: dict = {}
        midi_inputs = []
        seen_devices: set[str] = set()
        for inst in config.instruments:
            if not inst.is_midi or inst.midi_device.lower() in seen_devices:
                continue
            seen_devices.add(inst.midi_device.lower())
            synth = Synth(config.sample_rate, voice=inst.synth_voice)
            try:
                midi_in = MidiInput(
                    inst.midi_device, on_note_on=synth.note_on, on_note_off=synth.note_off,
                    on_sustain=synth.set_sustain, on_volume=synth.set_channel_volume,
                    on_expression=synth.set_expression, volume_cc=inst.keyboard_driver.volume_cc,
                    ignore_ccs=inst.keyboard_driver.handled_ccs,
                )
            except Exception:
                continue  # not plugged in right now — monitor everything else
            synths.append(synth)
            synths_by_device[inst.midi_device.lower()] = synth
            midi_inputs.append(midi_in)

        if not channels and not synths:
            raise BackendError("No inputs available to monitor right now.")
        if in_dev is None:
            in_dev, _ = self._resolve_midi_route(config, sd, resolve_device)

        from .audio.engine import AudioEngine
        try:
            engine = AudioEngine(
                sample_rate=config.sample_rate, buffer_size=config.buffer_size,
                input_device=in_dev, output_device=out_dev,
                input_channels=(max(channels) + 1) if channels else 1,
                output_channels=max(1, output_channels),
                monitor_instrument=self._monitoring_mode == "production",
                monitor_channels=channels, extra_synths=synths,
            )
        except Exception:
            for m in midi_inputs:
                m.close()
            raise
        return engine, midi_inputs, synths_by_device

    def monitoring_description(self) -> str:
        """What ambient monitoring is listening to right now, for the
        server's console log (see rig_watcher.py) — "" if nothing's open."""
        with self._record_lock:
            monitor = self._active_monitor
            if monitor is None:
                return ""
            if monitor.all_inputs:
                names = [il.label for il in self.get_config().input_labels]
                names += [m.device_name for m in monitor.midi_inputs]
                return f"all inputs ({', '.join(names)})" if names else "all inputs"
            return f"'{monitor.inst.full_name}'"

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
        self._ensure_control_listener()
        with self._record_lock:
            return self._start_monitoring_locked()

    def _start_monitoring_locked(self) -> bool:
        """start_monitoring()'s body, for callers that already hold
        self._record_lock (the teardown of any other engine, so ambient
        monitoring resumes right after)."""
        if (
            self._audio_hardware_absent
            or self._active_latency_test is not None
            or self._active_video_check is not None
            or self._active_session is not None
            or self._active_instrument_test is not None
            or self._active_detect_all is not None
            or self._active_auto_detect is not None
            or self._active_monitor is not None
        ):
            return False
        config = self.get_config()
        if self._monitor_all_inputs:
            midi_inputs: list = []
            try:
                engine, midi_inputs, synths_by_device = self._build_all_inputs_engine(config)
                engine.start()
            except Exception:
                for m in midi_inputs:
                    m.close()
                return False
            self._apply_hardware_direct_monitor_all(config, self._monitoring_mode == "recording")
            self._active_monitor = _ActiveMonitor(
                engine=engine, inst=None, all_inputs=True, midi_inputs=midi_inputs,
                synths_by_device=synths_by_device,
            )
            return True
        inst = config.get_instrument(config.last_selected_instrument)
        if inst is None:
            return False
        midi_input = None
        try:
            engine, midi_input, input_info = self._build_engine_for_instrument(
                config, inst, monitor_instrument=self._monitoring_mode == "production",
            )
            engine.start()
        except Exception:
            if midi_input is not None:
                midi_input.close()
            return False
        self._apply_hardware_direct_monitor(input_info, self._monitoring_mode == "recording", inst.instrument_volume)
        self._active_monitor = _ActiveMonitor(engine=engine, inst=inst, midi_input=midi_input)
        return True

    def _close_active_monitor(self) -> None:
        """Release start_monitoring()'s ambient stream so something else can
        open the hardware exclusively. Called with self._record_lock already
        held, same as _apply_hardware_direct_monitor."""
        if self._active_monitor is not None:
            self._active_monitor.engine.stop()
            if self._active_monitor.midi_input is not None:
                self._active_monitor.midi_input.close()
            for midi_in in self._active_monitor.midi_inputs:
                midi_in.close()
            self._active_monitor = None

    def set_audio_hardware_present(self, present: bool) -> bool:
        """The always-running `takeloom server`'s hardware watcher (see
        rig_watcher.py) saw the configured audio interface get powered on
        (`present`) or off. Returns whether ambient monitoring is open
        afterwards.

        Off: end whatever's using the hardware — a still-open session is
        ended normally (stop_recording(), so everything captured up to that
        point is kept and post-processed), an in-progress auto-detect scan
        is cancelled, and the ambient monitor stream is closed — and stop
        any of their teardowns from reopening monitoring on the vanished
        device.

        On: re-initialize PortAudio so it can see the device at all (it
        snapshots its device list at initialization — see refresh_devices),
        then open ambient monitoring, which also sets the Scarlett's
        hardware direct monitor (see _apply_hardware_direct_monitor). The
        re-init is skipped if any stream is somehow still open, since
        terminating PortAudio under an open stream isn't safe."""
        self._ensure_control_listener()
        if not present:
            self._audio_hardware_absent = True
            self.stop_auto_detect_instrument()
            self.stop_recording()
            with self._record_lock:
                self._close_active_monitor()
            return False
        with self._record_lock:
            self._audio_hardware_absent = False
            # Freshly powered on: nothing's been identified for this sitting
            # yet, so start out hearing every input — see _monitor_all_inputs.
            self._monitor_all_inputs = True
            busy = any(a is not None for a in (
                self._active_latency_test, self._active_video_check, self._active_session,
                self._active_instrument_test, self._active_detect_all, self._active_auto_detect,
                self._active_monitor,
            ))
            if not busy:
                try:
                    import sounddevice as sd
                    sd._terminate()
                    sd._initialize()
                except Exception:
                    pass
            self._start_monitoring_locked()
            monitoring = self._active_monitor is not None
        self._preview.restart()
        return monitoring

    def stop_monitoring(self) -> None:
        """Close start_monitoring()'s ambient stream, if open — e.g. when
        the UI switches to a remote backend, so this machine stops holding
        its own audio/MIDI hardware for no reason."""
        with self._record_lock:
            self._close_active_monitor()

    def restart_monitoring(self) -> bool:
        with self._record_lock:
            config = self.get_config()
            monitor = self._active_monitor
            if monitor is not None:
                if monitor.all_inputs and self._monitor_all_inputs:
                    return True  # already monitoring the right thing
                if (
                    not monitor.all_inputs and not self._monitor_all_inputs
                    and monitor.inst.full_name.lower() == (config.last_selected_instrument or "").lower()
                ):
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

        # Non-destructive "edit backing track" trim (edit_backing_track) —
        # a virtual playback window applied here (never to the files
        # themselves) to both the backing track and every already-
        # recorded take layered in below, so a new take recorded this
        # session is already exactly this length with nothing further to
        # do, while an existing take recorded before the trim was set
        # stays in sync with the now-"shorter" backing track instead of
        # replaying its own copy of the trimmed-off intro/outro.
        song_trim_start = round(track.trim_start_seconds * config.sample_rate)
        song_trim_end = round(track.trim_end_seconds * config.sample_rate)

        if backing_path.exists():
            engine.mixer.add_source(
                "backing", backing_path, volume=track.volume / 100.0,
                trim_frames=song_trim_start, trim_end_frames=song_trim_end,
            )
            # This recording's own pitch correction, if it's ever been
            # tuned (set_backing_pitch) — so it loads already in tune.
            from .vault import load_backing_tuning
            engine.mixer.set_pitch(
                "backing", load_backing_tuning(Path(config.session_vault_path), track.backing_track),
            )
            self._last_emitted_pitch = None

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
            if other_inst not in INSTRUMENT_LABELS:
                # A stale/orphaned key — e.g. a bare "bass"/"acoustic"/
                # "electric" from before instrument labels were
                # standardized to the current vocabulary (see
                # config.INSTRUMENT_LABELS), rather than "electric-bass"/
                # "acoustic-guitar"/"electric-guitar" etc. Not a real
                # label any instrument could ever actually be assigned,
                # so it can't legitimately be "some other instrument's
                # take" layering in here — skip it rather than silently
                # mixing in audio filed under a name that was never a
                # real instrument in the first place.
                continue
            take_path = project.completed_takes_dir / take_info.filename
            if take_path.exists():
                effective_vol = take_info.volume * (track.takes_volume / 100.0)
                engine.mixer.add_source(
                    f"take:{other_inst}", take_path, volume=effective_vol,
                    trim_frames=trim + song_trim_start, trim_end_frames=song_trim_end,
                    compressor_settings=config.compressor_for_label(other_inst),
                )

        engine.mixer.reset()
        session.current_track = track
        session.current_track_index = index
        self._log_session_event(
            "track_loaded", frame=engine.session_frames, track_index=index, track_name=track.name,
        )

    def _resolve_filter_slot(
        self, config: StudioConfig, track: TrackEntry, instrument_name: str, exclude_id: int | None = None,
    ) -> TrackEntry:
        """Resolve `track` for `instrument_name` to record. An ordinary
        track passes through unchanged; a song set slot draws one song from
        its list uniformly at random (see _pick_filter_match). Either way,
        the setlist itself never gains a new entry here — see TrackEntry's
        docstring and _resolve_filter_slot_for_session's caching wrapper,
        which is what actually gets called during a session; this is the
        pure "pick one" step, split out for testability.

        `exclude_id` (redraw_current_track's use) leaves one specific
        inspiration_track_id out of consideration — so "give me a
        different one" doesn't just hand back what's already loaded —
        falling back to allowing it anyway if excluding it would leave no
        candidates at all (a set of only one song shouldn't error out just
        because that one song is the one being redrawn away from)."""
        if not track.is_inspiration_filter:
            return track
        from .inspiration import build_inspiration_track_entry
        matches = list(track.song_set)
        if not matches:
            raise BackendError(f"The song set '{track.name}' has no songs in it.")

        from .vault import load_inspiration_index, vault_root
        index = load_inspiration_index(vault_root(config))
        chosen = self._pick_filter_match(matches, exclude_id=exclude_id)
        entry = build_inspiration_track_entry(chosen)

        # build_inspiration_track_entry only knows the raw inspiration-
        # server record — if this same song already has a shared-index
        # entry (e.g. some other instrument/project already recorded it,
        # or it's been through edit_backing_track), carry its trim over
        # too, so a song set slot redrawing a previously-trimmed song keeps
        # getting the trimmed version rather than silently reverting to
        # the untrimmed original the moment it's drawn fresh.
        shared = index.get(str(chosen.get("id")))
        if shared is not None and (shared.trim_start_seconds or shared.trim_end_seconds):
            entry.trim_start_seconds = shared.trim_start_seconds
            entry.trim_end_seconds = shared.trim_end_seconds
            entry.duration_seconds = max(
                0.0, entry.duration_seconds - shared.trim_start_seconds - shared.trim_end_seconds,
            )
        return entry

    @staticmethod
    def _pick_filter_match(matches: list[dict], exclude_id: int | None = None) -> dict:
        """The actual "which song" choice within a song set's `matches` —
        uniformly random, regardless of which songs other instruments
        already have takes on. Shared by _resolve_filter_slot and
        get_filter_slot_previews."""
        candidates = [m for m in matches if m.get("id") != exclude_id] or matches
        return random.choice(candidates)

    def _resolve_filter_slot_for_session(
        self, session: "_ActiveSession", config: StudioConfig, track: TrackEntry, index: int,
    ) -> TrackEntry:
        """_resolve_filter_slot(), cached for the rest of `session` — a
        song set slot revisited later in the same session (e.g. a manual
        reselect) gets the same resolved track back rather than a fresh
        random draw each time. Also marks the song set slot's own `index` as
        completed for this session (session.completed_track_indices) the
        moment it's drawn, regardless of whether the resulting take itself
        later succeeds — otherwise _advance_locked would keep re-offering
        the same song set slot indefinitely within one session, since the
        slot's own preferred_takes never actually gets a take (the take
        belongs to whatever song got drawn — recorded into the shared
        vault-wide index instead, see vault.record_inspiration_take).
        A non-filter track passes through unchanged (and isn't cached —
        nothing to cache).

        The draw itself may already be cached — either from an earlier
        visit to this slot this session, or pre-warmed at session open by
        _prefetch_setlist_locked (which resolves every slot up front so no
        draw ever has to hit the inspiration server mid-take).
        Either way, `index` is marked completed here, on the cache-hit
        path too: prefetch deliberately doesn't touch completed_track_
        indices (that would make _advance_locked skip every slot before
        the session even starts), so this is the one place a slot counts
        as drawn-for-real."""
        if not track.is_inspiration_filter:
            return track
        resolved = session.resolved_filter_picks.get(index)
        if resolved is None:
            # This queries the inspiration server (up to a 15s timeout) —
            # say so, so a Stream Deck / headless operator isn't left
            # watching an unchanged screen wondering whether the press
            # registered. (With prefetch working, only reached if the
            # up-front resolve for this slot had failed.)
            self._emit("recording_status", {"status": f"Finding a track for the '{track.name}' filter…"})
            resolved = self._resolve_filter_slot(config, track, session.inst.full_name)
            session.resolved_filter_picks[index] = resolved
        session.completed_track_indices.add(index)
        return resolved

    def _prefetch_setlist_locked(
        self, project: Project, inst: Instrument, config: StudioConfig,
    ) -> dict[int, TrackEntry]:
        """Do all the network/heavy-disk work for a session's whole setlist
        up front, at session open — *before* the audio/camera capture is
        even started — so advancing between takes never has to do any of it
        while capture is live (that was audible as choppy monitoring, and
        also front-loaded dead air into a streamed/recorded session):

        - resolve every song set slot now (one draw each), and
        - download every not-yet-local backing track now, so no mid-session
          multi-MB download + FLAC/opus write.

        Only slots/tracks that still need a take for `inst`'s label are
        touched. Best-effort per track: a slot that can't be resolved, or a
        download that fails, is reported and skipped — it just isn't in the
        returned dict / stays absent on disk, and surfaces its error again
        (loudly, via _resolve_filter_slot_for_session / _load_track_locked)
        if and when that track is actually reached, exactly as before, just
        not mid-take.

        Returns {setlist index -> resolved TrackEntry} for the song set slots
        it managed to resolve, to seed _ActiveSession.resolved_filter_picks
        so _resolve_filter_slot_for_session finds them already drawn. Called
        with self._record_lock held."""
        from .inspiration import InspirationError, download_inspiration_track
        label = inst.label
        pending = [
            (i, t) for i, t in enumerate(project.setlist.tracks)
            if t.get_take_for_instrument(label) is None
        ]
        resolved_picks: dict[int, TrackEntry] = {}
        if not pending:
            return resolved_picks
        self._emit("recording_status", {
            "status": f"Preparing session — resolving and downloading {len(pending)} track(s) up front…",
        })
        for n, (index, slot) in enumerate(pending, start=1):
            track = slot
            if slot.is_inspiration_filter:
                try:
                    track = self._resolve_filter_slot(config, slot, inst.full_name)
                except BackendError as e:
                    self._emit("recording_status", {
                        "status": f"Prep {n}/{len(pending)}: couldn't draw from song set '{slot.name}' — {e}",
                    })
                    continue
                resolved_picks[index] = track
            backing_path = project.backing_tracks_dir / track.backing_track
            if track.inspiration_track_id and not backing_path.exists():
                self._emit("recording_status", {
                    "status": f"Preparing session — downloading {n}/{len(pending)}: '{track.name}'…",
                })
                try:
                    download_inspiration_track(track, backing_path, config)
                except InspirationError as e:
                    self._emit("recording_status", {
                        "status": f"Prep {n}/{len(pending)}: download failed for '{track.name}' — {e}",
                    })
        self._emit("recording_status", {"status": "Session ready — everything's downloaded."})
        return resolved_picks

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
        is a song set slot — see _resolve_filter_slot_for_session), or None
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
        if session.midi_input is not None:
            session.midi_input.close()
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
            previous = session.resolved_filter_picks.get(index)
            if previous is not None:
                session.replaced_filter_picks.setdefault(index, []).append(previous)
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
            if inst.is_midi:
                raise BackendError(
                    f"'{inst.full_name}' is a MIDI instrument — its audio is generated inside the "
                    "engine, with no acoustic path from camera to microphone to measure, so the "
                    "camera latency test doesn't apply to it."
                )
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
                compressor_settings=config.compressor_for_label(inst.label),
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
        if inst.is_midi:
            raise BackendError(
                f"'{inst.full_name}' is a MIDI instrument — its notes already carry an exact pitch, "
                "so there's no frequency range to train the way an analog instrument's is."
            )
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
            monitor_channel=input_info.channel - 1, compressor_settings=config.compressor_for_label(inst.label),
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

    def _emit_tuner_reading(self, input_label: str, freq_hz: float, tuning: list[str]) -> None:
        """Resolve freq_hz against `tuning` (see audio/pitch.py's
        nearest_target) and emit it as a "tuner_status" event — shared by
        start_auto_detect_instrument's on_channel_tuner (the pre-
        detection scan) and _attach_tuner_sink below (the post-detection
        ambient-monitor tap), so the Record tab's tuner needle reads the
        same regardless of which one is currently supplying the audio."""
        note, _target_hz, cents = nearest_target(freq_hz, tuning)
        self._emit("tuner_status", {
            "input_label": input_label, "note": note, "cents": cents, "frequency_hz": freq_hz,
        })

    def _attach_tuner_sink(self, engine, input_label: str, tuning: list[str]) -> None:
        """Feed the tuner from `engine`'s realtime instrument-channel
        audio (see audio/engine.py's AudioEngine.set_instrument_sink)
        rather than a dedicated scanning stream — used once start_auto_
        detect_instrument's own TunerTracker-instrumented scan streams
        have already torn down (see on_channel_detected there) but the
        deck's identify_state is still "ready", not yet "idle": ambient
        monitoring just reopened this exact channel anyway so the
        performer can hear themselves, so tapping a TunerTracker onto
        that same already-flowing audio keeps tuning live right up until
        Play actually opens a session (which tears this engine down via
        _close_active_monitor(), same as any other ambient-monitor
        teardown) or Re-identify starts a fresh scan (start_auto_detect_
        instrument's own _close_active_monitor() call does the same —
        see that method). Caller must hold self._record_lock."""
        from .audio.instrument_classifier import TunerTracker
        tuner = TunerTracker(
            engine.sample_rate,
            lambda freq_hz: self._emit_tuner_reading(input_label, freq_hz, tuning),
        )
        engine.set_instrument_sink(tuner.process_block)

    # --- Detect-all (local-only; RemoteBackend refuses) ---

    def _open_channel_classifier_streams(
        self, config: StudioConfig, on_channel_detected, on_channel_active=None, on_channel_stats=None,
        on_channel_tuner=None, shared_engine=None,
    ) -> tuple[list, list[str], list[str]]:
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

        A MIDI-driven instrument (config.Instrument.is_midi) is scanned
        separately from the analog sd.InputStream logic above — its own
        MidiInput port (see audio/midi_input.py) treats any incoming note
        as an immediate, unambiguous detection, no classifier needed —
        but is returned in the same `streams` list (MidiInput exposes the
        same .stop()/.close() shape sd.InputStream does) so callers tear
        everything down uniformly.

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
        content, not an identified instrument. None (skipped, zero cost)
        for start_auto_detect_instrument — start_detect_all's own caller
        is the only one with a UI to show it to (detect-test's stats
        panel).

        `on_channel_tuner(input_label, frequency_hz)`, if given, fires
        from its own background thread (same pattern, via TunerTracker
        rather than SpectralStatsTracker — see that class in audio/
        instrument_classifier.py) roughly every TunerTracker window of
        non-silent audio, assuming a single monophonic note (one plucked/
        bowed string) rather than describing the whole spectrum. Unlike
        on_channel_stats, start_auto_detect_instrument *does* pass this —
        it's the only thing driving the Record tab's tuner needle while
        the deck's "identifying" phase is listening (see recording_
        driver.py); start_detect_all leaves it None.

        `shared_engine`, if given, is an already-running AudioEngine (the
        all-inputs monitor — see start_auto_detect_instrument) whose input
        device is tapped via AudioEngine.add_input_sink instead of opening
        a second sd.InputStream on it, which PortAudio's macOS backend
        can't do (both streams die — see add_input_sink). The tap goes in
        `streams` as a _EngineInputTap, whose stop()/close() remove it.

        Caller must hold self._record_lock and have already called
        self._close_active_monitor() (unless passing shared_engine). Returns (streams, skipped_
        instrument_names, immediately_detected_names) — skipped is every
        instrument whose input couldn't be resolved right now (e.g. its
        interface is powered off), left out rather than failing the
        whole scan; immediately_detected_names is every MIDI-driven
        instrument whose device *could* be resolved, i.e. is connected.
        start_detect_all reports these as detected (calling on_channel_
        detected(name, 1.0) itself, only after this call returns and its
        own record_lock section is done — never from in here, since this
        runs *inside* the caller's held lock); start_auto_detect_instrument
        ignores them and waits for an actual note, so a keyboard that's
        merely plugged in never gets picked. Raises BackendError if config has no
        instruments, or none of their inputs can currently be
        resolved."""
        from .audio.instrument_classifier import (
            InstrumentClassifier, SILENCE_THRESHOLD, SpectralStatsTracker, TunerTracker,
        )
        if not config.instruments:
            raise BackendError("No instruments configured.")

        try:
            import numpy as np
            import sounddevice as sd
        except Exception as e:
            raise BackendError(f"sounddevice unavailable: {e}") from e
        from .audio.devices import resolve_device

        midi_instruments = [inst for inst in config.instruments if inst.is_midi]

        by_device: dict[int, list[tuple[Instrument, int, str]]] = {}
        skipped: list[str] = []
        for inst in config.instruments:
            if inst.is_midi:
                continue  # scanned separately below — see midi_instruments
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

        if not by_device and not midi_instruments:
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
            tuner_trackers = {}
            if on_channel_tuner is not None:
                tuner_trackers = {
                    ch: TunerTracker(
                        config.sample_rate,
                        lambda freq_hz, il=channel_input_label[ch]: on_channel_tuner(il, freq_hz),
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
                    tuner = tuner_trackers.get(ch)
                    if tuner is not None:
                        tuner.process_block(block)
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
                if (
                    shared_engine is not None and shared_engine.input_device == in_dev
                    and channel_count <= shared_engine.input_channels
                ):
                    tap = _EngineInputTap(shared_engine, make_callback(entries))
                    streams.append(tap)
                    continue
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

        # MIDI-driven instruments: a played note (_on_note_on) is the
        # detection — what auto-detect acts on. A keyboard merely being
        # connected (its named device opening) is also reported back via
        # immediately_detected, but only detect-all uses that, to show
        # which keyboards are connected; auto-detect ignores it, since
        # a plugged-in keyboard isn't necessarily what's about to be
        # played. MidiInput
        # exposes the same .stop()/.close() shape as the sd.InputStream
        # objects above (see its own docstring) so it slots into this
        # same `streams` list, and start_detect_all/start_auto_detect_
        # instrument tear everything down uniformly without needing to
        # know which is which.
        from .audio.midi_input import MidiInput, MidiUnavailableError
        release_seconds = release_blocks * config.buffer_size / config.sample_rate  # same ~0.5s hold as analog
        immediately_detected: list[str] = []
        for inst in midi_instruments:
            label = inst.input_label or f"midi:{inst.midi_device}"
            release_timer: list = [None]

            def _on_note_on(_note: int, _velocity: int, inst=inst, label=label) -> None:
                if on_channel_detected is not None:
                    # Off rtmidi's callback thread: auto-detect's handler
                    # tears every scan stream down (this MidiInput too) and
                    # opens a new engine — slow, and closing a MIDI port
                    # from that thread deadlocks (see MidiInput.close).
                    # Same "fires from a background thread" contract the
                    # analog classifiers' detections already have.
                    threading.Thread(
                        target=on_channel_detected, args=(inst.full_name, 1.0), daemon=True,
                    ).start()
                if on_channel_active is not None:
                    on_channel_active(label, True)
                    old = release_timer[0]
                    if old is not None:
                        old.cancel()
                    timer = threading.Timer(release_seconds, lambda: on_channel_active(label, False))
                    timer.daemon = True
                    timer.start()
                    release_timer[0] = timer

            try:
                midi_in = MidiInput(inst.midi_device, on_note_on=_on_note_on)
                midi_in.start()
            except MidiUnavailableError:
                skipped.append(inst.full_name)
                continue
            streams.append(midi_in)
            immediately_detected.append(inst.full_name)

        if not streams:
            raise BackendError("None of the configured instruments' inputs are available right now.")

        return streams, skipped, immediately_detected

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

            streams, skipped, immediate = self._open_channel_classifier_streams(
                config, on_channel_detected, on_channel_active, on_channel_stats,
            )
            self._active_detect_all = _ActiveDetectAll(streams=streams, stop_event=stop_event)

        status = "Listening on every instrument's own input — play each one to confirm it."
        if skipped:
            status += f" Not available right now: {', '.join(skipped)}."
        self._emit("detect_all_status", {"phase": "started", "status": status})
        # Outside the lock above — see _open_channel_classifier_streams'
        # own docstring for why calling this from inside it would risk
        # deadlock. Every connected MIDI instrument reports itself right
        # away here rather than waiting for a note (see that method's
        # MIDI section) — this on_channel_detected doesn't touch record_
        # lock at all, so the ordering matters less than it does for
        # auto-detect below, but keeping both callers' "call it once
        # streams/status are settled" shape identical is simpler than
        # explaining why one of them could safely skip it.
        for name in immediate:
            on_channel_detected(name, 1.0)

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
            # Every input stays audible while listening (alongside the
            # scan's own input-only streams — CoreAudio/CoreMIDI both allow
            # more than one client per device), until a detection narrows
            # it to just that instrument (on_channel_detected, below).
            self._monitor_all_inputs = True
            if self._active_monitor is not None and not self._active_monitor.all_inputs:
                self._close_active_monitor()
            self._start_monitoring_locked()
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
                        # Identified — mute every other input from here on.
                        self._monitor_all_inputs = False
                        self._close_active_monitor()
                    self._start_monitoring_locked()
                    # _open_channel_classifier_streams' own TunerTracker
                    # instances just got torn down along with every other
                    # scanning stream above — but identify_state stays
                    # "ready" (not "idle") for a while yet on every client
                    # (see recording_driver.py/ui/record.py), during which
                    # a performer is very much still expected to be
                    # tuning, not just glancing at a frozen last reading.
                    # _start_monitoring_locked() just reopened this exact
                    # instrument's own channel anyway (so it can be heard
                    # in headphones) — tap a fresh TunerTracker onto that
                    # same already-flowing audio rather than trying to
                    # keep the (now-closed) scan streams alive artificially.
                    monitor = self._active_monitor
                    if monitor is not None and inst is not None:
                        self._attach_tuner_sink(monitor.engine, inst.input_label, effective_tuning(inst))
                from .audio.synth import DEFAULT_SYNTH_VOICE
                self._emit("auto_detect_status", {
                    "phase": "detected", "instrument": name,
                    "full_name": inst.full_name if inst is not None else "",
                    "label": inst.label if inst is not None else "",
                    "is_midi": bool(inst is not None and inst.is_midi),
                    # The Record page's "Sound" picker (ui/record.py) seeds
                    # its dropdown from this — empty/unset always resolves
                    # to DEFAULT_SYNTH_VOICE the same way Synth's own
                    # constructor does (see audio/synth.py), so the picker
                    # never shows a blank selection for a MIDI instrument.
                    "synth_voice": (
                        (inst.synth_voice or DEFAULT_SYNTH_VOICE) if inst is not None and inst.is_midi else ""
                    ),
                })

            def on_channel_tuner(input_label: str, freq_hz: float) -> None:
                # Union of every instrument sharing this channel's own
                # tuning (falling back to its label's default — see
                # audio/pitch.py's effective_tuning) — same "a channel can
                # be shared by more than one instrument" reality on_
                # channel_detected's InstrumentClassifier already accounts
                # for, just without needing to know *which* of them is
                # actually playing (nothing's been identified yet).
                tuning: list[str] = []
                for inst in config.instruments:
                    if inst.input_label != input_label:
                        continue
                    for note in effective_tuning(inst):
                        if note not in tuning:
                            tuning.append(note)
                self._emit_tuner_reading(input_label, freq_hz, tuning)

            # `immediate` (every MIDI instrument whose device merely opened)
            # is deliberately ignored here, unlike detect-all: a keyboard
            # that's just plugged in isn't what's about to be played, and
            # reporting it as detected would beat any real instrument to
            # "first one wins". A MIDI instrument is only picked once a
            # note is actually played on it (_on_note_on still calls
            # on_channel_detected).
            monitor = self._active_monitor
            streams, skipped, _immediate = self._open_channel_classifier_streams(
                config, on_channel_detected, on_channel_tuner=on_channel_tuner,
                shared_engine=monitor.engine if monitor is not None and monitor.all_inputs else None,
            )
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

            backing_path = project.backing_tracks_dir / track.backing_track
            if track.inspiration_track_id and not backing_path.exists():
                from .inspiration import InspirationError, download_inspiration_track
                self._emit("video_check_status", {"status": f"Downloading '{track.name}'..."})
                try:
                    download_inspiration_track(track, backing_path, config)
                except InspirationError as e:
                    raise BackendError(str(e)) from e

            try:
                import sounddevice as sd  # noqa: F401 — just confirms it's importable before real work starts
            except Exception as e:
                raise BackendError(f"sounddevice unavailable: {e}") from e

            # Video Check is played the same way a real take is, so it
            # always runs Recording Monitoring (zero-latency hardware
            # direct monitor for the instrument) regardless of whatever
            # the Record page's monitoring_mode toggle is currently set
            # to — see get_monitoring_mode()/set_monitoring_mode() —
            # hence monitor_instrument=False unconditionally here.
            # instrument_volume=1.0 (not the instrument's own saved dial
            # level) — same unity-gain default the original inline
            # AudioEngine(...) construction here left implicit, since
            # Video Check has no Instrument Volume dial of its own to
            # reflect; the hardware-monitor mirror below matches with a
            # flat 100% for the same reason.
            engine, midi_input, input_info = self._build_engine_for_instrument(
                config, inst, monitor_instrument=False, instrument_volume=1.0,
            )
            self._apply_hardware_direct_monitor(input_info, True, 100)

            # Same non-destructive trim treatment as _load_track_locked —
            # see that method's comment for why.
            song_trim_start = round(track.trim_start_seconds * config.sample_rate)
            song_trim_end = round(track.trim_end_seconds * config.sample_rate)

            if backing_path.exists():
                engine.mixer.add_source(
                    "backing", backing_path, volume=self._backing_volume / 100.0,
                    trim_frames=song_trim_start, trim_end_frames=song_trim_end,
                )
                from .vault import load_backing_tuning
                engine.mixer.set_pitch(
                    "backing", load_backing_tuning(Path(config.session_vault_path), track.backing_track),
                )

            trim = int(config.latency_compensation_ms / 1000.0 * config.sample_rate)
            for other_inst, take_info in track.preferred_takes.items():
                if other_inst.lower() == inst.label.lower():
                    continue
                take_path = project.completed_takes_dir / take_info.filename
                if take_path.exists():
                    effective_vol = take_info.volume * (self._takes_volume / 100.0)
                    engine.mixer.add_source(
                        f"take:{other_inst}", take_path, volume=effective_vol,
                        trim_frames=trim + song_trim_start, trim_end_frames=song_trim_end,
                        compressor_settings=config.compressor_for_label(other_inst),
                    )

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
                midi_input=midi_input,
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
        if active.midi_input is not None:
            active.midi_input.close()
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
        synth_voice = ""
        if session.inst.is_midi:
            from .audio.synth import DEFAULT_SYNTH_VOICE
            synth_voice = session.inst.synth_voice or DEFAULT_SYNTH_VOICE
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
            synth_voice=synth_voice,
        ))
        # Written to disk on every event, not just once at clean session
        # end — session_log.json used to only exist at all once
        # _end_session ran, so a crash (power loss, the process being
        # killed) mid-session lost the *entire* event log, even though
        # the audio itself was very likely still fine on disk: with
        # nothing to replay, process_session had nothing to work from.
        # Infrequent enough (song-transition-level events, never the
        # audio callback) that writing here costs nothing that matters,
        # and _save_session_log already builds its dict fresh from
        # `session` every time, so it's already safe to call mid-session.
        self._save_session_log(session)

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
        # A session is for one known instrument — once it ends, ambient
        # monitoring resumes on just that one, not every input.
        self._monitor_all_inputs = False

        # All the setlist's network/heavy-disk work (song-set draws +
        # backing-track downloads) up front, before any capture hardware is
        # touched — so nothing mid-session hits the inspiration server or
        # writes a big file while audio/video is live. See _prefetch_
        # setlist_locked; best-effort, never blocks the session start.
        prefetched_picks = self._prefetch_setlist_locked(project, inst, config)

        try:
            import sounddevice as sd  # noqa: F401 — just confirms it's importable before real work starts
        except Exception as e:
            raise BackendError(f"sounddevice unavailable: {e}") from e

        # Only a real recording session captures raw MIDI alongside the
        # audio — see audio/midi_log.py and _end_session below. None for
        # an analog instrument; _build_engine_for_instrument ignores it
        # entirely in that case.
        midi_log = None
        if inst.is_midi:
            from .audio.midi_log import MidiEventLog
            midi_log = MidiEventLog()

        engine, midi_input, input_info = self._build_engine_for_instrument(
            config, inst, monitor_instrument=self._monitoring_mode == "production", midi_log=midi_log,
        )
        try:
            self._apply_hardware_direct_monitor(
                input_info, self._monitoring_mode == "recording", inst.instrument_volume,
            )
            engine.start()
        except Exception:
            if midi_input is not None:
                midi_input.close()
            raise

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
        # inst.label (e.g. "electric-bass-fretless"), not inst.full_name
        # (e.g. "Ibanez SDGR fretless") — the directory name is cosmetic
        # only (see correct_session_instrument's docstring: nothing reads
        # it back out, every real lookup goes through session_log.json's
        # own instrument/instrument_label fields), but the label is the
        # shorter, filesystem-friendlier, and more useful-at-a-glance of
        # the two for browsing the vault directly.
        session_dir = ensure_dir(vault_session_dir(config, project.name, f"{session_name}_{inst.label}"))
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
            engine=engine, project=project, inst=inst, midi_input=midi_input, session_dir=session_dir,
            session_start=timestamp_now(),
            musician=inst.musician or config.studio_musician,
            studio_name=config.studio_name, studio_location=config.studio_location,
            session_flac=session_flac, session_video=session_dir / "session_video.mp4",
            video_recorder=video_recorder, session_video_raw=session_video_raw,
            session_mix_flac=session_mix_flac, video_start_wall_time=video_start_wall_time,
            mix_start_frame=mix_start_frame, stream_feeder=stream_feeder,
            youtube_broadcast_id=youtube_broadcast_id,
            resolved_filter_picks=prefetched_picks,
            midi_log=midi_log,
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
            if session.midi_input is not None:
                session.midi_input.close()
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

            if session.midi_log:
                # The whole session's raw MIDI performance, same spirit
                # (and same session-frame timeline) as session.flac —
                # processing/splicer.py slices a take's own range out of
                # this alongside its audio, so it can be revoiced later
                # (re-rendered through a different synth voice) without
                # re-recording. Quick (in-memory to file), so it belongs
                # here with the rest of the fast finalization, not the
                # background processing thread.
                from .audio.midi_log import write_midi_file
                write_midi_file(
                    session.midi_log.events(), session.engine.sample_rate, session.session_dir / "session_midi.mid",
                )

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
        """Write session_log.json fresh from `session`'s current state —
        called after every event (see _log_session_event), not just once
        at clean session end, so a crash mid-session leaves behind
        whatever was true as of the last event rather than nothing at
        all. atomic_write_text so a crash *during* this specific write
        can't leave a truncated/corrupt JSON file behind either — the
        previous, still-valid version stays in place until the new one
        is fully written."""
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
            # song-set index -> the song actually drawn for it this
            # session (see _resolve_filter_slot) — the setlist itself
            # never records this, so post-processing needs it from here
            # to record a completed take into the shared vault-wide
            # inspiration-take index (vault.py) rather than the setlist.
            "filter_slot_draws": {
                str(index): _filter_draw_dict(entry)
                for index, entry in session.resolved_filter_picks.items()
            },
            # Every song drawn for each song-set index, in order — the
            # final one (same as filter_slot_draws) plus any it was
            # redrawn away from. Looked up by name (see
            # filter_draw_for_track) so a take recorded on a skipped draw
            # still files under its own song.
            "filter_slot_draw_history": {
                str(index): [
                    _filter_draw_dict(e)
                    for e in [*session.replaced_filter_picks.get(index, []), entry]
                ]
                for index, entry in session.resolved_filter_picks.items()
            },
            "events": [e.to_dict() for e in session.events],
        }
        atomic_write_text(log_path, json.dumps(data, indent=2))
        return log_path
