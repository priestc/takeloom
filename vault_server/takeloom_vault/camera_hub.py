"""The Takeloom Camera phones' server, here on the vault.

The phones used to connect to the studio machine's `takeloom server`
(takeloom/phone_camera/hub.py). Now they connect here: the vault is wired
to the router, the recording nodes (the Mac Mini, the laptop) usually
aren't, so the phones' live feeds and uploads never cross a node's Wi-Fi.
The same wire protocol as before (takeloom/phone_camera/protocol.py, copied
below: KEEP IN STEP), on the vault's own port — server.py tells a phone's
binary frames from a browser's HTTP by the first byte (frame_first_byte) —
so the app needs no update: it finds the vault with its network scan, like
it found the studio machine, and pairs once (approved on the vault's web
page like any device; the app keeps a token per server).

What a phone does here:

- pairs and authenticates with this vault's own tokens (auth.py);
- keeps its clock in sync with this machine's (clock_ping/clock_pong, on
  time.monotonic_ns() here): every capture time it sends converts to this
  clock;
- sends its status, previews and live H.264 feed; during a session that
  feed is recorded here, into the session's folder, as the camera's live
  source (LiveRecorder; align.add_live_sources lines it up by clock);
- records its own full-quality copy when told, and uploads it straight
  into the session's folder afterwards (over HTTP to this vault, or over
  this connection for an app too old for that), where align.py lines it up.

A recording node ("node": a takeloom instance with audio devices plugged
in) connects too, with its vault token, and is told about every phone —
connections, statuses, previews, framing — and tells the phones what to do
through here: start/stop a session's recording, live quality, framing,
battery estimates (takeloom/phone_camera/vault_hub.py, its other end).
Messages on a node's connection:

    node -> vault  {"kind": "hello", "role": "node", "token": "...", "client_name": "..."}
    vault -> node  {"kind": "hello_ack", "ok": true|false, "error"?, "hostname"}
                   {"kind": "cameras", "cameras": [camera, ...]}    whenever one connects/leaves
                   {"kind": "camera_status", "camera": camera}       every status a phone sends
                   {"kind": "camera_framing", "device_id", "msg"}    a lens picked on the phone
                   {"kind": "recording_started", "device_id", "session_id", "video_start_pts_ns"}
                   {"kind": "recording_failed", "device_id", "session_id", "error"}
                   {"kind": "live_started", "device_id", "session_id",
                    "first_live_pts_ns", "first_live_clock_offset_ns"}
                   {"kind": "clock_pong", "id", "t0", "ts"}
                   FRAME_PREVIEW: 1 byte n + n bytes device id + a JPEG
    node -> vault  {"kind": "clock_ping", "id", "t0"}               (the node's clock vs this one)
                   {"kind": "send", "device_id", "msg"}              passed on to the phone
                   {"kind": "start_recording", "device_id", "session_id", "live_file", "upload_file"}
                   {"kind": "stop_recording", "device_id", "session_id"}

A camera, as a node is told about it: {"device_id", "client_name",
"platform", "ip", "app_version", "device_model", "status",
"clock_offset_ns", "clock_rtt_ns", "live_latency_ns"}.
"""

from __future__ import annotations

import io
import json
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .auth import Auth
from .store import Vault

# --- takeloom/phone_camera/protocol.py (keep in step) ---

FRAME_JSON = 1
FRAME_VIDEO = 2
FRAME_PREVIEW = 3
FRAME_UPLOAD = 4
MAX_FRAME_BYTES = 32 * 1024 * 1024
_HEADER = struct.Struct(">BI")
_VIDEO_HEADER = struct.Struct(">qB")


def frame_first_byte(byte: int) -> bool:
    """Whether a connection's first byte starts one of this protocol's
    frames (an HTTP request starts with a letter)."""
    return byte in (FRAME_JSON, FRAME_VIDEO, FRAME_PREVIEW, FRAME_UPLOAD)


class ProtocolError(Exception):
    pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 1024 * 1024))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return bytes(buf)


