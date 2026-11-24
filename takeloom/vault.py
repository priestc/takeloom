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
import threading
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
# Per backing-track-file pitch correction in cents (the QX25's K2 knob,
# committed at each song's end — see backend.py's set_backing_pitch and
# _commit_instrument_pitch_locked), keyed by the backing track's
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


def push_youtube_to_vault(config: StudioConfig, log: LogFn | None = None) -> None:
    """Best-effort: give the remote vault the YouTube account connected here
    (video-ui › Settings › Streaming), so its session pages can publish an
    edited video straight from there. A disconnected account ("" refresh
    token) tells it to forget it. Never raises: an older vault (no
    /api/youtube) or an unreachable one just doesn't get it."""
    client = remote_vault(config)
    if client is None:
        return
    try:
        client.set_youtube({
            "client_id": config.youtube_oauth_client_id, "client_secret": config.youtube_oauth_client_secret,
            "refresh_token": config.youtube_oauth_refresh_token,
            "title_template": config.youtube_title_template,
            "description_template": config.youtube_description_template,
            "visibility": config.youtube_broadcast_visibility,
        })
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"Couldn't give the vault at {client.url} the YouTube account: {e}")


def vault_encodes_videos(config: StudioConfig, log: LogFn | None = None) -> bool:
    """Whether a just-ended session's videos should be left for the vault
    server to encode (StudioConfig.video_encoding; video/render_jobs.py):
    only asked for "vault", only with "remote" storage (with "both" the
    local copy would never get its videos), and only if the vault says
    it can right now. Anything else: this machine encodes."""
    if config.video_encoding != "vault":
        return False
    if config.session_vault_mode != "remote":
        if log:
            log("Encoding the session videos here: the vault only encodes with \"Remote only\" storage.")
        return False
    client = remote_vault(config, log)
    if client is None:
        return False
    try:
        info = client.ping()
    except Exception as e:  # noqa: BLE001 — any failure: encode here
        if log:
            log(f"Encoding the session videos here: couldn't reach the vault at {client.url} ({e}).")
        return False
    if not info.get("encode"):
        if log:
            log(f"Encoding the session videos here: the vault at {client.url} can't encode "
                "(it needs takeloom-vault 0.2 or later, and ffmpeg with libx264).")
        return False
    return True


def phones_upload_to_vault(config: StudioConfig, log: LogFn | None = None) -> "VaultClient | None":
    """The remote vault, if this session's phone recordings should go
    straight to it instead of through this machine: only when the vault
    makes the session videos (vault_encodes_videos) — its render jobs then
    take a phone's recording from where it uploaded it — and it takes
    camera uploads (takeloom-vault 0.11+). Else None: they come here."""
    if not vault_encodes_videos(config):
        return None
    client = remote_vault(config)
    try:
        info = client.ping() if client is not None else {}
    except Exception:  # noqa: BLE001
        return None
    if not info.get("camera_uploads"):
        if log:
            log(f"Phone recordings come through this machine: the vault at {client.url} can't take them "
                "directly (it needs takeloom-vault 0.11 or later).")
        return None
    return client


# --- one pairing for the whole system ---
#
# Everything paired with this machine's server (Takeloom Camera phones,
# remote-control laptops: config.remote_authorized_clients) gets a vault
# token of its own, which this machine vouches for (the vault's
# auth.py delegate) — so nothing has to pair with the vault separately.
# Each is good for as long as this machine is paired with the vault, and
# is revoked there when its client is no longer authorized here
# (prune_vault_delegates).

_delegate_lock = threading.Lock()


def _delegate_key(token: str) -> str:
    """What the vault knows a client's token by: never the token itself."""
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()[:24]


def _unauthorized(error: Exception) -> bool:
    import urllib.error
    cause = error.__cause__
    return isinstance(cause, urllib.error.HTTPError) and cause.code == 401


