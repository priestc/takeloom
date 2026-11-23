"""PhoneCameraHub: the server's end of every Takeloom Camera phone
connection (see protocol.py for the wire format, docs/takeloom-camera.md
for the whole design).

Owned by `takeloom server` (__main__.py's server_command, the only place a
recording ever happens — see CLAUDE.md) and attached to its LocalBackend
with attach_phone_camera_hub(). The backend uses it to:

- list connected phones as cameras (camera_device = DEVICE_PREFIX + id);
- route a phone's preview JPEGs into the Record tab's live preview;
- feed a session's live H.264 into ffmpeg (recorder.PhoneVideoRecorder,
  via set_video_sink) and tell the phone to start/stop its own full-
  quality recording;
- wait for that recording's upload before processing the session
  (expect_upload / wait_for_upload) — or, when the remote vault makes the
  session's videos, point the phone at the vault to upload it there
  directly (set_callbacks' upload_redirect) and follow its progress there.

Every connection runs on its own thread, like RemoteServer's. Auth is the
same LAN-only pairing flow, sharing RemoteServer's token list
(config.remote_authorized_clients) and its request_authorization callback.
"""

from __future__ import annotations

import datetime
import hmac
import ipaddress
import json
import os
import secrets
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import AuthorizedClient
from .app_version import version_status
from .models import display_name
from .protocol import (
    CAMERA_SERVER_PORT, DEVICE_PREFIX, FRAME_JSON, FRAME_PREVIEW, FRAME_UPLOAD, FRAME_VIDEO,
    ProtocolError, decode_json, decode_video, encode_json, read_frame,
)

VideoSink = Callable[[int, bool, bytes], None]

# Where a phone recording lands for a session from before cameras were a set
# (its one camera; sync.py turns it into session_video_raw.mp4). Every camera
# of a newer session lands at its own path instead —
# see upload_paths().
UPLOAD_FILE_NAME = "phone_camera.mp4"
UPLOAD_META_NAME = "phone_camera_upload.json"


def upload_paths(session_dir: Path, file_name: str = UPLOAD_FILE_NAME) -> tuple[Path, Path, Path]:
    """(final file, in-progress .part, metadata JSON) for an upload saved
    as session_dir/file_name. Flat in session_dir, never a subdirectory:
    the vault only uploads a session's top-level files (vault_client.py)."""
    dest = session_dir / file_name
    if file_name == UPLOAD_FILE_NAME:
        return dest, session_dir / "phone_camera_upload.part", session_dir / UPLOAD_META_NAME
    return dest, dest.with_name(dest.name + ".part"), dest.with_name(dest.stem + ".upload.json")