def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    frame_type, length = _HEADER.unpack(_recv_exact(sock, _HEADER.size))
    if length > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame of {length} bytes exceeds the {MAX_FRAME_BYTES}-byte limit")
    return frame_type, _recv_exact(sock, length)


def encode_frame(frame_type: int, payload: bytes) -> bytes:
    return _HEADER.pack(frame_type, len(payload)) + payload


def encode_json(msg: dict) -> bytes:
    return encode_frame(FRAME_JSON, json.dumps(msg).encode("utf-8"))


def decode_json(payload: bytes) -> dict:
    try:
        msg = json.loads(payload.decode("utf-8"))
    except ValueError as e:
        raise ProtocolError(f"bad JSON frame: {e}") from e
    if not isinstance(msg, dict):
        raise ProtocolError("JSON frame is not an object")
    return msg


def decode_video(payload: bytes) -> tuple[int, bool, bytes]:
    if len(payload) < _VIDEO_HEADER.size:
        raise ProtocolError("truncated video frame")
    pts_ns, flags = _VIDEO_HEADER.unpack_from(payload)
    return pts_ns, bool(flags & 1), payload[_VIDEO_HEADER.size:]


def encode_node_preview(device_id: str, jpeg: bytes) -> bytes:
    name = device_id.encode("utf-8")[:255]
    return encode_frame(FRAME_PREVIEW, bytes([len(name)]) + name + jpeg)


# How big each chunk of an HTTP upload is (one PUT), as the studio told phones.
HTTP_CHUNK_BYTES = 8 * 1024 * 1024
# Where a node said each phone's recording of a session goes, so an upload
# finds its place even after this server restarts.
REGISTRY_FILE = "camera_sessions.json"
_LIVE_REPORT_AFTER_S = 10.0
# About 10 s of 30 fps video: past that the live recorder's ffmpeg is
# hopelessly behind; drop until the next keyframe rather than grow forever.
_MAX_QUEUED_FRAMES = 300


