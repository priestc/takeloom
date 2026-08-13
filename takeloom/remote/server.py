"""RemoteServer: threaded TCP server exposing one LocalBackend to remote
takeloom clients.

Each connection gets its own handler thread. `LocalBackend` events
(recording_status, preview_paused, preview_resumed) are broadcast to every
connected client, not just the one that triggered them, so a second viewer
stays in sync. Camera-preview frames are opt-in per connection via
subscribe_preview/unsubscribe_preview, since they're the one bandwidth-heavy
part of the protocol.

Auth is a pairing-request flow rather than a single shared secret: a client
with no token (or an unrecognized one) causes the handler thread to call
`request_authorization(ip, client_name)` and block on its result — this is
safe because every connection already runs on its own thread. The caller
(`takeloom server` — see __main__.py's server_command, the only place a
RemoteServer is ever constructed) supplies that callback as an interactive
terminal prompt with its own timeout. On approval the handler mints a new
per-client token, persists it to config, and returns it once in `hello_ack`.
"""

from __future__ import annotations

import base64
import datetime
import hmac
import ipaddress
import json
import secrets
import socket
import socketserver
import threading
from pathlib import Path
from typing import Callable

from ..backend import Backend, BackendError
from ..config import AuthorizedClient
from .protocol import dispatch


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


# Pure getters that clients call to refresh their own view (e.g. on
# connect, or after an event) rather than because the operator did
# anything. Logging these would drown out the ops that actually matter —
# see _handle_request, which only logs ops outside this set.
_READ_ONLY_OPS = {
    "hostname", "get_config", "list_audio_devices", "list_cameras",
    "list_projects", "get_setlist", "get_filter_slot_previews",
    "search_inspiration_artists", "search_inspiration_by_filter",
    "is_recording", "get_compressor_settings", "get_monitoring_mode",
    "list_sessions", "get_session_detail", "analyze_take", "fetch_take_file",
    "list_completed_takes",
}


def _is_local_network(ip: str) -> bool:
    """True for loopback/private/link-local addresses — i.e. not reachable
    from the public internet even if this machine's own NAT/firewall is
    misconfigured to forward the port. This is the enforcement point for
    "local network only": binding still has to be 0.0.0.0 (a specific LAN
    interface IP can't be assumed and may change with DHCP), so this check
    is what actually keeps a non-LAN peer from ever getting past hello."""
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


