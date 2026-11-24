"""The vault directory itself, laid out exactly as takeloom's own
takeloom/vault.py describes it:

    <vault>/
    ├── sessions/<session_name>_<project_name>/   session.flac, session_log.json, session_video.mp4, ...
    ├── backing_tracks/<filename>
    ├── completed_takes/<filename>                 .mp3 (LAME V2), .video.json, .mid
    ├── backing_tuning.json, inspiration_takes.json
    ├── deleted_sessions.json                      sessions deleted from the web UI (delete_session)
    ├── mixes/<song>.json                          the Completed Takes mixer's saved mixes
    ├── final_mixes/<song>.mp3                     exported song mixes
    ├── projects/<project>.json                    setlist backups
    └── camera_uploads/<session_name>/<file>       phone recordings uploaded here directly,
                                                   until their session's videos are made
                                                   (camera_uploads.py)

Everything the HTTP API touches goes through resolve() first, which is
what keeps a client-supplied path from ever escaping the vault root.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

# The only top-level folders a client may read or write, plus the few
# files takeloom keeps in the vault root itself. Anything else there
# (.DS_Store, ...) is out of the API's reach entirely.
TOP_LEVEL_DIRS = (
    "sessions", "backing_tracks", "completed_takes", "projects", "mixes", "final_mixes", "camera_uploads",
)
ROOT_FILES = ("inspiration_takes.json", "backing_tuning.json")

# In-progress uploads land beside their destination under this prefix and
# are renamed into place only once their hash checks out — so a dropped
# connection never leaves a truncated file under the real name.
PARTIAL_PREFIX = ".upload-"

# Written last by takeloom's session upload (see takeloom/vault_client.py's
# upload_session), so its presence is what marks a session as complete —
# a half-uploaded session directory without one is never listed.
SESSION_LOG = "session_log.json"

CHUNK = 1024 * 1024

# Sessions deleted from the web UI, for takeloom to catch up on (delete_session).
DELETED_SESSIONS = "deleted_sessions.json"


class VaultPathError(ValueError):
    pass


class Vault:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def resolve(self, relative: str) -> Path:
        """`relative` (e.g. "sessions/<name>/session.flac") as an absolute
        path inside the vault, or VaultPathError if it's empty, absolute,
        has a ".." or hidden component, or isn't under TOP_LEVEL_DIRS (or
        one of ROOT_FILES)."""
        parts = [p for p in relative.replace("\\", "/").split("/") if p]
        if parts and len(parts) == 1 and parts[0] in ROOT_FILES:
            return self.root / parts[0]
        if not parts or parts[0] not in TOP_LEVEL_DIRS:
            raise VaultPathError(f"Path must start with one of {', '.join(TOP_LEVEL_DIRS)} "
                                 f"(or be one of {', '.join(ROOT_FILES)}): {relative!r}")
        if any(p in (".", "..") or p.startswith(".") for p in parts):
            raise VaultPathError(f"Invalid path component in {relative!r}")
        path = self.root.joinpath(*parts)
        if self.root not in path.resolve().parents:
            raise VaultPathError(f"Path escapes the vault: {relative!r}")
        return path

    # --- sessions ---

    def session_dir(self, name: str) -> Path:
        if "/" in name or "\\" in name:
            raise VaultPathError(f"Invalid session name {name!r}")
        return self.resolve(f"sessions/{name}")

    def session_names(self) -> list[str]:
        sessions = self.root / "sessions"
        if not sessions.is_dir():
            return []
        return sorted(
            (d.name for d in sessions.iterdir() if d.is_dir() and (d / SESSION_LOG).is_file()),
            reverse=True,
        )

    def read_session_log(self, name: str) -> dict | None:
        path = self.session_dir(name) / SESSION_LOG
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def all_session_logs(self) -> dict[str, dict]:
        logs = {}
        for name in self.session_names():
            data = self.read_session_log(name)
            if data is not None:
                logs[name] = data
        return logs

    def write_session_log(self, name: str, data: dict) -> None:
        directory = self.session_dir(name)
        if not directory.is_dir():
            raise FileNotFoundError(name)
        _atomic_write(directory / SESSION_LOG, json.dumps(data, indent=2).encode())

    # --- files ---

    def list_dir(self, relative: str) -> list[dict]:
        directory = self.resolve(relative)
        if not directory.is_dir():
            return []
        return [
            {"name": f.name, "size": (st := f.stat()).st_size, "mtime": st.st_mtime}
            for f in sorted(directory.iterdir())
            if f.is_file() and not f.name.startswith(".")
        ]

    def receive_file(self, relative: str, body: BinaryIO, length: int, expected_sha256: str) -> dict:
        """Stream exactly `length` bytes from `body` into `relative`,
        hashing as it goes; only rename into place if the hash matches
        `expected_sha256`. Raises ValueError on a mismatch or short body
        (the partial file is removed either way)."""
        dest = self.resolve(relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        fd, tmp_name = tempfile.mkstemp(prefix=PARTIAL_PREFIX, dir=dest.parent)
        tmp = Path(tmp_name)
        try:
            remaining = length
            with os.fdopen(fd, "wb") as out:
                while remaining > 0:
                    chunk = body.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ValueError(f"Upload of {relative!r} ended after {length - remaining} of {length} bytes")
                    digest.update(chunk)
                    out.write(chunk)
                    remaining -= len(chunk)
                out.flush()
                os.fsync(out.fileno())
            actual = digest.hexdigest()
            if expected_sha256 and actual != expected_sha256.lower():
                raise ValueError(f"Checksum mismatch for {relative!r}: expected {expected_sha256}, got {actual}")
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return {"path": relative, "size": length, "sha256": actual}

    def delete(self, relatives: list[str]) -> None:
        """Delete every path (file or directory) in `relatives`; a path
        that's already gone is fine. Resolves them all before deleting any,
        so one invalid path deletes nothing."""
        targets = [self.resolve(r) for r in relatives]
        for target in targets:
            if target == self.root / target.relative_to(self.root).parts[0]:
                raise VaultPathError(f"Refusing to delete a whole top-level folder: {target.name}")
        for target in targets:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()

    def delete_session(self, name: str) -> list[str]:
        """Permanently delete session `name`: its folder, any phone
        recordings still waiting in camera_uploads/ for it, and every take
        it filed (completed_takes/<stem>.*). Recorded in
        deleted_sessions.json so takeloom, on its next sync, drops those
        takes from its setlists and the inspiration index and deletes its
        own copies instead of uploading them again (takeloom/vault.py's
        apply_vault_deletions). Returns the take filenames it filed.
        FileNotFoundError if there's no such session."""
        directory = self.session_dir(name)
        if not directory.is_dir():
            raise FileNotFoundError(name)
        data = self.read_session_log(name) or {}
        takes = sorted({
            t["filename"] for entries in (data.get("takes") or {}).values()
            for t in entries if isinstance(t, dict) and t.get("filename")
        })
        # Record it first: a crash part-way still leaves takeloom told.
        deleted = self.deleted_sessions()
        if not any(d.get("session") == name for d in deleted):
            deleted.append({"session": name, "takes": takes,
                            "deleted_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")})
            _atomic_write(self.root / DELETED_SESSIONS, json.dumps(deleted, indent=2).encode())
        for name_ in take_files(takes):
            (self.root / "completed_takes" / name_).unlink(missing_ok=True)
        shutil.rmtree(self.root / "camera_uploads" / name, ignore_errors=True)
        shutil.rmtree(directory)
        return takes

    def deleted_sessions(self) -> list[dict]:
        """[{"session", "takes", "deleted_at"}] for every session deleted
        here (delete_session), oldest first."""
        try:
            data = json.loads((self.root / DELETED_SESSIONS).read_text())
        except (OSError, ValueError):
            return []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def session_size(self, name: str) -> int:
        directory = self.session_dir(name)
        return sum(f.stat().st_size for f in directory.iterdir() if f.is_file())


# Every file a completed take can have beside it (as takeloom's
# Backend.delete_session).
TAKE_EXTENSIONS = (".mp3", ".flac", ".mp4", ".m4a", ".wav", ".mid", ".video.json")


def take_files(takes: list[str]) -> list[str]:
    takes = [t for t in takes if "/" not in t and "\\" not in t and not t.startswith(".")]
    return sorted({t for t in takes} | {Path(t).stem + ext for t in takes for ext in TAKE_EXTENSIONS})


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=PARTIAL_PREFIX, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