class LiveRecorder:
    """A phone's live feed during a session, into the session's folder:
    its H.264 copied as it comes (no re-encode) into fragmented MP4, so the
    file is playable however it ends. Raw H.264 carries no timestamps, so
    ffmpeg stamps each frame on arrival — which is why the phone's own
    recording replaces it once it's here (takeloom's phone_camera/
    recorder.py did the same on the studio machine)."""

    def __init__(self, path: Path, on_first_frame: Callable[[int], None], log: Callable[[str], None]) -> None:
        self.path = path
        self._on_first_frame = on_first_frame
        self._log = log
        self._proc: subprocess.Popen | None = None
        self._queue: list = []
        self._cond = threading.Condition()
        self._stopping = False
        self._started = False
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._proc = subprocess.Popen(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-use_wallclock_as_timestamps", "1", "-thread_queue_size", "1024",
                 "-probesize", "1M", "-analyzeduration", "500000", "-f", "h264", "-i", "pipe:0",
                 "-map", "0:v", "-c:v", "copy", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                 str(self.path)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        except OSError as e:
            self._log(f"Couldn't record {self.path.name}: {e}")
            return False
        self._thread = threading.Thread(target=self._write, name="live-recorder", daemon=True)
        self._thread.start()
        return True

    def push(self, pts_ns: int, keyframe: bool, data: bytes) -> None:
        with self._cond:
            if self._stopping or (len(self._queue) >= _MAX_QUEUED_FRAMES and not keyframe):
                return
            self._queue.append((pts_ns, keyframe, data))
            self._cond.notify()

    def _write(self) -> None:
        proc = self._proc
        dropping = False
        try:
            while True:
                with self._cond:
                    while not self._queue and not self._stopping:
                        self._cond.wait()
                    if not self._queue:
                        return
                    pts_ns, keyframe, data = self._queue.pop(0)
                    behind = len(self._queue) >= _MAX_QUEUED_FRAMES
                if behind:
                    dropping = True
                if dropping or not self._started:
                    if not keyframe:
                        continue
                    dropping = False
                if not self._started:
                    self._started = True
                    self._on_first_frame(pts_ns)
                proc.stdin.write(data)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()  # EOF: ffmpeg finishes the file
            except OSError:
                pass

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        proc = self._proc
        if proc is None:
            return
        try:
            proc.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        err = (proc.stderr.read() or b"").decode(errors="replace").strip() if proc.stderr else ""
        if proc.returncode not in (0, None) or err:
            self._log(f"Live recording {self.path.name} ended with ffmpeg {proc.returncode}: {err[-300:]}")


class _Camera:
    def __init__(self, sock: socket.socket, hello: dict, ip: str) -> None:
        self.sock = sock
        self.device_id = str(hello.get("device_id") or "")
        self.client_name = str(hello.get("client_name") or ip)
        self.platform = str(hello.get("platform") or "")
        self.app_version = str(hello.get("app_version") or "")
        self.device_model = str(hello.get("device_model") or "")
        self.ip = ip
        self.status: dict = {}
        self.clock_offset_ns: int | None = None
        self.clock_rtt_ns: int | None = None
        self.live_latency_ns: int | None = None
        self.connected_at = time.monotonic()
        self.video_frames = 0
        self.video_bytes = 0
        self.last_frame_at = 0.0
        self.live_reported = False
        self.closed = False
        self.replaced = False
        self.preview: bytes = b""  # the latest preview JPEG, for the web page
        self._write_lock = threading.Lock()

    def info(self) -> dict:
        return {
            "device_id": self.device_id, "client_name": self.client_name, "platform": self.platform,
            "ip": self.ip, "app_version": self.app_version, "device_model": self.device_model,
            "status": self.status, "clock_offset_ns": self.clock_offset_ns, "clock_rtt_ns": self.clock_rtt_ns,
            "live_latency_ns": self.live_latency_ns,
        }

    def send_raw(self, data: bytes) -> bool:
        try:
            with self._write_lock:
                self.sock.sendall(data)
            return True
        except OSError:
            return False

    def send_json(self, msg: dict) -> bool:
        return self.send_raw(encode_json(msg))

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _Node:
    def __init__(self, sock: socket.socket, label: str, ip: str) -> None:
        self.sock = sock
        self.label = label
        self.ip = ip
        self._write_lock = threading.Lock()

    def send_raw(self, data: bytes) -> bool:
        try:
            with self._write_lock:
                self.sock.sendall(data)
            return True
        except OSError:
            return False

    def send_json(self, msg: dict) -> bool:
        return self.send_raw(encode_json(msg))


class CameraHub:
    def __init__(self, vault: Vault, auth: Auth, session_cameras, aligner, hostname: Callable[[], str],
                 log: Callable[[str], None]) -> None:
        self.vault = vault
        self.auth = auth
        self.session_cameras = session_cameras  # camera_uploads.CameraUploads(in_session=True)
        self.aligner = aligner
        self._hostname = hostname
        self._log = log
        self._lock = threading.Lock()
        self._cameras: dict[str, _Camera] = {}
        self._nodes: list[_Node] = []
        self._pending_ips: set[str] = set()
        # (session_id, device_id) -> LiveRecorder, while a session records
        self._live: dict[tuple[str, str], LiveRecorder] = {}
        self._registry_lock = threading.Lock()

    # --- queries (the web page) ---

    def cameras(self) -> list[dict]:
        with self._lock:
            return [c.info() for c in self._cameras.values()]

    def preview(self, device_id: str) -> bytes:
        with self._lock:
            cam = self._cameras.get(device_id)
            return cam.preview if cam else b""

    def nodes(self) -> list[dict]:
        with self._lock:
            return [{"label": n.label, "ip": n.ip} for n in self._nodes]

    def recording(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._live)

    # --- where each phone's recording goes ---

    def _registry(self) -> dict:
        try:
            data = json.loads((self.vault.root / REGISTRY_FILE).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _register(self, session_id: str, device_id: str, live_file: str, upload_file: str) -> None:
        from .store import _atomic_write
        with self._registry_lock:
            data = self._registry()
            data.setdefault(session_id, {})[device_id] = {"live_file": live_file, "upload_file": upload_file,
                                                         "at": time.time()}
            # A month is plenty for any phone to get its upload in.
            cutoff = time.time() - 31 * 86400
            data = {s: d for s, d in data.items()
                    if any(isinstance(v, dict) and v.get("at", 0) > cutoff for v in d.values())}
            _atomic_write(self.vault.root / REGISTRY_FILE, json.dumps(data, indent=2).encode())

    def upload_file_for(self, session_id: str, device_id: str) -> str | None:
        entry = (self._registry().get(session_id) or {}).get(device_id)
        if isinstance(entry, dict) and entry.get("upload_file"):
            return str(entry["upload_file"])
        try:
            data = self.vault.read_session_log(session_id) or {}
        except Exception:  # noqa: BLE001 — a bad session name
            return None
        for cam in data.get("extra_cameras") or []:
            if (cam.get("phone_camera") or {}).get("device_id") == device_id and cam.get("upload_file"):
                return str(cam["upload_file"])
        return None

    # --- broadcasting to nodes ---

    def _to_nodes(self, data: bytes) -> None:
        with self._lock:
            nodes = list(self._nodes)
        for node in nodes:
            node.send_raw(data)

    def _cameras_changed(self) -> None:
        self._to_nodes(encode_json({"kind": "cameras", "cameras": self.cameras()}))

    # --- a connection (server.py hands over every one that isn't HTTP) ---

    def handle(self, sock: socket.socket, ip: str) -> None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(None)
        try:
            frame_type, payload = read_frame(sock)
            if frame_type != FRAME_JSON:
                return
            hello = decode_json(payload)
            if hello.get("kind") == "discover":
                sock.sendall(encode_json({"kind": "discover_ack", "server": "takeloom", "hostname": self._hostname()}))
                return
            if hello.get("kind") != "hello":
                return
            if hello.get("role") == "node":
                self._handle_node(sock, hello, ip)
                return
            client = self._authenticate(sock, hello, ip)
            if client is None:
                return
            if not str(hello.get("device_id") or ""):
                return
            if hello.get("role") == "upload":
                self._handle_upload(sock, hello, ip, client)
            else:
                self._handle_camera(sock, hello, ip)
        except (OSError, ConnectionError, ProtocolError):
            pass

    def _authenticate(self, sock: socket.socket, hello: dict, ip: str):
        """The phone's paired client here, pairing it first (approved on the
        web page) if its token isn't one of this vault's."""
        token = str(hello.get("token") or "")
        client = self.auth.authenticate(token, ip) if token else None
        if client is not None:
            sock.sendall(encode_json({"kind": "hello_ack", "ok": True, "hostname": self._hostname()}))
            return client
        with self._lock:
            if ip in self._pending_ips:
                sock.sendall(encode_json({"kind": "hello_ack", "ok": False,
                                          "error": "A request from this address is already pending approval."}))
                return None
            self._pending_ips.add(ip)
        try:
            name = f"{hello.get('client_name') or ip} (Takeloom Camera)"
            request = self.auth.request_pairing(name, ip)
            sock.sendall(encode_json({"kind": "hello_pending", "code": request.code}))
            self._log(f"Pairing request from {name} at {ip}: approve it on the vault's web page (code {request.code})")
            while True:
                result = self.auth.poll(request.id, ip)
                status = result.get("status")
                if status == "approved" and result.get("token"):
                    new_token = result["token"]
                    sock.sendall(encode_json({"kind": "hello_ack", "ok": True, "hostname": self._hostname(),
                                              "token": new_token}))
                    return self.auth.authenticate(new_token, ip)
                if status in ("denied", "expired", None):
                    sock.sendall(encode_json({"kind": "hello_ack", "ok": False,
                                              "error": "Authorization denied." if status == "denied"
                                              else "The pairing request expired."}))
                    return None
                time.sleep(1.0)
        finally:
            with self._lock:
                self._pending_ips.discard(ip)

    # --- a node ---

    def _handle_node(self, sock: socket.socket, hello: dict, ip: str) -> None:
        client = self.auth.authenticate(str(hello.get("token") or ""), ip)
        if client is None:
            sock.sendall(encode_json({"kind": "hello_ack", "ok": False,
                                      "error": "Unrecognized vault token: pair this machine with the vault first."}))
            return
        node = _Node(sock, str(hello.get("client_name") or client.label), ip)
        node.send_json({"kind": "hello_ack", "ok": True, "hostname": self._hostname()})
        with self._lock:
            self._nodes.append(node)
        self._log(f"Recording node {node.label} ({ip}) connected")
        node.send_json({"kind": "cameras", "cameras": self.cameras()})
        try:
            while True:
                frame_type, payload = read_frame(sock)
                if frame_type == FRAME_JSON:
                    self._on_node_message(node, decode_json(payload))
        except (OSError, ConnectionError, ProtocolError) as e:
            reason = "it closed the connection" if str(e) == "connection closed" else f"{type(e).__name__}: {e}"
            self._log(f"Recording node {node.label} ({ip}) disconnected: {reason}")
        finally:
            with self._lock:
                if node in self._nodes:
                    self._nodes.remove(node)

    def _on_node_message(self, node: _Node, msg: dict) -> None:
        kind = msg.get("kind")
        if kind == "clock_ping":
            node.send_json({"kind": "clock_pong", "id": msg.get("id"), "t0": msg.get("t0"),
                            "ts": time.monotonic_ns()})
        elif kind == "send":
            self.send(str(msg.get("device_id") or ""), msg.get("msg") or {})
        elif kind == "start_recording":
            self.start_recording(str(msg.get("device_id") or ""), str(msg.get("session_id") or ""),
                                 str(msg.get("live_file") or ""), str(msg.get("upload_file") or ""))
        elif kind == "stop_recording":
            self.stop_recording(str(msg.get("device_id") or ""), str(msg.get("session_id") or ""))

    def send(self, device_id: str, msg: dict) -> bool:
        with self._lock:
            cam = self._cameras.get(device_id)
        return cam.send_json(msg) if cam is not None and isinstance(msg, dict) else False

    def start_recording(self, device_id: str, session_id: str, live_file: str, upload_file: str) -> None:
        try:
            directory = self.vault.session_dir(session_id)
            if live_file:
                self.session_cameras.path(session_id, live_file)  # a proper file name
            if upload_file:
                self.session_cameras.path(session_id, upload_file)
        except Exception as e:  # noqa: BLE001
            self._log(f"Refused to record {device_id} for {session_id!r}: {e}")
            return
        self._register(session_id, device_id, live_file, upload_file)
        if live_file:
            def first(pts_ns: int) -> None:
                with self._lock:
                    cam = self._cameras.get(device_id)
                offset = cam.clock_offset_ns if cam else None
                self._to_nodes(encode_json({"kind": "live_started", "device_id": device_id, "session_id": session_id,
                                            "first_live_pts_ns": pts_ns, "first_live_clock_offset_ns": offset}))
            recorder = LiveRecorder(directory / live_file, first, self._log)
            if recorder.start():
                with self._lock:
                    old = self._live.pop((session_id, device_id), None)
                    self._live[(session_id, device_id)] = recorder
                if old is not None:
                    old.stop()
        self.send(device_id, {"kind": "request_keyframe"})
        self.send(device_id, {"kind": "start_recording", "session_id": session_id})
        self._log(f"Recording {device_id} for session {session_id}")

    def stop_recording(self, device_id: str, session_id: str) -> None:
        self.send(device_id, {"kind": "stop_recording", "session_id": session_id})
        with self._lock:
            recorder = self._live.pop((session_id, device_id), None)
        if recorder is not None:
            threading.Thread(target=self._finish_live, args=(recorder, session_id), daemon=True).start()

    def _finish_live(self, recorder: LiveRecorder, session_id: str) -> None:
        recorder.stop()
        # The session's log may be here already: line the live feed up now.
        if self.vault.read_session_log(session_id) is not None:
            self.aligner.enqueue(session_id)

    # --- a phone's live connection ---

    def _handle_camera(self, sock: socket.socket, hello: dict, ip: str) -> None:
        cam = _Camera(sock, hello, ip)
        with self._lock:
            old = self._cameras.get(cam.device_id)
            self._cameras[cam.device_id] = cam
        if old is not None:
            old.replaced = True
            old.close()
        self._log(f"Takeloom Camera '{cam.client_name}' ({ip}) connected, app {cam.app_version or 'version unknown'}")
        self._cameras_changed()
        end_reason = "the phone closed it"
        try:
            while not cam.closed:
                try:
                    frame_type, payload = read_frame(sock)
                except (OSError, ConnectionError) as e:
                    if str(e) != "connection closed":
                        end_reason = f"network error: {type(e).__name__}: {e}"
                    raise
                except ProtocolError as e:
                    end_reason = f"bad data from the phone: {e}"
                    raise
                cam.last_frame_at = time.monotonic()
                if frame_type == FRAME_VIDEO:
                    pts_ns, keyframe, data = decode_video(payload)
                    cam.video_frames += 1
                    cam.video_bytes += len(data)
                    if cam.clock_offset_ns is not None:
                        latency = time.monotonic_ns() - (pts_ns + cam.clock_offset_ns)
                        prev = cam.live_latency_ns
                        cam.live_latency_ns = latency if prev is None else (prev * 15 + latency) // 16
                    with self._lock:
                        recorders = [r for (s, d), r in self._live.items() if d == cam.device_id]
                    for r in recorders:
                        r.push(pts_ns, keyframe, data)
                elif frame_type == FRAME_PREVIEW:
                    cam.preview = payload
                    self._to_nodes(encode_node_preview(cam.device_id, payload))
                elif frame_type == FRAME_JSON:
                    self._on_camera_message(cam, decode_json(payload))
        finally:
            cam.closed = True
            with self._lock:
                if self._cameras.get(cam.device_id) is cam:
                    del self._cameras[cam.device_id]
            if cam.replaced:
                end_reason = "replaced: the phone opened a new connection"
            now = time.monotonic()
            quiet = f", last data {now - cam.last_frame_at:.1f} s before" if cam.last_frame_at else ", no data at all"
            self._log(f"Takeloom Camera '{cam.client_name}' ({ip}) disconnected — {end_reason} "
                      f"(after {now - cam.connected_at:.1f} s, {cam.video_frames} video frames{quiet})")
            self._cameras_changed()

    def _report_live_feed(self, cam: _Camera) -> None:
        elapsed = time.monotonic() - cam.connected_at
        if cam.live_reported or elapsed < _LIVE_REPORT_AFTER_S:
            return
        cam.live_reported = True
        st = cam.status
        phone = (f"phone reports {st.get('live_width')}x{st.get('live_height')} at "
                 f"{(st.get('live_bitrate') or 0) / 1e6:.1f} Mbps" if "live_width" in st else "phone doesn't say")
        got = (f"{cam.video_frames / elapsed:.0f} fps, {cam.video_bytes * 8 / elapsed / 1e6:.1f} Mbps"
               if cam.video_frames else f"NO live video in {elapsed:.0f} s")
        self._log(f"Takeloom Camera '{cam.client_name}' live feed: {got} ({phone})")

    def _on_camera_message(self, cam: _Camera, msg: dict) -> None:
        kind = msg.get("kind")
        if kind == "clock_ping":
            cam.send_json({"kind": "clock_pong", "id": msg.get("id"), "t0": msg.get("t0"), "ts": time.monotonic_ns()})
        elif kind == "clock_sync":
            cam.clock_offset_ns = int(msg["offset_ns"])
            cam.clock_rtt_ns = int(msg.get("rtt_ns") or 0)
        elif kind == "status":
            cam.status = {k: v for k, v in msg.items() if k != "kind"}
            self._report_live_feed(cam)
            self._to_nodes(encode_json({"kind": "camera_status", "camera": cam.info()}))
        elif kind == "framing":
            self._to_nodes(encode_json({"kind": "camera_framing", "device_id": cam.device_id, "msg": msg}))
        elif kind == "recording_started":
            self._to_nodes(encode_json({"kind": "recording_started", "device_id": cam.device_id,
                                        "session_id": str(msg.get("session_id")),
                                        "video_start_pts_ns": int(msg.get("video_start_pts_ns") or 0)}))
        elif kind == "recording_stopped":
            self._log(f"Takeloom Camera '{cam.client_name}' stopped recording "
                      f"({(msg.get('file_size') or 0) / 1e6:.0f} MB, {msg.get('duration_s', 0):.0f} s)")
        elif kind == "recording_failed":
            self._log(f"Takeloom Camera '{cam.client_name}' recording failed: {msg.get('error')}")
            self._to_nodes(encode_json({"kind": "recording_failed", "device_id": cam.device_id,
                                        "session_id": str(msg.get("session_id")), "error": str(msg.get("error") or "")}))

    # --- an upload ---

    def _handle_upload(self, sock: socket.socket, hello: dict, ip: str, client) -> None:
        device_id = str(hello.get("device_id") or "")
        frame_type, payload = read_frame(sock)
        if frame_type != FRAME_JSON:
            return
        begin = decode_json(payload)
        if begin.get("kind") != "upload_begin":
            return
        session_id = str(begin.get("session_id") or "")
        size = int(begin.get("size") or 0)
        file = self.upload_file_for(session_id, device_id) if session_id else None
        if file is None:
            sock.sendall(encode_json({"kind": "upload_rejected",
                                      "error": f"This vault isn't expecting a recording for session '{session_id}' "
                                               "(recorded elsewhere)."}))
            return
        directory = self.vault.session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        # What lining it up reads (align.py), beside it.
        (directory / (Path(file).stem + ".upload.json")).write_text(json.dumps(begin.get("metadata") or {}, indent=2))
        status = self.session_cameras.status(session_id, file)
        if status["done"]:
            sock.sendall(encode_json({"kind": "upload_resume", "offset": size}))
            frame_type, payload = read_frame(sock)
            if frame_type == FRAME_JSON and decode_json(payload).get("kind") == "upload_end":
                sock.sendall(encode_json({"kind": "upload_done", "ok": True}))
            return
        if hello.get("http_upload"):
            host = sock.getsockname()[0]
            port = sock.getsockname()[1]
            from urllib.parse import quote
            url = f"http://{host}:{port}/api/sessions/{quote(session_id, safe='')}/camera/{quote(file, safe='')}"
            sock.sendall(encode_json({"kind": "upload_redirect", "url": url, "token": str(hello.get("token") or ""),
                                      "chunk_size": HTTP_CHUNK_BYTES}))
            self._log(f"Takeloom Camera recording {session_id}/{file} ({size / 1e9:.2f} GB) from {ip}: uploading")
            return
        # An app too old to upload over HTTP: over this connection, into the
        # same resumable upload an HTTP one makes.
        offset = int(status["received"] or 0) if status.get("size") == size else 0
        sock.sendall(encode_json({"kind": "upload_resume", "offset": offset}))
        self._log(f"Receiving Takeloom Camera recording {session_id}/{file} from {ip} "
                  f"({size / 1e9:.2f} GB{f', resuming at {offset / 1e9:.2f} GB' if offset else ''})")
        while True:
            frame_type, payload = read_frame(sock)
            if frame_type == FRAME_UPLOAD and payload:
                result = self.session_cameras.append(session_id, file, offset, size, io.BytesIO(payload), len(payload))
                offset += len(payload)
                if result.get("done"):
                    self.aligner.arrived(session_id, file)
            elif frame_type == FRAME_JSON and decode_json(payload).get("kind") == "upload_end":
                break
        done = self.session_cameras.status(session_id, file)["done"]
        sock.sendall(encode_json({"kind": "upload_done", "ok": done,
                                  **({} if done else {"error": f"Received {offset} of {size} bytes."})}))
        if done:
            self._log(f"Takeloom Camera recording {session_id}/{file} received in full")