class RemoteServer:
    def __init__(
        self,
        backend: Backend,
        port: int,
        request_authorization: Callable[[str, str], bool],
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.backend = backend
        self.port = port
        self.request_authorization = request_authorization
        self._log = log or (lambda msg: None)
        self._tcp_server: _ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._connections_lock = threading.Lock()
        self._connections: list[_ClientHandler] = []
        self._pending_ips: set[str] = set()
        backend.on_event(self._on_backend_event)

    @property
    def is_running(self) -> bool:
        return self._tcp_server is not None

    @property
    def client_count(self) -> int:
        with self._connections_lock:
            return len(self._connections)

    def start(self) -> None:
        if self._tcp_server is not None:
            return

        def handler_factory(*args, **kwargs):
            return _ClientHandler(*args, server_owner=self, **kwargs)

        self._tcp_server = _ThreadingTCPServer(("0.0.0.0", self.port), handler_factory)
        self._thread = threading.Thread(target=self._tcp_server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._tcp_server is not None:
            self._tcp_server.shutdown()
            self._tcp_server.server_close()
            self._tcp_server = None
        self._thread = None
        with self._connections_lock:
            conns = list(self._connections)
            self._connections.clear()
        for conn in conns:
            conn.close()

    # --- connection registry, used by _ClientHandler ---

    def _register(self, handler: "_ClientHandler") -> None:
        with self._connections_lock:
            self._connections.append(handler)

    def _unregister(self, handler: "_ClientHandler") -> None:
        with self._connections_lock:
            if handler in self._connections:
                self._connections.remove(handler)

    def _broadcast(self, event: str, data: dict) -> None:
        with self._connections_lock:
            conns = list(self._connections)
        for conn in conns:
            conn.send_event(event, data)

    def broadcast_file(self, event: str, path: Path, extra: dict | None = None, chunk_size: int = 512 * 1024) -> None:
        """Send a local file's bytes to every connected client as a
        sequence of base64-encoded chunks under `event` — {"seq", "total",
        "data_b64", **extra} per chunk, `extra` repeated on every chunk for
        a simpler client (no special-casing chunk 0). Chunked rather than
        one giant line: a multi-ten-MB video check video would otherwise
        block any concurrent RPC on the same client connection (one
        write-lock-serialized TCP socket per client) for as long as the
        single write takes. Call sites are expected to check client_count
        first — this reads the whole file into memory regardless."""
        extra = extra or {}
        data = path.read_bytes()
        total = max(1, (len(data) + chunk_size - 1) // chunk_size)
        for seq in range(total):
            chunk = data[seq * chunk_size:(seq + 1) * chunk_size]
            self._broadcast(event, {
                "seq": seq, "total": total,
                "data_b64": base64.b64encode(chunk).decode("ascii"),
                **extra,
            })

    # Events with no legitimate remote audience: Video Check is refused
    # outright by RemoteBackend (a remote client can never trigger one), so
    # a video_check_status event only ever describes this server's own
    # local activity (e.g. its attached physical Stream Deck) — its
    # result_path names a file that only exists on this machine. Never
    # forward it; a connected client acting on it would try to open a path
    # that doesn't exist on its own filesystem.
    _LOCAL_ONLY_EVENTS = frozenset({"video_check_status"})

    def _on_backend_event(self, event: str, data: dict) -> None:
        if event in self._LOCAL_ONLY_EVENTS:
            return
        self._broadcast(event, data)

    def disconnect_token(self, token: str) -> None:
        """Close any live connection authenticated with this token (used when
        the UI revokes an AuthorizedClient)."""
        with self._connections_lock:
            conns = [c for c in self._connections if c.token == token]
        for conn in conns:
            conn.close()


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class _ClientHandler(socketserver.StreamRequestHandler):
    def __init__(self, *args, server_owner: RemoteServer, **kwargs) -> None:
        self._owner = server_owner
        self._write_lock = threading.Lock()
        self._preview_sub = None
        self.token: str | None = None
        self.client_name: str = "?"
        super().__init__(*args, **kwargs)  # runs handle() synchronously

    def handle(self) -> None:
        ip = self.client_address[0]
        if not _is_local_network(ip):
            self._owner._log(f"[{_timestamp()}] Refused connection from non-local address {ip}")
            try:
                self._write({
                    "kind": "hello_ack", "ok": False,
                    "error": "This server only accepts connections from the local network.",
                })
            except OSError:
                pass
            return
        try:
            hello_line = self.rfile.readline()
            if not hello_line:
                return
            try:
                hello = json.loads(hello_line.decode("utf-8"))
            except ValueError:
                return

            token = hello.get("token") or ""
            client_name = hello.get("client_name") or ip
            self.client_name = client_name

            config = self._owner.backend.get_config()
            match = None
            if token:
                match = next(
                    (c for c in config.remote_authorized_clients if hmac.compare_digest(token, c.token)),
                    None,
                )

            if match is not None:
                now = datetime.datetime.now().isoformat()
                match.last_ip = ip
                match.last_seen_at = now
                self._owner.backend.save_config(config)
                self.token = match.token
                self._owner._log(f"[{_timestamp()}] {client_name} ({ip}) connected")
                self._write({
                    "kind": "hello_ack", "ok": True, "hostname": self._owner.backend.hostname(),
                })
            else:
                with self._owner._connections_lock:
                    already_pending = ip in self._owner._pending_ips
                    if not already_pending:
                        self._owner._pending_ips.add(ip)
                if already_pending:
                    self._write({
                        "kind": "hello_ack", "ok": False,
                        "error": "A request from this address is already pending approval.",
                    })
                    return
                # Lets the client show "waiting to be authorized" instead of
                # a plain "connecting" while this thread blocks below, since
                # that can take up to request_authorization's own timeout
                # with nothing else on the wire to distinguish it from an
                # ordinary slow connect.
                self._write({"kind": "hello_pending"})
                try:
                    approved = self._owner.request_authorization(ip, client_name)
                finally:
                    with self._owner._connections_lock:
                        self._owner._pending_ips.discard(ip)

                if not approved:
                    self._write({"kind": "hello_ack", "ok": False, "error": "Authorization denied."})
                    return

                new_token = secrets.token_hex(16)
                now = datetime.datetime.now().isoformat()
                config = self._owner.backend.get_config()  # re-fetch: avoid clobbering concurrent edits
                config.remote_authorized_clients.append(
                    AuthorizedClient(token=new_token, label=client_name, last_ip=ip, authorized_at=now, last_seen_at=now)
                )
                self._owner.backend.save_config(config)
                self.token = new_token
                self._owner._log(f"[{_timestamp()}] {client_name} ({ip}) connected (newly paired)")
                self._write({
                    "kind": "hello_ack", "ok": True, "hostname": self._owner.backend.hostname(), "token": new_token,
                })

            self._owner._register(self)
            try:
                while True:
                    line = self.rfile.readline()
                    if not line:
                        break
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if msg.get("kind") == "request":
                        # One thread per request rather than handling it
                        # inline and only then going back to read the next
                        # line: a slow op (e.g. an inspiration-server query
                        # a filter slot preview kicks off — see backend.py's
                        # get_filter_slot_previews) would otherwise block
                        # this whole connection's request queue behind it,
                        # silently stalling unrelated commands (start_auto_
                        # detect_instrument, start_recording, ...) queued
                        # right after it with no error either side would
                        # see. LocalBackend is already built to tolerate
                        # concurrent calls (see its own locks, e.g.
                        # _record_lock) since local UI actions already run
                        # this way from separate worker threads — Remote
                        # should behave the same, not serialize everything
                        # through one connection.
                        threading.Thread(
                            target=self._handle_request, args=(msg,), daemon=True,
                        ).start()
            finally:
                if self._preview_sub is not None:
                    self._preview_sub.close()
                    self._preview_sub = None
                self._owner._unregister(self)
                self._owner._log(f"[{_timestamp()}] {self.client_name} ({ip}) disconnected")
        except (OSError, ConnectionError):
            pass

    def _handle_request(self, msg: dict) -> None:
        req_id = msg.get("id")
        op = msg.get("op")
        args = msg.get("args") or {}
        who = f"{self.client_name} ({self.client_address[0]})"
        if op not in _READ_ONLY_OPS:
            self._owner._log(f"[{_timestamp()}] {who}: {op}" + (f" {args}" if args else ""))
        try:
            if op == "subscribe_preview":
                if self._preview_sub is None:
                    self._preview_sub = self._owner.backend.open_camera_preview(self._on_preview_frame)
                result = {}
            elif op == "unsubscribe_preview":
                if self._preview_sub is not None:
                    self._preview_sub.close()
                    self._preview_sub = None
                result = {}
            elif op == "fetch_take_file":
                # Not a plain 1:1 Backend method: ensure_take_local only
                # resolves/downloads the file to *this* (server) machine's
                # disk — sending it back to whichever client actually
                # asked for it is this op's own job, via chunked "take_
                # file" events on this connection (see send_file), before
                # the ordinary RPC response below so that by the time the
                # client's blocking call() returns, every chunk has
                # already arrived (see RemoteBackend.play_take).
                path = self._owner.backend.ensure_take_local(args["project_name"], args["filename"])
                self.send_file("take_file", Path(path), extra={"filename": args["filename"]})
                result = {}
            else:
                result = dispatch(self._owner.backend, op, args)
            self._write({"kind": "response", "id": req_id, "ok": True, "result": result})
        except BackendError as e:
            self._owner._log(f"[{_timestamp()}]   <- error: {e}")
            self._write({"kind": "response", "id": req_id, "ok": False, "error": str(e)})
        except Exception as e:
            self._owner._log(f"[{_timestamp()}]   <- internal error: {e}")
            self._write({"kind": "response", "id": req_id, "ok": False, "error": f"Internal error: {e}"})

    def _on_preview_frame(self, jpeg: bytes) -> None:
        self.send_event("preview_frame", {"jpeg_b64": base64.b64encode(jpeg).decode("ascii")})

    def send_event(self, event: str, data: dict) -> None:
        try:
            self._write({"kind": "event", "event": event, "data": data})
        except OSError:
            pass

    def send_file(self, event: str, path: Path, extra: dict | None = None, chunk_size: int = 512 * 1024) -> None:
        """Same chunking as RemoteServer.broadcast_file, but to this one
        connection only — used by "fetch_take_file" to send a requested
        take back to whichever client asked for it, not every connected
        client."""
        extra = extra or {}
        data = path.read_bytes()
        total = max(1, (len(data) + chunk_size - 1) // chunk_size)
        for seq in range(total):
            chunk = data[seq * chunk_size:(seq + 1) * chunk_size]
            self.send_event(event, {
                "seq": seq, "total": total,
                "data_b64": base64.b64encode(chunk).decode("ascii"),
                **extra,
            })

    def _write(self, obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode("utf-8")
        with self._write_lock:
            self.wfile.write(data)

    def close(self) -> None:
        # shutdown() first: unlike close(), it reliably unblocks a concurrent
        # readline() this handler's own thread may be sitting in — closing
        # the fd from another thread alone doesn't wake a blocked read on
        # every platform (notably macOS/BSD).
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass
