"""Studio Session Vault: centralized, shared storage for everything a
project used to keep to itself — recorded sessions (session.flac,
session_video.mp4, session_log.json, ...), backing tracks, and completed
takes. A project (see project.py) is just a setlist file now; every
project reads and writes into these same three vault subfolders:

    <vault>/
    ├── sessions/<session_name>_<project_name>/...
    ├── backing_tracks/<filename>
    ├── completed_takes/<filename>
    └── mixes/<song>.json

mixes/ holds the Completed Takes tab's saved mixer settings, one file per
song — see load_song_mix/save_song_mix below.

Sharing backing_tracks/completed_takes across every project is what lets
two different projects reference the same inspiration-server song without
downloading or recording it twice — see inspiration_takes.json below,
the index that makes an already-recorded take on a given song findable
from any project, not just the one that originally recorded it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .config import StudioConfig
from .project import Project, TakeInfo, TrackEntry
from .utils import atomic_write_text, ensure_dir

if TYPE_CHECKING:
    from .vault_client import VaultClient

LogFn = Callable[[str], None]


def vault_root(config: StudioConfig) -> Path:
    return Path(config.session_vault_path)


def vault_backing_tracks_dir(config: StudioConfig) -> Path:
    return vault_root(config) / "backing_tracks"


def vault_completed_takes_dir(config: StudioConfig) -> Path:
    return vault_root(config) / "completed_takes"


def vault_session_dir(config: StudioConfig, project_name: str, session_name: str) -> Path:
    """All sessions sit flat in one root sessions/ folder, not nested by
    project — the project name is appended onto session_name instead
    (e.g. "2026-08-04_15-26-33_bass_Album1"), since project isn't part
    of a session's own identity here (it's whichever setlist happened to
    be open at record time), and there's no other reason for a folder
    listing to be split up by it."""
    return vault_root(config) / "sessions" / f"{session_name}_{project_name}"


# --- shared inspiration-take index ---
#
# A project's own setlist.json only knows about takes recorded while that
# project's own TrackEntry was loaded. Since backing_tracks/completed_takes
# are shared vault-wide now, this index is what makes a take findable by
# *any* project referencing the same inspiration_track_id — keyed by
# str(track_id) rather than by which project or session happened to record
# it, each entry shaped exactly like an ordinary TrackEntry (name,
# backing_track, duration_seconds, preferred_takes) so it can be treated
# as one wherever a track is needed.

_INDEX_FILENAME = "inspiration_takes.json"


def _index_path(root: Path) -> Path:
    return root / _INDEX_FILENAME


def load_inspiration_index(root: Path) -> dict[str, TrackEntry]:
    """`root` is a vault root (see vault_root()) — taken directly rather
    than a StudioConfig so this is callable from splicer.py, which has a
    bare vault path but no full config object."""
    path = _index_path(root)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {key: TrackEntry.from_dict(entry) for key, entry in data.items()}


def save_inspiration_index(root: Path, index: dict[str, TrackEntry]) -> None:
    ensure_dir(root)
    atomic_write_text(_index_path(root), json.dumps({k: v.to_dict() for k, v in index.items()}, indent=2))


def get_inspiration_entry(root: Path, track_id: int) -> TrackEntry | None:
    """The shared entry for `track_id` (name, backing_track, and every
    instrument's take recorded against it by any project so far), or None
    if nothing's ever been recorded for it yet."""
    if not track_id:
        return None
    return load_inspiration_index(root).get(str(track_id))


def record_inspiration_take(
    root: Path, track_id: int, name: str, backing_track: str, duration_seconds: float,
    instrument: str, take: TakeInfo,
) -> None:
    """Record a completed take against `track_id` in the shared index —
    called from splicer.py alongside (not instead of) the normal
    TrackEntry.set_preferred_take() on whichever project's setlist
    actually holds this track, so every *other* project referencing the
    same song can find this take too (see _resolve_filter_slot and
    _load_track_locked's "other instruments' takes" lookup in backend.py)."""
    if not track_id:
        return
    index = load_inspiration_index(root)
    key = str(track_id)
    entry = index.get(key)
    if entry is None:
        entry = TrackEntry(
            name=name, backing_track=backing_track, duration_seconds=duration_seconds, inspiration_track_id=track_id,
        )
        index[key] = entry
    entry.set_preferred_take(instrument, take)
    save_inspiration_index(root, index)


# --- saved song mixes ---
#
# The Completed Takes tab's mixer (ui/song_mixer.py) saves each song's
# per-instrument volume/mute settings here, so reopening that song in the
# mixer restores them. Keyed by the song's track_name (the same name the
# Completed Takes list groups by) and, within it, by instrument label —
# not take filename — so a mix still applies after a take is re-recorded,
# and a label that's new since the mix was saved just starts at 100%.
# Stored like inspiration_takes.json: in the local vault on the studio
# machine only, written by the server (see architecture notes on Remote
# being read-only for vault data).


def _mix_path(root: Path, track_name: str) -> Path:
    from .utils import sanitize_filename
    return root / "mixes" / f"{sanitize_filename(track_name) or 'untitled'}.json"


def load_song_mix(root: Path, track_name: str) -> dict | None:
    """{"track_name": str, "volumes": {label: gain}, "muted": [label, ...],
    "saved_at": float} for `track_name`, or None if it's never been saved
    (or the file is unreadable — treated the same as no mix)."""
    path = _mix_path(root, track_name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return {
        "track_name": data.get("track_name", track_name),
        "volumes": {str(k): float(v) for k, v in (data.get("volumes") or {}).items()},
        "muted": [str(m) for m in data.get("muted") or []],
        "saved_at": data.get("saved_at"),
    }


def save_song_mix(root: Path, track_name: str, volumes: dict[str, float], muted: list[str]) -> dict:
    import time
    mix = {
        "track_name": track_name,
        "volumes": {str(k): float(v) for k, v in volumes.items()},
        "muted": sorted(str(m) for m in muted),
        "saved_at": time.time(),
    }
    path = _mix_path(root, track_name)
    ensure_dir(path.parent)
    atomic_write_text(path, json.dumps(mix, indent=2))
    return mix


# --- backing track tuning ---
#
# Per backing-track-file pitch correction in cents (the QX25's K2 knob —
# see backend.py's set_backing_pitch), keyed by the backing track's
# filename so it follows that exact recording wherever it's used: every
# session that loads it, and Completed Takes playback. One small JSON map
# in the local vault, written by the server only — same as the song mixes
# above.


def _backing_tuning_path(root: Path) -> Path:
    return root / "backing_tuning.json"


def _load_backing_tuning_map(root: Path) -> dict[str, float]:
    path = _backing_tuning_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}


def load_backing_tuning(root: Path, backing_track: str) -> float:
    """Saved correction for `backing_track` (a filename), 0.0 if none."""
    return _load_backing_tuning_map(root).get(backing_track, 0.0) if backing_track else 0.0


def save_backing_tuning(root: Path, backing_track: str, cents: float) -> None:
    tuning = _load_backing_tuning_map(root)
    if cents:
        tuning[backing_track] = round(float(cents), 2)
    else:
        tuning.pop(backing_track, None)
    ensure_dir(root)
    atomic_write_text(_backing_tuning_path(root), json.dumps(tuning, indent=2, sort_keys=True))


def remote_vault(config: StudioConfig, log: LogFn | None = None) -> "VaultClient | None":
    """A client for the remote vault server, or None if session_vault_mode
    is "local" or no vault server is set/paired (logging which, if `log`
    is given). Every remote-vault access goes through this."""
    if config.session_vault_mode not in ("remote", "both"):
        return None
    if not config.vault_server:
        if log:
            log("Remote vault skipped: no vault server configured.")
        return None
    if not config.vault_server_token:
        if log:
            log(f"Remote vault skipped: not paired with {config.vault_server} yet (Studio Setup → Pair).")
        return None
    from .vault_client import VaultClient
    return VaultClient(config.vault_server, config.vault_server_token)


def sync_and_maybe_prune(config: StudioConfig, session_dir: Path, log: LogFn | None = None) -> None:
    """Best-effort: upload `session_dir` (already inside the vault) to the
    remote vault server if session_vault_mode is "remote" or "both", and
    — only for "remote" mode, and only once every file's upload was
    checksum-verified by the server — delete the local copy, so the
    remote ends up as the sole copy. Does nothing for "local" mode, or if
    no vault server is configured/paired (there's nowhere to push to).
    `log`, if given, reports progress — an emitted event from a live
    session's background processing thread, or plain stdout from the
    migration CLI command; either way this function never raises,
    matching this codebase's other best-effort sync/hardware paths."""
    log = log or (lambda msg: None)
    client = remote_vault(config, log)
    if client is None:
        return
    name = session_dir.name
    log(f"Uploading session '{name}' to the vault at {client.url}...")
    if not client.upload_session(session_dir, name, log=log):
        log(f"Vault upload failed for '{name}' — kept locally.")
        return
    log(f"Uploaded '{name}' to the vault.")
    if config.session_vault_mode == "remote":
        shutil.rmtree(session_dir, ignore_errors=True)
        log(f"Removed local copy of '{name}' (remote-only vault mode).")


def _files_used_by_track(project: Project, track: TrackEntry, config: StudioConfig) -> set[tuple[Path, str]]:
    """(local_path, vault-relative-path-string) pairs a track needs on
    local disk to be playable/recordable — its own backing track and
    every instrument's take on it, plus (for an inspiration-sourced
    track) whatever the shared index knows about it, since that's where
    an *other* project's take on the same song would be recorded."""
    needed: set[tuple[Path, str]] = set()

    def add_backing(name: str) -> None:
        if name:
            needed.add((project.backing_tracks_dir / name, f"backing_tracks/{name}"))

    def add_take(take: TakeInfo) -> None:
        needed.add((project.completed_takes_dir / take.filename, f"completed_takes/{take.filename}"))
        # No take video here: recording never needs it, and it's cut from
        # the session video on demand anyway (see take_video.py).
        if take.has_midi:
            midi_name = Path(take.filename).stem + ".mid"
            needed.add((project.completed_takes_dir / midi_name, f"completed_takes/{midi_name}"))

    add_backing(track.backing_track)
    for take in track.preferred_takes.values():
        add_take(take)

    if not track.is_inspiration_filter and track.inspiration_track_id:
        shared = get_inspiration_entry(vault_root(config), track.inspiration_track_id)
        if shared is not None:
            add_backing(shared.backing_track)
            for take in shared.preferred_takes.values():
                add_take(take)

    return needed


def ensure_setlist_files_local(config: StudioConfig, project: Project, log: LogFn | None = None) -> None:
    """Before a session starts, make sure every file its setlist could
    need — every track's backing track, and every instrument's take
    already recorded on it (including, for inspiration-sourced tracks,
    whatever the shared index knows from *other* projects) — actually
    exists on local disk, downloading from the remote vault server for
    whatever's missing. Only matters in "remote" vault mode, where a file
    can legitimately have been pruned locally after its own session
    synced; in "local"/"both" modes everything's already local, so this
    is a fast no-op scan. Best-effort: a download failure is logged, not
    raised — the session still starts, just possibly missing that one
    backing track or layered take."""
    log = log or (lambda msg: None)
    client = remote_vault(config)
    if client is None:
        return
    needed = set()
    for track in project.setlist.tracks:
        needed |= _files_used_by_track(project, track, config)
    for local_path, relative in sorted(needed, key=lambda pair: pair[1]):
        if local_path.exists():
            continue
        log(f"Downloading '{relative}' from the vault at {client.url}...")
        if not client.download_file(relative, local_path):
            log(f"Could not download '{relative}' from {client.url} — it may not exist there either.")