def vault_access_for(
    config: StudioConfig, client_token: str, save: Callable[[StudioConfig], None], log: LogFn | None = None,
) -> dict | None:
    """{"url", "token"} for the paired client with `client_token`: its own
    token for the remote vault, made on first use (and again if the vault
    no longer takes it) and kept in its AuthorizedClient. None if there's
    no paired remote vault, no such client, or the vault can't be reached
    to make one."""
    import hmac
    from .vault_client import VaultClient, VaultError
    client = remote_vault(config)
    if client is None or not client_token:
        return None
    with _delegate_lock:
        entry = next((c for c in config.remote_authorized_clients
                      if hmac.compare_digest(c.token, client_token)), None)
        if entry is None:
            return None
        if entry.vault_token and entry.vault_url == client.url:
            try:
                VaultClient(client.url, entry.vault_token).ping()
                return {"url": client.url, "token": entry.vault_token}
            except VaultError as e:
                if not _unauthorized(e):
                    # Unreachable right now: it's still the right token.
                    return {"url": client.url, "token": entry.vault_token}
        try:
            made = client.delegate(entry.label, _delegate_key(entry.token))
        except VaultError as e:
            if log:
                log(f"Couldn't get {entry.label} a token for the vault: {e}")
            return None
        entry.vault_url, entry.vault_token = client.url, made["token"]
        save(config)
        if log:
            log(f"{entry.label} can reach the vault at {client.url} now, through this server's pairing.")
        return {"url": client.url, "token": entry.vault_token}


def prune_vault_delegates(config: StudioConfig, log: LogFn | None = None) -> int:
    """Revoke the vault tokens of clients no longer authorized here.
    Returns how many."""
    from .vault_client import VaultError
    client = remote_vault(config)
    if client is None:
        return 0
    try:
        delegates = client.list_delegates()
    except VaultError:
        return 0  # unreachable, or a vault from before delegated tokens
    keys = {_delegate_key(c.token) for c in config.remote_authorized_clients}
    revoked = 0
    for d in delegates:
        if d.get("key") not in keys and client.revoke_delegate(str(d.get("id"))):
            revoked += 1
            if log:
                log(f"Revoked {d.get('label')}'s vault token: it's no longer authorized on this server.")
    return revoked


