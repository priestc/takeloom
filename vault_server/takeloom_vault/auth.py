"""Pairing and token auth — the same shape as takeloom's own remote-mode
pairing (takeloom/remote/server.py), adapted to HTTP:

1. A takeloom instance with no token POSTs /api/pair with its machine
   name. That creates a *pending* request with a short random code, which
   the client shows to its user ("approve code 482913 on the vault").
2. Someone opens the vault's web page, which lists pending requests with
   their codes and the requesting IP, and clicks Approve (or Deny).
3. The client, polling GET /api/pair/<id>, gets its freshly minted token
   back exactly once, and sends it as `Authorization: Bearer <token>` on
   every API call from then on.

Only a SHA-256 of each token is stored, so the clients file on disk isn't
itself a set of usable credentials. Like takeloom's remote mode, this is
LAN-trust (plain HTTP, local-network peers only — see server.py's
is_local_network), not something to expose to the internet.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# How long a pairing request waits for a human before it expires —
# matches the order of takeloom's own remote-mode approval prompt.
PAIRING_TTL_SECONDS = 300


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class PairedClient:
    id: str
    label: str
    token_sha256: str
    last_ip: str = ""
    paired_at: str = ""
    last_seen_at: str = ""


@dataclass
class PairingRequest:
    id: str
    code: str
    client_name: str
    ip: str
    created: float
    status: str = "pending"  # "pending" | "approved" | "denied"
    token: str = ""  # set on approval; handed out once, then cleared

    @property
    def expired(self) -> bool:
        return self.status == "pending" and time.time() - self.created > PAIRING_TTL_SECONDS


@dataclass
class Auth:
    path: Path
    clients: list[PairedClient] = field(default_factory=list)
    _pending: dict[str, PairingRequest] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _mtime: float = 0.0

    @classmethod
    def load(cls, path: Path) -> "Auth":
        auth = cls(path=path)
        auth._reload_if_changed()
        return auth

    def _reload_if_changed(self) -> None:
        """Pick up edits made by another process — `takeloom-vault revoke`
        run while the server is up takes effect on the next request."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != self._mtime:
            data = json.loads(self.path.read_text())
            self.clients = [PairedClient(**c) for c in data.get("clients", [])]
            self._mtime = mtime

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"clients": [asdict(c) for c in self.clients]}, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".clients-")
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    # --- token checks ---

    def authenticate(self, token: str, ip: str) -> PairedClient | None:
        if not token:
            return None
        digest = _hash(token)
        with self._lock:
            self._reload_if_changed()
            for client in self.clients:
                if hmac.compare_digest(digest, client.token_sha256):
                    # Only persist last-seen at most once a minute — this
                    # runs on every API call, including each file upload.
                    now = _now_iso()
                    if client.last_ip != ip or client.last_seen_at[:16] != now[:16]:
                        client.last_ip, client.last_seen_at = ip, now
                        self._save()
                    return client
        return None

    def revoke(self, client_id: str) -> bool:
        with self._lock:
            self._reload_if_changed()
            before = len(self.clients)
            self.clients = [c for c in self.clients if c.id != client_id]
            if len(self.clients) == before:
                return False
            self._save()
            return True

    # --- pairing ---

    def request_pairing(self, client_name: str, ip: str) -> PairingRequest:
        with self._lock:
            self._expire_locked()
            # One live request per IP: a client retrying shouldn't stack up
            # a pile of identical rows for the human to sort through.
            for req in list(self._pending.values()):
                if req.ip == ip and req.status == "pending":
                    del self._pending[req.id]
            req = PairingRequest(
                id=secrets.token_urlsafe(12),
                code=f"{secrets.randbelow(1_000_000):06d}",
                client_name=(client_name or "unknown")[:80],
                ip=ip,
                created=time.time(),
            )
            self._pending[req.id] = req
            return req

    def pending_requests(self) -> list[PairingRequest]:
        with self._lock:
            self._expire_locked()
            return sorted(
                (r for r in self._pending.values() if r.status == "pending"), key=lambda r: r.created,
            )

    def decide(self, request_id: str, approve: bool) -> PairingRequest | None:
        with self._lock:
            self._expire_locked()
            req = self._pending.get(request_id)
            if req is None or req.status != "pending":
                return None
            if not approve:
                req.status = "denied"
                return req
            token = secrets.token_hex(32)
            now = _now_iso()
            self._reload_if_changed()
            self.clients.append(PairedClient(
                id=secrets.token_hex(4), label=req.client_name, token_sha256=_hash(token),
                last_ip=req.ip, paired_at=now, last_seen_at=now,
            ))
            self._save()
            req.status, req.token = "approved", token
            return req

    def poll(self, request_id: str, ip: str) -> dict:
        """What GET /api/pair/<id> returns. The token is handed out once
        and only to the IP that asked for it, then the request is dropped."""
        with self._lock:
            self._expire_locked()
            req = self._pending.get(request_id)
            if req is None or req.ip != ip:
                return {"status": "expired"}
            if req.status == "pending":
                return {"status": "pending", "code": req.code}
            del self._pending[request_id]
            if req.status == "approved":
                return {"status": "approved", "token": req.token}
            return {"status": "denied"}

    def _expire_locked(self) -> None:
        cutoff = time.time() - PAIRING_TTL_SECONDS * 2
        for req_id, req in list(self._pending.items()):
            if req.created < cutoff or req.expired:
                del self._pending[req_id]