def _is_local_network(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


@dataclass
class PhoneCameraInfo:
    device_id: str
    client_name: str
    platform: str
    ip: str
    app_version: str = ""
    device_model: str = ""  # see models.py
    status: dict = field(default_factory=dict)
    # server_ns = phone_ns + clock_offset_ns; None until the phone's first
    # clock_sync report.
    clock_offset_ns: int | None = None
    clock_rtt_ns: int | None = None
    # Smoothed (server receive time - capture time) of live frames, i.e.
    # how stale the live feed is when it reaches us — what a live stream's
    # audio is delayed by to line up with it (see recorder.py).
    live_latency_ns: int | None = None
    # Live video received on this connection, for the one-off report
    # _report_live_feed logs a few seconds in.
    connected_at: float = field(default_factory=time.monotonic)
    video_frames: int = 0
    video_bytes: int = 0
    last_frame_at: float = 0.0  # time.monotonic() of the last frame of any kind
    live_reported: bool = False


# How long after a phone connects its live feed gets checked and logged.
_LIVE_REPORT_AFTER_S = 10.0


@dataclass
class _ExpectedUpload:
    session_id: str
    session_dir: Path
    device_id: str
    file_name: str = UPLOAD_FILE_NAME  # see upload_paths()
    state: str = "waiting"  # "waiting" | "uploading" | "done" | "failed"
    received: int = 0
    size: int = 0
    last_progress: float = field(default_factory=time.monotonic)
    error: str = ""
    # For a session already processed and moved to the vault (see
    # set_callbacks' late_upload_target): nothing waits on it — the
    # backend is told once it's in (on_late_upload).
    late: bool = False
    # Going straight to the remote vault (set_callbacks' upload_redirect):
    # its progress is the vault's (vault_progress), not this machine's.
    to_vault: bool = False
    # The phone coming back to ask again without the vault's copy having
    # grown since: it can't get through to the vault (_redirect_to_vault).
    vault_received_at_redirect: int = -1
    vault_stalls: int = 0
    vault_given_up: bool = False


# How big each chunk of an upload straight to the vault is (one HTTP PUT).
VAULT_CHUNK_BYTES = 8 * 1024 * 1024
# A phone asking again this many times with nothing more reaching the vault
# in between uploads here instead, for the rest of that recording.
_VAULT_STALLS_BEFORE_FALLBACK = 2
# How long a phone that's connected but sending nothing to the vault is
# waited for (it may be failing to reach it), and how often the vault is
# asked how far its upload is.
_VAULT_IDLE_TIMEOUT_S = 600.0
_VAULT_POLL_S = 5.0


# Status fields the backend reacts to: whether a phone's live feed can
# adapt and its recording size (bandwidth.py's plan), and what it can zoom
# (framing.py; video-ui's controls).
_LIVE_STATUS_KEYS = ("live_adaptive", "height", "zoom_caps")


class PhoneCameraHub:
    def __init__(
        self,
        get_config: Callable[[], object],
        save_config: Callable[[object], None],
        hostname: Callable[[], str],
        request_authorization: Callable[[str, str], bool],
        log: Callable[[str], None] | None = None,
        port: int = CAMERA_SERVER_PORT,
    ) -> None:
        self._get_config = get_config
        self._save_config = save_config
        self._hostname = hostname
        self.request_authorization = request_authorization
        self._log = log or (lambda msg: None)
        self.port = port
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._cameras: dict[str, _CameraConnection] = {}
        self._video_sinks: dict[str, VideoSink] = {}
        # Besides the one sink: everything else watching a phone's live feed
        # (full_preview.py), per device_id, across reconnects.
        self._video_taps: dict[str, list[VideoSink]] = {}
        self._pending_ips: set[str] = set()
        # device_id -> time.monotonic() it was last left with no connection
        # (gone from _cameras): how long a phone's really been away, not
        # just between one connection and the next.
        self._disconnected_at: dict[str, float] = {}
        # Both keyed by (session_id, device_id): every connected phone
        # records each session (see backend.py's session_cameras).
        self._uploads: dict[tuple[str, str], _ExpectedUpload] = {}
        self._recording_starts: dict[tuple[str, str], int] = {}
        self._session_dir_resolver: Callable[[str], Path | None] = lambda session_id: None
        self._on_preview: Callable[[str, bytes], None] = lambda device_id, jpeg: None
        self._on_change: Callable[[], None] = lambda: None
        self._on_live_change: Callable[[], None] = lambda: None
        self._on_framing: Callable[[str, dict], None] = lambda _device, _msg: None
        self._on_status: Callable[[str, dict], None] = lambda _device, _status: None
        self._late_upload_target: Callable[[str, str], tuple[Path, str] | None] = lambda _s, _d: None
        self._on_late_upload: Callable[[str, str], None] = lambda _s, _d: None
        self._upload_redirect: Callable[[str, str, str, str], dict | None] = lambda _s, _d, _f, _t: None
        self._vault_progress: Callable[[str, str], dict | None] = lambda _s, _f: None
        self._tcp_server: _ThreadingTCPServer | None = None

    # --- lifecycle ---

    def start(self) -> None:
        if self._tcp_server is not None:
            return

        def handler_factory(*args, **kwargs):
            return _Handler(*args, hub=self, **kwargs)

        self._tcp_server = _ThreadingTCPServer(("0.0.0.0", self.port), handler_factory)
        threading.Thread(target=self._tcp_server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._tcp_server is not None:
            self._tcp_server.shutdown()
            self._tcp_server.server_close()
            self._tcp_server = None
        with self._lock:
            conns = list(self._cameras.values())
        for conn in conns:
            conn.close()

    # --- hooks the backend installs ---

    def set_callbacks(
        self,
        on_preview: Callable[[str, bytes], None],
        on_change: Callable[[], None],
        session_dir_resolver: Callable[[str], Path | None],
        on_live_change: Callable[[], None] = lambda: None,
        on_framing: Callable[[str, dict], None] = lambda _device, _msg: None,
        on_status: Callable[[str, dict], None] = lambda _device, _status: None,
        late_upload_target: Callable[[str, str], tuple[Path, str] | None] = lambda _s, _d: None,
        on_late_upload: Callable[[str, str], None] = lambda _s, _d: None,
        upload_redirect: Callable[[str, str, str, str], dict | None] = lambda _s, _d, _f, _t: None,
        vault_progress: Callable[[str, str], dict | None] = lambda _s, _f: None,
    ) -> None:
        self._on_preview = on_preview
        self._on_change = on_change
        self._session_dir_resolver = session_dir_resolver
        # A phone's live feed reported a change (bandwidth.py re-plans).
        self._on_live_change = on_live_change
        # A lens picked on the phone's own screen ({"kind": "framing"}).
        self._on_framing = on_framing
        # Every status a phone sends (every few seconds): its battery.
        self._on_status = on_status
        # A recording for a session that's no longer here, already
        # processed from the live feeds and moved to the vault (e.g. the
        # server crashed mid-upload): (directory, file name) to take it into
        # anyway, or None to refuse it. Then on_late_upload once it's in.
        self._late_upload_target = late_upload_target
        self._on_late_upload = on_late_upload
        # Where a phone that can upload over HTTP should send a session's
        # recording instead of here: (session_id, device_id, file name,
        # the phone's token here) -> {"url", "token"} on the remote vault,
        # or None for here. Then vault_progress(session_id, file name) ->
        # the vault's {"received", "size", "done"} (None: couldn't ask).
        self._upload_redirect = upload_redirect
        self._vault_progress = vault_progress

    def log(self, message: str) -> None:
        self._log(message)

    # --- queries ---

    def cameras(self) -> list[PhoneCameraInfo]:
        with self._lock:
            return [c.info for c in self._cameras.values()]

    def camera(self, device_id: str) -> PhoneCameraInfo | None:
        with self._lock:
            conn = self._cameras.get(device_id)
            return conn.info if conn else None

    def camera_versions(self) -> dict[str, dict]:
        """camera_device -> app_version.version_status() of each connected
        phone."""
        return {DEVICE_PREFIX + c.device_id: version_status(c.app_version) for c in self.cameras()}

    def list_camera_devices(self) -> list[tuple[str, str]]:
        """(camera_device, display name) per connected phone, in the same
        shape video/devices.py's list_cameras returns."""
        return [
            (DEVICE_PREFIX + c.device_id, f"Takeloom Camera: {display_name(c.client_name, c.platform, c.device_model)}")
            for c in self.cameras()
        ]

    # --- live video + recording control ---

    def set_video_sink(self, device_id: str, sink: VideoSink | None) -> None:
        with self._lock:
            if sink is None:
                self._video_sinks.pop(device_id, None)
            else:
                self._video_sinks[device_id] = sink

    def add_video_tap(self, device_id: str, tap: VideoSink) -> None:
        with self._lock:
            self._video_taps.setdefault(device_id, []).append(tap)

    def remove_video_tap(self, device_id: str, tap: VideoSink) -> None:
        with self._lock:
            taps = self._video_taps.get(device_id, [])
            if tap in taps:
                taps.remove(tap)
            if not taps:
                self._video_taps.pop(device_id, None)

    def send(self, device_id: str, msg: dict) -> bool:
        with self._lock:
            conn = self._cameras.get(device_id)
        if conn is None:
            return False
        return conn.send_json(msg)

    def start_recording(self, device_id: str, session_id: str) -> bool:
        """Ask the phone to start its full-quality recording for session_id.
        Doesn't wait for the reply — that lands in recording_start_pts()."""
        return self.send(device_id, {"kind": "start_recording", "session_id": session_id})

    def recording_start_pts(self, session_id: str, device_id: str) -> int | None:
        """The phone's own recording's first-frame capture time (phone-clock
        ns), once it's confirmed starting it."""
        with self._lock:
            return self._recording_starts.get((session_id, device_id))

    def stop_recording(self, device_id: str, session_id: str) -> None:
        self.send(device_id, {"kind": "stop_recording", "session_id": session_id})

    # --- uploads ---

    def expect_upload(
        self, session_id: str, session_dir: Path, device_id: str, file_name: str = UPLOAD_FILE_NAME,
    ) -> None:
        with self._lock:
            self._uploads.setdefault(
                (session_id, device_id), _ExpectedUpload(session_id, session_dir, device_id, file_name),
            )
            self._cond.notify_all()

    def wait_for_upload(
        self,
        session_id: str,
        device_id: str,
        on_progress: Callable[[str], None],
        label: str = "Takeloom Camera",
        idle_timeout: float = 180.0,
        max_wait: float = 4 * 3600.0,
    ) -> Path | None:
        """Block until the phone's recording for session_id has fully
        uploaded, returning its path — or None once it's clearly not
        coming: nothing received for idle_timeout seconds while the phone
        is disconnected (or hasn't started sending at all), the phone
        reported a failure, or max_wait elapsed outright. Progress strings
        go to on_progress about every few seconds while waiting."""
        start = time.monotonic()
        last_report = 0.0
        last_poll = 0.0
        with self._cond:
            up = self._uploads.get((session_id, device_id))
            if up is None:
                return None
            while True:
                if up.state == "done":
                    return upload_paths(up.session_dir, up.file_name)[0]
                if up.state == "failed":
                    on_progress(f"{label} upload failed: {up.error}")
                    return None
                now = time.monotonic()
                connected = up.device_id in self._cameras
                # Away for the whole idle_timeout, not just at this moment:
                # a phone reconnecting is briefly in neither connection.
                away_for = 0.0 if connected else now - self._disconnected_at.get(up.device_id, start)
                if up.to_vault and now - last_poll >= _VAULT_POLL_S:
                    last_poll = now
                    self._cond.release()
                    try:
                        progress = self._vault_progress(session_id, up.file_name)
                    finally:
                        self._cond.acquire()
                    if progress:
                        if progress.get("done"):
                            up.state = "done"
                            continue
                        received = int(progress.get("received") or 0)
                        if received > up.received:
                            up.received, up.last_progress = received, now
                        up.size = int(progress.get("size") or up.size or 0)
                if now - start > max_wait:
                    return None
                if now - up.last_progress > idle_timeout and away_for > idle_timeout:
                    return None
                if up.to_vault and now - up.last_progress > _VAULT_IDLE_TIMEOUT_S:
                    on_progress(f"{label} has sent nothing to the vault for {_VAULT_IDLE_TIMEOUT_S / 60:.0f} minutes.")
                    return None
                if now - last_report >= 3.0:
                    last_report = now
                    if up.state == "uploading" and up.size:
                        pct = 100.0 * up.received / up.size
                        where = " to the vault" if up.to_vault else ""
                        msg = (f"{label} uploading its video{where}: {pct:.0f}% of {up.size / 1e9:.2f} GB"
                               if up.to_vault else f"Receiving {label} video: {pct:.0f}% of {up.size / 1e9:.2f} GB")
                    elif connected:
                        msg = f"Waiting for {label} to upload its recording…"
                    else:
                        msg = f"Waiting for {label} to reconnect and upload its recording…"
                    self._cond.release()
                    try:
                        on_progress(msg)
                    finally:
                        self._cond.acquire()
                self._cond.wait(1.0)

    def upload_went_to_vault(self, session_id: str, device_id: str) -> bool:
        with self._lock:
            up = self._uploads.get((session_id, device_id))
            return up is not None and up.to_vault

    def forget_upload(self, session_id: str, device_id: str) -> None:
        with self._lock:
            self._uploads.pop((session_id, device_id), None)
            self._recording_starts.pop((session_id, device_id), None)

    # --- called from connection handlers ---

    def _register_camera(self, conn: "_CameraConnection") -> None:
        with self._lock:
            old = self._cameras.get(conn.info.device_id)
            self._cameras[conn.info.device_id] = conn
            self._disconnected_at.pop(conn.info.device_id, None)
            self._cond.notify_all()
        if old is not None and old is not conn:
            old.replaced = True
            old.close()
        self._on_change()

    def _unregister_camera(self, conn: "_CameraConnection") -> None:
        with self._lock:
            if self._cameras.get(conn.info.device_id) is conn:
                del self._cameras[conn.info.device_id]
                self._disconnected_at[conn.info.device_id] = time.monotonic()
            self._cond.notify_all()
        self._on_change()

    def _deliver_video(self, device_id: str, pts_ns: int, keyframe: bool, data: bytes) -> None:
        with self._lock:
            sinks = [self._video_sinks.get(device_id), *self._video_taps.get(device_id, [])]
        for sink in sinks:
            if sink is not None:
                sink(pts_ns, keyframe, data)

    def _upload_target(self, session_id: str, device_id: str) -> _ExpectedUpload | None:
        """The expected upload for session_id, or — for a session whose
        server process restarted since (so nothing's expecting it in
        memory) — one rebuilt from its directory if it's still unprocessed
        and was recorded with this same phone."""
        with self._lock:
            up = self._uploads.get((session_id, device_id))
            if up is not None:
                return up
        session_dir = self._session_dir_resolver(session_id)
        data = None
        if session_dir is not None:
            try:
                data = json.loads((session_dir / "session_log.json").read_text())
            except (OSError, ValueError):
                return None
        # Already processed — gone to the vault, or still here waiting to be
        # uploaded there ("takes" is what processing leaves in the log; with
        # the vault making the videos, none of them is ever here to look
        # for) — so made from the live feed: a late upload.
        if session_dir is None or data.get("takes") is not None:
            try:
                late = self._late_upload_target(session_id, device_id)
            except Exception as e:  # noqa: BLE001 — e.g. the vault unreachable: refuse for now
                self._log(f"Couldn't check for a late Takeloom Camera recording of {session_id}: {e}")
                late = None
            if late is None:
                return None
            late_dir, late_file = late
            with self._lock:
                return self._uploads.setdefault(
                    (session_id, device_id), _ExpectedUpload(session_id, late_dir, device_id, late_file, late=True),
                )
        phone = data.get("phone_camera") or {}
        if phone.get("device_id") == device_id:
            if (session_dir / "session_video.mp4").exists():
                return None  # already processed
            file_name = UPLOAD_FILE_NAME
        else:
            extra = next(
                (c for c in data.get("extra_cameras", [])
                 if (c.get("phone_camera") or {}).get("device_id") == device_id),
                None,
            )
            if extra is None or not extra.get("upload_file"):
                return None
            if (session_dir / extra["final_file"]).exists():
                return None  # already processed
            file_name = extra["upload_file"]
        with self._lock:
            return self._uploads.setdefault(
                (session_id, device_id), _ExpectedUpload(session_id, session_dir, device_id, file_name),
            )


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class _CameraConnection:
    def __init__(self, sock: socket.socket, info: PhoneCameraInfo) -> None:
        self.sock = sock
        self.info = info
        self.closed = False
        self.replaced = False  # closed because the same phone connected again
        self._write_lock = threading.Lock()

    def send_json(self, msg: dict) -> bool:
        return self.send_raw(encode_json(msg))

    def send_raw(self, data: bytes) -> bool:
        try:
            with self._write_lock:
                self.sock.sendall(data)
            return True
        except OSError:
            return False

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _Handler(socketserver.BaseRequestHandler):
    def __init__(self, *args, hub: PhoneCameraHub, **kwargs) -> None:
        self._hub = hub
        super().__init__(*args, **kwargs)  # runs handle() synchronously

    def _send(self, msg: dict) -> None:
        self.request.sendall(encode_json(msg))

    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ip = self.client_address[0]
        try:
            if not _is_local_network(ip):
                self._hub._log(f"Takeloom Camera: refused connection from non-local address {ip}")
                self._send({"kind": "hello_ack", "ok": False,
                            "error": "This server only accepts connections from the local network."})
                return
            frame_type, payload = read_frame(sock)
            if frame_type != FRAME_JSON:
                return
            hello = decode_json(payload)
            if hello.get("kind") == "discover":
                # A phone scanning the network for this server (no pairing
                # needed to be found — still LAN-only, checked above).
                self._send({"kind": "discover_ack", "server": "takeloom", "hostname": self._hub._hostname()})
                return
            if hello.get("kind") != "hello" or not self._authenticate(hello, ip):
                return
            device_id = str(hello.get("device_id") or "")
            if not device_id:
                return
            if hello.get("role") == "upload":
                self._handle_upload(sock, device_id, hello)
            else:
                self._handle_camera(sock, hello, device_id, ip)
        except (OSError, ConnectionError, ProtocolError):
            pass

    def _authenticate(self, hello: dict, ip: str) -> bool:
        hub = self._hub
        token = str(hello.get("token") or "")
        client_name = str(hello.get("client_name") or ip)
        config = hub._get_config()
        match = None
        if token:
            match = next(
                (c for c in config.remote_authorized_clients if hmac.compare_digest(token, c.token)), None,
            )
        now = datetime.datetime.now().isoformat()
        if match is not None:
            match.last_ip = ip
            match.last_seen_at = now
            hub._save_config(config)
            self._send({"kind": "hello_ack", "ok": True, "hostname": hub._hostname()})
            return True

        with hub._lock:
            if ip in hub._pending_ips:
                already_pending = True
            else:
                already_pending = False
                hub._pending_ips.add(ip)
        if already_pending:
            self._send({"kind": "hello_ack", "ok": False,
                        "error": "A request from this address is already pending approval."})
            return False
        self._send({"kind": "hello_pending"})
        try:
            approved = hub.request_authorization(ip, f"{client_name} (Takeloom Camera)")
        finally:
            with hub._lock:
                hub._pending_ips.discard(ip)
        if not approved:
            self._send({"kind": "hello_ack", "ok": False, "error": "Authorization denied."})
            return False
        new_token = secrets.token_hex(16)
        config = hub._get_config()  # re-fetch: avoid clobbering concurrent edits
        config.remote_authorized_clients.append(AuthorizedClient(
            token=new_token, label=f"{client_name} (Takeloom Camera)", last_ip=ip,
            authorized_at=now, last_seen_at=now,
        ))
        hub._save_config(config)
        self._send({"kind": "hello_ack", "ok": True, "hostname": hub._hostname(), "token": new_token})
        return True

    # --- the live camera connection ---

    def _handle_camera(self, sock: socket.socket, hello: dict, device_id: str, ip: str) -> None:
        hub = self._hub
        info = PhoneCameraInfo(
            device_id=device_id, client_name=str(hello.get("client_name") or ip),
            platform=str(hello.get("platform") or ""), ip=ip,
            app_version=str(hello.get("app_version") or ""),
            device_model=str(hello.get("device_model") or ""),
        )
        conn = _CameraConnection(sock, info)
        hub._register_camera(conn)
        status = version_status(info.app_version)
        hub._log(f"Takeloom Camera '{info.client_name}' ({ip}) connected, app {info.app_version or 'version unknown'}"
                 + (f" — outdated, {status['latest']} is available" if status["outdated"] else ""))
        end_reason = "the phone closed it"
        read_failed = False
        try:
            while not conn.closed:
                try:
                    frame_type, payload = read_frame(sock)
                except (OSError, ConnectionError) as e:
                    read_failed = True
                    if str(e) != "connection closed":  # protocol.py's clean EOF: the phone hung up
                        end_reason = f"network error: {type(e).__name__}: {e}"
                    raise
                except ProtocolError as e:
                    read_failed = True
                    end_reason = f"bad data from the phone: {e}"
                    raise
                info.last_frame_at = time.monotonic()
                if frame_type == FRAME_VIDEO:
                    pts_ns, keyframe, data = decode_video(payload)
                    info.video_frames += 1
                    info.video_bytes += len(data)
                    if info.clock_offset_ns is not None:
                        latency = time.monotonic_ns() - (pts_ns + info.clock_offset_ns)
                        prev = info.live_latency_ns
                        info.live_latency_ns = latency if prev is None else (prev * 15 + latency) // 16
                    hub._deliver_video(device_id, pts_ns, keyframe, data)
                elif frame_type == FRAME_PREVIEW:
                    hub._on_preview(device_id, payload)
                elif frame_type == FRAME_JSON:
                    self._on_camera_message(conn, decode_json(payload))
        except BaseException as e:
            if not read_failed:
                end_reason = f"server error: {type(e).__name__}: {e}"
            raise
        finally:
            conn.closed = True
            hub._unregister_camera(conn)
            # Why, so a phone that keeps reconnecting can be told apart: the
            # phone giving up on a working link (video still arriving right
            # up to its replacement) vs. the link really dying (video
            # stopped some seconds before).
            now = time.monotonic()
            if conn.replaced:
                end_reason = "replaced: the phone opened a new connection"
            quiet = f", last data {now - info.last_frame_at:.1f} s before" if info.last_frame_at else ", no data at all"
            hub._log(f"Takeloom Camera '{info.client_name}' ({ip}) disconnected — {end_reason} "
                     f"(after {now - info.connected_at:.1f} s, {info.video_frames} video frames{quiet})")

    def _report_live_feed(self, info: PhoneCameraInfo) -> None:
        """Once per connection, a few seconds in: what live video actually
        arrived, next to what the phone says it's sending — the first thing
        to look at when a phone's feed looks wrong or missing."""
        elapsed = time.monotonic() - info.connected_at
        if info.live_reported or elapsed < _LIVE_REPORT_AFTER_S:
            return
        info.live_reported = True
        st = info.status
        phone = (f"phone reports {st.get('live_width')}x{st.get('live_height')} at "
                 f"{(st.get('live_bitrate') or 0) / 1e6:.1f} Mbps, {st.get('live_frames')} frames encoded"
                 + (f", encoder error {st.get('live_error')}" if st.get("live_error") else "")
                 if "live_frames" in st else "phone doesn't report its encoder")
        camera_state = st.get("camera_state")
        if camera_state and camera_state != "open":
            # The Android app says why its camera isn't running (1.5.1+).
            phone += f"; its camera: {camera_state}"
        if info.video_frames:
            got = (f"{info.video_frames / elapsed:.0f} fps, "
                   f"{info.video_bytes * 8 / elapsed / 1e6:.1f} Mbps over the first {elapsed:.0f} s")
        else:
            got = f"NO live video in {elapsed:.0f} s"
        self._hub._log(f"Takeloom Camera '{info.client_name}' live feed: {got} ({phone})")

    def _on_camera_message(self, conn: _CameraConnection, msg: dict) -> None:
        hub = self._hub
        kind = msg.get("kind")
        if kind == "clock_ping":
            # Stamped and answered right here on the reader thread, so the
            # only thing between receipt and reply is this one send.
            conn.send_json({"kind": "clock_pong", "id": msg.get("id"), "t0": msg.get("t0"),
                            "ts": time.monotonic_ns()})
        elif kind == "clock_sync":
            conn.info.clock_offset_ns = int(msg["offset_ns"])
            conn.info.clock_rtt_ns = int(msg.get("rtt_ns") or 0)
        elif kind == "status":
            before = {k: conn.info.status.get(k) for k in _LIVE_STATUS_KEYS}
            conn.info.status = {k: v for k, v in msg.items() if k != "kind"}
            if before != {k: conn.info.status.get(k) for k in _LIVE_STATUS_KEYS}:
                hub._on_live_change()
            self._report_live_feed(conn.info)
            hub._on_status(conn.info.device_id, conn.info.status)
        elif kind == "framing":
            # Off the reader thread: saving the config and answering the
            # phone mustn't hold up its video.
            threading.Thread(target=hub._on_framing, args=(conn.info.device_id, msg),
                             name="phone-framing", daemon=True).start()
        elif kind == "recording_started":
            with hub._lock:
                key = (str(msg.get("session_id")), conn.info.device_id)
                hub._recording_starts[key] = int(msg.get("video_start_pts_ns") or 0)
        elif kind == "recording_stopped":
            hub._log(f"Takeloom Camera '{conn.info.client_name}' stopped recording "
                     f"({(msg.get('file_size') or 0) / 1e6:.0f} MB, {msg.get('duration_s', 0):.0f} s)")
        elif kind == "recording_failed":
            session_id = str(msg.get("session_id"))
            hub._log(f"Takeloom Camera '{conn.info.client_name}' recording failed: {msg.get('error')}")
            with hub._lock:
                up = hub._uploads.get((session_id, conn.info.device_id))
                if up is not None:
                    up.state, up.error = "failed", str(msg.get("error") or "recording failed on the phone")
                hub._cond.notify_all()

    # --- an upload connection ---

    def _handle_upload(self, sock: socket.socket, device_id: str, hello: dict) -> None:
        hub = self._hub
        frame_type, payload = read_frame(sock)
        if frame_type != FRAME_JSON:
            return
        begin = decode_json(payload)
        if begin.get("kind") != "upload_begin":
            return
        session_id = str(begin.get("session_id") or "")
        size = int(begin.get("size") or 0)
        up = hub._upload_target(session_id, device_id) if session_id else None
        if up is None:
            self._send({"kind": "upload_rejected",
                        "error": f"This server isn't expecting a recording for session '{session_id}' "
                                 "(already processed, or recorded elsewhere)."})
            return
        if up.state == "done":
            self._send({"kind": "upload_resume", "offset": size})
            self._expect_end_and_ack(sock, up)
            return

        dest, part, meta_path = upload_paths(up.session_dir, up.file_name)
        if hello.get("http_upload") and not up.late and self._redirect_to_vault(up, begin, hello, size, meta_path):
            return
        offset = part.stat().st_size if part.exists() else 0
        if offset > size:
            part.unlink()
            offset = 0
        with hub._lock:
            up.state, up.size, up.received, up.last_progress = "uploading", size, offset, time.monotonic()
            up.to_vault = False  # coming here after all (the vault couldn't take it this time)
            hub._cond.notify_all()
        meta_path.write_text(json.dumps(begin.get("metadata") or {}, indent=2))
        self._send({"kind": "upload_resume", "offset": offset})
        hub._log(f"Receiving Takeloom Camera recording for {session_id} from {device_id} "
                 f"({size / 1e9:.2f} GB{f', resuming at {offset / 1e9:.2f} GB' if offset else ''})")

        try:
            with open(part, "ab") as out:
                while True:
                    frame_type, payload = read_frame(sock)
                    if frame_type == FRAME_UPLOAD:
                        out.write(payload)
                        with hub._lock:
                            up.received += len(payload)
                            up.last_progress = time.monotonic()
                            hub._cond.notify_all()
                    elif frame_type == FRAME_JSON and decode_json(payload).get("kind") == "upload_end":
                        out.flush()
                        os.fsync(out.fileno())
                        break
        finally:
            with hub._lock:
                if up.state == "uploading":
                    up.state = "waiting"  # dropped mid-transfer; the phone resumes from what's on disk
                hub._cond.notify_all()

        received = part.stat().st_size
        if received != size:
            self._send({"kind": "upload_done", "ok": False,
                        "error": f"Received {received} of {size} bytes."})
            return
        part.replace(dest)
        with hub._lock:
            up.state = "done"
            hub._cond.notify_all()
        self._send({"kind": "upload_done", "ok": True})
        hub._log(f"Takeloom Camera recording for {session_id} received in full")
        if up.late:
            hub.forget_upload(session_id, device_id)
            threading.Thread(target=hub._on_late_upload, args=(session_id, device_id),
                             name="late-camera-upload", daemon=True).start()

    def _redirect_to_vault(self, up: _ExpectedUpload, begin: dict, hello: dict, size: int, meta_path: Path) -> bool:
        """Point the phone at the remote vault for this recording, if the
        backend says so (set_callbacks' upload_redirect). The metadata
        stays here, where the session's processing reads it."""
        hub = self._hub
        if up.vault_given_up:
            return False
        # How far the vault's copy is, to tell next time whether anything
        # got through in between (asking again = the last try ended).
        try:
            progress = hub._vault_progress(up.session_id, up.file_name)
        except Exception:  # noqa: BLE001
            progress = None
        received = int((progress or {}).get("received") or 0)
        if up.to_vault:
            if progress is None or received <= up.vault_received_at_redirect:
                up.vault_stalls += 1
            else:
                up.vault_stalls = 0
        up.vault_received_at_redirect = max(up.vault_received_at_redirect, received)
        if up.to_vault:
            if up.vault_stalls >= _VAULT_STALLS_BEFORE_FALLBACK:
                up.vault_given_up = True
                hub._log(f"Takeloom Camera recording for {up.session_id} from {up.device_id}: nothing more has "
                         f"reached the vault in {up.vault_stalls} tries; receiving it here instead")
                return False
        try:
            target = hub._upload_redirect(up.session_id, up.device_id, up.file_name, str(hello.get("token") or ""))
        except Exception as e:  # noqa: BLE001 — anything wrong: take it here instead
            hub._log(f"Couldn't send a Takeloom Camera recording to the vault ({type(e).__name__}: {e}); "
                     "taking it here instead")
            target = None
        if not target:
            return False
        meta_path.write_text(json.dumps(begin.get("metadata") or {}, indent=2))
        with hub._lock:
            if not up.to_vault:
                up.last_progress = time.monotonic()
            up.to_vault, up.state, up.size = True, "uploading", size
            hub._cond.notify_all()
        self._send({"kind": "upload_redirect", "url": target["url"], "token": target["token"],
                    "chunk_size": VAULT_CHUNK_BYTES})
        hub._log(f"Takeloom Camera recording for {up.session_id} from {up.device_id} "
                 f"({size / 1e9:.2f} GB) is going straight to the vault")
        return True

    def _expect_end_and_ack(self, sock: socket.socket, up: _ExpectedUpload) -> None:
        frame_type, payload = read_frame(sock)
        if frame_type == FRAME_JSON and decode_json(payload).get("kind") == "upload_end":
            self._send({"kind": "upload_done", "ok": True})
