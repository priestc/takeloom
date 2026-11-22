"""`takeloom vault compress-takes`: turn the completed takes still stored
as lossless .flac (from before takes were LAME V2 .mp3: audio/formats.py)
into .mp3s — but only where the take's lossless original verifiably
survives in its session, so an exported mix (take_original.py) still gets
it. A .flac take whose session has nothing lossless left is the only
lossless copy there is, and stays.

For each .flac take that a session log's "takes" snapshot lists with a
video ref:

1. find exactly where it starts in its session, to the sample: its video
   ref says roughly (to under a ms: the muxer rounded the camera's audio
   delay), so the take's loudest stretch is looked for, sample for sample,
   in the session video's Instrument Only track around there;
2. record that as the take's TakeInfo.source in the session log, and have
   the vault cut the take back out of the session with it (GET
   /api/takes/<f>/original?legacy=0) — kept only if that's bit-identical
   to the .flac (otherwise the log is put back as it was, and the .flac
   stays);
3. encode the .flac to <same name>.mp3 and store it in the vault;
4. rename the take everywhere it's referenced (every project's setlist,
   the inspiration-take index, every session log's snapshot), with its
   source; only then
5. delete the .flac, here and on the vault.

Safe to stop and run again: a take whose .mp3 is already what its
snapshot names just has its leftover .flac deleted. With dry_run, only
step 1 runs (reading, never writing), reporting what would be converted.
Needs takeloom-vault 0.10.1+ for step 2 when the vault is remote.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import numpy as np
import soundfile as sf

from .config import StudioConfig
from .project import Project
from .take_original import exact_video_cut_args, instrument_track_offset
from .take_video import camera_refs
from .utils import atomic_write_text, ensure_dir
from .vault import load_inspiration_index, remote_vault, save_inspiration_index, vault_root

LogFn = Callable[[str], None]

_WINDOW = 8192   # samples matched to find the take's exact start
_SEARCH = 1500   # +/- samples searched around the video ref's estimate
MIN_VAULT_VERSION = (0, 10, 1)


class MigrationError(Exception):
    pass


def _version_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in version.split(".")[:3])
    except ValueError:
        return ()


class _Sessions:
    """Session logs and files, from the remote vault or this machine's own
    vault folder (whichever has the session)."""

    def __init__(self, config: StudioConfig) -> None:
        self.root = vault_root(config)
        self.client = remote_vault(config)
        self.logs: dict[str, dict] = {}
        self.where: dict[str, str] = {}
        if self.client is not None:
            for name, data in self.client.list_session_logs().items():
                self.logs[name], self.where[name] = data, "remote"
        for path in sorted((self.root / "sessions").glob("*/session_log.json")):
            try:
                self.logs[path.parent.name] = json.loads(path.read_text())
                self.where[path.parent.name] = "local"
            except (OSError, ValueError):
                pass
        self._files: dict[str, set[str]] = {}

    def files(self, name: str) -> set[str]:
        if name not in self._files:
            if self.where.get(name) == "local":
                self._files[name] = {p.name for p in (self.root / "sessions" / name).iterdir() if p.is_file()}
            else:
                self._files[name] = {f["name"] for f in self.client.list_files(f"sessions/{name}") or []}
        return self._files[name]

    def media(self, name: str, filename: str) -> tuple[str, list[str]]:
        """(path or URL, ffmpeg/ffprobe input args) for a session's file."""
        if self.where.get(name) == "local":
            return str(self.root / "sessions" / name / filename), []
        url = f"{self.client.url}/api/files/{quote(f'sessions/{name}/{filename}')}"
        return url, ["-headers", f"Authorization: Bearer {self.client.token}\r\n"]

    def write_log(self, name: str) -> None:
        if self.where.get(name) == "local":
            atomic_write_text(self.root / "sessions" / name / "session_log.json", json.dumps(self.logs[name], indent=2))
        elif not self.client.write_session_log(name, self.logs[name]):
            raise MigrationError(f"Couldn't write {name}'s session_log.json on the vault at {self.client.url}.")

    def entries(self, filename: str) -> list[tuple[str, dict]]:
        """Every (session, snapshot entry) naming take `filename`."""
        return [
            (name, entry) for name, data in self.logs.items()
            for entries in (data.get("takes") or {}).values() for entry in entries
            if entry.get("filename") == filename
        ]


def _cut(media: str, input_args: list[str], offset: int, start: int, end: int, sr: int, out: Path) -> np.ndarray | None:
    from .ffmpeg_bin import FFMPEG
    args = exact_video_cut_args(media, offset, max(0, start), end, sr, input_args)
    if subprocess.run([FFMPEG, *args, str(out)], capture_output=True).returncode != 0:
        return None
    data, _ = sf.read(out, dtype="int16", always_2d=True)
    return data[:, 0]


def _find_start(take: np.ndarray, sr: int, info: dict, session: str, sessions: _Sessions, work: Path) -> int | None:
    """The take's exact start frame in its session (step 1), or None."""
    video_session = str(info.get("session") or session)
    refs = [r for r in camera_refs(info) if str(r.get("file")) in sessions.files(video_session)]
    if not refs or len(take) < _WINDOW:
        return None
    ref = refs[0]
    media, input_args = sessions.media(video_session, str(ref["file"]))
    offset = instrument_track_offset(media, sr, input_args)
    estimate = round(float(ref["instrument_start_s"]) * sr) - offset
    # The loudest stretch: silence would match anywhere.
    energy = np.convolve(np.abs(take.astype(np.int32)), np.ones(_WINDOW, dtype=np.int32), "valid")
    i = int(np.argmax(energy))
    window = take[i:i + _WINDOW]
    lo = max(0, estimate + i - _SEARCH)
    around = _cut(media, input_args, offset, lo, estimate + i + _WINDOW + _SEARCH, sr, work / "around.flac")
    if around is None:
        return None
    hits = [
        lag for lag in np.flatnonzero(around[:len(around) - _WINDOW + 1] == window[0])
        if np.array_equal(around[lag:lag + _WINDOW], window)
    ]
    if len(hits) != 1:
        return None
    return lo + int(hits[0]) - i


def _session_cut(sessions: _Sessions, session: str, entry: dict, filename: str, start: int, n: int, sr: int,
                 out: Path) -> np.ndarray | None:
    """The take cut back out of its session exactly as an exported mix will
    get it: by the vault itself for a session there, else here."""
    if sessions.where.get(session) == "remote":
        if not sessions.client.download_take_original(filename, out, legacy=False):
            return None
        return sf.read(out, dtype="int16", always_2d=True)[0][:, 0]
    video_session = str(entry["video"].get("session") or session)
    ref = next(r for r in camera_refs(entry["video"]) if str(r.get("file")) in sessions.files(video_session))
    media, args = sessions.media(video_session, str(ref["file"]))
    return _cut(media, args, instrument_track_offset(media, sr, args), start, start + n, sr, out)


def _rename_references(config: StudioConfig, sessions: _Sessions, old: str, new: str, source: dict) -> None:
    """Step 4: `old` becomes `new` (with `source`) everywhere it's named."""
    root = vault_root(config)
    for path in Project.list_projects(Path(config.projects_dir)):
        try:
            project = Project.open(path, root)
        except Exception:
            continue
        changed = False
        for track in project.setlist.tracks:
            for take in track.preferred_takes.values():
                if take.filename == old:
                    take.filename, take.source, changed = new, source, True
        if changed:
            project.save_setlist()
    index = load_inspiration_index(root)
    changed = False
    for entry in index.values():
        for take in entry.preferred_takes.values():
            if take.filename == old:
                take.filename, take.source, changed = new, source, True
    if changed:
        save_inspiration_index(root, index)
    touched = set()
    for name, entry in sessions.entries(old):
        entry["filename"], entry["source"] = new, source
        touched.add(name)
    for name in touched:
        sessions.write_log(name)


def compress_takes(config: StudioConfig, log: LogFn, dry_run: bool = False) -> dict:
    from .audio.formats import encode_mp3
    sessions = _Sessions(config)
    client = sessions.client
    root = vault_root(config)
    completed = root / "completed_takes"
    if client is not None and not dry_run:
        version = str(client.ping().get("version") or "")
        if _version_tuple(version) < MIN_VAULT_VERSION:
            raise MigrationError(
                f"The vault at {client.url} runs takeloom-vault {version or 'older than 0.10.1'}; this needs "
                f"{'.'.join(map(str, MIN_VAULT_VERSION))} or later (see vault_server/README.md to upgrade it)."
            )

    flacs: dict[str, str] = {}  # take filename -> "remote" / "local"
    remote_names: set[str] = set()
    if client is not None:
        listing = client.list_files("completed_takes")
        if listing is None:
            raise MigrationError(f"Couldn't list completed_takes/ on the vault at {client.url}.")
        remote_names = {f["name"] for f in listing}
        flacs.update({n: "remote" for n in remote_names if n.endswith(".flac")})
    if completed.is_dir():
        flacs.update({p.name: "local" for p in completed.glob("*.flac") if not p.name.startswith(".")
                      and p.name not in flacs})

    counts = {"converted": 0, "would_convert": 0, "kept": 0, "leftovers_removed": 0, "failed": 0}
    work = Path(tempfile.mkdtemp(prefix="takeloom_compress_"))

    def delete_flac(name: str) -> None:
        if client is not None and name in remote_names and not client.delete_paths([f"completed_takes/{name}"]):
            raise MigrationError(f"Couldn't delete completed_takes/{name} on the vault at {client.url}.")
        (completed / name).unlink(missing_ok=True)

    try:
        for n, old in enumerate(sorted(flacs), 1):
            new = Path(old).stem + ".mp3"
            prefix = f"[{n}/{len(flacs)}] {old}"
            # An earlier run already moved every reference over: just the
            # leftover .flac to delete (its check passed before the rename).
            if not sessions.entries(old) and any(e.get("source") for _, e in sessions.entries(new)):
                if not dry_run:
                    delete_flac(old)
                counts["leftovers_removed"] += 1
                log(f"{prefix}: already converted; {'would remove' if dry_run else 'removed'} the leftover .flac")
                continue
            snapshot = next(((s, e) for s, e in sessions.entries(old) if e.get("video")), None)
            if snapshot is None:
                counts["kept"] += 1
                log(f"{prefix}: kept as FLAC (no session video holds its original)")
                continue
            session, entry = snapshot
            try:
                take_path = completed / old
                if not take_path.exists():
                    take_path = work / "take.flac"
                    if not client.download_file(f"completed_takes/{old}", take_path):
                        raise MigrationError(f"couldn't download it from {client.url}")
                take, sr = sf.read(take_path, dtype="int16", always_2d=True)
                take = take[:, 0]
                start = _find_start(take, sr, entry["video"], session, sessions, work)
                if start is None:
                    counts["kept"] += 1
                    log(f"{prefix}: kept as FLAC (couldn't find it exactly in its session video)")
                    continue
                source = {"session": session, "start_frame": start, "end_frame": start + len(take), "sample_rate": sr}
                if dry_run:
                    counts["would_convert"] += 1
                    log(f"{prefix}: would convert (found at frame {start} of {session})")
                    continue

                # Step 2: the vault's own cut must be the .flac, bit for bit.
                before = json.loads(json.dumps(sessions.logs[session]))
                entry["source"] = source
                sessions.write_log(session)
                try:
                    cut = _session_cut(sessions, session, entry, old, start, len(take), sr, work / "check.flac")
                except Exception:
                    cut = None
                if cut is None or len(cut) != len(take) or not np.array_equal(cut, take):
                    sessions.logs[session] = before
                    sessions.write_log(session)
                    counts["kept"] += 1
                    log(f"{prefix}: kept as FLAC (the session's copy didn't match it exactly)")
                    continue

                # Steps 3-5.
                mp3 = ensure_dir(completed) / new
                encode_mp3(take_path, mp3)
                if client is not None:
                    client.upload_file(mp3, f"completed_takes/{new}")
                    if config.session_vault_mode == "remote":
                        mp3.unlink(missing_ok=True)
                _rename_references(config, sessions, old, new, source)
                delete_flac(old)
                counts["converted"] += 1
                log(f"{prefix}: converted to MP3 (original at frame {start} of {session})")
            except Exception as e:  # noqa: BLE001 — one take's failure never stops the rest
                counts["failed"] += 1
                log(f"{prefix}: FAILED, left as it was: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return counts
