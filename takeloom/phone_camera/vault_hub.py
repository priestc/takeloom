"""VaultCameraHub: the Takeloom Camera phones, by way of the vault.

The phones connect to the vault now (vault_server/takeloom_vault/
camera_hub.py: it's wired to the router, this recording node usually
isn't), not to this machine. This is what the backend sees in place of
PhoneCameraHub (hub.py): the same queries and controls, over one
connection to the vault's camera hub (its docstring has the messages) —
the phones' statuses and previews come down it, and what the backend tells
a phone goes up it to be passed on.

What's different from a phone connected here:

- a session's live feed is recorded on the vault, straight into the
  session's folder there, not here (VaultPhoneRecorder); the vault lines
  it up by clock, and the phone's own recording uploads there too;
- every phone time converts to the vault's clock, not this machine's
  (the phones sync with the vault): this connection pings the vault the
  same NTP way the phones do, and a session's mix start is logged on the
  vault's clock (session_log_info), so everything that lines a recording
  up works unchanged;
- no live video reaches this machine, so no full-resolution preview of a
  phone, and no streaming from here (the vault streams).

The same connection carries the vault's web UI's operations on this node
(cameras, projects and setlists: node_rpc.py), which is how this node is
controlled from anywhere else now that remote mode is gone.

`takeloom server` uses this instead of listening for phones itself
whenever its remote vault says it has a camera hub (`camera_hub` in its
ping).
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .app_version import version_status
from .hub import PhoneCameraInfo
from .models import display_name, model_name
from .protocol import (
    DEVICE_PREFIX, FRAME_JSON, FRAME_PREVIEW, ProtocolError, decode_json, encode_json, read_frame,
)

_RETRY_S = 5.0
_PING_EVERY_S = 2.0
_PING_SAMPLES = 16


def _info(d: dict) -> PhoneCameraInfo:
    return PhoneCameraInfo(
        device_id=str(d.get("device_id") or ""), client_name=str(d.get("client_name") or ""),
        platform=str(d.get("platform") or ""), ip=str(d.get("ip") or ""),
        app_version=str(d.get("app_version") or ""), device_model=str(d.get("device_model") or ""),
        status=d.get("status") or {}, clock_offset_ns=d.get("clock_offset_ns"),
        clock_rtt_ns=d.get("clock_rtt_ns"), live_latency_ns=d.get("live_latency_ns"),
    )


class VaultCameraHub:
    def __init__(self, vault_url: str, token: str, client_name: str, log: Callable[[str], None] | None = None) -> None:
        parts = urlsplit(vault_url)
        self.host = parts.hostname or ""
        self.port = parts.port or 51824
        self.token = token
        self.client_name = client_name
        self._log = log or (lambda msg: None)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._cameras: dict[str, PhoneCameraInfo] = {}
        self._sock: socket.socket | None = None
        self._write_lock = threading.Lock()
        self._stopping = threading.Event()
        self._connected = False
        # vault_ns = node_ns + _offset_ns, from the lowest-RTT recent ping
        self._offset_ns: int | None = None
        self._samples: list[tuple[int, int]] = []  # (rtt, offset)
        self._ping_id = 0
        self._recording_starts: dict[tuple[str, str], int] = {}
        self.backend = None  # the LocalBackend it's attached to: what the vault's web UI runs operations on
        self._uploads: dict[str, Path] = {}
        self._live_starts: dict[tuple[str, str], tuple[int, int | None]] = {}
        self._on_preview: Callable[[str, bytes], None] = lambda _d, _j: None
        self._on_change: Callable[[], None] = lambda: None
        self._on_live_change: Callable[[], None] = lambda: None
        self._on_framing: Callable[[str, dict], None] = lambda _d, _m: None
        self._on_status: Callable[[str, dict], None] = lambda _d, _s: None

    # --- lifecycle ---

    def start(self) -> None:
        threading.Thread(target=self._run, name="vault-camera-hub", daemon=True).start()
        threading.Thread(target=self._pinger, name="vault-clock", daemon=True).start()

    def stop(self) -> None:
        self._stopping.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def set_callbacks(self, on_preview, on_change, session_dir_resolver=None, on_live_change=lambda: None,
                      on_framing=lambda _d, _m: None, on_status=lambda _d, _s: None, **_uploads) -> None:
        """PhoneCameraHub's; the upload hooks don't apply (uploads go to the
        vault, which lines them up)."""
        self._on_preview = on_preview
        self._on_change = on_change
        self._on_live_change = on_live_change
        self._on_framing = on_framing
        self._on_status = on_status

    def log(self, message: str) -> None:
        self._log(message)

    # --- the connection ---

    def _send(self, msg: dict) -> bool:
        sock = self._sock
        if sock is None:
            return False
        try:
            with self._write_lock:
                sock.sendall(encode_json(msg))
            return True
        except OSError:
            return False

    def _run(self) -> None:
        reported = ""
        while not self._stopping.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=10)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.sendall(encode_json({"kind": "hello", "role": "node", "token": self.token,
                                          "client_name": self.client_name}))
                frame_type, payload = read_frame(sock)
                ack = decode_json(payload) if frame_type == FRAME_JSON else {}
                if not ack.get("ok"):
                    raise ConnectionError(ack.get("error") or "the vault refused this machine")
                sock.settimeout(None)
                with self._lock:
                    self._sock, self._connected = sock, True
                    self._samples.clear()
                self._log(f"Takeloom Camera: connected to the vault's camera hub at {self.host}:{self.port}")
                reported = ""
                self._ping_once()
                while True:
                    frame_type, payload = read_frame(sock)
                    if frame_type == FRAME_JSON:
                        self._on_message(decode_json(payload))
                    elif frame_type == FRAME_PREVIEW and payload:
                        n = payload[0]
                        device_id = payload[1:1 + n].decode("utf-8", errors="replace")
                        self._on_preview(device_id, payload[1 + n:])
            except (OSError, ConnectionError, ProtocolError) as e:
                if self._stopping.is_set():
                    return
                why = f"{type(e).__name__}: {e}" if not isinstance(e, ConnectionError) else str(e)
                if why != reported:
                    self._log(f"Takeloom Camera: no connection to the vault's camera hub at {self.host}:{self.port} "
                              f"({why}); retrying every {_RETRY_S:.0f} s")
                    reported = why
            with self._lock:
                had = bool(self._cameras)
                self._sock, self._connected = None, False
                self._cameras.clear()
                self._cond.notify_all()
            if had:
                self._on_change()
            self._stopping.wait(_RETRY_S)

    def _on_message(self, msg: dict) -> None:
        kind = msg.get("kind")
        if kind == "clock_pong":
            now = time.monotonic_ns()
            t0, ts = int(msg.get("t0") or 0), int(msg.get("ts") or 0)
            rtt = now - t0
            offset = ts - (t0 + rtt // 2)
            with self._lock:
                self._samples = (self._samples + [(rtt, offset)])[-_PING_SAMPLES:]
                self._offset_ns = min(self._samples)[1]
        elif kind == "cameras":
            with self._lock:
                self._cameras = {c.device_id: c for c in (_info(d) for d in msg.get("cameras") or []) if c.device_id}
            self._on_change()
        elif kind == "camera_status":
            info = _info(msg.get("camera") or {})
            with self._lock:
                old = self._cameras.get(info.device_id)
                self._cameras[info.device_id] = info
            keys = ("live_adaptive", "height", "zoom_caps")  # hub.py's _LIVE_STATUS_KEYS
            if old is None or any(old.status.get(k) != info.status.get(k) for k in keys):
                self._on_live_change()
            self._on_status(info.device_id, info.status)
        elif kind == "camera_framing":
            threading.Thread(target=self._on_framing, args=(str(msg.get("device_id") or ""), msg.get("msg") or {}),
                             name="phone-framing", daemon=True).start()
        elif kind == "recording_started":
            with self._lock:
                self._recording_starts[(str(msg.get("session_id")), str(msg.get("device_id")))] = \
                    int(msg.get("video_start_pts_ns") or 0)
        elif kind == "live_started":
            with self._lock:
                self._live_starts[(str(msg.get("session_id")), str(msg.get("device_id")))] = (
                    int(msg.get("first_live_pts_ns") or 0), msg.get("first_live_clock_offset_ns"))
        elif kind == "recording_failed":
            self._log(f"Takeloom Camera {msg.get('device_id')} recording failed: {msg.get('error')}")
        elif kind == "rpc":
            threading.Thread(target=self._run_rpc, args=(msg,), name="vault-rpc", daemon=True).start()
        elif kind == "file_chunk":
            self._file_chunk(msg)
        elif kind == "control":
            if not msg.get("active") and msg.get("by"):
                self._log(f"Takeloom Camera: {msg['by']} has taken charge of the phones; "
                          "they'll get this machine's settings again when it next records")

    # --- the vault's web UI, running operations here (node_rpc.py) ---

    def _file_chunk(self, msg: dict) -> None:
        """A file from the browser (a backing track to add), in base64
        pieces ahead of the operation that uses it ("upload": its id)."""
        import base64
        import tempfile
        upload_id = str(msg.get("id") or "")
        if not upload_id.isalnum():
            return
        with self._lock:
            path = self._uploads.get(upload_id)
            if path is None:
                name = Path(str(msg.get("name") or "upload")).name or "upload"
                path = Path(tempfile.mkdtemp(prefix="takeloom-upload-")) / name
                self._uploads[upload_id] = path
        with open(path, "ab") as f:
            f.write(base64.b64decode(str(msg.get("data") or "")))

    def _run_rpc(self, msg: dict) -> None:
        from ..backend import BackendError
        from ..node_rpc import dispatch
        args = dict(msg.get("args") or {})
        upload = None
        if args.get("upload"):
            with self._lock:
                upload = self._uploads.pop(str(args.pop("upload")), None)
            if upload is None:
                self._send({"kind": "rpc_result", "id": msg.get("id"), "ok": False, "error": "The upload didn't arrive."})
                return
            args["source_path"] = str(upload)
        try:
            if self.backend is None:
                raise BackendError("This recording node isn't ready yet.")
            result = dispatch(self.backend, str(msg.get("op") or ""), args)
            reply = {"kind": "rpc_result", "id": msg.get("id"), "ok": True, "result": result}
        except Exception as e:  # noqa: BLE001 — every failure goes back to the browser
            reply = {"kind": "rpc_result", "id": msg.get("id"), "ok": False,
                     "error": str(e) if isinstance(e, BackendError) else f"{type(e).__name__}: {e}"}
        finally:
            if upload is not None:
                import shutil
                shutil.rmtree(upload.parent, ignore_errors=True)
        if not self._send(reply):
            self._log(f"Couldn't send the result of {msg.get('op')} back to the vault")

    def _ping_once(self) -> None:
        self._ping_id += 1
        self._send({"kind": "clock_ping", "id": self._ping_id, "t0": time.monotonic_ns()})

    def _pinger(self) -> None:
        while not self._stopping.wait(_PING_EVERY_S):
            if self._connected:
                self._ping_once()

    def to_vault_ns(self, node_ns: int | None) -> int | None:
        """A time on this machine's monotonic clock on the vault's (the
        phones' clocks are synced to the vault's)."""
        with self._lock:
            offset = self._offset_ns
        return None if node_ns is None or offset is None else node_ns + offset

    # --- PhoneCameraHub's queries ---

    def cameras(self) -> list[PhoneCameraInfo]:
        with self._lock:
            return list(self._cameras.values())

    def camera(self, device_id: str) -> PhoneCameraInfo | None:
        with self._lock:
            return self._cameras.get(device_id)

    def camera_versions(self) -> dict[str, dict]:
        return {DEVICE_PREFIX + c.device_id: version_status(c.app_version) for c in self.cameras()}

    def list_camera_devices(self) -> list[tuple[str, str]]:
        return [(DEVICE_PREFIX + c.device_id, f"Takeloom Camera: {display_name(c.client_name, c.platform, c.device_model)}")
                for c in self.cameras()]

    # --- controls ---

    def send(self, device_id: str, msg: dict) -> bool:
        return self._send({"kind": "send", "device_id": device_id, "msg": msg})

    def start_recording(self, device_id: str, session_id: str, live_file: str = "", upload_file: str = "") -> bool:
        return self._send({"kind": "start_recording", "device_id": device_id, "session_id": session_id,
                           "live_file": live_file, "upload_file": upload_file})

    def claim(self) -> bool:
        """Take charge of the phones (the vault passes on only the node in
        charge's settings: see its camera_hub.py). Ordered before anything
        sent after it on the same connection."""
        return self._send({"kind": "claim"})

    def stop_recording(self, device_id: str, session_id: str) -> None:
        self._send({"kind": "stop_recording", "device_id": device_id, "session_id": session_id})

    def recording_start_pts(self, session_id: str, device_id: str) -> int | None:
        with self._lock:
            return self._recording_starts.get((session_id, device_id))

    def live_start(self, session_id: str, device_id: str) -> tuple[int | None, int | None]:
        with self._lock:
            return self._live_starts.get((session_id, device_id), (None, None))

    def recorder(self, device_id: str, session_id: str, live_file: str, upload_file: str) -> "VaultPhoneRecorder":
        return VaultPhoneRecorder(self, device_id, session_id, live_file, upload_file)

    # No live video here (see the module docstring).
    def set_video_sink(self, device_id: str, sink) -> None:
        pass

    def add_video_tap(self, device_id: str, tap) -> None:
        pass

    def remove_video_tap(self, device_id: str, tap) -> None:
        pass

    # Uploads are the vault's: nothing here waits for one.
    def expect_upload(self, *args, **kwargs) -> None:
        pass

    def wait_for_upload(self, *args, **kwargs):
        return None

    def upload_went_to_vault(self, session_id: str, device_id: str) -> bool:
        return True

    def forget_upload(self, session_id: str, device_id: str) -> None:
        with self._lock:
            self._recording_starts.pop((session_id, device_id), None)
            self._live_starts.pop((session_id, device_id), None)


class VaultPhoneRecorder:
    """PhoneVideoRecorder's stand-in (recorder.py) for a phone on the vault:
    the live feed is recorded there, into the session's folder as
    `live_file`, and the phone's own recording uploads there as
    `upload_file`. Nothing is written here."""

    def __init__(self, hub: VaultCameraHub, device_id: str, session_id: str, live_file: str, upload_file: str) -> None:
        self.hub = hub
        self.device_id = device_id
        self.session_id = session_id
        self.live_file = live_file
        self.upload_file = upload_file
        self._running = False

    def start(self) -> bool:
        if self.hub.camera(self.device_id) is None:
            return False
        self._running = self.hub.start_recording(self.device_id, self.session_id, self.live_file, self.upload_file)
        return self._running

    def stop(self, timeout: float = 10.0) -> bool:
        self.hub.stop_recording(self.device_id, self.session_id)
        self._running = False
        return True

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def first_live_pts_ns(self) -> int | None:
        return self.hub.live_start(self.session_id, self.device_id)[0]

    def session_log_info(self, mix_start_ns: int | None) -> dict:
        """recorder.py's, with the mix start on the vault's clock — the one
        the phones' clock offsets are to."""
        info = self.hub.camera(self.device_id)
        first_live, first_offset = self.hub.live_start(self.session_id, self.device_id)
        return {
            "device_id": self.device_id,
            "client_name": info.client_name if info else "",
            "model": model_name(info.platform, info.device_model) if info else "",
            "session_id": self.session_id,
            "mix_start_server_ns": self.hub.to_vault_ns(mix_start_ns),
            "phone_video_start_pts_ns": self.hub.recording_start_pts(self.session_id, self.device_id),
            "first_live_pts_ns": first_live,
            "first_live_clock_offset_ns": first_offset,
            "clock_offset_ns": info.clock_offset_ns if info else None,
            "clock_rtt_ns": info.clock_rtt_ns if info else None,
            "logged_at_ns": self.hub.to_vault_ns(time.monotonic_ns()),
            "clock": "vault",
        }
