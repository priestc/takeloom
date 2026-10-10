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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlsplit

from .net import terse_source_note

# Same number as vault_server/takeloom_vault/__init__.py's DEFAULT_PORT —
# lets a vault address be entered as just "tank2" or "http://tank2".
DEFAULT_VAULT_PORT = 51824

_TIMEOUT = 20
# Per-socket-operation timeout during a transfer, not for the whole file:
# a multi-GB session upload is fine as long as bytes keep moving.
_TRANSFER_TIMEOUT = 120
_CHUNK = 1024 * 1024

LogFn = Callable[[str], None]


class VaultError(Exception):
    pass


def normalize_vault_url(address: str) -> str:
    """"tank2", "tank2:51824", "http://tank2" -> "http://tank2:51824"
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

    def write_session_log(self, session_dir_name: str, data: dict) -> bool:
        try:
            self._json("PUT", f"/api/sessions/{quote(session_dir_name, safe='')}/log", data)
            return True
        except VaultError:
            return False

    def upload_session(self, local_dir: Path, session_dir_name: str, log: LogFn | None = None) -> bool:
        """Upload every file in `local_dir` to sessions/<session_dir_name>/
        — each one checksum-verified by the server before it's renamed
        into place — with session_log.json deliberately last: the server
        only lists a session once that file exists, so a session cut off
        partway through never shows up as if it were complete. Re-running
        after a failure simply re-sends everything. Returns True only if
        every file made it."""
        log = log or (lambda msg: None)
        # A cheap auth/reachability check first: the server can't report
        # e.g. a revoked token cleanly once a multi-GB body is in flight.
        try:
            self.ping()
        except VaultError as e:
            log(f"Vault upload not started: {e}")
            return False
        files = sorted(
            (f for f in local_dir.iterdir() if f.is_file() and not f.name.startswith(".")),
            key=lambda f: (f.name == "session_log.json", f.name),
        )
        for f in files:
            try:
                self.upload_file(f, f"sessions/{session_dir_name}/{f.name}")
            except VaultError as e:
                log(f"Vault upload of {f.name} failed: {e}")
                return False
        return True

    # --- arbitrary vault files ---

    def upload_file(self, local_path: Path, vault_relative: str) -> None:
        """Raises VaultError on failure."""
        size = local_path.stat().st_size
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
            "X-Content-SHA256": _sha256_file(local_path),
        }
        path = f"/api/files/{self._q(vault_relative)}"
        try:
            with open(local_path, "rb") as body:
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
        local_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".download-", dir=local_path.parent)
        try:
            with os.fdopen(fd, "wb") as out, self._request(
                "GET", f"/api/files/{self._q(vault_relative)}", timeout=_TRANSFER_TIMEOUT,
            ) as resp:
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
        """[{"name", "size"}] of the files directly in a vault folder, or
        None if the vault couldn't be reached."""
        try:
            return self._json("GET", f"/api/list/{self._q(vault_relative_dir)}").get("files", [])
        except VaultError:
            return None

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
