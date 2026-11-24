"""Client for the remote Studio Vault server (the separate `takeloom-vault`
package in this repo's vault_server/ folder, running on the storage box).

This is the only way takeloom talks to the remote vault — it replaced the
old rsync-over-SSH sync.py. Every call is authenticated with the token
this machine got by pairing (see begin_pairing/poll_pairing, driven from
the Studio Setup tab's "Pair" button via Backend.begin_vault_pairing).

Methods that the old rsync functions mirrored keep their "best-effort,
return a bool/None, never raise" contract, since their callers (session
processing on a background thread, Sessions-tab fallbacks) depend on it;
VaultError is only raised by the pairing calls, whose callers show it.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlsplit

from .net import terse_source_note

# Same number as vault_server/takeloom_vault/__init__.py's DEFAULT_PORT —
# lets a vault address be entered as just "tank2.lan" or "http://tank2.lan".
DEFAULT_VAULT_PORT = 51824

_TIMEOUT = 20
# Per-socket-operation timeout during a transfer, not for the whole file:
# a multi-GB session upload is fine as long as bytes keep moving.
_TRANSFER_TIMEOUT = 120
# Cutting a long take out of its session video (a re-encode) on the vault
# server, before the first byte comes back.
_CLIP_TIMEOUT = 1800
_CHUNK = 1024 * 1024

LogFn = Callable[[str], None]


class VaultError(Exception):
    pass


def normalize_vault_url(address: str) -> str:
    """"tank2.lan", "tank2.lan:51824", "http://tank2.lan" -> "http://tank2.lan:51824"
    style base URL with no trailing slash; "" stays ""."""
    address = address.strip().rstrip("/")
    if not address:
        return ""
    if "://" not in address:
        address = f"http://{address}"
    parts = urlsplit(address)
    if parts.port is None:
        address = f"{parts.scheme}://{parts.hostname}:{DEFAULT_VAULT_PORT}{parts.path}"
    return address


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _describe(exc: Exception, url: str) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = json.loads(exc.read() or b"{}").get("error") or exc.reason
        except (ValueError, OSError):
            detail = exc.reason
        return f"{url} answered HTTP {exc.code}: {detail}"
    if isinstance(exc, urllib.error.URLError):
        return f"Could not reach {url}: {exc.reason}{terse_source_note(exc)}"
    return f"{url}: {type(exc).__name__}: {exc}"


class _Stopped(OSError):
    pass


_PROGRESS_MIN_BYTES = 50_000_000


class _StoppableReader:
    """A file body for urllib that raises once `should_stop()` turns true."""

    def __init__(self, f, should_stop: Callable[[], bool]) -> None:
        self._f, self._should_stop = f, should_stop

    def read(self, n: int = -1) -> bytes:
        if self._should_stop():
            raise _Stopped("stopped: a session opened")
        return self._f.read(n)


class VaultClient:
    def __init__(self, url: str, token: str = "") -> None:
        self.url = normalize_vault_url(url)
        self.token = token

    def __repr__(self) -> str:
        return f"VaultClient({self.url!r})"

    # --- plumbing ---

    def _request(
        self, method: str, path: str, body: bytes | None = None, timeout: float = _TIMEOUT,
        headers: dict | None = None,
    ):
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=headers or {})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        return urllib.request.urlopen(req, timeout=timeout)

    def _json(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Raises VaultError (with the URL in it) on any failure."""
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        try:
            with self._request(method, path, body, headers=headers) as resp:
                return json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise VaultError(_describe(e, self.url + path)) from e

    @staticmethod
    def _q(relative: str) -> str:
        return quote(relative, safe="/")

    # --- pairing ---

    def begin_pairing(self, client_name: str | None = None) -> dict:
        """{"request_id", "code"} — show `code` to the user, who approves
        it on the vault's web page; then poll_pairing() until it settles."""
        return self._json("POST", "/api/pair", {"client_name": client_name or socket.gethostname()})

    def poll_pairing(self, request_id: str) -> dict:
        """{"status": "pending"|"approved"|"denied"|"expired", "token"?}"""
        return self._json("GET", f"/api/pair/{quote(request_id)}")

    def ping(self) -> dict:
        """{"ok", "hostname", "client"} — raises VaultError if unreachable
        or this machine's token isn't (or is no longer) accepted."""
        return self._json("GET", "/api/ping")

    # --- sessions ---

    def list_session_logs(self) -> dict[str, dict]:
        """Every session's session_log.json, keyed by session directory
        name — {} on any failure (a convenience listing; see backend.py's
        list_sessions)."""
        try:
            return self._json("GET", "/api/sessions").get("sessions", {})
        except VaultError:
            return {}

    def fetch_session_log(self, session_dir_name: str) -> dict | None:
        try:
            return self._json("GET", f"/api/sessions/{quote(session_dir_name, safe='')}/log")
        except VaultError:
            return None

    def deleted_sessions(self) -> list[dict] | None:
        """[{"session", "takes", "deleted_at"}]: sessions deleted from the
        vault's web UI, for this machine to drop too (vault.py's
        apply_vault_deletions). None if the vault couldn't be reached, or
        predates that (takeloom-vault < 0.15)."""
        try:
            deleted = self._json("GET", "/api/deleted-sessions").get("deleted")
        except VaultError:
            return None
        return [d for d in deleted if isinstance(d, dict)] if isinstance(deleted, list) else None

    def setlist_edits(self) -> list[dict] | None:
        """Setlist changes queued from the vault's Completed Takes page
        ({"id", "type": "preferred_take"|"trim", "take_filename", ...}), in
        the order made — for Backend._apply_vault_setlist_edits. None if
        the vault couldn't be reached, or predates them (takeloom-vault < 0.16)."""
        try:
            edits = self._json("GET", "/api/setlist-edits").get("edits")
        except VaultError:
            return None
        return [e for e in edits if isinstance(e, dict) and e.get("id")] if isinstance(edits, list) else None

    def ack_setlist_edits(self, ids: list[str]) -> bool:
        """Take edits this machine has made and uploaded off the vault's queue."""
        if not ids:
            return True
        try:
            self._json("POST", "/api/setlist-edits/ack", {"ids": ids})
            return True
        except VaultError:
            return False

    def set_youtube(self, data: dict) -> None:
        """Give the vault this machine's YouTube account (or "" refresh_token
        to forget it), for publishing edits from there (the vault's
        youtube.py). Raises VaultError."""
        self._json("PUT", "/api/youtube", data)

    def write_session_log(self, session_dir_name: str, data: dict) -> bool:
        try:
            self._json("PUT", f"/api/sessions/{quote(session_dir_name, safe='')}/log", data)
            return True
        except VaultError:
            return False

    def upload_session(
        self, local_dir: Path, session_dir_name: str, log: LogFn | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict | None:
        """Upload every file in `local_dir` to sessions/<session_dir_name>/
        — each one checksum-verified by the server before it's renamed
        into place — with session_log.json deliberately last: the server
        only lists a session once that file exists, so a session cut off
        partway through never shows up as if it were complete. A file the
        vault already has at the same size (from an earlier, interrupted
        attempt) isn't sent again, so a retry picks up where that one
        stopped. `should_stop`, checked as it goes (even mid-file), aborts
        it — a retry in the background gives way to a session opening.

        Returns {"files", "skipped", "bytes", "seconds"} (what was actually
        sent, and how long it took) once every file is there, else None."""
        log = log or (lambda msg: None)
        # A cheap auth/reachability check first: the server can't report
        # e.g. a revoked token cleanly once a multi-GB body is in flight.
        try:
            self.ping()
        except VaultError as e:
            log(f"Vault upload not started: {e}")
            return None
        files = sorted(
            (f for f in local_dir.iterdir() if f.is_file() and not f.name.startswith(".")),
            key=lambda f: (f.name == "session_log.json", f.name),
        )
        there = {f["name"]: f["size"] for f in self.list_files(f"sessions/{session_dir_name}") or []}
        if "render_jobs.json" in there and not (local_dir / "render_jobs.json").exists():
            # An earlier attempt got the videos' jobs and inputs there, then
            # failed, and this machine made the videos itself (render_jobs.py's
            # run_locally). Left there, the vault would run those jobs again
            # once session_log.json arrives, from inputs since deleted here.
            self._drop_stale_render_jobs(session_dir_name, there, log)
        stats = {"files": 0, "skipped": 0, "bytes": 0, "seconds": 0.0}
        total = sum(
            f.stat().st_size for f in files
            if f.name == "session_log.json" or there.get(f.name) != f.stat().st_size
        )
        started = time.monotonic()
        for f in files:
            if should_stop is not None and should_stop():
                log(f"Vault upload of '{session_dir_name}' paused: a session opened.")
                return None
            size = f.stat().st_size
            if f.name != "session_log.json" and there.get(f.name) == size:
                stats["skipped"] += 1
                continue
            if size >= _PROGRESS_MIN_BYTES:
                # One line per big file (the videos), so there's visible
                # progress through a multi-GB upload — the Stream Deck
                # shows it between sessions.
                log(f"Uploading to the vault: {stats['bytes'] / 1e9:.1f} of {total / 1e9:.1f} GB sent ({f.name})")
            try:
                self.upload_file(f, f"sessions/{session_dir_name}/{f.name}", should_stop=should_stop)
            except VaultError as e:
                if should_stop is not None and should_stop():
                    log(f"Vault upload of '{session_dir_name}' paused partway through {f.name}: a session opened.")
                else:
                    log(f"Vault upload of {f.name} failed: {e}")
                return None
            stats["files"] += 1
            stats["bytes"] += size
        stats["seconds"] = time.monotonic() - started
        return stats

    def _drop_stale_render_jobs(self, session_dir_name: str, there: dict, log: LogFn) -> None:
        base = f"sessions/{session_dir_name}"
        stale = ["render_jobs.json"]
        fd, tmp = tempfile.mkstemp(prefix=".render-jobs-", suffix=".json")
        os.close(fd)
        try:
            if self.download_file(f"{base}/render_jobs.json", Path(tmp)):
                jobs = json.loads(Path(tmp).read_text())
                stale += [name for name in jobs.get("cleanup") or [] if name in there]
        except (OSError, ValueError):
            pass
        finally:
            Path(tmp).unlink(missing_ok=True)
        if self.delete_paths([f"{base}/{name}" for name in stale]):
            log(f"Removed {session_dir_name}'s stale video jobs from the vault (its videos were made here): "
                f"{', '.join(stale)}")
            for name in stale:
                there.pop(name, None)

    # --- arbitrary vault files ---

    def upload_file(
        self, local_path: Path, vault_relative: str, should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """Raises VaultError on failure — including `should_stop` turning
        true mid-upload, which cuts the body off (the server then discards
        the partial file: see its receive_file)."""
        size = local_path.stat().st_size
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
            "X-Content-SHA256": _sha256_file(local_path),
        }
        path = f"/api/files/{self._q(vault_relative)}"
        try:
            with open(local_path, "rb") as f:
                body = _StoppableReader(f, should_stop) if should_stop is not None else f
                req = urllib.request.Request(self.url + path, data=body, method="PUT", headers=headers)
                if self.token:
                    req.add_header("Authorization", f"Bearer {self.token}")
                with urllib.request.urlopen(req, timeout=_TRANSFER_TIMEOUT) as resp:
                    resp.read()
        except (urllib.error.URLError, OSError) as e:
            raise VaultError(_describe(e, self.url + path)) from e

    def download_file(self, vault_relative: str, local_path: Path) -> bool:
        """Download into `local_path` via a temp file beside it, renamed
        into place only once the whole body arrived — a dropped transfer
        never leaves a truncated file under the real name. Returns whether
        it succeeded (and the file now exists)."""
        return self._download(f"/api/files/{self._q(vault_relative)}", local_path, _TRANSFER_TIMEOUT)

    def download_take_video(self, take_filename: str, local_path: Path, camera: str = "") -> bool:
        """The video of completed take `take_filename` from `camera` (a
        label; "": the take's default), which the server cuts from that
        camera's session video on first request (and caches) — see
        take_video.py. The first request for a take can sit silent for
        minutes while that encode runs, hence the long timeout."""
        query = f"?camera={quote(camera, safe='')}" if camera else ""
        return self._download(
            f"/api/takes/{quote(take_filename, safe='')}/video{query}", local_path, _CLIP_TIMEOUT,
        )

    def download_take_original(self, take_filename: str, local_path: Path, legacy: bool = True) -> bool:
        """A lossless FLAC of completed take `take_filename`, which the
        server cuts back out of its session (see take_original.py). With
        legacy=False, never a legacy .flac take file itself, only a cut from
        the session (migrate_takes.py checks one against the other)."""
        return self._download(
            f"/api/takes/{quote(take_filename, safe='')}/original" + ("" if legacy else "?legacy=0"),
            local_path, _CLIP_TIMEOUT,
        )

    def _download(self, path: str, local_path: Path, timeout: float) -> bool:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".download-", dir=local_path.parent)
        try:
            with os.fdopen(fd, "wb") as out, self._request("GET", path, timeout=timeout) as resp:
                expected = int(resp.headers.get("Content-Length") or -1)
                written = 0
                while chunk := resp.read(_CHUNK):
                    out.write(chunk)
                    written += len(chunk)
            if expected >= 0 and written != expected:
                raise OSError(f"got {written} of {expected} bytes")
            os.replace(tmp_name, local_path)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            Path(tmp_name).unlink(missing_ok=True)
            return False

    def list_files(self, vault_relative_dir: str) -> list[dict] | None:
        """[{"name", "size", "mtime"?}] of the files directly in a vault
        folder ("mtime" only from takeloom-vault 0.10+), or None if the
        vault couldn't be reached."""
        try:
            return self._json("GET", f"/api/list/{self._q(vault_relative_dir)}").get("files", [])
        except VaultError:
            return None

    # --- devices this machine vouches for (vault_server's auth.py delegate) ---

    def delegate(self, label: str, key: str) -> dict:
        """{"id", "token"}: a vault token for a device paired with this
        machine (a phone, a remote-control laptop), good for as long as this
        machine stays paired; one per `key`, so asking again replaces it.
        Raises VaultError (takeloom-vault before 0.11 has no such route)."""
        return self._json("POST", "/api/clients", {"label": label, "key": key})

    def list_delegates(self) -> list[dict]:
        """[{"id", "label", "key", "last_seen_at"}] of the tokens this
        machine vouched for. Raises VaultError."""
        return self._json("GET", "/api/clients").get("clients", [])

    def revoke_delegate(self, client_id: str) -> bool:
        try:
            self._json("POST", f"/api/clients/{quote(client_id, safe='')}/revoke", {})
            return True
        except VaultError:
            return False

    # --- phone recordings uploaded straight to the vault (its camera_uploads.py) ---

    def camera_upload_url(self, session_dir_name: str, file_name: str) -> str:
        return f"{self.url}/api/camera-uploads/{quote(session_dir_name, safe='')}/{quote(file_name, safe='')}"

    def session_camera_url(self, session_dir_name: str, file_name: str) -> str:
        """Where a phone uploads its recording of a session whose cameras are
        sources: straight into the session's folder (the vault's align.py)."""
        return f"{self.url}{self._session_camera_path(session_dir_name, file_name)}"

    def _session_camera_path(self, session_dir_name: str, file_name: str) -> str:
        return f"/api/sessions/{quote(session_dir_name, safe='')}/camera/{quote(file_name, safe='')}"

    def session_camera_status(self, session_dir_name: str, file_name: str) -> dict:
        """{"received", "size", "done"} of a recording uploading into its
        session's folder. Raises VaultError."""
        return self._json("GET", self._session_camera_path(session_dir_name, file_name))

    def _camera_upload_path(self, session_dir_name: str, file_name: str) -> str:
        return f"/api/camera-uploads/{quote(session_dir_name, safe='')}/{quote(file_name, safe='')}"

    def camera_upload_status(self, session_dir_name: str, file_name: str) -> dict:
        """{"received", "size" (None before the first chunk), "done"}.
        Raises VaultError."""
        return self._json("GET", self._camera_upload_path(session_dir_name, file_name))

    def camera_upload_first_video_pts(self, session_dir_name: str, file_name: str) -> float:
        """Raises VaultError."""
        path = self._camera_upload_path(session_dir_name, file_name) + "/probe"
        return float(self._json("GET", path)["first_video_pts"])

    def camera_upload_audio(self, session_dir_name: str, file_name: str) -> bytes:
        """The recording's sound, mono at 8 kHz, raw float32: what
        phone_camera/sync.py lines it up with. Raises VaultError."""
        path = self._camera_upload_path(session_dir_name, file_name) + "/audio"
        try:
            with self._request("GET", path, timeout=_CLIP_TIMEOUT) as resp:
                data = resp.read()
                expected = int(resp.headers.get("Content-Length") or -1)
        except (urllib.error.URLError, OSError) as e:
            raise VaultError(_describe(e, self.url + path)) from e
        if expected >= 0 and len(data) != expected:
            raise VaultError(f"{self.url + path}: got {len(data)} of {expected} bytes")
        return data

    def delete_paths(self, vault_relatives: list[str]) -> bool:
        """Permanently delete each path (a session directory, take files);
        one that's already gone is fine. Returns whether it succeeded."""
        if not vault_relatives:
            return True
        try:
            self._json("POST", "/api/delete", {"paths": vault_relatives})
            return True
        except VaultError:
            return False
