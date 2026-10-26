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
  (expect_upload / wait_for_upload).

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
from .protocol import (
    CAMERA_SERVER_PORT, DEVICE_PREFIX, FRAME_JSON, FRAME_PREVIEW, FRAME_UPLOAD, FRAME_VIDEO,
    ProtocolError, decode_json, decode_video, encode_json, read_frame,
)

VideoSink = Callable[[int, bool, bytes], None]

# Where the main camera's phone recording lands (sync.py turns it into
# session_video_raw.mp4). An extra camera's lands at its own path instead —
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
    status: dict = field(default_factory=dict)
    # server_ns = phone_ns + clock_offset_ns; None until the phone's first
    # clock_sync report.
    clock_offset_ns: int | None = None
    clock_rtt_ns: int | None = None
    # Smoothed (server receive time - capture time) of live frames, i.e.
    # how stale the live feed is when it reaches us — what a live stream's
    # audio is delayed by to line up with it (see recorder.py).
    live_latency_ns: int | None = None


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
        self._pending_ips: set[str] = set()
        # Both keyed by (session_id, device_id): every connected phone
        # records each session (see backend.py's session_cameras).
        self._uploads: dict[tuple[str, str], _ExpectedUpload] = {}
        self._recording_starts: dict[tuple[str, str], int] = {}
        self._session_dir_resolver: Callable[[str], Path | None] = lambda session_id: None
        self._on_preview: Callable[[str, bytes], None] = lambda device_id, jpeg: None
        self._on_change: Callable[[], None] = lambda: None
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
    ) -> None:
        self._on_preview = on_preview
        self._on_change = on_change
        self._session_dir_resolver = session_dir_resolver

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
        return [(DEVICE_PREFIX + c.device_id, f"Takeloom Camera: {c.client_name}") for c in self.cameras()]

    # --- live video + recording control ---

    def set_video_sink(self, device_id: str, sink: VideoSink | None) -> None:
        with self._lock:
            if sink is None:
                self._video_sinks.pop(device_id, None)
            else:
                self._video_sinks[device_id] = sink

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
                if now - start > max_wait:
                    return None
                if now - up.last_progress > idle_timeout and not connected:
                    return None
                if now - last_report >= 3.0:
                    last_report = now
                    if up.state == "uploading" and up.size:
                        pct = 100.0 * up.received / up.size
                        msg = f"Receiving {label} video: {pct:.0f}% of {up.size / 1e9:.2f} GB"
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

    def forget_upload(self, session_id: str, device_id: str) -> None:
        with self._lock:
            self._uploads.pop((session_id, device_id), None)
            self._recording_starts.pop((session_id, device_id), None)

    # --- called from connection handlers ---

    def _register_camera(self, conn: "_CameraConnection") -> None:
        with self._lock:
            old = self._cameras.get(conn.info.device_id)
            self._cameras[conn.info.device_id] = conn
            self._cond.notify_all()
        if old is not None and old is not conn:
            old.close()
        self._on_change()

    def _unregister_camera(self, conn: "_CameraConnection") -> None:
        with self._lock:
            if self._cameras.get(conn.info.device_id) is conn:
                del self._cameras[conn.info.device_id]
            self._cond.notify_all()
        self._on_change()

    def _deliver_video(self, device_id: str, pts_ns: int, keyframe: bool, data: bytes) -> None:
        with self._lock:
            sink = self._video_sinks.get(device_id)
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
        if session_dir is None:
            return None
        try:
            data = json.loads((session_dir / "session_log.json").read_text())
        except (OSError, ValueError):
            return None
        if (session_dir / "session_video.mp4").exists():
            return None  # already processed
        phone = data.get("phone_camera") or {}
        if phone.get("device_id") == device_id:
            file_name = UPLOAD_FILE_NAME
        else:
            extra = next(
                (c for c in data.get("extra_cameras", [])
                 if (c.get("phone_camera") or {}).get("device_id") == device_id),
                None,
            )
            if extra is None or not extra.get("upload_file"):
                return None
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
            if hello.get("kind") != "hello" or not self._authenticate(hello, ip):
                return
            device_id = str(hello.get("device_id") or "")
            if not device_id:
                return
            if hello.get("role") == "upload":
                self._handle_upload(sock, device_id)
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
        )
        conn = _CameraConnection(sock, info)
        hub._register_camera(conn)
        status = version_status(info.app_version)
        hub._log(f"Takeloom Camera '{info.client_name}' ({ip}) connected, app {info.app_version or 'version unknown'}"
                 + (f" — outdated, {status['latest']} is available" if status["outdated"] else ""))
        try:
            while not conn.closed:
                frame_type, payload = read_frame(sock)
                if frame_type == FRAME_VIDEO:
                    pts_ns, keyframe, data = decode_video(payload)
                    if info.clock_offset_ns is not None:
                        latency = time.monotonic_ns() - (pts_ns + info.clock_offset_ns)
                        prev = info.live_latency_ns
                        info.live_latency_ns = latency if prev is None else (prev * 15 + latency) // 16
                    hub._deliver_video(device_id, pts_ns, keyframe, data)
                elif frame_type == FRAME_PREVIEW:
                    hub._on_preview(device_id, payload)
                elif frame_type == FRAME_JSON:
                    self._on_camera_message(conn, decode_json(payload))
        finally:
            conn.closed = True
            hub._unregister_camera(conn)
            hub._log(f"Takeloom Camera '{info.client_name}' ({ip}) disconnected")

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
            conn.info.status = {k: v for k, v in msg.items() if k != "kind"}
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

    def _handle_upload(self, sock: socket.socket, device_id: str) -> None:
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
        offset = part.stat().st_size if part.exists() else 0
        if offset > size:
            part.unlink()
            offset = 0
        with hub._lock:
            up.state, up.size, up.received, up.last_progress = "uploading", size, offset, time.monotonic()
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

    def _expect_end_and_ack(self, sock: socket.socket, up: _ExpectedUpload) -> None:
        frame_type, payload = read_frame(sock)
        if frame_type == FRAME_JSON and decode_json(payload).get("kind") == "upload_end":
            self._send({"kind": "upload_done", "ok": True})