def _format_bytes(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def _format_seconds(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def upload_summary(name: str, url: str, stats: dict) -> str:
    """One line for the server log: what a session upload sent, how long it
    took and how fast that was."""
    seconds = max(stats["seconds"], 0.001)
    line = (f"Uploaded session '{name}' to the vault at {url}: {stats['files']} file(s), "
            f"{_format_bytes(stats['bytes'])} in {_format_seconds(stats['seconds'])} "
            f"({_format_bytes(stats['bytes'] / seconds)}/s)")
    if stats["skipped"]:
        line += f"; {stats['skipped']} already there from an earlier attempt"
    return line + "."


# --- sessions deleted elsewhere ---

TAKE_FILE_EXTENSIONS = (".mp3", ".flac", ".mp4", ".mid", ".video.json")
# The vault's deletions this machine has already caught up on.
_APPLIED_DELETIONS = ".deleted_sessions_applied.json"


def forget_takes(config: StudioConfig, filenames: set[str]) -> None:
    """Drop every take in `filenames` from every project setlist and the
    shared inspiration-take index (each such track is left with no take
    for that label — an older take of the same song isn't promoted in its
    place), and delete its files from the local vault."""
    if not filenames:
        return
    root = vault_root(config)

    def drop(entry: TrackEntry) -> bool:
        stale = [label for label, take in entry.preferred_takes.items() if take.filename in filenames]
        for label in stale:
            del entry.preferred_takes[label]
        return bool(stale)

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
    for filename in filenames:
        for ext in TAKE_FILE_EXTENSIONS:
            (root / "completed_takes" / f"{Path(filename).stem}{ext}").unlink(missing_ok=True)


def _vault_deletions(client: "VaultClient") -> list[dict]:
    return [d for d in client.deleted_sessions() or []
            if isinstance(d.get("session"), str) and d["session"] and "/" not in d["session"]]


def apply_vault_deletions(config: StudioConfig, client: "VaultClient", log: LogFn | None = None,
                          deletions: list[dict] | None = None) -> None:
    """Catch up on sessions deleted from the vault's web UI: forget the
    takes each filed (forget_takes) and delete any local copy of the
    session, once each. Callers make sure no session is open (its
    in-memory setlist could write the takes back)."""
    log = log or (lambda msg: None)
    root = vault_root(config)
    if deletions is None:
        deletions = _vault_deletions(client)
    if not deletions:
        return
    applied_path = root / _APPLIED_DELETIONS
    try:
        applied = set(json.loads(applied_path.read_text()))
    except (OSError, ValueError, TypeError):
        applied = set()
    todo = [d for d in deletions if d["session"] not in applied]
    if not todo:
        return
    for d in todo:
        forget_takes(config, {t for t in d.get("takes") or [] if isinstance(t, str) and t})
        shutil.rmtree(root / "sessions" / d["session"], ignore_errors=True)
        applied.add(d["session"])
        log(f"Session '{d['session']}' was deleted on the vault at {client.url}; removed it here too.")
    ensure_dir(root)
    atomic_write_text(applied_path, json.dumps(sorted(applied), indent=2))


def sync_and_maybe_prune(
    config: StudioConfig, session_dir: Path, log: LogFn | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """Best-effort: upload `session_dir` (already inside the vault) to the
    remote vault server if session_vault_mode is "remote" or "both", and
    — only for "remote" mode, and only once every file's upload was
    checksum-verified by the server — delete the local copy, so the
    remote ends up as the sole copy. Does nothing for "local" mode, or if
    no vault server is configured/paired (there's nowhere to push to).
    `log`, if given, reports progress and, once it's done, a summary
    (upload_summary). A failed upload keeps the session here, and
    Backend's upload retry tries it again later (`should_stop` lets that
    give way to a session opening). Never raises, matching this
    codebase's other best-effort sync/hardware paths."""
    log = log or (lambda msg: None)
    client = remote_vault(config, log)
    if client is None:
        return False
    name = session_dir.name
    log(f"Uploading session '{name}' to the vault at {client.url}...")
    try:
        stats = client.upload_session(session_dir, name, log=log, should_stop=should_stop)
    except Exception as e:  # noqa: BLE001 — e.g. the folder vanishing mid-upload
        log(f"Vault upload of '{name}' failed: {type(e).__name__}: {e}")
        stats = None
    if stats is None:
        if should_stop is not None and should_stop():
            log(f"Kept '{name}' locally; its upload carries on once the session is over.")
        else:
            log(f"Vault upload failed for '{name}' — kept locally; it'll be retried automatically.")
        return False
    log(upload_summary(name, client.url, stats))
    if config.session_vault_mode == "remote":
        shutil.rmtree(session_dir, ignore_errors=True)
        log(f"Removed local copy of '{name}' (remote-only vault mode).")
    return True


def sessions_awaiting_upload(config: StudioConfig) -> list[Path]:
    """Processed sessions still on this machine that the remote vault
    doesn't have yet: under "Remote only", every one (a successful upload
    removes it here, so it being here means an earlier attempt failed —
    re-running it re-sends only what's missing); under "Both", the ones
    the vault doesn't list. Unprocessed ones (no "takes" in their log) are
    left alone: they're processed, and uploaded, first. Oldest first."""
    if config.session_vault_mode not in ("remote", "both"):
        return []
    client = remote_vault(config)
    sessions = vault_root(config) / "sessions"
    if client is None or not sessions.is_dir():
        return []
    candidates = []
    for d in sorted(sessions.iterdir()):
        try:
            data = json.loads((d / "session_log.json").read_text())
        except (OSError, ValueError):
            continue
        if "takes" in data:
            candidates.append(d)
    if candidates:
        # Deleted from the vault's web UI: never sent back up.
        deleted = {d["session"] for d in _vault_deletions(client)}
        candidates = [d for d in candidates if d.name not in deleted]
    if not candidates or config.session_vault_mode == "remote":
        return candidates
    remote = set(client.list_session_logs())
    return [d for d in candidates if d.name not in remote]


# --- everything else in the vault ---
#
# Sessions go up one at a time, as each is processed (above). Everything
# else the vault holds is mirrored by sync_vault_files: completed takes,
# backing tracks, the small metadata files (saved mixes, backing tuning, the
# shared inspiration-take index), exported mixes, and every project's
# setlist. Under "Remote only" storage the bulky media (take audio, backing
# tracks, exported mixes) are then removed here once the vault verifiably
# has them; they come back on demand (ensure_setlist_files_local, Backend.
# ensure_take_local). What stays here is tiny and read constantly: the
# metadata files themselves, take videos' .video.json refs and takes' .mid
# files (how has_video/has_midi are known), and the setlists.

# Media that's pruned locally under "Remote only" storage.
_PRUNABLE = {
    "completed_takes": {".mp3", ".flac", ".mp4", ".m4a", ".wav"},
    "backing_tracks": None,  # every file
    "final_mixes": None,
}
# Synced, never pruned. A take's audio is never overwritten on the vault
# (its name is unique: see utils.next_take_number); a mismatch is reported.
# Not mixes/: song mixes are the vault's own now (its Completed Takes page
# saves them; nothing here writes them any more), so an old copy here must
# never overwrite one saved there.
_SYNCED_DIRS = ("completed_takes", "backing_tracks", "final_mixes")
_ROOT_FILES = (_INDEX_FILENAME, "backing_tuning.json")


# (vault URL, vault path) -> (size, mtime_ns) of the copy this process last
# uploaded, for the files sync_vault_files can't list on the vault (the
# root's own files) or that change in place (setlists): unchanged since,
# nothing to send.
_uploaded: dict[tuple[str, str], tuple[int, int]] = {}


def _prunable(folder: str, path: Path) -> bool:
    if folder not in _PRUNABLE:
        return False
    exts = _PRUNABLE[folder]
    return exts is None or path.suffix.lower() in exts


def sync_vault_files(config: StudioConfig, log: LogFn | None = None, prune: bool = True) -> dict | None:
    """Best-effort: upload everything in the local vault (and every project
    setlist) that the remote vault doesn't have yet, or has an older copy
    of, then — under "Remote only" storage and with `prune` — delete the
    local copies of the media it now verifiably has (see above). Callers
    pass prune=False while a session is open, so nothing it may load gets
    removed under it. Returns counts, or None if there's no remote vault
    (mode "local", or not paired). Never raises."""
    from .vault_client import VaultError
    log = log or (lambda msg: None)
    client = remote_vault(config)
    if client is None:
        return None
    root = vault_root(config)
    counts = {"uploaded": 0, "pruned": 0, "failed": 0, "conflicts": 0}

    # Sessions deleted from the vault's web UI: drop their takes here first
    # (not while a session is open — prune=False — whose setlist could
    # write them back), and never upload them again either way.
    deletions = _vault_deletions(client)
    if prune and deletions:
        try:
            apply_vault_deletions(config, client, log, deletions)
        except Exception as e:  # noqa: BLE001 — best-effort, like the rest of sync
            log(f"Vault sync: couldn't catch up on sessions deleted on the vault: {type(e).__name__}: {e}")
    deleted_takes = {
        f"{Path(t).stem}{ext}" for d in deletions for t in d.get("takes") or [] if isinstance(t, str)
        for ext in TAKE_FILE_EXTENSIONS
    } | {t for d in deletions for t in d.get("takes") or [] if isinstance(t, str)}

    def upload(path: Path, relative: str) -> bool:
        st = path.stat()
        try:
            client.upload_file(path, relative)
            counts["uploaded"] += 1
            _uploaded[(client.url, relative)] = (st.st_size, st.st_mtime_ns)
            return True
        except VaultError as e:
            counts["failed"] += 1
            log(f"Vault sync: couldn't upload {relative}: {e}")
            return False

    def upload_if_changed(path: Path, relative: str) -> None:
        st = path.stat()
        if _uploaded.get((client.url, relative)) != (st.st_size, st.st_mtime_ns):
            upload(path, relative)

    for name in _ROOT_FILES:
        if (root / name).is_file():
            upload_if_changed(root / name, name)
    projects = Path(config.projects_dir)
    if projects.is_dir():
        for path in sorted(projects.glob("*.json")):
            if not path.name.startswith("."):
                upload_if_changed(path, f"projects/{path.name}")

    prune = prune and config.session_vault_mode == "remote"
    for folder in _SYNCED_DIRS:
        local_dir = root / folder
        if not local_dir.is_dir():
            continue
        local = sorted(f for f in local_dir.iterdir() if f.is_file() and not f.name.startswith("."))
        if not local:
            continue
        listing = client.list_files(folder)
        if listing is None:
            counts["failed"] += 1
            log(f"Vault sync: couldn't list {folder}/ on {client.url}; skipped it.")
            continue
        remote = {f["name"]: f for f in listing}
        for path in local:
            if folder == "completed_takes" and path.name in deleted_takes:
                continue
            relative = f"{folder}/{path.name}"
            st = path.stat()
            there = remote.get(path.name)
            if there is None:
                have = upload(path, relative)
            elif there["size"] == st.st_size and not (
                # Same size isn't proof for a small file that's edited in
                # place (a saved mix): newer here means upload it again.
                not _prunable(folder, path) and st.st_mtime > float(there.get("mtime") or 0)
            ):
                have = True
            elif _prunable(folder, path):
                counts["conflicts"] += 1
                log(f"Vault sync: {relative} differs from the vault's copy ({st.st_size:,} bytes here, "
                    f"{there['size']:,} there); left both alone.")
                have = False
            else:
                have = upload(path, relative)
            if have and prune and _prunable(folder, path):
                path.unlink(missing_ok=True)
                counts["pruned"] += 1
    if counts["uploaded"] or counts["pruned"] or counts["failed"] or counts["conflicts"]:
        log(f"Vault sync with {client.url}: {counts['uploaded']} uploaded, {counts['pruned']} removed locally"
            + (f", {counts['failed']} failed" if counts["failed"] else "")
            + (f", {counts['conflicts']} conflicting" if counts["conflicts"] else "") + ".")
    return counts


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
